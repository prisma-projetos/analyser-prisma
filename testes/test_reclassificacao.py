"""Teste 4 — reclassificar não obriga a re-resumir.

Este é o teste que dá sentido à regra 1. Se o rótulo entrasse no prompt do resumo, cada
correção do classificador invalidaria o resumo e obrigaria a pagar a notícia de novo.
"""

from __future__ import annotations

from pathlib import Path

from conftest import ler_json, rodar

from analyser.modelos import ModeloFake

ITEM_RECLASSIFICADO = "not_004"


def test_item_reclassificado_mantem_o_resumo_e_suja_a_janela(tmp_path: Path) -> None:
    """O item que volta com o rótulo trocado mantém o texto do resumo e suja a janela."""
    modelo = ModeloFake()

    rodar(tmp_path, "rodada_1.json", modelo)
    resumos_antes = ler_json(tmp_path / "resumos_item.json")
    antes = resumos_antes[ITEM_RECLASSIFICADO]

    modelo.zerar_contadores()
    segunda = rodar(tmp_path, "rodada_2.json", modelo)

    resumos_depois = ler_json(tmp_path / "resumos_item.json")
    depois = resumos_depois[ITEM_RECLASSIFICADO]

    # O resumo é exatamente o mesmo — não foi recalculado, nem sequer reescrito.
    assert depois["resumo"] == antes["resumo"]
    assert depois["temas"] == antes["temas"]
    assert depois["criado_em"] == antes["criado_em"]
    assert depois == antes

    # E o item veio do cache: nenhuma chamada de resumo foi gasta com ele.
    assert ITEM_RECLASSIFICADO in segunda.ids_do_cache
    assert ITEM_RECLASSIFICADO not in segunda.ids_resumidos_agora

    # Mas a janela dele foi marcada como suja e recalculada.
    assert ITEM_RECLASSIFICADO in segunda.itens_reclassificados
    assert "2026-08" in segunda.janelas_sujas


def test_rotulo_muda_de_camada_e_guarda_historico(tmp_path: Path) -> None:
    """O rótulo vive na camada mutável; o resumo, na imutável."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)
    rodar(tmp_path, "rodada_2.json", modelo)

    rotulos = ler_json(tmp_path / "rotulos.json")
    registro = rotulos[ITEM_RECLASSIFICADO]

    assert registro["rotulo_atual"] == "neutro"
    assert [h["rotulo"] for h in registro["historico"]] == ["negativo", "neutro"]

    # O resumo nunca guardou rótulo nenhum — não é assunto dele.
    resumo = ler_json(tmp_path / "resumos_item.json")[ITEM_RECLASSIFICADO]
    assert "rotulo" not in resumo
