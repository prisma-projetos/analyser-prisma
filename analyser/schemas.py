"""Contratos de dados do Analyser, validados com Pydantic.

Regra 7: nada que venha do modelo é usado antes de passar por aqui. Texto solto não passa.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analyser.config import Eixo, TipoAtor

# --------------------------------------------------------------------------------------
# Entrada — o que o Classifier entrega ao Analyser
# --------------------------------------------------------------------------------------


class ItemEntrada(BaseModel):
    """Uma notícia já classificada."""

    model_config = ConfigDict(extra="forbid")

    id: str
    veiculo: str
    url: str
    data: date
    titulo: str
    texto: str
    rotulo: Eixo

    @property
    def janela(self) -> str:
        """Janela mensal `YYYY-MM` derivada da data DA NOTÍCIA.

        Não da data em que a notícia chegou: uma matéria de julho descoberta hoje
        pertence à janela de julho.
        """
        return self.data.strftime("%Y-%m")


class AtorPolitico(BaseModel):
    """Alvo da análise.

    Ator político, não político: o alvo tanto pode ser uma pessoa quanto um partido, e o
    módulo trata os dois exatamente do mesmo jeito.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tipo: TipoAtor
    nome: str


class Rodada(BaseModel):
    """Um lote de notícias classificadas sobre um ator político."""

    model_config = ConfigDict(extra="forbid")

    ator: AtorPolitico
    itens: list[ItemEntrada]

    @field_validator("itens")
    @classmethod
    def _sem_ids_repetidos(cls, itens: list[ItemEntrada]) -> list[ItemEntrada]:
        vistos = [item.id for item in itens]
        if len(vistos) != len(set(vistos)):
            raise ValueError("A rodada tem ids de item repetidos.")
        return itens


# --------------------------------------------------------------------------------------
# Saída da etapa 1 (map) — camada imutável
# --------------------------------------------------------------------------------------


class ResumoItem(BaseModel):
    """Resumo de uma notícia. Gerado sem que o modelo veja o rótulo do classificador."""

    model_config = ConfigDict(extra="forbid")

    resumo: Annotated[str, Field(min_length=1)]
    temas: Annotated[list[str], Field(min_length=1, max_length=6)]
    fonte_id: str


class RegistroResumo(BaseModel):
    """Notícia resumida e classificação definitiva, gravadas uma vez e para sempre."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    ator_id: str
    janela: str
    rotulo: Eixo
    veiculo: str
    url: str
    data: date
    titulo: str
    resumo: str
    temas: list[str]
    fonte_id: str
    modelo: str
    hash_prompt_resumo: str
    criado_em: str


# --------------------------------------------------------------------------------------
# Saída da etapa 4, nível 1 (lote)
# --------------------------------------------------------------------------------------


class TemaContado(BaseModel):
    """Tema recorrente com a contagem de itens que o sustentam."""

    model_config = ConfigDict(extra="forbid")

    tema: str
    ocorrencias: Annotated[int, Field(ge=1)]
    fontes: Annotated[list[str], Field(min_length=1)]


class ConsolidacaoParcial(BaseModel):
    """Consolidação de um lote de resumos. Insumo do segundo nível do reduce."""

    model_config = ConfigDict(extra="forbid")

    temas: list[TemaContado]
    pontos: list[str]
    fontes: Annotated[list[str], Field(min_length=1)]


# --------------------------------------------------------------------------------------
# Saída da etapa 4, nível 2 (panorama) — schema ÚNICO para os três eixos
# --------------------------------------------------------------------------------------


class FatoCitado(BaseModel):
    """Fato mencionado pela cobertura, com os ids que o sustentam."""

    model_config = ConfigDict(extra="forbid")

    descricao: str
    fontes: Annotated[list[str], Field(min_length=1)]


class RespostaRegistrada(BaseModel):
    """Se a cobertura registra manifestação de quem foi citado."""

    model_config = ConfigDict(extra="forbid")

    citado: str
    houve_resposta: bool
    fontes: Annotated[list[str], Field(min_length=1)]


class Panorama(BaseModel):
    """Panorama de um eixo em uma janela.

    Schema único para os três eixos — o frontend tem um contrato só. Os campos que um
    formato não usa ficam vazios. Nenhum nome de campo sinaliza polaridade: todos
    descrevem a cobertura e servem igualmente a qualquer eixo.
    """

    model_config = ConfigDict(extra="forbid")

    ator_id: str
    janela: str
    eixo: Eixo
    total_itens: Annotated[int, Field(ge=0)]
    # Obrigatório de propósito: os DOIS fragmentos de formato pedem temas — o detalhado
    # de 3 a 6, o compacto de 2 a 4. Nenhum dispensa, ao contrário dos campos abaixo, que
    # variam mesmo. Enquanto este campo teve default, a decodificação restrita o tratou
    # como dispensável e o formato compacto passou a devolver panorama sem tema nenhum.
    temas: Annotated[list[TemaContado], Field(min_length=1)]
    fontes: Annotated[list[str], Field(min_length=1)]
    # Preenchido pelo formato `compacto`.
    texto: str | None = None
    # Preenchidos pelo formato `detalhado`.
    fatos_mais_citados: list[FatoCitado] = Field(default_factory=list)
    respostas_registradas: list[RespostaRegistrada] = Field(default_factory=list)


class RegistroPanorama(BaseModel):
    """Uma versão de panorama gravada em `panoramas.json`. Nunca sobrescrita."""

    model_config = ConfigDict(extra="forbid")

    ator_id: str
    janela: str
    eixo: Eixo
    versao: Annotated[int, Field(ge=1)]
    formato: str
    panorama: Panorama
    avisos: list[str] = Field(default_factory=list)
    lotes: int
    modelo: str
    hash_prompt_lote: str
    hash_prompt_final: str
    hash_prompt_formato: str
    rodada: str
    criado_em: str

TipoAviso = Literal["fonte_inexistente", "contagem_divergente", "falha_validacao"]
