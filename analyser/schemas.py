"""Contratos de dados do Analyser, validados com Pydantic.

Regra 7: nada que venha do modelo é usado antes de passar por aqui. Texto solto não passa.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analyser.config import Eixo, TipoAtor

# --------------------------------------------------------------------------------------
# Entrada — o que o Classifier entrega ao Analyser
# --------------------------------------------------------------------------------------


class ClassificacaoNotificada(BaseModel):
    """Uma classificação por trecho recebida no evento do Classifier.

    `paragraph_index` pertence ao payload publicado. Ele é preservado pelo Analyser,
    embora ainda não exista como coluna em `tabela_classificacao`.
    """

    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["politico", "partido"]
    entity_id: str | int
    paragraph_index: Annotated[int, Field(ge=0)]
    classificacao_sentimento: Literal["positivo", "negativo", "neutro"]
    score_confianca: Annotated[float, Field(ge=0, le=1)]


class NotificacaoClassifier(BaseModel):
    """Contrato do evento publicado pelo Classifier para o Analyser.

    Este modelo não substitui `Rodada`: a mensagem é um aviso granular por artigo e
    classificações de trecho, enquanto o núcleo analítico ainda recebe uma rodada por ator.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    article_id: int
    job_id: int
    pipeline_version: str
    link_noticia: str
    data_publicacao_noticia: datetime | None
    notificado_em: datetime
    classificacoes: list[ClassificacaoNotificada]


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


class Panorama(BaseModel):
    """Panorama de um eixo em uma janela.

    Schema único para os três eixos — o frontend tem um contrato só. Nenhum nome de campo
    sinaliza polaridade: todos descrevem a cobertura e servem igualmente a qualquer eixo.
    O que muda entre os eixos é o que o fragmento de formato manda reunir no parágrafo,
    não a forma do objeto.
    """

    model_config = ConfigDict(extra="forbid")

    ator_id: str
    janela: str
    eixo: Eixo
    total_itens: Annotated[int, Field(ge=0)]
    # Obrigatório de propósito: os TRÊS fragmentos de formato pedem temas, e nenhum
    # dispensa. Enquanto este campo teve default, a decodificação restrita o tratou como
    # dispensável e o formato compacto passou a devolver panorama sem tema nenhum. Os
    # temas seguem no contrato mesmo que a tela não os exiba de primeira.
    temas: Annotated[list[TemaContado], Field(min_length=1)]
    fontes: Annotated[list[str], Field(min_length=1)]
    # O conteúdo do panorama, nos três eixos: um parágrafo consolidado com os ids entre
    # parênteses. Obrigatório pelo mesmo motivo de `temas` — um campo com default é
    # tratado como dispensável pela decodificação restrita, e aqui o dispensável seria
    # justamente o texto que o cidadão lê. Substituiu a lista `fatos_mais_citados`, que
    # entregava os mesmos fatos soltos.
    texto: Annotated[str, Field(min_length=1)]


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
