# Prisma · Analyser (protótipo)

O **Prisma** coleta notícias sobre atores políticos brasileiros — pessoas ou partidos —,
classifica cada uma como positiva, negativa ou neutra em relação àquele ator, e apresenta
ao cidadão um panorama organizado e rastreável até a fonte original.

Pipeline completo: Scraper → Messageria → Classifier → Messageria → **Analyser** → System
DB → API → Frontend.

Este repositório é **só o Analyser**. Ele recebe notícias que já chegam classificadas e
produz o resumo que o eleitor lê.

O núcleo analítico continua sem banco, HTTP ou interface: os arquivos JSON em `saida/`
fazem o papel da persistência do protótipo. A partir desta versão, há também um consumidor
opcional de RabbitMQ que recebe e preserva os eventos do Classifier em um inbox JSON local;
ele não escreve no banco compartilhado.

---

## Rodar

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

python -m analyser.cli --entrada dados/rodada_1.json
python -m analyser.cli --entrada dados/rodada_2.json
```

**Nenhuma variável de ambiente é necessária.** O modelo padrão é o `ModeloFake`:
determinístico, sem rede e sem chave.

Testes:

```bash
python -m pytest
```

---

## Receber notificações do Classifier pelo RabbitMQ

O consumidor recebe o payload publicado pelo Classifier, valida o schema e grava cada evento
em `saida/inbox_eventos.jsonl` antes de enviar o ACK ao RabbitMQ. Isso preserva
`classificacoes[].paragraph_index`, mesmo ele ainda não sendo uma coluna de
`tabela_classificacao`.

1. Copie `.env.example` para `.env` se ainda não existir.
2. Preencha as variáveis `RABBITMQ_*` com os valores fornecidos pela infraestrutura.
3. Confirme somente a autenticação, sem consumir mensagens:

```bash
python -m analyser.consumidor_rabbitmq --verificar-conexao
```

4. Inicie o consumidor:

```bash
python -m analyser.consumidor_rabbitmq --saida saida
```

O contrato confirmado mantém `RABBITMQ_DECLARE_TOPOLOGY=false`: o Analyser não cria nem
altera exchange, fila ou binding. Para interromper, use `Ctrl+C`.

Esta primeira integração **não chama o modelo nem cria resumos automaticamente**. Ela é a
camada de entrada confiável. A transformação de vários trechos classificados em uma notícia
exige uma regra de negócio aprovada para agregar rótulos e uma leitura posterior dos trechos
no banco. Detalhes, limites e testes estão em
[Integração RabbitMQ do Analyser](docs/Integracao_RabbitMQ_Analyser.md).

---

## Demonstração auditável com dados reais

A execução isolada de setembro de 2026 usa 12 itens montados a partir de trechos do banco,
o modelo local `qwen3.5:9b` e lote 100. Consulte:

- [documentação técnica do teste](docs/Teste_Integracao_Banco_Real_2026_09.md);
- [relatório visual “Da notícia ao panorama”](relatorios/demo_banco_2026_08.html);
- [metadados de rastreabilidade](dados/rodada_banco_demo_2026_08_metadados.json).

O banco foi consultado em transações `READ ONLY` encerradas com `ROLLBACK`. O relatório
é demonstrativo: mostra parte das entradas e dos resumos, mas inclui integralmente os três
panoramas finais.

---

## Trocar para o modelo real (cinco linhas)

1. `cp .env.example .env`
2. Preencha `API_KEY` com sua chave.
3. Ajuste `BASE_URL` e `MODEL` para o provedor que quiser — qualquer API compatível com a
   da OpenAI serve.
4. Rode com `--modelo api`.
5. Nenhum nome de modelo existe no código: trocar de provedor é só editar o `.env`, e o
   modelo efetivamente usado vai para o registro de auditoria de cada execução.

O `.env.example` vem apontado para Maritaca/`sabiazinho-4`. É **ponto de partida, não
decisão final** — não há nada no código que dependa dessa escolha.

---

## A ideia central

Duas camadas, e a separação entre elas é o coração do módulo.

| | `saida/resumos_item.json` | `saida/panoramas.json` |
|---|---|---|
| granularidade | um resumo + classificação definitiva por notícia | um registro por `(ator, janela, eixo, versão)` |
| custo | **alto**, pago uma vez por notícia | baixo |
| escrita | **append-only**, nunca recalculado | versão nova a cada recálculo |

A **janela** é o mês da notícia (`YYYY-MM`), derivado do campo `data` do item — a data da
**notícia**, não a data em que ela chegou. Notícia de julho descoberta hoje pertence à
janela de julho.

Disso decorre a propriedade que o protótipo existe para demonstrar: uma notícia inédita é
resumida uma única vez; se o mesmo `item_id` reaparecer com a mesma classificação, o resumo
é recuperado do cache. Uma classificação diferente para um id conhecido é erro de contrato.

---

## O alvo é um ator político, não um político

O alvo da análise tanto pode ser uma pessoa quanto um partido. O módulo trata os dois
exatamente do mesmo jeito — muda só o `tipo` declarado na entrada:

```json
{ "ator": { "id": "ator_001", "tipo": "politico", "nome": "..." }, "itens": [ ... ] }
```

`tipo` aceita `politico` ou `partido`. Toda a persistência é chaveada por `ator_id`, e o
prompt do resumo recebe o tipo junto do nome, para não escrever sobre um partido como se
fosse uma pessoa.

---

## Pipeline, em seis etapas

1. **Resumo por item (map).** Já existe resumo? Pula, sem chamar o modelo. Senão, gera
   resumo de 2–3 frases + temas + id da fonte, e grava.
2. **Marcação de janelas sujas.** Uma janela entra na fila de recálculo quando recebe um
   item inédito.
3. **Agrupamento.** Pela classificação definitiva gravada no resumo, em Python puro.
4. **Consolidação em dois níveis (reduce).** Lotes de `--lote` (padrão 100) → parciais →
   panorama do eixo. A entrada são **sempre os resumos curtos**, nunca os textos completos
   e nunca um panorama anterior.
5. **Versionamento.** Versão nova; as anteriores nunca são tocadas.
6. **Verificação de contagem.** Em Python: todo id citado existe no conjunto de entrada e
   as contagens declaradas batem. Divergência vira aviso — não é corrigida em silêncio.

---

## Regras invioláveis

1. **O rótulo entra no prompt da consolidação, nunca no do resumo por item.** Constante
   única em [config.py](analyser/config.py): `ROTULO_NO_PROMPT = {"map": False, "reduce": True}`.
   A função que monta o prompt do map nem recebe rótulo como parâmetro. Assim, o resumo é
   **imparcial**: descreve a matéria sem assumir o tom do agrupamento.
2. **A classificação é imutável.** O rótulo definitivo é salvo no registro do resumo. Se
   um `item_id` conhecido reaparecer com outro rótulo, a rodada inteira é rejeitada antes
   de qualquer escrita.
3. **`resumos_item.json` é append-only.** `gravar_resumo()` levanta exceção se o id já
   existe, e não existe `atualizar_resumo` nem `apagar_resumo`.
4. **O panorama nunca é insumo de si mesmo.** Consolidação parte sempre dos resumos.
5. **Grounding estrito.** Os prompts proíbem conhecimento externo sobre o ator.
6. **Rastreabilidade.** Toda afirmação referencia os ids que a sustentam.
7. **A polaridade é da cobertura, não do ator.** Os textos dizem o que *as matérias*
   tratam; nunca julgam quem elas citam.
8. **Saída estruturada obrigatória**, validada com Pydantic. Uma tentativa de reparo,
   depois erro registrado.
9. **`temperature = 0`.**

---

## Formatos de saída, e por que os arquivos têm esses nomes

Cada eixo tem o seu fragmento de formato, resolvido por um mapa em configuração:

```python
FORMATO_POR_EIXO = {"negativo": "negativo", "positivo": "positivo", "neutro": "neutro"}
```

O prompt final é montado como **base única + fragmento de formato**. A base
([consolidacao_final.txt](prompts/consolidacao_final.txt)) concentra todas as regras de
fidelidade, rastreabilidade e estilo, e é a mesma para os três. O fragmento define a
estrutura da resposta e **o que o parágrafo consolidado deve reunir naquele recorte**:

```
prompts/formatos/negativo.txt   ← as críticas e cobranças que a cobertura registra
prompts/formatos/positivo.txt   ← os elogios e entregas que a cobertura registra
prompts/formatos/neutro.txt     ← os fatos que a cobertura registra sem tomar partido
```

Os três pedem a mesma coisa em forma: **um parágrafo de 3 a 6 frases, sem ids na prosa**,
cuja primeira frase nomeia o recorte em palavras — *"As críticas registradas pela cobertura
do mês concentram-se em…"*. O texto é lido por um cidadão e corre limpo; a rastreabilidade
mora em `temas[].fontes` e em `fontes`. A contrapartida é real e está registrada na
documentação: uma afirmação do parágrafo não tem mais como ser conferida contra uma matéria
específica.

### Por que isto mudou

Até 1º de setembro de 2026 os fragmentos eram nomeados pelo **formato** —
`detalhado.txt` servia negativo E positivo, o mesmo arquivo byte a byte — e o panorama
detalhado entregava `fatos_mais_citados`, uma lista de fatos soltos. A ideia era que uma
assimetria de tom entre negativo e positivo fosse impossível de introduzir por descuido:
se ninguém pode editar só um dos dois, ninguém torna um mais incisivo que o outro.

Por decisão de produto, o panorama passou a entregar **um parágrafo consolidado** no lugar
dos fatos soltos, e esse parágrafo reúne explicitamente as críticas no eixo negativo e os
elogios no positivo. Instruir esse recorte exige um fragmento por eixo. A simetria foi
trocada por três arquivos distintos — de propósito, e registrado aqui para que a troca não
seja confundida com o descuido que a regra antiga evitava.

**O que a troca não afrouxou:**

- o rótulo continua fora do prompt do resumo por item (`ROTULO_NO_PROMPT["map"] is False`);
  o resumo de uma matéria não sabe em que eixo ela caiu;
- a consolidação de lote, que alimenta os três eixos, continua com **um prompt único** e
  sem instrução de tom — e o teste segue cobrando isso;
- os três fragmentos mandam descrever o que a COBERTURA registra e proíbem adjetivo sobre
  o alvo. O Analyser relata a polaridade das matérias; não a fabrica.

O **schema Pydantic é único** para os três eixos: desde a troca, nenhum campo é exclusivo
de um formato. Nenhum nome de campo sinaliza polaridade.

---

## Arquitetura

```
analyser/
  config.py         constantes das regras, Config, leitor de .env
  schemas.py        Pydantic: entrada, resumo, parcial, panorama
  modelos.py        Protocol + ModeloFake + ModeloAPI + validação com retry
  armazenamento.py  JSON, append-only, versionamento
  prompts.py        carregamento e hash SHA-256
  pipeline.py       as seis etapas
  saida.py          impressão com rich
  cli.py
