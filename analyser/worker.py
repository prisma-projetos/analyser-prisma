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

Quando o provedor do modelo cai
-------------------------------
Falha do provedor (`ErroDoProvedor`: fora do ar, travado, sem rede, recusando a chamada)
não é falha do evento nem da janela. O worker desfaz o que começou, pausa por
`ESPERA_FALHA_SEGUNDOS` (padrão 1 h) e tenta de novo a MESMA unidade, sem marcá-la como
erro. Até 15/09/2026 cada evento virava `erro` e o lote avançava: com o Ollama
inacessível, três execuções seguidas marcaram 1.500 eventos como erro definitivo.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from analyser.armazenamento import agora_iso
from analyser.armazenamento_postgres import (
    ArmazenamentoPostgres,
    Ator,
    AuditoriaExecucaoPostgres,
)
from analyser.config import Config, ler_espera_falha
from analyser.enriquecimento import Enriquecedor, ItemEnriquecido
from analyser.modelos import ErroDeValidacao, ErroDoProvedor, Modelo, gerar_validado
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
    execucao_id: int | None = None
    #: Tokens de prompt e de resposta somados por tarefa. Passou a existir em 3 de
    #: setembro de 2026: a execução que quebrou por tamanho de prompt foi a mesma que
    #: não soube dizer quanto tinha gastado. As constantes de dimensionamento em
    #: `config.py` são estimativas justamente porque este dado não era guardado.
    tokens: Counter[str] = field(default_factory=Counter)
    #: Uma entrada por pausa causada por falha do provedor do modelo: qual unidade
    #: esperava, o erro e quando a pausa começou. Vai para a auditoria da execução.
    esperas: list[dict[str, Any]] = field(default_factory=list)


