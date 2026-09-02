"""Prepara uma cópia do relatório da demo para virar PDF, sem tocar no original."""
from pathlib import Path

ORIGEM = Path(r"c:\projetos\prisma-analyse\relatorios\demo_paragrafo_2026_08.html")
DESTINO = Path(
    r"C:\Users\crish\AppData\Local\Temp\claude\c--projetos"
    r"\d4dc2e26-e41b-481d-a7be-d693b954b093\scratchpad\demo_para_pdf.html"
)

CSS_IMPRESSAO = """
<style id="print-overrides">
  @page { size: A4 landscape; margin: 9mm; }

  /* O Chrome descarta cor de fundo por padrão; sem isto o tema escuro sai
     como texto claro sobre papel branco, ou seja, ilegível. */
  * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }

  /* A cor precisa estar no <html> para cobrir todas as páginas: o fundo do
     <body> pinta apenas a caixa dele, e as páginas seguintes sairiam brancas. */
  html { background: #0c0e13 !important; }

  .page { width: 100% !important; padding: 18px 0 24px !important; }

  /* Nada de card partido no meio por causa de quebra de página. */
  .card, .batch-card, .final-panel, .audit-card, .metric,
  .theme-row, .response-row, .panorama-text {
    break-inside: avoid !important; page-break-inside: avoid !important;
  }

  /* O hero usa clamp() com vw; em página estreita ele fica desproporcional. */
  h1 { font-size: 3.1rem !important; max-width: 100% !important; }
  .lede { font-size: 1.1rem !important; max-width: 100% !important; }

  a { text-decoration: none !important; }
</style>
"""


def main() -> None:
    html = ORIGEM.read_text(encoding="utf-8")
    assert "</head>" in html, "não achei o </head> para injetar o CSS"
    # Vai por último no <head> para vencer as regras anteriores por ordem de cascata.
    html = html.replace("</head>", CSS_IMPRESSAO + "</head>", 1)
    DESTINO.write_text(html, encoding="utf-8")
    print(f"copia preparada: {DESTINO.name} ({len(html)//1024} KB)")
    print(f"original intacto: {ORIGEM}")


if __name__ == "__main__":
    main()
