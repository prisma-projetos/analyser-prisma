"""Teste 5 — o panorama é versionado e nada é sobrescrito.

A camada recalculável acrescenta; ela não substitui. Uma versão antiga continua legível
depois de qualquer número de recálculos.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ler_json, rodar

from analyser.armazenamento import Armazenamento, ViolacaoAppendOnly
from analyser.config import Config
from analyser.modelos import ModeloFake


def _versoes(panoramas: list[dict], janela: str, eixo: str) -> list[int]:
    return sorted(
        p["versao"] for p in panoramas if p["janela"] == janela and p["eixo"] == eixo
    )


def test_agosto_ganha_v2_e_julho_nasce_em_v1(tmp_path: Path) -> None:
    """Depois de duas rodadas, agosto está na versão 2 e julho na 1."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)
    panoramas_v1 = ler_json(tmp_path / "panoramas.json")

    negativo_v1 = next(
        p for p in panoramas_v1 if p["janela"] == "2026-08" and p["eixo"] == "negativo"
    )

    rodar(tmp_path, "rodada_2.json", modelo)
    panoramas = ler_json(tmp_path / "panoramas.json")

    # Agosto foi recalculado: cada eixo ganhou uma versão nova.
    for eixo in ("negativo", "positivo", "neutro"):
        assert _versoes(panoramas, "2026-08", eixo) == [1, 2], f"eixo {eixo}"

    # Julho apareceu agora, por causa de uma notícia de julho descoberta na rodada 2.
    assert _versoes(panoramas, "2026-07", "negativo") == [1]

    # A v1 de agosto continua íntegra, byte a byte.
    negativo_v1_depois = next(
        p
        for p in panoramas
        if p["janela"] == "2026-08" and p["eixo"] == "negativo" and p["versao"] == 1
    )
    assert negativo_v1_depois == negativo_v1


def test_a_janela_vem_da_data_da_noticia(tmp_path: Path) -> None:
    """Notícia de julho descoberta em agosto pertence à janela de julho."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)
    segunda = rodar(tmp_path, "rodada_2.json", modelo)

    assert sorted(segunda.janelas_sujas) == ["2026-07", "2026-08"]

    resumos = ler_json(tmp_path / "resumos_item.json")
    assert resumos["not_017"]["data"].startswith("2026-07")
    assert resumos["not_017"]["janela"] == "2026-07"


def test_o_panorama_nunca_e_insumo_de_si_mesmo(tmp_path: Path) -> None:
    """A v2 é recalculada do zero, a partir dos resumos, não da v1.

    Se a v2 partisse da v1, o total de agosto no eixo negativo seria a soma acumulada.
    Ele é a contagem dos resumos daquela classificação na janela — nem mais, nem menos.
    """
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)
    rodar(tmp_path, "rodada_2.json", modelo)

    panoramas = ler_json(tmp_path / "panoramas.json")
    resumos = ler_json(tmp_path / "resumos_item.json")

    v2 = next(
        p
        for p in panoramas
        if p["janela"] == "2026-08" and p["eixo"] == "negativo" and p["versao"] == 2
    )
    esperado = {
        item_id
        for item_id, r in resumos.items()
        if r["janela"] == "2026-08" and r["rotulo"] == "negativo"
    }
    assert set(v2["panorama"]["fontes"]) == esperado
    assert v2["panorama"]["total_itens"] == len(esperado)

    # not_004 mantém a classificação definitiva e aparece nas duas versões.
    v1 = next(
        p
        for p in panoramas
        if p["janela"] == "2026-08" and p["eixo"] == "negativo" and p["versao"] == 1
    )
    assert "not_004" in v1["panorama"]["fontes"]
    assert "not_004" in v2["panorama"]["fontes"]


def test_resumo_existente_nunca_e_reescrito(tmp_path: Path) -> None:
    """A camada imutável se defende sozinha: não há caminho de código que a atualize."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)

    armazenamento = Armazenamento(Config(entrada="x", saida=tmp_path))
    existente = armazenamento.ler_resumos()["not_001"]

    with pytest.raises(ViolacaoAppendOnly):
        armazenamento.gravar_resumo(existente)

    # E a API nem oferece atualização ou remoção.
    assert not hasattr(armazenamento, "atualizar_resumo")
    assert not hasattr(armazenamento, "apagar_resumo")
