# syntax=docker/dockerfile:1
#
# Imagem do Prisma · Analyser.
#
# Este projeto NÃO é um servidor HTTP: é um conjunto de entrypoints de linha de
# comando (analyser.cli, analyser.consumidor_rabbitmq, scripts.drenar_fila,
# scripts.processar, scripts.aplicar_migration). Por isso a imagem não fixa um
# CMD "de produção" único — ela só garante que o ambiente Python + dependências
# estejam prontos, e o comando a rodar é escolhido em `docker run` ou no
# `command:` do compose.
#
# Build (a partir da raiz do projeto, não de dentro de docker/):
#   docker build -f docker/Dockerfile -t prisma-analyser .
#
# Exemplos de uso:
#   docker run --rm prisma-analyser \
#       python -m analyser.cli --entrada dados/rodada_1.json
#
#   docker run --rm --env-file .env prisma-analyser \
#       python -m scripts.processar --eventos 50 --consolidar-tudo
#
#   docker run --rm --env-file .env prisma-analyser \
#       python -m analyser.consumidor_rabbitmq --verificar-conexao

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED: logs aparecem em tempo real (importante para `docker logs -f`
#   em processos de longa duração como o consumidor RabbitMQ).
# PYTHONDONTWRITEBYTECODE: não grava .pyc no volume da imagem.
# PIP_NO_CACHE_DIR / PIP_DISABLE_PIP_VERSION_CHECK: build mais enxuto e silencioso.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] já embute o libpq no próprio wheel, então não é preciso
# libpq-dev/gcc para compilar nada. Mantemos apenas ca-certificates, exigido
# para TLS ao falar com RabbitMQ (RABBITMQ_USE_TLS=true) e com provedores de
# modelo via HTTPS (ModeloAPI / httpx).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copia só o requirements primeiro para aproveitar o cache de camadas do Docker:
# o `pip install` só roda de novo se requirements.txt mudar.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Agora o restante do código-fonte.
COPY . .

# Usuário não-root. O container escreve em `saida/` (JSON append-only) e em
# `.pytest_tmp/` durante testes, então essas pastas precisam pertencer a ele.
RUN useradd --create-home --uid 1000 analyser \
    && chown -R analyser:analyser /app
USER analyser

# Sem ENTRYPOINT fixo: o comando completo (cli, worker, consumidor, migration,
# ou até `pytest`) é passado no `docker run`/`docker-compose`. Isto evita ter
# que reconstruir a imagem só para trocar qual entrypoint ela executa.
CMD ["python", "-m", "analyser.cli", "--help"]
