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

from analyser.config import LIMIAR_CONFIANCA, Eixo
from analyser.schemas import NotificacaoClassifier

#: Versão da regra que transforma vários rótulos de trecho em um só. Gravada em cada item
#: para que trocar a regra no futuro não torne os rótulos antigos incomparáveis com os novos.
REGRA_AGREGACAO = "eixo-por-sinal-v2"

#: Separador entre trechos na concatenação do texto de um item.
SEPARADOR = '\n\n'


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


def separar_por_eixo(
    trechos: list[Trecho], limiar: float = LIMIAR_CONFIANCA
) -> dict[Eixo, list[Trecho]]:
    """Divide os trechos de um ator nos recortes que a matéria de fato sustenta.

    Substituiu `agregar_rotulo` (maioria simples, empate vira neutro) em 3 de setembro de
    2026. A regra antiga tinha um erro de categoria: `score_confianca = 0` significa "o
    léxico não achou marcador", nunca "a cobertura é neutra" — medido, é o ÚNICO valor com
    que `neutro` aparece em toda a base. Sob maioria simples, essas abstenções votavam com
    o mesmo peso de classificações confiantes, e ganhavam: 121 itens (14%) viraram neutro
    tendo sinal confiante, e o eixo neutro concentrou 90,5% de tudo.

    A regra nova não decide, SEPARA. Não há maioria a apurar nem empate a desempatar:

        tem trecho positivo confiante?  -> existe um recorte positivo
        tem trecho negativo confiante?  -> existe um recorte negativo
        não tem nenhum dos dois?        -> um recorte neutro, com tudo

    Uma matéria que elogia E critica passa a render os dois recortes, em vez de colapsar
    num neutro que apagava os dois — o que importa para um produto cujas abas são
    exatamente "Elogios" e "Críticas".

    Os trechos abaixo do limiar entram em TODOS os recortes, como contexto compartilhado.
    Descartá-los deixaria o elogio ou a crítica sem o parágrafo que diz quem, quando e em
    que circunstância — e o resumo é escrito só a partir do que está no recorte.

    A ordem de parágrafo é preservada: o texto do item é a concatenação desses trechos, e
    fora de ordem ele deixa de se ler como matéria.

    Efeito medido sobre os 859 itens existentes: neutro 90,5% -> 75,3%, positivo
    6,4% -> 17,2%, negativo 3,1% -> 7,5%. Apenas 12 itens (1,4%) têm os dois sinais e
    portanto se dividem em dois.
    """
    if not trechos:
        return {"neutro": []}

    def em_ordem(lista: list[Trecho]) -> list[Trecho]:
        return sorted(lista, key=lambda t: t.paragraph_index)

    confiantes = [t for t in trechos if t.score_confianca >= limiar and t.score_confianca > 0]
    contexto = [t for t in trechos if t not in confiantes]

    positivos = [t for t in confiantes if t.classificacao_sentimento == "positivo"]
    negativos = [t for t in confiantes if t.classificacao_sentimento == "negativo"]

    if not positivos and not negativos:
        return {"neutro": em_ordem(trechos)}

    recortes: dict[Eixo, list[Trecho]] = {}
    if positivos:
        recortes["positivo"] = em_ordem(positivos + contexto)
    if negativos:
        recortes["negativo"] = em_ordem(negativos + contexto)
    return recortes


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

        # Um ator pode render MAIS de um item: uma matéria que elogia e critica vira um
        # recorte positivo e um negativo. Ver `separar_por_eixo`.
        return [
            item
            for (tipo, eid), cls in por_ator.items()
            for item in self._montar(evento, tipo, eid, cls)
        ]

    def _montar(
        self,
        evento: NotificacaoClassifier,
        entity_type: str,
        entity_id: str,
        classificacoes: list[Any],
    ) -> list[ItemEnriquecido]:
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

        titulo = titulo_sintetico(evento.article_id, nome)
        recortes = separar_por_eixo(trechos)

        itens: list[ItemEnriquecido] = []
        for rotulo, trechos_do_eixo in recortes.items():
            # O texto é recortado junto: o resumo de um item é escrito só a partir dos
            # trechos DELE. Por isso o hash muda entre recortes e cada um paga o seu
            # resumo — mas só quando há de fato dois sinais. Quando há um só, o recorte
            # contém todos os trechos e o texto é idêntico ao de antes.
            partes_eixo = [tr.texto for tr in trechos_do_eixo if tr.texto]
            texto_eixo = SEPARADOR.join(p for p in partes_eixo if p.strip()) or texto

            itens.append(self._item_do_recorte(
                evento=evento, entity_type=entity_type, entity_id=entity_id,
                nome=nome, titulo=titulo, texto=texto_eixo, rotulo=rotulo,
                trechos=trechos_do_eixo, primeira=primeira, veiculo=veiculo,
                publicacao=publicacao, avisos=avisos,
            ))
        return itens

    def _item_do_recorte(
        self, *, evento: NotificacaoClassifier, entity_type: str, entity_id: str,
        nome: str, titulo: str, texto: str, rotulo: Eixo, trechos: list[Trecho],
        primeira: dict[str, Any], veiculo: str, publicacao: Any, avisos: list[str],
    ) -> ItemEnriquecido:
        """Monta o item de UM recorte de eixo.

        O `item_id` carrega o eixo porque a unidade deixou de ser notícia × ator e passou
        a ser notícia × ator × eixo. Isso tem um efeito colateral bom: a regra de
        classificação imutável (`ConflitoClassificacao`) deixa de ser uma checagem em
        tempo de execução e vira propriedade da chave — um item não tem como mudar de
        rótulo, porque com outro rótulo ele é outro item.
        """
        return ItemEnriquecido(
            item_id=f"art_{evento.article_id}__{entity_type}_{entity_id}__{rotulo}",
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
            regra_agregacao_versao=REGRA_AGREGACAO,
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
    "separar_por_eixo",
    "titulo_sintetico",
    "REGRA_AGREGACAO",
]
