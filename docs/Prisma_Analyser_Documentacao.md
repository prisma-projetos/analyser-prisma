# Prisma · Analyser — Documentação Técnica

Documento de referência sobre arquitetura, etapas do pipeline, fluxo de informação e dicionário dos campos de saída.

**Última atualização:** 2026-08-31

---

## 1. O que é o Analyser e onde ele fica

O **Prisma** coleta notícias sobre atores políticos brasileiros (pessoas ou partidos), classifica cada notícia como positiva, negativa ou neutra **em relação àquele ator**, e apresenta ao cidadão um panorama organizado e rastreável até a fonte original.

O pipeline completo do produto é:

Scraper → Messageria → **Classifier** → Messageria → **ANALYSER** → System DB → API → Frontend

Este repositório é **somente o Analyser**. Ele recebe notícias que **já chegam classificadas** e produz o resumo que o eleitor lê. Não há banco, fila, HTTP nem interface: os arquivos JSON em `saida/` fazem o papel do banco, e isso é proposital.

### A ideia central

Duas camadas, e a separação entre elas é o coração do módulo:

| Aspecto | resumos_item.json | panoramas.json |
|---|---|---|
| granularidade | um resumo por notícia | um registro por (ator, janela, eixo, versão) |
| custo | ALTO — pago uma vez por notícia | baixo |
| escrita | append-only, nunca recalculado | versão nova a cada recálculo |

A **janela** é o mês da notícia (formato YYYY-MM), derivado do campo `data` do item — a data da **notícia**, não a data em que ela chegou. Uma notícia de julho descoberta hoje pertence à janela de julho.

Disso decorre a propriedade que o protótipo existe para demonstrar: **uma notícia inédita é resumida uma única vez; uma repetição coerente do mesmo `item_id` usa o cache, enquanto uma classificação conflitante é rejeitada.**

### O alvo é um ator político, não um político

O alvo tanto pode ser uma pessoa quanto um partido. O módulo trata os dois igual — muda só o campo `tipo` na entrada, que aceita `politico` ou `partido`. Toda a persistência é chaveada por `ator_id`, e o prompt do resumo recebe o tipo junto do nome, para não escrever sobre um partido como se fosse uma pessoa.

---

## 2. As etapas do pipeline

### Rodada, execução e versão de panorama

Uma `Rodada` é o **lote de entrada de uma execução**. No protótipo, ela é representada por um arquivo JSON com exatamente um bloco `ator` e uma lista `itens`. A quantidade de itens é variável: uma rodada pode trazer uma notícia ou centenas delas, inclusive notícias de meses diferentes, desde que todas pertençam ao mesmo ator declarado no arquivo.

Rodada e panorama não são a mesma coisa, e também não existe correspondência de um para um entre eles. Uma única rodada pode tocar mais de uma janela e produzir vários panoramas, um para cada grupo não vazio `(ator_id, janela, eixo)` afetado.

Quando chega uma rodada posterior com notícias inéditas, o Analyser:

1. gera somente os resumos individuais que ainda não existem;
2. identifica quais janelas receberam novidade;
3. reúne todos os resumos individuais já armazenados de cada janela afetada;
4. agrupa pela classificação definitiva;
5. refaz as consolidações parciais e a final;
6. grava uma nova versão do panorama.

Portanto, **não é feito um novo resumo do panorama anterior**. O panorama anterior permanece guardado como versão histórica e nunca é usado como entrada. No experimento, `rodada_1.json` gera as versões 1 de agosto; depois, `rodada_2.json` constitui uma nova rodada e faz agosto ganhar versões 2 porque trouxe notícias inéditas daquele mês.

### Nota sobre a contagem: 6 ou 7 etapas?

O README e o código numeram explicitamente **seis** etapas — são os métodos `_etapa_1_e_2`, `_etapa_3_agrupar`, `_etapa_4_e_5` e `_etapa_6_verificar` em `analyser/pipeline.py`.

Na prática, uma execução completa tem **duas fases adicionais não numeradas**: a validação da entrada, que acontece antes da etapa 1, e a contabilização + gravação da auditoria, que acontece depois da etapa 6 (métodos `_contabilizar` e `_gravar_auditoria`). Quando se fala em "sete etapas", o sétimo passo é essa fase final de **contabilização e auditoria** — ela produz um arquivo de saída próprio e por isso é razoável contá-la como etapa.

Este documento descreve as sete fases na ordem em que rodam, marcando quais são numeradas no código.

---

### Etapa 0 — Validação da entrada (não numerada no código)

**Onde:** `Analyser.executar()`, primeira linha. **Chama o modelo:** não.

O arquivo da rodada é um arquivo JSON que representa um lote de notícias, não uma notícia isolada. Sua estrutura possui um bloco `ator` e uma lista `itens`; cada elemento dessa lista é uma notícia individual, validada pelo schema `ItemEntrada`. O schema Pydantic `Rodada` valida o arquivo inteiro, incluindo o ator e todos os itens. A validação é estrita (`extra="forbid"`): qualquer campo não previsto derruba a execução. Também é aqui que se rejeita uma rodada com ids de item repetidos.

Nos exemplos do projeto, `dados/rodada_1.json` contém 10 itens e `dados/rodada_2.json` contém 8 itens. O pipeline percorre essa lista com `for item in rodada.itens` e aplica o resumo individualmente a cada notícia inédita.

Se a entrada não passa, nada acontece — não existe processamento parcial de um arquivo malformado.

---

### Etapa 1 — Resumo por item (MAP) — NUMERADA

**Onde:** `_etapa_1_e_2` / `_resumir`. **Chama o modelo:** SIM, uma vez por notícia inédita. **Grava em:** `resumos_item.json`.

Para cada item da rodada, o Analyser pergunta: **já existe resumo para este id?**

- **Já existe** → não chama o modelo. O id vai para `ids_do_cache`. É aqui que mora a economia que o protótipo precisa demonstrar.
- **Não existe** → chama o modelo uma vez, gera resumo de 2–3 frases + temas + id da fonte, e grava. O id vai para `ids_resumidos_agora`.

**O ponto crítico desta etapa:** o prompt do resumo **não recebe o rótulo** do classificador. A função `montar_resumo_item()` sequer aceita rótulo como parâmetro — não há como vazá-lo por descuido. Isso mantém o resumo **imparcial**. No produto, o cidadão navega pelos agrupamentos e lê o resumo dentro daquele em que a matéria foi classificada. O resumo descreve a matéria; é o agrupamento que informa o eixo. Ele não pode assumir o tom do grupo que o hospeda.

