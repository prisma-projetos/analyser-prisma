# Fontes dos PDFs

Os PDFs em `docs/pdf/` são gerados a partir destes arquivos HTML. Editar o PDF direto não é
possível — edite o HTML e regere.

## Por que HTML e não Markdown

Os três documentos vão para leitores externos (responsável pelo banco, desenvolvedor do
frontend) e precisam de controle de quebra de página, largura de coluna em tabela e realce
de bloco. Markdown convertido não dá esse controle, e o projeto não tem pandoc instalado.

## Como regerar

Não há dependência de biblioteca: usa o Chrome em modo headless, que já existe na máquina.

```bash
CH="/c/Program Files/Google/Chrome/Application/chrome.exe"
SRC="docs/pdf/fontes"

"$CH" --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="docs/pdf/Proposta_Tabelas_Analyser.pdf" \
  "file:///$(pwd)/$SRC/Proposta_Tabelas_Analyser.html"
```

Repetir trocando o par (html, pdf) para os outros dois documentos.

## O caso especial do relatório da demo

`Demo_Panorama_Paragrafo_2026_08.pdf` não tem fonte própria: ele é gerado a partir de
`relatorios/demo_paragrafo_2026_08.html`, que por sua vez é produzido por
`scripts/gerar_relatorio_demo.py`.

O relatório usa tema escuro e não tem regras de impressão, então uma conversão direta sairia
ilegível — o Chrome descarta cor de fundo ao imprimir. O script `preparar_demo_pdf.py` faz uma
**cópia** do relatório com CSS de impressão injetado, sem tocar no original:

```bash
.venv/Scripts/python.exe docs/pdf/fontes/preparar_demo_pdf.py
```

Ele explica no próprio código por que cada regra existe (cor no `<html>` e não no `<body>`,
paisagem em vez de retrato, quebra de página travada dentro dos cards).
