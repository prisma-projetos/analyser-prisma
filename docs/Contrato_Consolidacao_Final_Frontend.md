# Contrato da consolidação final para o frontend

Data: 1º de setembro de 2026

## Objetivo

Descrever, em formato utilizável pelo frontend, a estrutura real produzida pela tarefa
`consolidacao_final` do Prisma Analyser. Este documento diferencia:

- contrato confirmado no código atual;
- metadados técnicos de persistência;
- DTO proposto para a API e para o frontend;
- decisões de produto que ainda não estão implementadas.

Nenhuma tabela, endpoint ou tipo do frontend foi criado por este documento.

## Resposta direta

`consolidacao_final` retorna um objeto `Panorama`. O pipeline valida esse objeto com Pydantic,
normaliza as contagens, verifica as fontes e o grava dentro de um `RegistroPanorama`.

```text
ConsolidacaoParcial[]
        -> consolidacao_final
        -> Panorama
        -> verificacao e normalizacao em Python
        -> RegistroPanorama versionado
        -> futura API
        -> frontend
```

A identidade lógica de um panorama é:

```text
(ator_id, janela, eixo)
```

`versao` é metadado histórico. Não é um quarto componente da identidade do conteúdo.

## Decisão de transição sobre respostas registradas

Por decisão de produto informada em 1º de setembro de 2026, o campo
`respostas_registradas` foi retirado do `Panorama` e não deve ser tipado nem visualizado pelo
frontend. A remoção já foi aplicada ao schema, aos prompts e à saída; `ler_panoramas`
recusa, com mensagem explícita, diretórios de saída gravados no formato anterior.

## Contrato TypeScript alvo

```ts
export type Eixo = "positivo" | "negativo" | "neutro";
export type FormatoPanorama = Eixo;
export type FonteId = string;

export interface TemaContado {
  id: string;
  tema: string;
  ocorrencias: number;
  fontes: FonteId[];
}

export interface Panorama {
  ator_id: string;
  janela: string; // YYYY-MM
  eixo: Eixo;
  total_itens: number;
  temas: TemaContado[];
  fontes: FonteId[];
  texto: string;
}

export interface RegistroPanorama {
  ator_id: string;
  janela: string;
  eixo: Eixo;
  versao: number;
  formato: FormatoPanorama;
  panorama: Panorama;
  avisos: string[];
  lotes: number;
  modelo: string;
  hash_prompt_lote: string;
  hash_prompt_final: string;
  hash_prompt_formato: string;
  rodada: string;
  criado_em: string; // ISO 8601
}
```

## Dicionário dos campos de Panorama

| Campo | Tipo | Regra confirmada | Uso sugerido na interface |
|---|---|---|---|
| `ator_id` | `string` | Identifica o político ou partido consolidado. | Cabeçalho e rota da entidade. |
| `janela` | `string` | Mês no formato `YYYY-MM`. | Seletor de período. |
| `eixo` | `Eixo` | `positivo`, `negativo` ou `neutro`. | Aba ou seção, mantendo o nome real. |
| `total_itens` | `number` | Total de fontes distintas do panorama. A contagem é normalizada em Python. | Indicador "N notícias". |
| `temas` | `TemaContado[]` | Lista não vazia. Cada tema possui id, assunto, contagem e fontes. | Ranking ou chips de temas; use `id` como chave. |
| `fontes` | `string[]` | Todos os ids de matérias usados, sem repetição. | Base para buscar URLs e títulos na API. |
| `texto` | `string` | Sempre preenchido, nos três eixos. Um parágrafo de 3 a 6 frases, **sem ids na prosa**, abrindo por uma frase que nomeia o recorte ("As críticas registradas pela cobertura..."). Nunca vazio. | Corpo do panorama. É o conteúdo principal da tela, e pode ser exibido como veio. |

## Estruturas aninhadas

### TemaContado

- `id`: identificador estável, derivado do nome (`campanha-de-reeleicao-de-lula`). Não vem do modelo — é gerado em Python, porque um gerador de texto livre devolveria id diferente a cada execução. O mesmo tema mantém o mesmo id entre versões do panorama e entre janelas, o que permite acompanhar um assunto ao longo do tempo;
- `tema`: nome curto do assunto;
- `ocorrencias`: quantidade de fontes distintas;
- `fontes`: ids que sustentam o tema.

