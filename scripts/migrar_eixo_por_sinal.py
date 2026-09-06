"""Migra os itens da regra `maioria-v1` para `eixo-por-sinal-v2`.

    .venv/Scripts/python.exe -m scripts.migrar_eixo_por_sinal            # só o plano
    .venv/Scripts/python.exe -m scripts.migrar_eixo_por_sinal --executar

O QUE MUDA
----------
A unidade deixa de ser notícia × ator e passa a ser notícia × ator × eixo, e o `item_id`
carrega o eixo. O porquê da regra está em `analyser.enriquecimento.separar_por_eixo`.

POR QUE NÃO É UM `ALTER TABLE`
------------------------------
O eixo de cada item depende de `trechos_json`, então a decisão é código Python, não SQL.
E os itens que passam a ter DOIS recortes precisam de resumo novo, porque o texto de cada
recorte é diferente do texto original.

A ESTRATÉGIA, E POR QUE ELA É MAIS SEGURA QUE A ÓBVIA
-----------------------------------------------------
O caminho óbvio seria construir os itens divididos aqui: recortar `trechos_json`, buscar
os parágrafos em `tabela_classificacao`, remontar o texto, recalcular o hash. Seria
reimplementar, num script de uso único e sem teste, exatamente o que o `Enriquecedor` já
faz e a suíte cobre.

Em vez disso, os eventos que geram divisão VOLTAM para a inbox e o worker os reconstrói
pelo caminho normal. `gravar_item` usa `ON CONFLICT DO NOTHING`, então os itens dos outros
atores do mesmo evento — já renomeados aqui — são reconhecidos e ignorados; e
`_processar_item` pula quem já tem resumo. Só os itens realmente novos pagam o modelo.

Resultado: os resumos existentes são preservados e apenas os dos recortes novos são
gerados. Sem isso seriam 859 do zero, que na medição real levaram 1h32.

O QUE ESTE SCRIPT NÃO FAZ
-------------------------
Não apaga panorama nenhum. `analyser_panorama` é append-only: as versões atuais ficam como
histórico e a reconsolidação grava versões novas. Os `fontes_json` das versões antigas
passam a apontar para `item_id` que não existem mais — é esperado, e é a razão de a
leitura do frontend dever usar sempre a versão mais recente de cada eixo.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from analyser.config import carregar_dotenv
from analyser.enriquecimento import REGRA_AGREGACAO, Trecho, separar_por_eixo

TABELA_BACKUP = "analyser_item_backup_maioria_v1"


def _trechos(linha: dict[str, Any]) -> list[Trecho]:
    """Reconstrói os `Trecho` a partir do que está gravado em `trechos_json`."""
    return [
        Trecho(
            paragraph_index=t["paragraph_index"],
            classificacao_sentimento=t["classificacao_sentimento"],
            score_confianca=float(t["score_confianca"]),
            id_classificacao=t.get("id_classificacao"),
        )
        for t in (linha["trechos_json"] or [])
    ]


def levantar(conexao: psycopg.Connection) -> dict[str, Any]:
    """Decide, item a item, o que acontece com ele. Não escreve nada."""
    with conexao.cursor(row_factory=dict_row) as cur:
        linhas = cur.execute(
            "SELECT item_id, evento_id, rotulo_definitivo, trechos_json,"
            "       resumo IS NOT NULL AS tem_resumo"
            "  FROM analyser_item ORDER BY item_id"
        ).fetchall()

    renomear: list[tuple[str, str, str]] = []
    dividir: list[dict[str, Any]] = []
    eixos: Counter[str] = Counter()

    for linha in linhas:
        recortes = separar_por_eixo(_trechos(linha))
        for eixo in recortes:
            eixos[eixo] += 1

        if len(recortes) == 1:
            eixo = next(iter(recortes))
            novo = linha["item_id"] + "__" + eixo
            renomear.append((linha["item_id"], novo, eixo))
        else:
            dividir.append(
                {
                    "item_id": linha["item_id"],
                    "evento_id": linha["evento_id"],
                    "eixos": sorted(recortes),
                    "de": linha["rotulo_definitivo"],
                }
            )

    return {
        "total": len(linhas),
        "renomear": renomear,
        "dividir": dividir,
        "eixos_depois": eixos,
        "eventos_a_reenfileirar": sorted({d["evento_id"] for d in dividir}),
        "resumos_a_gerar": sum(len(d["eixos"]) for d in dividir),
    }


def imprimir_plano(p: dict[str, Any]) -> None:
    depois = len(p["renomear"]) + p["resumos_a_gerar"]
    print()
    print(f"itens hoje                {p['total']:>5}")
    print(f"  renomeados (1 recorte)  {len(p['renomear']):>5}   resumo preservado")
    print(f"  divididos  (2 recortes) {len(p['dividir']):>5}   viram {p['resumos_a_gerar']}")
    print(f"itens depois              {depois:>5}")
    print()
    print(f"chamadas ao modelo        {p['resumos_a_gerar']:>5}   em vez de {p['total']} do zero")
    print(f"eventos a reenfileirar    {len(p['eventos_a_reenfileirar']):>5}")

    print("\ndistribuição por eixo depois:")
    total = sum(p["eixos_depois"].values())
    for eixo, n in p["eixos_depois"].most_common():
        print(f"  {eixo:<9} {n:>5}  ({100 * n / total:>4.1f}%)")

    if p["dividir"]:
        print("\nitens que se dividem:")
        for d in p["dividir"]:
            juntos = " + ".join(d["eixos"])
            print(f"  {d['item_id']:<46} {d['de']:>8} -> {juntos}")


def executar(conexao: psycopg.Connection, p: dict[str, Any]) -> None:
    """Aplica tudo numa transação só. Backup antes, para haver volta."""
    cur = conexao.cursor()

    cur.execute("DROP TABLE IF EXISTS " + TABELA_BACKUP)
    cur.execute("CREATE TABLE " + TABELA_BACKUP + " AS SELECT * FROM analyser_item")
    copiadas = cur.execute("SELECT count(*) FROM " + TABELA_BACKUP).fetchone()[0]
    if copiadas != p["total"]:
        raise RuntimeError(f"backup incompleto: {copiadas} de {p['total']}")
    print(f"backup: {TABELA_BACKUP} com {copiadas} linhas")

    # `status='resumido'` porque toda janela precisa ser reconsolidada: os eixos mudaram,
    # então nenhum panorama atual descreve mais a composição real da janela.
    for antigo, novo, eixo in p["renomear"]:
        cur.execute(
            "UPDATE analyser_item"
            "   SET item_id = %s, rotulo_definitivo = %s, regra_agregacao_versao = %s,"
            "       status = 'resumido', atualizado_em = now()"
            " WHERE item_id = %s",
            (novo, eixo, REGRA_AGREGACAO, antigo),
        )
    print(f"renomeados: {len(p['renomear'])}")

    for d in p["dividir"]:
        cur.execute("DELETE FROM analyser_item WHERE item_id = %s", (d["item_id"],))
    print(f"apagados para reconstrução: {len(p['dividir'])}")

    if p["eventos_a_reenfileirar"]:
        cur.execute(
            "UPDATE analyser_inbox_evento"
            "   SET status = 'recebido', processado_em = NULL"
            " WHERE evento_id = ANY(%s)",
            (p["eventos_a_reenfileirar"],),
        )
        print(f"eventos devolvidos à inbox: {len(p['eventos_a_reenfileirar'])}")

    conexao.commit()
    print("\ncommit feito.")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="migrar_eixo_por_sinal")
    parser.add_argument(
        "--executar",
        action="store_true",
        help="aplica de fato; sem isto apenas imprime o plano",
    )
    args = parser.parse_args(argv)

    carregar_dotenv(Path(".env"))
    dsn = os.environ.get("ANALYSER_DATABASE_URL", "").strip()
    if not dsn:
        print("ANALYSER_DATABASE_URL ausente.", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conexao:
        plano = levantar(conexao)
        imprimir_plano(plano)

        if not args.executar:
            print("\n(plano apenas — nada foi alterado; use --executar para aplicar)")
            return 0

        executar(conexao, plano)

    print("\nPRÓXIMOS PASSOS")
    print("  1) python -m scripts.processar --eventos 20       # resumos dos recortes novos")
    print("  2) python -m scripts.processar --consolidar-tudo  # reconsolida tudo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
