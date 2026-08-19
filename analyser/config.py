"""Configuração central do Analyser.

Concentra as constantes que a especificação exige que existam em um lugar só, para que
não haja duas fontes de verdade sobre as regras invioláveis do módulo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

# --------------------------------------------------------------------------------------
# Regras invioláveis, expressas como constantes únicas
# --------------------------------------------------------------------------------------

Eixo = Literal["positivo", "negativo", "neutro"]

#: O alvo da análise é um ATOR POLÍTICO: tanto pode ser uma pessoa quanto um partido, e o
#: módulo trata os dois exatamente do mesmo jeito.
TipoAtor = Literal["politico", "partido"]

#: Eixos consolidados, sempre nesta ordem de exibição.
EIXOS: Final[tuple[Eixo, ...]] = ("negativo", "positivo", "neutro")

#: Regra 1. O rótulo do classificador entra no prompt da consolidação (reduce) e NUNCA no
#: prompt do resumo por item (map). É desta metade `map` que dependem duas propriedades:
#:
#: 1. O `resumo_item` continua válido quando o item é reclassificado — senão cada correção
#:    do Classifier obrigaria a re-resumir a notícia inteira.
#: 2. O resumo é IMPARCIAL. No sistema, o cidadão navega pelos agrupamentos e lê o resumo
#:    dentro daquele em que a matéria caiu. O resumo não pode assumir o tom do agrupamento
#:    que o hospeda: ele descreve a matéria, e é o agrupamento que diz onde ela está.
ROTULO_NO_PROMPT: Final[dict[str, bool]] = {"map": False, "reduce": True}

#: Mapa eixo -> formato de saída. Os arquivos de prompt são nomeados pelo FORMATO, nunca
#: pelo eixo: `negativo` e `positivo` compartilham literalmente o mesmo arquivo, de modo
#: que uma assimetria de tom entre os dois não seja possível nem por descuido.
FORMATO_POR_EIXO: Final[dict[Eixo, str]] = {
    "negativo": "detalhado",
    "positivo": "detalhado",
    "neutro": "compacto",
}

#: Regra 8. Determinismo é requisito, não preferência.
TEMPERATURA: Final[float] = 0.0

#: Regra 7. Uma tentativa de reparo depois da falha de validação; depois disso, erro
#: registrado — nunca um conserto silencioso.
MAX_TENTATIVAS: Final[int] = 2

#: Tamanho padrão do lote no primeiro nível do reduce. Pequeno de propósito, para que os
#: dois níveis de consolidação sejam exercitados já com poucos itens.
LOTE_PADRAO: Final[int] = 5


# --------------------------------------------------------------------------------------
# Leitura de .env (sem dependência externa; o ambiente real tem precedência)
# --------------------------------------------------------------------------------------


def carregar_dotenv(caminho: Path) -> dict[str, str]:
    """Lê um arquivo `.env` simples e devolve os pares encontrados.

    Variáveis já presentes no ambiente do processo têm precedência e não são
    sobrescritas — o `.env` serve como ponto de partida, não como autoridade.
    """
    lidos: dict[str, str] = {}
    if not caminho.is_file():
        return lidos
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        limpa = linha.strip()
        if not limpa or limpa.startswith("#") or "=" not in limpa:
            continue
        chave, _, valor = limpa.partition("=")
        chave = chave.strip()
        valor = valor.strip().strip('"').strip("'")
        lidos[chave] = valor
        os.environ.setdefault(chave, valor)
    return lidos


# --------------------------------------------------------------------------------------
# Configuração de execução
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Config:
    """Parâmetros de uma execução do Analyser.

    Todos os caminhos são injetáveis para que os testes possam apontar a saída para um
    diretório temporário sem tocar em `saida/`.
    """

    entrada: Path
    saida: Path = field(default_factory=lambda: Path("saida"))
    prompts: Path = field(default_factory=lambda: Path("prompts"))
    lote: int = LOTE_PADRAO
    modelo: Literal["fake", "api"] = "fake"

    def __post_init__(self) -> None:
        self.entrada = Path(self.entrada)
        self.saida = Path(self.saida)
        self.prompts = Path(self.prompts)
        if self.lote < 1:
            raise ValueError("O tamanho do lote precisa ser no mínimo 1.")

    @property
    def arquivo_resumos(self) -> Path:
        """Camada imutável: um resumo por notícia, append-only."""
        return self.saida / "resumos_item.json"

    @property
    def arquivo_panoramas(self) -> Path:
        """Camada recalculável: uma versão nova a cada recálculo, nada sobrescrito."""
        return self.saida / "panoramas.json"

    @property
    def arquivo_rotulos(self) -> Path:
        """Registro do rótulo vigente por item — a única camada mutável do sistema.

        Existe porque detectar "o rótulo mudou" exige lembrar o rótulo anterior, e a
        camada de resumos é append-only: o rótulo não pode morar lá.
        """
        return self.saida / "rotulos.json"

    def arquivo_auditoria(self, carimbo: str) -> Path:
        """Um registro de auditoria por execução."""
        return self.saida / f"auditoria_{carimbo}.json"