class Worker:
    """Processa eventos já ingeridos e consolida janelas pendentes."""

    def __init__(
        self,
        conexao: psycopg.Connection,
        modelo: Modelo,
        config: Config,
        *,
        espera_falha_segundos: int | None = None,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        """`dormir` é injetável para os testes exercitarem a pausa sem esperar de verdade."""
        self.conexao = conexao
        self.modelo = modelo
        self.config = config
        self.espera_falha_segundos = (
            espera_falha_segundos if espera_falha_segundos is not None else ler_espera_falha()
        )
        self.dormir = dormir
        self.enriquecedor = Enriquecedor(conexao)
        self.prompts = Prompts(raiz=config.prompts)
        self.prompts.precarregar()

    # -- execução auditada -------------------------------------------------------------

    def executar(
        self,
        *,
        limite_eventos: int = 0,
        limite_consolidar: int = 0,
        consolidar_tudo: bool = False,
    ) -> ResultadoWorker:
        """Executa as trilhas pedidas e registra uma linha em ``analyser_execucao``.

        A linha nasce e recebe commit antes do processamento. Por isso uma queda abrupta
        não apaga a evidência da tentativa: ela fica ``em_andamento``. Os commits por
        evento e por janela continuam os mesmos; a auditoria não decide retomada.
        """
        if limite_eventos < 0 or limite_consolidar < 0:
            raise ValueError("os limites não podem ser negativos")
        if limite_consolidar and consolidar_tudo:
            raise ValueError("use limite_consolidar ou consolidar_tudo, não ambos")
        if not limite_eventos and not limite_consolidar and not consolidar_tudo:
            raise ValueError(
                "informe limite_eventos, limite_consolidar e/ou consolidar_tudo"
            )

        capacidade_lote = limite_eventos or limite_consolidar
        unidade_capacidade = (
            "eventos"
            if limite_eventos
            else "todas_as_janelas_pendentes"
            if consolidar_tudo
            else "janelas"
        )
        limites_auditados: dict[str, int | bool] = {
            "eventos": limite_eventos,
            "consolidar": limite_consolidar,
        }
        if consolidar_tudo:
            limites_auditados["consolidar_tudo"] = True
        auditoria = AuditoriaExecucaoPostgres(self.conexao)
        execucao_id, numero_execucao = auditoria.iniciar(
            capacidade_lote=capacidade_lote,
            modelo=self.modelo.nome,
            dados={
                "tipo_execucao": "worker",
                "limites": limites_auditados,
                "unidade_capacidade_lote": unidade_capacidade,
            },
        )
        # A execução precisa sobreviver mesmo se o processo cair no primeiro resumo.
        self.conexao.commit()

        resultado = ResultadoWorker(execucao_id=execucao_id)
        try:
            if limite_eventos:
                self._somar_resultado(
                    resultado, self.processar_eventos(limite=limite_eventos)
                )
            if limite_consolidar:
                self._somar_resultado(
                    resultado, self.consolidar_janelas(limite=limite_consolidar)
                )
            elif consolidar_tudo:
                self._somar_resultado(
                    resultado, self.consolidar_janelas(limite=None)
                )

            status = "falhou" if resultado.erros else "concluida"
            erro = "\n".join(resultado.erros) or None
            auditoria.finalizar(
                execucao_id,
                status=status,
                quantidade_eventos=resultado.eventos_processados,
                quantidade_itens=resultado.itens_criados,
                quantidade_resumos=resultado.resumos_gerados,
                quantidade_panoramas=resultado.panoramas_gravados,
                dados=self._dados_auditoria(
                    numero_execucao,
                    limite_eventos,
                    limite_consolidar,
                    consolidar_tudo,
                    unidade_capacidade,
                    resultado,
                    status,
                ),
                erro=erro,
            )
            self.conexao.commit()
            return resultado

        except Exception as erro:  # noqa: BLE001 — a execução deve ficar observável
            self.conexao.rollback()
            mensagem = str(erro)
            auditoria.finalizar(
                execucao_id,
                status="falhou",
                quantidade_eventos=resultado.eventos_processados,
                quantidade_itens=resultado.itens_criados,
                quantidade_resumos=resultado.resumos_gerados,
                quantidade_panoramas=resultado.panoramas_gravados,
                dados=self._dados_auditoria(
                    numero_execucao,
                    limite_eventos,
                    limite_consolidar,
                    consolidar_tudo,
                    unidade_capacidade,
                    resultado,
                    "falhou",
                    erro_inesperado=mensagem,
                ),
                erro=mensagem,
            )
            self.conexao.commit()
            raise

    @staticmethod
    def _somar_resultado(destino: ResultadoWorker, origem: ResultadoWorker) -> None:
        """Acumula as duas trilhas em uma única auditoria de invocação."""
        for campo in (
            "eventos_processados",
            "itens_criados",
            "itens_ja_existentes",
            "resumos_gerados",
            "janelas_consolidadas",
            "panoramas_gravados",
        ):
            setattr(destino, campo, getattr(destino, campo) + getattr(origem, campo))
        destino.avisos.extend(origem.avisos)
        destino.erros.extend(origem.erros)
        destino.tokens.update(origem.tokens)
        destino.esperas.extend(origem.esperas)

    def _dados_auditoria(
        self,
        numero_execucao: int,
        limite_eventos: int,
        limite_consolidar: int,
        consolidar_tudo: bool,
        unidade_capacidade: str,
        resultado: ResultadoWorker,
        status: str,
        *,
        erro_inesperado: str | None = None,
    ) -> dict[str, Any]:
        """Detalhes que não cabem nas colunas resumidas da tabela de auditoria."""
        limites: dict[str, int | bool] = {
            "eventos": limite_eventos,
            "consolidar": limite_consolidar,
        }
        if consolidar_tudo:
            limites["consolidar_tudo"] = True

        dados: dict[str, Any] = {
            "tipo_execucao": "worker",
            "execucao_id": resultado.execucao_id,
            "numero_execucao": numero_execucao,
            "status": status,
            "modelo": self.modelo.nome,
            "limites": limites,
            "unidade_capacidade_lote": unidade_capacidade,
            "resultado": {
                "eventos_processados": resultado.eventos_processados,
                "itens_criados": resultado.itens_criados,
                "itens_ja_existentes": resultado.itens_ja_existentes,
                "resumos_gerados": resultado.resumos_gerados,
                "janelas_consolidadas": resultado.janelas_consolidadas,
                "panoramas_gravados": resultado.panoramas_gravados,
            },
            "chamadas_por_tarefa": dict(self.modelo.chamadas_por_tarefa),
            "chamadas_total": self.modelo.chamadas,
            # Sem isto não existe média real de tokens por resumo ou por panorama, e o
            # dimensionamento do lote continua apoiado em estimativa. `dict(...)` porque
            # `Counter` não é serializável para JSONB como está.
            "tokens": dict(resultado.tokens),
            "avisos": resultado.avisos,
            "erros": resultado.erros,
            "esperas_por_falha_do_provedor": resultado.esperas,
        }
        if erro_inesperado:
            dados["erro_inesperado"] = erro_inesperado
        return dados

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

        As contagens de cada tentativa ficam num resultado parcial e só entram no total
        depois do commit. Antes, o `rollback` desfazia os itens no banco mas não no
        relatório: a execução 27 registrou "500 itens criados" sem ter gravado nenhum. Com
        a retomada, a mesma tentativa pode se repetir, e o erro se multiplicaria.

        Falha do provedor não marca o evento: o worker pausa e tenta o mesmo evento de
        novo (ver `_pausar`). Qualquer outra falha continua marcando o evento como `erro`
        e seguindo para o próximo, porque ela depende do evento e se repetiria igual.
        """
        resultado = ResultadoWorker()

        for linha in self._eventos_pendentes(limite):
            evento_id = linha["evento_id"]
            while True:
                parcial = ResultadoWorker()
                try:
                    evento = NotificacaoClassifier.model_validate(linha["payload_json"])
                    itens = self.enriquecedor.enriquecer(evento)

                    for item in itens:
                        self._processar_item(item, evento_id, parcial)

                    self.conexao.execute(
                        """UPDATE analyser_inbox_evento
                              SET status = 'processado', processado_em = now()
                            WHERE evento_id = %s""",
                        (evento_id,),
                    )
                    self.conexao.commit()
                    parcial.eventos_processados += 1
                    self._somar_resultado(resultado, parcial)
                    break

                except ErroDoProvedor as erro:
                    self.conexao.rollback()
                    # Os tokens foram gastos mesmo com o rollback; as contagens, não.
                    resultado.tokens.update(parcial.tokens)
                    self._pausar(f"evento {evento_id}", erro, resultado)

                except Exception as erro:  # noqa: BLE001 — um evento ruim não derruba o lote
                    self.conexao.rollback()
                    resultado.tokens.update(parcial.tokens)
                    self.conexao.execute(
                        """UPDATE analyser_inbox_evento
                              SET status = 'erro', tentativas = tentativas + 1, erro = %s
                            WHERE evento_id = %s""",
                        (str(erro)[:2000], evento_id),
                    )
                    self.conexao.commit()
                    resultado.erros.append(f"evento {evento_id}: {erro}")
                    logger.exception("Falha ao processar o evento %s", evento_id)
                    break

        return resultado

    def _pausar(self, unidade: str, erro: ErroDoProvedor, resultado: ResultadoWorker) -> None:
        """Espera o provedor do modelo voltar, sem descartar a unidade que falhou.

        Quem chama já desfez o trabalho parcial com `rollback` e, depois da pausa, tenta
        a MESMA unidade de novo. É isso que faz o processamento continuar de onde parou:
        o evento continua `recebido` e a janela continua com itens `resumido`.

        A espera acontece com a transação fechada, para o banco não segurar travas nem uma
        sessão ociosa em transação durante uma hora. Se o processo for encerrado no meio
        da pausa — pelo prazo do Job, por exemplo —, nada se perde: a próxima execução
        encontra a mesma unidade pendente.
        """
        resultado.esperas.append(
            {
                "unidade": unidade,
                "erro": str(erro)[:500],
                "espera_segundos": self.espera_falha_segundos,
                "inicio": agora_iso(),
            }
        )
        logger.warning(
            "Provedor do modelo indisponível (%s): %s. Processamento pausado por %d s; "
            "depois retoma do mesmo ponto.",
            unidade,
            erro,
            self.espera_falha_segundos,
        )
        self.dormir(self.espera_falha_segundos)

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
            ator_apelidos=item.ator_apelidos,
            item_id=item.item_id,
            veiculo=item.veiculo,
            data=item.data_publicacao.date().isoformat() if item.data_publicacao else "",
            titulo=item.titulo,
            texto=item.texto,
            contexto=item.contexto,
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

        resultado.tokens.update(saida.tokens or {})

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
                """SELECT entity_type, nr_titulo_eleitoral_candidato, nr_partido,
                          max(ator_nome) AS ator_nome, janela_mes
                     FROM analyser_item
                    WHERE status = 'resumido'
                    GROUP BY entity_type, nr_titulo_eleitoral_candidato,
                             nr_partido, janela_mes
                    ORDER BY janela_mes, entity_type,
                             coalesce(nr_titulo_eleitoral_candidato, nr_partido::text)"""
            ).fetchall()

    def quantidade_janelas_pendentes(self) -> int:
        """Conta identidades distintas de ator + mês ainda não consolidadas."""
        return len(self._atores_com_janela_pendente())

    def consolidar_janelas(self, limite: int | None = 20) -> ResultadoWorker:
        """Consolida as janelas pendentes, uma transação por janela.

        ``limite=None`` processa uma fotografia de todas as janelas pendentes no momento
        da consulta. Itens resumidos concorrentemente depois dessa fotografia continuam
        pendentes e serão encontrados numa próxima execução.

        Reusa o `Analyser` inteiro: as etapas 3 a 6 já sabem agrupar por eixo, lotear,
        consolidar em dois níveis, normalizar contagens e verificar ids. A `Rodada` é
        montada só com o ator porque `_recalcular_janela` lê os resumos do armazenamento,
        não dela.
        """
        resultado = ResultadoWorker()
        pendentes = self._atores_com_janela_pendente()
        if limite is not None:
            if limite < 0:
                raise ValueError("o limite de janelas não pode ser negativo")
            pendentes = pendentes[:limite]

        for pendente in pendentes:
            ator_id = (
                pendente["nr_titulo_eleitoral_candidato"]
                if pendente["entity_type"] == "politico"
                else str(pendente["nr_partido"])
            )
            janela = pendente["janela_mes"]
            # Falha do provedor volta para cá e tenta a mesma janela depois da pausa;
            # qualquer outro desfecho, sucesso ou erro, segue para a próxima janela.
            while True:
                # Fora do `try` porque o `except` lê os tokens desta instância: se a falha
                # acontecer antes da atribuição, `analyser` estaria indefinido e o handler
                # levantaria NameError por cima do erro real, escondendo a causa.
                analyser: Analyser | None = None
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
                            # Os apelidos não ficam em `analyser_item`; vêm do cadastro,
                            # para o panorama reconhecer o ator como a imprensa o chama.
                            apelidos=self.enriquecedor.apelidos_do_ator(
                                pendente["entity_type"], ator_id
                            ),
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
                    resultado.tokens.update(analyser.tokens)
                    break

                except ErroDoProvedor as erro:
                    # O rollback mantém os itens em `resumido`: a janela continua pendente
                    # e é ela mesma que se tenta de novo depois da pausa.
                    self.conexao.rollback()
                    if analyser is not None:
                        resultado.tokens.update(analyser.tokens)
                    self._pausar(f"janela {ator_id}/{janela}", erro, resultado)

                except Exception as erro:  # noqa: BLE001
                    self.conexao.rollback()
                    # O consumo é contabilizado mesmo na falha, e de propósito: uma janela
                    # que estourou o contexto é exatamente aquela cujo tamanho de prompt
                    # precisa aparecer na auditoria. Descartar o número junto com o erro
                    # apagaria a evidência de por que ela falhou.
                    if analyser is not None:
                        resultado.tokens.update(analyser.tokens)
                    resultado.erros.append(f"janela {ator_id}/{janela}: {erro}")
                    logger.exception("Falha ao consolidar %s/%s", ator_id, janela)
                    break

        return resultado


__all__ = ["Worker", "ResultadoWorker"]
