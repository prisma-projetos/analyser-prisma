"""Confere, somente por leitura, o schema real das tabelas que o Analyser consulta.

Existe porque a DDL que foi para os PDFs saiu de `criar_tabelas_iniciais.sql`, que é o script
**inicial** do banco, não o estado cumulativo: `entidade_matching` e
`classifier_analyser_outbox` nem constam nele, embora existam no banco implantado. Escrever
migration sem conferir seria chutar.

Não altera nada. Só lê `information_schema`.

    .venv/Scripts/python.exe -m scripts.conferir_schema
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

from analyser.config import carregar_dotenv

#: Tabelas que o enriquecimento vai consultar, mais as duas que a auditoria cita e que não
#: estão no SQL inicial — é justamente sobre essas que a dúvida existe.
ALVOS = (
    "tabela_classificacao",
    "tabela_midia",
    "entidade_matching",
    "classifier_analyser_outbox",
    "tabela_politico",
    "tabela_partido",
)

#: Colunas cuja ausência muda decisão de projeto, e por quê.
ESPERADO_AUSENTE = {
    ("tabela_classificacao", "paragraph_index"): (
        "confirma que o índice do parágrafo só existe na mensagem; por isso ele é gravado "
        "em analyser_item.trechos_json"
    ),
    ("tabela_classificacao", "titulo"): (
        "confirma que o título não existe no prisma-dev; por isso o Analyser usa título "
        "sintético e não persiste a coluna"
    ),
}


def main() -> int:
    # O console do Windows abre em cp1252 e quebra em acento e traço de caixa. Um script de
    # diagnóstico não pode falhar por causa da fonte do terminal.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    carregar_dotenv(Path(".env"))
    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente. Ver Passo 0 do plano.", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conexao:
        for tabela in ALVOS:
            linhas = conexao.execute(
                """
                SELECT column_name, data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (tabela,),
            ).fetchall()

            if not linhas:
                print(f"\n── {tabela}: NÃO EXISTE no schema public")
                continue

            colunas = {c for c, _, _ in linhas}
            print(f"\n── {tabela} ({len(linhas)} colunas)")
            for coluna, tipo, nulo in linhas:
                marca = "NULL" if nulo == "YES" else "NOT NULL"
                print(f"   {coluna:<34} {tipo:<28} {marca}")

            for (alvo, coluna), motivo in ESPERADO_AUSENTE.items():
                if alvo != tabela:
                    continue
                if coluna in colunas:
                    print(f"   [!] {coluna} EXISTE — o plano assume que não. Revisar: {motivo}")
                else:
                    print(f"   [ok] sem {coluna} — {motivo}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