O que entra no prompt: nome do ator, tipo do ator, id do item, veículo, data, url, título e o **texto integral** da notícia.

O que sai (schema `ResumoItem`): `resumo` (string), `temas` (1 a 6 strings), `fonte_id`.

---

### Etapa 2 — Marcação de janelas sujas — NUMERADA

**Onde:** `_etapa_1_e_2`, no mesmo laço da etapa 1. **Chama o modelo:** não além da chamada de resumo da etapa 1. **Grava em:** o campo `rotulo` de `resumos_item.json`, somente para itens inéditos.

O Analyser não classifica a notícia: o `rotulo` já vem do Classifier, que é um módulo anterior, e é tratado como definitivo. Antes de qualquer escrita, `_validar_classificacoes_imutaveis()` compara todo `item_id` já conhecido com o registro correspondente em `resumos_item.json`:

- mesmo id e mesmo rótulo → repetição coerente; usa o cache;
- mesmo id e rótulo diferente → `ConflitoClassificacao`; a rodada inteira é rejeitada;
- id inédito → resumo e rótulo são gravados juntos no registro imutável.

Uma janela entra na fila de recálculo somente quando recebe ao menos um **item inédito resumido com sucesso**. Uma janela sem novidade permanece exatamente como estava — nenhuma versão nova de panorama é criada para ela.

O motivo aparece na saída do terminal no formato `not_017 (item novo)`. A validação de conflitos ocorre antes do laço de gravação para uma rodada inválida não deixar resumos parcialmente persistidos.

#### Para que serve, na prática?

Uma **janela suja** não é um arquivo com defeito nem uma notícia inválida. É uma marca temporária de que o panorama derivado daquele mês ficou desatualizado porque recebeu uma notícia inédita. Ela implementa uma **invalidação seletiva** (decisão de refazer somente um resultado derivado cuja entrada mudou): em vez de consolidar todos os meses conhecidos a cada rodada, o Analyser consolida apenas os meses afetados.

A marca não é gravada como um novo arquivo permanente. Durante a execução, `_etapa_1_e_2()` reúne os meses em um conjunto (`set[str]`), para que vários itens de agosto, por exemplo, ainda provoquem apenas um recálculo de `2026-08`. Depois `executar()` percorre esse conjunto e chama `_recalcular_janela()` uma vez para cada mês. Os motivos ficam em `resultado.motivo_janela` para aparecer no terminal e na auditoria.

| Situação recebida na rodada | O que a etapa 2 decide | Consequência |
|---|---|---|
| `id` novo | A janela da data da notícia fica suja | O item é resumido e o panorama do mês é recalculado |
| Mesmo `id` e mesmo rótulo | A janela permanece limpa | O resumo e o panorama existentes são reaproveitados; não há nova chamada ao modelo |
| Mesmo `id` e rótulo diferente | A execução é rejeitada antes de qualquer escrita | O conflito fica explícito; nenhum resumo, panorama ou registro de auditoria da rodada é criado |

**Exemplo passo a passo:** `not_004`, publicada em agosto, aparece nas duas rodadas como `negativo`. Na segunda rodada, `_validar_classificacoes_imutaveis()` confirma que o valor coincide com o registro existente; o item entra em `ids_do_cache`, não é resumido novamente e não é a causa de `2026-08` ficar suja. Agosto é recalculado porque a rodada também contém seis notícias inéditas daquele mês.

**Analogia:** é como uma ficha arquivada com uma categoria definitiva. Reapresentar uma cópia idêntica permite localizar a ficha existente; apresentar a mesma identificação com outra categoria denuncia um conflito no documento de entrada. Tecnicamente, a “ficha” é o `RegistroResumo`, a “categoria” é `rotulo` e a conferência é `_validar_classificacoes_imutaveis()`. A analogia tem um limite: o sistema também não compara todo o conteúdo da notícia nessa conferência.

Há uma limitação deliberada: se uma notícia reaparecer com o mesmo `id` e o mesmo rótulo, mas com texto ou data alterados, a etapa 2 não detectará essa alteração. Esse comportamento é confirmado pelo código atual e exigiria uma regra adicional de versionamento ou hash do conteúdo caso atualizações editoriais precisem ser tratadas.

---

### Etapa 3 — Agrupamento — NUMERADA

**Onde:** `_etapa_3_agrupar`. **Chama o modelo:** não, Python puro.

Para cada janela suja, todos os resumos daquela janela são agrupados pela **classificação definitiva** armazenada no próprio `RegistroResumo`.

Dois detalhes importantes:

- A base é o **conjunto completo de resumos da janela**, não apenas os itens que vieram na rodada atual. O panorama é recalculado do zero, sempre.
- O agrupamento lê `registro.rotulo`; não existe consulta a uma camada mutável de rótulos.

Dentro de cada grupo, os resumos são ordenados por `item_id`, para que a saída seja determinística.

Não existe um número fixo de notícias por grupo. O agrupamento reúne todos os resumos conhecidos do mesmo ator, mês e classificação. O parâmetro `--lote` é uma divisão posterior para as chamadas de consolidação do modelo e vale 5 por padrão; portanto, um grupo com 8 notícias vira dois lotes (5 + 3). Na rodada 1, agosto tem três grupos: 6 negativos, 2 positivos e 2 neutros. Depois da rodada 2, agosto passa a ter 8 negativos, 4 positivos e 4 neutros; julho possui 1 negativo.

A quantidade de lotes é determinada por `ceil(quantidade_de_resumos / tamanho_do_lote)`, e o último lote recebe o restante. Por isso, um grupo com 6 notícias produz lotes de 5 e 1, enquanto um grupo com 4 produz um único lote de 4. No caso de julho na rodada 2, há apenas uma notícia naquele grupo, então o único lote contém 1 resumo.

---

### Etapa 4 — Consolidação em dois níveis (REDUCE) — NUMERADA

**Onde:** `_etapa_4_e_5`. **Chama o modelo:** SIM, em dois níveis.

**Nível 1 — por lote.** Os resumos do eixo são fatiados em lotes de tamanho `--lote` (padrão 5). Cada lote vira uma chamada ao modelo, que produz uma `ConsolidacaoParcial` com `temas`, `pontos` e `fontes`.

