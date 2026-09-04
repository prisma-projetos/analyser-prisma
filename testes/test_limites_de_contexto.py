"""Regressões do incidente de 3 de setembro de 2026 — prompt maior que o contexto.

O que estes testes protegem não é uma preferência de dimensionamento: é a única falha
deste módulo que o servidor NÃO sinaliza. Quando o prompt passa do contexto, o Ollama
não recusa a chamada — ele corta o prompt pela cabeça, reporta em `prompt_tokens` o
tamanho já cortado e devolve `finish_reason="stop"`.

Como o começo do prompt traz as REGRAS e a TAREFA, e o fim traz os dados e o formato da
resposta, o que sobrevive ao corte é exatamente o que faz o modelo produzir um JSON bem
formado — sobre parte da entrada e sem as regras de fidelidade. A validação passa, a
gravação acontece, e nada em lugar nenhum registra que houve perda.

Foi assim que a janela de agosto de 2026 do Lula gravou um panorama citando 47 das 94
matérias, com status de sucesso, deixando os outros 47 itens órfãos em `resumido`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from analyser.config import (
    CONTEXTO_TOKENS,
    MAX_PONTOS_POR_LOTE,
    MAX_TEMAS_POR_LOTE,
)
from analyser.modelos import ErroDeCobertura, ErroDePrompt, ModeloAPI, ModeloFake
from analyser.pipeline import Analyser
from analyser.schemas import ConsolidacaoParcial, Panorama, RegistroResumo, TemaContado
from testes.conftest import ler_json, rodar


def _resumo(fonte_id: str) -> RegistroResumo:
    """O mínimo de `RegistroResumo` para exercitar `_verificar_cobertura` isoladamente.

    Os valores fora de `fonte_id` são irrelevantes para esta checagem — ela só compara
    conjuntos de ids —, mas o schema exige todos os campos.
    """
    return RegistroResumo(
        item_id=fonte_id,
        ator_id="ator_teste",
        janela="2026-08",
        rotulo="neutro",
        veiculo="veículo de teste",
        url="https://exemplo.invalido",
        data=date(2026, 8, 1),
        titulo="título de teste",
        resumo="resumo de teste",
        temas=["tema"],
        fonte_id=fonte_id,
        modelo="fake",
        hash_prompt_resumo="a" * 64,
        criado_em="2026-08-01T00:00:00",
    )


def _panorama(fontes: list[str]) -> Panorama:
    """Um `Panorama` mínimo e válido, com `fontes` controlada pelo teste."""
    return Panorama(
        ator_id="ator_teste",
        janela="2026-08",
        eixo="neutro",
        temas=[TemaContado(tema="assunto", ocorrencias=1, fontes=[fontes[0]])],
        texto="Texto de teste sobre a cobertura.",
        fontes=fontes,
        total_itens=len(fontes),
    )


@pytest.fixture
def modelo(monkeypatch: pytest.MonkeyPatch) -> ModeloAPI:
    """Um `ModeloAPI` real com o transporte HTTP substituído."""
    monkeypatch.setenv("BASE_URL", "http://servidor.invalido/v1")
    monkeypatch.setenv("API_KEY", "irrelevante")
    monkeypatch.setenv("MODEL", "modelo-de-teste")
    monkeypatch.delenv("EXTRA_BODY", raising=False)
    return ModeloAPI()


class _Resposta:
    """O mínimo da interface de `httpx.Response` que `gerar` consome."""

    status_code = 200

    def __init__(self, prompt_tokens: int, texto: str = '{"ok": true}') -> None:
        self._corpo: dict[str, Any] = {
            "choices": [{"message": {"content": texto}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10},
        }

    def json(self) -> dict[str, Any]:
        return self._corpo


def test_prompt_maior_que_o_contexto_nao_chega_a_ser_enviado(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conta é conferida ANTES da chamada, não depois.

    Descobrir depois custou 2h08 de execução para terminar em cinco janelas cortadas e
    uma corrompida. Conferir antes custa uma divisão.
    """
    enviadas: list[Any] = []
    monkeypatch.setattr(
        modelo._cliente, "post", lambda *a, **k: enviadas.append(k) or _Resposta(10)
    )

    # Folgadamente acima do contexto, mesmo pela estimativa mais conservadora.
    prompt = "a" * (CONTEXTO_TOKENS * 10)

    with pytest.raises(ErroDePrompt, match="cortaria o prompt pela cabeça"):
        modelo.gerar(prompt=prompt, contexto={}, tarefa="consolidacao_lote")

    assert not enviadas, "a chamada não deveria ter sido gasta"


