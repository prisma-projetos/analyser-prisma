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
from typing import Any

from analyser.config import Config, Eixo
from analyser.schemas import RegistroPanorama, RegistroResumo


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

    def gravar_auditoria(self, carimbo: str, dados: dict[str, Any]) -> Path:
        """Grava o registro de auditoria da execução e devolve o caminho."""
        caminho = self.config.arquivo_auditoria(carimbo)
        _escrever_json(caminho, dados)
        return caminho