**Nível 2 — final.** Todas as parciais do eixo vão numa única chamada, que produz o `Panorama` daquele eixo naquela janela.

#### Relação entre resumo individual, parcial e final

| Produto | Entrada | Saída | Escopo preservado |
|---|---|---|---|
| `ResumoItem` | texto integral de uma notícia inédita | um resumo daquela notícia | uma notícia e um ator |
| `ConsolidacaoParcial` | um lote de até `--lote` resumos individuais | uma síntese parcial do lote | mesmo ator, mesma janela e mesmo eixo |
| `Panorama` | todas as consolidações parciais daquele grupo lógico | a consolidação final exibida ao usuário | mesmo ator, mesma janela e mesmo eixo |

Portanto, a sequência conceitual é **notícia → resumo individual → parcial do lote → panorama final**. A ressalva é que **grupo** e **lote** não são sinônimos: o grupo contém todos os resumos com a mesma identidade `(ator_id, janela, eixo)`, enquanto o lote é apenas uma fatia desse grupo usada para limitar cada chamada ao modelo. Por exemplo, um grupo com 7 notícias e `--lote 5` produz 7 resumos individuais, dois lotes (5 + 2), duas parciais e um panorama final. O nível final nunca mistura atores, meses ou eixos diferentes.

#### Exemplo incremental: 400 notícias anteriores e 500 novas

As 400 notícias anteriores não são resumidas novamente: seus `ResumoItem` são lidos de `resumos_item.json`. Somente as 500 notícias inéditas passam pela etapa de resumo. Em seguida, o recálculo não usa o panorama anterior nem consolida apenas as 500 novas; ele reúne os resumos antigos e novos pertencentes a cada grupo lógico e reconstrói o panorama desse grupo do zero.

Entretanto, dizer que “os 900 resumos entram todos juntos” só é correto se os 900 tiverem o mesmo ator, a mesma janela e o mesmo eixo. O exemplo hipotético abaixo supõe um único ator e um único mês, mas três classificações vigentes:

| Grupo `(ator_id, janela, eixo)` | Resumos anteriores | Resumos novos | Total do grupo | Lotes de até 5 | Parciais | Panoramas finais |
|---|---:|---:|---:|---:|---:|---:|
| `(ator_001, 2026-08, negativo)` | 180 | 220 | 400 | 80 | 80 | 1 |
| `(ator_001, 2026-08, positivo)` | 140 | 170 | 310 | 62 | 62 | 1 |
| `(ator_001, 2026-08, neutro)` | 80 | 110 | 190 | 38 | 38 | 1 |
| **Total** | **400** | **500** | **900** | **180** | **180** | **3** |

O agrupamento é feito em Python antes das chamadas de consolidação. Cada `ConsolidacaoParcial` recebe somente um lote já pertencente a uma única chave `(ator_id, janela, eixo)`, e a `consolidacao_final` recebe somente as parciais dessa mesma chave. As parciais são intermediárias da execução e são geradas novamente no recálculo; o Analyser não reutiliza uma parcial ou um panorama anterior como substituto dos resumos individuais.

Esse desenho evita a compressão acumulativa de um panorama antigo, mas não elimina toda perda de informação: cada consolidação feita pelo modelo ainda é uma síntese. Além disso, um grupo muito grande pode gerar tantas parciais que a chamada final se aproxime do limite de contexto do modelo. O protótipo atual possui dois níveis fixos e não implementa uma árvore de redução com profundidade dinâmica.

**Duas regras estruturais desta etapa:**

- A entrada são **sempre os resumos curtos** da etapa 1. Nunca os textos completos das notícias, e **nunca um panorama de rodada anterior** — o panorama jamais é insumo de si mesmo.
- É aqui, e só aqui, que o **rótulo entra no prompt**. A constante `ROTULO_NO_PROMPT = {"map": False, "reduce": True}` em `config.py` é a fonte única dessa regra. O eixo aparece no prompt apenas para identificar o recorte; o prompt **não instrui tom**, e o teste 2 cobra isso a cada execução.

---

### Etapa 5 — Versionamento — NUMERADA

**Onde:** `_etapa_4_e_5`, na gravação. **Chama o modelo:** não.

O panorama produzido é gravado como uma **versão nova**. As versões anteriores nunca são tocadas nem sobrescritas. O arquivo `panoramas.json` é uma lista que só cresce.

Assim, depois de duas rodadas, agosto tem tanto a versão 1 (produzida na rodada 1) quanto a versão 2 (produzida na rodada 2), lado a lado no mesmo arquivo. É possível auditar exatamente o que o cidadão lia antes da chegada das notícias adicionais.

---

### Etapa 6 — Verificação de contagem — NUMERADA

**Onde:** `_etapa_6_verificar`. **Chama o modelo:** não, Python puro.

Confere quatro coisas contra o conjunto de resumos que efetivamente entrou:

1. Todo id citado no panorama existe no conjunto de entrada?
2. O `total_itens` declarado bate com o tamanho real do conjunto?
3. Cada tema declara um número de `ocorrencias` igual ao número de fontes distintas que lista?
4. Existe algum item do conjunto que o panorama **não** cita em `fontes`?

**Divergência vira aviso, nunca correção.** O sistema não conserta a saída do modelo em silêncio. Os avisos ficam registrados em dois lugares: no campo `avisos` do próprio registro de panorama, e no campo `avisos_verificacao` da auditoria.

---

### Etapa 7 — Contabilização e auditoria (não numerada no código)

**Onde:** `_contabilizar` e `_gravar_auditoria`. **Chama o modelo:** não. **Grava em:** `saida/auditoria_<carimbo>.json`.

Duas coisas acontecem aqui.

**Contabilização:** para cada janela recalculada, conta-se quantos itens há por eixo e quantos há por veículo. Isso alimenta as tabelas impressas no terminal e serve para enxergar de relance se a cobertura está concentrada em um único veículo.

**Auditoria:** grava-se um arquivo por execução com tudo que é preciso para reproduzir e explicar aquela rodada — modelo usado, hash de cada prompt, ids de entrada, ids resumidos agora, ids vindos do cache, janelas recalculadas, chamadas por tarefa, tokens, avisos e erros.

---

## 3. O que vem do Classifier

