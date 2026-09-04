"""O pipeline do Analyser, em seis etapas.

A ideia que organiza tudo: o resumo por notícia é caro e é pago uma vez só; o panorama é
recalculado do zero quando uma notícia inédita altera uma janela. A classificação recebida
do Classifier é definitiva e faz parte do registro imutável da notícia.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from analyser.armazenamento import (
    Armazenamento,
    DestinoArmazenamento,
    agora_iso,
    carimbo_execucao,
)
from analyser.config import EIXOS, FORMATO_POR_EIXO, Config, Eixo
from analyser.modelos import (
    ErroDeCobertura,
    ErroDeValidacao,
    Modelo,
    gerar_validado,
)
from analyser.prompts import Prompts
from analyser.schemas import (
    AtorPolitico,
    ConsolidacaoParcial,
    Panorama,
    RegistroPanorama,
    RegistroResumo,
    ResumoItem,
    Rodada,
)


@dataclass(slots=True)
class ResultadoExecucao:
    """Tudo o que uma execução produziu, para impressão e auditoria."""

    ator_id: str
    ator_nome: str
    rodada: str
    numero_rodada: int = 0
    ids_entrada: list[str] = field(default_factory=list)
    ids_do_cache: list[str] = field(default_factory=list)
    ids_resumidos_agora: list[str] = field(default_factory=list)
    janelas_sujas: list[str] = field(default_factory=list)
    motivo_janela: dict[str, list[str]] = field(default_factory=dict)
    panoramas: list[RegistroPanorama] = field(default_factory=list)
    eixos_vazios: list[tuple[str, str]] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    contagem_eixo: dict[str, Counter[str]] = field(default_factory=dict)
    contagem_veiculo: dict[str, Counter[str]] = field(default_factory=dict)
    erros: list[str] = field(default_factory=list)
    modelo: str = ""
    hashes_prompts: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    lotes_processados: list[dict[str, Any]] = field(default_factory=list)
    carimbo: str = ""
    caminho_auditoria: str = ""

    @property
    def economia(self) -> tuple[int, int]:
        """(itens vindos do cache, itens resumidos agora)."""
        return len(self.ids_do_cache), len(self.ids_resumidos_agora)


class ConflitoClassificacao(ValueError):
    """Mesmo `item_id` recebido com rótulo diferente do registro imutável."""


#: Tudo que não é letra sem acento ou dígito vira separador no id do tema.
_NAO_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(texto: str) -> str:
    """Converte o nome de um tema em id legível e estável.

    Legível de propósito: `campanha-de-reeleicao-de-lula` diz o que é ao ser lido num
    log ou numa URL, enquanto um hash exigiria consultar o banco para saber de que
    tema se trata. Acentos são removidos para o id sobreviver a URL, nome de arquivo e
    comparação entre bancos com collation diferente.
    """
    decomposto = unicodedata.normalize("NFD", texto.lower())
    sem_acento = "".join(c for c in decomposto if unicodedata.category(c) != "Mn")
    return _NAO_SLUG.sub("-", sem_acento).strip("-")


#: Um id vazado para dentro do parágrafo do panorama. Os três fragmentos de formato
#: proíbem ids na prosa — a rastreabilidade mora em `temas[].fontes` e em `fontes`, e o
#: texto corre limpo para quem lê. Esta expressão existe para detectar a desobediência e
#: transformá-la em aviso de auditoria, em vez de deixá-la chegar à tela.
#:
#: O filtro exige o sublinhado porque todo id deste módulo o tem (`not_001`,
#: `art_408__politico_122418060191`), e um parêntese de texto corrido não — é o que
#: separa uma citação de "(Republicanos)" numa frase sobre um partido.
_ID_ENTRE_PARENTESES = re.compile(r"\(([^)]*)\)")


def _ids_no_texto(texto: str) -> set[str]:
    """Ids citados entre parênteses ao longo do parágrafo do panorama."""
    encontrados: set[str] = set()
    for grupo in _ID_ENTRE_PARENTESES.findall(texto):
        for pedaco in re.split(r"[,;]", grupo):
            candidato = pedaco.strip()
            if "_" in candidato and " " not in candidato:
                encontrados.add(candidato)
    return encontrados


class Analyser:
    """Orquestra as seis etapas sobre uma rodada."""

    def __init__(
        self,
        config: Config,
        modelo: Modelo,
        armazenamento: DestinoArmazenamento | None = None,
    ) -> None:
        self.config = config
        self.modelo = modelo
        # O destino é injetável para o mesmo pipeline servir JSON e PostgreSQL sem saber
        # qual dos dois está do outro lado. O padrão continua sendo JSON, e é o que mantém
        # a suíte inteira rodando sem banco.
        self.armazenamento: DestinoArmazenamento = armazenamento or Armazenamento(config)
        self.prompts = Prompts(raiz=config.prompts)
        self.prompts.precarregar()
        self._tokens: Counter[str] = Counter()

    # ----------------------------------------------------------------------------------

    def executar(self) -> ResultadoExecucao:
        """Roda o pipeline inteiro sobre o arquivo de entrada da configuração."""
        rodada = Rodada.model_validate_json(
            self.config.entrada.read_text(encoding="utf-8")
        )
        nome_rodada = self.config.entrada.name
        resultado = ResultadoExecucao(
            ator_id=rodada.ator.id,
            ator_nome=rodada.ator.nome,
            rodada=nome_rodada,
            numero_rodada=self.armazenamento.proximo_numero_rodada(),
            ids_entrada=[i.id for i in rodada.itens],
            modelo=self.modelo.nome,
            carimbo=carimbo_execucao(),
        )

        janelas_sujas = self._etapa_1_e_2(rodada, resultado)
        resultado.janelas_sujas = sorted(janelas_sujas)

        for janela in resultado.janelas_sujas:
            self._recalcular_janela(rodada, janela, resultado)

        self._contabilizar(rodada, resultado)
        self._gravar_auditoria(resultado)
        return resultado

    # -- etapas 1 e 2 ------------------------------------------------------------------

    def _etapa_1_e_2(self, rodada: Rodada, resultado: ResultadoExecucao) -> set[str]:
        """Resume o que é inédito e marca as janelas tocadas por itens novos."""
        ja_resumidos = self.armazenamento.ler_resumos()
        self._validar_classificacoes_imutaveis(rodada, ja_resumidos)
        sujas: set[str] = set()

        for item in rodada.itens:
            inedito = item.id not in ja_resumidos

            if inedito:
                try:
                    self._resumir(rodada.ator, item)
                except ErroDeValidacao as erro:
                    resultado.erros.append(f"{item.id}: {erro}")
                    continue
                resultado.ids_resumidos_agora.append(item.id)
            else:
                # Etapa 1: já existe resumo, então nem se chama o modelo. É aqui que mora
                # a economia que o protótipo precisa demonstrar.
                resultado.ids_do_cache.append(item.id)

            if inedito:
                sujas.add(item.janela)
                resultado.motivo_janela.setdefault(item.janela, [])
                marca = f"{item.id} (item novo)"
                if marca not in resultado.motivo_janela[item.janela]:
                    resultado.motivo_janela[item.janela].append(marca)

        return sujas

    @staticmethod
    def _validar_classificacoes_imutaveis(
        rodada: Rodada, ja_resumidos: dict[str, RegistroResumo]
    ) -> None:
        """Rejeita conflito de rótulo antes de qualquer escrita da rodada.

        O mesmo `item_id` pode reaparecer para demonstrar o cache, mas precisa trazer a
        mesma classificação definitiva. A validação antecede o laço de gravação para uma
        rodada inválida não deixar resumos novos parcialmente persistidos.
        """
        for item in rodada.itens:
            existente = ja_resumidos.get(item.id)
            if existente is None or existente.rotulo == item.rotulo:
                continue
            raise ConflitoClassificacao(
                f"O item {item.id} já foi classificado como {existente.rotulo!r} e "
                f"reapareceu como {item.rotulo!r}. A classificação é imutável."
            )

    def _resumir(self, ator: AtorPolitico, item: Any) -> None:
        """Etapa 1 (map): uma chamada por notícia inédita, gravada uma única vez.

        A chamada não recebe o rótulo do classificador, para o texto permanecer imparcial.
        Depois da resposta do modelo, a classificação definitiva é anexada ao registro
        persistido e usada apenas para agrupamento.
        """
        prompt_resumo = self.prompts.montar_resumo_item(
            ator_nome=ator.nome,
            ator_tipo=ator.tipo,
            item_id=item.id,
            veiculo=item.veiculo,
            data=item.data.isoformat(),
            url=item.url,
            titulo=item.titulo,
            texto=item.texto,
        )
        resumo = gerar_validado(
            self.modelo,
            prompt=prompt_resumo.texto,
            schema=ResumoItem,
            tarefa="resumo_item",
            contexto={"item_id": item.id, "titulo": item.titulo, "texto": item.texto},
        )
        self._somar_tokens(resumo.tokens)

        self.armazenamento.gravar_resumo(
            RegistroResumo(
                item_id=item.id,
                ator_id=ator.id,
                janela=item.janela,
                rotulo=item.rotulo,
                veiculo=item.veiculo,
                url=item.url,
                data=item.data,
                titulo=item.titulo,
                resumo=resumo.dados.resumo,
                temas=resumo.dados.temas,
                fonte_id=resumo.dados.fonte_id,
                modelo=self.modelo.nome,
                hash_prompt_resumo=prompt_resumo.sha256,
                criado_em=agora_iso(),
            )
        )

    # -- etapas 3 a 6 ------------------------------------------------------------------

    def _recalcular_janela(
        self, rodada: Rodada, janela: str, resultado: ResultadoExecucao
    ) -> None:
        """Reagrupa e reconsolida uma janela suja, do zero."""
        grupos = self._etapa_3_agrupar(rodada.ator.id, janela)

        for eixo in EIXOS:
            resumos = grupos.get(eixo, [])
            if not resumos:
                resultado.eixos_vazios.append((janela, eixo))
                continue
            try:
                registro = self._etapa_4_e_5(rodada, janela, eixo, resumos, resultado)
            except ErroDeValidacao as erro:
                resultado.erros.append(f"{janela}/{eixo}: {erro}")
                continue
            resultado.panoramas.append(registro)

    def _etapa_3_agrupar(self, ator_id: str, janela: str) -> dict[Eixo, list[RegistroResumo]]:
        """Agrupa pela classificação definitiva todos os resumos da janela. Python puro.

        Note que a base é o conjunto completo de resumos da janela, não apenas os itens
        da rodada: o panorama é recalculado do zero, sempre.
        """
        grupos: dict[Eixo, list[RegistroResumo]] = defaultdict(list)
        for registro in self.armazenamento.ler_resumos_da_janela(
            ator_id=ator_id, janela=janela
        ):
            grupos[registro.rotulo].append(registro)
        for lista in grupos.values():
            lista.sort(key=lambda r: r.item_id)
        return grupos

    def _etapa_4_e_5(
        self,
        rodada: Rodada,
        janela: str,
        eixo: Eixo,
        resumos: list[RegistroResumo],
        resultado: ResultadoExecucao,
    ) -> RegistroPanorama:
        """Consolidação em dois níveis, verificação de contagem e gravação versionada.

        A entrada são sempre os resumos curtos da etapa 1 — nunca os textos completos das
        notícias, nunca um panorama de rodada anterior.
        """
        lotes = [
            resumos[i : i + self.config.lote] for i in range(0, len(resumos), self.config.lote)
        ]

        # Nível 1: uma consolidação parcial por lote.
        parciais: list[ConsolidacaoParcial] = []
        for numero_lote, lote in enumerate(lotes, start=1):
            carga = [
                {"fonte_id": r.fonte_id, "resumo": r.resumo, "temas": r.temas, "veiculo": r.veiculo}
                for r in lote
            ]
            prompt_lote = self.prompts.montar_consolidacao_lote(
                eixo=eixo, resumos=json.dumps(carga, ensure_ascii=False, indent=2)
            )
            parcial = gerar_validado(
                self.modelo,
                prompt=prompt_lote.texto,
                schema=ConsolidacaoParcial,
                tarefa="consolidacao_lote",
                contexto={"resumos": carga, "eixo": eixo},
            )
            self._somar_tokens(parcial.tokens)
            parciais.append(parcial.dados)
            resultado.lotes_processados.append(
                {
                    "janela": janela,
                    "eixo": eixo,
                    "numero_lote": numero_lote,
                    "quantidade_noticias": len(lote),
                }
            )

        # Nível 2: as parciais viram o panorama do eixo.
        carga_parciais = [p.model_dump(mode="json") for p in parciais]
        prompt_final = self.prompts.montar_consolidacao_final(
            eixo=eixo,
            ator_id=rodada.ator.id,
            janela=janela,
            parciais=json.dumps(carga_parciais, ensure_ascii=False, indent=2),
            materias=len(resumos),
        )
        final = gerar_validado(
            self.modelo,
            prompt=prompt_final.texto,
            schema=Panorama,
            esquema_pedido=Panorama.esquema_do_modelo(),
            tarefa="consolidacao_final",
            contexto={
                "parciais": carga_parciais,
                "eixo": eixo,
                "janela": janela,
                "ator_id": rodada.ator.id,
                "formato": FORMATO_POR_EIXO[eixo],
            },
        )
        self._somar_tokens(final.tokens)

        # Escrituração antes de qualquer verificação ou gravação: o conjunto de entrada
        # desta consolidação é conhecido aqui, exatamente, e é ele que define tanto o que
        # o panorama cobre quanto quais itens viram `consolidado`. O modelo não é mais
        # perguntado sobre isso — ver `Panorama.fontes`.
        final.dados.fontes = [r.fonte_id for r in resumos]
        final.dados.total_itens = len(final.dados.fontes)

        self._atribuir_ids_de_tema(final.dados)
        avisos = self._normalizar_contagens(final.dados)
        avisos += self._etapa_6_verificar(final.dados, resumos)
        for aviso in avisos:
            resultado.avisos.append(f"[{janela}/{eixo}] {aviso}")

        self._verificar_cobertura(final.dados, resumos, janela=janela, eixo=eixo)

        registro = RegistroPanorama(
            ator_id=rodada.ator.id,
            janela=janela,
            eixo=eixo,
            versao=self.armazenamento.proxima_versao(
                ator_id=rodada.ator.id, janela=janela, eixo=eixo
            ),
            formato=FORMATO_POR_EIXO[eixo],
            panorama=final.dados,
            avisos=avisos,
            lotes=len(lotes),
            modelo=self.modelo.nome,
            hash_prompt_lote=self.prompts.carregar("consolidacao_lote.txt").sha256,
            hash_prompt_final=prompt_final.sha256,
            hash_prompt_formato=self.prompts.hash_formato(eixo),
            rodada=resultado.rodada,
            criado_em=agora_iso(),
        )
        self.armazenamento.gravar_panorama(registro)
        return registro

    @staticmethod
    def _atribuir_ids_de_tema(panorama: Panorama) -> None:
        """Dá a cada tema um id estável, derivado do próprio nome.

        O id não é pedido ao modelo de propósito. Quem gera texto livre não produz
        identificador estável: o mesmo assunto voltaria com id diferente a cada
        execução, e um id que muda sozinho não serve para nada. Derivando do nome, o
        mesmo tema mantém o mesmo id entre versões do panorama e entre janelas — que é
        o que permite ao frontend acompanhar um assunto ao longo do tempo.

        Diferente de `_normalizar_contagens`, aqui não há aviso a registrar: nada está
        sendo corrigido, só preenchido onde o modelo nunca escreveu.
        """
        vistos: Counter[str] = Counter()
        for tema in panorama.temas:
            base = _slug(tema.tema) or "tema"
            vistos[base] += 1
            # Dois temas podem cair no mesmo slug ("Lula" e "lula"). O sufixo mantém o
            # id único dentro do panorama sem perder a legibilidade do primeiro.
            tema.id = base if vistos[base] == 1 else f"{base}-{vistos[base]}"

    @staticmethod
    def _normalizar_contagens(panorama: Panorama) -> list[str]:
        """Deriva as contagens da própria resposta, em vez de aceitar as declaradas.

        `ocorrencias` e `total_itens` não são afirmações do modelo sobre a cobertura —
        são o tamanho de listas que ele mesmo escreveu, na mesma resposta. Perguntá-los
        é convidar erro justamente onde `len()` dá a resposta exata. Medido com o
        Qwen3.5-9B: um tema declarou 3 ocorrências listando 2 fontes, e o mesmo tema
        havia agrupado corretamente as duas matérias que cobriam o mesmo fato.

        Isto NÃO é conserto silencioso, e a diferença importa. Toda substituição vira
        aviso registrado na auditoria, exatamente como viraria se a etapa 6 apenas
        apontasse a divergência. O que muda é que o número gravado passa a ser o certo:
        `ocorrencias` ordena os temas no painel, então um número inflado promove um tema
        acima do que as matérias sustentam — o cidadão veria como mais relevante algo
        que não é.

        A distinção com a etapa 6 é o tipo de alegação. Verificar se um id citado existe
        no conjunto é conferir uma afirmação sobre o CONTEÚDO, e ali corrigir seria
        adulterar o que o modelo disse. Aqui não há alegação a preservar: há aritmética
        sobre uma lista que está à vista.
        """
        avisos: list[str] = []

        for tema in panorama.temas:
            real = len(set(tema.fontes))
            if tema.ocorrencias != real:
                avisos.append(
                    f"tema '{tema.tema}' declarou {tema.ocorrencias} ocorrências e lista "
                    f"{real} fontes distintas; gravado {real}"
                )
                tema.ocorrencias = real

        total = len(set(panorama.fontes))
        if panorama.total_itens != total:
            avisos.append(
                f"total_itens declarado é {panorama.total_itens} e o panorama lista "
                f"{total} fontes distintas; gravado {total}"
            )
            panorama.total_itens = total

        return avisos

    @staticmethod
    def _etapa_6_verificar(panorama: Panorama, resumos: list[RegistroResumo]) -> list[str]:
        """Confere ids e contagens em Python. Divergência vira aviso, nunca correção."""
        validos = {r.fonte_id for r in resumos}
        avisos: list[str] = []

        citados: set[str] = set(panorama.fontes)
        for tema in panorama.temas:
            citados.update(tema.fontes)

        # O parágrafo é lido por um cidadão e corre sem códigos: a rastreabilidade mora em
        # `temas[].fontes` e em `fontes`. Um id que apareça na prosa não é rastreabilidade
        # a mais — é o formato desobedecido, e o cidadão lendo "(art_408)" no meio do texto.
        no_paragrafo = _ids_no_texto(panorama.texto)
        if no_paragrafo:
            avisos.append(
                "o parágrafo cita ids entre parênteses "
                f"({', '.join(sorted(no_paragrafo))}); o formato pede o texto sem ids, "
                "com a rastreabilidade em temas e fontes"
            )

        inexistentes = sorted(citados - validos)
        if inexistentes:
            avisos.append(
                f"ids citados que não existem no conjunto de entrada: {', '.join(inexistentes)}"
            )

        if panorama.total_itens != len(validos):
            avisos.append(
                f"total_itens declarado é {panorama.total_itens}, "
                f"mas o conjunto de entrada tem {len(validos)} itens"
            )

        for tema in panorama.temas:
            reais = len(set(tema.fontes))
            if tema.ocorrencias != reais:
                avisos.append(
                    f"tema '{tema.tema}' declara {tema.ocorrencias} ocorrências, "
                    f"mas lista {reais} fontes distintas"
                )

        faltantes = sorted(validos - set(panorama.fontes))
        if faltantes:
            avisos.append(
                f"itens do conjunto que o panorama não cita em fontes: {', '.join(faltantes)}"
            )
        return avisos

    @staticmethod
    def _verificar_cobertura(
        panorama: Panorama, resumos: list[RegistroResumo], *, janela: str, eixo: Eixo
    ) -> None:
        """Um panorama que não cita toda a sua entrada NÃO pode ser gravado.

        Extraído como método próprio — em vez de inline em `_etapa_4_e_5` — para
        continuar testável por si mesmo depois de 3 de setembro de 2026, quando `fontes`
        passou a ser preenchida pelo Python a partir do próprio `resumos`, e não mais
        pedida ao modelo (ver `Panorama.fontes`). Essa mudança tornou esta condição
        impossível de falhar pelo caminho normal: `_etapa_4_e_5` atribui
        `panorama.fontes = [r.fonte_id for r in resumos]` antes de chamar isto, então
        `nao_citados` é sempre vazio por construção.

        A checagem continua existindo como arame de tropeço, não decoração: protege
        contra uma mudança futura que devolva o campo ao modelo, ou que filtre a lista
        entre a atribuição e a gravação. `gravar_panorama` marca `consolidado` exatamente
        os ids de `panorama.fontes` — um item fora dessa lista fica órfão em `resumido`,
        uma perda invisível e uma pendência eterna.

        Foi assim que a janela de agosto de 2026 do Lula gravou 47 das 94 matérias com
        status de sucesso, quando isto ainda era apenas um aviso registrado e ignorado.
        Falhar é a alternativa segura: nada gravado, resumos intactos, erro na auditoria.
        """
        nao_citados = {r.fonte_id for r in resumos} - set(panorama.fontes)
        if nao_citados:
            raise ErroDeCobertura(
                f"O panorama de {janela}/{eixo} cita {len(panorama.fontes)} das "
                f"{len(resumos)} matérias da entrada e deixaria {len(nao_citados)} itens "
                f"órfãos em 'resumido'. Não foi gravado. Ids ausentes: "
                f"{', '.join(sorted(nao_citados)[:10])}"
                f"{' ...' if len(nao_citados) > 10 else ''}"
            )

    # -- contagens e auditoria ---------------------------------------------------------

    def _contabilizar(self, rodada: Rodada, resultado: ResultadoExecucao) -> None:
        """Contagem por eixo e por veículo nas janelas recalculadas."""
        for janela in resultado.janelas_sujas:
            por_eixo: Counter[str] = Counter()
            for registro in self.armazenamento.ler_resumos().values():
                if registro.ator_id != rodada.ator.id or registro.janela != janela:
                    continue
                por_eixo[registro.rotulo] += 1
                resultado.contagem_veiculo.setdefault(registro.veiculo, Counter())
                resultado.contagem_veiculo[registro.veiculo][registro.rotulo] += 1
            resultado.contagem_eixo[janela] = por_eixo

    def _somar_tokens(self, tokens: dict[str, int] | None) -> None:
        if tokens:
            self._tokens.update(tokens)

    @property
    def tokens(self) -> dict[str, int]:
        """Tokens de prompt e de resposta acumulados por esta instância.

        Público porque o `Worker` chama `_recalcular_janela` diretamente, sem passar por
        `executar()`, e portanto nunca chega em `_gravar_auditoria`. Sem este acessor o
        consumo de uma consolidação do worker não era registrado em lugar nenhum — foi
        por isso que a execução de agosto de 2026 não soube dizer quantos tokens gastou,
        justamente na execução em que o tamanho dos prompts era o problema.
        """
        return dict(self._tokens)

    def _gravar_auditoria(self, resultado: ResultadoExecucao) -> None:
        """Grava o registro de auditoria da execução."""
        resultado.tokens = dict(self._tokens)
        resultado.hashes_prompts = self.prompts.hashes
        dados: dict[str, Any] = {
            "timestamp": agora_iso(),
            "execucao_id": resultado.carimbo,
            "numero_rodada": resultado.numero_rodada,
            "arquivo_entrada": resultado.rodada,
            "capacidade_maxima_lote": self.config.lote,
            "lotes_processados": resultado.lotes_processados,
            # Aliases legados preservados para consumidores existentes.
            "rodada": resultado.rodada,
            "ator_id": resultado.ator_id,
            "modelo": resultado.modelo,
            "lote": self.config.lote,
            "prompts": self.prompts.hashes,
            "ids_entrada": resultado.ids_entrada,
            "ids_resumidos_agora": resultado.ids_resumidos_agora,
            "ids_do_cache": resultado.ids_do_cache,
            "janelas_recalculadas": resultado.janelas_sujas,
            "panoramas_gravados": [
                {"janela": p.janela, "eixo": p.eixo, "versao": p.versao, "lotes": p.lotes}
                for p in resultado.panoramas
            ],
            "chamadas_por_tarefa": dict(self.modelo.chamadas_por_tarefa),
            "chamadas_total": self.modelo.chamadas,
            "tokens": resultado.tokens,
            "avisos_verificacao": resultado.avisos,
            "erros": resultado.erros,
        }
        caminho = self.armazenamento.gravar_auditoria(resultado.carimbo, dados)
        resultado.caminho_auditoria = str(caminho)
