"""Persistência em JSON. Os arquivos de `saida/` fazem o papel do banco, de propósito.

Duas camadas, e a separação entre elas organiza o módulo:

* `resumos_item.json` — imutável, append-only. É aqui que mora o custo, pago uma vez por
  notícia. O registro também guarda a classificação definitiva recebida do Classifier.
  Não existe função de atualizar nem de apagar: a regra é imposta pelo código.
* `panoramas.json` — recalculável. Cada recálculo acrescenta uma versão nova; as
  anteriores nunca são sobrescritas nem apagadas.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from analyser.config import Config, Eixo
from analyser.schemas import RegistroPanorama, RegistroResumo


class DestinoArmazenamento(Protocol):
    """A porta que o pipeline usa para persistir. JSON hoje, PostgreSQL depois.

    O pipeline nunca fala com um arquivo nem com uma conexão: fala com estes nove métodos.
    Isolá-los é o que permite trocar o destino sem tocar nas seis etapas — e é o que mantém
    a suíte inteira rodando sobre JSON, sem banco, mesmo depois de a persistência real
    existir.

    O projeto já usa esse desenho em `DestinoInbox` (`analyser/rabbitmq.py`), pelo mesmo
    motivo: o consumidor recebe a porta por parâmetro e os testes injetam um dublê.
    """

    def ler_resumos(self) -> dict[str, RegistroResumo]: ...

    def ler_resumos_da_janela(
        self, *, ator_id: str, janela: str
    ) -> list[RegistroResumo]: ...

    def gravar_resumo(self, registro: RegistroResumo) -> None: ...

    def ler_panoramas(self) -> list[RegistroPanorama]: ...

    def proxima_versao(self, *, ator_id: str, janela: str, eixo: Eixo) -> int: ...

    def gravar_panorama(self, registro: RegistroPanorama) -> None: ...

    def panoramas_mais_recentes(
        self, *, ator_id: str, janela: str
    ) -> dict[Eixo, RegistroPanorama]: ...

    def proximo_numero_rodada(self) -> int: ...

    def gravar_auditoria(self, carimbo: str, dados: dict[str, Any]) -> Path: ...


class ViolacaoAppendOnly(RuntimeError):
    """Levantada quando algo tenta reescrever uma entrada já gravada.

    Não é um detalhe defensivo: é a regra 2 se defendendo sozinha.
    """


class SaidaLegadaIncompativel(RuntimeError):
    """A saída anterior não possui a classificação definitiva no resumo."""


def agora_iso() -> str:
    """Instante atual em ISO-8601 UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def carimbo_execucao() -> str:
    """Carimbo usado no nome do arquivo de auditoria.

    Com resolução de milissegundos: duas execuções seguidas cabem no mesmo segundo, e um
    carimbo de segundo faria a segunda sobrescrever o registro da primeira — justamente o
    que a auditoria não pode deixar acontecer.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")[:-3]


def _ler_json(caminho: Path, padrao: Any) -> Any:
    if not caminho.is_file():
        return padrao
    conteudo = caminho.read_text(encoding="utf-8").strip()
    if not conteudo:
        return padrao
    return json.loads(conteudo)


def _escrever_json(caminho: Path, dados: Any) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


class Armazenamento:
    """Acesso aos arquivos JSON de uma execução."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.config.saida.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------------------------
    # Camada imutável — resumos por item
    # ----------------------------------------------------------------------------------

    def ler_resumos(self) -> dict[str, RegistroResumo]:
        """Todos os resumos já gravados, indexados pelo id do item."""
        cru: dict[str, Any] = _ler_json(self.config.arquivo_resumos, {})
        legados = [
            item_id
            for item_id, registro in cru.items()
            if isinstance(registro, dict) and "rotulo" not in registro
        ]
        if legados:
            amostra = ", ".join(sorted(legados)[:5])
            raise SaidaLegadaIncompativel(
                f"{self.config.arquivo_resumos} usa o formato anterior, sem o campo "
                f"'rotulo' ({len(legados)} registro(s); exemplo: {amostra}). Migre ou "
                "regenere essa saída antes de usá-la com a classificação imutável."
            )
        return {item_id: RegistroResumo.model_validate(v) for item_id, v in cru.items()}

    def ler_resumos_da_janela(
        self, *, ator_id: str, janela: str
    ) -> list[RegistroResumo]:
        """Só os resumos de um `(ator, janela)`, que é o recorte do panorama.

        Em JSON o filtro é em memória e não economiza nada — o arquivo é lido inteiro de
        qualquer jeito. O método existe pela outra ponta da porta: contra PostgreSQL isto
        vira um `WHERE`, enquanto `ler_resumos()` traria a tabela toda para dentro do
        processo a cada recálculo de janela.
        """
        return [
            registro
            for registro in self.ler_resumos().values()
            if registro.ator_id == ator_id and registro.janela == janela
        ]

    def gravar_resumo(self, registro: RegistroResumo) -> None:
        """Grava UM resumo. Recusa-se a sobrescrever um id já existente.

        Não existe contrapartida `atualizar_resumo` nem `apagar_resumo`, e isso é
        deliberado: nenhum caminho de código consegue alterar um resumo já pago.
        """
        cru: dict[str, Any] = _ler_json(self.config.arquivo_resumos, {})
        if registro.item_id in cru:
            raise ViolacaoAppendOnly(
                f"O resumo do item {registro.item_id} já existe e não pode ser reescrito. "
                "A camada de resumos é append-only."
            )
        cru[registro.item_id] = registro.model_dump(mode="json")
        _escrever_json(self.config.arquivo_resumos, cru)

    # ----------------------------------------------------------------------------------
    # Camada recalculável — panoramas versionados
    # ----------------------------------------------------------------------------------

    def ler_panoramas(self) -> list[RegistroPanorama]:
        """Todas as versões de todos os panoramas, em ordem de gravação."""
        cru: list[Any] = _ler_json(self.config.arquivo_panoramas, [])
        legados = [
            registro
            for registro in cru
            if isinstance(registro, dict)
            and isinstance(registro.get("panorama"), dict)
            and "respostas_registradas" in registro["panorama"]
        ]
        if legados:
            raise SaidaLegadaIncompativel(
                f"{self.config.arquivo_panoramas} usa o formato anterior, com o campo "
                f"'respostas_registradas' ({len(legados)} registro(s)). O campo saiu do "
                "contrato porque media as manifestações de qualquer citado, "
                "não o contraditório do alvo. Regenere essa saída ou "
                "aponte --saida para um diretório novo."
            )
        return [RegistroPanorama.model_validate(v) for v in cru]

    def proxima_versao(self, *, ator_id: str, janela: str, eixo: Eixo) -> int:
        """Número da próxima versão de um `(ator, janela, eixo)`."""
        existentes = [
            r.versao
            for r in self.ler_panoramas()
            if r.ator_id == ator_id and r.janela == janela and r.eixo == eixo
        ]
        return max(existentes, default=0) + 1

    def gravar_panorama(self, registro: RegistroPanorama) -> None:
        """Acrescenta uma versão nova. Nunca sobrescreve nem apaga as anteriores."""
        cru: list[Any] = _ler_json(self.config.arquivo_panoramas, [])
        colisao = any(
            r["ator_id"] == registro.ator_id
            and r["janela"] == registro.janela
            and r["eixo"] == registro.eixo
            and r["versao"] == registro.versao
            for r in cru
        )
        if colisao:
            raise ViolacaoAppendOnly(
                f"Já existe panorama v{registro.versao} para "
                f"({registro.ator_id}, {registro.janela}, {registro.eixo})."
            )
        cru.append(registro.model_dump(mode="json"))
        _escrever_json(self.config.arquivo_panoramas, cru)

    def panoramas_mais_recentes(
        self, *, ator_id: str, janela: str
    ) -> dict[Eixo, RegistroPanorama]:
        """Última versão de cada eixo de uma janela."""
        recentes: dict[Eixo, RegistroPanorama] = {}
        for r in self.ler_panoramas():
            if r.ator_id != ator_id or r.janela != janela:
                continue
            atual = recentes.get(r.eixo)
            if atual is None or r.versao > atual.versao:
                recentes[r.eixo] = r
        return recentes

    # ----------------------------------------------------------------------------------
    # Auditoria
    # ----------------------------------------------------------------------------------

    def proximo_numero_rodada(self) -> int:
        """Próximo número sequencial de execução dentro deste diretório de saída.

        Auditorias anteriores à introdução de ``numero_rodada`` contam pela posição
        cronológica do arquivo. Isso permite continuar uma saída existente sem editar os
        registros históricos.

        O número é local ao diretório de saída; ele não pretende ser um contador global
        entre várias instâncias concorrentes do Analyser.
        """
        maior_numero = 0
        auditorias = sorted(self.config.saida.glob("auditoria_*.json"))
        for posicao, caminho in enumerate(auditorias, start=1):
            dados = _ler_json(caminho, {})
            numero = dados.get("numero_rodada") if isinstance(dados, dict) else None
            if isinstance(numero, int) and not isinstance(numero, bool) and numero >= 1:
                maior_numero = max(maior_numero, numero)
            else:
                maior_numero = max(maior_numero, posicao)
        return maior_numero + 1

    def gravar_auditoria(self, carimbo: str, dados: dict[str, Any]) -> Path:
        """Grava o registro de auditoria da execução e devolve o caminho."""
        caminho = self.config.arquivo_auditoria(carimbo)
        _escrever_json(caminho, dados)
        return caminho