Esta é uma distinção que costuma se perder, então vale explicitar: **não existe classificador neste repositório.**

O Classifier é um módulo **anterior** no pipeline do Prisma. O Analyser não o executa, não o consulta e não o verifica. O que existe aqui é apenas o **contrato de entrada** que o Classifier precisa cumprir.

### Como o rótulo chega até aqui, no protótipo

No sistema real, o Classifier publicaria em uma fila (Messageria) e o Analyser consumiria de lá. Neste protótipo, essa fronteira é simulada por um arquivo JSON: os campos `rotulo` que aparecem em `dados/rodada_1.json` e `dados/rodada_2.json` foram **escritos à mão** para representar o que o classificador teria decidido. Não há modelo de classificação rodando em lugar nenhum.

O item `not_004` aparece como `negativo` nas duas rodadas para demonstrar uma repetição coerente: o Analyser confere a classificação definitiva e reutiliza o resumo. Um rótulo diferente para esse mesmo id seria rejeitado como `ConflitoClassificacao`.

### O que o Analyser considera que vem do Classifier

O contrato está no schema `ItemEntrada` em `analyser/schemas.py`, e é estrito (`extra="forbid"` — qualquer campo a mais é rejeitado). São exatamente sete campos por notícia:

| Campo | Tipo | O que é |
|---|---|---|
| `id` | string | Identificador único da notícia. Vira `fonte_id` e é a base de toda a rastreabilidade |
| `veiculo` | string | Nome do veículo que publicou |
| `url` | string | Endereço da matéria original |
| `data` | date | Data de publicação da notícia. É dela que a janela é derivada |
| `titulo` | string | Título da matéria |
| `texto` | string | Texto integral da matéria |
| `rotulo` | positivo / negativo / neutro | A decisão do Classifier |

Mais um bloco por rodada, que identifica o alvo: `ator.id`, `ator.tipo` (`politico` ou `partido`) e `ator.nome`.

### O que o Analyser NÃO recebe e não usa

Vale registrar o que deliberadamente não faz parte do contrato: não há score de confiança, não há justificativa da classificação, não há nome do modelo classificador, não há data da classificação, não há classificação multi-ator. Se um dia esses campos passarem a existir, o `extra="forbid"` fará a entrada falhar até que o schema seja atualizado de propósito — que é o comportamento desejado.

### O que o Analyser deriva sozinho

Dois campos importantes não vêm do Classifier:

- **`janela`** — calculada a partir de `data` (`data.strftime("%Y-%m")`). O Classifier não decide a que período a notícia pertence.
- **`ator_id`** — vem do bloco `ator` da rodada, não de cada item.

### Decisão registrada: não há verificação do classificador

Decidido em 19/08/2026. O Analyser **confia no rótulo que recebe**: agrupa por ele, consolida por ele e conta por ele, sem segunda opinião. Em troca, o custo por notícia inédita caiu pela metade — uma chamada de modelo em vez de duas.

Se um dia o Classifier passar a errar de forma perceptível, o lugar de reintroduzir a conferência é na etapa 1, no nível do item, porque é o único ponto do pipeline que ainda enxerga o texto integral da notícia.

---

## 4. Não confundir: Classifier vs. ModeloFake

São duas coisas completamente diferentes, e o nome "fake" ajuda a confundir.

| | Classifier | ModeloFake |
|---|---|---|
| o que é | módulo externo do Prisma | implementação de LLM usada dentro do Analyser |
| existe neste repo? | não | sim, em `analyser/modelos.py` |
| o que produz | o campo `rotulo` de cada notícia | os textos de resumo e de consolidação |
| quando roda | antes do Analyser | nas etapas 1 e 4 |
| como é simulado | rótulos escritos à mão nos JSON de entrada | classe determinística e extrativa |

O **ModeloFake** é o modelo padrão do protótipo. Ele é determinístico, não usa rede e não exige chave, para que o repositório rode em qualquer máquina sem configuração. Tudo que ele escreve é **extraído do texto que recebeu**: não inventa, não opina, não sabe nada sobre o ator.

Como ele funciona, resumidamente:

- **Resumo de item:** pega a primeira frase da notícia e a frase que traz a manifestação de quem foi citado (procurada por verbos como "afirmou", "informou", "defendeu", "reconheceu"). Os temas saem por frequência de palavras com 5+ letras, descontadas listas de palavras vazias e numerais.
- **Consolidação de lote:** agrega os temas dos resumos do lote, contando quais ids sustentam cada um.
- **Consolidação final:** junta as parciais, corta os temas em 6 (formato detalhado) ou 4 (formato compacto), e monta os campos do formato pedido.

Ele também expõe contadores de chamada por tarefa, que são o que os testes usam para provar que a rodada 2 resumiu somente os sete itens inéditos e recuperou `not_004` do cache.

### Trocar para um modelo real

O `ModeloAPI` fala com qualquer API compatível com a da OpenAI. **Nenhum nome de modelo existe no código** — ele lê `BASE_URL`, `API_KEY` e `MODEL` do ambiente. Trocar de provedor é editar o `.env` e rodar com `--modelo api`. O modelo efetivamente usado vai para o registro de auditoria de cada execução.

#### Limites de tokens e de tempo com Ollama local

Há três limites diferentes, que não devem ser confundidos:

| Limite | Configuração atual | O que limita |
|---|---:|---|
| Contexto | `OLLAMA_CONTEXT_LENGTH=16384` em `compose.yml` | O total de tokens que o modelo mantém disponível para uma requisição: prompt, dados de entrada e resposta precisam caber nesse espaço. |
| Resposta por tarefa | `3000` (`resumo_item`), `6000` (`consolidacao_lote`) e `8000` (`consolidacao_final`) em `MAX_TOKENS_POR_TAREFA` | O teto enviado pelo Analyser em `max_tokens` para a geração de cada chamada. É um máximo, não uma quantidade que o modelo necessariamente usará. |
| Tempo por chamada HTTP | `TIMEOUT_SEGUNDOS=900` no `.env` | Até 15 minutos para uma chamada ao servidor de modelo responder. Não é o tempo máximo da execução inteira. |

