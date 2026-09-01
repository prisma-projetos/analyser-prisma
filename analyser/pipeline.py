"""O pipeline do Analyser, em seis etapas.

A ideia que organiza tudo: o resumo por notícia é caro e é pago uma vez só; o panorama é
recalculado do zero quando uma notícia inédita altera uma janela. A classificação recebida
do Classifier é definitiva e faz parte do registro imutável da notícia.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from analyser.armazenamento import Armazenamento, agora_iso, carimbo_execucao
from analyser.config import EIXOS, FORMATO_POR_EIXO, Config, Eixo
from analyser.modelos import ErroDeValidacao, Modelo, gerar_validado
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
    carimbo: str = ""
    caminho_auditoria: str = ""

    @property
    def economia(self) -> tuple[int, int]:
        """(itens vindos do cache, itens resumidos agora)."""
        return len(self.ids_do_cache), len(self.ids_resumidos_agora)


class ConflitoClassificacao(ValueError):
    """Mesmo `item_id` recebido com rótulo diferente do registro imutável."""


class Analyser:
    """Orquestra as seis etapas sobre uma rodada."""

    def __init__(self, config: Config, modelo: Modelo) -> None:
        self.config = config
        self.modelo = modelo
        self.armazenamento = Armazenamento(config)
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
        for registro in self.armazenamento.ler_resumos().values():
            if registro.ator_id != ator_id or registro.janela != janela:
                continue
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
        for lote in lotes:
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

        avisos = self._normalizar_contagens(final.dados)
        avisos += self._etapa_6_verificar(final.dados, resumos)
        for aviso in avisos:
            resultado.avisos.append(f"[{janela}/{eixo}] {aviso}")

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
        for fato in panorama.fatos_mais_citados:
            citados.update(fato.fontes)
        for resposta in panorama.respostas_registradas:
            citados.update(resposta.fontes)

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

    def _gravar_auditoria(self, resultado: ResultadoExecucao) -> None:
        """Grava o registro de auditoria da execução."""
        resultado.tokens = dict(self._tokens)
        resultado.hashes_prompts = self.prompts.hashes
        dados: dict[str, Any] = {
            "timestamp": agora_iso(),
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
