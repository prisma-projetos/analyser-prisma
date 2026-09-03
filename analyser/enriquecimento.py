"""Transforma a mensagem do Classifier em itens notícia × ator.

O descompasso que este módulo atravessa: o Classifier trabalha em *parágrafo × entidade*, o
Analyser em *notícia × ator*. Uma mensagem sobre um artigo vira vários itens — um por ator
citado — e cada um precisa de UM rótulo, enquanto a mensagem traz vários.

O que a mensagem traz e o que falta
------------------------------------
A mensagem tem `article_id`, `pipeline_version`, `link_noticia`, `data_publicacao_noticia` e
as classificações (entidade, `paragraph_index`, sentimento, score). Falta o conteúdo, que
vem do `prisma-dev`:

* texto      → `tabela_classificacao.paragrafo_analisado`, concatenado por ator
* veículo    → `tabela_classificacao.id_midia` → `tabela_midia.nome_midia`
* nome       → `entidade_matching.nome_principal`, que cobre político e partido na mesma
               tabela, chaveada igual ao payload
* título     → NÃO EXISTE no prisma-dev. Sintético, derivado de (artigo, ator), nunca
               persistido: coluna que só guarda valor fabricado parece dado.

O `paragraph_index` só existe na mensagem
------------------------------------------
`tabela_classificacao` não tem essa coluna — confirmado contra o banco vivo. É por isso que
ele é gravado em `analyser_item.trechos_json`, e é por isso que a correlação entre uma
entrada da mensagem e sua linha no banco é inferida, não lida.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from analyser.config import Eixo
from analyser.schemas import NotificacaoClassifier

#: Versão da regra que transforma vários rótulos de trecho em um só. Gravada em cada item
#: para que trocar a regra no futuro não torne os rótulos antigos incomparáveis com os novos.
REGRA_AGREGACAO = "maioria-v1"


@dataclass(frozen=True, slots=True)
class Trecho:
    """Uma classificação recebida, já casada (ou não) com sua linha no banco."""

    paragraph_index: int
    classificacao_sentimento: Eixo
    score_confianca: float
    id_classificacao: int | None = None
    texto: str = ""

    def para_json(self) -> dict[str, Any]:
        """O que vai para `analyser_item.trechos_json`.

        O texto do parágrafo NÃO entra: ele já está em
        `tabela_classificacao.paragrafo_analisado`, e duplicá-lo dobraria o maior conteúdo
        textual do banco sem acrescentar informação.
        """
        return {
            "paragraph_index": self.paragraph_index,
            "classificacao_sentimento": self.classificacao_sentimento,
            "score_confianca": self.score_confianca,
            "id_classificacao": self.id_classificacao,
        }


@dataclass(slots=True)
class ItemEnriquecido:
    """Uma notícia vista por um ator, pronta para virar linha e resumo."""

    item_id: str
    article_id: int
    entity_type: str
    entity_id: str
    ator_nome: str
    id_midia: int | None
    veiculo: str
    link_noticia: str
    data_publicacao: datetime | None
    janela_mes: str
    titulo: str
    texto: str
    rotulo: Eixo
    regra_agregacao_versao: str
    trechos: list[Trecho]
    conteudo_hash: str
    avisos: list[str] = field(default_factory=list)


def agregar_rotulo(trechos: list[Trecho]) -> tuple[Eixo, str]:
    """Reduz os rótulos dos trechos de um ator a um só.

    Maioria simples, empate vira neutro. Devolve também a versão da regra, para o item
    guardar sob qual critério aquele rótulo foi calculado.

    ISOLADA DE PROPÓSITO, e a regra está sob questionamento. Medido na amostra real: 65%
    dos trechos têm `score_confianca` 0, e TODOS são "neutro" — o que significa "o léxico
    não achou marcador", não "a cobertura é neutra". Sob maioria simples esses "não sei"
    votam com o mesmo peso das classificações confiantes, e num artigo real a única
    classificação confiante foi derrotada por catorze delas.

    Trocar isto é trocar o corpo desta função e a constante de versão. Nada mais no módulo
    depende de como a decisão é tomada.
    """
    if not trechos:
        return "neutro", REGRA_AGREGACAO

    votos = Counter(t.classificacao_sentimento for t in trechos)
    mais_votados = votos.most_common()
    if len(mais_votados) > 1 and mais_votados[0][1] == mais_votados[1][1]:
        return "neutro", REGRA_AGREGACAO
    return mais_votados[0][0], REGRA_AGREGACAO


def titulo_sintetico(article_id: int, ator_nome: str) -> str:
    """Título derivado, porque o real não existe no prisma-dev.

    Determinístico de propósito: o mesmo item gera sempre o mesmo título, então o
    `conteudo_hash` é estável entre execuções. No dia em que o Classifier persistir o
    título de verdade, o hash muda e o resumo é refeito com a manchete real.
    """
    return f"Artigo {article_id} — trechos classificados sobre {ator_nome}"


class Enriquecedor:
    """Consulta o `prisma-dev` para montar os itens de uma mensagem.

    Somente leitura sobre `tabela_classificacao`, `tabela_midia` e `entidade_matching`.
    """

    def __init__(self, conexao: psycopg.Connection) -> None:
        self.conexao = conexao

    # -- consultas ---------------------------------------------------------------------

    def _linhas_do_ator(
        self, article_id: int, pipeline_version: str, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        """Trechos daquele ator naquele artigo, na ordem de inserção.

        Ramifica por tipo de entidade em vez de usar `OR` com nulo: `nr_partido = NULL`
        nunca é verdadeiro, e um `OR` mal escrito devolveria silenciosamente zero linhas.
        """
        if entity_type == "politico":
            filtro = "c.nr_titulo_eleitoral_candidato = %(entidade)s"
            entidade: Any = entity_id
        else:
            filtro = "c.nr_partido = %(entidade)s"
            entidade = int(entity_id)

        with self.conexao.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                f"""
                SELECT c.id_classificacao, c.paragrafo_analisado,
                       c.classificacao_sentimento, c.score_confianca,
                       c.link_noticia, c.data_publicacao_noticia, c.id_midia,
                       m.nome_midia
                  FROM tabela_classificacao c
                  LEFT JOIN tabela_midia m ON m.id_midia = c.id_midia
                 WHERE c.id_artigo_scrape = %(artigo)s
                   AND c.pipeline_version = %(pipeline)s
                   AND {filtro}
                 ORDER BY c.id_classificacao
                """,
                {"artigo": article_id, "pipeline": pipeline_version, "entidade": entidade},
            ).fetchall()

    def _nome_do_ator(self, entity_type: str, entity_id: str) -> str | None:
        if entity_type == "politico":
            filtro, entidade = "nr_titulo_eleitoral_candidato = %(e)s", entity_id
        else:
            filtro, entidade = "nr_partido = %(e)s", int(entity_id)

        linha = self.conexao.execute(
            f"""SELECT nome_principal FROM entidade_matching
                 WHERE entity_type = %(tipo)s AND ativo AND {filtro}
                 LIMIT 1""",
            {"tipo": entity_type, "e": entidade},
        ).fetchone()
        return linha[0] if linha else None

    # -- montagem ----------------------------------------------------------------------

    def enriquecer(self, evento: NotificacaoClassifier) -> list[ItemEnriquecido]:
        """Um item por ator distinto citado na mensagem."""
        por_ator: dict[tuple[str, str], list[Any]] = {}
        for c in evento.classificacoes:
            por_ator.setdefault((c.entity_type, str(c.entity_id)), []).append(c)

        return [
            self._montar(evento, tipo, eid, cls)
            for (tipo, eid), cls in por_ator.items()
        ]

    def _montar(
        self,
        evento: NotificacaoClassifier,
        entity_type: str,
        entity_id: str,
        classificacoes: list[Any],
    ) -> ItemEnriquecido:
        avisos: list[str] = []
        linhas = self._linhas_do_ator(
            evento.article_id, evento.pipeline_version, entity_type, entity_id
        )

        indices = sorted(c.paragraph_index for c in classificacoes)

        # A correlação entre entrada da mensagem e linha do banco é INFERIDA: ordena-se o
        # banco por id_classificacao e a mensagem por paragraph_index, e casa-se posição a
        # posição. Só vale quando as contagens batem; se não baterem, grava-se
        # id_classificacao nulo em vez de inventar um vínculo possivelmente errado.
        casa = len(linhas) == len(indices)
        if not casa and linhas:
            avisos.append(
                f"mensagem traz {len(indices)} classificações e o banco tem {len(linhas)} "
                f"linhas para {entity_type}:{entity_id} no artigo {evento.article_id}; "
                "trechos gravados sem id_classificacao"
            )
        if not linhas:
            avisos.append(
                f"nenhuma linha em tabela_classificacao para {entity_type}:{entity_id} "
                f"no artigo {evento.article_id}; item montado só com a mensagem"
            )

        trechos: list[Trecho] = []
        for posicao, c in enumerate(sorted(classificacoes, key=lambda x: x.paragraph_index)):
            linha = linhas[posicao] if casa and posicao < len(linhas) else None
            trechos.append(
                Trecho(
                    paragraph_index=c.paragraph_index,
                    classificacao_sentimento=c.classificacao_sentimento,
                    score_confianca=c.score_confianca,
                    id_classificacao=linha["id_classificacao"] if linha else None,
                    texto=(linha["paragrafo_analisado"] or "") if linha else "",
                )
            )

        # O texto é a concatenação dos trechos na ordem do parágrafo — nunca a matéria
        # inteira, porque é só isso que existe no prisma-dev.
        if casa:
            partes = [t.texto for t in trechos if t.texto]
        else:
            partes = [(l["paragrafo_analisado"] or "") for l in linhas]
        texto = "\n\n".join(p for p in partes if p.strip())

        nome = self._nome_do_ator(entity_type, entity_id)
        if nome is None:
            nome = f"{entity_type}:{entity_id}"
            avisos.append(f"ator {entity_type}:{entity_id} sem nome em entidade_matching")

        primeira = linhas[0] if linhas else {}
        veiculo = primeira.get("nome_midia")
        if not veiculo:
            veiculo = "veículo não identificado"
            avisos.append(f"artigo {evento.article_id} sem mídia associada")

        publicacao = evento.data_publicacao_noticia or primeira.get("data_publicacao_noticia")
        if publicacao is None:
            avisos.append(f"artigo {evento.article_id} sem data de publicação")

        rotulo, regra = agregar_rotulo(trechos)
        titulo = titulo_sintetico(evento.article_id, nome)

        return ItemEnriquecido(
            item_id=f"art_{evento.article_id}__{entity_type}_{entity_id}",
            article_id=evento.article_id,
            entity_type=entity_type,
            entity_id=entity_id,
            ator_nome=nome,
            id_midia=primeira.get("id_midia"),
            veiculo=veiculo,
            link_noticia=evento.link_noticia or primeira.get("link_noticia") or "",
            data_publicacao=publicacao,
            janela_mes=publicacao.strftime("%Y-%m") if publicacao else "",
            titulo=titulo,
            texto=texto,
            rotulo=rotulo,
            regra_agregacao_versao=regra,
            trechos=trechos,
            # Cobre título E texto: os dois entram no prompt do resumo, então mudar o
            # título sem mudar o hash deixaria um resumo escrito sobre manchete velha.
            conteudo_hash=hashlib.sha256(
                (titulo + "\n" + texto).encode("utf-8")
            ).hexdigest(),
            avisos=avisos,
        )


__all__ = [
    "Enriquecedor",
    "ItemEnriquecido",
    "Trecho",
    "agregar_rotulo",
    "titulo_sintetico",
    "REGRA_AGREGACAO",
]
