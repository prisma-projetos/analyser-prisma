-- Migration 001 — tabelas próprias do Prisma Analyser
--
-- ESCOPO: cria quatro tabelas novas, todas com prefixo `analyser_`.
-- NÃO altera, remove ou renomeia coluna alguma de tabela existente. De
-- `tabela_classificacao`, `tabela_midia` e `entidade_matching` o Analyser apenas LÊ.
--
-- Aplicar primeiro num schema descartável:
--     CREATE SCHEMA analyser_teste;
--     SET search_path TO analyser_teste;
--     \i migrations/001_analyser.sql
--     DROP SCHEMA analyser_teste CASCADE;
--
-- Não usa `IF NOT EXISTS` de propósito: rodar duas vezes deve falhar em vez de mascarar
-- que o estado do banco não é o esperado.
--
-- A descrição coluna a coluna está em docs/pdf/Dicionario_Dados_Analyser.pdf.


-- ─────────────────────────────────────────────────────────────────────────────────────
-- 1. analyser_inbox_evento — uma linha por mensagem recebida do Classifier
--
-- Impede processar a mesma mensagem duas vezes. O RabbitMQ reentrega sempre que o ACK se
-- perde; sem este registro, a mesma notícia seria resumida e cobrada ao modelo de novo.
-- ─────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE analyser_inbox_evento (
    evento_id        BIGSERIAL   PRIMARY KEY,
    -- SHA-256 do corpo da mensagem. É o mecanismo de deduplicação: a mensagem do
    -- Classifier não traz identificador próprio, então o hash faz esse papel.
    payload_hash     CHAR(64)    NOT NULL UNIQUE,
    article_id       BIGINT      NOT NULL,
    pipeline_version VARCHAR(50) NOT NULL,
    -- A mensagem inteira, como chegou. Guarda também schema_version, job_id e
    -- notificado_em, que não viram coluna porque quase nunca se filtra por eles.
    payload_json     JSONB       NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'recebido'
                     CHECK (status IN ('recebido','processado','rejeitado','erro')),
    tentativas       INT         NOT NULL DEFAULT 0,
    recebido_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processado_em    TIMESTAMPTZ,
    erro             TEXT
);

CREATE INDEX ix_analyser_inbox_artigo ON analyser_inbox_evento (article_id, pipeline_version);

-- Índice parcial: a varredura de recuperação só procura o que ainda não terminou.
CREATE INDEX ix_analyser_inbox_pendente ON analyser_inbox_evento (status)
    WHERE status <> 'processado';


-- ─────────────────────────────────────────────────────────────────────────────────────
-- 2. analyser_item — uma linha por artigo × ator, já com o resumo
--
-- O resumo é dos TRECHOS classificados para aquele ator, nunca do artigo inteiro: é só
-- isso que existe no prisma-dev. Um artigo que cita cinco atores gera cinco linhas — uma
-- por ator, nunca uma por classificação.
-- ─────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE analyser_item (
    -- Artigo E ator: art_1__politico_122418060191. Não inclui pipeline_version, para uma
    -- reclassificação do mesmo artigo atualizar a mesma linha.
    item_id                       TEXT         PRIMARY KEY,
    evento_id                     BIGINT       NOT NULL
                                  REFERENCES analyser_inbox_evento (evento_id),
    article_id                    BIGINT       NOT NULL,
    entity_type                   VARCHAR(20)  NOT NULL
                                  CHECK (entity_type IN ('politico','partido')),
    -- Mesma convenção de tabela_classificacao: um dos dois é sempre nulo. VARCHAR(20)
    -- confere com tabela_classificacao e tabela_politico (maior valor em uso: 12 chars).
    nr_titulo_eleitoral_candidato VARCHAR(20),
    nr_partido                    INT,
    -- Retrato do nome no momento da gravação, lido de entidade_matching.nome_principal.
    -- Se a carga do TSE for refeita, esta coluna preserva o nome usado na geração do
    -- texto; para o nome corrente, consultar entidade_matching.
    ator_nome                     VARCHAR(255) NOT NULL,
    id_midia                      INT,
    link_noticia                  TEXT,
    data_publicacao               TIMESTAMPTZ,
    -- 'YYYY-MM'. É o agrupamento do panorama; sai da data de publicação.
    janela_mes                    CHAR(7)      NOT NULL,
    rotulo_definitivo             VARCHAR(10)  NOT NULL
                                  CHECK (rotulo_definitivo IN ('positivo','negativo','neutro')),
    -- Qual regra produziu o rótulo (ex.: 'maioria-v1'). Sem isto, trocar a regra no
    -- futuro tornaria os rótulos antigos incomparáveis com os novos.
    regra_agregacao_versao        VARCHAR(30)  NOT NULL,
    -- As classificações recebidas para este ator, uma entrada por trecho. É onde mora o
    -- paragraph_index, que NÃO existe em tabela_classificacao — só na mensagem.
    trechos_json                  JSONB        NOT NULL,
    -- SHA-256 de título + texto enviados ao modelo. Detecta insumo alterado sob o mesmo
    -- item_id e é o que força refazer o resumo quando o título deixar de ser sintético.
    conteudo_hash                 CHAR(64)     NOT NULL,
    -- Nulos entre a criação do item e a chamada ao modelo.
    resumo                        TEXT,
    temas_json                    JSONB,
    modelo                        VARCHAR(100),
    hash_prompt_resumo            CHAR(64),
    resumido_em                   TIMESTAMPTZ,
    status                        VARCHAR(20)  NOT NULL DEFAULT 'novo',
    criado_em                     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    atualizado_em                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_analyser_item_uma_entidade
        CHECK (num_nonnulls(nr_titulo_eleitoral_candidato, nr_partido) = 1)
);

