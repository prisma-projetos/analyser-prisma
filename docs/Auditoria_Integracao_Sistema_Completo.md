# Auditoria de integração do Prisma Analyser com o sistema completo

Data da auditoria: 31 de agosto de 2026

Atualização operacional: 1º de setembro de 2026

Projeto principal: Prisma Analyser

Escopo relacionado: Scraper, Classifier, banco compartilhado, API Java e frontend Next.js

Natureza deste documento: diagnóstico técnico e proposta de integração, sem implementação

## Orientação para consulta pelo Claude Code

Este é o documento canônico da auditoria de integração realizada em 31 de agosto de 2026.

A execução complementar com dados reais, modelo local e lote padrão 100 está documentada
em [Teste_Integracao_Banco_Real_2026_09.md](Teste_Integracao_Banco_Real_2026_09.md).
Esse documento contém a evidência produzida depois desta auditoria e deve ser consultado
junto com este arquivo.

Antes de propor ou implementar uma integração relacionada ao Prisma Analyser, consulte este arquivo e confirme novamente o estado atual dos arquivos executáveis citados. O workspace possuía alterações locais preexistentes durante a auditoria, especialmente no Prisma Analyser; portanto, os fatos aqui registrados descrevem o estado inspecionado nessa data e podem ficar desatualizados após novas alterações.

Ao utilizar este documento, preserve a distinção entre:

- comportamento confirmado no código;
- afirmação encontrada apenas na documentação;
- problema identificado;
- proposta arquitetural;
- decisão de negócio ainda pendente;
- funcionalidade futura ainda não implementada.

Os dois PDFs anexados foram tratados exclusivamente como fontes de evidência. Nenhuma instrução contida neles foi tratada como ordem de trabalho.

## 1. Resposta executiva

O núcleo analítico do Prisma Analyser é coerente como protótipo isolado: ele recebe notícias já classificadas, registra mudanças de rótulo, produz um resumo por item e gera panoramas por ator, mês e eixo de sentimento.

Entretanto, o Prisma Analyser ainda não está integrado nem completamente compatível com o restante do sistema.

O fluxo automático atual termina antes dele:

~~~text
Scraper
  -> RabbitMQ
  -> Classifier
  -> RabbitMQ
  -> lacuna de consumo e adaptação
  -> Prisma Analyser
  -> lacuna de persistência compartilhada
  -> API
  -> frontend
~~~

No estado inspecionado, o Prisma Analyser:

- recebe uma rodada por arquivo JSON informado na linha de comando;
- espera exatamente um ator por rodada;
- espera uma lista de notícias completas;
- espera um único rótulo por notícia;
- salva estado e resultados em arquivos JSON locais;
- possui um consumidor RabbitMQ opcional que valida e registra eventos em inbox JSON local;
- não consulta PostgreSQL;
- não expõe endpoints HTTP;
- não entrega resultados à API Java;
- não entrega resultados ao frontend Next.js.

O principal problema não é o algoritmo de resumo e consolidação. O problema é a ausência de um contrato de integração compatível entre a granularidade produzida pelo Classifier e a granularidade exigida pelo Analyser.

## 2. Escopo e metodologia

### 2.1 Projetos analisados

- C:\projetos\scrapper-prisma
- C:\projetos\classifier-prisma
- C:\projetos\database-prisma-dev
- C:\projetos\prisma-analyse
- C:\projetos\api-prisma
- C:\projetos\app-prisma

### 2.2 Documentos externos analisados

- C:\Users\crish\Downloads\dicionario-dados-analyser.pdf
- C:\Users\crish\Downloads\dicionario-de-dados-bd-scrapping.pdf

Os PDFs totalizam 16 páginas: seis páginas no dicionário direcionado ao Analyser e dez páginas no dicionário do banco do Scraper. Eles foram inspecionados por extração textual e renderização visual.

### 2.3 Fontes de evidência

A prioridade adotada foi:

1. código executável atual;
2. migrations e SQL aplicados cumulativamente;
3. testes e fixtures;
4. configurações e arquivos de infraestrutura;
5. documentação Markdown mais recente;
6. PDFs anexados;
7. hipóteses e propostas.

Quando código e documentação divergiram, o código executável foi tratado como a melhor evidência do comportamento atual.

### 2.4 Limitação da validação

Esta foi uma auditoria estática. Os testes não foram executados porque a restrição original proibia qualquer modificação nos projetos, e testes poderiam gerar caches, bancos temporários, logs ou outros artefatos.

Consequentemente:

- a auditoria confirma contratos e comportamentos expressos no código;
- ela não confirma que RabbitMQ, PostgreSQL, API e frontend estejam funcionando juntos em um ambiente real;
- nomes, constraints e dados do banco implantado ainda precisam ser verificados no próprio PostgreSQL;
- configurações de produção podem ser diferentes das configurações locais versionadas.

## 3. Estado dos repositórios durante a auditoria

Ao final da inspeção:

- scrapper-prisma estava limpo;
- classifier-prisma estava limpo;
- database-prisma-dev estava limpo;
- api-prisma estava limpo;
- app-prisma estava limpo;
- prisma-analyse já possuía 15 arquivos modificados e um compose.yml não rastreado.

As alterações preexistentes do Prisma Analyser foram preservadas. Nenhum arquivo desses seis projetos foi alterado durante a auditoria que originou este documento.

## 4. Visão geral do sistema atual

| Componente | Comportamento confirmado | Lacuna principal |
|---|---|---|
| Scraper | Coleta artigos, extrai conteúdo, cria analysis_jobs e possui publicador RabbitMQ | O publicador é um processo separado e precisa ser executado continuamente ou agendado |
| Classifier | Consome artigo, identifica entidades, classifica parágrafos, persiste classificações e cria notificação para o Analyser | A notificação não contém todos os dados exigidos pelo Analyser |
| Banco compartilhado | Possui entidades políticas, mídias e classificações | Não possui tabelas de resumo, panorama, inbox ou auditoria do Analyser |
| Prisma Analyser | Resume itens e consolida panoramas versionados | Entrada manual, armazenamento em JSON e ausência de integração |
| API Java | Possui estrutura inicial e endpoint de teste | Não há domínio, repositório, serviço ou endpoint de análise |
| Frontend Next.js | Possui projeto inicial | Não consulta a API e a documentação descreve funcionalidades ainda inexistentes |

## 5. Scraper

### 5.1 Banco do Scraper

O dicionário anexado descreve PostgreSQL 16 e as seguintes tabelas:

- search_terms;
- articles;
- article_search_matches;
- analysis_jobs;
- schema_migrations.

### 5.2 Tabela articles

A tabela articles representa o artigo coletado desde o feed até a extração do conteúdo.

Campos relevantes para o Analyser:

