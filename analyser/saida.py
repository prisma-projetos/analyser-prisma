"""Impressão do panorama no terminal, com `rich`.

A ordem das seções conta uma história: quem é o alvo, quanto se economizou, o que foi
recalculado, como a cobertura se distribui, o que os panoramas dizem e o que a verificação
de contagem encontrou.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from analyser.config import EIXOS, Config
from analyser.pipeline import ResultadoExecucao
from analyser.schemas import RegistroPanorama

# O console legado do Windows usa cp1252 e quebra em acentos e sinais tipográficos.
# Reconfigurar a saída para UTF-8 resolve; `errors="replace"` garante que, num terminal
# que ainda assim não dê conta, o programa degrade em vez de morrer.
for _fluxo in (sys.stdout, sys.stderr):
    if hasattr(_fluxo, "reconfigure"):
        _fluxo.reconfigure(encoding="utf-8", errors="replace")

console = Console()

#: Cores por eixo. São só rótulos de leitura no terminal — não influenciam nada do
#: processamento, que trata os três eixos exatamente do mesmo jeito.
_COR_EIXO = {"negativo": "red", "positivo": "green", "neutro": "cyan"}


def imprimir(resultado: ResultadoExecucao, config: Config) -> None:
    """Imprime o resultado completo de uma execução."""
    _cabecalho(resultado, config)
    _economia(resultado)
    _janelas(resultado)
    _contagens(resultado)
    _panoramas(resultado)
    _avisos(resultado)
    _auditoria(resultado)


# --------------------------------------------------------------------------------------


def _cabecalho(resultado: ResultadoExecucao, config: Config) -> None:
    corpo = Text()
    corpo.append("ator      ", style="dim")
    corpo.append(f"{resultado.ator_nome} ({resultado.ator_id})\n", style="bold")
    corpo.append("rodada    ", style="dim")
    corpo.append(f"{resultado.rodada}  •  {len(resultado.ids_entrada)} itens de entrada\n")
    corpo.append("modelo    ", style="dim")
    corpo.append(f"{resultado.modelo}  •  lote={config.lote}")
    console.print(Panel(corpo, title="[bold]Prisma · Analyser[/bold]", border_style="blue"))


def _economia(resultado: ResultadoExecucao) -> None:
    do_cache, resumidos = resultado.economia
    linha = Text()
    linha.append("Economia  ", style="bold")
    linha.append(f"{do_cache}", style="bold green")
    linha.append(" do cache (sem chamar o modelo)  ·  ")
    linha.append(f"{resumidos}", style="bold yellow")
    linha.append(" resumidos agora")
    if do_cache:
        linha.append(f"\n          reaproveitados: {', '.join(resultado.ids_do_cache)}", style="dim")
    if resultado.itens_reclassificados:
        linha.append(
            f"\n          reclassificados: {', '.join(resultado.itens_reclassificados)}"
            " (rótulo mudou, resumo preservado)",
            style="dim",
        )
    console.print(linha)
    console.print()


def _janelas(resultado: ResultadoExecucao) -> None:
    console.print("[bold]Janelas marcadas como sujas e recalculadas[/bold]")
    if not resultado.janelas_sujas:
        console.print("  [dim]nenhuma — nada novo e nenhum rótulo alterado[/dim]\n")
        return
    for janela in resultado.janelas_sujas:
        motivos = resultado.motivo_janela.get(janela, [])
        novos = [m.split(" ")[0] for m in motivos if "item novo" in m]
        alterados = [m.split(" ")[0] for m in motivos if "rótulo alterado" in m]
        partes: list[str] = []
        if novos:
            partes.append(f"{len(novos)} item(ns) novo(s): {', '.join(novos)}")
        if alterados:
            partes.append(f"{len(alterados)} com rótulo alterado: {', '.join(alterados)}")
        console.print(f"  [bold]{janela}[/bold]  ←  " + "  ·  ".join(partes))
    console.print()


def _contagens(resultado: ResultadoExecucao) -> None:
    if not resultado.contagem_eixo:
        return

    tabela = Table(title="Contagem por eixo", title_justify="left", header_style="bold")
    tabela.add_column("janela")
    for eixo in EIXOS:
        tabela.add_column(eixo, justify="right", style=_COR_EIXO[eixo])
    tabela.add_column("total", justify="right", style="bold")
    for janela, contagem in sorted(resultado.contagem_eixo.items()):
        tabela.add_row(
            janela,
            *[str(contagem.get(eixo, 0)) for eixo in EIXOS],
            str(sum(contagem.values())),
        )
    console.print(tabela)
    console.print()

    if resultado.contagem_veiculo:
        por_veiculo = Table(
            title="Contagem por veículo (janelas recalculadas)",
            title_justify="left",
            header_style="bold",
        )
        por_veiculo.add_column("veículo")
        for eixo in EIXOS:
            por_veiculo.add_column(eixo, justify="right", style=_COR_EIXO[eixo])
        por_veiculo.add_column("total", justify="right", style="bold")
        for veiculo, contagem in sorted(resultado.contagem_veiculo.items()):
            por_veiculo.add_row(
                veiculo,
                *[str(contagem.get(eixo, 0)) for eixo in EIXOS],
                str(sum(contagem.values())),
            )
        console.print(por_veiculo)
        console.print()


def _panoramas(resultado: ResultadoExecucao) -> None:
    if not resultado.panoramas:
        return
    console.print("[bold]Panoramas[/bold]\n")
    ordenados = sorted(
        resultado.panoramas, key=lambda p: (p.janela, EIXOS.index(p.eixo))
    )
    for registro in ordenados:
        console.print(_render_panorama(registro))
        console.print()

    for janela, eixo in sorted(resultado.eixos_vazios):
        console.print(f"  [dim]{janela} · {eixo}: sem itens nesta janela — nada a consolidar[/dim]")
    if resultado.eixos_vazios:
        console.print()


def _render_panorama(registro: RegistroPanorama) -> Panel:
    """Um painel por eixo, com o formato que aquele eixo usa."""
    p = registro.panorama
    cor = _COR_EIXO[registro.eixo]
    corpo = Text()

    corpo.append(f"{p.total_itens} matérias  ·  formato {registro.formato}", style="dim")
    corpo.append(f"  ·  {registro.lotes} lote(s) no primeiro nível\n\n", style="dim")

    if p.texto:
        corpo.append(p.texto + "\n\n")

    corpo.append("temas\n", style="bold")
    for tema in p.temas:
        corpo.append(f"  • {tema.tema} ")
        corpo.append(f"({tema.ocorrencias}) ", style="bold")
        corpo.append(f"{', '.join(tema.fontes)}\n", style="dim")

    if p.fatos_mais_citados:
        corpo.append("\nfatos mais citados\n", style="bold")
        for fato in p.fatos_mais_citados:
            corpo.append(f"  • {fato.descricao}\n")
            corpo.append(f"    {', '.join(fato.fontes)}\n", style="dim")

    if p.respostas_registradas:
        corpo.append("\nresposta dos citados registrada\n", style="bold")
        for resposta in p.respostas_registradas:
            marca = "sim" if resposta.houve_resposta else "não"
            corpo.append(f"  • {resposta.citado}: {marca} ")
            corpo.append(f"{', '.join(resposta.fontes)}\n", style="dim")

    corpo.append("\nfontes  ", style="bold")
    corpo.append(", ".join(p.fontes), style="dim")

    if registro.avisos:
        corpo.append("\n\navisos de verificação\n", style="bold yellow")
        for aviso in registro.avisos:
            corpo.append(f"  ! {aviso}\n", style="yellow")

    return Panel(
        corpo,
        title=f"[bold {cor}]{registro.janela} · {registro.eixo}[/bold {cor}]",
        subtitle=f"[bold]versão {registro.versao}[/bold]",
        border_style=cor,
        title_align="left",
        subtitle_align="right",
    )


def _avisos(resultado: ResultadoExecucao) -> None:
    console.print("[bold]Verificação de contagem[/bold]")
    if not resultado.avisos:
        console.print(
            "  [green]sem divergências[/green] "
            "[dim]— todos os ids citados existem e as contagens batem[/dim]\n"
        )
        return
    for aviso in resultado.avisos:
        console.print(f"  [yellow]![/yellow] {aviso}")
    console.print()


def _auditoria(resultado: ResultadoExecucao) -> None:
    corpo = Text()
    corpo.append("modelo            ", style="dim")
    corpo.append(f"{resultado.modelo}\n")
    corpo.append("prompts (sha-256) ", style="dim")
    corpo.append("\n")
    for nome, sha in resultado.hashes_prompts.items():
        corpo.append(f"  {nome:<28} {sha[:16]}…\n", style="dim")
    corpo.append("janelas recalculadas  ", style="dim")
    corpo.append(f"{', '.join(resultado.janelas_sujas) or '—'}\n")
    corpo.append("entrada / cache / novos  ", style="dim")
    do_cache, resumidos = resultado.economia
    corpo.append(f"{len(resultado.ids_entrada)} / {do_cache} / {resumidos}\n")
    if resultado.tokens:
        corpo.append("tokens            ", style="dim")
        corpo.append(
            ", ".join(f"{k}={v}" for k, v in sorted(resultado.tokens.items())) + "\n"
        )
    if resultado.erros:
        corpo.append("erros             ", style="dim")
        corpo.append(f"{len(resultado.erros)}\n", style="red")
        for erro in resultado.erros:
            corpo.append(f"  {erro}\n", style="red")
    corpo.append("registro          ", style="dim")
    corpo.append(resultado.caminho_auditoria)
    console.print(Panel(corpo, title="[bold]Auditoria[/bold]", border_style="magenta"))