CREATE INDEX ix_analyser_item_janela ON analyser_item
    (janela_mes, entity_type, nr_titulo_eleitoral_candidato, nr_partido);

CREATE INDEX ix_analyser_item_artigo ON analyser_item (article_id);


-- ─────────────────────────────────────────────────────────────────────────────────────
-- 3. analyser_execucao — uma linha por rodada de processamento
--
-- É o único registro que atravessa vários itens e vários panoramas, e por isso não cabe
-- nas outras tabelas.
-- ─────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE analyser_execucao (
    execucao_id          BIGSERIAL   PRIMARY KEY,
    numero_execucao      INT         NOT NULL,
    status               VARCHAR(20) NOT NULL
                         CHECK (status IN ('em_andamento','concluida','falhou')),
    -- Teto configurado, NÃO a quantidade processada. A distinção é registrada de
    -- propósito: já houve relatório que confundiu as duas.
    capacidade_lote      INT         NOT NULL,
    quantidade_eventos   INT         NOT NULL DEFAULT 0,
    quantidade_itens     INT         NOT NULL DEFAULT 0,
    quantidade_resumos   INT         NOT NULL DEFAULT 0,
    quantidade_panoramas INT         NOT NULL DEFAULT 0,
    modelo               VARCHAR(100),
    iniciado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizado_em        TIMESTAMPTZ,
    auditoria_json       JSONB,
    erro                 TEXT
);


-- ─────────────────────────────────────────────────────────────────────────────────────
-- 4. analyser_panorama — todas as versões, com as fontes dentro
--
-- Versionado e nunca sobrescrito: cada recálculo cria uma versão nova e as anteriores
-- permanecem para auditoria. A API expõe apenas a maior.
-- ─────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE analyser_panorama (
    panorama_id                   BIGSERIAL    PRIMARY KEY,
    execucao_id                   BIGINT       REFERENCES analyser_execucao (execucao_id),
    entity_type                   VARCHAR(20)  NOT NULL
                                  CHECK (entity_type IN ('politico','partido')),
    nr_titulo_eleitoral_candidato VARCHAR(20),
    nr_partido                    INT,
    ator_nome                     VARCHAR(255) NOT NULL,
    janela_mes                    CHAR(7)      NOT NULL,
    eixo                          VARCHAR(10)  NOT NULL
                                  CHECK (eixo IN ('positivo','negativo','neutro')),
    versao                        INT          NOT NULL CHECK (versao >= 1),
    -- Hoje repete o eixo. Fica por compatibilidade de contrato; não serve mais para
    -- escolher componente no frontend.
    formato                       VARCHAR(20)  NOT NULL,
    total_itens                   INT          NOT NULL,
    -- O parágrafo consolidado. É o conteúdo que o cidadão lê, e nunca é vazio.
    texto                         TEXT         NOT NULL,
    -- Temas com id (slug do nome), contagem de matérias e os item_id que sustentam cada um.
    temas_json                    JSONB        NOT NULL,
    fontes_json                   JSONB        NOT NULL,
    -- Divergências que a verificação registrou sem corrigir.
    avisos_json                   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    quantidade_lotes              INT          NOT NULL,
    modelo                        VARCHAR(100),
    hashes_prompts_json           JSONB,
    criado_em                     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    publicado_em                  TIMESTAMPTZ,
    CONSTRAINT ck_analyser_panorama_uma_entidade
        CHECK (num_nonnulls(nr_titulo_eleitoral_candidato, nr_partido) = 1)
);

-- O COALESCE não é enfeite. Um dos campos de ator é SEMPRE nulo, e no comportamento
-- padrão do PostgreSQL dois NULL são distintos numa restrição UNIQUE — um UNIQUE comum
-- não impediria duas versões iguais do mesmo panorama. Em PG 15+, a alternativa direta
-- seria UNIQUE NULLS NOT DISTINCT.
CREATE UNIQUE INDEX uk_analyser_panorama_versao ON analyser_panorama (
    entity_type,
    COALESCE(nr_titulo_eleitoral_candidato, ''),
    COALESCE(nr_partido, -1),
    janela_mes,
    eixo,
    versao
);
