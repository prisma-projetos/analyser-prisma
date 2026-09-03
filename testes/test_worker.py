"""O ciclo completo: evento na inbox → itens → resumos → panorama.

Usa o `ModeloFake` para exercitar a orquestração sem depender de GPU. Roda num schema
descartável e é pulado sem `ANALYSER_DATABASE_URL`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from analyser.config import carregar_dotenv

psycopg = pytest.importorskip("psycopg")

carregar_dotenv(Path(__file__).resolve().parents[1] / ".env")
DSN = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="ANALYSER_DATABASE_URL não definida")

SCHEMA = "analyser_pytest_worker"


@pytest.fixture()
def conexao(raiz: Path) -> Iterator["psycopg.Connection"]:
    """Schema descartável com um evento real do outbox já na inbox."""
    with psycopg.connect(DSN) as con:
        payload = con.execute(
            """SELECT payload FROM public.classifier_analyser_outbox
                WHERE jsonb_array_length(payload->'classificacoes') BETWEEN 1 AND 8
                ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if payload is None:
            pytest.skip("outbox sem evento adequado")

        con.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        con.execute(f"CREATE SCHEMA {SCHEMA}")
        # `public` no caminho porque o enriquecimento lê tabela_classificacao e midia.
        con.execute(f"SET search_path TO {SCHEMA}, public")
        con.execute((raiz / "migrations" / "001_analyser.sql").read_text(encoding="utf-8"))
        con.execute(
            """INSERT INTO analyser_inbox_evento (payload_hash, article_id, pipeline_version, payload_json)
               VALUES (repeat('f', 64), %s, %s, %s)""",
            (payload[0]["article_id"], payload[0]["pipeline_version"], json.dumps(payload[0])),
        )
        con.commit()
        try:
            yield con
        finally:
            con.rollback()
            con.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
            con.commit()


@pytest.fixture()
def worker(conexao, raiz: Path):
    from analyser.config import Config
    from analyser.modelos import ModeloFake
    from analyser.worker import Worker

    config = Config(entrada=raiz / "dados" / "rodada_1.json", prompts=raiz / "prompts")
    return Worker(conexao, ModeloFake(), config)


def test_ciclo_completo_evento_ate_panorama(worker, conexao) -> None:
    """Um evento vira itens, resumos e panorama — e as duas trilhas se encontram."""
    eventos = worker.processar_eventos(limite=10)

    assert eventos.eventos_processados == 1, eventos.erros
    assert eventos.itens_criados >= 1
    assert eventos.resumos_gerados >= 1

    # Trilha 1 terminou: evento processado, itens resumidos.
    status_evento = conexao.execute(
        "SELECT status FROM analyser_inbox_evento"
    ).fetchone()[0]
    assert status_evento == "processado"

    resumidos = conexao.execute(
        "SELECT count(*) FROM analyser_item WHERE status = 'resumido'"
    ).fetchone()[0]
    assert resumidos >= 1

    # Trilha 2 encontra a janela sozinha, sem marcador nem timestamp.
    janelas = worker.consolidar_janelas(limite=10)
    assert janelas.janelas_consolidadas >= 1, janelas.erros
    assert janelas.panoramas_gravados >= 1

    # Depois de consolidar, nada mais pendente — o ciclo fecha.
    sobrando = conexao.execute(
        "SELECT count(*) FROM analyser_item WHERE status = 'resumido'"
    ).fetchone()[0]
    assert sobrando == 0, "itens continuam pendentes depois da consolidação"

    consolidados = conexao.execute(
        "SELECT count(*) FROM analyser_item WHERE status = 'consolidado'"
    ).fetchone()[0]
    assert consolidados >= 1

    texto = conexao.execute(
        "SELECT texto FROM analyser_panorama ORDER BY panorama_id LIMIT 1"
    ).fetchone()[0]
    assert texto and texto.strip(), "panorama gravado sem texto"


def test_reprocessar_o_mesmo_evento_nao_paga_o_modelo_de_novo(worker, conexao) -> None:
    """O cache é o que torna seguro rodar de novo depois de uma interrupção."""
    primeira = worker.processar_eventos(limite=10)
    assert primeira.resumos_gerados >= 1

    # Devolve o evento ao estado inicial, como se tivesse caído antes do UPDATE final.
    conexao.execute("UPDATE analyser_inbox_evento SET status = 'recebido'")
    conexao.commit()

    segunda = worker.processar_eventos(limite=10)

    assert segunda.eventos_processados == 1
    assert segunda.itens_criados == 0, "recriou item que já existia"
    assert segunda.resumos_gerados == 0, "pagou o modelo por resumo que já existia"
    assert segunda.itens_ja_existentes >= 1


def test_evento_invalido_vira_erro_sem_derrubar_o_lote(worker, conexao) -> None:
    """Uma mensagem ruim não pode impedir as boas de serem processadas."""
    conexao.execute(
        """INSERT INTO analyser_inbox_evento (payload_hash, article_id, pipeline_version, payload_json)
           VALUES (repeat('e', 64), -1, 'v1', '{"invalido": true}'::jsonb)"""
    )
    conexao.commit()

    resultado = worker.processar_eventos(limite=10)

    assert resultado.eventos_processados == 1, "o evento válido não foi processado"
    assert resultado.erros, "o evento inválido não virou erro registrado"

    status = dict(
        conexao.execute(
            "SELECT article_id, status FROM analyser_inbox_evento ORDER BY article_id"
        ).fetchall()
    )
    assert status[-1] == "erro"