prompts/            todos os prompts, carregados em runtime — nada hardcoded
dados/              rodada_1.json e rodada_2.json
testes/
```

**`ModeloFake` é o padrão.** É determinístico, extrativo e expõe contadores de chamada por
tarefa, usados nos testes. Tudo o que ele escreve sai do texto que recebeu.

**Auditoria** por execução em `saida/auditoria_<execucao_id>.json`: identificação
imutável da execução, número sequencial da rodada no diretório de saída, arquivo de
entrada, capacidade máxima configurada, quantidade real de notícias em cada lote,
modelo, hashes dos prompts, cache, chamadas por tarefa, tokens, avisos e erros.

---

## Onde a classificação fica armazenada

Não existe mais `saida/rotulos.json`. O campo `rotulo` é gravado diretamente no registro
imutável de cada notícia em `saida/resumos_item.json`, ao lado de `item_id`, `ator_id` e
`janela`. A resposta textual do modelo (`ResumoItem`) continua sem receber o rótulo no
prompt; o pipeline anexa a classificação somente ao montar o registro persistido.

Quando um id conhecido reaparece, o Analyser compara o rótulo recebido com o que já está
gravado. Valores iguais exercitam o cache normalmente. Valores diferentes levantam
`ConflitoClassificacao` antes de qualquer resumo, panorama ou auditoria da nova rodada ser
escrito. Assim, não existe atualização nem histórico de classificação.

Uma saída criada pelo schema anterior, cujos resumos ainda não possuem `rotulo`, levanta
`SaidaLegadaIncompativel`. O programa não escolhe automaticamente um valor de um antigo
`rotulos.json`; é necessário migrar ou regenerar essa saída de forma explícita.

---

## Dados

Ator político, município e veículos são **inventados**. Ariel Fontenele Dourado é um personagem
de ficção, prefeita do município fictício de Vila Serena do Norte; os sete veículos não
correspondem a nenhum jornal real.

- **Rodada 1** — 10 itens, todos de agosto de 2026, rótulos desbalanceados (6/2/2).
- **Rodada 2** — 8 itens: 6 novos de agosto, 1 novo de **julho** (prova que a janela vem
  da data da notícia) e 1 item da rodada 1 com **o mesmo rótulo** (prova que uma repetição
  coerente usa o cache).

Plantados de propósito: um item rotulado negativo com texto equilibrado (`not_003`), um
que menciona dois políticos sendo só um o alvo (`not_008`), uma nota protocolar sem
substância (`not_013`) e dois itens sobre o mesmo fato por veículos diferentes
(`not_005`/`not_006`).

Os textos evitam de propósito os radicais `positiv`, `negativ` e `neutr` — o teste 1
renderiza o prompt do map com os dados reais e falharia se a palavra chegasse ao modelo
por dentro da notícia.

---

## Testes

| # | arquivo | o que trava |
|---|---|---|
| 1 | `test_vazamento_rotulo.py` | o prompt do map, com dados reais, não menciona rótulo |
| 2 | `test_vazamento_rotulo.py` | o prompt do reduce nomeia o eixo mas não instrui tom |
| 3 | `test_cache.py` | a rodada 2 só chama o modelo para os itens inéditos |
| 4 | `test_classificacao_imutavel.py` | repetição coerente usa cache e rótulo conflitante rejeita toda a rodada |
| 5 | `test_versionamento.py` | agosto fica em v2, julho em v1, e a v1 continua íntegra |
| 6 | `test_schemas.py` | saída malformada é rejeitada, com um retry e depois erro |

---

## Decisões registradas

**O rótulo entra no prompt da consolidação.** Decidido em 18/08/2026. O que se perde, para
constar:

- A coerência entre o texto do panorama e o rótulo deixa de ser evidência independente. Se
  o eixo negativo sai duro, não dá mais para saber se é porque as notícias são duras ou
  porque o prompt informou que aquele conjunto é o negativo.
- Some a possibilidade de detectar erro de classificação a partir do texto consolidado.

Mitigações em vigor: o rótulo fica fora do map, o prompt do reduce não instrui tom, e o
teste 2 cobra isso automaticamente.

**As contagens são derivadas, não perguntadas.** Decidido em 31/08/2026, depois de rodar
o pipeline contra um modelo real. `ocorrencias` e `total_itens` deixaram de ser aceitos como
o modelo os declara: passam a vir de `len(set(fontes))`, sobre a lista que ele mesmo escreveu
na mesma resposta. O que se perde, para constar:

- A divergência entre o número declarado e a lista deixa de ser evidência sobre a atenção do
  modelo naquela chamada. Ela vira aviso, mas o panorama gravado não a carrega mais.
- Some um sinal barato de degradação: se um dia o modelo passar a errar contagem com muito
  mais frequência, isso só aparecerá no volume de avisos, não na saída.

O que se ganha: `ocorrencias` ordena os temas no painel, então um número inflado promovia um
tema acima do que as matérias sustentam. Medido no Qwen3.5-9B, um tema declarou 3 ocorrências
listando 2 fontes — e era justamente o tema que havia agrupado corretamente os dois veículos
que cobriram o mesmo fato.

Isto não contradiz "divergência vira aviso, não é corrigida em silêncio": toda substituição é
registrada na auditoria. A distinção é o tipo de alegação. Conferir se um id citado existe no
conjunto verifica uma afirmação sobre o **conteúdo**, e ali corrigir seria adulterar o que o
modelo disse. Uma contagem sobre uma lista visível não é alegação — é aritmética que o
pipeline já tem em mãos.

**A classificação é definitiva.** Decidido em 31/08/2026. O rótulo passou a integrar o
registro append-only da notícia e `rotulos.json` foi removido. O que se ganha é um contrato
mais simples, sem estado mutável nem versões concorrentes da classificação. O que se perde é
a capacidade de corrigir um rótulo dentro do Analyser: o mesmo `item_id` com valor diferente
agora rejeita a rodada inteira. Caso o produto venha a admitir correções no futuro, será
necessário definir explicitamente uma nova identidade ou um protocolo de revisão antes de
reintroduzir esse comportamento.

**Não há verificação do classificador.** Decidido em 19/08/2026. O Analyser confia no
rótulo que recebe: agrupa por ele, consolida por ele e conta por ele, sem segunda opinião.
Em troca, o custo por notícia inédita caiu pela metade — uma chamada de modelo em vez de
duas. Se um dia o Classifier passar a errar de forma perceptível, o lugar de reintroduzir
a conferência é aqui, no nível do item, porque é o único ponto que ainda enxerga o texto
integral.

---

## Fora de escopo

Nenhum visualizador, dashboard ou frontend. Nenhuma integração com RabbitMQ, Kafka ou
Postgres. Nenhuma API HTTP. Nenhum scraper e nenhum classificador. Nenhum Docker.
