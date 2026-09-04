"""Implementação PostgreSQL de `DestinoArmazenamento`.

Preenche a porta declarada em `analyser/armazenamento.py`, que o pipeline já usa. As seis
etapas não sabem que o destino mudou — é o que permitiu trocar arquivo por banco sem tocar
nelas.

O ciclo de vida de um item, e por que ele importa
--------------------------------------------------
`analyser_item.status` é o que torna a recuperação após crash explícita em vez de inferida:

    novo         → linha criada pelo enriquecimento, sem resumo
    resumido     → resumo gravado, ainda não refletido em panorama nenhum
    consolidado  → já entrou num panorama

Uma janela está pendente quando tem item `resumido`. A consolidação vira `consolidado`
SOMENTE os itens que ela leu — um item resumido enquanto ela roda não estava no conjunto
lido, continua `resumido`, e a janela segue pendente na próxima varredura.

Essa escolha veio de descartar duas alternativas piores. Marcar a janela numa tabela
própria exige acertar duas fronteiras de transação (gravar o marcador junto com o resumo,
apagá-lo junto com o panorama), e errar qualquer uma some com o item silenciosamente.
Derivar por timestamp (`max(resumido_em) > max(panorama.criado_em)`) tem a MESMA falha: um
item resumido durante a consolidação fica com carimbo anterior ao do panorama que não o
contém, a janela parece limpa e o item desaparece do painel para sempre.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from analyser.armazenamento import ViolacaoAppendOnly, agora_iso
from analyser.config import Eixo
from analyser.schemas import Panorama, RegistroPanorama, RegistroResumo


@dataclass(frozen=True, slots=True)
class Ator:
    """Identificação do ator de uma execução.

    A tabela separa o ator em `entity_type` mais uma de duas colunas — a mesma convenção de
    `tabela_classificacao` — enquanto `RegistroResumo` e `RegistroPanorama` carregam um
    `ator_id` único. Esta classe faz a tradução num lugar só.
    """

    entity_type: str
    ator_id: str
    nome: str

    @property
    def nr_titulo(self) -> str | None:
        return self.ator_id if self.entity_type == "politico" else None

    @property
    def nr_partido(self) -> int | None:
        return int(self.ator_id) if self.entity_type == "partido" else None


class AuditoriaExecucaoPostgres:
    """Ciclo de vida auditável de uma invocação do worker.

    A auditoria não participa da retomada: quem decide o que ainda falta são os estados da
    inbox e dos itens. Ela existe para tornar a execução observável, inclusive quando o
    processo termina de forma inesperada e a linha permanece ``em_andamento``.
    """

    def __init__(self, conexao: psycopg.Connection) -> None:
        self.conexao = conexao

    def iniciar(
        self,
        *,
        capacidade_lote: int,
        modelo: str,
        dados: dict[str, Any],
    ) -> tuple[int, int]:
        """Cria uma execução visível antes do trabalho pesado.

        O advisory lock (trava transacional leve, compartilhada somente por quem pede a
        mesma chave) evita que dois workers calculem o mesmo ``numero_execucao``.
        """
        if capacidade_lote < 0:
            raise ValueError("capacidade_lote não pode ser negativa")

        self.conexao.execute(
            "SELECT pg_advisory_xact_lock(hashtext('analyser_execucao_numero'))"
        )
        numero = self.conexao.execute(
            "SELECT COALESCE(max(numero_execucao), 0) + 1 FROM analyser_execucao"
        ).fetchone()[0]
        linha = self.conexao.execute(
            """INSERT INTO analyser_execucao (
                   numero_execucao, status, capacidade_lote, modelo, auditoria_json
               ) VALUES (%s, 'em_andamento', %s, %s, %s)
               RETURNING execucao_id""",
            (numero, capacidade_lote, modelo, Jsonb(dados)),
        ).fetchone()
        return int(linha[0]), int(numero)

    def finalizar(
        self,
        execucao_id: int,
        *,
        status: str,
        quantidade_eventos: int,
        quantidade_itens: int,
        quantidade_resumos: int,
        quantidade_panoramas: int,
        dados: dict[str, Any],
        erro: str | None = None,
    ) -> None:
        """Fecha uma execução iniciada, preservando as contagens reais."""
        if status not in {"concluida", "falhou"}:
            raise ValueError(f"status final inválido: {status}")

        linha = self.conexao.execute(
            """UPDATE analyser_execucao
                  SET status = %s,
                      quantidade_eventos = %s,
                      quantidade_itens = %s,
                      quantidade_resumos = %s,
                      quantidade_panoramas = %s,
                      finalizado_em = now(),
                      auditoria_json = %s,
                      erro = %s
                WHERE execucao_id = %s AND status = 'em_andamento'
                RETURNING execucao_id""",
            (
                status,
                quantidade_eventos,
                quantidade_itens,
                quantidade_resumos,
                quantidade_panoramas,
                Jsonb(dados),
                erro[:2000] if erro else None,
                execucao_id,
            ),
        ).fetchone()
        if linha is None:
            raise RuntimeError(
                f"execução {execucao_id} não está em andamento ou não existe"
            )


class ArmazenamentoPostgres:
    """Persistência nas tabelas `analyser_*`.

    Recebe a conexão pronta em vez de abri-la: o consumidor precisa que a gravação do
    evento e o processamento dele caibam na mesma transação, e o ACK só ocorra depois do
    commit. Abrir conexão própria aqui tornaria isso impossível.

    Não commita. Quem chama decide a fronteira da transação — é o que permite gravar
    panorama e virar o status dos itens atomicamente.
    """

    def __init__(self, conexao: psycopg.Connection, ator: Ator) -> None:
        self.conexao = conexao
        self.ator = ator

    # -- filtro de ator, repetido em toda consulta -------------------------------------

    def _onde_ator(self, prefixo: str = "") -> tuple[str, dict[str, Any]]:
        p = f"{prefixo}." if prefixo else ""
        return (
            f"{p}entity_type = %(entity_type)s "
            f"AND {p}nr_titulo_eleitoral_candidato IS NOT DISTINCT FROM %(nr_titulo)s "
            f"AND {p}nr_partido IS NOT DISTINCT FROM %(nr_partido)s",
            {
                "entity_type": self.ator.entity_type,
                "nr_titulo": self.ator.nr_titulo,
                "nr_partido": self.ator.nr_partido,
            },
        )

    # -- camada imutável: resumos por item ---------------------------------------------

    @staticmethod
    def _para_registro(linha: dict[str, Any], ator_id: str) -> RegistroResumo:
        # `data_publicacao` é TIMESTAMPTZ e `RegistroResumo.data` é `date`. O Pydantic
        # recusa converter datetime com hora diferente de zero, então a conversão é
        # explícita aqui, na fronteira entre o banco e o schema.
        publicacao = linha["data_publicacao"]
        return RegistroResumo(
            item_id=linha["item_id"],
            ator_id=ator_id,
            janela=linha["janela_mes"],
            rotulo=linha["rotulo_definitivo"],
            veiculo=linha["veiculo"] or "veículo não identificado",
            url=linha["link_noticia"] or "",
            data=publicacao.date() if publicacao else date.today(),
            titulo=linha["titulo_sintetico"] or "",
            resumo=linha["resumo"],
            temas=linha["temas_json"] or [],
            fonte_id=linha["item_id"],
            modelo=linha["modelo"] or "",
            hash_prompt_resumo=linha["hash_prompt_resumo"] or "",
            criado_em=linha["criado_em"].isoformat(),
        )

    _SELECT_ITEM = """
        SELECT i.item_id, i.janela_mes, i.rotulo_definitivo, i.link_noticia,
               i.data_publicacao, i.resumo, i.temas_json, i.modelo,
               i.hash_prompt_resumo, i.criado_em,
               m.nome_midia AS veiculo,
               NULL::text   AS titulo_sintetico
          FROM analyser_item i
          LEFT JOIN tabela_midia m ON m.id_midia = i.id_midia
    """

    def ler_resumos(self) -> dict[str, RegistroResumo]:
        """Resumos já gravados deste ator, indexados pelo id do item.

        Escopado ao ator porque uma execução trata um ator só, e `item_id` já codifica a
        entidade — não há colisão possível com itens de outro. Sem o escopo isto viraria
        varredura da tabela inteira a cada rodada.
        """
        onde, params = self._onde_ator("i")
        with self.conexao.cursor(row_factory=dict_row) as cur:
            linhas = cur.execute(
                f"{self._SELECT_ITEM} WHERE {onde} AND i.resumo IS NOT NULL", params
            ).fetchall()
        return {
            l["item_id"]: self._para_registro(l, self.ator.ator_id) for l in linhas
        }

    def ler_resumos_da_janela(
        self, *, ator_id: str, janela: str
    ) -> list[RegistroResumo]:
        """Só os resumos de uma janela — o recorte que o panorama consolida."""
        onde, params = self._onde_ator("i")
        params["janela"] = janela
        with self.conexao.cursor(row_factory=dict_row) as cur:
            linhas = cur.execute(
                f"{self._SELECT_ITEM} WHERE {onde} AND i.janela_mes = %(janela)s "
                "AND i.resumo IS NOT NULL ORDER BY i.item_id",
                params,
            ).fetchall()
        return [self._para_registro(l, ator_id) for l in linhas]

    def gravar_item(self, item: Any, evento_id: int) -> bool:
        """Cria a linha de um item enriquecido, com `status='novo'` e sem resumo.

        Devolve `True` se a linha nasceu agora, `False` se já existia. `ON CONFLICT DO
        NOTHING` em vez de consultar antes: o mesmo artigo pode voltar por reprocessamento
        ou por outra réplica, e deixar o banco decidir é o que torna isso correto com mais
        de um worker.

        Não sobrescreve item existente de propósito — o resumo dele já pode estar pago.
        """
        cur = self.conexao.execute(
            """
            INSERT INTO analyser_item (
                item_id, evento_id, article_id, entity_type,
                nr_titulo_eleitoral_candidato, nr_partido, ator_nome, id_midia,
                link_noticia, data_publicacao, janela_mes, rotulo_definitivo,
                regra_agregacao_versao, trechos_json, conteudo_hash, status
            ) VALUES (
                %(item_id)s, %(evento_id)s, %(article_id)s, %(entity_type)s,
                %(nr_titulo)s, %(nr_partido)s, %(ator_nome)s, %(id_midia)s,
                %(link)s, %(data)s, %(janela)s, %(rotulo)s,
                %(regra)s, %(trechos)s, %(hash)s, 'novo'
            )
            ON CONFLICT (item_id) DO NOTHING
            """,
            {
                "item_id": item.item_id,
                "evento_id": evento_id,
                "article_id": item.article_id,
                "entity_type": item.entity_type,
                "nr_titulo": item.entity_id if item.entity_type == "politico" else None,
                "nr_partido": int(item.entity_id) if item.entity_type == "partido" else None,
                "ator_nome": item.ator_nome,
                "id_midia": item.id_midia,
                "link": item.link_noticia,
                "data": item.data_publicacao,
                "janela": item.janela_mes,
                "rotulo": item.rotulo,
                "regra": item.regra_agregacao_versao,
                "trechos": Jsonb([t.para_json() for t in item.trechos]),
                "hash": item.conteudo_hash,
            },
        )
        return cur.rowcount > 0

    def gravar_resumo(self, registro: RegistroResumo) -> None:
        """Grava o resumo de um item e o marca `resumido`.

        `WHERE resumo IS NULL` é o append-only depois da fusão item+resumo: uma segunda
        gravação não sobrescreve o resumo já pago, ela simplesmente não afeta linha
        nenhuma — e o `rowcount` zero vira erro em vez de sucesso silencioso.

        Faz UPSERT porque a linha pode ainda não existir: o enriquecimento a cria com
        `status='novo'`, mas o caminho de teste e o de reprocessamento chegam aqui direto.
        """
        params = {
            "item_id": registro.item_id,
            "janela": registro.janela,
            "rotulo": registro.rotulo,
            "resumo": registro.resumo,
            "temas": Jsonb(registro.temas),
            "modelo": registro.modelo,
            "hash_prompt": registro.hash_prompt_resumo,
            "url": registro.url,
            "data": registro.data,
            "entity_type": self.ator.entity_type,
            "nr_titulo": self.ator.nr_titulo,
            "nr_partido": self.ator.nr_partido,
            "ator_nome": self.ator.nome,
        }
        cur = self.conexao.execute(
            """
            INSERT INTO analyser_item (
                item_id, evento_id, article_id, entity_type,
                nr_titulo_eleitoral_candidato, nr_partido, ator_nome,
                link_noticia, data_publicacao, janela_mes, rotulo_definitivo,
                regra_agregacao_versao, trechos_json, conteudo_hash,
                resumo, temas_json, modelo, hash_prompt_resumo, resumido_em, status
            )
            SELECT %(item_id)s, e.evento_id, e.article_id, %(entity_type)s,
                   %(nr_titulo)s, %(nr_partido)s, %(ator_nome)s,
                   %(url)s, %(data)s, %(janela)s, %(rotulo)s,
                   'externa', '[]'::jsonb, repeat('0', 64),
                   %(resumo)s, %(temas)s, %(modelo)s, %(hash_prompt)s, now(), 'resumido'
              FROM (SELECT evento_id, article_id FROM analyser_inbox_evento
                     ORDER BY evento_id LIMIT 1) e
            ON CONFLICT (item_id) DO UPDATE SET
                resumo             = EXCLUDED.resumo,
                temas_json         = EXCLUDED.temas_json,
                modelo             = EXCLUDED.modelo,
                hash_prompt_resumo = EXCLUDED.hash_prompt_resumo,
                resumido_em        = now(),
                status             = 'resumido',
                atualizado_em      = now()
              WHERE analyser_item.resumo IS NULL
            """,
            params,
        )
        if cur.rowcount == 0:
            raise ViolacaoAppendOnly(
                f"O resumo do item {registro.item_id} já existe e não pode ser reescrito. "
                "A camada de resumos é append-only."
            )

    # -- camada recalculável: panoramas versionados ------------------------------------

    def ler_panoramas(self) -> list[RegistroPanorama]:
        """Todas as versões deste ator, em ordem de gravação."""
        onde, params = self._onde_ator("p")
        with self.conexao.cursor(row_factory=dict_row) as cur:
            linhas = cur.execute(
                f"""SELECT p.* FROM analyser_panorama p WHERE {onde}
                    ORDER BY p.janela_mes, p.eixo, p.versao""",
                params,
            ).fetchall()
        return [self._para_panorama(l) for l in linhas]

    def _para_panorama(self, linha: dict[str, Any]) -> RegistroPanorama:
        hashes = linha["hashes_prompts_json"] or {}
        return RegistroPanorama(
            ator_id=self.ator.ator_id,
            janela=linha["janela_mes"],
            eixo=linha["eixo"],
            versao=linha["versao"],
            formato=linha["formato"],
            panorama=Panorama(
                ator_id=self.ator.ator_id,
                janela=linha["janela_mes"],
                eixo=linha["eixo"],
                total_itens=linha["total_itens"],
                temas=linha["temas_json"],
                fontes=linha["fontes_json"],
                texto=linha["texto"],
            ),
            avisos=linha["avisos_json"] or [],
            lotes=linha["quantidade_lotes"],
            modelo=linha["modelo"] or "",
            hash_prompt_lote=hashes.get("lote", ""),
            hash_prompt_final=hashes.get("final", ""),
            hash_prompt_formato=hashes.get("formato", ""),
            rodada=hashes.get("rodada", ""),
            criado_em=linha["criado_em"].isoformat(),
        )

    def proxima_versao(self, *, ator_id: str, janela: str, eixo: Eixo) -> int:
        onde, params = self._onde_ator()
        params.update({"janela": janela, "eixo": eixo})
        linha = self.conexao.execute(
            f"""SELECT COALESCE(max(versao), 0) + 1 FROM analyser_panorama
                 WHERE {onde} AND janela_mes = %(janela)s AND eixo = %(eixo)s""",
            params,
        ).fetchone()
        return int(linha[0])

    def gravar_panorama(self, registro: RegistroPanorama) -> None:
        """Grava uma versão nova E marca `consolidado` os itens que entraram nela.

        As duas escritas ficam na MESMA transação de propósito. Se o panorama fosse
        gravado e os itens virassem `consolidado` num commit separado, um crash entre os
        dois deixaria a janela limpa sem estar — ou marcada suja para sempre, dependendo
        da ordem. Juntas, ou as duas acontecem ou nenhuma.

        Vira `consolidado` apenas os itens listados em `fontes` — os que esta consolidação
        de fato leu. Um item resumido enquanto ela rodava não está nessa lista, continua
        `resumido`, e a janela reaparece como pendente na varredura seguinte.
        """
        onde, params = self._onde_ator()
        params.update(
            {
                "janela": registro.janela,
                "eixo": registro.eixo,
                "versao": registro.versao,
                "formato": registro.formato,
                "ator_nome": self.ator.nome,
                "total": registro.panorama.total_itens,
                "texto": registro.panorama.texto,
                "temas": Jsonb([t.model_dump(mode="json") for t in registro.panorama.temas]),
                "fontes": Jsonb(registro.panorama.fontes),
                "avisos": Jsonb(registro.avisos),
                "lotes": registro.lotes,
                "modelo": registro.modelo,
                "hashes": Jsonb(
                    {
                        "lote": registro.hash_prompt_lote,
                        "final": registro.hash_prompt_final,
                        "formato": registro.hash_prompt_formato,
                        "rodada": registro.rodada,
                    }
                ),
            }
        )
        try:
            self.conexao.execute(
                """
                INSERT INTO analyser_panorama (
                    entity_type, nr_titulo_eleitoral_candidato, nr_partido, ator_nome,
                    janela_mes, eixo, versao, formato, total_itens, texto,
                    temas_json, fontes_json, avisos_json, quantidade_lotes,
                    modelo, hashes_prompts_json
                ) VALUES (
                    %(entity_type)s, %(nr_titulo)s, %(nr_partido)s, %(ator_nome)s,
                    %(janela)s, %(eixo)s, %(versao)s, %(formato)s, %(total)s, %(texto)s,
                    %(temas)s, %(fontes)s, %(avisos)s, %(lotes)s,
                    %(modelo)s, %(hashes)s
                )
                """,
                params,
            )
        except psycopg.errors.UniqueViolation as erro:
            raise ViolacaoAppendOnly(
                f"A versão {registro.versao} do panorama "
                f"{registro.ator_id}/{registro.janela}/{registro.eixo} já existe."
            ) from erro

        self.conexao.execute(
            """UPDATE analyser_item SET status = 'consolidado', atualizado_em = now()
                WHERE item_id = ANY(%(fontes)s) AND status = 'resumido'""",
            {"fontes": list(registro.panorama.fontes)},
        )

    def panoramas_mais_recentes(
        self, *, ator_id: str, janela: str
    ) -> dict[Eixo, RegistroPanorama]:
        onde, params = self._onde_ator("p")
        params["janela"] = janela
        with self.conexao.cursor(row_factory=dict_row) as cur:
            linhas = cur.execute(
                f"""SELECT DISTINCT ON (p.eixo) p.* FROM analyser_panorama p
                     WHERE {onde} AND p.janela_mes = %(janela)s
                     ORDER BY p.eixo, p.versao DESC""",
                params,
            ).fetchall()
        return {l["eixo"]: self._para_panorama(l) for l in linhas}

    # -- janelas pendentes --------------------------------------------------------------

    def janelas_pendentes(self) -> list[str]:
        """Janelas deste ator com item resumido que ainda não entrou em panorama.

        É a recuperação após crash, e ela é explícita: não infere nada de timestamp, só
        pergunta quais itens estão `resumido`. O estado sobrevive a queda em qualquer
        ponto porque é o próprio dado, não um marcador paralelo que pode dessincronizar.
        """
        onde, params = self._onde_ator()
        linhas = self.conexao.execute(
            f"""SELECT DISTINCT janela_mes FROM analyser_item
                 WHERE {onde} AND status = 'resumido' ORDER BY janela_mes""",
            params,
        ).fetchall()
        return [l[0] for l in linhas]

    # -- auditoria ----------------------------------------------------------------------

    def proximo_numero_rodada(self) -> int:
        linha = self.conexao.execute(
            "SELECT COALESCE(max(numero_execucao), 0) + 1 FROM analyser_execucao"
        ).fetchone()
        return int(linha[0])

    def gravar_auditoria(self, carimbo: str, dados: dict[str, Any]) -> Path:
        """Registra a rodada em `analyser_execucao`.

        Devolve `Path` por causa da porta, que nasceu do destino em arquivo. Aqui o
        caminho é simbólico — identifica a linha, não um arquivo em disco.
        """
        linha = self.conexao.execute(
            """INSERT INTO analyser_execucao (
                   numero_execucao, status, capacidade_lote,
                   quantidade_eventos, quantidade_itens, quantidade_resumos,
                   quantidade_panoramas, modelo, finalizado_em, auditoria_json
               ) VALUES (%s, 'concluida', %s, %s, %s, %s, %s, %s, now(), %s)
               RETURNING execucao_id""",
            (
                dados.get("numero_rodada", 1),
                dados.get("capacidade_maxima_lote", 0),
                len(dados.get("ids_entrada", [])),
                len(dados.get("ids_entrada", [])),
                len(dados.get("ids_resumidos_agora", [])),
                len(dados.get("panoramas_gravados", [])),
                dados.get("modelo", ""),
                Jsonb(dados),
            ),
        ).fetchone()
        return Path(f"analyser_execucao://{linha[0]}")


__all__ = ["ArmazenamentoPostgres", "Ator", "agora_iso"]
