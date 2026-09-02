"""Regressões das configurações públicas do Analyser."""

from pathlib import Path

from analyser.cli import montar_parser
from analyser.config import LOTE_PADRAO, Config


def test_lote_padrao_e_cem_na_configuracao_e_na_cli() -> None:
    """A constante, a dataclass e a CLI devem expor o mesmo lote padrão."""
    config = Config(entrada=Path("rodada.json"))
    argumentos = montar_parser().parse_args(["--entrada", "rodada.json"])

    assert LOTE_PADRAO == 100
    assert config.lote == 100
    assert argumentos.lote == 100
