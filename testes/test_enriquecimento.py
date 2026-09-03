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
    agregar_rotulo,
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


def test_maioria_simples_vence() -> None:
    """Dois negativos contra um neutro dão negativo."""
    rotulo, regra = agregar_rotulo(
        [trecho(0, "negativo"), trecho(2, "negativo"), trecho(5, "neutro")]
    )
    assert (rotulo, regra) == ("negativo", REGRA_AGREGACAO)


def test_empate_vira_neutro() -> None:
    """Quando a cobertura se divide igualmente, não se afirma tendência."""
    rotulo, _ = agregar_rotulo([trecho(0, "negativo"), trecho(1, "positivo")])
    assert rotulo == "neutro"


def test_trecho_unico_define_o_item() -> None:
    rotulo, _ = agregar_rotulo([trecho(3, "positivo")])
    assert rotulo == "positivo"


def test_sem_trechos_e_neutro() -> None:
    assert agregar_rotulo([])[0] == "neutro"


def test_score_zero_vota_com_o_mesmo_peso() -> None:
    """Registra a limitação conhecida da regra atual, em vez de escondê-la.

    Na amostra real, 65% dos trechos têm score 0 e TODOS são neutro — o que significa "o
    léxico não achou marcador", não "a cobertura é neutra". Sob maioria simples, uma única
    classificação confiante é derrotada por catorze "não sei".

    Este teste falha de propósito no dia em que a regra mudar, forçando quem mudar a
    reconhecer que o comportamento antigo era este.
    """
    trechos = [trecho(0, "positivo", score=1.0)] + [
        trecho(i, "neutro", score=0.0) for i in range(1, 15)
    ]
    rotulo, _ = agregar_rotulo(trechos)
    assert rotulo == "neutro", "a regra atual deixa o score 0 derrotar a classificação confiante"


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

    atores = {(i.entity_type, i.entity_id) for i in itens}
    assert len(atores) == len(itens), "mais de um item para o mesmo ator"

    for item in itens:
        assert item.item_id.startswith(f"art_{evento.article_id}__")
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