Uma execução do pipeline pode ultrapassar 15 minutos, porque as chamadas ao modelo são sequenciais: cada resumo novo, cada consolidação de lote e cada consolidação final tem seu próprio timeout. O campo `OLLAMA_KEEP_ALIVE=30m` também **não** é um timeout; ele apenas mantém o modelo carregado na memória por até 30 minutos após o uso, reduzindo o custo de carregá-lo novamente.

No Ollama local não existe, por si só, uma cota mensal de tokens como haveria em uma API paga. Os limites reais passam a ser contexto, memória RAM/VRAM, velocidade do hardware e os tetos configurados acima. A auditoria registra os tokens de prompt e resposta de cada execução, permitindo medir esse consumo. O contexto efetivo deve ser conferido com `docker compose exec modelo ollama ps`, pois um `num_ctx` definido no próprio modelo pode ter precedência sobre a configuração do servidor.

---

## 5. O que vai para o usuário e o que é só registro

Esta é a resposta direta: **dois tipos de arquivo são de consumo e um é de infraestrutura.**

| Arquivo | Destino | Papel |
|---|---|---|
| `resumos_item.json` | **Usuário** | O resumo de cada notícia que o cidadão lê |
| `panoramas.json` | **Usuário** | O panorama do período que o cidadão lê |
| `auditoria_*.json` | **Só registro** | Nunca é exibido. Existe para reproduzir e explicar uma execução |

### Consolidação final e panorama não são duas etapas sucessivas

`ConsolidacaoParcial`, `consolidacao_final` e `Panorama` representam coisas de naturezas diferentes:

- `ConsolidacaoParcial` é o resultado intermediário de um lote e existe somente durante o recálculo;
- `consolidacao_final` é o nome da tarefa/chamada ao modelo que recebe todas as parciais de um grupo;
- `Panorama` é o objeto final devolvido por essa chamada, validado pelo schema `Panorama`;
- `panoramas.json` guarda esse `Panorama` dentro de um `RegistroPanorama`, junto com versão, hashes, modelo, quantidade de lotes e demais metadados.

Assim, não existe uma quarta sumarização depois da consolidação final. A relação correta é **parciais → chamada de consolidação final → objeto Panorama → gravação em `panoramas.json`**.

Sua intuição estava certa sobre a auditoria: ela é registro puro. Mas vale a ressalva sobre os outros dois: **`panoramas.json` e `resumos_item.json` são de consumo, e não de registro** — só que nem todo campo dentro deles é para o usuário.

### Dentro de panoramas.json: o que é conteúdo e o que é metadado

Cada entrada de `panoramas.json` tem duas partes. O objeto `panorama` aninhado é **o conteúdo**; tudo em volta dele é metadado técnico.

**Vai para o usuário** (dentro de `panorama`): `total_itens`, `temas`, `fontes`, `texto`, `fatos_mais_citados`, `respostas_registradas`.

**Metadado técnico** (fora de `panorama`): `versao`, `formato`, `avisos`, `lotes`, `modelo`, `hash_prompt_lote`, `hash_prompt_final`, `hash_prompt_formato`, `rodada`, `criado_em`.

Uma nuance de produto: o frontend deve exibir apenas a **versão mais alta** de cada (ator, janela, eixo). As versões antigas continuam no arquivo, mas para auditoria — não para leitura.

### Dentro de resumos_item.json

**Vai para o usuário:** `resumo`, `temas`, `titulo`, `veiculo`, `url`, `data`.

**Metadado técnico e de agrupamento:** `modelo`, `hash_prompt_resumo`, `criado_em`, `fonte_id`, `janela`, `ator_id`, `rotulo`.

### Onde fica a classificação definitiva

Não existe `rotulos.json` no fluxo atual. O campo `rotulo` fica no próprio `RegistroResumo`, porque resumo e classificação são imutáveis para um `item_id`. A resposta `ResumoItem` gerada pelo modelo continua contendo somente `resumo`, `temas` e `fonte_id`; o pipeline anexa o rótulo recebido do Classifier apenas ao construir o registro persistido.

A etapa `_etapa_3_agrupar` lê diretamente `registro.rotulo`. No exemplo persistido, `not_004` possui um único registro com `rotulo: "negativo"` e aparece no panorama negativo das versões 1 e 2. Não existe histórico nem atualização de rótulo. O arquivo `saida/rotulos.json` e o schema `RegistroRotulo` foram removidos em 31/08/2026.

---

## 6. Dicionário de campos da auditoria

Tomando como exemplo `saida/auditoria_20260831T200014_719.json`, que é o registro da rodada 2.

### Identificação da execução

**`timestamp`** — Quando a execução rodou, em ISO 8601. Exemplo: `2026-08-31T20:00:14+00:00`.

**`rodada`** — Nome do arquivo de entrada processado. Exemplo: `rodada_2.json`. Aparece também dentro de cada registro de panorama, permitindo saber qual execução produziu qual versão.

**`ator_id`** — Identificador do alvo daquela rodada. Exemplo: `ator_001`.

**`modelo`** — Qual implementação de modelo rodou. `fake` no padrão; com `--modelo api`, o valor de `MODEL` do `.env` — isto é, o nome real do modelo do provedor. É o que permite saber, meses depois, com que modelo um texto foi gerado.

**`lote`** — Tamanho do lote usado no primeiro nível do reduce. Exemplo: `5`. Afeta diretamente quantas chamadas de `consolidacao_lote` acontecem.

**`prompts`** — Hash SHA-256 de cada arquivo de prompt vigente na execução. Todos os cinco são pré-carregados de propósito, mesmo os que não foram exercitados, porque o que interessa auditar é o **conjunto de prompts vigente**. Se um prompt for editado, o hash muda, e fica registrado que aquela execução usou um texto diferente. É o que torna uma execução antiga reproduzível.

### Rastreamento de itens

**`ids_entrada`** — Todos os ids que vieram no arquivo da rodada, novos ou não. Na rodada 2: oito ids (`not_011` a `not_017`, mais `not_004`).

**`ids_resumidos_agora`** — Os ids que exigiram chamada ao modelo nesta execução, por serem inéditos. Na rodada 2: sete (`not_011` a `not_017`). Este número é exatamente o valor de `chamadas_por_tarefa.resumo_item`.

**`ids_do_cache`** — Os ids que já tinham resumo gravado e por isso **não** custaram nada. Na rodada 2: um (`not_004`). Junto com o campo anterior, é a medida direta da economia: 7 pagos, 1 de graça.

