"""As duas trilhas de processamento, depois que a mensagem já está na inbox.

    trilha 1  evento `recebido` → enriquece → itens → resume → evento `processado`
    trilha 2  janela com item `resumido` → consolida → panorama + itens `consolidado`

Elas são independentes de propósito. Um evento termina quando seus itens estão resumidos;
a consolidação pergunta separadamente quais janelas ficaram pendentes. Amarrar as duas
exigiria mapear evento → janela, e uma janela reúne muitos eventos — 100 mensagens tocando
agosto do mesmo ator gerariam 100 consolidações onde só a última importa.

Por que isto não roda dentro do callback da fila
------------------------------------------------
A ingestão custa milissegundos e o processamento custa segundos por item. Prender o ACK
esperando o modelo faria a fila drenar a passo de tartaruga. A mensagem é confirmada assim
que está durável na inbox, e o que garante a retomada depois de uma queda é o `status` —
não a reentrega do broker, que já não vai acontecer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from analyser.armazenamento import agora_iso
from analyser.armazenamento_postgres import ArmazenamentoPostgres, Ator
from analyser.config import Config
from analyser.enriquecimento import Enriquecedor, ItemEnriquecido
from analyser.modelos import ErroDeValidacao, Modelo, gerar_validado
from analyser.pipeline import Analyser
from analyser.prompts import Prompts
from analyser.schemas import (
    AtorPolitico,
    NotificacaoClassifier,
    RegistroResumo,
    ResumoItem,
    Rodada,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResultadoWorker:
    """O que uma execução do worker fez, para relatório e auditoria."""

    eventos_processados: int = 0
    itens_criados: int = 0
    itens_ja_existentes: int = 0
    resumos_gerados: int = 0
    janelas_consolidadas: int = 0
    panoramas_gravados: int = 0
    avisos: list[str] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)


class Worker:
    """Processa eventos já ingeridos e consolida janelas pendentes."""

    def __init__(
        self,
        conexao: psycopg.Connection,
        modelo: Modelo,
        config: Config,
    ) -> None:
        self.conexao = conexao
        self.modelo = modelo
        self.config = config
        self.enriquecedor = Enriquecedor(conexao)
        self.prompts = Prompts(raiz=config.prompts)
        self.prompts.precarregar()

    # -- trilha 1: eventos --------------------------------------------------------------

    def _eventos_pendentes(self, limite: int) -> list[dict[str, Any]]:
        """Eventos ingeridos e ainda não processados.

        `FOR UPDATE SKIP LOCKED` permite mais de um worker sem que dois peguem o mesmo
        evento: quem chega depois pula as linhas já travadas em vez de esperar.
        """
        with self.conexao.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                """SELECT evento_id, payload_json FROM analyser_inbox_evento
                    WHERE status = 'recebido'
                    ORDER BY evento_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED""",
                (limite,),
            ).fetchall()

    def processar_eventos(self, limite: int = 50) -> ResultadoWorker:
        """Enriquece e resume os eventos pendentes, um commit por evento.

        Um commit por evento, e não um no fim: uma queda no meio preserva tudo o que já foi
        resumido. O resumo é a parte cara, e refazê-lo por causa de um `rollback` amplo
        seria pagar duas vezes pelo mesmo trabalho.
        """
        resultado = ResultadoWorker()

        for linha in self._eventos_pendentes(limite):
            evento_id = linha["evento_id"]
            try:
                evento = NotificacaoClassifier.model_validate(linha["payload_json"])
                itens = self.enriquecedor.enriquecer(evento)

                for item in itens:
                    self._processar_item(item, evento_id, resultado)

                self.conexao.execute(
                    """UPDATE analyser_inbox_evento
                          SET status = 'processado', processado_em = now()
                        WHERE evento_id = %s""",
                    (evento_id,),
                )
                self.conexao.commit()
                resultado.eventos_processados += 1

            except Exception as erro:  # noqa: BLE001 — um evento ruim não derruba o lote
                self.conexao.rollback()
                self.conexao.execute(
                    """UPDATE analyser_inbox_evento
                          SET status = 'erro', tentativas = tentativas + 1, erro = %s
                        WHERE evento_id = %s""",
                    (str(erro)[:2000], evento_id),
                )
                self.conexao.commit()
                resultado.erros.append(f"evento {evento_id}: {erro}")
                logger.exception("Falha ao processar o evento %s", evento_id)

        return resultado

    def _processar_item(
        self, item: ItemEnriquecido, evento_id: int, resultado: ResultadoWorker
    ) -> None:
        """Grava o item e, se for inédito, paga o resumo."""
        if not item.janela_mes:
            resultado.avisos.append(f"{item.item_id}: sem data, item ignorado")
            return

        destino = self._destino(item)
        if destino.gravar_item(item, evento_id):
            resultado.itens_criados += 1
        else:
            resultado.itens_ja_existentes += 1

        resultado.avisos.extend(f"{item.item_id}: {a}" for a in item.avisos)

        # Se já tem resumo, nada a fazer: é o cache que evita pagar o modelo de novo.
        ja_resumido = self.conexao.execute(
            "SELECT resumo IS NOT NULL FROM analyser_item WHERE item_id = %s",
            (item.item_id,),
        ).fetchone()
        if ja_resumido and ja_resumido[0]:
            return

        if not item.texto.strip():
            resultado.avisos.append(f"{item.item_id}: sem texto, resumo não gerado")
            return

        prompt = self.prompts.montar_resumo_item(
            ator_nome=item.ator_nome,
            ator_tipo=item.entity_type,
            item_id=item.item_id,
            veiculo=item.veiculo,
            data=item.data_publicacao.date().isoformat() if item.data_publicacao else "",
            url=item.link_noticia,
            titulo=item.titulo,
            texto=item.texto,
        )
        try:
            saida = gerar_validado(
                self.modelo,
                prompt=prompt.texto,
                schema=ResumoItem,
                tarefa="resumo_item",
                contexto={
                    "item_id": item.item_id,
                    "titulo": item.titulo,
                    "texto": item.texto,
                },
            )
        except ErroDeValidacao as erro:
            resultado.erros.append(f"{item.item_id}: {erro}")
            return

        destino.gravar_resumo(
            RegistroResumo(
                item_id=item.item_id,
                ator_id=item.entity_id,
                janela=item.janela_mes,
                rotulo=item.rotulo,
                veiculo=item.veiculo,
                url=item.link_noticia or "https://sem-link.invalido",
                data=item.data_publicacao.date() if item.data_publicacao else None,
                titulo=item.titulo,
                resumo=saida.dados.resumo,
                temas=saida.dados.temas,
                fonte_id=item.item_id,
                modelo=self.modelo.nome,
                hash_prompt_resumo=prompt.sha256,
                criado_em=agora_iso(),
            )
        )
        resultado.resumos_gerados += 1

    def _destino(self, item: ItemEnriquecido) -> ArmazenamentoPostgres:
        return ArmazenamentoPostgres(
            self.conexao,
            Ator(
                entity_type=item.entity_type,
                ator_id=item.entity_id,
                nome=item.ator_nome,
            ),
        )

    # -- trilha 2: janelas --------------------------------------------------------------

    def _atores_com_janela_pendente(self) -> list[dict[str, Any]]:
        """Quem tem item resumido fora de panorama, e em qual janela.

        Nada de marcador nem de comparação de timestamp: a pergunta é feita ao próprio
        dado, então a resposta não depende de onde um processo anterior caiu.
        """
        with self.conexao.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                """SELECT DISTINCT entity_type, nr_titulo_eleitoral_candidato,
                          nr_partido, ator_nome, janela_mes
                     FROM analyser_item
                    WHERE status = 'resumido'
                    ORDER BY janela_mes, entity_type"""
            ).fetchall()

    def consolidar_janelas(self, limite: int = 20) -> ResultadoWorker:
        """Consolida as janelas pendentes, uma transação por janela.

        Reusa o `Analyser` inteiro: as etapas 3 a 6 já sabem agrupar por eixo, lotear,
        consolidar em dois níveis, normalizar contagens e verificar ids. A `Rodada` é
        montada só com o ator porque `_recalcular_janela` lê os resumos do armazenamento,
        não dela.
        """
        resultado = ResultadoWorker()

        for pendente in self._atores_com_janela_pendente()[:limite]:
            ator_id = (
                pendente["nr_titulo_eleitoral_candidato"]
                if pendente["entity_type"] == "politico"
                else str(pendente["nr_partido"])
            )
            janela = pendente["janela_mes"]
            try:
                destino = ArmazenamentoPostgres(
                    self.conexao,
                    Ator(
                        entity_type=pendente["entity_type"],
                        ator_id=ator_id,
                        nome=pendente["ator_nome"],
                    ),
                )
                analyser = Analyser(self.config, self.modelo, armazenamento=destino)
                rodada = Rodada(
                    ator=AtorPolitico(
                        id=ator_id,
                        tipo=pendente["entity_type"],
                        nome=pendente["ator_nome"],
                    ),
                    itens=[],
                )
                interno = analyser._recalcular_janela  # etapas 3 a 6
                from analyser.pipeline import ResultadoExecucao

                parcial = ResultadoExecucao(
                    ator_id=ator_id,
                    ator_nome=pendente["ator_nome"],
                    rodada=f"worker/{janela}",
                )
                interno(rodada, janela, parcial)

                self.conexao.commit()
                resultado.janelas_consolidadas += 1
                resultado.panoramas_gravados += len(parcial.panoramas)
                resultado.avisos.extend(parcial.avisos)
                resultado.erros.extend(parcial.erros)

            except Exception as erro:  # noqa: BLE001
                self.conexao.rollback()
                resultado.erros.append(f"janela {ator_id}/{janela}: {erro}")
                logger.exception("Falha ao consolidar %s/%s", ator_id, janela)

        return resultado


__all__ = ["Worker", "ResultadoWorker"]
