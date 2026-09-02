# Integração RabbitMQ do Prisma Analyser

Data: 1º de setembro de 2026

## Objetivo

Registrar de forma durável as notificações publicadas pelo Classifier no RabbitMQ, sem
modificar o banco PostgreSQL compartilhado e sem perder o campo `paragraph_index` presente
no payload.

## Estado implementado

O Analyser possui agora um consumidor opcional executado por:

```powershell
python -m analyser.consumidor_rabbitmq --saida saida
```

Ele executa este fluxo:

```text
Classifier → classifier_analyser_outbox → RabbitMQ → Consumidor do Analyser
                                                     ↓
                                           validação Pydantic do payload
                                                     ↓
                                      saida/inbox_eventos.jsonl com fsync
                                                     ↓
                                                    ACK
```

**Nenhuma tabela, coluna, linha ou migration do PostgreSQL é criada, alterada ou removida
por esta implementação.** O consumidor também não consulta o banco nesta primeira camada.

## Contexto anterior

O Classifier já monta um payload com `classificacoes[].paragraph_index`, mas a tabela
compartilhada `tabela_classificacao` não persiste esse índice como coluna. O índice existe no
JSON do outbox e na mensagem RabbitMQ; `paragrafo_analisado` guarda o texto do trecho, não a
posição dele no artigo.

O núcleo original do Analyser recebia somente arquivos de rodada no formato `Rodada` e não
possuía cliente RabbitMQ. Também não seria correto enviar diretamente o payload do Classifier
para `ItemEntrada`: as granularidades são diferentes e o schema é estrito.

## Decisão implementada

Foi criada uma camada de ingestão, não uma integração semântica completa:

1. validar a mensagem do Classifier com `NotificacaoClassifier`;
2. preservar o payload válido em JSON Lines;
3. preservar também mensagens inválidas, com o erro de validação, para auditoria;
4. enviar ACK somente após a escrita ter sido concluída e sincronizada no disco;
5. reenfileirar a mensagem se a gravação local falhar;
6. não declarar topologia quando `RABBITMQ_DECLARE_TOPOLOGY=false`.

Inbox (registro durável local das mensagens recebidas por um consumidor) evita perder uma
mensagem se o processo cair depois de recebê-la. Idempotência (capacidade de receber a mesma
mensagem novamente sem produzir um segundo registro) é feita pelo SHA-256 do payload. O hash
é uma solução de fallback porque o payload atual não traz `event_id` próprio.

## Configuração

Os valores devem estar no `.env` local ou em secrets do ambiente. `.env.example` contém
somente chaves e valores não secretos de referência.

```dotenv
RABBITMQ_HOST=<fornecido-pela-infra>
RABBITMQ_PORT=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_USER=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_PASSWORD=<segredo>
RABBITMQ_NOTIFIER_EXCHANGE=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_EXCHANGE_TYPE=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_QUEUE=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_ROUTING_KEY=<fornecido-pela-infra>
RABBITMQ_NOTIFIER_DURABLE=<fornecido-pela-infra>

RABBITMQ_VHOST=/
RABBITMQ_USE_TLS=false
RABBITMQ_HEARTBEAT=60
RABBITMQ_PREFETCH=1
RABBITMQ_DECLARE_TOPOLOGY=false
RABBITMQ_RECONNECT_DELAY_SECONDS=5
```

Com `RABBITMQ_DECLARE_TOPOLOGY=false`, o Analyser só consome a fila existente. Exchange,
routing key, tipo de exchange e durable permanecem configurados para registrar o contrato e
para uma eventual topologia de desenvolvimento explicitamente autorizada, mas não são usados
para criar recursos no broker em produção.

O usuário RabbitMQ precisa, no mínimo, ter permissão de leitura sobre a fila no vhost
configurado. A conta `admin` funciona tecnicamente se possuir essa permissão, mas uma conta
específica do Analyser com menor privilégio é a recomendação operacional futura.

## Diagnóstico da conexão em 1º de setembro de 2026

### Como o Classifier se conecta

O código executável do Classifier confirma que a publicação usa AMQP por meio do Pika, e
não HTTP:

1. `src/notify_analyser.py` carrega o `.env`, abre o banco compartilhado somente para ler e
   atualizar o outbox próprio do Classifier e instancia `RabbitMQPublisher`;
