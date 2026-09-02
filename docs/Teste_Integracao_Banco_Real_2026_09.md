# Teste de integração do Prisma Analyser com dados reais

**Data:** 1º de setembro de 2026  
**Projeto:** Prisma Analyser  
**Ator analisado:** Luiz Inácio Lula da Silva, identificador eleitoral
`122418060191`  
**Janela:** agosto de 2026  
**Modelo:** `qwen3.5:9b`, executado localmente pelo Ollama  
**Situação:** execução concluída, com três panoramas finais

> **O BANCO NÃO FOI ALTERADO.** Todas as consultas SQL foram abertas com
> `BEGIN TRANSACTION READ ONLY` e encerradas com `ROLLBACK`. Nenhum `INSERT`,
> `UPDATE`, `DELETE`, `CREATE` ou outra operação de escrita foi executada.

## 1. Objetivo

O objetivo foi verificar, com dados existentes no banco do projeto, se o contrato atual
do Prisma Analyser consegue:

1. receber somente os trechos classificados relevantes para um ator;
2. produzir um `resumo_item` por artigo;
3. agrupar por ator, mês e eixo;
4. executar a consolidação em dois níveis;
5. produzir e persistir os panoramas negativo, positivo e neutro;
6. conservar rastreabilidade até artigo, classificação e índice do parágrafo;
7. apresentar o resultado em uma tela demonstrativa próxima das referências visuais.

O teste também aplicou a mudança solicitada de `LOTE_PADRAO = 100`.

## 2. Resposta direta

O núcleo do Analyser processou a amostra real de ponta a ponta. Foram gerados 12 resumos,
três consolidações de lote e três consolidações finais. A execução terminou com código
zero, sem erro de modelo ou de schema.

Isso oferece evidência de que o contrato atual aceita uma adaptação construída com os
trechos relevantes, sem exigir o texto jornalístico completo. Contudo, o teste também
encontrou problemas de integração que precisam ser resolvidos antes de produção:

- `id_artigo_scrape` sozinho não identifica com segurança a mesma notícia entre
  `tabela_classificacao` e `classifier_analyser_outbox`;
- o título original não está no banco de classificação;
- `paragraph_index` existe na outbox, mas não em `tabela_classificacao`;
- não há uma regra de negócio oficial para converter vários rótulos de parágrafo em um
  rótulo único do item;
- a qualidade do classificador léxico apresenta casos semanticamente discutíveis;
- lote 100 foi configurado, mas este teste tinha somente quatro resumos em cada eixo e
  não constitui teste de estresse com 100 resumos no mesmo prompt.

## 3. Conceitos fundamentais

**Transação somente leitura** (modo do PostgreSQL que permite consultar dados, mas impede
operações de escrita dentro da transação) foi usada como barreira técnica adicional contra
alterações acidentais.

**Proveniência de dados** (registro de onde cada informação veio e de quais identificadores
permitem reencontrá-la) foi preservada em um arquivo de metadados separado da entrada do
modelo.

**Map-reduce** (estratégia em que cada item é processado isoladamente e, depois, os
resultados menores são consolidados) corresponde às etapas
`resumo_item → consolidacao_lote → consolidacao_final`.

**Limite de contexto** (quantidade máxima de tokens que o modelo consegue considerar em
uma chamada) é diferente do tamanho do lote. O lote define até quantos resumos são
agrupados; o contexto define se o texto resultante realmente cabe na chamada.

## 4. Analogia didática

Analogia: o banco de classificação funciona como um arquivo com recortes marcados, e o
Analyser funciona como um redator que recebe somente os recortes referentes a uma pessoa
para escrever um panorama.

Correspondência técnica:

- cada “recorte” é um `paragrafo_analisado`;
- a etiqueta do recorte contém ator, rótulo e `score_confianca`;
- a ordem do recorte vem do `paragraph_index` da outbox;
- o “redator” é o modelo `qwen3.5:9b`;
- a ficha que permite voltar à fonte é o conjunto
  `(item_id, id_artigo_scrape, outbox_id, id_classificacao, paragraph_index)`.

Limitação da analogia: os recortes não carregam necessariamente o contexto jornalístico
completo. Portanto, a síntese pode perder elementos que estavam fora dos parágrafos
selecionados. Além disso, rótulos e resumos do modelo são probabilísticos, não decisões
humanas infalíveis.