- id;
- title;
- google_link;
- resolved_link;
- canonical_link;
- html_canonical_link;
- source_name;
- published_at;
- collected_at;
- full_text;
- author;
- content_hash;
- extraction_status;
- extraction_error;
- created_at;
- updated_at.

title é obrigatório no estado documentado do banco. source_name, published_at e full_text são anuláveis na tabela, embora um trabalho de análise somente deva ser criado quando a extração alcança success.

### 5.3 Tabela analysis_jobs

analysis_jobs funciona como outbox transacional interno do Scraper.

Outbox transacional é uma tabela que registra uma mensagem de integração na mesma base durável do processo de negócio. Um publicador separado retira os registros pendentes e os envia ao broker. Isso reduz o risco de salvar o artigo e perder a mensagem se houver uma falha entre o banco e o RabbitMQ.

Campos documentados:

- id;
- article_id;
- pipeline_version;
- status;
- attempts;
- max_attempts;
- scheduled_at;
- started_at;
- completed_at;
- error_message;
- created_at;
- updated_at.

A combinação article_id e pipeline_version é única.

O trigger trg_enqueue_article_analysis cria o job quando extraction_status passa a success.

### 5.4 Mensagem real do Scraper para o Classifier

O código atual monta:

~~~json
{
  "schema_version": 1,
  "job_id": 456,
  "article_id": 123,
  "pipeline_version": "v1",
  "title": "Título da notícia",
  "full_text": "Texto completo extraído",
  "canonical_link": "https://exemplo.test/noticia",
  "source_name": "Veículo",
  "published_at": "2026-08-20T10:00:00Z"
}
~~~

O Classifier também aceita content_type e usa news_article como valor padrão quando o campo não é enviado.

### 5.5 Lacunas operacionais do Scraper

O publicador de analysis_jobs é um programa separado. O Dockerfile principal executa o processo de scraping, não o publicador.

No compose local inspecionado não ficou demonstrado:

- um processo contínuo para publicar os jobs;
- um scheduler;
- um container RabbitMQ;
- a criação do exchange esperado pelo publicador;
- uma ligação completa entre exchange, routing key e scraper.dados.queue.

Isso não prova que a infraestrutura de produção não exista. Prova apenas que ela não está completa nos artefatos locais inspecionados.

## 6. Classifier

### 6.1 Validação da entrada

O Classifier exige:

- schema_version igual a 1;
- job_id inteiro;
- article_id inteiro;
- pipeline_version não vazio;
- title não vazio;
- full_text não vazio;
- canonical_link não vazio;
- source_name opcional;
- published_at opcional;
- content_type com padrão news_article.

Type hints são anotações que documentam os tipos esperados e ajudam ferramentas de análise, mas não validam dados externos automaticamente. Nesse caso, o Classifier também possui validação em runtime para rejeitar mensagens inválidas.

### 6.2 Granularidade da classificação

Granularidade é a unidade individual tratada pelo processamento.

O Classifier gera um ClassificationResult para cada combinação:

~~~text
parágrafo + entidade encontrada nesse parágrafo
~~~

O resultado contém, entre outros dados:

- paragraph_index;
- evidence_text;
- entity_id;
- entity_type;
- category;
- confidence;
- informações de auditoria do matching.

Uma mesma notícia pode produzir:

- vários resultados para o mesmo ator;
- resultados com sentimentos diferentes para o mesmo ator;
- resultados para vários atores;
- resultados para político e partido.

O sentimento calculado para um parágrafo é atribuído a todas as entidades reconhecidas naquele parágrafo. Essa é uma limitação relevante do classificador léxico atual: duas entidades no mesmo parágrafo podem receber o mesmo sentimento mesmo quando o texto se posiciona de maneira diferente sobre cada uma.

### 6.3 Escopo de entidades

O escopo configurado está direcionado a:

- presidente;
- vice-presidente;
- partidos associados.

O Analyser não deve presumir que qualquer entidade política possível aparecerá sem que a configuração do Classifier seja ampliada.

### 6.4 Persistência e outbox

O processamento atual:

1. valida a mensagem;
2. classifica o artigo;
3. grava as classificações;
4. grava classifier_analyser_outbox na mesma transação;
5. confirma a transação;
6. deixa a publicação RabbitMQ para outro processo.

O PDF do dicionário direcionado ao Analyser descreve o writer e o publisher de outbox como ainda não implementados. Essa afirmação está desatualizada em relação ao código inspecionado.

O Markdown mais recente do Classifier e o código executável confirmam que a gravação no outbox e o publicador separado já existem.

### 6.5 Mensagem real do Classifier para o Analyser

O payload atual é:

~~~json
{
  "schema_version": 1,
  "article_id": 123,
  "job_id": 456,
  "pipeline_version": "v1",
  "link_noticia": "https://exemplo.test/noticia",
  "data_publicacao_noticia": "2026-08-20T10:00:00Z",
  "notificado_em": "2026-08-20T10:05:00Z",
  "classificacoes": [
    {
      "entity_type": "politico",
      "entity_id": 10,
      "paragraph_index": 3,
      "classificacao_sentimento": "positivo",
      "score_confianca": 0.82
    }
  ]
}
~~~

### 6.6 O que não é enviado

A notificação não contém:

- title;
- full_text;
- evidence_text;
- source_name;
- nome do ator;
- identificador da classificação;
- identificador do registro de outbox;
- event_id;
- content_hash;
- versão explícita do algoritmo de classificação;
- regra de precedência entre versões.

notificado_em representa o momento em que o payload foi construído ou colocado no outbox. Não comprova o instante real de publicação no RabbitMQ.

### 6.7 Divergências e riscos no Classifier

#### paragraph_index

paragraph_index é enviado na notificação, mas não foi encontrado como coluna persistida em tabela_classificacao.

Consequência: depois de receber a notificação, o Analyser não consegue localizar com precisão a linha correspondente no banco usando paragraph_index.

##### Verificação no banco implantado em 1º de setembro de 2026

Uma consulta somente leitura ao banco `prisma-dev`, schema `public`, confirmou que `tabela_classificacao` possui 14 colunas e não possui `paragraph_index` nem uma coluna equivalente com nomes contendo `paragraph`, `paragra`, `indice`, `index`, `ordem`, `position` ou `posicao`.

A única coluna da tabela relacionada a parágrafo é `paragrafo_analisado` (`text`), que armazena o conteúdo do trecho. Ela não representa a posição do parágrafo no artigo. `id_classificacao` identifica a linha da classificação e também não equivale ao índice do parágrafo. O JSON `evidencia_match` continha apenas as chaves `alias`, `score` e `tipo`.

