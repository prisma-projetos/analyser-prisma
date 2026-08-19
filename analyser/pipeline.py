"""O pipeline do Analyser, em seis etapas.

A ideia que organiza tudo: o resumo por notícia é caro e é pago uma vez só; o panorama é
barato e é recalculado do zero sempre que algo na janela muda. Reclassificar uma notícia
recalcula o panorama e NÃO re-resume a notícia.
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
    itens_reclassificados: list[str] = field(default_factory=list)
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
        """Resume o que é inédito e marca as janelas sujas.

        Uma janela fica suja quando é tocada por um item novo OU por um item cujo rótulo
        mudou. Só essas serão recalculadas.
        """
        ja_resumidos = self.armazenamento.ler_resumos()
        sujas: set[str] = set()

        for item in rodada.itens:
            # O rótulo é registrado à parte, na única camada mutável do sistema.
            mudou = self.armazenamento.registrar_rotulo(item.id, item.rotulo, resultado.rodada)
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

            if mudou:
                resultado.itens_reclassificados.append(item.id)

            if inedito or mudou:
                sujas.add(item.janela)
                motivo = "item novo" if inedito else "rótulo alterado"
                resultado.motivo_janela.setdefault(item.janela, [])
                marca = f"{item.id} ({motivo})"
                if marca not in resultado.motivo_janela[item.janela]:
                    resultado.motivo_janela[item.janela].append(marca)

        return sujas

    def _resumir(self, ator: AtorPolitico, item: Any) -> None:
        """Etapa 1 (map): uma chamada por notícia inédita, gravada uma única vez.

        A chamada não recebe o rótulo do classificador. É isso que faz o resumo continuar
        válido quando o item é reclassificado, e é isso que o mantém imparcial: no
        sistema ele será exibido dentro do agrupamento em que a matéria caiu, e não pode
        assumir o tom desse agrupamento.
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
        """Agrupa por rótulo VIGENTE todos os resumos daquela janela. Python puro.

        Note que a base é o conjunto completo de resumos da janela, não apenas os itens
        da rodada: o panorama é recalculado do zero, sempre.
        """
        rotulos = self.armazenamento.ler_rotulos()
        grupos: dict[Eixo, list[RegistroResumo]] = defaultdict(list)
        for registro in self.armazenamento.ler_resumos().values():
            if registro.ator_id != ator_id or registro.janela != janela:
                continue
            vigente = rotulos.get(registro.item_id)
            if vigente is None:
                continue
            grupos[vigente.rotulo_atual].append(registro)
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

        avisos = self._etapa_6_verificar(final.dados, resumos)
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
        rotulos = self.armazenamento.ler_rotulos()
        for janela in resultado.janelas_sujas:
            por_eixo: Counter[str] = Counter()
            for registro in self.armazenamento.ler_resumos().values():
                if registro.ator_id != rodada.ator.id or registro.janela != janela:
                    continue
                vigente = rotulos.get(registro.item_id)
                if vigente is None:
                    continue
                por_eixo[vigente.rotulo_atual] += 1
                resultado.contagem_veiculo.setdefault(registro.veiculo, Counter())
                resultado.contagem_veiculo[registro.veiculo][vigente.rotulo_atual] += 1
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
            "itens_reclassificados": resultado.itens_reclassificados,
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