## 5. Contexto anterior e problema

O schema `ItemEntrada` exige exatamente:

| Campo | Exigência atual |
|---|---|
| `id` | identificador único do item |
| `veiculo` | nome do veículo |
| `url` | link da matéria |
| `data` | data de publicação |
| `titulo` | título |
| `texto` | conteúdo que será resumido |
| `rotulo` | um eixo único do item |

O banco de classificação oferece trechos, URL, data, ator, rótulo e score, mas não
oferece título nem um rótulo único por artigo. O `paragraph_index` está apenas no
payload da outbox. Foi necessária uma camada de adaptação para produzir uma `Rodada`
válida sem alterar o schema do Analyser.

## 6. Diagnóstico do banco

### 6.1 Estruturas usadas

Foram consultadas, somente para leitura:

- `tabela_classificacao`;
- `classifier_analyser_outbox`;
- `entidade_matching`;
- `tabela_midia`.

### 6.2 Divergência de identidade encontrada

Havia 2.283 pares distintos de `(id_artigo_scrape, pipeline_version)` na classificação:

| Condição | Quantidade |
|---|---:|
| existe outbox com o mesmo link | 583 |
| existe outbox para o id, mas o link é diferente | 1.700 |
| mais de um link interno na classificação para o mesmo par | 0 |

Esse resultado confirma que `id_artigo_scrape` não deve ser usado isoladamente para unir
as duas tabelas. A causa raiz exata da divergência — reutilização de id, bases de scraping
distintas ou outbox fora de sincronia — não foi confirmada neste teste.

Medida de proteção aplicada: somente entraram na amostra registros cujo
`id_artigo_scrape`, `pipeline_version` e `link_noticia` coincidiram exatamente e
cujas contagens de rótulos também coincidiram entre tabela e payload.

## 7. Contrato construído para o teste

| Necessidade do Analyser | Fonte utilizada | Decisão do teste |
|---|---|---|
| `ator.id` | `entity_id` da outbox | `122418060191` |
| `ator.tipo` | `entity_type` | `politico` |
| `ator.nome` | `entidade_matching.nome_principal` | nome oficial ativo |
| `item.id` | artigo + ator | `art_<artigo>__politico_<ator>` |
| `veiculo` | `tabela_midia.nome_midia` | ausente em parte da amostra; valor explícito de não identificado |
| `url` | `link_noticia` | somente link coincidente com a outbox |
| `data` | `data_publicacao_noticia` | convertida para `YYYY-MM-DD` |
| `titulo` | não disponível | título técnico sintético, identificado como tal |
| `texto` | `paragrafo_analisado` | concatenação apenas dos trechos do ator |
| `rotulo` | vários rótulos de trecho | maioria simples; empate resulta em neutro |

O rótulo e o score não foram inseridos no campo `texto`. Assim, o prompt de
`resumo_item` recebeu conteúdo e metadados editoriais, mas não recebeu a classificação
que poderia induzir o tom do resumo.

## 8. Amostragem

Foram escolhidos 12 artigos coerentes entre as duas fontes:

- 4 com rótulo agregado negativo;
- 4 com rótulo agregado positivo;
- 4 com rótulo agregado neutro;
- 43 trechos classificados no total.

A amostra foi balanceada para exercitar os três eixos. Ela não representa a distribuição
real do banco e não deve ser usada para calcular prevalência de sentimento.

O texto fornecido ao Analyser é formado somente pelos trechos classificados do ator.
Essa decisão reduz o consumo em relação a buscar o texto completo de cada notícia, mas
depende de o Classifier selecionar trechos com contexto suficiente.

## 9. Fluxo técnico executado

```text
PostgreSQL (READ ONLY)
  -> filtro por ator, agosto/2026 e coerência de link
  -> 43 trechos / 12 artigos
  -> adaptador local cria Rodada
  -> 12 chamadas resumo_item
  -> agrupamento Python: 4 negativo + 4 positivo + 4 neutro
  -> 3 chamadas consolidacao_lote (um lote por eixo)
  -> 3 chamadas consolidacao_final
  -> 3 panoramas versionados
  -> auditoria JSON
  -> relatório HTML
```

