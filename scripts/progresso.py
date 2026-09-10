"""Mostra quanto já foi processado e quanto falta, sem tocar em nada.

    .venv/Scripts/python.exe -m scripts.progresso
    .venv/Scripts/python.exe -m scripts.progresso --janelas

Só LÊ. Existe porque `scripts.processar` só imprime o relatório no fim: durante uma
corrida de horas não há como saber onde ela está sem consultar o banco na mão.

A contagem da execução em curso sai dos timestamps das linhas (`processado_em`,
`resumido_em`), e não dos contadores de `analyser_execucao` — esses só são gravados
quando a execução fecha, e ficam em zero enquanto ela roda.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from analyser.config import carregar_dotenv

INBOX = """
    SELECT count(*) FILTER (WHERE status = 'processado') AS processados,
           count(*) FILTER (WHERE status = 'recebido')   AS faltam,
           count(*) FILTER (WHERE status = 'erro')       AS erros
      FROM analyser_inbox_evento"""

ULTIMA = """
    SELECT numero_execucao, status, iniciado_em
      FROM analyser_execucao
     ORDER BY numero_execucao DESC
     LIMIT 1"""


def _duracao(minutos: float) -> str:
    """Minutos em `1h23` ou `47min`, para caber numa linha."""
    if minutos < 60:
        return f"{minutos:.0f}min"
    return f"{int(minutos // 60)}h{int(minutos % 60):02d}"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="progresso")
    parser.add_argument(
        "--janelas", action="store_true", help="também conta as janelas por consolidar"
    )
    args = parser.parse_args(argv)

    carregar_dotenv(Path(".env"))
    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conexao:
        processados, faltam, erros = conexao.execute(INBOX).fetchone()
        numero, status, iniciado_em = conexao.execute(ULTIMA).fetchone()

        eventos, = conexao.execute(
            "SELECT count(*) FROM analyser_inbox_evento WHERE processado_em >= %s",
            (iniciado_em,),
        ).fetchone()
        resumos, = conexao.execute(
            "SELECT count(*) FROM analyser_item WHERE resumido_em >= %s",
            (iniciado_em,),
        ).fetchone()

        minutos = (datetime.now(timezone.utc) - iniciado_em).total_seconds() / 60

        print(f"\nexecução #{numero} — {status}")
        print(f"   nesta corrida       {eventos} eventos, {resumos} resumos "
              f"em {_duracao(minutos)}")
        if eventos and minutos:
            print(f"   ritmo               {resumos / minutos:.1f} resumos/min")

        print("\ninbox")
        print(f"   processados         {processados}")
        print(f"   faltam              {faltam}")
        print(f"   em erro             {erros}"
              + ("   <- presos: nada os reseleciona" if erros else ""))

        if faltam and eventos and minutos:
            print(f"\n   ETA dos eventos     ~{_duracao(faltam / (eventos / minutos))}")

        if args.janelas:
            pendentes, = conexao.execute(
                "SELECT count(*) FROM analyser_item WHERE status = 'resumido'"
            ).fetchone()
            print(f"\n   itens resumidos aguardando consolidação   {pendentes}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
