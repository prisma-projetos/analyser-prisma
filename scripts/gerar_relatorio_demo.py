"""Gera um relatório HTML autocontido para a demonstração com dados reais.

O script não consulta o banco e não chama o modelo. Ele apenas transforma em uma
visualização os artefatos já produzidos pelo teste do Prisma Analyser.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

EIXOS = ("negativo", "positivo", "neutro")
ROTULOS = {
    "negativo": "Negativo",
    "positivo": "Positivo",
    "neutro": "Neutro",
}


def carregar_json(caminho: Path) -> Any:
    """Lê JSON em UTF-8 e preserva acentuação."""
    return json.loads(caminho.read_text(encoding="utf-8"))


def escapar(valor: object) -> str:
    """Escapa conteúdo externo antes de inseri-lo no HTML."""
    return html.escape(str(valor), quote=True)


def resumir_texto(texto: str, limite: int = 760) -> str:
    """Produz apenas uma prévia visual; o JSON de entrada continua integral."""
    limpo = " ".join(texto.split())
    if len(limpo) <= limite:
        return limpo
    return limpo[: limite - 1].rstrip() + "…"


def badges_fontes(fontes: list[str]) -> str:
    return "".join(f'<code class="source">{escapar(fonte)}</code>' for fonte in fontes)


def completar_identificacao_auditoria(
    auditoria: dict[str, Any], caminho: Path, numero_rodada: int
) -> dict[str, Any]:
    """Completa campos novos sem reescrever uma auditoria histórica.

    A primeira demonstração foi executada antes de esses nomes explícitos existirem.
    O id vem do nome imutável do arquivo e o número vem de sua posição cronológica no
    mesmo diretório de saída.
    """
    completa = dict(auditoria)
    completa.setdefault("execucao_id", caminho.stem.removeprefix("auditoria_"))
    completa.setdefault("numero_rodada", numero_rodada)
    completa.setdefault("arquivo_entrada", auditoria.get("rodada", ""))
    completa.setdefault("capacidade_maxima_lote", auditoria.get("lote", 0))
    return completa


def escolher_amostra(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Escolhe dois itens por eixo, mantendo a ordem da extração."""
    escolhidos: list[dict[str, Any]] = []
    for eixo in EIXOS:
        escolhidos.extend([item for item in itens if item["rotulo"] == eixo][:2])
    return escolhidos


def render_entradas(
    itens: list[dict[str, Any]], metadados_por_id: dict[str, dict[str, Any]]
) -> str:
    cards: list[str] = []
    for item in itens:
        meta = metadados_por_id[item["id"]]
        contagens = meta["contagem_rotulos"]
        cards.append(
            f"""
            <article class="card input-card">
              <div class="card-kicker">
                <code>{escapar(item["id"])}</code>
                <span>{escapar(item["veiculo"])}</span>
                <time>{escapar(item["data"])}</time>
                <span class="pill {escapar(item["rotulo"])}">{ROTULOS[item["rotulo"]]}</span>
              </div>
              <h3>{escapar(item["titulo"])}</h3>
              <p>{escapar(resumir_texto(item["texto"]))}</p>
              <div class="card-footer">
                <span>{meta["quantidade_trechos"]} trecho(s)</span>
                <span>neg {contagens["negativo"]} · pos {contagens["positivo"]} · neu {contagens["neutro"]}</span>
                <a href="{escapar(item["url"])}" rel="noreferrer">matéria original ↗</a>
              </div>
            </article>
            """
        )
    return "".join(cards)


def render_resumos(
    itens: list[dict[str, Any]], resumos: dict[str, dict[str, Any]]
) -> str:
    cards: list[str] = []
    for item in itens:
        resumo = resumos[item["id"]]
        temas = "".join(f"<li>{escapar(tema)}</li>" for tema in resumo["temas"])
        cards.append(
            f"""
            <article class="card summary-card">
              <div class="card-kicker">
                <code>{escapar(item["id"])}</code>
                <span class="pill {escapar(resumo["rotulo"])}">{ROTULOS[resumo["rotulo"]]}</span>
              </div>
              <p class="summary-text">{escapar(resumo["resumo"])}</p>
              <ul class="tags">{temas}</ul>
            </article>
            """
        )
    return "".join(cards)