A CLI também executou uma chamada de preflight antes do pipeline. Essa chamada verifica
a conectividade e é zerada dos contadores antes da rodada; por isso não aparece nas 18
chamadas registradas na auditoria.

## 10. Resultado confirmado

| Métrica | Resultado |
|---|---:|
| itens de entrada | 12 |
| itens vindos do cache | 0 |
| `resumo_item` | 12 chamadas |
| `consolidacao_lote` | 3 chamadas |
| `consolidacao_final` | 3 chamadas |
| chamadas contabilizadas | 18 |
| panoramas finais | 3 |
| lotes por eixo | 1, 1 e 1 |
| tokens de prompt | 26.075 |
| tokens de resposta | 10.197 |
| erros | 0 |
| avisos de verificação | 1 |

O aviso foi:

```text
[2026-08/neutro] tema 'cenários eleitorais' declarou 5 ocorrências
e lista 4 fontes distintas; gravado 4
```

Esse caso comprova que a normalização de contagem torna uma divergência do modelo
observável e persiste a quantidade derivada das quatro fontes listadas. O teste não
prova que o nome do tema ou a associação semântica das fontes esteja correta; isso
continua dependendo da qualidade do modelo.

## 11. Resultado por eixo

Todos os detalhes estão em `saida/demo_banco_2026_08/panoramas.json` e são exibidos sem
truncamento no relatório HTML.

- **Negativo:** 4 matérias, formato detalhado, 4 temas, 4 fatos mais citados e
  5 registros de resposta.
- **Positivo:** 4 matérias, formato detalhado, 4 temas, 5 fatos mais citados e
  2 registros de resposta.
- **Neutro:** 4 matérias, formato compacto, texto corrido e 4 temas; não usa os campos
  detalhados de fatos e respostas.

Há itens semanticamente discutíveis nos eixos recebidos. Por exemplo, o conjunto positivo
inclui texto sobre investigação de Fábio Luís Lula da Silva e pesquisa com queda de
popularidade. Isso é evidência de limitação da classificação léxica anterior, não de uma
reclassificação feita pelo Analyser: o Analyser preservou os rótulos que recebeu.

## 12. Lote padrão 100

`LOTE_PADRAO` foi alterado de 5 para 100. A constante, a `Config` e a CLI agora
expõem o mesmo padrão e isso possui teste de regressão.

Critério em que a mudança ajuda: reduz o número de chamadas de consolidação de primeiro
nível quando muitos resumos pertencem ao mesmo ator, mês e eixo.

Custo introduzido: cada chamada pode carregar mais tokens. O Ollama informou contexto
efetivo de 16.384 tokens. Um lote com 100 resumos pode ultrapassar esse limite dependendo
do comprimento dos resumos e do prompt.

O teste atual confirma que `lote=100` foi aplicado e que grupos menores usam somente os
itens existentes. Ele não comprova que 100 resumos reais caibam no contexto. A validação
correta exige um teste separado com aproximadamente 100 resumos no mesmo eixo e medição
de tokens antes da chamada.

## 13. Relatório visual

O arquivo `relatorios/demo_banco_2026_08.html` apresenta:

- 6 das 12 entradas, apenas para legibilidade;
- 6 dos 12 `resumo_item`;
- os três lotes executados e suas fontes, cada um identificado como
  `Lote 1: 4 notícias` e separado da `Capacidade máxima configurada: 100`;
- os três panoramas finais completos;
- métricas, avisos e limitações;
- identificação explícita com `execucao_id`, `numero_rodada` e `arquivo_entrada`.

A auditoria histórica `auditoria_20260901T030503_871.json` não foi reescrita. Para essa
execução anterior ao contrato novo, o relatório deriva `execucao_id` do nome do arquivo,
`numero_rodada = 1` de sua posição cronológica e `arquivo_entrada` do campo legado
`rodada`. Auditorias futuras gravam esses três campos diretamente, além de
`capacidade_maxima_lote` e `lotes_processados`.

As `ConsolidacaoParcial` são objetos intermediários mantidos apenas em memória no código
atual. O relatório comprova as três chamadas pela auditoria e mostra suas fontes, mas não
fabrica o conteúdo textual das parciais. Persistir esse objeto é uma evolução possível se
o frontend precisar exibi-lo.