Em contraste, as 100 mensagens mais recentes examinadas em `classifier_analyser_outbox.payload.classificacoes[]` ainda usavam exatamente a chave `paragraph_index`, junto com `entity_type`, `entity_id`, `classificacao_sentimento` e `score_confianca`. Portanto, não foi encontrada evidência de renomeação no banco implantado. Pode existir uma mudança planejada ou ainda não implantada, mas isso não está refletido no schema nem nos payloads consultados.

#### Decisão faseada proposta: conectar o RabbitMQ antes da coluna

Status: proposta arquitetural ainda não implementada. Nenhuma alteração de banco foi realizada.

É possível implementar primeiro o consumidor e o adaptador do Analyser, porque a notificação atual do Classifier já transporta `paragraph_index`. Nessa fase, o consumidor pode validar e registrar o payload original e consultar todas as classificações do mesmo artigo, versão e ator para produzir o resumo no nível do artigo.

Essa integração inicial não resolve a correlação exata entre cada elemento de `classificacoes` recebido e a linha que contém `paragrafo_analisado`. O RabbitMQ apenas transporta a mensagem; ele não cria colunas nem modifica o PostgreSQL. Enquanto `tabela_classificacao` não persistir `paragraph_index`, a ordem e a proveniência individual dos trechos permanecem provisórias ou ambíguas.

A evolução posterior, sujeita a autorização explícita para modificar o banco, deve adicionar inicialmente uma coluna anulável, fazer o Classifier persistir `result.paragraph_index` e atualizar consultas, contratos, testes e documentação. Registros históricos só devem ser preenchidos quando a correspondência com o payload do outbox for inequívoca; casos ambíguos devem permanecer nulos.

Portanto, a divisão segura é:

1. agora: recepção, validação, persistência durável do evento e resumo agregado por artigo e ator;
2. depois: migration autorizada e correlação determinística por `paragraph_index`;
3. somente após essa segunda fase: declarar rastreabilidade exata por parágrafo.

#### Constraint de unicidade com valores nulos

A tabela de classificação usa uma restrição UNIQUE envolvendo os identificadores de político e partido. Em cada classificação, um desses identificadores tende a ser nulo.

No comportamento padrão do PostgreSQL, valores NULL são considerados distintos em uma restrição UNIQUE. Portanto, duplicatas podem ser aceitas quando parte da chave está nula.

O README do banco menciona UNIQUE NULLS NOT DISTINCT, mas o SQL executável inspecionado não confirmou esse comportamento.

O banco implantado precisa ser consultado com pg_get_constraintdef antes de se concluir qual regra está realmente ativa.

Alternativas futuras:

- UNIQUE NULLS NOT DISTINCT em uma versão compatível do PostgreSQL;
- índice único parcial para classificações de políticos;
- índice único parcial para classificações de partidos.

#### Vocabulário de evidencia_match

A documentação descreve valores em português, como exato e aproximado. O código usa exact e fuzzy.

O consumidor deve seguir o código atual ou o contrato precisa ser formalmente normalizado.

#### pipeline_version e metodo_classificacao

pipeline_version vem originalmente do job do Scraper, normalmente com valor semelhante a v1.

metodo_classificacao identifica o algoritmo usado pelo Classifier, por exemplo lexicon-baseline-v1.

Esses conceitos não são equivalentes. Como a classificação é definitiva no contrato atual do Analyser, `pipeline_version` não autoriza atualizar o rótulo de um `item_id` já conhecido. Um resultado conflitante deve ser rejeitado, não escolhido por precedência.

#### Reprocessamento

O Classifier evita repetir article_id e pipeline_version já registrados.

Entretanto, a mensagem para o Analyser não declara:

- uma chave idempotente estável para redelivery;
- como garantir que reprocessamentos produzam a mesma classificação definitiva;
- como representar uma notícia editorialmente alterada sem reutilizar indevidamente o mesmo id.

## 7. Contrato atual do Prisma Analyser

### 7.1 ItemEntrada

Cada notícia precisa conter exatamente:

- id: string;
- veiculo: string;
- url: string;
- data: date;
- titulo: string;
- texto: string;
- rotulo: positivo, negativo ou neutro.

Campos extras são proibidos pelo schema atual.

### 7.2 AtorPolitico

A rodada possui um ator com:

- id;
- tipo;
- nome.

### 7.3 Rodada

Uma Rodada contém:

- exatamente um ator;
- uma lista de notícias;
- proibição de ids duplicados dentro da rodada.

O arquivo de rodada não representa uma única notícia. Ele representa um conjunto de notícias para um ator.

### 7.4 Processamento

O pipeline:

1. abre o arquivo JSON;
2. valida a Rodada;
3. percorre cada item;
4. valida se um item conhecido repete a mesma classificação definitiva;
5. rejeita toda a rodada se o mesmo item.id chegar com outro rótulo;
6. resume itens ainda não resumidos e grava o rótulo no `RegistroResumo`;
7. marca a janela ator/mês como alterada quando há item inédito;
8. agrupa resumos por ator, mês e classificação definitiva;
9. divide cada grupo em lotes de redução;
10. consolida o panorama;
11. cria uma nova versão do panorama.

### 7.5 Resumo e classificação imutável

O prompt do resumo continua independente do rótulo para não assumir o tom do grupo. Depois da resposta do modelo, o pipeline anexa a classificação definitiva ao registro append-only da notícia.

O mesmo `item_id` pode reaparecer com o mesmo rótulo para exercitar a idempotência e o cache. Um rótulo diferente levanta `ConflitoClassificacao` antes de qualquer escrita. Não há histórico nem substituição de classificação.

Limitações confirmadas:

- texto alterado sob o mesmo id e mesmo rótulo ainda pode usar o resumo anterior;
- o mesmo id não deve ser reutilizado para dois atores;
- `content_hash` ainda não é verificado pelo protótipo;
- uma correção futura exigiria nova identidade ou um protocolo explícito, hoje inexistente.

### 7.6 Agrupamento e lotes

O grupo lógico reúne todos os resumos que compartilham:

- ator;
- mês;
- classificação definitiva.

Depois, o grupo é dividido em lotes com no máximo `LOTE_PADRAO` itens. O valor atual é 100 e representa o tamanho máximo, não uma quantidade obrigatória. Esse aumento reduz o número de chamadas de consolidação, mas pode ampliar o prompt; por isso, grupos próximos de 100 itens precisam ser medidos contra o limite de contexto do modelo.

### 7.7 Persistência

O armazenamento atual usa arquivos JSON com o padrão:

~~~text
ler arquivo
alterar estrutura em memória
reescrever arquivo
~~~

Isso é suficiente para protótipo local, mas não é seguro para vários workers concorrentes.

Riscos:

- lost update, quando dois processos sobrescrevem alterações;
- corrupção se o processo for interrompido durante a escrita;
- inexistência de transação envolvendo evento, resumo e panorama;
- dificuldade de consulta pela API Java;
- dificuldade de auditoria e reconciliação.

