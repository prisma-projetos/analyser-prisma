"""Ponto de entrada do Analyser.

    python -m analyser.cli --entrada dados/rodada_1.json

Roda sem nenhuma variável de ambiente configurada: o modelo padrão é o fake, que não usa
rede nem chave.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from analyser.config import LOTE_PADRAO, Config, carregar_dotenv
from analyser.modelos import Modelo, ModeloAPI, ModeloFake
from analyser.pipeline import Analyser
from analyser.saida import console, imprimir


def montar_parser() -> argparse.ArgumentParser:
    """Argumentos do CLI."""
    parser = argparse.ArgumentParser(
        prog="analyser",
        description=(
            "Consolida notícias já classificadas em um panorama por político e período."
        ),
    )
    parser.add_argument(
        "--entrada", required=True, type=Path, help="arquivo JSON da rodada a processar"
    )
    parser.add_argument(
        "--modelo",
        choices=("fake", "api"),
        default="fake",
        help="fake (padrão, sem rede e sem chave) ou api (lê BASE_URL, API_KEY e MODEL)",
    )
    parser.add_argument(
        "--lote",
        type=int,
        default=LOTE_PADRAO,
        help=f"tamanho do lote no primeiro nível da consolidação (padrão: {LOTE_PADRAO})",
    )
    parser.add_argument(
        "--saida", type=Path, default=Path("saida"), help="diretório dos arquivos JSON"
    )
    parser.add_argument(
        "--prompts", type=Path, default=Path("prompts"), help="diretório dos prompts"
    )
    return parser


def construir_modelo(escolha: str) -> Modelo:
    """Instancia a implementação escolhida."""
    if escolha == "fake":
        return ModeloFake()
    carregar_dotenv(Path(".env"))
    return ModeloAPI()


def main(argv: list[str] | None = None) -> int:
    """Executa uma rodada e imprime o resultado."""
    args = montar_parser().parse_args(argv)

    if not args.entrada.is_file():
        console.print(f"[red]Arquivo de entrada não encontrado:[/red] {args.entrada}")
        return 2

    config = Config(
        entrada=args.entrada,
        saida=args.saida,
        prompts=args.prompts,
        lote=args.lote,
        modelo=args.modelo,
    )

    try:
        modelo = construir_modelo(args.modelo)
    except RuntimeError as erro:
        console.print(f"[red]{erro}[/red]")
        return 2

    resultado = Analyser(config, modelo).executar()
    imprimir(resultado, config)
    return 1 if resultado.erros else 0


if __name__ == "__main__":
    sys.exit(main())
