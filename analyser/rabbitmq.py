"""Recepção durável dos eventos RabbitMQ enviados pelo Classifier.

Esta camada é deliberadamente pequena: recebe, valida e registra o payload original em
um inbox local antes de confirmar a mensagem. Ela não altera o banco compartilhado nem
chama o modelo. A adaptação de vários trechos para uma notícia exige uma regra de negócio
explícita para agregar os rótulos, portanto permanece fora do callback da fila.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import pika
from pydantic import ValidationError

from analyser.schemas import NotificacaoClassifier

logger = logging.getLogger(__name__)


class ConfiguracaoRabbitMQInvalida(ValueError):
    """Variável RabbitMQ ausente ou com valor inválido."""


class InboxCorrompida(RuntimeError):
    """O arquivo local de inbox não pode ser lido com segurança."""


def _obrigatoria(valores: Mapping[str, str], nome: str) -> str:
    valor = valores.get(nome, "").strip()
    if not valor:
        raise ConfiguracaoRabbitMQInvalida(f"A variável {nome} é obrigatória.")
    return valor


def _booleano(valores: Mapping[str, str], nome: str, *, padrao: bool) -> bool:
    bruto = valores.get(nome)
    if bruto is None or not bruto.strip():
        return padrao

    valor = bruto.strip().lower()
    if valor in {"1", "true", "yes", "sim"}:
        return True
    if valor in {"0", "false", "no", "nao", "não"}:
        return False
    raise ConfiguracaoRabbitMQInvalida(
        f"A variável {nome} deve ser true/false, 1/0, yes/no ou sim/não."
    )


def _inteiro(valores: Mapping[str, str], nome: str, *, padrao: int, minimo: int) -> int:
    bruto = valores.get(nome)
    if bruto is None or not bruto.strip():
        return padrao
    try:
        valor = int(bruto)
    except ValueError as erro:
        raise ConfiguracaoRabbitMQInvalida(
            f"A variável {nome} deve ser um número inteiro."
        ) from erro
    if valor < minimo:
        raise ConfiguracaoRabbitMQInvalida(
            f"A variável {nome} deve ser maior ou igual a {minimo}."
        )
    return valor


@dataclass(frozen=True, slots=True)
class ConfiguracaoRabbitMQ:
    """Parâmetros de consumo da fila Classifier → Analyser."""

    host: str
    port: int
    usuario: str
    senha: str
    vhost: str
    exchange: str
    exchange_type: str
    fila: str
    routing_key: str
    durable: bool
    use_tls: bool
    heartbeat: int
    prefetch: int
    declare_topology: bool
    reconnect_delay_seconds: int

    @classmethod
    def from_env(cls, valores: Mapping[str, str] | None = None) -> "ConfiguracaoRabbitMQ":
        """Lê o contrato operacional sem registrar segredo em log algum."""
        ambiente: Mapping[str, str] = os.environ if valores is None else valores

        return cls(
            host=_obrigatoria(ambiente, "RABBITMQ_HOST"),
            port=_inteiro(ambiente, "RABBITMQ_PORT", padrao=5672, minimo=1),
            usuario=_obrigatoria(ambiente, "RABBITMQ_NOTIFIER_USER"),
            senha=_obrigatoria(ambiente, "RABBITMQ_NOTIFIER_PASSWORD"),
            vhost=ambiente.get("RABBITMQ_VHOST", "/").strip() or "/",
            exchange=_obrigatoria(ambiente, "RABBITMQ_NOTIFIER_EXCHANGE"),
            exchange_type=_obrigatoria(
                ambiente, "RABBITMQ_NOTIFIER_EXCHANGE_TYPE"
            ),
            fila=_obrigatoria(ambiente, "RABBITMQ_NOTIFIER_QUEUE"),
            routing_key=_obrigatoria(ambiente, "RABBITMQ_NOTIFIER_ROUTING_KEY"),
            durable=_booleano(
                ambiente, "RABBITMQ_NOTIFIER_DURABLE", padrao=True
            ),
            use_tls=_booleano(ambiente, "RABBITMQ_USE_TLS", padrao=False),
            heartbeat=_inteiro(ambiente, "RABBITMQ_HEARTBEAT", padrao=60, minimo=0),
            prefetch=_inteiro(ambiente, "RABBITMQ_PREFETCH", padrao=1, minimo=1),
            declare_topology=_booleano(
                ambiente, "RABBITMQ_DECLARE_TOPOLOGY", padrao=False
            ),
            reconnect_delay_seconds=_inteiro(
                ambiente,
                "RABBITMQ_RECONNECT_DELAY_SECONDS",
                padrao=5,
                minimo=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class RegistroInbox:
    """Resultado de uma tentativa de registro local."""

    event_id: str
    novo: bool
    tipo: str


class DestinoInbox(Protocol):
    """Porta de persistência usada pelo consumidor e substituível nos testes."""

    def registrar(self, evento: NotificacaoClassifier, payload: bytes) -> RegistroInbox: ...

    def registrar_rejeitado(self, payload: bytes, erro: str) -> RegistroInbox: ...


class InboxLocal:
    """Inbox JSON Lines durável e idempotente para uma única instância consumidora.

    JSON Lines permite acrescentar uma mensagem sem reescrever as entradas anteriores. Cada
    escrita recebe ``flush`` e ``fsync`` antes do ACK. Caso o processo caia depois da escrita
    e antes da confirmação, o RabbitMQ pode reenviar a mensagem; o hash do payload impede que
    ela seja registrada duas vezes.
    """

    def __init__(self, diretorio_saida: Path) -> None:
        self.caminho = Path(diretorio_saida) / "inbox_eventos.jsonl"
        self._eventos_vistos = self._carregar_eventos_vistos()

    @staticmethod
    def _id_evento(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _carregar_eventos_vistos(self) -> set[str]:
        if not self.caminho.is_file():
            return set()

        eventos: set[str] = set()
        for numero_linha, linha in enumerate(
            self.caminho.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not linha.strip():
                continue
            try:
                registro = json.loads(linha)
            except json.JSONDecodeError as erro:
                raise InboxCorrompida(
                    f"{self.caminho} possui JSON inválido na linha {numero_linha}."
                ) from erro
            event_id = registro.get("event_id") if isinstance(registro, dict) else None
            if not isinstance(event_id, str) or not event_id:
                raise InboxCorrompida(
                    f"{self.caminho} possui event_id ausente na linha {numero_linha}."
                )
            eventos.add(event_id)
        return eventos

    def _acrescentar(self, registro: dict[str, Any]) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        linha = json.dumps(registro, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.caminho.open("a", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(linha)
            arquivo.flush()
            os.fsync(arquivo.fileno())

    def _registrar(self, *, payload: bytes, tipo: str, dados: dict[str, Any]) -> RegistroInbox:
        event_id = self._id_evento(payload)
        if event_id in self._eventos_vistos:
            return RegistroInbox(event_id=event_id, novo=False, tipo=tipo)

        self._acrescentar(
            {
                "event_id": event_id,
                "tipo": tipo,
                "recebido_em": datetime.now(timezone.utc).isoformat(),
                "payload_sha256": event_id,
                **dados,
            }
        )
        self._eventos_vistos.add(event_id)
        return RegistroInbox(event_id=event_id, novo=True, tipo=tipo)

    def registrar(self, evento: NotificacaoClassifier, payload: bytes) -> RegistroInbox:
        """Registra um evento válido antes de sua confirmação ao broker."""
        return self._registrar(
            payload=payload,
            tipo="recebido",
            dados={
                "article_id": evento.article_id,
                "pipeline_version": evento.pipeline_version,
                "payload": evento.model_dump(mode="json"),
            },
        )

    def registrar_rejeitado(self, payload: bytes, erro: str) -> RegistroInbox:
        """Preserva uma mensagem inválida para auditoria sem deixá-la em loop na fila."""
        return self._registrar(
            payload=payload,
            tipo="rejeitado",
            dados={
                "erro_validacao": erro,
                "payload_original": payload.decode("utf-8", errors="replace"),
            },
        )


class ConsumidorRabbitMQ:
    """Consome uma fila existente e confirma apenas o que foi salvo no inbox."""

    def __init__(
        self,
        configuracao: ConfiguracaoRabbitMQ,
        inbox: DestinoInbox,
        *,
        connection_factory: Callable[[pika.ConnectionParameters], Any] = pika.BlockingConnection,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.configuracao = configuracao
        self.inbox = inbox
        self._connection_factory = connection_factory
        self._sleeper = sleeper

    def _parametros_conexao(self) -> pika.ConnectionParameters:
        cfg = self.configuracao
        ssl_options = None
        if cfg.use_tls:
            contexto = ssl.create_default_context()
            ssl_options = pika.SSLOptions(contexto, cfg.host)

        return pika.ConnectionParameters(
            host=cfg.host,
            port=cfg.port,
            virtual_host=cfg.vhost,
            credentials=pika.PlainCredentials(cfg.usuario, cfg.senha),
            heartbeat=cfg.heartbeat,
            blocked_connection_timeout=30,
            connection_attempts=3,
            retry_delay=3,
            ssl_options=ssl_options,
        )

    @staticmethod
    def _fechar(connection: Any) -> None:
        if connection is not None and getattr(connection, "is_open", False):
            connection.close()

    def verificar_conexao(self) -> None:
        """Abre e fecha uma conexão autenticada, sem consumir nem alterar topologia."""
        connection = None
        try:
            connection = self._connection_factory(self._parametros_conexao())
        finally:
            self._fechar(connection)

    def _preparar_canal(self, canal: Any) -> None:
        cfg = self.configuracao
        if cfg.declare_topology:
            canal.exchange_declare(
                exchange=cfg.exchange,
                exchange_type=cfg.exchange_type,
                durable=cfg.durable,
            )
            canal.queue_declare(queue=cfg.fila, durable=cfg.durable)
            canal.queue_bind(
                queue=cfg.fila,
                exchange=cfg.exchange,
                routing_key=cfg.routing_key,
            )
        canal.basic_qos(prefetch_count=cfg.prefetch)

    def _processar_entrega(self, canal: Any, metodo: Any, body: bytes) -> None:
        """Valida, registra e só então envia ACK para a mensagem recebida."""
        try:
            evento = NotificacaoClassifier.model_validate_json(body)
        except ValidationError as erro:
            try:
                registro = self.inbox.registrar_rejeitado(body, str(erro))
            except Exception:
                logger.exception("Não foi possível preservar uma mensagem inválida no inbox")
                canal.basic_nack(delivery_tag=metodo.delivery_tag, requeue=True)
                return
            logger.error(
                "Mensagem inválida preservada no inbox (event_id=%s, nova=%s)",
                registro.event_id,
                registro.novo,
            )
            canal.basic_ack(delivery_tag=metodo.delivery_tag)
            return

        try:
            registro = self.inbox.registrar(evento, body)
        except Exception:
            logger.exception("Falha ao gravar evento válido no inbox; mensagem será reenfileirada")
            canal.basic_nack(delivery_tag=metodo.delivery_tag, requeue=True)
            return

        logger.info(
            "Evento do Classifier registrado (event_id=%s, novo=%s, article_id=%s)",
            registro.event_id,
            registro.novo,
            evento.article_id,
        )
        canal.basic_ack(delivery_tag=metodo.delivery_tag)

    def _consumir_uma_conexao(self) -> None:
        connection = None
        try:
            connection = self._connection_factory(self._parametros_conexao())
            canal = connection.channel()
            self._preparar_canal(canal)
            canal.basic_consume(
                queue=self.configuracao.fila,
                on_message_callback=lambda ch, method, properties, body: self._processar_entrega(
                    ch, method, body
                ),
                auto_ack=False,
            )
            logger.info("Consumindo a fila RabbitMQ %s", self.configuracao.fila)
            canal.start_consuming()
        finally:
            self._fechar(connection)

    def consumir_para_sempre(self) -> None:
        """Mantém o consumidor ativo e reconecta após falhas de transporte."""
        while True:
            try:
                self._consumir_uma_conexao()
            except KeyboardInterrupt:
                logger.info("Consumidor RabbitMQ interrompido pelo operador.")
                return
            except pika.exceptions.AMQPError:
                logger.exception(
                    "Conexão RabbitMQ encerrada; nova tentativa em %s segundo(s).",
                    self.configuracao.reconnect_delay_seconds,
                )
                self._sleeper(self.configuracao.reconnect_delay_seconds)