### 7.8 Validação de respostas do modelo

O modelo produz objetos com estrutura validada. Porém, não foram identificadas validações cruzadas que confirmem, por exemplo:

- se fonte_id devolvido pelo modelo é igual ao item.id processado;
- se ator_id do panorama corresponde ao ator solicitado;
- se janela corresponde ao mês solicitado;
- se eixo corresponde ao grupo solicitado;
- se todas as fontes retornadas pertencem realmente à entrada.

Uma resposta pode ser estruturalmente válida e semanticamente pertencer ao item errado.

### 7.9 Conteúdo não confiável e prompt injection

O texto coletado da internet é uma entrada externa não confiável.

Prompt injection é a tentativa de inserir instruções dentro do conteúdo para fazer o modelo ignorar a tarefa original. No contexto do Analyser, um artigo poderia conter frases que tentassem instruir o modelo a modificar o formato ou inventar uma conclusão.

Structured output reduz erros de formato, mas não garante fidelidade semântica.

São recomendados:

- separação explícita entre instrução e conteúdo;
- delimitadores;
- validação cruzada de ids;
- verificação das fontes;
- limites de tamanho;
- tratamento explícito de falhas HTTP;
- auditoria do modelo e dos hashes;
- testes adversariais.

## 8. Comparação campo a campo

| Necessidade do Analyser | Notificação do Classifier | Fonte possível | Conclusão |
|---|---|---|---|
| ator.id | entity_id | Payload | Disponível |
| ator.tipo | entity_type | Payload | Disponível |
| ator.nome | Ausente | entidade_matching | Exige consulta |
| item.id | article_id parcial | Composição local | article_id sozinho é inseguro |
| veiculo | Ausente | tabela_midia ou articles.source_name | Exige consulta e pode permanecer nulo |
| url | link_noticia | Payload | Disponível |
| data | data_publicacao_noticia anulável | Payload ou articles | Contrato atual exige valor |
| titulo | Ausente | articles.title | Exige acesso ao banco do Scraper |
| texto | Ausente | tabela_classificacao ou articles.full_text | Exige decisão entre trecho e artigo completo |
| rotulo único | Lista de classificações | Regra de agregação | Decisão de negócio pendente |
| score | score_confianca | Payload | Analyser atual não aceita |
| paragraph_index | Presente no payload | Não persistido na classificação | Correlação exata incompleta |
| event_id | Ausente | Hash derivado como fallback | Idempotência fica mais frágil |
| versão autoritativa | Ambígua | pipeline_version e metodo_classificacao | Regra de precedência pendente |

Não existe uma transformação completa e sem perda entre o payload atual e ItemEntrada.

## 9. Analogia do fluxo de integração

Analogia: a mensagem do Classifier funciona como uma campainha dizendo que existem novas classificações. O banco funciona como um arquivo onde o Analyser procura os detalhes da notícia.

Correspondência técnica:

- a campainha é o evento RabbitMQ;
- o número informado é article_id;
- o arquivo é o PostgreSQL;
- o atendente é o adaptador do Analyser;
- a ficha montada é o item interno normalizado.

Funcionamento técnico:

1. o consumidor recebe o evento;
2. registra o evento de forma durável;
3. consulta classificações, entidade e mídia;
4. busca título e texto na origem;
5. produz itens internos por ator;
6. executa resumo e consolidação;
7. confirma o evento somente depois da persistência.

Limitação da analogia:

O arquivo atual também está incompleto para esse uso. O banco de classificação não possui o título e o texto completo, e paragraph_index não está persistido. Portanto, consultar apenas o banco do Classifier não recupera todo o contrato exigido pelo Analyser.

## 10. Incompatibilidades centrais

### 10.1 Notícia versus parágrafo

O Classifier classifica parágrafo e entidade. O Analyser resume notícia e ator.

Antes da implementação, o produto precisa escolher:

#### Alternativa A: notícia mais ator

É a alternativa mais compatível com o Analyser atual.

Processamento:

- agrupar todas as classificações do mesmo artigo e ator;
- reunir os parágrafos relevantes;
- produzir um texto relevante para aquele ator;
- aplicar uma regra explícita para obter um rótulo agregado;
- gerar um resumo por artigo e ator.

Vantagens:

- preserva a ideia de uma notícia como fonte;
- mantém contagens compreensíveis para o frontend;
- exige menos alteração no núcleo analítico.

Custos:

- precisa definir como combinar sentimentos diferentes;
- pode esconder divergências internas do artigo;
- exige acesso aos parágrafos relevantes.

#### Alternativa B: parágrafo mais ator

Processamento:

- cada parágrafo classificado vira um item do Analyser.

Vantagens:

- preserva o resultado original do Classifier;
- evita inventar um único rótulo para a notícia inteira.

Custos:

- as contagens deixam de representar notícias;
- um único artigo pode pesar várias vezes;
- o frontend e os panoramas precisam mudar de semântica;
- o título e URL seriam repetidos em vários itens.

### 10.2 Recomendação de granularidade

Recomenda-se notícia mais ator para preservar o propósito atual do Analyser.

Uma regra candidata para o rótulo agregado seria:

1. contar os rótulos dos parágrafos relevantes;
2. escolher a maioria;
3. tratar empate como neutro;
4. registrar a versão da regra;
5. manter os rótulos originais para auditoria.

Essa regra é apenas uma proposta. Ela precisa ser avaliada com um conjunto de notícias anotadas em nível de artigo. score_confianca não deve ser usado como probabilidade calibrada sem validação específica.

### 10.3 Identificador do item

article_id sozinho não pode identificar um item do Analyser, porque um artigo pode citar vários atores.

Proposta:

~~~text
artigo:123|politico:10
artigo:123|partido:22
~~~

O identificador:

- deve incluir artigo e entidade;
- não deve incluir pipeline_version;
- deve ser estável em redeliveries e reprocessamentos com o mesmo resultado definitivo;
- deve ser acompanhado por content_hash;
- deve permitir rastrear as classificações de origem.

Se o conteúdo mudar, o resumo deve ser refeito mesmo que o id lógico permaneça igual.

### 10.4 Eventos fora de ordem

RabbitMQ pode redeliver mensagens e entregar duplicatas fora de ordem.

No contrato imutável, uma duplicata coerente é idempotente e uma classificação conflitante é rejeitada. O Analyser não aplica “o último evento recebido”.

É preciso registrar:

- event_id;
- pipeline_version;
- metodo_classificacao;
- instante efetivo da classificação;
- sequência ou revisão;
- regra que define o resultado vigente;
- histórico das transições.

### 10.5 Vários atores sob o mesmo article_id

