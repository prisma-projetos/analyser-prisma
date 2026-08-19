"""Infraestrutura comum dos testes.

Todo teste que roda o pipeline escreve num diretório temporário. Nenhum deles toca o
`saida/` de verdade.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from analyser.config import Config  # noqa: E402
from analyser.modelos import ModeloFake  # noqa: E402
from analyser.pipeline import Analyser, ResultadoExecucao  # noqa: E402
from analyser.prompts import Prompts  # noqa: E402
from analyser.schemas import Rodada  # noqa: E402


@pytest.fixture
def raiz() -> Path:
    """Raiz do projeto."""
    return RAIZ


@pytest.fixture
def prompts() -> Prompts:
    """Repositório de prompts apontando para os arquivos reais do projeto."""
    return Prompts(raiz=RAIZ / "prompts")


@pytest.fixture
def rodada_1() -> Rodada:
    """Fixture real da rodada 1, validada."""
    return Rodada.model_validate_json(
        (RAIZ / "dados" / "rodada_1.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def rodada_2() -> Rodada:
    """Fixture real da rodada 2, validada."""
    return Rodada.model_validate_json(
        (RAIZ / "dados" / "rodada_2.json").read_text(encoding="utf-8")
    )


def montar_config(saida: Path, entrada: str, lote: int = 5) -> Config:
    """Configuração apontando para uma saída temporária."""
    return Config(
        entrada=RAIZ / "dados" / entrada,
        saida=saida,
        prompts=RAIZ / "prompts",
        lote=lote,
    )


def rodar(saida: Path, entrada: str, modelo: ModeloFake, lote: int = 5) -> ResultadoExecucao:
    """Roda uma rodada inteira contra um diretório de saída temporário."""
    return Analyser(montar_config(saida, entrada, lote), modelo).executar()


def ler_json(caminho: Path) -> object:
    """Lê um arquivo JSON de saída."""
    return json.loads(caminho.read_text(encoding="utf-8"))