Após a saída do modelo, o Python substitui `ocorrencias` pelo tamanho real do conjunto de
`fontes`. O frontend não precisa recalcular, mas deve tratar o número como contagem de
matérias, não de parágrafos ou menções.

## Formato por eixo

| Eixo | Fragmento | O que o parágrafo reúne |
|---|---|---|
| `negativo` | `formatos/negativo.txt` | as críticas e cobranças que a cobertura registra |
| `positivo` | `formatos/positivo.txt` | os elogios e entregas que a cobertura registra |
| `neutro` | `formatos/neutro.txt` | os fatos que a cobertura registra sem tomar partido |

A **forma do objeto é idêntica nos três eixos**: nenhum campo é exclusivo de um formato e
nenhum vem vazio. O que muda entre eles é o que o parágrafo reúne, não a estrutura — o
frontend renderiza os três com o mesmo componente.

`formato` deixou de distinguir a estrutura e passou a repetir o eixo. Ele segue no
registro por compatibilidade, mas não serve mais para escolher componente.

## Exemplo (a forma é a mesma nos três eixos)

```json
{
  "ator_id": "politico:123",
  "janela": "2026-08",
  "eixo": "negativo",
  "total_itens": 2,
  "temas": [
    {
      "tema": "atraso em obra",
      "ocorrencias": 2,
      "fontes": ["art_101", "art_102"]
    }
  ],
  "fontes": ["art_101", "art_102"],
  "texto": "As matérias apontam atraso no cronograma da obra e registram a cobrança de moradores na sessão da Câmara (art_101, art_102). O noticiário atribui o atraso à troca da empresa contratada (art_101)."
}
```

## Exemplo do eixo neutro

```json
{
  "ator_id": "politico:123",
  "janela": "2026-08",
  "eixo": "neutro",
  "total_itens": 2,
  "temas": [
    {
      "tema": "agenda institucional",
      "ocorrencias": 2,
      "fontes": ["art_201", "art_202"]
    }
  ],
  "fontes": ["art_201", "art_202"],
  "texto": "A cobertura do mês registra compromissos da agenda institucional (art_201, art_202)."
}
```

Os exemplos são fictícios e mostram somente a forma do objeto.

## Metadados de RegistroPanorama

| Campo | Expor publicamente? | Observação |
|---|---|---|
| `versao` | Sim | A API deve retornar a maior versão publicada por ator, janela e eixo. |
| `formato` | Opcional | Repete o eixo desde que os três passaram a ter a mesma forma. Não serve para escolher componente. |
| `avisos` | Com critério | Pode virar um indicador de qualidade; mensagens técnicas completas podem ficar restritas. |
| `lotes` | Opcional | Métrica operacional, não quantidade de notícias. |
| `modelo` | Opcional | Útil em transparência e auditoria. |
| hashes de prompt | Não na tela comum | Devem permanecer disponíveis para auditoria técnica. |
| `rodada` | Não como identidade | No fluxo RabbitMQ, deve evoluir para referência de execução, não nome de arquivo. |
| `criado_em` | Sim | Pode alimentar "atualizado em". |

## DTO proposto para API e frontend

Esta estrutura não está implementada. Ela adapta o contrato interno sem destruir sua
semântica e resolve os ids de fonte para dados navegáveis.

```ts
export interface EntidadeDTO {
  id: string;
  tipo: "politico" | "partido";
  nome: string;
}

export interface FonteDTO {
  itemId: string;
  titulo: string | null;
  veiculo: string | null;
  url: string;
  dataPublicacao: string | null;
}

export interface TemaDTO {
  tema: string;
  ocorrencias: number;
  fontes: FonteDTO[];
}

export interface FatoDTO {
  descricao: string;
  fontes: FonteDTO[];
}

export interface PanoramaEixoDTO {
  eixo: Eixo;
  formato: FormatoPanorama;
  versao: number;
  totalItens: number;
  texto: string | null;
  temas: TemaDTO[];
  fatosMaisCitados: FatoDTO[];
  fontes: FonteDTO[];
  criadoEm: string;
}

export interface PanoramaJanelaDTO {
  entidade: EntidadeDTO;
  janela: string;
  eixos: Partial<Record<Eixo, PanoramaEixoDTO>>;
}
```

