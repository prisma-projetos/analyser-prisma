"""Contratos de dados do Analyser, validados com Pydantic.

Regra 7: nada que venha do modelo é usado antes de passar por aqui. Texto solto não passa.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analyser.config import (
    MAX_PONTOS_POR_LOTE,
    MAX_TEMAS_POR_LOTE,
    Eixo,
    TipoAtor,
)

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

    #: Identificador estável do tema, derivado do nome em Python — nunca pedido ao
    #: modelo. Um gerador de texto livre não produz identificador estável: o mesmo
    #: assunto viria com id diferente a cada execução, e o campo perderia justamente
    #: a serventia de ser id. Fica com default porque a resposta do modelo não o traz;
    #: `Analyser._atribuir_ids_de_tema` o preenche antes de qualquer gravação.
    id: str = ""
    tema: str
    ocorrencias: Annotated[int, Field(ge=1)]
    fontes: Annotated[list[str], Field(min_length=1)]


class ConsolidacaoParcial(BaseModel):
    """Consolidação de um lote de resumos. Insumo do segundo nível do reduce.

    Os tetos de `temas` e `pontos` são o que faz o reduce REDUZIR. Sem eles as duas
    listas crescem proporcionais ao lote, e a saída deste nível é exatamente o prompt do
    nível seguinte: um primeiro nível que não comprime só adia o estouro para a
    `consolidacao_final`. Medido em agosto de 2026, antes dos tetos: 31 resumos do Zema
    devolveram 78 temas e 9694 tokens: 313 tokens de saída por resumo de entrada.

    Os tetos são ABSOLUTOS, não por item — é essa independência do tamanho da janela que
    mantém o prompt do nível 2 limitado por mais que a janela cresça.

    Eles precisam estar no schema, e não no texto do prompt, porque o prompt já pedia
    "prefira poucos temas bem sustentados" e recebeu 78. Com `SAIDA_ESTRUTURADA=1` estes
    campos viram `maxItems` no JSON Schema, o servidor converte o schema em gramática, e
    o decodificador fica obrigado a fechar a lista. Vira garantia mecânica.

    `fontes` continua sem teto de propósito: é a lista de ids que marca cada item como
    `consolidado`. Limitá-la produziria exatamente o dano que este módulo passou a
    detectar — item que participou da consolidação e ficou órfão em `resumido`.
    """

    model_config = ConfigDict(extra="forbid")

    temas: Annotated[list[TemaContado], Field(max_length=MAX_TEMAS_POR_LOTE)]
    pontos: Annotated[list[str], Field(max_length=MAX_PONTOS_POR_LOTE)]
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
    # Obrigatório de propósito: os TRÊS fragmentos de formato pedem temas, e nenhum
    # dispensa. Enquanto este campo teve default, a decodificação restrita o tratou como
    # dispensável e o formato compacto passou a devolver panorama sem tema nenhum. Os
    # temas seguem no contrato mesmo que a tela não os exiba de primeira.
    temas: Annotated[list[TemaContado], Field(min_length=1)]
    # O conteúdo do panorama, nos três eixos: um parágrafo consolidado com os ids entre
    # parênteses. Obrigatório pelo mesmo motivo de `temas` — um campo com default é
    # tratado como dispensável pela decodificação restrita, e aqui o dispensável seria
    # justamente o texto que o cidadão lê. Substituiu a lista `fatos_mais_citados`, que
    # entregava os mesmos fatos soltos.
    texto: Annotated[str, Field(min_length=1)]

    # -- preenchidos pelo Python, NUNCA pelo modelo -------------------------------------
    #
    # Estes dois campos saíram do que se pede ao modelo em 3 de setembro de 2026. O
    # motivo é que o campo tinha dois donos com definições incompatíveis: o fragmento de
    # formato pedia "todos os ids USADOS no panorama", e `gravar_panorama` usava a mesma
    # lista para decidir quais itens viram `consolidado`.
    #
    # As duas leituras divergem exatamente na medida em que o modelo faz bem o trabalho
    # dele: quanto melhor ele consolida, menos ids distintos ele "usa", e mais itens
    # ficam órfãos em `resumido`. Medido: uma janela de 88 matérias com teto de 12 temas
    # devolveu 24 ids — os outros 64 itens teriam ficado pendentes para sempre.
    #
    # A divergência ficou escondida enquanto as janelas eram pequenas, porque o modelo
    # produzia quase um tema por matéria e "ids usados" era acidentalmente igual a "todos
    # os ids". Não era um invariante: era coincidência de escala.
    #
    # Isto é contagem e escrituração, não interpretação — e a regra do módulo é que o
    # modelo escreve texto enquanto o Python controla agrupamento, contagem e estado. O
    # conjunto de entrada da consolidação é conhecido exatamente em Python; pedi-lo de
    # volta ao modelo gastava ~1200 tokens de saída por chamada e ainda abria espaço para
    # alucinação (um id inexistente, `art_28__partido_13`, apareceu numa resposta real).
    #
    # `temas[].fontes` continua sendo do modelo: ali o id É interpretação — diz qual
    # matéria sustenta AQUELE tema, e só quem leu o resumo sabe responder.
    #
    # Os defaults existem porque a resposta do modelo não traz mais estes campos;
    # `Analyser._etapa_4_e_5` os preenche antes de qualquer verificação ou gravação.
    total_itens: Annotated[int, Field(ge=0)] = 0
    fontes: list[str] = Field(default_factory=list)

    @classmethod
    def esquema_do_modelo(cls) -> dict[str, Any]:
        """JSON Schema do que se PEDE ao modelo — sem os campos que o Python preenche.

        Separado de `model_json_schema()`, que descreve o objeto persistido inteiro.
        Mandar o schema completo faria a decodificação restrita exigir `fontes` e
        `total_itens` do modelo, que é justamente o que se está tirando dele.
        """
        esquema = cls.model_json_schema()
        for campo in ("fontes", "total_itens"):
            esquema["properties"].pop(campo, None)
            if campo in esquema.get("required", []):
                esquema["required"].remove(campo)
        return esquema


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
