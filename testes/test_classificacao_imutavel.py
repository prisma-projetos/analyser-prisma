"""A classificação de uma notícia é definitiva.

O mesmo `item_id` pode reaparecer para exercitar o cache, mas precisa trazer o mesmo
rótulo. Um valor diferente é erro de contrato e deve ser rejeitado antes de qualquer
efeito colateral.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import RAIZ, ler_json, rodar

from analyser.armazenamento import Armazenamento, SaidaLegadaIncompativel
from analyser.config import Config
from analyser.modelos import ModeloFake
from analyser.pipeline import Analyser, ConflitoClassificacao

ITEM_REPETIDO = "not_004"


def test_item_repetido_com_mesmo_rotulo_reusa_o_resumo(tmp_path: Path) -> None:
    """Uma repetição coerente usa o cache e não cria uma camada de rótulos."""
    modelo = ModeloFake()

    rodar(tmp_path, "rodada_1.json", modelo)
    antes = ler_json(tmp_path / "resumos_item.json")[ITEM_REPETIDO]

    modelo.zerar_contadores()
    segunda = rodar(tmp_path, "rodada_2.json", modelo)
    depois = ler_json(tmp_path / "resumos_item.json")[ITEM_REPETIDO]

    assert depois == antes
    assert depois["rotulo"] == "negativo"
    assert ITEM_REPETIDO in segunda.ids_do_cache
    assert ITEM_REPETIDO not in segunda.ids_resumidos_agora
    assert all(
        ITEM_REPETIDO not in motivo
        for motivos in segunda.motivo_janela.values()
        for motivo in motivos
    )
    assert not (tmp_path / "rotulos.json").exists()


def test_rotulo_diferente_para_id_existente_e_rejeitado_sem_escrita(
    tmp_path: Path,
) -> None:
    """O conflito aborta toda a rodada antes de resumir ou persistir qualquer item."""
    modelo = ModeloFake()
    rodar(tmp_path, "rodada_1.json", modelo)

    rodada = json.loads((RAIZ / "dados" / "rodada_1.json").read_text(encoding="utf-8"))
    item = next(item for item in rodada["itens"] if item["id"] == ITEM_REPETIDO)
    item["rotulo"] = "neutro"
    rodada["itens"] = [item]
    entrada = tmp_path / "rodada_conflitante.json"
    entrada.write_text(json.dumps(rodada, ensure_ascii=False), encoding="utf-8")

    resumos_antes = (tmp_path / "resumos_item.json").read_bytes()
    panoramas_antes = (tmp_path / "panoramas.json").read_bytes()
    auditorias_antes = sorted(tmp_path.glob("auditoria_*.json"))

    modelo.zerar_contadores()
    config = Config(entrada=entrada, saida=tmp_path, prompts=RAIZ / "prompts")
    with pytest.raises(ConflitoClassificacao, match=r"not_004.*imutável"):
        Analyser(config, modelo).executar()

    assert modelo.chamadas == 0
    assert (tmp_path / "resumos_item.json").read_bytes() == resumos_antes
    assert (tmp_path / "panoramas.json").read_bytes() == panoramas_antes
    assert sorted(tmp_path.glob("auditoria_*.json")) == auditorias_antes


def test_saida_legada_sem_rotulo_e_rejeitada_com_mensagem_clara(tmp_path: Path) -> None:
    """O formato antigo não é migrado silenciosamente nem produz erro Pydantic obscuro."""
    legado = {"not_001": {"item_id": "not_001", "resumo": "legado"}}
    (tmp_path / "resumos_item.json").write_text(
        json.dumps(legado, ensure_ascii=False), encoding="utf-8"
    )
    armazenamento = Armazenamento(Config(entrada="x", saida=tmp_path))

    with pytest.raises(SaidaLegadaIncompativel, match=r"formato anterior.*not_001"):
        armazenamento.ler_resumos()
