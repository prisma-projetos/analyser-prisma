"""A transformação de mensagem em itens notícia × ator.

Os testes de agregação rodam sem banco. Os de consulta usam o `prisma-dev` real, somente
leitura, e são pulados sem `ANALYSER_DATABASE_URL`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from analyser.config import carregar_dotenv
from analyser.enriquecimento import (
    REGRA_AGREGACAO,
    Trecho,
    separar_por_eixo,
    titulo_sintetico,
)

carregar_dotenv(Path(__file__).resolve().parents[1] / ".env")
DSN = os.environ.get("ANALYSER_DATABASE_URL", "").strip()


def trecho(indice: int, sentimento: str, score: float = 1.0) -> Trecho:
    return Trecho(
        paragraph_index=indice,
        classificacao_sentimento=sentimento,  # type: ignore[arg-type]
        score_confianca=score,
    )


# --------------------------------------------------------------------------------------
# Agregação de rótulo — sem banco
# --------------------------------------------------------------------------------------


def test_sinal_confiante_nao_e_derrotado_por_nao_sei() -> None:
    """A regressão do erro de categoria que a regra antiga cometia.

    Este teste é o inverso exato do que existia aqui até 3 de setembro de 2026. O antigo
    afirmava, e documentava como limitação conhecida, que uma classificação confiante era
    derrotada por catorze "não sei" — porque `score_confianca = 0` era gravado como
    `neutro` e votava com peso igual.

    Medido na base: score 0 aparece EXCLUSIVAMENTE com neutro, nas 56.689 linhas do
    Classifier. "Neutro" e "não achei marcador" são o mesmo valor, e deixar uma abstenção
    vencer uma afirmação apagava sinal real: 121 itens (14%) viraram neutro tendo
    classificação confiante.
    """
    trechos = [trecho(0, "positivo", score=1.0)] + [
        trecho(i, "neutro", score=0.0) for i in range(1, 15)
    ]
    recortes = separar_por_eixo(trechos)

    assert set(recortes) == {"positivo"}
    assert len(recortes["positivo"]) == 15, "os 14 sem sinal entram como contexto"


def test_materia_com_os_dois_sinais_rende_dois_recortes() -> None:
    """Elogio e crítica na mesma matéria não se anulam — cada um vira seu recorte.

    Sob maioria simples isto virava um empate, e empate virava neutro: os dois sinais
    eram apagados de uma vez. Num produto cujas abas são "Elogios" e "Críticas", era a
    matéria mais informativa que sumia das duas.
    """
    recortes = separar_por_eixo(
        [trecho(0, "positivo"), trecho(1, "positivo"),
         trecho(2, "negativo"), trecho(3, "negativo")]
    )

    assert set(recortes) == {"positivo", "negativo"}
    assert [t.paragraph_index for t in recortes["positivo"]] == [0, 1]
    assert [t.paragraph_index for t in recortes["negativo"]] == [2, 3]


def test_trechos_sem_sinal_entram_em_todos_os_recortes() -> None:
    """Contexto é compartilhado, não dividido.

    Um trecho positivo isolado costuma não dizer quem, quando, nem em que circunstância.
    Como o resumo de um item é escrito apenas a partir dos trechos DELE, descartar o
    contexto deixaria elogio e crítica pendurados no ar.
    """
    recortes = separar_por_eixo(
        [trecho(0, "neutro", score=0.0), trecho(1, "positivo"),
         trecho(2, "negativo"), trecho(3, "neutro", score=0.0)]
    )

    for eixo in ("positivo", "negativo"):
        indices = [t.paragraph_index for t in recortes[eixo]]
        assert 0 in indices and 3 in indices, f"contexto faltando no recorte {eixo}"
        assert indices == sorted(indices), "ordem de parágrafo precisa ser preservada"


def test_sem_sinal_nenhum_e_neutro_com_tudo() -> None:
    """Neutro deixa de ser o resultado de uma votação e passa a ser a ausência de sinal.

    Continua sendo a maioria — 656 dos 859 itens (76%) não têm nenhum marcador. Esse teto
    é do Classifier, não da regra de agregação: nenhuma regra inventa sinal onde não há.
    """
    recortes = separar_por_eixo([trecho(i, "neutro", score=0.0) for i in range(3)])

    assert set(recortes) == {"neutro"}
    assert len(recortes["neutro"]) == 3


def test_sem_trechos_e_neutro() -> None:
    assert set(separar_por_eixo([])) == {"neutro"}


def test_titulo_sintetico_e_deterministico() -> None:
    """O mesmo item gera sempre o mesmo título, então o conteudo_hash é estável."""
    a = titulo_sintetico(408, "LULA")
    b = titulo_sintetico(408, "LULA")
    assert a == b
    assert "408" in a and "LULA" in a


def test_trecho_json_nao_copia_o_texto_do_paragrafo() -> None:
    """O texto já está em tabela_classificacao; duplicá-lo dobraria o maior conteúdo do banco."""
    t = Trecho(0, "negativo", 1.0, id_classificacao=55524, texto="parágrafo inteiro aqui")  # type: ignore[arg-type]
    assert t.para_json() == {
        "paragraph_index": 0,
        "classificacao_sentimento": "negativo",
        "score_confianca": 1.0,
        "id_classificacao": 55524,
    }


# --------------------------------------------------------------------------------------
# Consulta ao prisma-dev — somente leitura
# --------------------------------------------------------------------------------------

pytest_postgres = pytest.mark.skipif(not DSN, reason="ANALYSER_DATABASE_URL não definida")


@pytest_postgres
def test_enriquece_um_evento_real_do_outbox() -> None:
    """Pega um payload que o Classifier realmente publicou e monta os itens dele."""
    import psycopg

    from analyser.enriquecimento import Enriquecedor
    from analyser.schemas import NotificacaoClassifier

    with psycopg.connect(DSN) as con:
        payload = con.execute(
            "SELECT payload FROM classifier_analyser_outbox ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        evento = NotificacaoClassifier.model_validate(payload)

        itens = Enriquecedor(con).enriquecer(evento)

    assert itens, "nenhum item montado a partir de um evento real"

    # A unidade passou a ser notícia × ator × EIXO em 3 de setembro de 2026: um ator com
    # elogio e crítica na mesma matéria rende dois itens. O que continua único é a
    # combinação com o eixo — e é isso que faz `item_id` ser chave.
    recortes = {(i.entity_type, i.entity_id, i.rotulo) for i in itens}
    assert len(recortes) == len(itens), "dois itens para o mesmo ator no mesmo eixo"
    assert len({i.item_id for i in itens}) == len(itens), "item_id repetido"

    for item in itens:
        assert item.item_id.startswith(f"art_{evento.article_id}__")
        assert item.item_id.endswith(f"__{item.rotulo}"), "eixo ausente no item_id"
        assert item.rotulo in ("positivo", "negativo", "neutro")
        assert item.regra_agregacao_versao == REGRA_AGREGACAO
        assert item.trechos, "item sem trechos"
        assert len(item.conteudo_hash) == 64
        # O nome tem de ter sido resolvido em entidade_matching, não ficar como id cru.
        assert item.ator_nome and not item.ator_nome.startswith(item.entity_type + ":")


@pytest_postgres
def test_texto_e_so_dos_trechos_do_ator() -> None:
    """O texto do item reúne os parágrafos daquele ator — nunca a matéria inteira.

    Num artigo com vários atores, cada item recebe um recorte diferente. Se dois itens
    saíssem com o mesmo texto, a concatenação estaria ignorando o filtro por entidade.
    """
    import psycopg

    from analyser.enriquecimento import Enriquecedor
    from analyser.schemas import NotificacaoClassifier

    with psycopg.connect(DSN) as con:
        # Um evento que cite pelo menos dois atores distintos.
        for (payload,) in con.execute(
            "SELECT payload FROM classifier_analyser_outbox ORDER BY id DESC LIMIT 40"
        ).fetchall():
            evento = NotificacaoClassifier.model_validate(payload)
            atores = {(c.entity_type, str(c.entity_id)) for c in evento.classificacoes}
            if len(atores) >= 2:
                break
        else:
            pytest.skip("nenhum evento recente com dois atores distintos")

        itens = Enriquecedor(con).enriquecer(evento)

    com_texto = [i for i in itens if i.texto]
    if len(com_texto) < 2:
        pytest.skip("evento não rendeu dois itens com texto")

    assert len({i.texto for i in com_texto}) > 1, (
        "todos os itens saíram com o mesmo texto — o filtro por entidade não está valendo"
    )
