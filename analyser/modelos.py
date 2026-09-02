"""O modelo fica atrás de uma interface.

`ModeloFake` é o padrão: determinístico, sem rede e sem chave, para que o protótipo rode
em qualquer máquina sem nenhuma variável de ambiente configurada. `ModeloAPI` fala com
qualquer API compatível com a da OpenAI — trocar de provedor é editar o `.env`.

Nenhum nome de modelo aparece hardcoded aqui. `ModeloAPI` lê `MODEL` do ambiente e falha
com mensagem clara se ele não estiver definido.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from analyser.config import MAX_TENTATIVAS, MAX_TOKENS_POR_TAREFA, TEMPERATURA

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class RespostaBruta:
    """O que o modelo devolveu, antes de qualquer validação."""

    texto: str
    tokens: dict[str, int] | None = None


class ErroDeValidacao(RuntimeError):
    """Saída que não passou no schema nem depois da tentativa de reparo.

    Regra 7: uma nova tentativa, depois erro registrado. Nunca um conserto silencioso.
    """


class ErroDeResposta(RuntimeError):
    """A resposta veio inutilizável por CONFIGURAÇÃO, não por prompt ruim.

    Distinta de `ErroDeValidacao` de propósito. Aquela é um problema daquele item: o
    pipeline registra e segue, porque a próxima notícia pode dar certo. Esta é um
    problema da execução inteira — conteúdo vazio porque o modelo raciocina, ou JSON
    cortado porque o teto de tokens é curto. Repetir não conserta nenhuma das duas, só
    gasta chamada, então o pipeline deixa esta subir e a execução para na primeira
    ocorrência.
    """


class Modelo(Protocol):
    """Contrato mínimo que o pipeline exige de um modelo."""

    nome: str
    chamadas: int
    chamadas_por_tarefa: Counter[str]

    def gerar(
        self,
        *,
        prompt: str,
        contexto: dict[str, Any],
        tarefa: str,
        esquema: dict[str, Any] | None = None,
    ) -> RespostaBruta:
        """Gera texto para uma tarefa. `contexto` traz os dados já estruturados.

        `esquema` é o JSON Schema da resposta esperada. Uma implementação pode usá-lo
        para restringir a decodificação, ou ignorá-lo — a validação com Pydantic
        acontece de qualquer forma, depois.
        """
        ...


# --------------------------------------------------------------------------------------
# Utilidades de texto, compartilhadas pelo fake
# --------------------------------------------------------------------------------------


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos, para comparação estável."""
    decomposto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def dividir_frases(texto: str) -> list[str]:
    """Divide um texto em frases, de forma simples e determinística."""
    partes = re.split(r"(?<=[.!?])\s+", texto.strip())
    return [p.strip() for p in partes if p.strip()]


_VAZIAS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos", "das", "em",
    "no", "na", "nos", "nas", "por", "para", "com", "sem", "sob", "sobre", "entre", "que",
    "e", "ou", "mas", "se", "ao", "aos", "a", "as", "pelo", "pela", "pelos", "pelas", "foi",
    "ser", "sao", "era", "eram", "tem", "tinha", "havia", "ha", "esta", "estao", "estava",
    "seu", "sua", "seus", "suas", "este", "esta", "esse", "essa", "isso", "aquele", "ainda",
    "ja", "nao", "mais", "menos", "muito", "pouco", "todo", "toda", "todos", "todas", "outro",
    "outra", "quando", "onde", "como", "porque", "apos", "ate", "desde", "durante", "sera",
    "disse", "afirmou", "segundo", "informou", "declarou", "nesta", "neste", "cidade", "ano",
    "mes", "dia", "prefeita", "prefeitura", "municipio", "municipal", "vereador", "vereadora",
}


_NUMERAIS = {
    "zero", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove",
    "dez", "onze", "doze", "quinze", "vinte", "trinta", "cem", "mil", "milhoes",
    "primeiro", "primeira", "segundo", "segunda", "terceiro", "terceira",
}


def extrair_temas(texto: str, *, quantidade: int = 3) -> list[str]:
    """Temas extraídos por frequência de termos significativos. Determinístico."""
    palavras = re.findall(r"[a-zà-ÿ]{5,}", texto.lower())
    contagem = Counter(
        p for p in palavras if normalizar(p) not in _VAZIAS and normalizar(p) not in _NUMERAIS
    )
    # Ordena por frequência e, no empate, alfabeticamente — determinismo total.
    ordenadas = sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0]))
    temas = [p for p, _ in ordenadas[:quantidade]]
    return temas or ["cobertura do mês"]