2. `RabbitMQConfig.from_env()` lê `RABBITMQ_HOST` como texto e `RABBITMQ_PORT` como inteiro;
3. `RabbitMQPublisher._connect()` cria `pika.PlainCredentials` e
   `pika.ConnectionParameters(host=..., port=..., virtual_host=...)`;
4. `pika.BlockingConnection` abre a conexão AMQP e o canal ativa publisher confirms;
5. cada payload já armazenado em `classifier_analyser_outbox.payload` é publicado com
   `basic_publish`, como JSON persistente e com roteamento obrigatório;
6. somente depois da publicação confirmada o registro do outbox é marcado como notificado.

O Classifier não remove `http://`, não interpreta uma URL e não usa a API de management.
Ele entrega o valor de `RABBITMQ_HOST` diretamente ao resolvedor de nomes do sistema
operacional. Também não declara exchange, fila ou binding em produção: essa topologia deve
existir previamente.

No ambiente Docker local do Classifier, `docker-compose.yml` deixa explícita a separação:

- porta `5672` do container: protocolo AMQP usado pelo Pika;
- porta `15672` do container: interface web de administração, acessada por HTTP.

### Comparação com o Analyser

O Analyser usa o mesmo mecanismo essencial: `pika.PlainCredentials`,
`pika.ConnectionParameters` e `pika.BlockingConnection`. As opções adicionais de heartbeat,
tentativas e prefetch não alteram o formato esperado para o host. Com
`RABBITMQ_USE_TLS=false` e `RABBITMQ_DECLARE_TOPOLOGY=false`, ele tenta uma conexão AMQP sem
TLS a uma topologia já existente, conforme confirmado pelo responsável.

Portanto, o erro observado não é uma diferença de protocolo entre Classifier e Analyser. A
mesma configuração de host em formato HTTP também falharia em
`src/rabbitmq_publisher.py` do Classifier.

### Evidências do erro atual

Os testes foram feitos sem imprimir hostname, usuário ou senha:

1. o `.env` do Analyser contém `RABBITMQ_HOST` iniciado por `http://` e com barras;
2. a resolução DNS do valor bruto falha, o que explica `getaddrinfo failed`;
3. ao extrair apenas o hostname da URL, a resolução DNS funciona;
4. a conexão TCP ao hostname extraído e à `RABBITMQ_PORT` configurada é recusada
   (`ConnectionRefused`);
5. `docker ps` não mostrou containers em execução. O Docker Desktop estar ativo não
   significa que um broker RabbitMQ local tenha sido iniciado.

A primeira causa comprovada é, portanto, o formato inválido do host. Existe ainda uma
segunda pendência independente: a porta AMQP fornecida não está aceitando conexão a partir
desta máquina. Como a comunicação TCP nem chegou ao broker, autenticação, vhost,
permissões, fila e contrato da mensagem ainda não foram testados na integração real.

### Informação que a infraestrutura precisa confirmar

O valor correto deve possuir este formato, sem protocolo, caminho ou porta embutida:

```dotenv
RABBITMQ_HOST=nome-do-broker
RABBITMQ_PORT=porta-amqp-acessivel
```

O checkout local do Classifier não possui `.env` real nem manifesto Kubernetes/Helm com os
valores efetivos de produção. A documentação informa que o `CronJob` é administrado fora do
repositório. Assim, ainda é necessário confirmar com a infraestrutura:

1. qual hostname e porta AMQP o processo `notify_analyser.py` usa efetivamente;
2. se esse endpoint é acessível pela estação Windows ou somente pela rede interna do
   Kubernetes;
3. caso seja interno, qual acesso deve ser usado: VPN, endpoint externo ou port-forward;
4. se o usuário fornecido possui permissão de leitura na fila e no vhost informados.

Somente depois de `Resolve-DnsName` e `Test-NetConnection` comprovarem acesso ao hostname e à
porta AMQP faz sentido repetir `--verificar-conexao`. A interface web abrir no navegador não
comprova que a porta AMQP esteja exposta.

## Arquivos e responsabilidades

| Arquivo | Responsabilidade |
|---|---|
| `analyser/schemas.py` | Valida o contrato externo `NotificacaoClassifier` e preserva `paragraph_index`. |
| `analyser/rabbitmq.py` | Configuração, conexão, ACK/NACK, reconexão e inbox JSON Lines. |
| `analyser/consumidor_rabbitmq.py` | Comando de operação do consumidor. |
| `.env.example` | Lista as variáveis necessárias sem expor segredos. |
| `testes/test_rabbitmq.py` | Cobre contrato, ACK, reentrega, deduplicação e topologia. |