`Partial<Record<...>>` é necessário porque um eixo sem matérias não gera panorama. O
frontend deve exibir estado "sem cobertura neste recorte" em vez de inventar um objeto vazio.

## Mapeamento para componentes

| Componente sugerido | Dados |
|---|---|
| `PanoramaHeader` | entidade, janela, versão e data de criação. |
| `EixoTabs` | chaves presentes em `eixos`. |
| `PanoramaCompacto` | texto, temas e fontes. |
| `PanoramaDetalhado` | temas, fatos e fontes. |
| `TemaCard` | tema, ocorrências e links das fontes. |
| `FatoCard` | descrição e múltiplas fontes. |
| `FontesDrawer` | título, veículo, data e URL de todas as matérias. |

Estados mínimos de interface:

- carregando;
- erro de API;
- entidade inexistente;
- janela sem panorama;
- eixo ausente;
- lista de fatos vazia;
- fonte com título ou veículo ainda indisponível;
- panorama com aviso de qualidade.

## Incompatibilidades com o frontend atual

O frontend atual usa dados simulados e o contrato `Sintese` com `sobre`, `elogios` e
`criticas`. O Analyser produz `positivo`, `negativo` e `neutro`. Não existe equivalência
automática entre esses termos:

```text
positivo != elogio
negativo != critica
neutro   != sobre
```

Além disso, o tipo atual `Trecho` aceita uma única fonte, enquanto temas e fatos do panorama
podem possuir várias. O DTO proposto usa `fontes: FonteDTO[]` para preservar a
rastreabilidade real.

## O que não faz parte da consolidação final

- `respostas_registradas`, retirado por decisão de produto;
- texto completo da notícia;
- `paragraph_index`;
- `score_confianca` do Classifier;
- título, veículo e URL resolvidos de cada fonte;
- nome do ator;
- classificação ideológica;
- verificação factual externa.

Esses dados precisam vir de outras tabelas ou ser resolvidos pela API. A consolidação final
carrega ids de fontes, não os objetos completos das matérias.

## Critérios de aceite para iniciar a integração do frontend

1. Tipos internos e DTO externo não são tratados como o mesmo objeto.
2. `texto` aceita `null`.
3. fatos aceitam lista vazia.
4. `eixos` aceita ausência de positivo, negativo ou neutro.
5. toda afirmação exibe uma ou mais fontes navegáveis.
6. a API resolve ids de fonte para título, veículo e URL.
7. a tela usa os nomes reais dos eixos até existir decisão formal de produto.
8. o frontend mostra somente a versão publicada mais recente.
9. hashes e mensagens técnicas não são expostos na tela comum.
10. estados vazios e de erro possuem apresentação definida.

## Arquivos confirmados

- `analyser/schemas.py`: tipos atuais `TemaContado`, `Panorama` e `RegistroPanorama`;
  `RespostaRegistrada` e `FatoCitado` foram removidos;
- `analyser/config.py`: eixos, ordem e formato por eixo;
- `analyser/pipeline.py`: chamada de `consolidacao_final`, normalização, verificação e
  persistência versionada;
- `prompts/consolidacao_final.txt`: regras de fidelidade, contagem e rastreabilidade;
- `prompts/formatos/negativo.txt`, `positivo.txt` e `neutro.txt`: forma solicitada ao
  modelo e o que o parágrafo reúne em cada eixo;
- `app-prisma/app/lib/types.ts`: contrato atual e ainda simulado do frontend.

## Limitações

O documento parte do código atual, incorpora a decisão já comunicada de remover
`respostas_registradas` e propõe um DTO, mas não cria API, persistência em banco ou integração
no Next.js. O DTO precisa ser aprovado por Analyser, API e frontend antes de virar contrato de
produção.
