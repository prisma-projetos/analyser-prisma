"""CLI do consumidor RabbitMQ do Prisma Analyser.

Uso:
    python -m analyser.consumidor_rabbitmq --verificar-conexao
    python -m analyser.consumidor_rabbitmq --saida saida
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from analyser.config import carregar_dotenv
from analyser.rabbitmq import (
    ConfiguracaoRabbitMQ,
    ConfiguracaoRabbitMQInvalida,
    ConsumidorRabbitMQ,
    InboxCorrompida,
    InboxLocal,
)
from analyser.saida import console


def montar_parser() -> argparse.ArgumentParser:
    """Argumentos exclusivos da recepção de eventos do Classifier."""
    parser = argparse.ArgumentParser(
        prog="analyser-rabbitmq",
        description=(
            "Recebe notificações do Classifier e as preserva em um inbox JSON local."
        ),
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=Path("saida"),
        help="diretório que receberá inbox_eventos.jsonl",
    )
    parser.add_argument(
        "--verificar-conexao",
        action="store_true",
        help="conecta e fecha sem consumir mensagens nem declarar topologia",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="nível de detalhe dos logs (padrão: INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Valida a configuração e executa o modo solicitado."""
    args = montar_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    carregar_dotenv(Path(".env"))

    try:
        configuracao = ConfiguracaoRabbitMQ.from_env()
        inbox = InboxLocal(args.saida)
    except (ConfiguracaoRabbitMQInvalida, InboxCorrompida) as erro:
        console.print(f"[red]Configuração RabbitMQ rejeitada:[/red] {erro}")
        return 2

    consumidor = ConsumidorRabbitMQ(configuracao, inbox)

    if args.verificar_conexao:
        try:
            consumidor.verificar_conexao()
        except Exception as erro:
            console.print(f"[red]Falha ao conectar ao RabbitMQ:[/red] {erro}")
            return 2
        console.print("[green]Conexão RabbitMQ autenticada com sucesso.[/green]")
        return 0

    console.print(
        "[cyan]Consumindo notificações do Classifier.[/cyan] "
        "Use Ctrl+C para encerrar; o ACK ocorre somente após a gravação no inbox local."
    )
    consumidor.consumir_para_sempre()
    return 0


if __name__ == "__main__":
    sys.exit(main())

