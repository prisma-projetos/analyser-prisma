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


def test_execucao_auditada_registra_as_duas_trilhas(worker, conexao) -> None:
    """A invocação do worker deixa contagens e limites consultáveis no PostgreSQL."""
    resultado = worker.executar(limite_eventos=10, limite_consolidar=10)

    assert resultado.execucao_id is not None
    assert resultado.eventos_processados == 1, resultado.erros
    assert resultado.itens_criados >= 1
    assert resultado.resumos_gerados >= 1
    assert resultado.panoramas_gravados >= 1

    linha = conexao.execute(
        """SELECT numero_execucao, status, capacidade_lote, quantidade_eventos,
                  quantidade_itens, quantidade_resumos, quantidade_panoramas,
                  modelo, finalizado_em, auditoria_json, erro
             FROM analyser_execucao WHERE execucao_id = %s""",
        (resultado.execucao_id,),
    ).fetchone()
    assert linha is not None
    numero, status, capacidade, eventos, itens, resumos, panoramas, modelo, fim, dados, erro = linha
    assert numero == 1
    assert status == "concluida"
    assert capacidade == 10
    assert (eventos, itens, resumos, panoramas) == (
        resultado.eventos_processados,
        resultado.itens_criados,
        resultado.resumos_gerados,
        resultado.panoramas_gravados,
    )
    assert modelo == "fake"
    assert fim is not None
    assert erro is None
    assert dados["limites"] == {"eventos": 10, "consolidar": 10}
    assert dados["resultado"]["itens_criados"] == resultado.itens_criados


def test_consolidar_tudo_fecha_a_fotografia_de_janelas_pendentes(
    worker, conexao
) -> None:
    """O modo sem teto descobre ator + mês e não exige que o operador calcule K."""
    eventos = worker.processar_eventos(limite=10)
    assert eventos.eventos_processados == 1, eventos.erros

    pendentes_antes = worker.quantidade_janelas_pendentes()
    assert pendentes_antes >= 1

    resultado = worker.executar(consolidar_tudo=True)

    assert resultado.janelas_consolidadas == pendentes_antes, resultado.erros
    assert worker.quantidade_janelas_pendentes() == 0

    capacidade, dados = conexao.execute(
        """SELECT capacidade_lote, auditoria_json FROM analyser_execucao
             WHERE execucao_id = %s""",
        (resultado.execucao_id,),
    ).fetchone()
    assert capacidade == 0, "zero representa consolidação sem teto numérico"
    assert dados["limites"] == {
        "eventos": 0,
        "consolidar": 0,
        "consolidar_tudo": True,
    }
    assert dados["unidade_capacidade_lote"] == "todas_as_janelas_pendentes"


def test_eventos_com_consolidar_tudo_fecha_o_fluxo_automaticamente(
    worker, conexao
) -> None:
    """Quando os modos são combinados, a consolidação roda depois dos eventos."""
    resultado = worker.executar(limite_eventos=10, consolidar_tudo=True)

    assert resultado.eventos_processados == 1, resultado.erros
    assert resultado.resumos_gerados >= 1
    assert resultado.janelas_consolidadas >= 1
    assert resultado.panoramas_gravados >= 1
    assert worker.quantidade_janelas_pendentes() == 0

    dados = conexao.execute(
        "SELECT auditoria_json FROM analyser_execucao WHERE execucao_id = %s",
        (resultado.execucao_id,),
    ).fetchone()[0]
    assert dados["limites"]["eventos"] == 10
    assert dados["limites"]["consolidar_tudo"] is True


def test_consolidar_tudo_e_limite_numerico_sao_excludentes(worker) -> None:
    """Dois modos contraditórios devem falhar antes de criar uma execução."""
    with pytest.raises(ValueError, match="não ambos"):
        worker.executar(limite_consolidar=1, consolidar_tudo=True)


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


def test_evento_invalido_marca_execucao_como_falha_sem_derrubar_o_lote(worker, conexao) -> None:
    """Uma mensagem ruim não pode impedir as boas de serem processadas."""
    conexao.execute(
        """INSERT INTO analyser_inbox_evento (payload_hash, article_id, pipeline_version, payload_json)
           VALUES (repeat('e', 64), -1, 'v1', '{"invalido": true}'::jsonb)"""
    )
    conexao.commit()

    resultado = worker.executar(limite_eventos=10)

    assert resultado.eventos_processados == 1, "o evento válido não foi processado"
    assert resultado.erros, "o evento inválido não virou erro registrado"

    status = dict(
        conexao.execute(
            "SELECT article_id, status FROM analyser_inbox_evento ORDER BY article_id"
        ).fetchall()
    )
    assert status[-1] == "erro"

    status_execucao, erro_execucao = conexao.execute(
        "SELECT status, erro FROM analyser_execucao WHERE execucao_id = %s",
        (resultado.execucao_id,),
    ).fetchone()
    assert status_execucao == "falhou"
    assert "evento" in erro_execucao


def test_interrupcao_deixa_execucao_em_andamento_e_nao_bloqueia_retomada(
    worker, conexao, monkeypatch
) -> None:
    """Uma interrupção abrupta é visível e a próxima execução continua independente."""
    processar_original = worker.processar_eventos

    def interromper(*, limite: int) -> ResultadoWorker:
        raise KeyboardInterrupt("interrupção de teste")

    monkeypatch.setattr(worker, "processar_eventos", interromper)
    with pytest.raises(KeyboardInterrupt):
        worker.executar(limite_eventos=1)

    execucao_interrompida = conexao.execute(
        "SELECT execucao_id, status, finalizado_em FROM analyser_execucao"
    ).fetchone()
    assert execucao_interrompida[1:] == ("em_andamento", None)

    monkeypatch.setattr(worker, "processar_eventos", processar_original)
    retomada = worker.executar(limite_eventos=10)

    assert retomada.eventos_processados == 1, retomada.erros
    status = conexao.execute(
        "SELECT status FROM analyser_execucao WHERE execucao_id = %s",
        (retomada.execucao_id,),
    ).fetchone()[0]
    assert status == "concluida"
