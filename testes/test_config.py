"""Regressões das configurações públicas do Analyser."""

from pathlib import Path

import pytest

from analyser.cli import montar_parser
from analyser.config import (
    CONTEXTO_TOKENS,
    LOTE_MAXIMO,
    LOTE_PADRAO,
    Config,
    custo_estimado_do_lote,
    lote_que_cabe,
)


def test_lote_padrao_e_o_mesmo_na_configuracao_e_na_cli() -> None:
    """A constante, a dataclass e a CLI devem expor o mesmo lote padrão."""
    config = Config(entrada=Path("rodada.json"))
    argumentos = montar_parser().parse_args(["--entrada", "rodada.json"])

    assert config.lote == LOTE_PADRAO
    assert argumentos.lote == LOTE_PADRAO


def test_lote_padrao_e_derivado_do_contexto_e_nao_escrito_a_mao() -> None:
    """O lote precisa ACOMPANHAR o contexto, não ser um número fixo ao lado dele.

    Era 100 fixo, escolhido quando as janelas tinham dezenas de itens. Quando agosto de
    2026 acumulou 117 matérias do PL, o lote encheu pela primeira vez e o prompt passou
    do contexto. Um lote menor que o contexto por coincidência não é a mesma coisa que um
    lote derivado dele.
    """
    assert LOTE_PADRAO == lote_que_cabe(CONTEXTO_TOKENS)
    assert lote_que_cabe(8192) < lote_que_cabe(16384) < LOTE_MAXIMO


@pytest.mark.parametrize("contexto", [8192, 16384, 32768, 65536])
def test_o_lote_calculado_sempre_deixa_espaco_para_a_resposta(contexto: int) -> None:
    """Prompt MAIS resposta precisam caber no contexto, com folga.

    Esta é a regressão do incidente. O contexto não é o orçamento do prompt: prompt e
    resposta disputam o mesmo espaço, e quem estourar não recebe erro — o servidor corta
    o prompt pela cabeça em silêncio e responde como se nada fosse.

    A folga importa tanto quanto o teto: o lote que falhou operava a 96% de ocupação, uma
    faixa em que a variação normal entre execuções decide entre sucesso e falha.
    """
    prompt, resposta = custo_estimado_do_lote(lote_que_cabe(contexto))

    assert prompt + resposta <= contexto
    assert (prompt + resposta) / contexto <= 0.85
