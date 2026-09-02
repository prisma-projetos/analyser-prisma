"""Contrato explícito da identificação da execução e dos lotes na auditoria."""

from __future__ import annotations

import json
from pathlib import Path

from analyser.modelos import ModeloFake

from conftest import ler_json, rodar


def test_auditoria_separa_execucao_capacidade_e_quantidade_real(
    tmp_path: Path,
) -> None:
    modelo = ModeloFake()

    primeira = rodar(tmp_path, "rodada_1.json", modelo, lote=100)
    auditoria = ler_json(Path(primeira.caminho_auditoria))

    assert auditoria["execucao_id"] == primeira.carimbo
    assert auditoria["numero_rodada"] == 1
    assert auditoria["arquivo_entrada"] == "rodada_1.json"
    assert auditoria["capacidade_maxima_lote"] == 100
    assert auditoria["lotes_processados"] == [
        {
            "janela": "2026-08",
            "eixo": "negativo",
            "numero_lote": 1,
            "quantidade_noticias": 6,
        },
        {
            "janela": "2026-08",
            "eixo": "positivo",
            "numero_lote": 1,
            "quantidade_noticias": 2,
        },
        {
            "janela": "2026-08",
            "eixo": "neutro",
            "numero_lote": 1,
            "quantidade_noticias": 2,
        },
    ]

    # Compatibilidade: consumidores antigos continuam encontrando os aliases.
    assert auditoria["rodada"] == auditoria["arquivo_entrada"]
    assert auditoria["lote"] == auditoria["capacidade_maxima_lote"]


def test_numero_rodada_avanca_sem_confundir_com_nome_do_arquivo(
    tmp_path: Path,
) -> None:
    primeira = rodar(tmp_path, "rodada_1.json", ModeloFake(), lote=100)
    segunda = rodar(tmp_path, "rodada_1.json", ModeloFake(), lote=100)

    auditoria_primeira = ler_json(Path(primeira.caminho_auditoria))
    auditoria_segunda = ler_json(Path(segunda.caminho_auditoria))

    assert auditoria_primeira["numero_rodada"] == 1
    assert auditoria_segunda["numero_rodada"] == 2
    assert auditoria_segunda["arquivo_entrada"] == "rodada_1.json"
    assert auditoria_segunda["execucao_id"] != auditoria_primeira["execucao_id"]


def test_auditoria_legada_e_contada_sem_ser_reescrita(tmp_path: Path) -> None:
    legada = tmp_path / "auditoria_20260831T200014_719.json"
    conteudo_original = json.dumps({"rodada": "rodada_antiga.json"}, indent=2) + "\n"
    legada.write_text(conteudo_original, encoding="utf-8")

    resultado = rodar(tmp_path, "rodada_1.json", ModeloFake(), lote=100)
    auditoria_nova = ler_json(Path(resultado.caminho_auditoria))

    assert auditoria_nova["numero_rodada"] == 2
    assert legada.read_text(encoding="utf-8") == conteudo_original
