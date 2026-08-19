"""Testes 1 e 2 — as duas metades da regra 1.

Regra 1: o rótulo entra no prompt da consolidação e nunca no prompt do resumo por item.
Mesmo onde ele entra, o prompt não pode encomendar o tom.
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from analyser.config import EIXOS
from analyser.prompts import Prompts
from analyser.schemas import Rodada

#: Radicais dos três rótulos. Usar o radical, e não a palavra exata, pega também plural e
#: flexão de gênero — e pega "dispositivo", que contém "positiv" por acidente.
RADICAIS_ROTULO = ("positiv", "negativ", "neutr")

#: Termos de ênfase valorativa proibidos no reduce. O prompt pode dizer QUAL conjunto está
#: consolidando; não pode dizer COMO escrever sobre ele.
TERMOS_DE_TOM = (
    "gravidade", "grave", "denuncia", "escandalo", "realizacao", "conquista", "merito",
    "elogio", "positivamente", "negativamente", "duro", "favoravel", "desfavoravel",
)


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos — a checagem não pode ser burlada por acentuação."""
    decomposto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def achados(texto: str, termos: tuple[str, ...]) -> list[str]:
    """Termos proibidos presentes no texto."""
    base = normalizar(texto)
    return [t for t in termos if t in base]


# --------------------------------------------------------------------------------------
# Teste 1 — o map não vê o rótulo
# --------------------------------------------------------------------------------------


def test_map_nao_vaza_rotulo(prompts: Prompts, rodada_1: Rodada, rodada_2: Rodada) -> None:
    """O prompt do resumo por item, renderizado com dados reais, não menciona rótulo.

    É desta metade que dependem duas propriedades que o resto do módulo assume: o resumo
    continuar válido quando o item é reclassificado, e o resumo ser imparcial — no
    sistema ele é exibido dentro do agrupamento em que a matéria caiu, e não pode assumir
    o tom desse agrupamento.
    """
    itens = list(rodada_1.itens) + list(rodada_2.itens)
    assert itens, "o fixture precisa ter itens para o teste valer alguma coisa"

    for item in itens:
        prompt = prompts.montar_resumo_item(
            ator_nome=rodada_1.ator.nome,
            ator_tipo=rodada_1.ator.tipo,
            item_id=item.id,
            veiculo=item.veiculo,
            data=item.data.isoformat(),
            url=item.url,
            titulo=item.titulo,
            texto=item.texto,
        )
        vazou = achados(prompt.texto, RADICAIS_ROTULO)
        assert not vazou, (
            f"o prompt do resumo de {item.id} menciona {vazou} — o rótulo do "
            "classificador não pode chegar ao map"
        )


def test_montar_resumo_nem_recebe_rotulo(prompts: Prompts) -> None:
    """A função de montagem do map não tem parâmetro de rótulo.

    Não dá para vazar por descuido aquilo que a assinatura não aceita.
    """
    import inspect

    assinatura = inspect.signature(prompts.montar_resumo_item)
    assert "rotulo" not in assinatura.parameters
    assert "eixo" not in assinatura.parameters


# --------------------------------------------------------------------------------------
# Teste 2 — o reduce nomeia o eixo, mas não instrui tom
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("eixo", EIXOS)
def test_reduce_nao_instrui_tom(prompts: Prompts, eixo: str) -> None:
    """O prompt final dos três eixos não contém termo de ênfase valorativa.

    Inspeciona as INSTRUÇÕES — base única mais fragmento de formato — porque instruir tom
    é uma propriedade do que se pede ao modelo, não dos dados que o acompanham.
    """
    instrucoes = prompts.instrucoes_finais(eixo=eixo)  # type: ignore[arg-type]
    proibidos = achados(instrucoes.texto, TERMOS_DE_TOM)
    assert not proibidos, (
        f"o prompt final do eixo {eixo} contém {proibidos} — o fragmento descreve forma, "
        "não encomenda conteúdo"
    )


@pytest.mark.parametrize("eixo", EIXOS)
def test_lote_nao_instrui_tom(prompts: Prompts, eixo: str) -> None:
    """O primeiro nível do reduce também não pode instruir tom."""
    prompt = prompts.montar_consolidacao_lote(eixo=eixo, resumos="[]")  # type: ignore[arg-type]
    proibidos = achados(prompt.texto, TERMOS_DE_TOM)
    assert not proibidos, f"o prompt de lote do eixo {eixo} contém {proibidos}"


@pytest.mark.parametrize("eixo", EIXOS)
def test_reduce_informa_qual_eixo(prompts: Prompts, eixo: str) -> None:
    """A outra metade da regra: o reduce PODE e DEVE dizer qual conjunto consolida.

    Sem esta asserção, o teste anterior passaria também num prompt que simplesmente
    omitisse o eixo — e aí seria outro módulo, não este.
    """
    instrucoes = prompts.instrucoes_finais(eixo=eixo)  # type: ignore[arg-type]
    assert eixo in normalizar(instrucoes.texto)


def test_negativo_e_positivo_usam_o_mesmo_arquivo_de_formato(prompts: Prompts) -> None:
    """Os fragmentos são nomeados pelo formato, nunca pelo eixo.

    `negativo` e `positivo` resolvem para o mesmo arquivo, byte a byte. É isso que torna
    uma assimetria de tom entre os dois impossível de introduzir por descuido.
    """
    assert prompts.arquivo_formato("negativo") == prompts.arquivo_formato("positivo")
    assert prompts.hash_formato("negativo") == prompts.hash_formato("positivo")
    assert prompts.arquivo_formato("neutro") != prompts.arquivo_formato("negativo")

    # E o que diferencia os dois formatos é estrutura, não tom.
    detalhado = prompts.carregar("formatos/detalhado.txt").texto
    compacto = prompts.carregar("formatos/compacto.txt").texto
    assert "fatos_mais_citados" in detalhado
    assert re.search(r"2 a 3 frases", compacto)


def test_arquivos_de_formato_nao_sao_nomeados_por_eixo(raiz) -> None:
    """Não pode existir fragmento batizado com o nome de um eixo."""
    existentes = {p.name for p in (raiz / "prompts" / "formatos").glob("*.txt")}
    for eixo in EIXOS:
        assert f"eixo_{eixo}.txt" not in existentes
        assert f"{eixo}.txt" not in existentes
