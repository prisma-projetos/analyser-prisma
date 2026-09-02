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

    O resumo precisa ser imparcial: no sistema ele é exibido dentro do agrupamento em que
    a matéria foi classificada e não pode assumir o tom desse agrupamento.
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
def test_reduce_manda_descrever_a_cobertura_e_nao_julgar_o_ator(
    prompts: Prompts, eixo: str
) -> None:
    """O reduce agora recorta por eixo, mas continua falando da COBERTURA.

    Até 1º de setembro de 2026 havia aqui um teste que proibia qualquer termo valorativo
    no prompt final. Ele caiu junto com a decisão de produto que fez o panorama reunir as
    críticas no eixo negativo e os elogios no positivo — instruir esse recorte é agora o
    objetivo, não o acidente.

    O que sobrou da regra, e que este teste guarda: o fragmento manda descrever o que as
    matérias registram, nunca emitir juízo sobre o ator.
    """
    instrucoes = normalizar(prompts.instrucoes_finais(eixo=eixo).texto)  # type: ignore[arg-type]
    assert "sem adjetivo sobre o alvo" in instrucoes
    assert "a cobertura" in instrucoes


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


def test_cada_eixo_tem_seu_proprio_fragmento(prompts: Prompts) -> None:
    """Os três fragmentos são distintos, e cada eixo resolve para o seu.

    Substitui o teste que exigia arquivo único para negativo e positivo. A assimetria
    passou a ser o produto — ver `FORMATO_POR_EIXO` para o porquê e para o que ela não
    afrouxou.
    """
    arquivos = {eixo: prompts.arquivo_formato(eixo) for eixo in EIXOS}  # type: ignore[arg-type]
    assert len(set(arquivos.values())) == len(EIXOS)

    hashes = {eixo: prompts.hash_formato(eixo) for eixo in EIXOS}  # type: ignore[arg-type]
    assert len(set(hashes.values())) == len(EIXOS)


@pytest.mark.parametrize("eixo", EIXOS)
def test_fragmento_pede_paragrafo_unico_e_nao_lista_de_fatos(
    prompts: Prompts, eixo: str
) -> None:
    """Todo fragmento pede UM parágrafo em `texto`, e nenhum pede a lista antiga."""
    fragmento = prompts.carregar(prompts.arquivo_formato(eixo)).texto  # type: ignore[arg-type]
    assert "fatos_mais_citados" not in fragmento
    assert re.search(r"UM par[áa]grafo [úu]nico", fragmento)
    # A rastreabilidade saiu da prosa e ficou nos campos próprios.
    assert "NÃO escreva ids dentro do parágrafo" in fragmento
    assert "ABRA NOMEANDO O RECORTE" in fragmento


def test_fragmentos_existentes_sao_exatamente_os_tres_eixos(raiz) -> None:
    """Não sobrou fragmento órfão nomeado pelo formato antigo."""
    existentes = {p.name for p in (raiz / "prompts" / "formatos").glob("*.txt")}
    assert existentes == {f"{eixo}.txt" for eixo in EIXOS}