O navegador integrado não estava conectado à sessão, portanto a inspeção visual por
screenshot não pôde ser executada. A estrutura do HTML foi validada automaticamente,
mas aparência, quebra de linha e comportamento em navegadores reais permanecem como
validação visual pendente.

## 14. Arquivos e componentes

### Alterados

- `analyser/armazenamento.py`: numeração sequencial local das execuções;
- `analyser/pipeline.py`: identificação explícita e tamanho real dos lotes na auditoria;
- `analyser/config.py`: lote padrão 100;
- `README.md`: lote e links da demonstração;
- `docs/Prisma_Analyser_Documentacao.md`: exemplos e padrão 100;
- `docs/Auditoria_Integracao_Sistema_Completo.md`: limite atual e link desta evidência.
- `scripts/gerar_relatorio_demo.py`: apresentação separada de quantidade e capacidade;
- `testes/test_relatorio_demo.py`: validação estrutural dos novos campos visuais.

### Criados

- `dados/rodada_banco_demo_2026_08.json`;
- `dados/rodada_banco_demo_2026_08_metadados.json`;
- `saida/demo_banco_2026_08/resumos_item.json`;
- `saida/demo_banco_2026_08/panoramas.json`;
- `saida/demo_banco_2026_08/auditoria_20260901T030503_871.json`;
- `scripts/gerar_relatorio_demo.py`;
- `relatorios/demo_banco_2026_08.html`;
- `testes/test_config.py`;
- `testes/test_auditoria.py`;
- `testes/test_relatorio_demo.py`;
- este documento.

Nenhum arquivo dos projetos Scraper, Classifier, API ou frontend foi modificado.

## 15. Validação e comandos

### Suite automatizada

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp <pasta-temporaria-nova>
```

Resultado final:

```text
......................................... [100%]
41 testes aprovados
```

O teste `test_lote_padrao_e_cem_na_configuracao_e_na_cli` comprova a consistência do
padrão. Os testes do relatório comprovam:

- 6 entradas e 6 resumos visíveis;
- três registros de lote;
- quantidade real de notícias separada da capacidade máxima;
- `execucao_id`, `numero_rodada` e `arquivo_entrada` exibidos no relatório;
- auditorias legadas entram na numeração sem serem reescritas;
- três painéis finais;
- presença de todos os temas, fatos, respostas, fontes e avisos dos panoramas;
- escape de conteúdo externo contra injeção de HTML.

Esses testes não avaliam qualidade editorial nem substituem inspeção visual.

### Execução do modelo real

```powershell
.\.venv\Scripts\python.exe -m analyser.cli `
  --entrada dados\rodada_banco_demo_2026_08.json `
  --modelo api `
  --lote 100 `
  --saida saida\demo_banco_2026_08 `
  --prompts prompts
```

Resultado: código de saída zero, três panoramas e auditoria sem erros.

### Geração do relatório

```powershell
.\.venv\Scripts\python.exe scripts\gerar_relatorio_demo.py `
  --entrada dados\rodada_banco_demo_2026_08.json `
  --metadados dados\rodada_banco_demo_2026_08_metadados.json `
  --saida-analyser saida\demo_banco_2026_08 `
  --relatorio relatorios\demo_banco_2026_08.html
```

## 16. Decisões e alternativas

### Trechos relevantes ou texto completo

Decisão do teste: usar os trechos classificados do ator.

Alternativa: buscar o texto completo no Scraper. Isso oferece mais contexto, porém aumenta
transferência e tokens e repete conteúdo que o Classifier já considerou irrelevante.

A decisão é adequada para este teste, mas precisa de avaliação de cobertura: se um trecho
depender do parágrafo anterior para fazer sentido, somente o trecho pode ser insuficiente.

### Título sintético ou consulta adicional

Decisão do teste: título técnico explícito.  
Alternativa de produção: obter o título original do banco do Scraper ou incluí-lo no
contrato publicado pelo Classifier.

### Persistir ou não as parciais

Decisão: não modificar o pipeline para persistir as parciais.  
Motivo: o comportamento atual foi testado sem ampliar o contrato de persistência apenas
para a demonstração. Se a interface exigir esse nível, a mudança deve ser projetada e
testada como funcionalidade própria.

