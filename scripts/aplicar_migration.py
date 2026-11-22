"""Aplica a migration 001 do Analyser com verificação de estado.

    .venv/Scripts/python.exe -m scripts.aplicar_migration
    .venv/Scripts/python.exe -m scripts.aplicar_migration --schema public
    .venv/Scripts/python.exe -m scripts.aplicar_migration --schema public --check-only

Sem ``--schema``, o script cria e remove ``analyser_teste`` para provar a DDL sem deixar
rastro. Em um schema real, aplica a migration somente quando nenhuma das quatro tabelas do
núcleo existe; se todas já existirem e o contrato mínimo estiver íntegro, retorna sucesso
sem reaplicar. Estados parciais ou incompatíveis são recusados explicitamente.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql as psql

from analyser.config import carregar_dotenv

RAIZ_PROJETO = Path(__file__).resolve().parents[1]
MIGRATION = RAIZ_PROJETO / "migrations" / "001_analyser.sql"

ESPERADAS = (
    "analyser_inbox_evento",
    "analyser_item",
    "analyser_execucao",
    "analyser_panorama",
)

COLUNAS_ESPERADAS = {
    "analyser_inbox_evento": {
        "evento_id",
        "payload_hash",
        "article_id",
        "pipeline_version",
        "payload_json",
        "status",
        "tentativas",
        "recebido_em",
        "processado_em",
        "erro",
    },
    "analyser_item": {
        "item_id",
        "evento_id",
        "article_id",
        "entity_type",
        "nr_titulo_eleitoral_candidato",
        "nr_partido",
        "ator_nome",
        "id_midia",
        "link_noticia",
        "data_publicacao",
        "janela_mes",
        "rotulo_definitivo",
        "regra_agregacao_versao",
        "trechos_json",
        "conteudo_hash",
        "resumo",
        "temas_json",
        "modelo",
        "hash_prompt_resumo",
        "resumido_em",
        "status",
        "criado_em",
        "atualizado_em",
    },
    "analyser_execucao": {
        "execucao_id",
        "numero_execucao",
        "status",
        "capacidade_lote",
        "quantidade_eventos",
        "quantidade_itens",
        "quantidade_resumos",
        "quantidade_panoramas",
        "modelo",
        "iniciado_em",
        "finalizado_em",
        "auditoria_json",
        "erro",
    },
    "analyser_panorama": {
        "panorama_id",
        "execucao_id",
        "entity_type",
        "nr_titulo_eleitoral_candidato",
        "nr_partido",
        "ator_nome",
        "janela_mes",
        "eixo",
        "versao",
        "formato",
        "total_itens",
        "texto",
        "temas_json",
        "fontes_json",
        "avisos_json",
        "quantidade_lotes",
        "modelo",
        "hashes_prompts_json",
        "criado_em",
        "publicado_em",
    },
}

RESTRICOES_ESPERADAS = {
    "analyser_inbox_evento": {
        "analyser_inbox_evento_pkey",
        "analyser_inbox_evento_payload_hash_key",
        "analyser_inbox_evento_status_check",
    },
    "analyser_item": {
        "analyser_item_pkey",
        "analyser_item_evento_id_fkey",
        "analyser_item_entity_type_check",
        "analyser_item_rotulo_definitivo_check",
        "ck_analyser_item_uma_entidade",
    },
    "analyser_execucao": {
        "analyser_execucao_pkey",
        "analyser_execucao_status_check",
    },
    "analyser_panorama": {
        "analyser_panorama_pkey",
        "analyser_panorama_execucao_id_fkey",
        "analyser_panorama_entity_type_check",
        "analyser_panorama_eixo_check",
        "analyser_panorama_versao_check",
        "ck_analyser_panorama_uma_entidade",
    },
}

INDICES_ESPERADOS = {
    "analyser_inbox_evento": {
        "ix_analyser_inbox_artigo",
        "ix_analyser_inbox_pendente",
    },
    "analyser_item": {
        "ix_analyser_item_janela",
        "ix_analyser_item_artigo",
    },
    "analyser_execucao": set(),
    "analyser_panorama": {"uk_analyser_panorama_versao"},
}

EstadoSchema = Literal["vazio", "completo"]


class SchemaIncompativel(RuntimeError):
    """Indica que a migration não pode prosseguir sem intervenção consciente."""


def _classificar_estado_tabelas(tabelas: Iterable[str]) -> EstadoSchema:
    """Distingue um núcleo vazio, completo ou parcialmente criado."""
    esperadas = set(ESPERADAS)
    presentes = esperadas.intersection(tabelas)
    if not presentes:
        return "vazio"
    if presentes == esperadas:
        return "completo"

    faltantes = sorted(esperadas - presentes)
    raise SchemaIncompativel(
        "schema parcial: tabelas presentes "
        f"{sorted(presentes)}; tabelas ausentes {faltantes}. "
        "A migration 001 não foi executada."
    )


def _agrupar_por_tabela(linhas: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    agrupadas = {tabela: set() for tabela in ESPERADAS}
    for tabela, nome in linhas:
        if tabela in agrupadas:
            agrupadas[tabela].add(nome)
    return agrupadas


def _problemas_catalogo(
    colunas: Mapping[str, set[str]],
    restricoes: Mapping[str, set[str]],
    indices: Mapping[str, set[str]],
) -> list[str]:
    """Compara o catálogo real com o contrato mínimo da migration 001."""
    problemas: list[str] = []
    categorias = (
        ("colunas", COLUNAS_ESPERADAS, colunas),
        ("restrições", RESTRICOES_ESPERADAS, restricoes),
        ("índices", INDICES_ESPERADOS, indices),
    )
    for categoria, esperado_por_tabela, real_por_tabela in categorias:
        for tabela, esperados in esperado_por_tabela.items():
            faltantes = sorted(esperados - real_por_tabela.get(tabela, set()))
            if faltantes:
                problemas.append(f"{tabela}: {categoria} ausentes: {', '.join(faltantes)}")
    return problemas


def _schema_existe(con: psycopg.Connection, schema: str) -> bool:
    linha = con.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
        (schema,),
    ).fetchone()
    return bool(linha and linha[0])


def _ler_e_validar_catalogo(
    con: psycopg.Connection, schema: str
) -> tuple[EstadoSchema, dict[str, set[str]]]:
    """Lê somente metadados e recusa um núcleo parcial ou estruturalmente incompleto."""
    tabelas = {
        tabela
        for (tabela,) in con.execute(
            """SELECT table_name
               FROM information_schema.tables
               WHERE table_schema = %s AND table_name = ANY(%s)
               ORDER BY table_name""",
            (schema, list(ESPERADAS)),
        ).fetchall()
    }
    estado = _classificar_estado_tabelas(tabelas)
    if estado == "vazio":
        return estado, {tabela: set() for tabela in ESPERADAS}

    colunas = _agrupar_por_tabela(
        con.execute(
            """SELECT table_name, column_name
               FROM information_schema.columns
               WHERE table_schema = %s AND table_name = ANY(%s)""",
            (schema, list(ESPERADAS)),
        ).fetchall()
    )
    restricoes = _agrupar_por_tabela(
        con.execute(
            """SELECT table_name, constraint_name
               FROM information_schema.table_constraints
               WHERE table_schema = %s AND table_name = ANY(%s)""",
            (schema, list(ESPERADAS)),
        ).fetchall()
    )
    indices = _agrupar_por_tabela(
        con.execute(
            """SELECT tablename, indexname
               FROM pg_indexes
               WHERE schemaname = %s AND tablename = ANY(%s)""",
            (schema, list(ESPERADAS)),
        ).fetchall()
    )

    problemas = _problemas_catalogo(colunas, restricoes, indices)
    if problemas:
        detalhes = "\n - ".join(problemas)
        raise SchemaIncompativel(
            "as quatro tabelas existem, mas o contrato da migration 001 está incompleto:"
            f"\n - {detalhes}\nNada foi alterado."
        )
    return estado, indices


def _provar_indice_unico(con: psycopg.Connection, schema: str) -> None:
    """Prova no schema descartável que duas versões iguais são recusadas."""
    comando = psql.SQL(
        """
        INSERT INTO {}.analyser_panorama (
            entity_type, nr_titulo_eleitoral_candidato, ator_nome, janela_mes, eixo,
            versao, formato, total_itens, texto, temas_json, fontes_json,
            quantidade_lotes
        ) VALUES ('politico','122418060191','FULANO','2026-08','negativo',
                  1,'negativo',1,'texto','[]'::jsonb,'[]'::jsonb,1)
        """
    ).format(psql.Identifier(schema))

    try:
        # Como já existe uma transação, este bloco usa um SAVEPOINT. A violação esperada
        # desfaz somente os inserts de prova e preserva a DDL que ainda será validada.
        with con.transaction():
            con.execute(comando)
            con.execute(comando)
    except psycopg.errors.UniqueViolation:
        print("   [ok] o índice barrou a segunda versão igual, como deve")
        return
    raise SchemaIncompativel("duas versões iguais do mesmo panorama foram aceitas")


def _imprimir_catalogo(indices: Mapping[str, set[str]]) -> None:
    print(f"\ntabelas do núcleo ({len(ESPERADAS)}): {', '.join(sorted(ESPERADAS))}")
    nomes = sorted(
        (tabela, indice)
        for tabela, indices_da_tabela in indices.items()
        for indice in indices_da_tabela
    )
    print(f"\níndices do núcleo ({len(nomes)}):")
    for tabela, indice in nomes:
        print(f"   {tabela:<24} {indice}")


def _abrir_conexao(dsn: str, *, somente_leitura: bool) -> psycopg.Connection:
    if somente_leitura:
        return psycopg.connect(dsn, options="-c default_transaction_read_only=on")
    return psycopg.connect(dsn)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="aplicar-migration")
    parser.add_argument(
        "--schema",
        default="analyser_teste",
        help="schema alvo; o padrão é descartável e removido ao final",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verifica a migration 001 em uma transação somente leitura",
    )
    args = parser.parse_args()
    descartavel = args.schema == "analyser_teste"

    if args.check_only and descartavel:
        print(
            "Em --check-only, informe explicitamente o schema existente com --schema.",
            file=sys.stderr,
        )
        return 2

    carregar_dotenv(RAIZ_PROJETO / ".env")
    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    try:
        with _abrir_conexao(dsn, somente_leitura=args.check_only) as con:
            if not args.check_only:
                # Serializa Jobs concorrentes. O segundo espera o primeiro concluir e então
                # reavalia o catálogo, em vez de criar as mesmas tabelas em paralelo.
                con.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"prisma-analyse:migration:{args.schema}",),
                )

            if descartavel:
                con.execute(
                    psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psql.Identifier(args.schema)
                    )
                )
                con.execute(
                    psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(args.schema))
                )
            elif not _schema_existe(con, args.schema):
                raise SchemaIncompativel(f"o schema '{args.schema}' não existe")

            con.execute(
                psql.SQL("SET LOCAL search_path TO {}").format(psql.Identifier(args.schema))
            )
            estado, indices = _ler_e_validar_catalogo(con, args.schema)

            if estado == "completo":
                con.rollback()
                print(
                    f"migration 001 já aplicada em '{args.schema}'; "
                    "contrato compatível, nada a fazer."
                )
                _imprimir_catalogo(indices)
                return 0

            if args.check_only:
                raise SchemaIncompativel(
                    f"nenhuma tabela do núcleo existe em '{args.schema}'; "
                    "a migration 001 ainda não foi aplicada"
                )

            con.execute(MIGRATION.read_text(encoding="utf-8"))
            estado, indices = _ler_e_validar_catalogo(con, args.schema)
            if estado != "completo":  # proteção para futuras mudanças na classificação
                raise SchemaIncompativel("a migration terminou sem criar o núcleo completo")

            print(f"migration 001 aplicada em '{args.schema}'")
            _imprimir_catalogo(indices)

            if descartavel:
                print("\nprovando as restrições:")
                _provar_indice_unico(con, args.schema)
                con.execute(
                    psql.SQL("DROP SCHEMA {} CASCADE").format(
                        psql.Identifier(args.schema)
                    )
                )
                con.commit()
                print(f"\nschema '{args.schema}' removido — nada ficou no banco")
            else:
                con.commit()
                print(f"\ntabelas criadas em '{args.schema}' de verdade")

    except SchemaIncompativel as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