O cache atual de resumos é orientado pelo item.id. Usar apenas article_id pode fazer o resumo preparado para um ator ser reutilizado para outro ator.

Esse é um defeito de integração de identidade, não apenas uma diferença de nome de campo.

## 11. Arquitetura de integração recomendada

~~~text
Scraper
  -> articles
  -> analysis_jobs
  -> publicador
  -> RabbitMQ
  -> Classifier
  -> tabela_classificacao
  -> classifier_analyser_outbox
  -> publicador
  -> RabbitMQ
  -> consumidor/adaptador do Prisma Analyser
  -> tabelas próprias do Analyser
  -> API Java
  -> BFF do Next.js
  -> navegador
~~~

### 11.1 Consumidor do Analyser

Responsabilidades:

1. receber a notificação;
2. validar schema_version e campos;
3. registrar o evento em uma inbox;
4. detectar duplicata;
5. enriquecer a mensagem com dados dos bancos;
6. separar os dados por ator;
7. montar o modelo interno;
8. validar e registrar a classificação definitiva;
9. resumir conteúdo inédito;
10. marcar janelas alteradas;
11. recalcular panoramas;
12. confirmar a mensagem;
13. registrar erro e aplicar retentativa quando necessário.

O ack, confirmação da mensagem ao RabbitMQ, deve ocorrer somente depois que o estado necessário estiver persistido de forma durável.

### 11.2 Inbox idempotente

Idempotência é a capacidade de processar a mesma mensagem mais de uma vez sem produzir duplicatas ou efeitos adicionais incorretos.

Uma inbox deve armazenar:

- event_id;
- schema_version;
- article_id;
- pipeline_version;
- payload original ou hash;
- received_at;
- processed_at;
- status;
- attempts;
- erro.

O payload atual não possui event_id. Enquanto o contrato não for ampliado, um hash determinístico do payload pode servir como fallback, mas é menos robusto do que um identificador produzido pelo outbox de origem.

### 11.3 Enriquecimento

O adaptador deve buscar:

- ator.id e ator.tipo no payload;
- ator.nome em entidade_matching;
- classificações em tabela_classificacao;
- veículo em tabela_midia quando houver correspondência;
- título, texto completo e content_hash em articles;
- parágrafos relevantes na classificação, quando disponíveis.

O documento mais recente do Classifier registra que uma parcela limitada de source_name foi associada a tabela_midia. Essa métrica não foi recalculada no banco ao vivo durante a auditoria. Portanto, veiculo deve ser tratado como potencialmente ausente até uma medição atual.

### 11.4 Alternativas para obter título e texto

#### Opção 1: ler o banco do Scraper

Vantagem:

- funciona sem alterar a mensagem do Classifier.

Custos:

- acopla o Analyser a dois bancos;
- exige credenciais e rede adicionais;
- aumenta risco de inconsistência temporal;
- dificulta testes e implantação.

#### Opção 2: enriquecer o evento do Classifier

Vantagem:

- o evento fica autossuficiente;
- reduz consultas durante o consumo;
- facilita replay e testes.

Custos:

- aumenta o tamanho da mensagem;
- duplica dados;
- exige modificar o contrato e o Classifier;
- texto completo pode ser grande demais para a fila.

#### Opção 3: criar uma projeção compartilhada

Projeção compartilhada é uma tabela ou visão de leitura preparada para consumidores, contendo somente os campos necessários.

Vantagem:

- reduz acoplamento ao schema interno do Scraper;
- centraliza o contrato de leitura;
- pode armazenar somente os metadados necessários.

Custos:

- exige definir proprietário e migrations;
- precisa de sincronização;
- continua dependendo de acesso ao banco.

Recomendação:

- usar evento leve como notificação;
- manter event_id e identificadores de origem;
- usar uma projeção de leitura bem definida para enriquecimento;
- evitar transportar o texto integral repetidamente no RabbitMQ;
- registrar as referências necessárias para replay e auditoria.

## 12. Modelo de persistência proposto para o Analyser

Esta seção é uma proposta. Nenhuma dessas tabelas estava implementada durante a auditoria.

### 12.1 analyser_inbox_evento

Finalidade:

- deduplicar mensagens;
- registrar tentativas;
- tornar falhas observáveis;
- permitir replay.

Campos principais:

- event_id;
- payload_hash;
- schema_version;
- article_id;
- source_pipeline_version;
- received_at;
- processed_at;
- status;
- attempts;
- error_message;
- payload_json.

### 12.2 analyser_item

Finalidade:

- representar a unidade notícia mais ator.

Campos principais:

- item_id composto;
- article_id;
- entity_type;
- entity_id;
- actor_name;
- source_name anulável;
- canonical_link;
- published_at anulável;
- title anulável enquanto a fonte não estiver completa;
- relevant_text;
- content_hash;
- derived_label;
- classifier_pipeline_version;
- classification_method;
- aggregation_rule_version;
- original_classifications;
- created_at;
- updated_at.

### 12.3 Classificação no analyser_item

No contrato imutável, não é necessária uma tabela de histórico ou um indicador `is_current`. A classificação definitiva e sua proveniência pertencem ao próprio `analyser_item`, da mesma forma que o campo `rotulo` pertence ao `RegistroResumo` no protótipo.

Restrições recomendadas:

- `item_id` único;
- `derived_label` limitado a positivo, negativo ou neutro;
- repetição do mesmo id com o mesmo rótulo tratada como idempotente;
- repetição do mesmo id com outro rótulo registrada como conflito e rejeitada;
- `original_classifications` preservado apenas como evidência imutável da agregação inicial, não como versões sucessivas.

### 12.4 analyser_resumo_item

Finalidade:

- guardar resumos imutáveis por versão do conteúdo.

Campos principais:

- summary_id;
- item_id;
- content_hash;
- summary_json;
- model;
- prompt_hash;
- created_at.

A chave de reutilização deve considerar item_id e content_hash.

### 12.5 analyser_panorama

Finalidade:

- guardar panoramas versionados.

Campos principais:

- panorama_id;
- entity_type;
- entity_id;
- window_month;
- axis;
- version;
- format;
- panorama_json;
- warnings_json;
- item_count;
- model;
- prompt_hashes;
- created_at;
- published_at;
- is_current.

### 12.6 analyser_panorama_fonte

Finalidade:

- garantir rastreabilidade entre panorama e itens utilizados.

Campos principais:

- panorama_id;
- item_id;
- summary_id;
- source_label;
- source_classification_version.

### 12.7 analyser_cursor_reconciliacao

Finalidade:

- executar catch-up caso eventos sejam perdidos;
- reconciliar outbox, classificação e inbox.

Catch-up é uma varredura que encontra registros ainda não processados e recupera o fluxo.

