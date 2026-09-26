"""Retomada quando o provedor do modelo cai: pausa e tenta de novo o mesmo ponto.

O incidente que motivou isto (docs/Diagnostico_Kubernetes_Migration_Ollama_2026_09_17.md):
com o Ollama inacessível, cada evento virava `erro` e o lote avançava para o próximo. Três
execuções seguidas marcaram 1.500 eventos como erro definitivo, e nenhum deles seria
retomado quando o Ollama voltasse.

Estes testes não usam banco: a conexão é um registro de comandos e as partes do worker que
consultam o PostgreSQL são substituídas. O que se verifica é o fluxo de controle — quem
pausa, quem vira erro, o que é contado e o que é gravado.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from analyser import worker as modulo_worker
from analyser.config import ESPERA_FALHA_PADRAO_SEGUNDOS, Config, ler_espera_falha
from analyser.modelos import ErroDePrompt, ErroDeResposta, ErroDoProvedor, ModeloAPI, ModeloFake
from analyser.worker import ResultadoWorker, Worker

# --------------------------------------------------------------------------------------
# Classificação das falhas no cliente do modelo
# --------------------------------------------------------------------------------------


@pytest.fixture
def modelo(monkeypatch: pytest.MonkeyPatch) -> ModeloAPI:
    """Um `ModeloAPI` real com o transporte HTTP substituído."""
    monkeypatch.setenv("BASE_URL", "http://servidor.invalido/v1")
    monkeypatch.setenv("API_KEY", "irrelevante")
    monkeypatch.setenv("MODEL", "modelo-de-teste")
    monkeypatch.delenv("EXTRA_BODY", raising=False)
    monkeypatch.delenv("EXTRA_BODY_POR_TAREFA", raising=False)
    return ModeloAPI()


class _Resposta:
    """O mínimo da interface de `httpx.Response` que `gerar` consome."""

    def __init__(self, status_code: int = 200, corpo: Any = None, texto: str = "") -> None:
        self.status_code = status_code
        self._corpo = corpo
        self.text = texto

    def json(self) -> Any:
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo


def _levanta(erro: Exception):
    def post(*_a: Any, **_k: Any) -> Any:
        raise erro

    return post


@pytest.mark.parametrize(
    "erro",
    [
        httpx.ConnectError("[Errno 111] Connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
    ids=["conexao-recusada", "timeout", "conexao-derrubada"],
)
def test_falha_de_rede_vira_erro_do_provedor(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch, erro: Exception
) -> None:
    """Ollama fora do ar, travado ou derrubando a conexão: nada disso é culpa do item."""
    monkeypatch.setattr(modelo._cliente, "post", _levanta(erro))

    with pytest.raises(ErroDoProvedor, match="não respondeu") as capturado:
        modelo.gerar(prompt="oi", contexto={}, tarefa="resumo_item")

    # Continua sendo um `ErroDeResposta`: quem já para a execução nele continua parando.
    assert isinstance(capturado.value, ErroDeResposta)


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_status_http_de_erro_vira_erro_do_provedor(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Status de erro vale para qualquer item da execução, então também pausa."""
    monkeypatch.setattr(
        modelo._cliente, "post", lambda *a, **k: _Resposta(status, texto="modelo não encontrado")
    )

    with pytest.raises(ErroDoProvedor, match=f"HTTP {status}.*modelo não encontrado"):
        modelo.gerar(prompt="oi", contexto={}, tarefa="resumo_item")