Não existe campo `itens_reclassificados`: o contrato não admite reclassificação. `not_004` aparece apenas em `ids_do_cache`, pois repetiu a mesma classificação definitiva.

### Rastreamento do recálculo

**`janelas_recalculadas`** — Os períodos que ficaram sujos e foram reprocessados do zero. Na rodada 2: `2026-07` e `2026-08`. Julho entrou porque `not_017` é uma notícia de julho descoberta agora; agosto entrou por conter seis itens inéditos.

**`panoramas_gravados`** — Um registro por panorama produzido, com `janela`, `eixo`, `versao` e `lotes`. Na rodada 2 foram quatro:

| janela | eixo | versão | lotes |
|---|---|---|---|
| 2026-07 | negativo | 1 | 1 |
| 2026-08 | negativo | 2 | 2 |
| 2026-08 | positivo | 2 | 1 |
| 2026-08 | neutro | 2 | 1 |

Lê-se assim: julho negativo é a **primeira** versão (a janela nunca existiu antes); as três de agosto são **segundas** versões, porque a rodada 1 já tinha produzido a versão 1 de cada. E agosto negativo precisou de **2 lotes** porque tinha 8 itens, acima do lote de 5.

Repare no que **não** está na lista: não há `2026-07` positivo nem neutro. Julho só tinha uma notícia, negativa — eixos vazios não geram panorama.

### Custo

**`chamadas_por_tarefa`** — Quantas vezes o modelo foi chamado, por tipo de tarefa. Na rodada 2:

| tarefa | chamadas | etapa | de onde vem o número |
|---|---|---|---|
| `resumo_item` | 7 | 1 | um por notícia inédita |
| `consolidacao_lote` | 5 | 4, nível 1 | 1 (jul/neg) + 2 (ago/neg) + 1 (ago/pos) + 1 (ago/neu) |
| `consolidacao_final` | 4 | 4, nível 2 | um por panorama gravado |

**`chamadas_total`** — Soma das anteriores: 16.

**`tokens`** — Contadores de `prompt` (enviados) e `resposta` (recebidos). Com o `ModeloFake` são estimativas grosseiras (comprimento dividido por 4), só para o campo existir. Com `--modelo api`, são os números reais devolvidos pelo provedor no bloco `usage`, e servem para estimar custo.

### Qualidade da execução

**`avisos_verificacao`** — O resultado da etapa 6. Lista de strings no formato `[janela/eixo] descrição`. Vazio significa que todo id citado existe, todas as contagens batem e nenhum item ficou de fora. Exemplos do que apareceria aqui:

- `ids citados que não existem no conjunto de entrada: not_999` — o modelo inventou uma fonte
- `total_itens declarado é 7, mas o conjunto de entrada tem 6 itens` — o modelo contou errado
- `tema 'contrato' declara 3 ocorrências, mas lista 2 fontes distintas` — contagem interna inconsistente
- `itens do conjunto que o panorama não cita em fontes: not_013` — uma notícia foi ignorada na consolidação

São **avisos**, não erros: o panorama é gravado assim mesmo, com o aviso anexado. A verificação denuncia; não conserta.

**`erros`** — Este é o campo mais severo do arquivo. Registra falhas que **impediram** a produção de alguma peça. Vazio significa execução limpa.

Um erro entra aqui quando a saída do modelo não passa na validação Pydantic **nem depois da tentativa de reparo** (a regra 7: uma nova tentativa, depois erro registrado — nunca um conserto silencioso). Há dois formatos:

- `not_014: <mensagem>` — falhou o resumo daquele item. O item fica sem resumo e não entra em nenhum panorama.
- `2026-08/negativo: <mensagem>` — falhou a consolidação daquele eixo. Nenhuma versão nova é gravada para ele; a versão anterior continua sendo a vigente.

Em ambos os casos o pipeline **continua** — o erro é isolado ao item ou ao eixo afetado, e o resto da execução é concluído. O CLI encerra com código de saída 1 quando há qualquer erro, e 0 quando não há.

---

## 7. Dicionário de campos do panorama

O schema `Panorama` é **único para os três eixos**, com os campos não usados ficando vazios. Isso é deliberado: o frontend tem um contrato só, e **nenhum nome de campo sinaliza polaridade** — todos descrevem a cobertura e servem igualmente a qualquer eixo.

**`ator_id`**, **`janela`**, **`eixo`** — Identificam o recorte: de quem, de quando, de qual agrupamento.

**`total_itens`** — Quantas notícias compõem este panorama. Conferido na etapa 6.

**`temas`** — Lista de assuntos recorrentes. Cada um traz `tema` (a palavra), `ocorrencias` (quantas notícias o sustentam) e `fontes` (os ids dessas notícias). Limitado a 6 no formato detalhado e 4 no compacto. É o que permite ao leitor ver do que a cobertura tratou, com o caminho de volta para as matérias.

**`fontes`** — Todos os ids que compõem o panorama. É a lista completa de rastreabilidade.

**`texto`** — Preenchido **apenas no formato compacto** (eixo neutro); fica `null` no detalhado. É a narrativa de 2–3 frases. Exemplo real: *"A cobertura do período reúne 4 matérias sobre o alvo (not_009, not_010, not_013, not_015). Os assuntos mais frequentes são ajuste, audiência. As matérias descrevem os fatos sem que os veículos registrem contestação."*

**`fatos_mais_citados`** — Preenchido **apenas no formato detalhado**; fica `[]` no compacto. Lista de até 5 fatos, cada um com `descricao` e as `fontes` que o sustentam. Nenhuma afirmação existe sem id que a ancore.

**`respostas_registradas`** — Preenchido **apenas no formato detalhado**. Registra se a cobertura traz manifestação de quem foi citado: `citado`, `houve_resposta` (booleano) e `fontes`. Tipicamente aparecem duas entradas por panorama — uma com `houve_resposta: true` e outra com `false`, cada uma listando os ids correspondentes.

Este campo é importante para a leitura crítica: ele mostra em quantas matérias o alvo teve espaço de resposta, sem que o Analyser precise julgar se a resposta foi boa.

### Os formatos, e por que os arquivos têm esses nomes

| Eixo | Formato | Estrutura |
|---|---|---|
| negativo | detalhado | campos estruturados: temas, fatos, respostas |
| positivo | detalhado | campos estruturados: temas, fatos, respostas |
| neutro | compacto | texto corrido de 2–3 frases |