Um cursor baseado somente em timestamp é arriscado por causa de empates e commits tardios. É preferível usar paginação por uma chave composta estável, como timestamp mais id, ou diretamente um identificador monotônico de outbox.

## 13. Transações e concorrência

O processamento deve usar transação para manter coerência entre:

- inbox;
- item;
- classificação definitiva;
- resumo;
- janela alterada;
- versão do panorama.

Locks precisam ser definidos por ator e janela ou por outra unidade suficientemente restrita.

O objetivo não é bloquear todo o sistema, mas impedir que dois workers publiquem simultaneamente versões conflitantes do mesmo panorama.

Também são necessários:

- timeout;
- retentativa com backoff;
- limite de tentativas;
- dead-letter queue;
- logs com event_id;
- métricas de atraso;
- métricas de falha;
- reconciliação periódica.

Dead-letter queue é uma fila separada para mensagens que não puderam ser processadas após o limite de tentativas. Ela mantém o erro observável e evita que uma mensagem inválida interrompa continuamente a fila principal.

## 14. API recomendada

No estado inspecionado, a API Java possui apenas um endpoint de teste e não implementa domínio de análises.

### 14.1 Endpoints mínimos propostos

~~~text
GET /api/entidades
GET /api/entidades/{tipo}/{id}/janelas
GET /api/entidades/{tipo}/{id}/panoramas?janela=2026-08
GET /api/panoramas/{id}/fontes
~~~

Opcionalmente:

~~~text
GET /api/panoramas/{id}/versoes
GET /api/panoramas/{id}/auditoria
~~~

Os endpoints de auditoria podem exigir autorização diferente dos endpoints públicos.

### 14.2 DTO do panorama

DTO, Data Transfer Object, é um objeto preparado especificamente para transportar dados entre camadas ou sistemas.

Exemplo conceitual:

~~~json
{
  "ator": {
    "id": "10",
    "tipo": "politico",
    "nome": "Nome do ator"
  },
  "janela": "2026-08",
  "versao": 3,
  "gerado_em": "2026-08-31T12:00:00Z",
  "eixos": {
    "positivo": {
      "total_itens": 8,
      "temas": [],
      "texto": "..."
    },
    "negativo": {
      "total_itens": 5,
      "temas": [],
      "texto": "..."
    },
    "neutro": {
      "total_itens": 4,
      "temas": [],
      "texto": "..."
    }
  },
  "avisos": [],
  "fontes": [
    {
      "item_id": "artigo:123|politico:10",
      "titulo": "Título",
      "veiculo": "Veículo",
      "url": "https://exemplo.test/noticia",
      "data": "2026-08-20",
      "rotulo": "positivo"
    }
  ]
}
~~~

### 14.3 Problemas adicionais encontrados na API

application.properties usa API_SECURITY_KEY como seletor de perfil Spring. A chave de segurança e o perfil de execução são conceitos diferentes e precisam de variáveis distintas.

Também existe diferença entre o pacote principal api_prima e o pacote de teste api_prisma. Isso pode impedir a descoberta automática da configuração principal pelo teste SpringBootTest. O problema foi identificado estaticamente e não foi confirmado por execução.

## 15. Exposição no frontend

O frontend inspecionado continua próximo do template inicial do Next.js e não realiza fetch de dados do domínio.

### 15.1 BFF

BFF, Backend for Frontend, é uma camada executada no servidor da aplicação frontend que consulta a API e entrega ao navegador somente os dados necessários.

Fluxo recomendado:

~~~text
navegador
  -> rota server-side do Next.js
  -> API Java
  -> banco de panoramas
~~~

Isso evita colocar uma chave compartilhada em variáveis NEXT_PUBLIC, que são expostas ao navegador.

### 15.2 Semântica do produto

O README do frontend descreve:

- viés;
- espectro ideológico;
- perspectivas políticas.

O Classifier atual produz:

- positivo;
- negativo;
- neutro.

Esses resultados representam sentimento ou percepção textual, não posição ideológica.

O frontend não deve exibir viés ideológico como se ele já fosse calculado. É necessária uma decisão de produto:

1. apresentar sentimento e percepção, conforme o código atual; ou
2. implementar futuramente um método específico e validado de análise ideológica.

## 16. Dados e decisões ainda necessários

### 16.1 Amostras reais

Obter, com dados pessoais e credenciais removidos:

1. uma mensagem do Scraper com um artigo simples;
2. uma mensagem do Classifier com um único ator;
3. uma mensagem com vários atores;
4. uma mensagem com vários parágrafos para o mesmo ator;
5. uma redelivery coerente e uma mensagem conflitante para o mesmo item_id;
6. a linha correspondente em classifier_analyser_outbox;
7. as linhas correspondentes em tabela_classificacao;
8. o registro correspondente em articles;
9. os registros correspondentes em entidade_matching e tabela_midia.

### 16.2 Schema real do banco

Executar no banco implantado:

~~~sql
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_name IN (
    'articles',
    'analysis_jobs',
    'tabela_classificacao',
    'entidade_matching',
    'tabela_midia',
    'classifier_analyser_outbox'
)
ORDER BY table_schema, table_name, ordinal_position;
~~~

Restrições:

~~~sql
SELECT
    conrelid::regclass AS tabela,
    conname,
    pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid IN (
    'tabela_classificacao'::regclass,
    'classifier_analyser_outbox'::regclass
);
~~~

Também é útil obter um schema-only dump, sem proprietário, privilégios ou credenciais.

### 16.3 RabbitMQ

Obter:

- export das definitions;
- virtual host;
- exchanges;
- queues;
- bindings;
- routing keys;
- dead-letter exchanges;
- dead-letter queues;
- políticas de retentativa;
- configuração de heartbeat;
- publisher confirms;
- prefetch do consumidor;
- responsável por executar cada worker.

Os valores dos arquivos locais ainda parecem provisórios ou não confirmados para o fluxo completo.

### 16.4 Decisões de negócio prioritárias

1. O produto mede sentimento ou viés ideológico?
2. A unidade apresentada é notícia ou parágrafo?
3. Como vários rótulos são combinados?
4. Empate deve ser neutro, inconclusivo ou multirrótulo?
5. O que fazer quando published_at estiver ausente?
6. O que fazer quando source_name não tiver correspondência?
7. O título é obrigatório para o resumo?
8. O Analyser pode usar apenas parágrafos relevantes?
9. Qual versão de classificação prevalece?
10. Um panorama antigo continua publicamente acessível?

### 16.5 Decisões técnicas prioritárias

1. Quem é proprietário das tabelas do Analyser?
2. Qual repositório cria suas migrations?
3. O Analyser poderá ler o banco do Scraper?
4. API e Analyser usarão o mesmo banco?
5. Qual serviço executará o consumer continuamente?
6. Qual serviço executará a reconciliação?
7. Qual política de retenção será usada?
8. Qual provedor de modelo pode receber o texto?
9. Qual limite de custo, tamanho e timeout?
10. Como segredos serão injetados sem serem versionados?

