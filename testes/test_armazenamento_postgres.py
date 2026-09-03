"""Integração com PostgreSQL. Pulado inteiro sem `ANALYSER_DATABASE_URL`.

Cada teste roda num schema descartável, criado e apagado na fixture — nenhum deles toca
as tabelas de `public`.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest

from analyser.armazenamento import ViolacaoAppendOnly
from analyser.schemas import RegistroPanorama, RegistroResumo

psycopg = pytest.importorskip("psycopg")

# O `.env` precisa ser lido na importação: o `skipif` abaixo é avaliado na coleta, antes de
# qualquer fixture rodar. Sem isto os testes seriam pulados mesmo com o banco configurado.
from analyser.config import carregar_dotenv  # noqa: E402

carregar_dotenv(Path(__file__).resolve().parents[1] / ".env")

DSN = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="ANALYSER_DATABASE_URL não definida")

SCHEMA = "analyser_pytest"


@pytest.fixture()
def conexao(raiz: Path) -> Iterator["psycopg.Connection"]:
    from analyser.config import carregar_dotenv

    carregar_dotenv(raiz / ".env")
    with psycopg.connect(os.environ["ANALYSER_DATABASE_URL"]) as con:
        con.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        con.execute(f"CREATE SCHEMA {SCHEMA}")
        # `public` no fim do caminho porque o enriquecimento junta com tabela_midia.
        con.execute(f"SET search_path TO {SCHEMA}, public")
        con.execute((raiz / "migrations" / "001_analyser.sql").read_text(encoding="utf-8"))
        con.execute(
            """INSERT INTO analyser_inbox_evento (payload_hash, article_id, pipeline_version, payload_json)
               VALUES (repeat('a', 64), 408, 'v1', '{}'::jsonb)"""
        )
        con.commit()
        try:
            yield con
        finally:
            con.rollback()
            con.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
            con.commit()


@pytest.fixture()
def destino(conexao):
    from analyser.armazenamento_postgres import ArmazenamentoPostgres, Ator

    return ArmazenamentoPostgres(
        conexao, Ator(entity_type="politico", ator_id="122418060191", nome="LULA")
    )


def resumo(item_id: str, janela: str = "2026-08", rotulo: str = "negativo") -> RegistroResumo:
    return RegistroResumo(
        item_id=item_id,
        ator_id="122418060191",
        janela=janela,
        rotulo=rotulo,
        veiculo="O Globo",
        url="https://exemplo.test/n",
        data=date(2026, 8, 29),
        titulo="Título",
        resumo="Duas frases. Bem curtas.",
        temas=["economia"],
        fonte_id=item_id,
        modelo="qwen3.5:9b",
        hash_prompt_resumo="a" * 64,
        criado_em="2026-08-29T10:00:00+00:00",
    )


def panorama(versao: int, fontes: list[str], janela: str = "2026-08") -> RegistroPanorama:
    return RegistroPanorama(
        ator_id="122418060191",
        janela=janela,
        eixo="negativo",
        versao=versao,
        formato="negativo",
        panorama={
            "ator_id": "122418060191",
            "janela": janela,
            "eixo": "negativo",
            "total_itens": len(fontes),
            "temas": [
                {"id": "obra-parada", "tema": "obra parada", "ocorrencias": 1, "fontes": fontes[:1]}
            ],
            "fontes": fontes,
            "texto": "As críticas registradas pela cobertura apontam a obra parada.",
        },
        avisos=[],
        lotes=1,
        modelo="qwen3.5:9b",
        hash_prompt_lote="b" * 64,
        hash_prompt_final="c" * 64,
        hash_prompt_formato="d" * 64,
        rodada="rodada.json",
        criado_em="2026-08-29T10:05:00+00:00",
    )


def test_resumo_ida_e_volta(destino) -> None:
    destino.gravar_resumo(resumo("art_408__politico_122418060191"))

    lidos = destino.ler_resumos()
    assert set(lidos) == {"art_408__politico_122418060191"}
    assert lidos["art_408__politico_122418060191"].rotulo == "negativo"
    assert lidos["art_408__politico_122418060191"].temas == ["economia"]


def test_resumo_nao_pode_ser_reescrito(destino) -> None:
    """O append-only sobrevive à fusão item+resumo: vira `WHERE resumo IS NULL`."""
    destino.gravar_resumo(resumo("art_408__politico_122418060191"))

    with pytest.raises(ViolacaoAppendOnly):
        destino.gravar_resumo(resumo("art_408__politico_122418060191"))


def test_leitura_por_janela_nao_traz_a_base_inteira(destino) -> None:
    destino.gravar_resumo(resumo("art_1__politico_122418060191", janela="2026-08"))
    destino.gravar_resumo(resumo("art_2__politico_122418060191", janela="2026-09"))

    da_janela = destino.ler_resumos_da_janela(ator_id="122418060191", janela="2026-08")

    assert [r.item_id for r in da_janela] == ["art_1__politico_122418060191"]


def test_versao_cresce_e_nao_sobrescreve(destino) -> None:
    fontes = ["art_408__politico_122418060191"]
    destino.gravar_resumo(resumo(fontes[0]))

    assert destino.proxima_versao(ator_id="122418060191", janela="2026-08", eixo="negativo") == 1
    destino.gravar_panorama(panorama(1, fontes))
    assert destino.proxima_versao(ator_id="122418060191", janela="2026-08", eixo="negativo") == 2

    with pytest.raises(ViolacaoAppendOnly):
        destino.gravar_panorama(panorama(1, fontes))


def test_janela_pendente_aparece_e_some_com_a_consolidacao(destino) -> None:
    """O ciclo que sustenta a recuperação após crash.

    Um item resumido deixa a janela pendente; gravar o panorama a limpa. Sem marcador
    paralelo e sem comparar timestamp — o estado é o próprio dado.
    """
    item = "art_408__politico_122418060191"
    destino.gravar_resumo(resumo(item))

    assert destino.janelas_pendentes() == ["2026-08"]

    destino.gravar_panorama(panorama(1, [item]))

    assert destino.janelas_pendentes() == []


def test_item_resumido_durante_a_consolidacao_mantem_a_janela_pendente(destino) -> None:
    """A corrida que derrubou a alternativa por timestamp.

    A consolidação vira `consolidado` só os itens que ELA leu. Um item resumido enquanto
    ela rodava não está na lista de fontes, continua `resumido`, e a janela reaparece —
    em vez de sumir do painel para sempre.
    """
    lido = "art_408__politico_122418060191"
    concorrente = "art_783__politico_122418060191"

    destino.gravar_resumo(resumo(lido))
    # A consolidação já leu suas fontes; o item abaixo chega depois disso.
    destino.gravar_resumo(resumo(concorrente))

    destino.gravar_panorama(panorama(1, [lido]))

    assert destino.janelas_pendentes() == ["2026-08"], (
        "o item resumido durante a consolidação foi tratado como consolidado"
    )


def test_auditoria_vira_linha_de_execucao(destino, conexao) -> None:
    destino.gravar_auditoria("20260903T120000_000", {
        "numero_rodada": 1,
        "capacidade_maxima_lote": 100,
        "ids_entrada": ["art_408__politico_122418060191"],
        "ids_resumidos_agora": ["art_408__politico_122418060191"],
        "panoramas_gravados": [],
        "modelo": "qwen3.5:9b",
    })

    n, modelo = conexao.execute(
        "SELECT quantidade_resumos, modelo FROM analyser_execucao"
    ).fetchone()
    assert (n, modelo) == (1, "qwen3.5:9b")