Repare onde cai a divisão: **negativo + positivo de um lado, neutro do outro**. Não é negativo contra positivo. É "campos estruturados detalhados" contra "texto curto".

Os fragmentos de prompt são nomeados pelo **formato**, nunca pelo eixo: `prompts/formatos/detalhado.txt` é usado por negativo E positivo — o mesmo arquivo, literalmente.

O motivo: se houvesse um `eixo_negativo.txt` e um `eixo_positivo.txt`, os dois nasceriam idênticos, mas nada impediria alguém, meses depois e de boa fé, de editar um para "seja mais incisivo" e o outro para "destaque as entregas". A partir daí, a diferença de tom que o cidadão lê viria do **prompt**, não das notícias: o Analyser passaria a **fabricar** a polaridade em vez de relatá-la. Compartilhar o mesmo arquivo torna essa assimetria impossível por descuido. É estrutura, não disciplina.

---

## 8. Rodada 1 e rodada 2: o experimento

As duas rodadas não são apenas dados de exemplo — elas formam um **experimento desenhado** para provar as propriedades do módulo. Rodam em sequência, com a segunda dependendo do estado deixado pela primeira.

Comando:

```
python -m analyser.cli --entrada dados/rodada_1.json
python -m analyser.cli --entrada dados/rodada_2.json
```

### Rodada 1 — o estado inicial

10 itens (`not_001` a `not_010`), todos de agosto de 2026, com rótulos desbalanceados de propósito: **6 negativos, 2 positivos, 2 neutros**.

O que acontece: `saida/` está vazio, então os 10 itens são inéditos e os 10 são resumidos — 10 chamadas ao modelo na etapa 1. A janela `2026-08` fica suja e os três eixos são consolidados, gerando três panoramas, todos na **versão 1**.

### Rodada 2 — as três provas

8 itens, escolhidos para provar três coisas distintas:

| Prova | Itens | O que demonstra |
|---|---|---|
| Cache | `not_011` a `not_016` (6 novos de agosto) | Itens novos custam uma chamada cada |
| Janela vem da notícia | `not_017` (novo, de **julho**) | Notícia antiga descoberta hoje cria a janela de julho, não a de agosto |
| Cache com classificação imutável | `not_004` (já existia e continua `negativo`) | Repetição coerente reaproveita o resumo; valor diferente seria rejeitado |

O que acontece:

1. Sete itens são inéditos → 7 resumos gerados. `not_004` já tinha resumo e repete `negativo` → vem do cache, **zero chamadas**.
2. Os seis itens inéditos de agosto sujam `2026-08`; `not_017`, inédito e datado de julho, suja `2026-07`.
3. Julho é agrupado e tem apenas um item, negativo. Agosto é reagrupado **do zero**, mantendo `not_004` no grupo negativo.
4. Quatro panoramas são gravados: julho negativo na versão 1, e os três de agosto na versão 2.
5. As versões 1 de agosto continuam intactas no arquivo.

### O que se lê na comparação

Comparando as duas versões de `2026-08` no `panoramas.json`:

| | v1 (rodada 1) | v2 (rodada 2) |
|---|---|---|
| negativo | 6 itens | 8 itens |
| positivo | 2 itens | 4 itens |
| neutro | 2 itens | 4 itens |

O eixo negativo ganhou dois itens novos (`not_011`, `not_014`) e manteve `not_004`. O resumo de `not_004` é byte a byte o mesmo de antes, e seu campo `rotulo` continua `negativo`.

### Os itens plantados de propósito

Alguns itens existem para testar situações difíceis: `not_003` é rotulado negativo mas tem texto equilibrado, com direito de resposta exercido; `not_008` menciona dois políticos sendo só um o alvo; `not_013` é uma nota protocolar sem substância; `not_005` e `not_006` cobrem o **mesmo fato** por veículos diferentes.

Os textos também evitam de propósito os radicais "positiv", "negativ" e "neutr" — o teste 1 renderiza o prompt do map com os dados reais e falharia se a palavra chegasse ao modelo por dentro da notícia.

### Nota sobre os dados

Ator político, município e veículos são **inventados**. Ariel Fontenele Dourado é uma personagem de ficção, prefeita do município fictício de Vila Serena do Norte; os sete veículos não correspondem a nenhum jornal real.

---

## 9. Arquitetura

### Organização do código

```
analyser/
  config.py         constantes das regras, Config, leitor de .env
  schemas.py        Pydantic: entrada, resumo, parcial, panorama
  modelos.py        Protocol + ModeloFake + ModeloAPI + validação com retry
  armazenamento.py  JSON, append-only, versionamento
  prompts.py        carregamento e hash SHA-256
  pipeline.py       as etapas
  saida.py          impressão com rich
  cli.py            ponto de entrada

prompts/            todos os prompts, carregados em runtime — nada hardcoded
  resumo_item.txt
  consolidacao_lote.txt
  consolidacao_final.txt
  formatos/detalhado.txt    usado por negativo E positivo
  formatos/compacto.txt     usado por neutro

dados/              rodada_1.json e rodada_2.json
saida/              resumos_item, panoramas, rotulos, auditoria_*
testes/
```

### Decisões estruturais

**O modelo fica atrás de um Protocol.** `Modelo` define o contrato mínimo (`gerar`, `nome`, contadores). `ModeloFake` e `ModeloAPI` o implementam. O pipeline não sabe qual está usando.

**Nenhum prompt é hardcoded.** Todos vivem em `prompts/*.txt` e são carregados em runtime, com hash SHA-256 registrado.

**Nenhum nome de modelo existe no código.** `ModeloAPI` lê `MODEL` do ambiente e falha com mensagem clara se não estiver definido.

**Todos os caminhos são injetáveis.** `Config` recebe `saida` e `prompts` como parâmetros, para que os testes apontem para um diretório temporário sem tocar em `saida/`.

**A validação é uma camada separada.** `gerar_validado()` envolve qualquer modelo: chama, extrai o JSON (tolerando cercas de código), valida contra o schema; se falhar, tenta **uma vez** mais com uma dica de reparo anexada; se falhar de novo, levanta `ErroDeValidacao`, que o pipeline captura e registra em `erros`.

### Fluxo de informação, do começo ao fim