#: Verbos que costumam introduzir a versão de quem foi citado. O prompt do resumo manda
#: incluir a manifestação registrada, então o extrator vai atrás dela.
_MARCAS_RESPOSTA = (
    "afirmou", "informou", "disse", "declarou", "respondeu", "defendeu", "sustentou",
    "reconheceu", "contestou", "atribuiu", "apresentou", "rebateu", "esclareceu",
)

# --------------------------------------------------------------------------------------
# ModeloFake — o padrão
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ModeloFake:
    """Modelo determinístico, sem rede e sem chave.

    Produz saída plausível e válida contra o schema, de forma extrativa: tudo o que ele
    escreve sai do texto que recebeu. Expõe contadores de chamada, usados nos testes.
    """

    nome: str = "fake"
    chamadas: int = 0
    chamadas_por_tarefa: Counter[str] = field(default_factory=Counter)

    def zerar_contadores(self) -> None:
        """Zera os contadores. Usado entre rodadas nos testes."""
        self.chamadas = 0
        self.chamadas_por_tarefa = Counter()

    def gerar(
        self,
        *,
        prompt: str,
        contexto: dict[str, Any],
        tarefa: str,
        esquema: dict[str, Any] | None = None,
    ) -> RespostaBruta:
        """Despacha para o gerador da tarefa e contabiliza a chamada.

        Ignora `esquema`: o fake já constrói a saída a partir do schema, por construção.
        """
        self.chamadas += 1
        self.chamadas_por_tarefa[tarefa] += 1
        geradores = {
            "resumo_item": self._resumo_item,
            "consolidacao_lote": self._consolidacao_lote,
            "consolidacao_final": self._consolidacao_final,
        }
        if tarefa not in geradores:
            raise ValueError(f"Tarefa desconhecida para o modelo fake: {tarefa}")
        dados = geradores[tarefa](contexto)
        # Estimativa grosseira, só para o campo de tokens da auditoria ter algum valor.
        tokens = {"prompt": len(prompt) // 4, "resposta": len(json.dumps(dados)) // 4}
        return RespostaBruta(texto=json.dumps(dados, ensure_ascii=False), tokens=tokens)

    # -- etapa 1 -----------------------------------------------------------------------

    def _resumo_item(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Resumo extrativo: a frase de abertura mais a versão de quem foi citado.

        O prompt manda incluir a manifestação registrada pela matéria, então o extrator
        procura a frase que a traz em vez de cortar cegamente nas duas primeiras.
        """
        frases = dividir_frases(ctx["texto"])
        if not frases:
            return {"resumo": ctx["titulo"], "temas": extrair_temas(ctx["titulo"]),
                    "fonte_id": ctx["item_id"]}

        resposta = next(
            (f for f in frases[1:] if any(m in normalizar(f) for m in _MARCAS_RESPOSTA)),
            None,
        )
        escolhidas = [frases[0], resposta] if resposta else frases[:2]
        return {
            "resumo": " ".join(f for f in escolhidas if f),
            "temas": extrair_temas(f"{ctx['titulo']} {ctx['texto']}"),
            "fonte_id": ctx["item_id"],
        }

    # -- etapa 4, nível 1 --------------------------------------------------------------

    def _consolidacao_lote(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Agrega os temas do lote, contando os ids que sustentam cada um."""
        resumos: list[dict[str, Any]] = ctx["resumos"]
        por_tema: dict[str, list[str]] = {}
        for r in resumos:
            for tema in r["temas"]:
                por_tema.setdefault(tema, [])
                if r["fonte_id"] not in por_tema[tema]:
                    por_tema[tema].append(r["fonte_id"])

        temas = [
            {"tema": tema, "ocorrencias": len(fontes), "fontes": fontes}
            for tema, fontes in sorted(por_tema.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ]
        pontos = [f"{r['resumo']} ({r['fonte_id']})" for r in resumos]
        return {
            "temas": temas,
            "pontos": pontos,
            "fontes": [r["fonte_id"] for r in resumos],
        }

    # -- etapa 4, nível 2 --------------------------------------------------------------

    def _consolidacao_final(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Consolida as parciais no panorama, respeitando o formato pedido."""
        parciais: list[dict[str, Any]] = ctx["parciais"]

        por_tema: dict[str, list[str]] = {}
        for parcial in parciais:
            for tema in parcial["temas"]:
                alvo = por_tema.setdefault(tema["tema"], [])
                for fonte in tema["fontes"]:
                    if fonte not in alvo:
                        alvo.append(fonte)

        ordenados = sorted(por_tema.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        fontes: list[str] = []
        for parcial in parciais:
            for fonte in parcial["fontes"]:
                if fonte not in fontes:
                    fontes.append(fonte)

        limite = 4 if ctx["formato"] == "neutro" else 6
        temas = [
            {"tema": tema, "ocorrencias": len(ids), "fontes": ids}
            for tema, ids in ordenados[:limite]
        ]

        return {
            "ator_id": ctx["ator_id"],
            "janela": ctx["janela"],
            "eixo": ctx["eixo"],
            "total_itens": len(fontes),
            "temas": temas,
            "fontes": fontes,
            "texto": _paragrafo(ctx["eixo"], parciais, temas),
        }


def _primeira_frase(ponto: str) -> str:
    """Primeira frase de um ponto consolidado, sem os ids ao final."""
    sem_ids = re.sub(r"\s*\([^)]*\)\s*$", "", ponto).strip()
    frases = dividir_frases(sem_ids)
    return frases[0] if frases else sem_ids


def _ids_do_ponto(ponto: str) -> list[str]:
    """Ids citados entre parênteses em um ponto consolidado."""
    return re.findall(r"\b(?:not_\d+)\b", ponto) or ["desconhecido"]


#: Abertura que nomeia o recorte, espelhando o que cada fragmento de formato pede ao
#: modelo de verdade. O fake não sabe distinguir crítica de elogio — ele copia o rótulo
#: que já veio do classificador; a frase existe para a FORMA bater com a do modelo real.
_ABERTURA_POR_EIXO = {
    "negativo": "As críticas que a cobertura do período registra são estas.",
    "positivo": "Os destaques que a cobertura do período registra são estes.",
    "neutro": "O que a cobertura do período relata, sem tomar partido, é isto.",
}


def _paragrafo(
    eixo: str,
    parciais: list[dict[str, Any]],
    temas: list[dict[str, Any]],
) -> str:
    """Junta os pontos das parciais em um parágrafo único, sem ids na prosa.

    O fake é extrativo: quem faz o recorte de verdade é o fragmento de formato lido pelo
    modelo real. Aqui o que importa é produzir, sem rede e sem chave, um `texto` com a
    MESMA forma que o schema e a etapa 6 exigem — parágrafo não vazio, abrindo pelo
    recorte e sem nenhum id entre parênteses.
    """
    abertura = _ABERTURA_POR_EIXO.get(eixo, _ABERTURA_POR_EIXO["neutro"])

    frases: list[str] = []
    for parcial in parciais:
        for ponto in parcial["pontos"]:
            frase = _sem_ids(_primeira_frase(ponto)).rstrip(".").strip()
            if frase:
                frases.append(f"{frase}.")
            if len(frases) == 5:
                break
        if len(frases) == 5:
            break

    if not frases:
        principais = ", ".join(t["tema"] for t in temas[:2]) or "assuntos variados"
        frases = [f"Os assuntos mais frequentes são {principais}."]

    return " ".join([abertura, *frases])


def _sem_ids(frase: str) -> str:
    """Remove parênteses que carreguem id, onde quer que estejam na frase."""
    limpa = re.sub(r"\s*\([^)]*_[^)]*\)", "", frase)
    return re.sub(r"\s{2,}", " ", limpa).strip()


# --------------------------------------------------------------------------------------
# ModeloAPI — compatível com a API da OpenAI (Maritaca/Sabiá trocando o BASE_URL)
# --------------------------------------------------------------------------------------


class ModeloAPI:
    """Cliente para API compatível com a da OpenAI.

    Lê `BASE_URL`, `API_KEY`, `MODEL` e `EXTRA_BODY` do ambiente. O nome do modelo não
    existe no código: trocar de provedor é editar o `.env`.

    `EXTRA_BODY` é um JSON repassado ao corpo da requisição sem interpretação. É por
    onde passam as particularidades de cada provedor — desligar o raciocínio de um
    modelo que raciocina, por exemplo — sem que este arquivo precise conhecer nenhuma
    delas.
    """

    def __init__(self) -> None:
        import httpx  # importado aqui para não ser exigido no caminho padrão (fake)

        base_url = os.environ.get("BASE_URL", "").strip().rstrip("/")
        api_key = os.environ.get("API_KEY", "").strip()
        modelo = os.environ.get("MODEL", "").strip()

        faltando = [
            nome
            for nome, valor in (("BASE_URL", base_url), ("API_KEY", api_key), ("MODEL", modelo))
            if not valor
        ]
        if faltando:
            raise RuntimeError(
                f"Faltam variáveis de ambiente para --modelo api: {', '.join(faltando)}. "
                "Copie o .env.example para .env e preencha. "
                "Sem isso, use o padrão --modelo fake, que não exige nenhuma chave."
            )

        def ler_json(nome: str) -> dict[str, Any]:
            bruto = os.environ.get(nome, "").strip() or "{}"
            try:
                valor = json.loads(bruto)
            except json.JSONDecodeError as erro:
                raise RuntimeError(
                    f"{nome} não é um JSON válido: {erro}. Valor lido: {bruto[:120]}"
                ) from erro
            if not isinstance(valor, dict):
                raise RuntimeError(f"{nome} precisa ser um objeto JSON.")
            return valor

        extra = ler_json("EXTRA_BODY")

        # Mapa tarefa -> corpo extra, aplicado por cima do EXTRA_BODY geral. Existe
        # porque as tarefas não têm o mesmo perfil: o map é extrativo e uma notícia de
        # 500 caracteres não tem o que deliberar, enquanto o reduce agrega dezenas de
        # resumos e conta ids. Sem esta separação, ligar raciocínio para ajudar o reduce
        # também o liga no map, onde ele só gasta orçamento e devolve conteúdo vazio.
        por_tarefa = ler_json("EXTRA_BODY_POR_TAREFA")
        for tarefa, corpo in por_tarefa.items():
            if not isinstance(corpo, dict):
                raise RuntimeError(
                    f"EXTRA_BODY_POR_TAREFA['{tarefa}'] precisa ser um objeto JSON."
                )
        self._extra_por_tarefa: dict[str, dict[str, Any]] = por_tarefa

        # 60 s serve para uma API remota, mas não para um servidor local: a primeira
        # chamada depois de o servidor subir paga o carregamento do modelo do disco
        # para a VRAM, o que passa de um minuto num modelo de 6 GB. O timeout é
        # configurável porque o valor certo depende de onde o modelo roda.
        try:
            timeout = float(os.environ.get("TIMEOUT_SEGUNDOS", "300"))
        except ValueError as erro:
            raise RuntimeError(
                f"TIMEOUT_SEGUNDOS precisa ser um número: {erro}"
            ) from erro

        # Ligada por padrão porque é o que impede o modelo de inventar nome de campo.
        # Desligue com SAIDA_ESTRUTURADA=0 se o provedor não aceitar `response_format`.
        self._estruturada = os.environ.get("SAIDA_ESTRUTURADA", "1").strip() not in (
            "0", "false", "False", "nao", "não",
        )

        self.nome: str = modelo
        self.chamadas: int = 0
        self.chamadas_por_tarefa: Counter[str] = Counter()
        self._base_url = base_url
        self._extra: dict[str, Any] = extra
        self._cliente = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def zerar_contadores(self) -> None:
        """Zera os contadores. Simetria com o fake."""
        self.chamadas = 0
        self.chamadas_por_tarefa = Counter()

    def gerar(
        self,
        *,
        prompt: str,
        contexto: dict[str, Any],
        tarefa: str,
        esquema: dict[str, Any] | None = None,
    ) -> RespostaBruta:
        """Uma chamada de chat completion. `contexto` é ignorado aqui."""
        self.chamadas += 1
        self.chamadas_por_tarefa[tarefa] += 1

        corpo_envio: dict[str, Any] = {
            "model": self.nome,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": TEMPERATURA,
            "max_tokens": MAX_TOKENS_POR_TAREFA.get(tarefa, 2000),
        }

        # Decodificação restrita: o servidor passa a só conseguir emitir tokens que
        # levem a um JSON válido contra este schema. Deixa de ser possível o modelo
        # escolher o nome do campo — medido: sem isto, 3 de 11 consolidações de lote
        # escreveram "puntos" no lugar de "pontos", e com temperatura 0 o reparo
        # reproduzia o mesmo erro. Nem todo provedor aceita; daí o interruptor.
        if esquema is not None and self._estruturada:
            corpo_envio["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": tarefa, "strict": True, "schema": esquema},
            }

        # O que vem do .env tem a última palavra: é o ponto de escape do usuário. O
        # ajuste por tarefa vem depois do geral, então refina em vez de competir.
        corpo_envio.update(self._extra)
        corpo_envio.update(self._extra_por_tarefa.get(tarefa, {}))

        resposta = self._cliente.post(
            f"{self._base_url}/chat/completions", json=corpo_envio
        )
        if resposta.status_code >= 400:
            # O corpo costuma dizer qual campo o provedor recusou — sem ele, um
            # EXTRA_BODY que este provedor não aceita vira um 400 mudo.
            raise ErroDeResposta(
                f"O provedor recusou a chamada de '{tarefa}' "
                f"(HTTP {resposta.status_code}): {resposta.text[:400]}"
            )

        corpo = resposta.json()
        uso = corpo.get("usage") or {}
        escolha = corpo["choices"][0]
        texto = escolha.get("message", {}).get("content") or ""
        motivo = escolha.get("finish_reason")

        if not texto.strip():
            raciocinio = uso.get("reasoning_tokens") or uso.get("completion_tokens") or 0
            raise ErroDeResposta(
                f"O modelo '{self.nome}' devolveu conteúdo vazio em '{tarefa}' "
                f"(finish_reason={motivo}, tokens gerados={raciocinio}). "
                "Um modelo que raciocina gasta o orçamento pensando e não escreve a "
                "resposta; desligue isso pelo EXTRA_BODY no .env. Repetir a chamada "
                "não conserta — só consome cota."
            )
        if motivo == "length":
            teto = MAX_TOKENS_POR_TAREFA.get(tarefa, 2000)
            raise ErroDeResposta(
                f"A resposta de '{tarefa}' foi cortada no teto de {teto} tokens, então "
                f"o JSON chegou incompleto. Aumente MAX_TOKENS_POR_TAREFA['{tarefa}'] "
                "em config.py."
            )

        return RespostaBruta(
            texto=texto,
            tokens={
                "prompt": uso.get("prompt_tokens", 0),
                "resposta": uso.get("completion_tokens", 0),
            },
        )


# --------------------------------------------------------------------------------------
# Camada de validação — regra 7, comum às duas implementações
# --------------------------------------------------------------------------------------

_DICA_REPARO = (
    "\n\nA resposta anterior não pôde ser lida como o objeto JSON pedido. "
    "Responda de novo, apenas com o objeto JSON, sem cercas de código, sem texto antes "
    "ou depois, e com todos os campos exigidos."
)


def extrair_json(texto: str) -> Any:
    """Extrai o objeto JSON de uma resposta, tolerando cercas de código.

    Não conserta conteúdo: se não houver um objeto JSON legível, levanta erro.
    """
    limpo = texto.strip()
    limpo = re.sub(r"^```(?:json)?\s*", "", limpo)
    limpo = re.sub(r"\s*```$", "", limpo)
    try:
        return json.loads(limpo)
    except json.JSONDecodeError:
        pass
    inicio = limpo.find("{")
    fim = limpo.rfind("}")
    if inicio == -1 or fim <= inicio:
        raise ValueError("Nenhum objeto JSON encontrado na resposta.")
    return json.loads(limpo[inicio : fim + 1])


@dataclass(slots=True)
class ResultadoValidado(Generic[T]):
    """Saída já validada, com o que a auditoria precisa saber sobre a chamada."""

    dados: T
    tentativas: int
    tokens: dict[str, int] | None


def gerar_validado(
    modelo: Modelo,
    *,
    prompt: str,
    schema: type[T],
    contexto: dict[str, Any],
    tarefa: str,
) -> ResultadoValidado[T]:
    """Gera, valida com Pydantic e, se falhar, tenta reparar UMA vez.

    Regra 7: texto solto não passa. Depois da tentativa de reparo, o erro é registrado —
    nunca contornado.
    """
    # O mesmo schema que valida a resposta pode restringir a geração dela. Quem sabe
    # aproveitar, aproveita; quem não sabe, ignora — a validação abaixo é a mesma.
    esquema = schema.model_json_schema()

    ultimo_erro = ""
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        prompt_efetivo = prompt if tentativa == 1 else prompt + _DICA_REPARO
        bruta = modelo.gerar(
            prompt=prompt_efetivo, contexto=contexto, tarefa=tarefa, esquema=esquema
        )
        try:
            return ResultadoValidado(
                dados=schema.model_validate(extrair_json(bruta.texto)),
                tentativas=tentativa,
                tokens=bruta.tokens,
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as erro:
            ultimo_erro = str(erro).splitlines()[0] if str(erro) else type(erro).__name__

    raise ErroDeValidacao(
        f"A saída do modelo para '{tarefa}' não passou na validação após "
        f"{MAX_TENTATIVAS} tentativas: {ultimo_erro}"
    )