### 16.6 Contexto ausente

O Classifier referencia Lab-inovação/context.md como fonte de contexto, mas esse arquivo não estava presente em C:\projetos durante a auditoria.

Ele deve ser obtido ou sua referência deve ser atualizada antes de se tratar o contexto funcional como completamente confirmado.

## 17. Estratégia quando outros projetos não puderem ser modificados

Se Scraper e Classifier permanecerem intocáveis, o Prisma Analyser ainda pode:

- consumir o payload atual;
- validar schema_version;
- gerar um hash determinístico como identificador provisório;
- consultar tabela_classificacao;
- consultar entidade_matching;
- consultar tabela_midia;
- ler articles em modo somente leitura;
- montar um item interno notícia mais ator;
- aceitar metadados ausentes;
- persistir seus próprios resultados.

Limitações:

- forte acoplamento a dois bancos;
- ausência de event_id real;
- paragraph_index não persistido;
- necessidade de reconciliar eventos com consultas;
- maior complexidade operacional;
- contratos internos dos bancos tornam-se dependências externas;
- título e texto dependem de acesso ao banco do Scraper.

Se API e frontend também nunca puderem ser alterados, o resultado não poderá aparecer na interface atual. O fluxo terminará nas tabelas ou arquivos do Analyser.

## 18. Plano de implementação futuro

Nenhum item desta seção está implementado apenas por constar neste documento. Exceção
registrada em 1º de setembro de 2026: a primeira camada da Fase 3 foi implementada em
`analyser/rabbitmq.py`. Ela consome uma fila existente sem declarar topologia, valida o
payload, preserva `paragraph_index` em inbox JSON local e confirma a mensagem somente após a
escrita durável. Ela não consulta nem modifica PostgreSQL e não chama o modelo. Consulte
[Integração RabbitMQ do Analyser](Integracao_RabbitMQ_Analyser.md).

### Fase 1: fechar contratos

- obter amostras reais;
- verificar schemas implantados;
- definir notícia versus parágrafo;
- definir agregação de rótulos;
- definir identidade;
- definir precedência de versões;
- definir campos obrigatórios e anuláveis.

### Fase 2: persistência

- criar migrations do Analyser;
- implementar repositórios PostgreSQL;
- preservar histórico;
- implementar inbox;
- implementar idempotência;
- adicionar content_hash;
- adicionar transações.

### Fase 3: integração RabbitMQ

- validar mensagens;
- processar os eventos válidos já registrados no inbox;
- configurar dead-letter queue;
- implementar reconciliação;
- adicionar métricas e logs.

### Fase 4: adaptar o núcleo analítico

- converter evento enriquecido em item interno;
- separar itens por ator;
- agregar classificações;
- validar ids devolvidos pelo modelo;
- tratar conteúdo alterado;
- tornar o recálculo concorrente seguro.

### Fase 5: API

- criar entidades e DTOs;
- implementar consultas de panorama;
- implementar consultas de fontes;
- definir autenticação;
- corrigir configuração de perfil;
- testar serialização e erros.

### Fase 6: frontend

- definir telas e estados;
- implementar BFF;
- exibir janelas e eixos;
- mostrar fontes;
- apresentar avisos e limitações;
- remover afirmações de viés não sustentadas;
- implementar estados de carregamento, ausência e erro.

## 19. Plano de testes

### 19.1 Testes de contrato

Validar:

- payload válido;
- schema_version desconhecida;
- campo ausente;
- tipo inválido;
- classificacoes vazias;
- rótulo desconhecido;
- ator desconhecido;
- data nula.

### 19.2 Testes unitários

Validar:

- composição do item_id;
- agrupamento por ator;
- agregação de rótulos;
- desempate;
- rejeição de classificação conflitante para um id conhecido;
- comparação de content_hash;
- validações cruzadas do modelo;
- marcação de janela alterada.

### 19.3 Testes de idempotência

Enviar a mesma mensagem:

- uma vez;
- duas vezes seguidas;
- depois de reiniciar o processo;
- durante uma falha parcial;
- após uma confirmação RabbitMQ perdida.

O resultado deve ser único e consistente.

### 19.4 Testes de classificação imutável

Cobrir:

- mesmo id e mesmo rótulo repetidos;
- mesmo id com rótulo diferente, rejeitado antes de qualquer escrita;
- redelivery depois de reiniciar o consumidor;
- mensagem conflitante entregue fora de ordem;
- mudança de texto sob o mesmo item e mesmo rótulo;
- vários atores no mesmo artigo, cada um com identidade própria.

### 19.5 Testes de integração

Executar com PostgreSQL e RabbitMQ reais de teste:

- Scraper outbox para RabbitMQ;
- RabbitMQ para Classifier;
- Classifier para tabela e outbox;
- outbox para RabbitMQ;
- RabbitMQ para Analyser;
- Analyser para PostgreSQL;
- API para PostgreSQL;
- frontend para API.

Mocks são objetos falsos que simulam dependências. Eles validam a lógica ao redor da integração, mas não comprovam a topologia real, constraints reais, serialização real ou comportamento real do broker.

### 19.6 Testes de concorrência

Validar:

- dois eventos do mesmo ator e mês;
- eventos de atores diferentes;
- dois workers;
- reprocessamento simultâneo;
- falha antes do commit;
- falha depois do commit e antes do ack.

### 19.7 Testes de modelo

Validar:

- JSON inválido;
- resposta vazia;
- ids incorretos;
- janela incorreta;
- eixo incorreto;
- fonte inventada;
- timeout;
- erro HTTP;
- rate limit;
- prompt injection;
- artigo muito grande.

### 19.8 Smoke test

Smoke test é uma verificação curta que comprova que o fluxo básico inicia e executa a principal jornada.

Cenário mínimo:

1. inserir ou coletar um artigo de teste;
2. confirmar analysis_job;
3. publicar para o Classifier;
4. confirmar classificação e outbox;
5. consumir no Analyser;
6. confirmar resumo e panorama;
7. consultar pela API;
8. visualizar no frontend.

Esse teste não mede qualidade analítica; ele comprova apenas que a integração técnica básica funciona.

## 20. Riscos residuais

- qualidade probabilística do modelo;
- viés das fontes coletadas;
- cobertura limitada dos termos de busca;
- reconhecimento incompleto de entidades;
- associação incompleta de mídias;
- ausência de data em artigos;
- parágrafos com várias entidades;
- score não calibrado;
- eventos fora de ordem;
- duplicação de mensagens;
- indisponibilidade de banco ou RabbitMQ;
- custo e latência do modelo;
- retenção de textos de terceiros;
- exposição indevida de chaves;
- documentação ficando desatualizada;
- diferença entre ambiente local e produção.

