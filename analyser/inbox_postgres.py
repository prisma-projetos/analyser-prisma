"""Inbox durável em PostgreSQL, no lugar do arquivo JSON Lines.

Implementa `DestinoInbox` (`analyser/rabbitmq.py`), a porta que o consumidor já recebe por
parâmetro. O consumidor não sabe qual dos dois está do outro lado — é o que permite trocar
arquivo por banco sem tocar na lógica de ACK, reconexão e reentrega.

Por que trocar o JSON Lines
---------------------------
O inbox em arquivo serve a uma instância só. Duas réplicas gravando no mesmo `.jsonl`
corromperiam o arquivo, e a deduplicação vive num `set` em memória, reconstruído lendo o
arquivo inteiro na inicialização. Aqui a deduplicação é a restrição `UNIQUE` de
`payload_hash`: o banco recusa a segunda inserção, sem que o processo precise lembrar de
nada.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from analyser.rabbitmq import RegistroInbox
from analyser.schemas import NotificacaoClassifier


class InboxPostgres:
    """Grava cada mensagem recebida em `analyser_inbox_evento`.

    Recebe a conexão pronta em vez de abri-la: quem consome precisa que a gravação do
    evento e o processamento dele caibam na MESMA transação, e o ACK ao RabbitMQ só ocorra
    depois do commit. Uma conexão própria aqui dentro tornaria isso impossível.
    """

    def __init__(self, conexao: psycopg.Connection) -> None:
        self.conexao = conexao

    @staticmethod
    def _id_evento(payload: bytes) -> str:
        """SHA-256 do corpo da mensagem.

        A mensagem do Classifier não traz identificador próprio, então o hash faz esse
        papel. É solução de contorno, não escolha: um id vindo do outbox de origem seria
        mais robusto, porque dois payloads semanticamente iguais com espaçamento diferente
        geram hashes diferentes.
        """
        return hashlib.sha256(payload).hexdigest()

    def _inserir(
        self,
        *,
        payload: bytes,
        tipo: str,
        article_id: int,
        pipeline_version: str,
        payload_json: Any,
        status: str,
        erro: str | None = None,
    ) -> RegistroInbox:
        event_id = self._id_evento(payload)

        # ON CONFLICT DO NOTHING em vez de consultar antes: entre o SELECT e o INSERT
        # caberia outra réplica inserindo o mesmo evento. Deixar o banco decidir é o que
        # torna a deduplicação correta com mais de um consumidor.
        cur = self.conexao.execute(
            """
            INSERT INTO analyser_inbox_evento (
                payload_hash, article_id, pipeline_version, payload_json, status, erro
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (payload_hash) DO NOTHING
            RETURNING evento_id
            """,
            (event_id, article_id, pipeline_version, Jsonb(payload_json), status, erro),
        )
        linha = cur.fetchone()
        return RegistroInbox(event_id=event_id, novo=linha is not None, tipo=tipo)

    def registrar(
        self, evento: NotificacaoClassifier, payload: bytes
    ) -> RegistroInbox:
        """Registra um evento válido antes de sua confirmação ao broker."""
        return self._inserir(
            payload=payload,
            tipo="recebido",
            article_id=evento.article_id,
            pipeline_version=evento.pipeline_version,
            payload_json=evento.model_dump(mode="json"),
            status="recebido",
        )

    def registrar_rejeitado(self, payload: bytes, erro: str) -> RegistroInbox:
        """Preserva uma mensagem inválida sem deixá-la em loop na fila.

        O payload não passou no schema, então `article_id` e `pipeline_version` podem não
        existir. Tenta extraí-los do JSON cru para a linha continuar consultável; se nem
        isso der, grava os sentinelas e deixa a mensagem original em `payload_json`.
        """
        article_id, pipeline_version, cru = -1, "desconhecida", None
        try:
            cru = json.loads(payload.decode("utf-8", errors="replace"))
            if isinstance(cru, dict):
                article_id = int(cru.get("article_id", -1))
                pipeline_version = str(cru.get("pipeline_version", "desconhecida"))
        except (ValueError, TypeError):
            pass

        return self._inserir(
            payload=payload,
            tipo="rejeitado",
            article_id=article_id,
            pipeline_version=pipeline_version,
            payload_json={"original": cru, "bruto": payload.decode("utf-8", "replace")},
            status="rejeitado",
            erro=erro,
        )


__all__ = ["InboxPostgres", "errors"]