## Funcionamento técnico

O consumidor usa `pika.BlockingConnection`, adequado nesta etapa porque o callback somente
valida e grava um registro pequeno no disco. O parâmetro `prefetch=1` limita o broker a uma
mensagem não confirmada por consumidor, reduzindo o risco de acumular mensagens em memória.

Para um evento válido, o arquivo `inbox_eventos.jsonl` recebe, entre outros campos:

```json
{
  "event_id": "sha256-do-payload",
  "tipo": "recebido",
  "article_id": 123,
  "pipeline_version": "v1",
  "payload": {
    "classificacoes": [
      {"paragraph_index": 3}
    ]
  }
}
```

Se o mesmo payload for reenviado após uma queda antes do ACK, o hash já estará no inbox. O
consumidor não grava uma segunda linha e confirma a mensagem normalmente. Se não conseguir
gravar no arquivo, executa `basic_nack(..., requeue=True)`, mantendo a mensagem disponível no
RabbitMQ.

Uma mensagem que não segue o schema é registrada como `tipo: "rejeitado"`, com seu payload e
o erro de validação. Ela é confirmada depois dessa preservação para não bloquear a fila em um
loop infinito de mensagens inválidas.

## Como validar a instalação

Instalar dependências:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Validar credenciais e conectividade sem consumir mensagens:

```powershell
.\.venv\Scripts\python.exe -m analyser.consumidor_rabbitmq --verificar-conexao
```

Iniciar o consumidor:

```powershell
.\.venv\Scripts\python.exe -m analyser.consumidor_rabbitmq --saida saida
```

Depois de publicar uma mensagem do Classifier, conferir `saida/inbox_eventos.jsonl`. A presença
de `payload.classificacoes[].paragraph_index` comprova que o índice chegou ao Analyser.

## Testes executados

Os testes automatizados cobrem:

1. leitura dos parâmetros confirmados (`vhost`, TLS, heartbeat, prefetch e topologia);
2. rejeição de configuração incompleta;
3. validação estrita do payload e preservação de `paragraph_index`;
4. gravação antes do ACK;
5. deduplicação depois de uma possível reentrega;
6. preservação de mensagem inválida sem loop na fila;
7. NACK com requeue quando o inbox falha;
8. garantia de que a topologia não é declarada com o valor confirmado `false`.

## Limitações e próximos passos

Esta implementação não resume nem consolida mensagens assim que elas chegam. Isso é
intencional: o evento contém múltiplas classificações de trecho, enquanto `ItemEntrada` exige
um único `rotulo` por notícia. A regra demonstrativa de maioria com empate neutro não foi
promovida automaticamente a regra de produção.

Para executar a próxima camada, ainda é necessário decidir formalmente:

1. como agregar rótulos de vários trechos em um rótulo de notícia;
2. como identificar canonicamente o artigo quando id e link divergem;
3. como enriquecer a mensagem com os textos de `paragrafo_analisado` somente por leitura;
4. se o inbox local deve continuar como protótipo ou migrar para persistência própria do
   Analyser.

O inbox JSON Lines é adequado a uma única instância consumidora. Várias réplicas exigem uma
persistência compartilhada e coordenação adicional. Qualquer criação de tabela para isso só
poderá ocorrer após autorização explícita.

## Rollback

Para interromper o consumidor, usar `Ctrl+C`. Ele não alterou o banco. Para deixar de usar a
integração, basta não executar `analyser.consumidor_rabbitmq`; o CLI existente
`analyser.cli --entrada ...` permanece independente da fila.

## Glossário

- **ACK:** confirmação ao RabbitMQ de que uma mensagem já foi registrada e pode sair da fila.
- **NACK:** não confirmação; com `requeue=true`, o broker disponibiliza a mensagem novamente.
- **Heartbeat:** sinal periódico que detecta conexões interrompidas.
- **Prefetch:** máximo de mensagens ainda não confirmadas entregues a um consumidor.
- **Vhost:** espaço lógico isolado de filas, exchanges e permissões dentro do RabbitMQ.
- **JSON Lines:** arquivo com um objeto JSON por linha, apropriado para acrescentar eventos.