def organizar_lotes(
    resumos: dict[str, dict[str, Any]], capacidade_maxima: int
) -> list[tuple[str, str, int, list[str]]]:
    """Reproduz as fatias usadas pelo pipeline por janela e eixo."""
    if capacidade_maxima < 1:
        raise ValueError("A capacidade máxima do lote precisa ser maior que zero.")

    grupos: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for item_id, registro in resumos.items():
        chave = (registro["janela"], registro["rotulo"])
        grupos.setdefault(chave, []).append((item_id, registro["fonte_id"]))

    lotes: list[tuple[str, str, int, list[str]]] = []
    for janela, eixo in sorted(
        grupos, key=lambda chave: (chave[0], EIXOS.index(chave[1]))
    ):
        # O pipeline fatia os registros em ordem de item_id; a tela repete a mesma
        # ordem e apenas exibe o fonte_id correspondente.
        fontes = [fonte_id for _, fonte_id in sorted(grupos[(janela, eixo)])]
        for inicio in range(0, len(fontes), capacidade_maxima):
            numero_lote = inicio // capacidade_maxima + 1
            lotes.append(
                (janela, eixo, numero_lote, fontes[inicio : inicio + capacidade_maxima])
            )
    return lotes


def render_lotes(
    resumos: dict[str, dict[str, Any]], capacidade_maxima: int
) -> str:
    cards: list[str] = []
    for janela, eixo, numero_lote, fontes in organizar_lotes(
        resumos, capacidade_maxima
    ):
        cards.append(
            f"""
            <article class="batch-card">
              <div>
                <span class="eyebrow">{escapar(janela)} · eixo {ROTULOS[eixo].lower()}</span>
                <h3>Lote {numero_lote}: {len(fontes)} notícias</h3>
                <p class="batch-capacity">Capacidade máxima configurada: {capacidade_maxima}</p>
              </div>
              <div class="source-row">{badges_fontes(fontes)}</div>
              <p class="notice">
                A chamada <code>consolidacao_lote</code> foi executada. O objeto
                <code>ConsolidacaoParcial</code> não é persistido pelo código atual e,
                portanto, seu texto exato não é reconstruído nesta tela.
              </p>
            </article>
            """
        )
    return "".join(cards)


def render_temas(temas: list[dict[str, Any]]) -> str:
    linhas = []
    for tema in temas:
        linhas.append(
            f"""
            <li class="theme-row">
              <strong><span>{tema["ocorrencias"]}×</span> {escapar(tema["tema"])}</strong>
              <div>{badges_fontes(tema["fontes"])}</div>
            </li>
            """
        )
    return "".join(linhas)


def render_panorama(registro: dict[str, Any]) -> str:
    panorama = registro["panorama"]
    avisos = (
        "".join(f"<li>{escapar(aviso)}</li>" for aviso in registro["avisos"])
        if registro["avisos"]
        else "<li>Nenhum aviso de verificação.</li>"
    )
    return f"""
    <article class="final-panel {escapar(registro["eixo"])}">
      <header class="final-header">
        <div>
          <span class="eyebrow">ETAPA 4 · NÍVEL 2</span>
          <h3>{ROTULOS[registro["eixo"]]}</h3>
        </div>
        <div class="final-meta">
          {panorama["total_itens"]} matérias · {escapar(registro["formato"])}
          · versão {registro["versao"]} · {registro["lotes"]} lote
        </div>
      </header>
      <section class="result-block">
        <h4>Panorama — parágrafo consolidado</h4>
        <p class="panorama-text">{escapar(panorama["texto"])}</p>
      </section>
      <section class="result-block">
        <h4>Temas — todos os resultados finais</h4>
        <ul class="theme-list">{render_temas(panorama["temas"])}</ul>
      </section>
      <section class="result-block">
        <h4>Fontes do panorama — todas</h4>
        <div class="source-row">{badges_fontes(panorama["fontes"])}</div>
      </section>
      <section class="warnings">
        <h4>Avisos de verificação</h4>
        <ul>{avisos}</ul>
      </section>
    </article>
    """