## 17. Riscos, limitações e casos de falha

1. Associação de `paragraph_index`: como a tabela não guarda o índice, o pareamento foi
   inferido por rótulo, score e ordem relativa. Scores duplicados podem tornar a associação
   ambígua.
2. Identidade do artigo: 1.700 divergências de link impedem união apenas por id.
3. Regra de maioria: pode apagar um trecho minoritário importante.
4. Classificador léxico: palavras positivas ou negativas podem não representar o sentido
   real da notícia.
5. Contexto: lote 100 pode exceder 16.384 tokens.
6. Veículo: parte da amostra não possui `id_midia`.
7. Título: o título sintético não deve ser mostrado ao cidadão como se fosse jornalístico.
8. Amostragem: o balanceamento artificial não representa frequências reais.
9. Browser: a página ainda precisa de inspeção visual em navegador conectado.

### Caso confirmado: artigos 1700 e 1703 no eixo positivo

Os artigos `art_1700__politico_122418060191` e
`art_1703__politico_122418060191` já entraram no Analyser com rótulo `positivo`.
Cada um possuía somente um trecho para Lula, e as linhas de classificação `35271` e
`35291` estavam registradas como positivas, com `score_confianca = 1`. Portanto, a regra
demonstrativa de maioria por artigo não mudou o resultado nesses dois casos.

A explicação reproduzível no código vigente está na granularidade da heurística léxica v1
do Classifier. `avançou` faz parte do léxico positivo; o algoritmo conta termos no
parágrafo inteiro e aplica a mesma categoria a cada entidade reconhecida naquele
parágrafo. No artigo 1700, o avanço é da comissão mista. No artigo 1703, quem avançou foi
Carlos Viana na coleta de assinaturas para investigar o filho de Lula. O código atual
confirma apenas que Lula foi mencionado; ele não atribui semanticamente a palavra
positiva a Lula.

Os metadados exportados não incluíram `metodo_classificacao`. A configuração vigente usa
`lexicon-baseline-v1`, e o resultado observado é exatamente reproduzido por esse léxico,
mas a consulta de confirmação dessa coluna nas duas linhas não pôde ser repetida nesta
revisão porque o daemon do Docker não estava acessível.

O banco armazenou esse resultado, mas não é a origem lógica da decisão. O Classifier
produziu o rótulo, `tabela_classificacao` o persistiu e o Analyser o preservou ao agrupar
em Python por `(ator_id, janela, eixo)`. A consolidação não moveu essas matérias para o
eixo positivo e também não tem autorização contratual para reclassificá-las.

### Consulta somente leitura para reproduzir o diagnóstico

