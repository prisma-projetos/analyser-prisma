"""Teste 3 — o cache é respeitado.

A propriedade que o protótipo existe para demonstrar: a segunda rodada não reprocessa o
que já foi processado. O custo por notícia é pago uma vez só.
"""

from __future__ import annotations

from pathlib import Path

from conftest import rodar

from analyser.modelos import ModeloFake


def test_segunda_rodada_so_resume_o_que_e_inedito(tmp_path: Path) -> None:
    """Depois da rodada 1, a rodada 2 chama o modelo só para os itens inéditos."""
    modelo = ModeloFake()

    primeira = rodar(tmp_path, "rodada_1.json", modelo)
    assert modelo.chamadas_por_tarefa["resumo_item"] == 10
    assert primeira.economia == (0, 10)

    # Zera o contador entre as rodadas, para medir só o que a segunda gastou.
    modelo.zerar_contadores()

    segunda = rodar(tmp_path, "rodada_2.json", modelo)

    # A rodada 2 traz 8 itens, dos quais 7 são inéditos (6 de agosto e 1 de julho) e 1 já
    # havia sido resumido na rodada 1, repetindo a mesma classificação definitiva.
    ineditos = [i for i in segunda.ids_entrada if i not in primeira.ids_entrada]
    assert len(ineditos) == 7
    assert segunda.ids_do_cache == ["not_004"]

    assert modelo.chamadas_por_tarefa["resumo_item"] == len(ineditos), (
        "o número de chamadas de resumo tem que ser exatamente o número de itens "
        "inéditos — os repetidos não podem gerar chamada"
    )
    assert segunda.economia == (1, 7)


def test_reexecutar_a_mesma_rodada_nao_gasta_nada(tmp_path: Path) -> None:
    """Rodar a mesma rodada de novo não chama o modelo nem suja janela nenhuma."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)

    modelo.zerar_contadores()
    repetida = rodar(tmp_path, "rodada_1.json", modelo)

    assert modelo.chamadas == 0
    assert repetida.economia == (10, 0)
    assert repetida.janelas_sujas == []
    assert repetida.panoramas == []
