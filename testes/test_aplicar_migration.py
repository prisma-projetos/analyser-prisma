"""Regressões do executor seguro da migration 001."""

import pytest

from scripts.aplicar_migration import (
    COLUNAS_ESPERADAS,
    ESPERADAS,
    INDICES_ESPERADOS,
    RESTRICOES_ESPERADAS,
    SchemaIncompativel,
    _classificar_estado_tabelas,
    _problemas_catalogo,
)


def _copiar(mapeamento: dict[str, set[str]]) -> dict[str, set[str]]:
    return {tabela: set(nomes) for tabela, nomes in mapeamento.items()}


def test_schema_sem_tabelas_do_nucleo_e_classificado_como_vazio() -> None:
    assert _classificar_estado_tabelas([]) == "vazio"
    assert _classificar_estado_tabelas(["tabela_classificacao"]) == "vazio"


def test_schema_com_as_quatro_tabelas_e_classificado_como_completo() -> None:
    tabelas = [*ESPERADAS, "analyser_item_backup_maioria_v1"]

    assert _classificar_estado_tabelas(tabelas) == "completo"


def test_schema_parcial_e_recusado_sem_tentar_executar_a_ddl() -> None:
    with pytest.raises(SchemaIncompativel, match="schema parcial") as erro:
        _classificar_estado_tabelas(["analyser_inbox_evento"])

    assert "analyser_item" in str(erro.value)
    assert "A migration 001 não foi executada" in str(erro.value)


def test_catalogo_com_contrato_minimo_nao_tem_problemas() -> None:
    assert (
        _problemas_catalogo(
            _copiar(COLUNAS_ESPERADAS),
            _copiar(RESTRICOES_ESPERADAS),
            _copiar(INDICES_ESPERADOS),
        )
        == []
    )


def test_catalogo_incompleto_informa_cada_objeto_ausente() -> None:
    colunas = _copiar(COLUNAS_ESPERADAS)
    restricoes = _copiar(RESTRICOES_ESPERADAS)
    indices = _copiar(INDICES_ESPERADOS)
    colunas["analyser_item"].remove("resumo")
    restricoes["analyser_item"].remove("analyser_item_evento_id_fkey")
    indices["analyser_panorama"].remove("uk_analyser_panorama_versao")

    problemas = _problemas_catalogo(colunas, restricoes, indices)

    assert "analyser_item: colunas ausentes: resumo" in problemas
    assert (
        "analyser_item: restrições ausentes: analyser_item_evento_id_fkey" in problemas
    )
    assert (
        "analyser_panorama: índices ausentes: uk_analyser_panorama_versao"
        in problemas
    )