def test_corpo_que_nao_e_da_api_vira_erro_do_provedor(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um proxy respondendo 200 com HTML no lugar do modelo não é problema do item."""
    monkeypatch.setattr(
        modelo._cliente, "post", lambda *a, **k: _Resposta(200, corpo=ValueError("não é JSON"))
    )

    with pytest.raises(ErroDoProvedor, match="formato da API"):
        modelo.gerar(prompt="oi", contexto={}, tarefa="resumo_item")


def test_prompt_grande_demais_continua_sendo_erro_de_prompt(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Erro que depende do item não pode pausar: esperar não o conserta."""
    monkeypatch.setattr(modelo._cliente, "post", _levanta(AssertionError("não devia enviar")))

    with pytest.raises(ErroDePrompt) as capturado:
        modelo.gerar(prompt="a" * 2_000_000, contexto={}, tarefa="consolidacao_lote")

    assert not isinstance(capturado.value, ErroDoProvedor)


def test_resposta_valida_continua_passando(
    modelo: ModeloAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpo = {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }
    monkeypatch.setattr(modelo._cliente, "post", lambda *a, **k: _Resposta(200, corpo=corpo))

    bruta = modelo.gerar(prompt="oi", contexto={}, tarefa="resumo_item")

    assert bruta.texto == '{"ok": true}'


# --------------------------------------------------------------------------------------
# Tempo de pausa
# --------------------------------------------------------------------------------------


def test_pausa_padrao_e_uma_hora(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESPERA_FALHA_SEGUNDOS", raising=False)
    assert ler_espera_falha() == ESPERA_FALHA_PADRAO_SEGUNDOS == 3600


def test_pausa_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESPERA_FALHA_SEGUNDOS", "900")
    assert ler_espera_falha() == 900


@pytest.mark.parametrize("valor", ["uma hora", "0", "-5"])
def test_pausa_invalida_falha_com_mensagem_clara(
    monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    monkeypatch.setenv("ESPERA_FALHA_SEGUNDOS", valor)
    with pytest.raises(RuntimeError, match="ESPERA_FALHA_SEGUNDOS"):
        ler_espera_falha()


# --------------------------------------------------------------------------------------
# Worker: pausa e retomada, sem banco
# --------------------------------------------------------------------------------------


class ConexaoFalsa:
    """Registra os comandos enviados; nenhuma consulta devolve linhas."""

    def __init__(self) -> None:
        self.comandos: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql: str, params: Any = None) -> "ConexaoFalsa":
        self.comandos.append((" ".join(sql.split()), params))
        return self

    def fetchone(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def com(self, trecho: str) -> list[tuple[str, Any]]:
        return [c for c in self.comandos if trecho in c[0]]


@pytest.fixture
def conexao() -> ConexaoFalsa:
    return ConexaoFalsa()


@pytest.fixture
def dormidas() -> list[float]:
    return []


@pytest.fixture
def worker(conexao: ConexaoFalsa, dormidas: list[float], raiz: Path) -> Worker:
    config = Config(entrada=raiz / "dados" / "rodada_1.json", prompts=raiz / "prompts")
    return Worker(
        conexao,  # type: ignore[arg-type]
        ModeloFake(),
        config,
        espera_falha_segundos=3600,
        dormir=dormidas.append,
    )


@pytest.fixture
def um_evento(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Um evento pendente com dois itens; enriquecimento sem consulta ao banco."""
    monkeypatch.setattr(
        worker, "_eventos_pendentes", lambda limite: [{"evento_id": 7, "payload_json": {}}]
    )

    class _Notificacao:
        @staticmethod
        def model_validate(payload: Any) -> Any:
            return payload

    monkeypatch.setattr(modulo_worker, "NotificacaoClassifier", _Notificacao)
    monkeypatch.setattr(worker.enriquecedor, "enriquecer", lambda evento: ["item-a", "item-b"])


def _itens_que_falham(falhas: list[Exception]):
    """`_processar_item` que levanta os erros da lista antes de passar a funcionar.

    Cada item processado conta um item criado e um resumo — é o que permite ver se uma
    tentativa desfeita deixou contagem para trás.
    """

    def processar(item: Any, evento_id: int, resultado: ResultadoWorker) -> None:
        resultado.itens_criados += 1
        resultado.tokens.update({"resposta": 1})
        if item == "item-b" and falhas:
            raise falhas.pop(0)
        resultado.resumos_gerados += 1

    return processar


@pytest.mark.usefixtures("um_evento")
def test_provedor_fora_pausa_e_retoma_o_mesmo_evento(
    worker: Worker, conexao: ConexaoFalsa, dormidas: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """O Ollama cai duas vezes no segundo item e volta: o evento termina `processado`."""
    queda = ErroDoProvedor("O provedor do modelo não respondeu (ConnectError).")
    monkeypatch.setattr(worker, "_processar_item", _itens_que_falham([queda, queda]))

    resultado = worker.processar_eventos(limite=10)

    # Pausou duas vezes, pelo tempo configurado, e retomou o mesmo evento.
    assert dormidas == [3600, 3600]
    assert [e["unidade"] for e in resultado.esperas] == ["evento 7", "evento 7"]
    assert resultado.eventos_processados == 1
    assert resultado.erros == []

    # Nunca marcou o evento como erro; marcou como processado uma vez só.
    assert conexao.com("SET status = 'erro'") == []
    assert len(conexao.com("SET status = 'processado'")) == 1
    assert conexao.rollbacks == 2
    assert conexao.commits == 1

    # As tentativas desfeitas não deixaram contagem para trás: são 2 itens, não 6.
    assert resultado.itens_criados == 2
    assert resultado.resumos_gerados == 2
    # Os tokens, ao contrário, foram gastos de verdade nas três tentativas.
    assert resultado.tokens["resposta"] == 6


@pytest.mark.usefixtures("um_evento")
def test_erro_do_proprio_evento_continua_virando_erro_sem_pausa(
    worker: Worker, conexao: ConexaoFalsa, dormidas: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falha que não é do provedor se repetiria igual: marca o evento e segue."""
    monkeypatch.setattr(
        worker, "_processar_item", _itens_que_falham([ValueError("payload inesperado")])
    )

    resultado = worker.processar_eventos(limite=10)

    assert dormidas == []
    assert resultado.esperas == []
    assert resultado.eventos_processados == 0
    assert resultado.itens_criados == 0
    assert resultado.erros == ["evento 7: payload inesperado"]
    assert len(conexao.com("SET status = 'erro'")) == 1
    assert conexao.com("SET status = 'processado'") == []


@pytest.fixture
def uma_janela(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker,
        "_atores_com_janela_pendente",
        lambda: [
            {
                "entity_type": "partido",
                "nr_titulo_eleitoral_candidato": None,
                "nr_partido": 70,
                "ator_nome": "AVANTE",
                "janela_mes": "2026-08",
            }
        ],
    )


def _analyser_que_falha(falhas: list[Exception]):
    """Substituto do `Analyser` cuja consolidação levanta os erros da lista primeiro."""

    class AnalyserFalso:
        def __init__(self, config: Any, modelo: Any, armazenamento: Any) -> None:
            self.tokens: Counter[str] = Counter({"prompt": 10})

        def _recalcular_janela(self, rodada: Any, janela: str, parcial: Any) -> None:
            if falhas:
                raise falhas.pop(0)
            parcial.panoramas.append(object())

    return AnalyserFalso


@pytest.mark.usefixtures("uma_janela")
def test_provedor_fora_pausa_e_retoma_a_mesma_janela(
    worker: Worker, conexao: ConexaoFalsa, dormidas: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    queda = ErroDoProvedor("O provedor recusou a chamada (HTTP 503).")
    monkeypatch.setattr(modulo_worker, "Analyser", _analyser_que_falha([queda, queda]))

    resultado = worker.consolidar_janelas(limite=10)

    assert dormidas == [3600, 3600]
    assert [e["unidade"] for e in resultado.esperas] == ["janela 70/2026-08"] * 2
    assert resultado.janelas_consolidadas == 1
    assert resultado.panoramas_gravados == 1
    assert resultado.erros == []
    assert conexao.rollbacks == 2
    assert conexao.commits == 1
    assert resultado.tokens["prompt"] == 30


@pytest.mark.usefixtures("uma_janela")
def test_erro_da_propria_janela_nao_pausa(
    worker: Worker, conexao: ConexaoFalsa, dormidas: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        modulo_worker, "Analyser", _analyser_que_falha([ErroDePrompt("prompt grande demais")])
    )

    resultado = worker.consolidar_janelas(limite=10)

    assert dormidas == []
    assert resultado.janelas_consolidadas == 0
    assert resultado.erros == ["janela 70/2026-08: prompt grande demais"]


def test_pausas_entram_na_auditoria(worker: Worker) -> None:
    resultado = ResultadoWorker()
    resultado.esperas.append({"unidade": "evento 7", "erro": "x", "espera_segundos": 3600})

    dados = worker._dados_auditoria(1, 10, 0, False, "eventos", resultado, "concluida")

    assert dados["esperas_por_falha_do_provedor"] == resultado.esperas


def test_worker_le_a_pausa_do_ambiente(
    conexao: ConexaoFalsa, raiz: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESPERA_FALHA_SEGUNDOS", "120")
    config = Config(entrada=raiz / "dados" / "rodada_1.json", prompts=raiz / "prompts")

    worker = Worker(conexao, ModeloFake(), config)  # type: ignore[arg-type]

    assert worker.espera_falha_segundos == 120
