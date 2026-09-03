"""Processa eventos já ingeridos e consolida as janelas pendentes.

    .venv/Scripts/python.exe -m scripts.processar --eventos 50
    .venv/Scripts/python.exe -m scripts.processar --consolidar 20
    .venv/Scripts/python.exe -m scripts.processar --eventos 50 --consolidar 20

Roda DEPOIS de `scripts.drenar_fila`, que só traz a mensagem para a inbox. A separação é o
que permite drenar a fila em segundos e pagar o modelo depois, no próprio ritmo.

Interromper no meio é seguro: o commit é por evento e por janela, e o `status` de cada item
diz exatamente onde parou. Retomar é rodar de novo.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

from analyser.config import Config, carregar_dotenv
from analyser.modelos import ModeloAPI, ModeloFake
from analyser.worker import ResultadoWorker, Worker


def _relatar(titulo: str, r: ResultadoWorker) -> None:
    print(f"\n── {titulo}")
    for campo in (
        "eventos_processados",
        "itens_criados",
        "itens_ja_existentes",
        "resumos_gerados",
        "janelas_consolidadas",
        "panoramas_gravados",
    ):
        valor = getattr(r, campo)
        if valor:
            print(f"   {campo.replace('_', ' '):<24} {valor}")
    if r.avisos:
        print(f"   avisos ({len(r.avisos)}):")
        for aviso in r.avisos[:10]:
            print(f"      {aviso}")
        if len(r.avisos) > 10:
            print(f"      ... e mais {len(r.avisos) - 10}")
    if r.erros:
        print(f"   ERROS ({len(r.erros)}):")
        for erro in r.erros[:10]:
            print(f"      {erro}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="processar")
    parser.add_argument("--eventos", type=int, default=0, help="máximo de eventos a processar")
    parser.add_argument("--consolidar", type=int, default=0, help="máximo de janelas")
    parser.add_argument("--modelo", choices=("fake", "api"), default="api")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    if not args.eventos and not args.consolidar:
        parser.error("informe --eventos e/ou --consolidar")

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    carregar_dotenv(Path(".env"))

    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    # `entrada` não é usada por este caminho — o worker lê da inbox, não de arquivo —,
    # mas `Config` a exige. Aponta para o próprio script, que existe.
    config = Config(entrada=Path(__file__), saida=Path("saida"), modelo=args.modelo)
    modelo = ModeloAPI() if args.modelo == "api" else ModeloFake()

    with psycopg.connect(dsn) as conexao:
        worker = Worker(conexao, modelo, config)

        if args.eventos:
            print(f"processando até {args.eventos} eventos...")
            _relatar("eventos", worker.processar_eventos(limite=args.eventos))

        if args.consolidar:
            print(f"\nconsolidando até {args.consolidar} janelas...")
            _relatar("janelas", worker.consolidar_janelas(limite=args.consolidar))

        pendentes = conexao.execute(
            "SELECT count(*) FROM analyser_inbox_evento WHERE status = 'recebido'"
        ).fetchone()[0]
        sujas = conexao.execute(
            "SELECT count(DISTINCT janela_mes) FROM analyser_item WHERE status = 'resumido'"
        ).fetchone()[0]

    print(f"\nrestam: {pendentes} eventos por processar, {sujas} janelas por consolidar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
