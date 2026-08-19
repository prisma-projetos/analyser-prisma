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

from analyser.config import MAX_TENTATIVAS, TEMPERATURA

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


class Modelo(Protocol):
    """Contrato mínimo que o pipeline exige de um modelo."""

    nome: str
    chamadas: int
    chamadas_por_tarefa: Counter[str]

    def gerar(self, *, prompt: str, contexto: dict[str, Any], tarefa: str) -> RespostaBruta:
        """Gera texto para uma tarefa. `contexto` traz os dados já estruturados."""
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

    def gerar(self, *, prompt: str, contexto: dict[str, Any], tarefa: str) -> RespostaBruta:
        """Despacha para o gerador da tarefa e contabiliza a chamada."""
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

        limite = 6 if ctx["formato"] == "detalhado" else 4
        temas = [
            {"tema": tema, "ocorrencias": len(ids), "fontes": ids}
            for tema, ids in ordenados[:limite]
        ]

        panorama: dict[str, Any] = {
            "ator_id": ctx["ator_id"],
            "janela": ctx["janela"],
            "eixo": ctx["eixo"],
            "total_itens": len(fontes),
            "temas": temas,
            "fontes": fontes,
            "texto": None,
            "fatos_mais_citados": [],
            "respostas_registradas": [],
        }

        if ctx["formato"] == "detalhado":
            pontos = [p for parcial in parciais for p in parcial["pontos"]]
            panorama["fatos_mais_citados"] = [
                {"descricao": _primeira_frase(ponto), "fontes": _ids_do_ponto(ponto)}
                for ponto in pontos[:5]
            ]
            panorama["respostas_registradas"] = _respostas(parciais)
        else:
            quantidade = len(fontes)
            principais = ", ".join(t["tema"] for t in temas[:2]) or "assuntos variados"
            panorama["texto"] = (
                f"A cobertura do período reúne {quantidade} "
                f"{'matéria' if quantidade == 1 else 'matérias'} sobre o alvo "
                f"({', '.join(fontes)}). Os assuntos mais frequentes são {principais}. "
                "As matérias descrevem os fatos sem que os veículos registrem contestação."
            )
        return panorama


def _primeira_frase(ponto: str) -> str:
    """Primeira frase de um ponto consolidado, sem os ids ao final."""
    sem_ids = re.sub(r"\s*\([^)]*\)\s*$", "", ponto).strip()
    frases = dividir_frases(sem_ids)
    return frases[0] if frases else sem_ids


def _ids_do_ponto(ponto: str) -> list[str]:
    """Ids citados entre parênteses em um ponto consolidado."""
    return re.findall(r"\b(?:not_\d+)\b", ponto) or ["desconhecido"]


def _respostas(parciais: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detecta, de forma extrativa, se a cobertura registra manifestação de citados."""
    com_resposta: list[str] = []
    sem_resposta: list[str] = []
    for parcial in parciais:
        for ponto in parcial["pontos"]:
            base = normalizar(ponto)
            ids = _ids_do_ponto(ponto)
            alvo = com_resposta if ("procurad" in base or "afirmou" in base or
                                    "respondeu" in base or "informou" in base or
                                    "defende" in base or "contesta" in base) else sem_resposta
            for identificador in ids:
                if identificador not in alvo:
                    alvo.append(identificador)

    registros: list[dict[str, Any]] = []
    if com_resposta:
        registros.append(
            {"citado": "alvo da cobertura", "houve_resposta": True, "fontes": com_resposta}
        )
    if sem_resposta:
        registros.append(
            {"citado": "alvo da cobertura", "houve_resposta": False, "fontes": sem_resposta}
        )
    return registros


# --------------------------------------------------------------------------------------
# ModeloAPI — compatível com a API da OpenAI (Maritaca/Sabiá trocando o BASE_URL)
# --------------------------------------------------------------------------------------


class ModeloAPI:
    """Cliente para API compatível com a da OpenAI.

    Lê `BASE_URL`, `API_KEY` e `MODEL` do ambiente. O nome do modelo não existe no
    código: trocar de provedor é editar o `.env`.
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

        self.nome: str = modelo
        self.chamadas: int = 0
        self.chamadas_por_tarefa: Counter[str] = Counter()
        self._base_url = base_url
        self._cliente = httpx.Client(
            timeout=60.0, headers={"Authorization": f"Bearer {api_key}"}
        )

    def zerar_contadores(self) -> None:
        """Zera os contadores. Simetria com o fake."""
        self.chamadas = 0
        self.chamadas_por_tarefa = Counter()

    def gerar(self, *, prompt: str, contexto: dict[str, Any], tarefa: str) -> RespostaBruta:
        """Uma chamada de chat completion. `contexto` é ignorado aqui."""
        self.chamadas += 1
        self.chamadas_por_tarefa[tarefa] += 1
        resposta = self._cliente.post(
            f"{self._base_url}/chat/completions",
            json={
                "model": self.nome,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": TEMPERATURA,
            },
        )
        resposta.raise_for_status()
        corpo = resposta.json()
        uso = corpo.get("usage") or {}
        return RespostaBruta(
            texto=corpo["choices"][0]["message"]["content"],
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
    ultimo_erro = ""
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        prompt_efetivo = prompt if tentativa == 1 else prompt + _DICA_REPARO
        bruta = modelo.gerar(prompt=prompt_efetivo, contexto=contexto, tarefa=tarefa)
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