def test_truncamento_silencioso_do_servidor_vira_erro(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Servidor que reporta menos tokens do que recebeu cortou o prompt.

    É o caso do Lula: `finish_reason="stop"`, JSON válido, nenhum sinal de erro — só o
    `prompt_tokens` denunciando que o texto que chegou ao modelo era menor que o enviado.
    Sem este teste, a única evidência do dano volta a ser um aviso que ninguém lê.
    """
    prompt = "palavra " * 4000  # ~32000 caracteres

    # O servidor responde como se tivesse processado um prompt muito menor — foi o que o
    # Ollama fez com o lote de 94 resumos: 8194 tokens no lugar de ~16900.
    monkeypatch.setattr(modelo._cliente, "post", lambda *a, **k: _Resposta(300))

    with pytest.raises(ErroDePrompt, match="TRUNCADO pelo servidor"):
        modelo.gerar(prompt=prompt, contexto={}, tarefa="consolidacao_lote")


def test_prompt_que_coube_inteiro_nao_dispara_falso_positivo(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O detector não pode derrubar execução boa.

    O piso usado na comparação é deliberadamente frouxo: ele responde "qual o MENOR
    número de tokens que este texto poderia ter?", não "quantos tokens ele tem". Um
    prompt que passou inteiro fica muito acima dele.
    """
    prompt = "palavra " * 4000
    monkeypatch.setattr(modelo._cliente, "post", lambda *a, **k: _Resposta(9000))

    resposta = modelo.gerar(prompt=prompt, contexto={}, tarefa="consolidacao_lote")

    assert resposta.tokens == {"prompt": 9000, "resposta": 10}


def test_a_parcial_tem_teto_de_temas_e_de_pontos() -> None:
    """O teto do nível 1 é o que faz o reduce reduzir.

    Sem ele as listas crescem proporcionais ao lote, e a saída do nível 1 é exatamente o
    prompt do nível 2 — um primeiro nível que não comprime só adia o estouro para a
    `consolidacao_final`. Medido antes do teto: 31 resumos devolveram 78 temas.
    """
    tema = TemaContado(tema="assunto", ocorrencias=1, fontes=["not_1"])

    with pytest.raises(ValidationError):
        ConsolidacaoParcial(
            temas=[tema] * (MAX_TEMAS_POR_LOTE + 1), pontos=[], fontes=["not_1"]
        )

    with pytest.raises(ValidationError):
        ConsolidacaoParcial(
            temas=[], pontos=["ponto"] * (MAX_PONTOS_POR_LOTE + 1), fontes=["not_1"]
        )


def test_o_teto_chega_ao_json_schema_como_maxitems() -> None:
    """O teto precisa virar gramática no servidor, não só validação nossa.

    Validar depois transforma o estouro em retentativa cara; `maxItems` no schema faz o
    decodificador ser OBRIGADO a fechar a lista. O prompt já pedia "prefira poucos temas
    bem sustentados" e recebeu 78 — pedir no texto não é impor.
    """
    propriedades = ConsolidacaoParcial.model_json_schema()["properties"]

    assert propriedades["temas"]["maxItems"] == MAX_TEMAS_POR_LOTE
    assert propriedades["pontos"]["maxItems"] == MAX_PONTOS_POR_LOTE


def test_fontes_continua_sem_teto() -> None:
    """`fontes` é a lista que marca item como `consolidado`.

    Limitá-la produziria exatamente o dano que o resto deste arquivo detecta: item que
    participou da consolidação, não foi citado, e ficou órfão em `resumido` — invisível
    como perda e eterno como pendência.
    """
    propriedades = ConsolidacaoParcial.model_json_schema()["properties"]

    assert "maxItems" not in propriedades["fontes"]


def test_verificar_cobertura_dispara_quando_fontes_esta_incompleta() -> None:
    """A regressão do dano silencioso, testada direto no arame de tropeço.

    Um panorama que cobre parte da janela faz duas coisas ruins de uma vez: publica uma
    síntese que afirma cobrir o mês cobrindo parte dele, e deixa os itens não citados
    órfãos em `resumido` — a janela volta a aparecer pendente para sempre, e refazê-la
    gera outro panorama incompleto.

    Este teste teria simulado o modelo "esquecendo" uma fonte na resposta, como fazia
    antes de 3 de setembro de 2026 — mas desde que `_etapa_4_e_5` passou a atribuir
    `panorama.fontes` a partir do próprio `resumos`, ANTES de chamar esta verificação,
    esse caminho de ataque via resposta do modelo ficou estruturalmente inalcançável: o
    valor que o fake devolvesse seria sempre sobrescrito antes de chegar aqui.
    Isso é o comportamento correto, não uma lacuna — por isso o teste passou a chamar
    `_verificar_cobertura` diretamente, construindo a divergência à mão, para que o
    tripwire continue provado mesmo que o caminho normal não consiga mais alcançá-lo.
    """
    resumos = [_resumo("art_1"), _resumo("art_2"), _resumo("art_3")]
    panorama = _panorama(["art_1", "art_2"])  # "art_3" fica de fora

    with pytest.raises(ErroDeCobertura, match="órfãos em 'resumido'") as excinfo:
        Analyser._verificar_cobertura(panorama, resumos, janela="2026-08", eixo="neutro")

    assert "art_3" in str(excinfo.value)


def test_verificar_cobertura_nao_dispara_com_cobertura_completa() -> None:
    """Contraprova: cobertura completa não é confundida com incompleta."""
    resumos = [_resumo("art_1"), _resumo("art_2")]
    panorama = _panorama(["art_1", "art_2"])

    Analyser._verificar_cobertura(panorama, resumos, janela="2026-08", eixo="neutro")


def test_o_pipeline_real_sempre_grava_fontes_completa(tmp_path: Path) -> None:
    """Fim a fim: o panorama gravado cobre 100% da sua entrada, sempre.

    Complementa o teste unitário do tripwire provando a outra metade do que ele protege:
    que a atribuição em `_etapa_4_e_5` (`panorama.fontes = [r.fonte_id for r in
    resumos]`) de fato acontece antes da gravação no pipeline real, com o `ModeloFake`
    sem nenhuma modificação — não é preciso mais simular um modelo capenga para a
    cobertura sair completa: ela sai completa por construção.
    """
    resultado = rodar(tmp_path, "rodada_1.json", ModeloFake())

    assert not resultado.erros

    panoramas = ler_json(tmp_path / "panoramas.json")
    assert panoramas, "a rodada de teste precisa ter gerado ao menos um panorama"

    resumos = ler_json(tmp_path / "resumos_item.json")
    assert isinstance(resumos, dict)

    for registro in panoramas:
        panorama = registro["panorama"]
        entrada = {
            r["fonte_id"]
            for r in resumos.values()
            if r["ator_id"] == panorama["ator_id"]
            and r["janela"] == panorama["janela"]
            and r["rotulo"] == panorama["eixo"]
        }
        assert set(panorama["fontes"]) == entrada
        assert panorama["total_itens"] == len(entrada)
