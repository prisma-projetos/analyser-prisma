"""Consome um lote limitado da fila do Classifier para `analyser_inbox_evento`.

    .venv/Scripts/python.exe -m scripts.drenar_fila --limite 200

Só INGERE: valida a mensagem e a grava na inbox. Não enriquece, não chama o modelo e não
gera panorama — isso é o passo seguinte. A separação é deliberada: a ingestão custa
milissegundos por mensagem, enquanto o resumo custa ~15s, e misturar as duas tornaria
impossível trazer o histórico sem pagar horas de modelo antes de conferir qualquer coisa.

Por que existe limite
---------------------
A fila acumulou milhares de eventos enquanto ninguém consumia. Drenar tudo de uma vez, com
o processamento ligado, custaria mais de vinte horas de modelo. Com o limite dá para trazer
um lote, olhar o que chegou e decidir.

ATENÇÃO: mensagem consumida e confirmada SAI da fila. Ela fica durável em
`analyser_inbox_evento` (é o que o commit antes do ACK garante), mas não volta ao RabbitMQ.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

from analyser.config import carregar_dotenv
from analyser.inbox_postgres import InboxPostgres
from analyser.rabbitmq import ConfiguracaoRabbitMQ, ConsumidorRabbitMQ


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="drenar-fila")
    parser.add_argument("--limite", type=int, default=200, help="máximo de mensagens")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    carregar_dotenv(Path(".env"))

    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    configuracao = ConfiguracaoRabbitMQ.from_env()

    with psycopg.connect(dsn) as conexao:
        antes = conexao.execute("SELECT count(*) FROM analyser_inbox_evento").fetchone()[0]
        inbox = InboxPostgres(conexao)

        # O commit é o gancho do ACK: cada mensagem só é confirmada ao broker depois de a
        # linha estar durável no banco. Confirmar antes perderia a mensagem se o commit
        # falhasse, porque o RabbitMQ já a teria descartado.
        consumidor = ConsumidorRabbitMQ(
            configuracao,
            inbox,
            limite=args.limite,
            ao_confirmar=conexao.commit,
        )

        print(f"drenando até {args.limite} mensagens de {configuracao.fila!r}...")
        try:
            consumidor.consumir_para_sempre()
        except KeyboardInterrupt:
            print("\ninterrompido pelo operador")

        depois = conexao.execute("SELECT count(*) FROM analyser_inbox_evento").fetchone()[0]

    print(f"\ninbox: {antes} -> {depois} linhas ({depois - antes} novas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
