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
#: prompt do resumo por item (map). Isso mantém o resumo IMPARCIAL: ele descreve a matéria,
#: enquanto o agrupamento informa em qual eixo ela foi classificada.
ROTULO_NO_PROMPT: Final[dict[str, bool]] = {"map": False, "reduce": True}

#: Mapa eixo -> fragmento de formato. Cada eixo tem o seu.
#:
#: Até 1º de setembro de 2026 os fragmentos eram nomeados pelo FORMATO (`detalhado` para
#: negativo e positivo, `compacto` para neutro), justamente para que uma assimetria de
#: tom entre negativo e positivo fosse impossível de introduzir por descuido. Por decisão
#: de produto, o panorama passou a entregar um parágrafo consolidado em vez de fatos
#: soltos, e esse parágrafo reúne explicitamente as críticas no eixo negativo e os
#: elogios no positivo. Instruir esse recorte exige um fragmento por eixo, então a
#: simetria foi trocada por três arquivos distintos, de propósito e não por descuido.
#:
#: O que a troca NÃO afrouxou: o rótulo continua fora do prompt do resumo por item
#: (`ROTULO_NO_PROMPT["map"] is False`) e a consolidação de lote, que alimenta os três
#: eixos, continua com um prompt único e sem instrução de tom.
FORMATO_POR_EIXO: Final[dict[Eixo, str]] = {
    "negativo": "negativo",
    "positivo": "positivo",
    "neutro": "neutro",
}

#: Confiança mínima para um trecho contar como SINAL de polaridade.
#:
#: Abaixo disso o trecho não deixa de existir e não é reescrito: ele continua no registro
#: com o que o Classifier disse, e passa a valer como CONTEXTO — entra no texto dos dois
#: recortes, mas não decide eixo nenhum.
#:
#: Medido na base real em 3 de setembro de 2026: `score_confianca = 0` aparece
#: EXCLUSIVAMENTE com `neutro`, nas 56.689 linhas de `tabela_classificacao` e nos 2.156
#: trechos que chegaram ao Analyser. Ou seja, "neutro" e "o léxico não achou marcador"
#: são o mesmo valor — não existe um único neutro confiante na base.
#:
#: Por isso a regra antiga, de maioria simples, era um erro de categoria: ela deixava uma
#: abstenção votar contra uma classificação confiante, e num artigo real a única
#: classificação confiante foi derrotada por catorze "não sei". Custo medido: 121 itens
#: (14%) viraram neutro apesar de terem sinal confiante, e o eixo neutro ficou com 90,5%
#: de tudo.
#:
#: Fica em zero porque hoje é o que reproduz a intenção: qualquer marcador encontrado
#: conta. Existe como constante, e não como `> 0` cravado no código, porque o Classifier
#: PRODUZ gradiente — 0,111 a 0,846 aparecem em 173 das 56.689 linhas. Esses casos ainda
#: não chegaram ao Analyser, mas vão chegar, e no dia em que 0,111 decidir a polaridade
#: de um artigo inteiro, subir este número é mudar um valor, não reescrever a lógica.
LIMIAR_CONFIANCA: Final[float] = 0.0

#: Regra 8. Determinismo é requisito, não preferência.
TEMPERATURA: Final[float] = 0.0

#: Regra 7. Uma tentativa de reparo depois da falha de validação; depois disso, erro
#: registrado — nunca um conserto silencioso.
MAX_TENTATIVAS: Final[int] = 2

