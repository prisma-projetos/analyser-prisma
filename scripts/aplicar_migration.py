"""Aplica uma migration do Analyser, por padrão num schema descartável.

    .venv/Scripts/python.exe -m scripts.aplicar_migration                 # testa e descarta
    .venv/Scripts/python.exe -m scripts.aplicar_migration --schema public # vale de verdade

O padrão é o schema de teste porque criar tabela em `public` de um banco compartilhado é
mudança que afeta outras pessoas. Rodar sem argumento cria, confere, prova as restrições e
apaga tudo — não deixa rastro.

Nenhuma tabela existente é tocada em modo algum.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

from analyser.config import carregar_dotenv

MIGRATION = Path("migrations/001_analyser.sql")
ESPERADAS = (
    "analyser_inbox_evento",
    "analyser_item",
    "analyser_execucao",
    "analyser_panorama",
)


def _provar_indice_unico(con: psycopg.Connection, schema: str) -> None:
    """Prova que o índice com COALESCE barra a segunda versão igual.

    Sem o COALESCE, os dois inserts abaixo passariam: `nr_partido` é nulo nos dois, e no
    padrão do PostgreSQL dois NULL são distintos num UNIQUE. É a mesma armadilha que já
    existe em `tabela_classificacao`, e a razão de o índice ser por expressão.
    """
    linha = """
        INSERT INTO {s}.analyser_panorama (
            entity_type, nr_titulo_eleitoral_candidato, ator_nome, janela_mes, eixo,
            versao, formato, total_itens, texto, temas_json, fontes_json,
            quantidade_lotes
        ) VALUES ('politico','122418060191','FULANO','2026-08','negativo',
                  1,'negativo',1,'texto','[]'::jsonb,'[]'::jsonb,1)
    """.format(s=schema)

    con.execute(linha)
    try:
        con.execute(linha)
    except psycopg.errors.UniqueViolation:
        con.rollback()
        print("   [ok] o índice barrou a segunda versão igual, como deve")
        return
    con.rollback()
    raise SystemExit("FALHOU: duas versões iguais do mesmo panorama foram aceitas")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="aplicar-migration")
    parser.add_argument(
        "--schema",
        default="analyser_teste",
        help="schema alvo; o padrão é descartável e apagado ao final",
    )
    args = parser.parse_args()
    descartavel = args.schema == "analyser_teste"

    carregar_dotenv(Path(".env"))
    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    sql = MIGRATION.read_text(encoding="utf-8")

    with psycopg.connect(dsn) as con:
        if descartavel:
            con.execute(f"DROP SCHEMA IF EXISTS {args.schema} CASCADE")
            con.execute(f"CREATE SCHEMA {args.schema}")
        con.execute(f"SET search_path TO {args.schema}")
        con.execute(sql)
        con.commit()
        print(f"migration aplicada em '{args.schema}'")

        # Filtra pelo prefixo: em `public` as tabelas do Classifier apareceriam junto e a
        # saída ficaria ilegível justamente onde a conferência importa mais.
        criadas = [
            t
            for (t,) in con.execute(
                """SELECT table_name FROM information_schema.tables
                    WHERE table_schema = %s AND table_name LIKE 'analyser\\_%%'
                    ORDER BY table_name""",
                (args.schema,),
            ).fetchall()
        ]
        print(f"\ntabelas ({len(criadas)}): {', '.join(criadas)}")
        faltando = set(ESPERADAS) - set(criadas)
        if faltando:
            raise SystemExit(f"FALHOU: não foram criadas: {sorted(faltando)}")

        indices = con.execute(
            """SELECT tablename, indexname FROM pg_indexes
                WHERE schemaname = %s ORDER BY tablename, indexname""",
            (args.schema,),
        ).fetchall()
        print(f"\nindices ({len(indices)}):")
        for tabela, indice in indices:
            print(f"   {tabela:<24} {indice}")

        print("\nprovando as restrições:")
        _provar_indice_unico(con, args.schema)

        if descartavel:
            con.execute(f"DROP SCHEMA {args.schema} CASCADE")
            con.commit()
            print(f"\nschema '{args.schema}' apagado — nada ficou no banco")
        else:
            con.commit()
            print(f"\ntabelas criadas em '{args.schema}' de verdade")

    return 0


if __name__ == "__main__":
    sys.exit(main())