def montar_html(
    rodada: dict[str, Any],
    metadados: dict[str, Any],
    resumos: dict[str, dict[str, Any]],
    panoramas: list[dict[str, Any]],
    auditoria: dict[str, Any],
) -> str:
    itens = rodada["itens"]
    amostra = escolher_amostra(itens)
    metadados_por_id = {item["item_id"]: item for item in metadados["itens"]}
    integridade = metadados["integridade_interbases"]
    avisos = auditoria.get("avisos_verificacao", [])
    erros = auditoria.get("erros", [])
    capacidade_maxima = int(auditoria["capacidade_maxima_lote"])
    lotes_organizados = organizar_lotes(resumos, capacidade_maxima)
    identificacao_execucao = {
        "execucao_id": auditoria["execucao_id"],
        "numero_rodada": auditoria["numero_rodada"],
        "arquivo_entrada": auditoria["arquivo_entrada"],
    }
    identificacao_json = escapar(
        json.dumps(identificacao_execucao, ensure_ascii=False, indent=2)
    )
    panoramas_ordenados = sorted(
        panoramas, key=lambda p: EIXOS.index(p["eixo"])
    )
    finais = "".join(render_panorama(registro) for registro in panoramas_ordenados)

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Da notícia ao panorama · Prisma Analyser</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0c0e13;
      --panel: #151821;
      --panel-2: #10131a;
      --line: #2b3040;
      --text: #f3f1f8;
      --muted: #9eacca;
      --purple: #8f7cff;
      --green: #50cf91;
      --red: #ff7d8c;
      --yellow: #e7bb68;
      --serif: Georgia, "Times New Roman", serif;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --mono: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 82% 0%, rgba(143,124,255,.09), transparent 28rem),
        var(--bg);
      color: var(--text);
      font-family: var(--serif);
      line-height: 1.55;
    }}
    a {{ color: #aebfff; text-underline-offset: .2em; }}
    code {{ font-family: var(--mono); }}
    .page {{ width: min(1480px, calc(100% - 40px)); margin: 0 auto; padding: 58px 0 100px; }}
    .hero {{ border-bottom: 1px solid #d7d9e1; padding-bottom: 42px; }}
    .hero-top {{ display: flex; gap: 24px; justify-content: space-between; align-items: flex-start; }}
    h1, h2, h3, h4 {{ font-family: var(--sans); margin: 0; line-height: 1.06; letter-spacing: -.035em; }}
    h1 {{ font-size: clamp(3.5rem, 8vw, 8rem); max-width: 1100px; }}
    .lede {{ color: #aebfe0; font-size: clamp(1.2rem, 2vw, 1.85rem); max-width: 900px; margin: 24px 0 0; }}
    .read-only {{
      border: 1px solid rgba(80,207,145,.5); background: rgba(80,207,145,.08);
      color: var(--green); padding: 8px 12px; font: 700 .78rem var(--mono);
      text-transform: uppercase; letter-spacing: .08em; white-space: nowrap;
    }}
    .metrics {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 1px; margin-top: 38px; background: var(--line); border: 1px solid var(--line); }}
    .metric {{ background: var(--panel-2); padding: 18px; min-height: 94px; }}
    .metric strong {{ display: block; color: var(--purple); font: 700 1.55rem var(--sans); }}
    .metric span {{ color: var(--muted); font: .75rem var(--mono); text-transform: uppercase; letter-spacing: .08em; }}
    .stage {{ margin-top: 54px; border-top: 3px solid var(--purple); padding-top: 28px; }}
    .stage-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 24px; margin-bottom: 26px; }}
    .stage-title {{ display: flex; align-items: baseline; gap: 24px; }}
    .eyebrow {{ color: var(--purple); font: 800 .76rem var(--mono); text-transform: uppercase; letter-spacing: .16em; }}
    .stage h2 {{ font-size: clamp(2rem, 4vw, 3.4rem); }}
    .stage-note {{ color: var(--muted); font: .86rem var(--mono); text-align: right; }}
    .cards {{ display: grid; gap: 8px; }}
    .card {{ border: 1px solid var(--line); background: var(--panel); padding: 24px 28px; }}
    .card-kicker {{ display: flex; flex-wrap: wrap; align-items: center; gap: 14px; color: #b5c4e2; font: .86rem var(--mono); }}
    .card-kicker code {{ color: var(--purple); font-weight: 800; }}
    .card h3 {{ font-size: 1.25rem; margin-top: 12px; }}
    .card p {{ color: #b6c7e8; font-size: 1.08rem; margin: 13px 0; }}
    .card-footer {{ display: flex; flex-wrap: wrap; gap: 18px; color: var(--muted); font: .76rem var(--mono); }}
    .pill {{ padding: 3px 8px; border: 1px solid var(--line); font-size: .7rem; text-transform: uppercase; }}
    .pill.negativo {{ color: var(--red); border-color: rgba(255,125,140,.45); }}
    .pill.positivo {{ color: var(--green); border-color: rgba(80,207,145,.45); }}
    .pill.neutro {{ color: var(--yellow); border-color: rgba(231,187,104,.45); }}
    .summary-text {{ color: #f5f2fb !important; font-size: 1.18rem !important; }}
    .tags {{ list-style: none; padding: 0; margin: 16px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }}
    .tags li {{ background: #0e1118; padding: 7px 10px; color: #a9bde2; font: .76rem var(--mono); }}
    .batch-grid {{ display: grid; gap: 8px; }}
    .batch-card {{ background: var(--panel); border: 1px solid var(--line); padding: 26px; }}
    .batch-card h3 {{ margin-top: 8px; font-size: 1.5rem; }}
    .batch-capacity {{ color: #dae3f6; font: 700 .92rem var(--mono); margin: 9px 0 0; }}
    .notice {{ border-left: 3px solid var(--yellow); padding-left: 14px; color: #b7c3db; margin: 18px 0 0; }}
    .source-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 14px; }}
    .source {{ background: #0d1017; color: #9cafff; padding: 5px 7px; font-size: .72rem; }}
    .finals {{ display: grid; gap: 28px; }}
    .final-panel {{ border: 1px solid var(--line); background: var(--panel); overflow: hidden; }}
    .final-panel.negativo {{ border-top: 4px solid var(--red); }}
    .final-panel.positivo {{ border-top: 4px solid var(--green); }}
    .final-panel.neutro {{ border-top: 4px solid var(--yellow); }}
    .final-header {{ padding: 26px; display: flex; justify-content: space-between; gap: 20px; background: var(--panel-2); border-bottom: 1px solid var(--line); }}
    .final-header h3 {{ font-size: 2rem; margin-top: 5px; }}
    .final-meta {{ color: var(--muted); font: .78rem var(--mono); text-align: right; }}
    .compact-text {{ margin: 24px 26px 0; padding: 20px; background: rgba(231,187,104,.06); color: #e9e3d5; font-size: 1.16rem; border-left: 3px solid var(--yellow); }}
    .result-block {{ margin: 0 26px; padding: 24px 0; border-bottom: 1px solid var(--line); }}
    .result-block h4, .warnings h4 {{ color: var(--purple); font: 800 .77rem var(--mono); letter-spacing: .12em; text-transform: uppercase; margin-bottom: 15px; }}
    .theme-list, .fact-list, .response-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 5px; }}
    .theme-row {{ background: #10131a; padding: 12px 14px; display: flex; justify-content: space-between; gap: 18px; align-items: center; }}
    .theme-row strong {{ font-family: var(--mono); font-size: .92rem; }}
    .theme-row strong span {{ color: var(--purple); }}
    .theme-row .source {{ color: var(--muted); }}
    .panorama-text {{ margin: 0; padding: 18px 22px; background: #10131a; color: #e9eefb; font-size: 1.14rem; line-height: 1.72; border-left: 3px solid #2b3040; }}
    .final-panel.negativo .panorama-text {{ border-left-color: var(--red); }}
    .final-panel.positivo .panorama-text {{ border-left-color: var(--green); }}
    .final-panel.neutro .panorama-text {{ border-left-color: var(--yellow); }}
    .response-row {{ display: grid; grid-template-columns: 48px 1fr 2fr; gap: 12px; align-items: center; background: #10131a; padding: 10px 14px; }}
    .response-row strong {{ color: var(--purple); font-family: var(--mono); }}
    .empty {{ color: var(--muted); font-style: italic; }}
    .warnings {{ margin: 0; padding: 22px 26px; background: rgba(231,187,104,.055); }}
    .warnings ul {{ margin: 0; padding-left: 20px; color: #e7c98f; }}
    .audit-grid {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 12px; }}
    .audit-card {{ border: 1px solid var(--line); background: var(--panel); padding: 24px; }}
    .audit-card h3 {{ font-size: 1.25rem; margin-bottom: 16px; }}
    .audit-card dl {{ display: grid; grid-template-columns: 1fr auto; gap: 8px 16px; margin: 0; }}
    .audit-card dt {{ color: var(--muted); }}
    .audit-card dd {{ margin: 0; font-family: var(--mono); color: #dae3f6; text-align: right; }}
    .audit-json {{ grid-column: 1 / -1; }}
    .audit-json pre {{ margin: 0; padding: 18px; overflow-x: auto; background: #0d1017; color: #dae3f6; }}
    .audit-json code {{ font-size: .92rem; }}
    .risk-list {{ margin: 0; padding-left: 20px; color: #b8c7e2; }}
    .risk-list li + li {{ margin-top: 10px; }}
    .critical {{ color: var(--red); }}
    footer {{ border-top: 1px solid var(--line); color: var(--muted); margin-top: 54px; padding-top: 24px; font: .76rem var(--mono); }}
    @media (max-width: 900px) {{
      .page {{ width: min(100% - 24px, 1480px); padding-top: 28px; }}
      .hero-top, .stage-head, .final-header {{ flex-direction: column; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .audit-grid {{ grid-template-columns: 1fr; }}
      .stage-title {{ align-items: flex-start; flex-direction: column; gap: 8px; }}
      .stage-note, .final-meta {{ text-align: left; }}
      .theme-row {{ align-items: flex-start; flex-direction: column; }}
      .response-row {{ grid-template-columns: 42px 1fr; }}
      .response-row div {{ grid-column: 1 / -1; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <div class="hero-top">
        <div>
          <span class="eyebrow">PRISMA ANALYSER · TESTE COM DADOS REAIS</span>
          <h1>Da notícia ao panorama</h1>
          <p class="lede">
            Trechos classificados sobre {escapar(rodada["ator"]["nome"])} atravessando
            todo o pipeline local. Entradas e resumos aparecem por amostragem visual;
            os três panoramas finais são exibidos integralmente.
          </p>
        </div>
        <div class="read-only">banco somente leitura · rollback</div>
      </div>
      <div class="metrics">
        <div class="metric"><strong>{len(itens)}</strong><span>itens processados</span></div>
        <div class="metric"><strong>{metadados["amostragem"]["trechos_total"]}</strong><span>trechos classificados</span></div>
        <div class="metric"><strong>3</strong><span>eixos finais</span></div>
        <div class="metric"><strong>{capacidade_maxima}</strong><span>capacidade máxima</span></div>
        <div class="metric"><strong>{auditoria["tokens"]["prompt"]}</strong><span>tokens de prompt</span></div>
        <div class="metric"><strong>{auditoria["tokens"]["resposta"]}</strong><span>tokens de resposta</span></div>
      </div>
    </header>

    <section class="stage" id="entrada">
      <div class="stage-head">
        <div class="stage-title"><span class="eyebrow">ENTRADA</span><h2>O que chegou ao Analyser</h2></div>
        <div class="stage-note">{len(amostra)} de {len(itens)} itens exibidos · todos os {len(itens)} foram processados</div>
      </div>
      <div class="cards">{render_entradas(amostra, metadados_por_id)}</div>
    </section>

    <section class="stage" id="resumos">
      <div class="stage-head">
        <div class="stage-title"><span class="eyebrow">ETAPA 1 · MAP</span><h2>resumo_item</h2></div>
        <div class="stage-note">{len(amostra)} de {len(resumos)} resumos exibidos · modelo {escapar(auditoria["modelo"])}</div>
      </div>
      <div class="cards">{render_resumos(amostra, resumos)}</div>
    </section>

    <section class="stage" id="lotes">
      <div class="stage-head">
        <div class="stage-title"><span class="eyebrow">ETAPA 4 · NÍVEL 1</span><h2>consolidacao_lote</h2></div>
        <div class="stage-note">{len(lotes_organizados)} lotes reais · capacidade máxima {capacidade_maxima}</div>
      </div>
      <div class="batch-grid">{render_lotes(resumos, capacidade_maxima)}</div>
    </section>

    <section class="stage" id="finais">
      <div class="stage-head">
        <div class="stage-title"><span class="eyebrow">SAÍDA FINAL</span><h2>consolidacao_final — o que o cidadão lê</h2></div>
        <div class="stage-note">{len(panoramas_ordenados)} panoramas · sem truncamento nesta seção</div>
      </div>
      <div class="finals">{finais}</div>
    </section>

    <section class="stage" id="auditoria">
      <div class="stage-head">
        <div class="stage-title"><span class="eyebrow">EVIDÊNCIA</span><h2>Auditoria e limites do teste</h2></div>
      </div>
      <div class="audit-grid">
        <article class="audit-card audit-json">
          <h3>Identificação da execução</h3>
          <pre><code>{identificacao_json}</code></pre>
        </article>
        <article class="audit-card">
          <h3>Execução comprovada</h3>
          <dl>
            <dt>Modelo</dt><dd>{escapar(auditoria["modelo"])}</dd>
            <dt>Capacidade máxima do lote</dt><dd>{capacidade_maxima}</dd>
            <dt>resumo_item</dt><dd>{auditoria["chamadas_por_tarefa"]["resumo_item"]} chamadas</dd>
            <dt>consolidacao_lote</dt><dd>{auditoria["chamadas_por_tarefa"]["consolidacao_lote"]} chamadas</dd>
            <dt>consolidacao_final</dt><dd>{auditoria["chamadas_por_tarefa"]["consolidacao_final"]} chamadas</dd>
            <dt>Erros</dt><dd>{len(erros)}</dd>
            <dt>Avisos</dt><dd>{len(avisos)}</dd>
          </dl>
        </article>
        <article class="audit-card">
          <h3>Integridade entre tabelas</h3>
          <dl>
            <dt>Pares artigo/pipeline</dt><dd>{integridade["pares_artigo_pipeline"]}</dd>
            <dt>Link coincidente</dt><dd>{integridade["links_coerentes"]}</dd>
            <dt class="critical">Outbox com link diferente</dt><dd class="critical">{integridade["outbox_sem_link_identico"]}</dd>
            <dt>Selecionados para o teste</dt><dd>{len(itens)}</dd>
          </dl>
        </article>
        <article class="audit-card">
          <h3>Limitações que não foram escondidas</h3>
          <ul class="risk-list">
            <li>O título jornalístico não existe no banco de classificação; o título mostrado é técnico e sintético.</li>
            <li>O <code>paragraph_index</code> não é persistido em <code>tabela_classificacao</code>; ele foi associado pela outbox usando rótulo, score e ordem relativa.</li>
            <li>O rótulo do item é uma regra provisória de maioria dos trechos; empate resulta em neutro.</li>
            <li>A amostra é demonstrativa e balanceada por eixo, não uma amostra estatística da cobertura.</li>
            <li>Lote 100 reduz chamadas, mas este teste tem apenas quatro resumos por eixo e não valida um prompt com 100 resumos.</li>
            <li>A classificação léxica pode contrariar o sentido editorial do trecho; o Analyser preserva o eixo recebido.</li>
          </ul>
        </article>
        <article class="audit-card">
          <h3>Aviso produzido pelo próprio pipeline</h3>
          <ul class="risk-list">
            {''.join(f"<li>{escapar(aviso)}</li>" for aviso in avisos) if avisos else "<li>Nenhum aviso.</li>"}
          </ul>
        </article>
      </div>
    </section>

    <footer>
      Gerado somente a partir de artefatos locais · banco consultado com
      <code>BEGIN TRANSACTION READ ONLY</code> e <code>ROLLBACK</code> ·
      nenhuma credencial incorporada ao relatório.
    </footer>
  </main>
</body>
</html>
"""


def montar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entrada", type=Path, required=True)
    parser.add_argument("--metadados", type=Path, required=True)
    parser.add_argument("--saida-analyser", type=Path, required=True)
    parser.add_argument("--relatorio", type=Path, required=True)
    return parser


def main() -> None:
    args = montar_parser().parse_args()
    rodada = carregar_json(args.entrada)
    metadados = carregar_json(args.metadados)
    resumos = carregar_json(args.saida_analyser / "resumos_item.json")
    panoramas = carregar_json(args.saida_analyser / "panoramas.json")
    auditorias = sorted(args.saida_analyser.glob("auditoria_*.json"))
    if not auditorias:
        raise FileNotFoundError("Nenhuma auditoria encontrada na saída do Analyser.")
    caminho_auditoria = auditorias[-1]
    auditoria = completar_identificacao_auditoria(
        carregar_json(caminho_auditoria), caminho_auditoria, len(auditorias)
    )

    if not isinstance(resumos, dict) or len(resumos) != len(rodada["itens"]):
        raise ValueError("A quantidade de resumos não corresponde à entrada.")
    if not isinstance(panoramas, list) or {p["eixo"] for p in panoramas} != set(EIXOS):
        raise ValueError("São esperados panoramas finais para os três eixos.")

    conteudo = montar_html(rodada, metadados, resumos, panoramas, auditoria)
    args.relatorio.parent.mkdir(parents=True, exist_ok=True)
    args.relatorio.write_text(conteudo, encoding="utf-8")
    print(
        json.dumps(
            {
                "relatorio": str(args.relatorio),
                "bytes": args.relatorio.stat().st_size,
                "entradas": len(rodada["itens"]),
                "resumos": len(resumos),
                "panoramas_finais": len(panoramas),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