Nenhuma arquitetura elimina completamente esses riscos. A proposta busca torná-los observáveis, testáveis e controláveis.

## 21. Divergências documentais identificadas

1. O PDF do Classifier afirma que a escrita/publicação do outbox ainda não existia; o código atual mostra implementação.
2. O Markdown mais recente ainda usa em um ponto linguagem de payload proposto, mas em outro registra o contrato como decidido.
3. A documentação usa exato e aproximado; o código usa exact e fuzzy.
4. O README do banco afirma UNIQUE NULLS NOT DISTINCT; o SQL inspecionado usa UNIQUE comum.
5. README e SQL divergem sobre obrigatoriedade de alguns campos.
6. db/schema.sql do Scraper é histórico e não representa sozinho o schema cumulativo das migrations.
7. O frontend descreve viés ideológico; o Classifier implementa sentimento positivo, negativo e neutro.
8. A documentação do frontend menciona componentes, tipos e endpoints que não existem no código atual.

## 22. Arquivos centrais para reinspeção

### Scraper

- ..\..\scrapper-prisma\src\jobs_publisher.py
- ..\..\scrapper-prisma\src\jobs_store.py
- ..\..\scrapper-prisma\src\rabbitmq_client.py
- ..\..\scrapper-prisma\src\publish_jobs.py
- ..\..\scrapper-prisma\db\migrations
- ..\..\scrapper-prisma\docker-compose.yml
- ..\..\scrapper-prisma\docker\Dockerfile

### Classifier

- ..\..\classifier-prisma\src\payload.py
- ..\..\classifier-prisma\src\pipeline.py
- ..\..\classifier-prisma\src\message_handler.py
- ..\..\classifier-prisma\src\classification_store.py
- ..\..\classifier-prisma\src\analyser_notifier.py
- ..\..\classifier-prisma\src\notifications_store.py
- ..\..\classifier-prisma\src\notification_publisher.py
- ..\..\classifier-prisma\src\rabbitmq_publisher.py
- ..\..\classifier-prisma\docker\rabbitmq\definitions.json
- ..\..\classifier-prisma\docs\dicionario-dados-analyser.md
- ..\..\classifier-prisma\docs\adr

### Banco compartilhado

- ..\..\database-prisma-dev\criar_tabelas_iniciais.sql
- ..\..\database-prisma-dev\README.md

### Prisma Analyser

- ..\analyser\schemas.py
- ..\analyser\pipeline.py
- ..\analyser\armazenamento.py
- ..\analyser\modelos.py
- ..\analyser\prompts.py
- ..\prompts\resumo_item.txt
- ..\README.md
- .\Prisma_Analyser_Documentacao.md

### API

- ..\..\api-prisma\README.md
- ..\..\api-prisma\src\main
- ..\..\api-prisma\src\test

### Frontend

- ..\..\app-prisma\README.md
- ..\..\app-prisma\app\page.tsx
- ..\..\app-prisma\app\layout.tsx

## 23. Procedimento de rollback deste documento

Este arquivo é documentação nova e não altera comportamento executável.

Se for necessário desfazer somente sua criação:

1. confirmar que o alvo é exatamente docs\Auditoria_Integracao_Sistema_Completo.md;
2. remover apenas esse arquivo;
3. não reverter docs\Prisma_Analyser_Documentacao.md nem outros arquivos modificados;
4. verificar git status.

## 24. Glossário

### Ack

Confirmação enviada ao RabbitMQ informando que a mensagem foi processada e pode ser removida da fila.

### Backoff

Espera progressivamente maior entre tentativas, usada para evitar repetição agressiva durante uma falha.

### BFF

Backend for Frontend. Camada de servidor criada para atender especificamente às necessidades do frontend e proteger credenciais.

### Catch-up

Processo de recuperação que procura registros não processados e repõe o fluxo.

### Content hash

Resumo criptográfico do conteúdo. Ajuda a detectar se o texto mudou mesmo quando o identificador lógico é o mesmo.

### Contrato de dados

Acordo sobre nomes, tipos, obrigatoriedade e significado dos campos trocados entre componentes.

### Dead-letter queue

Fila que recebe mensagens que excederam o limite de tentativas ou não puderam ser processadas.

### DTO

Data Transfer Object. Objeto preparado para transportar dados entre sistemas ou camadas.

### Granularidade

Unidade individual processada, como artigo, parágrafo ou combinação de artigo e ator.

### Idempotência

Propriedade que permite repetir uma operação sem criar efeitos duplicados.

### Inbox

Tabela do consumidor usada para registrar eventos recebidos, deduplicar e acompanhar processamento.

### Migration

Alteração versionada do schema do banco.

### Outbox transacional

Tabela usada pelo produtor para registrar de forma durável as mensagens que precisam ser publicadas.

### Prompt injection

Conteúdo malicioso ou acidental que tenta se comportar como instrução para o modelo.

### Projeção de leitura

Estrutura preparada para consultas de consumidores, sem expor todo o schema interno do produtor.

### RabbitMQ

Broker de mensagens que transporta eventos entre serviços por exchanges, filas e routing keys.

### Reconciliação

Comparação periódica entre origem e destino para encontrar mensagens ou dados perdidos.

### Structured output

Resposta do modelo restringida a uma estrutura definida, como um schema JSON.

### Type hint

Anotação de tipo usada para documentação e análise estática. Não substitui validação de dados externos em runtime.

## 25. Síntese para apresentação acadêmica

O Prisma Analyser já implementa o núcleo de resumo e consolidação por ator, período e sentimento, incluindo classificação definitiva no registro imutável da notícia e versionamento de panoramas. Contudo, ele ainda é alimentado manualmente e utiliza persistência local.

A auditoria identificou que o Classifier trabalha na granularidade parágrafo mais entidade, enquanto o Analyser trabalha na granularidade notícia mais ator. Além disso, a notificação do Classifier omite título, texto e veículo e ainda precisa definir uma identidade idempotente compatível com a classificação definitiva.

A arquitetura proposta introduz:

- consumidor RabbitMQ;
- inbox idempotente;
- enriquecimento em banco;
- identificador composto por artigo e ator;
- classificação imutável associada ao resumo da notícia;
- persistência PostgreSQL;
- rastreabilidade de fontes;
- API de consulta;
- BFF para o frontend;
- testes de contrato, integração, concorrência e conflito de classificação.

Essa proposta não afirma que o sistema já esteja integrado. Ela separa o núcleo confirmado das funcionalidades ainda necessárias e fornece um caminho verificável para transformar o protótipo em um componente integrado.
