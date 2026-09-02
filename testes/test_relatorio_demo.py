"""Validação estrutural do relatório demonstrativo com dados reais."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.gerar_relatorio_demo import (
    completar_identificacao_auditoria,
    escapar,
    montar_html,
    organizar_lotes,
)


def ler_json(caminho: Path) -> object:
    return json.loads(caminho.read_text(encoding="utf-8"))


def test_relatorio_amostra_intermediarios_e_todos_os_finais(raiz: Path) -> None:
    """A tela limita map/entrada, mas não omite nenhum campo dos panoramas finais."""
    rodada = ler_json(raiz / "dados" / "rodada_banco_demo_2026_08.json")
    metadados = ler_json(
        raiz / "dados" / "rodada_banco_demo_2026_08_metadados.json"
    )
    saida = raiz / "saida" / "demo_paragrafo_2026_08"
    resumos = ler_json(saida / "resumos_item.json")
    panoramas = ler_json(saida / "panoramas.json")
    auditorias = sorted(saida.glob("auditoria_*.json"))
    auditoria = completar_identificacao_auditoria(
        ler_json(auditorias[-1]), auditorias[-1], len(auditorias)
    )

    conteudo = montar_html(rodada, metadados, resumos, panoramas, auditoria)

    assert conteudo.count('class="card input-card"') == 6
    assert conteudo.count('class="card summary-card"') == 6
    assert conteudo.count('class="batch-card"') == 3
    assert conteudo.count('class="final-panel ') == 3
    assert "6 de 12 itens exibidos · todos os 12 foram processados" in conteudo
    assert "ConsolidacaoParcial" in conteudo
    assert conteudo.count("Lote 1: 4 notícias") == 3
    assert conteudo.count("Capacidade máxima configurada: 100") == 3
    assert "capacidade máxima 100" in conteudo
    assert "20260902T030116_054" in conteudo
    assert "numero_rodada" in conteudo
    assert "rodada_banco_demo_2026_08.json" in conteudo
    assert "<script" not in conteudo.lower()

    for registro in panoramas:
        panorama = registro["panorama"]
        if panorama["texto"]:
            assert escapar(panorama["texto"]) in conteudo
        for tema in panorama["temas"]:
            assert escapar(tema["tema"]) in conteudo
            for fonte in tema["fontes"]:
                assert escapar(fonte) in conteudo
        for fonte in panorama["fontes"]:
            assert escapar(fonte) in conteudo
        for aviso in registro["avisos"]:
            assert escapar(aviso) in conteudo


def test_conteudo_externo_e_escapado() -> None:
    assert escapar('<img src=x onerror="alert(1)">') == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    )


def test_lotes_exibem_quantidade_real_e_respeitam_capacidade() -> None:
    resumos = {
        f"not_{indice:03}": {
            "fonte_id": f"not_{indice:03}",
            "janela": "2026-08",
            "rotulo": "negativo",
        }
        for indice in range(1, 7)
    }

    lotes = organizar_lotes(resumos, capacidade_maxima=4)

    assert lotes == [
        ("2026-08", "negativo", 1, ["not_001", "not_002", "not_003", "not_004"]),
        ("2026-08", "negativo", 2, ["not_005", "not_006"]),
    ]