As quatro estruturas consultadas foram `tabela_classificacao` (resultado autoritativo),
`classifier_analyser_outbox` (payload da notificação e `paragraph_index`),
`entidade_matching` (identidade legível do ator) e `tabela_midia` (veículo, quando
disponível). A consulta abaixo reproduz os artigos 1700 e 1703 para Lula e também calcula
o rótulo agregado usado pela demonstração:

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH classificacoes_alvo AS (
    SELECT tc.*
    FROM tabela_classificacao AS tc
    WHERE tc.nr_titulo_eleitoral_candidato = '122418060191'
      AND tc.id_artigo_scrape IN (1700, 1703)
),
contagens_artigo AS (
    SELECT
        ca.id_artigo_scrape,
        ca.pipeline_version,
        ca.link_noticia,
        COUNT(*) FILTER (
            WHERE ca.classificacao_sentimento = 'positivo'
        ) AS quantidade_positivos,
        COUNT(*) FILTER (
            WHERE ca.classificacao_sentimento = 'negativo'
        ) AS quantidade_negativos,
        COUNT(*) FILTER (
            WHERE ca.classificacao_sentimento = 'neutro'
        ) AS quantidade_neutros
    FROM classificacoes_alvo AS ca
    GROUP BY ca.id_artigo_scrape, ca.pipeline_version, ca.link_noticia
),
rotulo_artigo AS (
    SELECT
        c.*,
        CASE
            WHEN c.quantidade_positivos > GREATEST(
                c.quantidade_negativos, c.quantidade_neutros
            ) THEN 'positivo'
            WHEN c.quantidade_negativos > GREATEST(
                c.quantidade_positivos, c.quantidade_neutros
            ) THEN 'negativo'
            WHEN c.quantidade_neutros > GREATEST(
                c.quantidade_positivos, c.quantidade_negativos
            ) THEN 'neutro'
            ELSE 'neutro'
        END AS rotulo_item_agregado_demo
    FROM contagens_artigo AS c
),
outbox_flat AS (
    SELECT
        o.id AS outbox_id,
        o.id_artigo_scrape,
        o.pipeline_version,
        o.status AS outbox_status,
        o.attempts AS outbox_tentativas,
        o.notified_at AS outbox_notificado_em,
        o.created_at AS outbox_criado_em,
        o.payload ->> 'link_noticia' AS outbox_link_noticia,
        classificacao.ordem_no_payload,
        classificacao.item ->> 'entity_type' AS entity_type,
        classificacao.item ->> 'entity_id' AS entity_id,
        NULLIF(
            classificacao.item ->> 'paragraph_index', ''
        )::integer AS paragraph_index,
        classificacao.item ->> 'classificacao_sentimento'
            AS outbox_classificacao_sentimento,
        NULLIF(
            classificacao.item ->> 'score_confianca', ''
        )::double precision AS outbox_score_confianca
    FROM classifier_analyser_outbox AS o
    CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(o.payload -> 'classificacoes', '[]'::jsonb)
    ) WITH ORDINALITY AS classificacao(item, ordem_no_payload)
    WHERE EXISTS (
        SELECT 1
        FROM classificacoes_alvo AS ca
        WHERE ca.id_artigo_scrape = o.id_artigo_scrape
          AND ca.pipeline_version = o.pipeline_version
    )
)
SELECT
    ca.id_classificacao,
    ca.id_artigo_scrape,
    ca.pipeline_version,
    ca.link_noticia,
    ca.data_publicacao_noticia,
    ca.registro_classificacao,
    ca.nr_titulo_eleitoral_candidato AS ator_id,
    entidade.nome_principal AS ator_nome,
    midia.nome_midia AS veiculo,
    ca.classificacao_sentimento AS rotulo_do_trecho,
    ca.score_confianca,
    ROUND(ca.score_confianca::numeric * 100, 2)
        AS score_confianca_percentual,
    ca.metodo_classificacao,
    ca.paragrafo_analisado,
    ca.evidencia_match ->> 'tipo' AS tipo_match_entidade,
    ca.evidencia_match ->> 'alias' AS alias_reconhecido,
    NULLIF(ca.evidencia_match ->> 'score', '')::double precision
        AS score_match_entidade,
    ra.quantidade_positivos,
    ra.quantidade_negativos,
    ra.quantidade_neutros,
    ra.rotulo_item_agregado_demo,
    ob.outbox_id,
    ob.paragraph_index,
    ob.outbox_classificacao_sentimento,
    ob.outbox_score_confianca,
    ob.outbox_status,
    ob.outbox_tentativas,
    ob.outbox_notificado_em,
    (ob.outbox_id IS NOT NULL) AS pareamento_outbox_encontrado
FROM classificacoes_alvo AS ca
JOIN rotulo_artigo AS ra
  ON ra.id_artigo_scrape = ca.id_artigo_scrape
 AND ra.pipeline_version = ca.pipeline_version
 AND ra.link_noticia IS NOT DISTINCT FROM ca.link_noticia
LEFT JOIN LATERAL (
    SELECT em.nome_principal
    FROM entidade_matching AS em
    WHERE em.entity_type = 'politico'
      AND em.nr_titulo_eleitoral_candidato =
          ca.nr_titulo_eleitoral_candidato
      AND em.ativo = true
    ORDER BY em.atualizado_em DESC, em.id DESC
    LIMIT 1
) AS entidade ON true
LEFT JOIN tabela_midia AS midia
  ON midia.id_midia = ca.id_midia
LEFT JOIN outbox_flat AS ob
  ON ob.id_artigo_scrape = ca.id_artigo_scrape
 AND ob.pipeline_version = ca.pipeline_version
 AND ob.outbox_link_noticia IS NOT DISTINCT FROM ca.link_noticia
 AND ob.entity_type = 'politico'
 AND ob.entity_id = ca.nr_titulo_eleitoral_candidato
 AND ob.outbox_classificacao_sentimento
     IS NOT DISTINCT FROM ca.classificacao_sentimento
 AND ob.outbox_score_confianca IS NOT DISTINCT FROM ca.score_confianca