#: Teto de tokens de resposta por tarefa.
#:
#: Isto NÃO é conhecimento sobre o provedor: deriva dos schemas e dos fragmentos de
#: formato deste módulo — é o tamanho que a NOSSA saída pode ter. Medido: `resumo_item`
#: gastou 134 tokens numa chamada real; um `consolidacao_final` no formato detalhado
#: ocupa 419 tokens com 7 itens e estima ~835 com 60. Os tetos abaixo deixam 3 a 4 vezes
#: de folga.
#:
#: Sem teto, um modelo que raciocina gasta o orçamento inteiro pensando e devolve
#: conteúdo vazio; com teto curto demais, o JSON chega truncado. As duas situações são
#: detectadas em `ModeloAPI.gerar` e interrompem a execução em vez de virar retry.
#: Os valores são folgados de propósito. Um teto não é uma meta: o modelo para sozinho
#: quando termina, e uma chamada que gastou 129 tokens custa o mesmo com teto de 500 ou
#: de 3000. O que o teto precisa cobrir é o pior caso — e o pior caso é um modelo que
#: RACIOCINA, porque a deliberação sai do mesmo orçamento da resposta. Com o raciocínio
#: ligado, um teto dimensionado só para a resposta faz o modelo gastar tudo pensando e
#: devolver conteúdo vazio, que é como esta constante nasceu.
MAX_TOKENS_POR_TAREFA: Final[dict[str, int]] = {
    "resumo_item": 3000,
    "consolidacao_lote": 14000,
    "consolidacao_final": 13000,
}

#: Contexto do servidor de modelo, em tokens. Prompt E resposta disputam ESTE espaço:
#: não são orçamentos separados, e cada chamada HTTP recebe o valor inteiro para si
#: (o módulo manda uma mensagem só, sem histórico — nada acumula entre chamadas).
#:
#: Precisa espelhar `OLLAMA_CONTEXT_LENGTH` do `compose.yml`. Se este valor for MAIOR que
#: o do servidor, o dimensionamento abaixo autoriza lotes que o servidor vai truncar.
#:
#: Por que isto virou constante em 3 de setembro de 2026: o valor não era conhecido aqui,
#: e `LOTE_PADRAO` era 100 sem que ninguém tivesse conferido um contra o outro. Com
#: contexto de 16384, o prompt de um lote de 100 mede 16372 tokens — ele consome o
#: contexto inteiro e não sobra nada para a resposta.
CONTEXTO_TOKENS: Final[int] = int(os.environ.get("CONTEXTO_TOKENS", "32768"))

#: Custo do prompt de `consolidacao_lote`, MEDIDO contra o tokenizer real do servidor
#: (`prompt_eval_count` do Ollama, não estimativa por caracteres — o tokenizer do Qwen é
#: ~14% mais denso que `o200k_base` em português, e estimar por caracteres subdimensiona).
#:
#: Ajuste linear sobre 7 tamanhos de lote com dados reais de agosto de 2026:
#:
#:     n=10 -> 2382    n=39 -> 6921    n=66 -> 11125    n=94 -> 15398
#:
#: A regressão é estável: 154,3 a 155,3 tokens por resumo em toda a faixa.
TOKENS_INSTRUCOES_LOTE: Final[int] = 872
TOKENS_POR_RESUMO_NO_LOTE: Final[int] = 155

#: Tetos ABSOLUTOS da saída do primeiro nível do reduce — não por item, absolutos.
#:
#: É isto que faz o reduce reduzir. Sem eles `ConsolidacaoParcial.pontos` e `.temas` são
#: listas sem máximo, a saída do nível 1 cresce proporcional à entrada, e essa saída é
#: exatamente o que vira o PROMPT do nível 2. Um reduce cujo primeiro nível não comprime
#: só adia o estouro para a `consolidacao_final` — foi o que derrubou a janela do PL em
#: agosto de 2026.
#:
#: Medido no incidente: um lote de 31 resumos do Zema devolveu 78 temas e 9694 tokens de
#: saída — 313 tokens de saída por resumo de entrada, com o total batendo em 15761 de
#: 16384 (96% do contexto). Não era loop de repetição: era o nível 1 não consolidando
#: nada, emitindo mais de dois temas por matéria.
#:
#: Com `SAIDA_ESTRUTURADA=1` estes tetos viram `maxItems` no JSON Schema, e o servidor
#: converte o schema em gramática. O decodificador fica OBRIGADO a fechar a lista. É
#: garantia mecânica, não pedido no texto do prompt — o prompt já pedia "prefira poucos
#: temas bem sustentados" e recebeu 78.
MAX_TEMAS_POR_LOTE: Final[int] = 12
MAX_PONTOS_POR_LOTE: Final[int] = 25

