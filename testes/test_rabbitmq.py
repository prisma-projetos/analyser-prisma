"""Contratos do consumidor RabbitMQ sem conectar ao broker real."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from analyser.rabbitmq import (
    ConfiguracaoRabbitMQ,
    ConfiguracaoRabbitMQInvalida,
    ConsumidorRabbitMQ,
    InboxLocal,
    RegistroInbox,
)
from analyser.schemas import NotificacaoClassifier


def ambiente_rabbit(**sobrescritas: str) -> dict[str, str]:
    ambiente = {
        "RABBITMQ_HOST": "rabbit.interno",
        "RABBITMQ_PORT": "5672",
        "RABBITMQ_NOTIFIER_USER": "analyser",
        "RABBITMQ_NOTIFIER_PASSWORD": "segredo-apenas-teste",
        "RABBITMQ_NOTIFIER_EXCHANGE": "classifier.saida.exchange",
        "RABBITMQ_NOTIFIER_EXCHANGE_TYPE": "direct",
        "RABBITMQ_NOTIFIER_QUEUE": "classifier.notificacoes.queue",
        "RABBITMQ_NOTIFIER_ROUTING_KEY": "classifier.notificacoes.queue",
        "RABBITMQ_NOTIFIER_DURABLE": "true",
        "RABBITMQ_VHOST": "/",
        "RABBITMQ_USE_TLS": "false",
        "RABBITMQ_HEARTBEAT": "60",
        "RABBITMQ_PREFETCH": "1",
        "RABBITMQ_DECLARE_TOPOLOGY": "false",
    }
    ambiente.update(sobrescritas)
    return ambiente


def payload_valido() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "article_id": 42,
            "job_id": 99,
            "pipeline_version": "v1",
            "link_noticia": "https://exemplo.test/noticia",
            "data_publicacao_noticia": "2026-09-01T10:00:00+00:00",
            "notificado_em": "2026-09-01T10:01:00+00:00",
            "classificacoes": [
                {
                    "entity_type": "politico",
                    "entity_id": "122418060191",
                    "paragraph_index": 3,
                    "classificacao_sentimento": "positivo",
                    "score_confianca": 1.0,
                }
            ],
        }
    ).encode("utf-8")


class CanalFalso:
    def __init__(self) -> None:
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []
        self.qos: list[int] = []
        self.operacoes_topologia: list[tuple[str, dict[str, Any]]] = []

    def basic_ack(self, *, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)

    def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
        self.nacks.append((delivery_tag, requeue))

    def basic_qos(self, *, prefetch_count: int) -> None:
        self.qos.append(prefetch_count)

    def exchange_declare(self, **kwargs: Any) -> None:
        self.operacoes_topologia.append(("exchange_declare", kwargs))

    def queue_declare(self, **kwargs: Any) -> None:
        self.operacoes_topologia.append(("queue_declare", kwargs))

    def queue_bind(self, **kwargs: Any) -> None:
        self.operacoes_topologia.append(("queue_bind", kwargs))


class MetodoFalso:
    def __init__(self, delivery_tag: int = 7) -> None:
        self.delivery_tag = delivery_tag


class InboxQueFalha:
    def registrar(self, evento: NotificacaoClassifier, payload: bytes) -> RegistroInbox:
        raise OSError("disco indisponível")

    def registrar_rejeitado(self, payload: bytes, erro: str) -> RegistroInbox:
        raise OSError("disco indisponível")


def consumidor(inbox: Any, **ambiente: str) -> ConsumidorRabbitMQ:
    configuracao = ConfiguracaoRabbitMQ.from_env(ambiente_rabbit(**ambiente))
    return ConsumidorRabbitMQ(configuracao, inbox)


def test_configuracao_confirmada_e_lida_sem_declarar_topologia() -> None:
    cfg = ConfiguracaoRabbitMQ.from_env(ambiente_rabbit())

    assert cfg.vhost == "/"
    assert cfg.use_tls is False
    assert cfg.heartbeat == 60
    assert cfg.prefetch == 1
    assert cfg.declare_topology is False


def test_configuracao_rejeita_segredo_ou_fila_ausente() -> None:
    ambiente = ambiente_rabbit()
    del ambiente["RABBITMQ_NOTIFIER_PASSWORD"]

    with pytest.raises(ConfiguracaoRabbitMQInvalida, match="PASSWORD"):
        ConfiguracaoRabbitMQ.from_env(ambiente)


def test_payload_do_classifier_preserva_paragraph_index() -> None:
    evento = NotificacaoClassifier.model_validate_json(payload_valido())

    assert evento.classificacoes[0].paragraph_index == 3
    assert evento.classificacoes[0].score_confianca == 1.0


def test_payload_com_campo_desconhecido_e_rejeitado() -> None:
    dados = json.loads(payload_valido())
    dados["campo_inventado"] = True

    with pytest.raises(ValidationError, match="campo_inventado"):
        NotificacaoClassifier.model_validate(dados)


def test_consumidor_valido_grava_antes_do_ack_e_deduplica(tmp_path: Path) -> None:
    inbox = InboxLocal(tmp_path)
    receiver = consumidor(inbox)
    canal = CanalFalso()
    metodo = MetodoFalso()
    payload = payload_valido()

    receiver._processar_entrega(canal, metodo, payload)
    receiver._processar_entrega(canal, MetodoFalso(8), payload)

    registros = [
        json.loads(linha)
        for linha in (tmp_path / "inbox_eventos.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert canal.acks == [7, 8]
    assert canal.nacks == []
    assert len(registros) == 1
    assert registros[0]["tipo"] == "recebido"
    assert registros[0]["payload"]["classificacoes"][0]["paragraph_index"] == 3


def test_inbox_deduplica_depois_de_reiniciar_o_consumidor(tmp_path: Path) -> None:
    payload = payload_valido()
    evento = NotificacaoClassifier.model_validate_json(payload)
    primeiro = InboxLocal(tmp_path)
    assert primeiro.registrar(evento, payload).novo is True

    reiniciado = InboxLocal(tmp_path)
    repetido = reiniciado.registrar(evento, payload)

    assert repetido.novo is False
    assert len((tmp_path / "inbox_eventos.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_consumidor_preserva_mensagem_invalida_e_confirma_para_evitar_loop(tmp_path: Path) -> None:
    receiver = consumidor(InboxLocal(tmp_path))
    canal = CanalFalso()

    receiver._processar_entrega(canal, MetodoFalso(), b'{"schema_version": 1}')

    registro = json.loads((tmp_path / "inbox_eventos.jsonl").read_text(encoding="utf-8"))
    assert canal.acks == [7]
    assert canal.nacks == []
    assert registro["tipo"] == "rejeitado"
    assert "article_id" in registro["erro_validacao"]


def test_falha_no_inbox_reenfileira_em_vez_de_confirmar() -> None:
    receiver = consumidor(InboxQueFalha())
    canal = CanalFalso()

    receiver._processar_entrega(canal, MetodoFalso(), payload_valido())

    assert canal.acks == []
    assert canal.nacks == [(7, True)]


def test_topologia_nao_e_declarada_quando_infra_e_responsavel() -> None:
    canal = CanalFalso()
    receiver = consumidor(InboxQueFalha())

    receiver._preparar_canal(canal)

    assert canal.qos == [1]
    assert canal.operacoes_topologia == []


def test_topologia_so_e_declarada_quando_configurada_expressamente() -> None:
    canal = CanalFalso()
    receiver = consumidor(InboxQueFalha(), RABBITMQ_DECLARE_TOPOLOGY="true")

    receiver._preparar_canal(canal)

    assert [operacao for operacao, _ in canal.operacoes_topologia] == [
        "exchange_declare",
        "queue_declare",
        "queue_bind",
    ]


# --------------------------------------------------------------------------------------
# Limite de mensagens por execução
# --------------------------------------------------------------------------------------


class CanalComEntregas(CanalFalso):
    """Canal que entrega mensagens até o consumidor mandar parar."""

    def __init__(self, quantas: int) -> None:
        super().__init__()
        self.pendentes = quantas
        self.parou = False
        self._callback: Any = None

    def basic_consume(self, *, queue: str, on_message_callback: Any, auto_ack: bool) -> None:
        self._callback = on_message_callback

    def start_consuming(self) -> None:
        while not self.parou and self.pendentes > 0:
            self.pendentes -= 1
            self._callback(self, MetodoFalso(delivery_tag=self.pendentes), None, payload_valido())

    def stop_consuming(self) -> None:
        self.parou = True


class ConexaoComEntregas:
    def __init__(self, canal: CanalComEntregas) -> None:
        self._canal = canal
        self.is_open = True
        self.fechada = 0

    def channel(self) -> CanalComEntregas:
        return self._canal

    def close(self) -> None:
        self.fechada += 1
        self.is_open = False


class InboxContadora:
    def __init__(self) -> None:
        self.registrados = 0

    def registrar(self, evento: NotificacaoClassifier, payload: bytes) -> RegistroInbox:
        self.registrados += 1
        return RegistroInbox(event_id=f"e{self.registrados}", novo=True, tipo="recebido")

    def registrar_rejeitado(self, payload: bytes, erro: str) -> RegistroInbox:  # pragma: no cover
        raise AssertionError("payload válido não deveria ser rejeitado")


def test_limite_encerra_a_execucao_e_nao_reabre_conexao() -> None:
    """O limite para o processo, não só a conexão.

    `stop_consuming()` faz `start_consuming()` retornar SEM erro. Sem a checagem no fim do
    laço de reconexão, `consumir_para_sempre` abriria uma conexão nova e continuaria — o
    limite pararia uma conexão de cada vez e a fila seria drenada em blocos, que é o
    oposto do que ele existe para evitar.
    """
    inbox = InboxContadora()
    canal = CanalComEntregas(quantas=50)
    conexoes: list[ConexaoComEntregas] = []

    def fabrica(_parametros: Any) -> ConexaoComEntregas:
        conexao = ConexaoComEntregas(canal)
        conexoes.append(conexao)
        return conexao

    consumidor = ConsumidorRabbitMQ(
        ConfiguracaoRabbitMQ.from_env(ambiente_rabbit()),
        inbox,
        connection_factory=fabrica,
        limite=3,
    )
    consumidor.consumir_para_sempre()

    assert inbox.registrados == 3, "consumiu além do limite"
    assert len(canal.acks) == 3
    assert len(conexoes) == 1, "reabriu conexão depois de atingir o limite"
    assert canal.pendentes == 47, "as mensagens restantes têm de ficar na fila"
    assert consumidor.atingiu_limite()


def test_sem_limite_o_consumo_segue_ate_a_fila_esvaziar() -> None:
    """Sem limite, nada muda: o comportamento anterior é preservado."""
    inbox = InboxContadora()
    canal = CanalComEntregas(quantas=5)

    consumidor = ConsumidorRabbitMQ(
        ConfiguracaoRabbitMQ.from_env(ambiente_rabbit()),
        inbox,
        connection_factory=lambda _p: ConexaoComEntregas(canal),
        limite=None,
    )
    # Sem limite `consumir_para_sempre` reconectaria eternamente; aqui exercita-se uma
    # conexão só, que é o que o laço faz por vez.
    consumidor._consumir_uma_conexao()

    assert inbox.registrados == 5
    assert consumidor.atingiu_limite() is False


def test_commit_acontece_antes_do_ack() -> None:
    """A ordem importa: ACK antes do commit perderia a mensagem se o commit falhasse.

    O broker descarta a mensagem ao receber o ACK. Se o commit viesse depois e falhasse,
    o evento sumiria da fila sem ter sido gravado em lugar nenhum.
    """
    ordem: list[str] = []

    class CanalQueRegistraOrdem(CanalComEntregas):
        def basic_ack(self, *, delivery_tag: int) -> None:
            ordem.append("ack")
            super().basic_ack(delivery_tag=delivery_tag)

    canal = CanalQueRegistraOrdem(quantas=2)
    consumidor = ConsumidorRabbitMQ(
        ConfiguracaoRabbitMQ.from_env(ambiente_rabbit()),
        InboxContadora(),
        connection_factory=lambda _p: ConexaoComEntregas(canal),
        limite=2,
        ao_confirmar=lambda: ordem.append("commit"),
    )
    consumidor.consumir_para_sempre()

    assert ordem == ["commit", "ack", "commit", "ack"]