ORDER BY ca.id_artigo_scrape, ca.id_classificacao, ob.paragraph_index;

ROLLBACK;
```

O `score_confianca` e o `score_match_entidade` medem coisas diferentes. O primeiro vem
do classificador de sentimento; o segundo registra quão bem o nome ou alias foi associado
à entidade. Na baseline léxica, `score_confianca = 1` significa que os termos encontrados
apontaram todos para uma única direção do léxico. Não é uma probabilidade calibrada de
que o rótulo esteja semanticamente correto para Lula.

O pareamento com `paragraph_index` é seguro para esses dois artigos porque cada um possui
somente uma classificação de Lula. Em uma consulta genérica, duas linhas do mesmo artigo,
entidade, rótulo e score podem produzir correspondência ambígua: `tabela_classificacao`
não persiste `paragraph_index`, enquanto a outbox não carrega o texto do parágrafo.

## 18. Pendências e próximos passos

1. Definir formalmente a identidade canônica de notícia entre Scraper, Classifier e
   Analyser.
2. Corrigir ou explicar a divergência de links da outbox.
3. Persistir `paragraph_index` junto ao trecho classificado.
4. Definir a regra oficial de rótulo por artigo ou alterar o Analyser para trabalhar com
   unidade de trecho.
5. Disponibilizar título e veículo no contrato de integração.
6. Executar teste de estresse com 100 resumos no mesmo eixo e medição prévia de tokens.
7. Avaliar lote dinâmico por orçamento de tokens, em vez de depender apenas da quantidade.
8. Calibrar e avaliar a qualidade do classificador com conjunto rotulado por humanos e
   classificação direcionada à entidade, não apenas ao parágrafo inteiro.
9. Decidir se `ConsolidacaoParcial` precisa ser persistida para auditoria ou frontend.
10. Inspecionar o HTML em desktop e celular quando um navegador estiver conectado.
11. Implementar, em tarefa separada, o consumidor de fila e o contrato mínimo de API já
    descritos na auditoria canônica.

## 19. Rollback

Não existe rollback de banco, pois não houve escrita.

O rollback local consiste em:

- restaurar `LOTE_PADRAO` e os trechos documentais relacionados caso a decisão de 100
  seja cancelada;
- remover somente os novos arquivos de demonstração listados na seção 14;
- preservar todos os demais arquivos e saídas preexistentes.

Não se deve apagar `saida/` de forma ampla, porque ela pode conter resultados anteriores
do usuário.

## 20. Glossário

- **Outbox:** tabela de eventos prontos para notificação a outro componente.
- **Payload:** corpo estruturado do evento, neste caso um objeto JSON.
- **Schema:** contrato de campos e tipos aceitos.
- **Rastreabilidade:** capacidade de ligar uma conclusão às fontes que a sustentam.
- **Preflight:** chamada curta usada para verificar modelo e conectividade antes da rodada.
- **Token:** unidade de texto processada pelo modelo.
- **Lote:** subconjunto de resumos enviado a uma consolidação parcial.
- **Janela:** período mensal `YYYY-MM` derivado da data da notícia.
- **Eixo:** classificação negativa, positiva ou neutra.
- **Smoke test:** teste curto que verifica se o fluxo principal inicia e termina.

## 21. Como explicar em uma banca

“Nós testamos o Analyser com trechos reais do banco sem fazer qualquer escrita. Para não
confiar apenas no id do artigo, exigimos coincidência de artigo, versão do pipeline e URL,
porque a auditoria encontrou 1.700 casos em que a outbox tinha link diferente. Construímos
12 itens rastreáveis, quatro por eixo, e o modelo local gerou 12 resumos e três panoramas
finais. O teste mostrou que usar somente os trechos relevantes é tecnicamente viável e
mais econômico que buscar todo o texto, mas também revelou decisões pendentes: identidade
canônica, regra de rótulo por artigo, contexto perdido fora do trecho e capacidade real de
um lote com 100 resumos.” 