#: Custo estimado da saída de um lote, dados os tetos acima. Ao contrário das constantes
#: de prompt, estes números NÃO são medidos: são dimensionamento para reservar espaço.
#: A auditoria passou a persistir `prompt_tokens` e `completion_tokens` por tarefa
#: justamente para que a próxima execução real os substitua por medição.
TOKENS_POR_TEMA: Final[int] = 50
TOKENS_POR_PONTO: Final[int] = 55
TOKENS_POR_FONTE: Final[int] = 14

#: Caracteres por token, para estimar o tamanho de um prompt sem carregar um tokenizer.
#:
#: `CHARS_POR_TOKEN` é a média medida contra o tokenizer real do servidor em prompts de
#: consolidação reais (3,37 a 3,67 conforme a proporção entre instruções e dados). Serve
#: à verificação PREVENTIVA: barato errar um pouco antes de gastar a chamada.
#:
#: `CHARS_POR_TOKEN_MINIMO` não é uma média — é um limite inferior deliberadamente
#: frouxo. Ele responde a uma pergunta diferente: "qual o MENOR número de tokens que este
#: texto poderia ter?". Serve ao detector de truncamento, onde um falso positivo derruba
#: uma execução boa. Com 6,0 a folga sobre o pior caso observado (3,89) é de 54%, e ainda
#: assim o corte do Ollama — que é sempre para metade do contexto — fica muito abaixo
#: dele: o lote do Lula tem ~57000 caracteres, piso de ~9500 tokens, e chegou ao servidor
#: como 8194.
CHARS_POR_TOKEN: Final[float] = 3.4
CHARS_POR_TOKEN_MINIMO: Final[float] = 6.0

#: Quanto do contexto o dimensionamento pode comprometer.
#:
#: A conta sem reserva devolve um lote que ocupa 99,98% do contexto — e encher o contexto
#: é a falha que este bloco existe para impedir. A reserva cobre o que a conta não sabe:
#: as constantes de SAÍDA acima são estimativas, não medições, e o incidente de agosto de
#: 2026 mostrou o custo de operar sem folga. O lote do Zema fechou em 15761 de 16384
#: tokens — 96% — e passou; na execução anterior, com a mesma temperatura 0, a mesma
#: chamada tinha estourado. A 96% de ocupação, a variação normal entre execuções (o
#: split GPU/CPU muda a ordem das reduções em ponto flutuante) decide entre sucesso e
#: falha. A reserva é o que tira a consolidação dessa faixa.
FRACAO_UTIL_DO_CONTEXTO: Final[float] = 0.80

#: Teto de lote independente do contexto.
#:
#: Não é uma restrição de tokens: é de confiabilidade. O lote é a unidade de retentativa
#: (um lote que falha é refeito inteiro) e a parcial precisa ecoar corretamente o id de
#: cada matéria do lote — `fontes` é o que marca item como `consolidado`, e id não citado
#: vira item órfão. Pedir isso sobre centenas de matérias numa chamada só troca um limite
#: mecânico, que o servidor sinaliza, por um limite de qualidade, que ninguém sinaliza.
LOTE_MAXIMO: Final[int] = 100