```
    dados/rodada_N.json
            |
            v
  [0] Validação Pydantic (Rodada)
            |
            v
  [1] Para cada item:
        ja tem resumo? --sim--> ids_do_cache (0 chamadas)
            |
           nao
            |
            v
        MODELO: resumo_item ---> grava em resumos_item.json (append-only)
            |
            v
  [2] id conhecido com outro rotulo? --sim--> rejeita a rodada
            |
           nao; item novo? -----------> marca janela como suja
            v
  [3] Para cada janela suja:
        agrupa TODOS os resumos da janela por rotulo definitivo
        (Python puro, sem modelo)
            |
            v
  [4] Para cada eixo nao vazio:
        nivel 1: MODELO: consolidacao_lote (um por lote de 5)
        nivel 2: MODELO: consolidacao_final (um por eixo)
            |
            v
  [5] grava panorama com versao = anterior + 1
            |                     em panoramas.json
            v
  [6] verificacao de contagem (Python puro)
        divergencia --> aviso anexado, nunca correcao
            |
            v
  [7] contabiliza por eixo e por veiculo
      grava auditoria_<carimbo>.json
            |
            v
   impressao no terminal (rich)
```

### As três saídas

```
  saida/
    resumos_item.json  -->  USUARIO   resumo + rotulo, append-only
    panoramas.json     -->  USUARIO   versionado, so a ultima versao e exibida
    auditoria_*.json   -->  registro  um por execucao, nunca exibido
```

---

## 10. As regras invioláveis

1. **O rótulo entra no prompt da consolidação, nunca no do resumo por item.** Constante única em `config.py`: `ROTULO_NO_PROMPT = {"map": False, "reduce": True}`. A função que monta o prompt do map nem recebe rótulo como parâmetro.
2. **A classificação é imutável.** O rótulo fica no `RegistroResumo`; um valor conflitante para o mesmo id rejeita a rodada antes de qualquer escrita.
3. **`resumos_item.json` é append-only.** `gravar_resumo()` levanta exceção se o id já existe, e não existe `atualizar_resumo` nem `apagar_resumo`.
4. **O panorama nunca é insumo de si mesmo.** A consolidação parte sempre dos resumos.
5. **Grounding estrito.** Os prompts proíbem conhecimento externo sobre o ator.
6. **Rastreabilidade.** Toda afirmação referencia os ids que a sustentam.
7. **A polaridade é da cobertura, não do ator.** Os textos dizem o que *as matérias* tratam; nunca julgam quem elas citam.
8. **Saída estruturada obrigatória**, validada com Pydantic. Uma tentativa de reparo, depois erro registrado.
9. **`temperature = 0`.** Determinismo é requisito, não preferência.

### Decisão registrada: o rótulo entra no prompt da consolidação

Decidido em 18/08/2026. O que se perde, para constar:

- A coerência entre o texto do panorama e o rótulo deixa de ser evidência independente. Se o eixo negativo sai duro, não dá mais para saber se é porque as notícias são duras ou porque o prompt informou que aquele conjunto é o negativo.
- Some a possibilidade de detectar erro de classificação a partir do texto consolidado.

Mitigações em vigor: o rótulo fica fora do map, o prompt do reduce não instrui tom, e o teste 2 cobra isso automaticamente.

### Decisão registrada: a classificação é definitiva

Decidido em 31/08/2026. O rótulo passou a integrar o `RegistroResumo` append-only e a camada `rotulos.json` foi removida. A mesma notícia pode reaparecer com o mesmo rótulo para usar o cache; outro valor para o mesmo `item_id` levanta `ConflitoClassificacao` antes de qualquer escrita.

**Benefícios:** elimina o estado mutável e o histórico de classificação, simplifica o agrupamento e torna explícita uma violação do contrato de entrada.

**Custo:** o Analyser deixa de oferecer correção de rótulo. Se o produto admitir revisões futuras, será preciso definir uma nova identidade para a notícia corrigida ou um protocolo de revisão antes de reintroduzir essa funcionalidade. Essa possibilidade é evolução futura, não comportamento implementado.

#### Compatibilidade, migração e rollback

Arquivos `resumos_item.json` produzidos pelo contrato anterior não possuem o campo `rotulo` e não são aceitos silenciosamente. `Armazenamento.ler_resumos()` levanta `SaidaLegadaIncompativel` com uma mensagem de migração antes de o pipeline escrever qualquer dado. O sistema não copia automaticamente o valor de um `rotulos.json` antigo porque um histórico com mais de um valor não informa, sozinho, qual deles deve se tornar definitivo sob a nova regra.

A saída rastreada em `saida/` foi regenerada a partir das duas rodadas atualizadas: os 17 registros agora contêm `rotulo`, e `rotulos.json` foi removido. A saída local `saida_qwen/` foi deliberadamente preservada no formato legado, pois contém resultados de modelo real e exige uma escolha explícita entre migração orientada pelo domínio e nova geração.

Para migrar outra saída, há duas alternativas: regenerá-la a partir de entradas compatíveis com o contrato imutável ou adicionar a cada resumo um rótulo definitivo escolhido de forma auditável, preservando antes uma cópia dos artefatos antigos. Para rollback dos arquivos rastreados, o histórico do Git conserva o schema e as saídas anteriores; diretórios ignorados pelo Git precisam de backup próprio antes de qualquer migração.

---

## 11. Testes

| # | Arquivo | O que trava |
|---|---|---|
| 1 | `test_vazamento_rotulo.py` | O prompt do map, com dados reais, não menciona rótulo |
| 2 | `test_vazamento_rotulo.py` | O prompt do reduce nomeia o eixo mas não instrui tom |
| 3 | `test_cache.py` | A rodada 2 só chama o modelo para os itens inéditos |
| 4 | `test_classificacao_imutavel.py` | Repetição coerente usa cache; rótulo conflitante rejeita a rodada sem escrita parcial |
| 5 | `test_versionamento.py` | Agosto fica em v2, julho em v1, e a v1 continua íntegra |
| 6 | `test_schemas.py` | Saída malformada é rejeitada, com um retry e depois erro |

Rodar: `python -m pytest`

---

## 12. Fora de escopo

Nenhum visualizador, dashboard ou frontend. Nenhuma integração com RabbitMQ, Kafka ou Postgres. Nenhuma API HTTP. Nenhum scraper e nenhum classificador. Nenhum Docker.