def lote_que_cabe(contexto: int = CONTEXTO_TOKENS) -> int:
    """Maior lote cujo prompt E resposta cabem juntos no contexto do servidor.

    O que acontece quando não cabem não é um erro: o Ollama **trunca o prompt pela
    cabeça, em silêncio**, para `contexto/2`, reporta o tamanho já cortado em
    `prompt_tokens` e devolve `finish_reason="stop"`. Medido em 3 de setembro de 2026:
    um lote de 117 resumos (21008 tokens) com `num_ctx=16384` chegou ao modelo como
    8194 tokens.

    O começo do prompt é onde ficam as REGRAS DE FIDELIDADE e a TAREFA; o fim é onde
    ficam os dados e o formato da resposta. Truncar pela cabeça apaga exatamente as
    regras e preserva o formato — então o modelo devolve um JSON bem formado, válido
    contra o schema, sobre metade dos dados e sem as regras que garantiam fidelidade.
    Nenhuma camada percebe. Foi assim que a janela de agosto do Lula gravou um panorama
    citando 47 das 94 matérias sem levantar um único erro.

    Por isso o lote é derivado do contexto e nunca mais escrito à mão.
    """
    saida_fixa = MAX_TEMAS_POR_LOTE * TOKENS_POR_TEMA + MAX_PONTOS_POR_LOTE * TOKENS_POR_PONTO
    disponivel = int(contexto * FRACAO_UTIL_DO_CONTEXTO) - TOKENS_INSTRUCOES_LOTE - saida_fixa
    # `fontes` é o único campo da parcial que não pode ter teto: é a lista de ids que
    # marca cada item como `consolidado`. Cortá-la deixa item órfão em `resumido`.
    por_resumo = TOKENS_POR_RESUMO_NO_LOTE + TOKENS_POR_FONTE
    return max(1, min(LOTE_MAXIMO, disponivel // por_resumo))


def custo_estimado_do_lote(itens: int) -> tuple[int, int]:
    """Tokens estimados de prompt e de resposta para um lote de `itens` resumos.

    Serve à verificação preventiva em `ModeloAPI`: é barato conferir a conta antes de
    gastar a chamada, e caro descobrir depois — a consolidação de agosto de 2026 levou
    2h08 para terminar em cinco janelas cortadas e uma corrompida em silêncio.
    """
    prompt = TOKENS_INSTRUCOES_LOTE + TOKENS_POR_RESUMO_NO_LOTE * itens
    resposta = (
        MAX_TEMAS_POR_LOTE * TOKENS_POR_TEMA
        + MAX_PONTOS_POR_LOTE * TOKENS_POR_PONTO
        + TOKENS_POR_FONTE * itens
    )
    return prompt, resposta


#: Tamanho padrão do lote no primeiro nível do reduce. O valor é um teto: grupos menores
#: usam apenas os resumos existentes.
#:
#: Era 100 fixo até 3 de setembro de 2026, escolhido quando a maior janela tinha algumas
#: dezenas de itens — o teto nunca chegava a ser exercitado. Agosto acumulou 117 matérias
#: do PL, o lote encheu pela primeira vez e o contexto estourou. O número agora é
#: derivado, então subir `CONTEXTO_TOKENS` reajusta o lote sozinho.
LOTE_PADRAO: Final[int] = lote_que_cabe()


def faixa_de_temas(materias: int) -> tuple[int, int]:
    """Quantos temas um panorama deve ter, dado quantas matérias ele consolida.

    Uma faixa fixa não serve a um módulo cujo eixo pode ter 2 matérias num mês e 60 em
    outro. Medido: com o mínimo em 5, um eixo de 2 matérias produziu 8 temas, quase todos
    de uma fonte só — fragmentação. Com o máximo em 6, um eixo de 60 matérias caberia em
    6 temas — mutilação. Pior: quando faltam vagas, o modelo funde assuntos distintos num
    rótulo composto em vez de descartar, o que estraga o nome e a contagem de uma vez.

    A faixa acompanha o volume, entre limites que preservam a legibilidade: um panorama
    é lido por um cidadão, não é um índice.
    """
    minimo = max(2, min(8, round(materias / 4)))
    maximo = max(minimo + 2, min(12, round(materias / 1.2)))
    return minimo, maximo


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

    def arquivo_auditoria(self, carimbo: str) -> Path:
        """Um registro de auditoria por execução."""
        return self.saida / f"auditoria_{carimbo}.json"
