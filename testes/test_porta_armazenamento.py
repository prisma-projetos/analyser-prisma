"""A porta de persistência é substituível — é o que permite trocar JSON por PostgreSQL.

Sem este teste, `DestinoArmazenamento` seria só documentação: nada garantiria que o pipeline
realmente fala apenas com a porta, e a primeira chamada direta a um arquivo passaria
despercebida até a implementação PostgreSQL quebrar em produção.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import montar_config

from analyser.armazenamento import DestinoArmazenamento
from analyser.config import Eixo
from analyser.modelos import ModeloFake
from analyser.pipeline import Analyser
from analyser.schemas import RegistroPanorama, RegistroResumo


class ArmazenamentoEmMemoria:
    """Dublê que satisfaz a porta sem tocar em disco.

    Não herda de `DestinoArmazenamento`: `Protocol` casa por forma, não por herança. Se um
    método sumir ou mudar de assinatura aqui, o pipeline quebra — que é exatamente o alarme
    que se quer.
    """

    def __init__(self) -> None:
        self.resumos: dict[str, RegistroResumo] = {}
        self.panoramas: list[RegistroPanorama] = []
        self.auditorias: list[dict[str, Any]] = []

    def ler_resumos(self) -> dict[str, RegistroResumo]:
        return dict(self.resumos)

    def ler_resumos_da_janela(
        self, *, ator_id: str, janela: str
    ) -> list[RegistroResumo]:
        return [
            r
            for r in self.resumos.values()
            if r.ator_id == ator_id and r.janela == janela
        ]

    def gravar_resumo(self, registro: RegistroResumo) -> None:
        self.resumos[registro.item_id] = registro

    def ler_panoramas(self) -> list[RegistroPanorama]:
        return list(self.panoramas)

    def proxima_versao(self, *, ator_id: str, janela: str, eixo: Eixo) -> int:
        anteriores = [
            p.versao
            for p in self.panoramas
            if p.ator_id == ator_id and p.janela == janela and p.eixo == eixo
        ]
        return max(anteriores, default=0) + 1

    def gravar_panorama(self, registro: RegistroPanorama) -> None:
        self.panoramas.append(registro)

    def panoramas_mais_recentes(
        self, *, ator_id: str, janela: str
    ) -> dict[Eixo, RegistroPanorama]:
        recentes: dict[Eixo, RegistroPanorama] = {}
        for p in self.panoramas:
            if p.ator_id != ator_id or p.janela != janela:
                continue
            atual = recentes.get(p.eixo)
            if atual is None or p.versao > atual.versao:
                recentes[p.eixo] = p
        return recentes

    def proximo_numero_rodada(self) -> int:
        return len(self.auditorias) + 1

    def gravar_auditoria(self, carimbo: str, dados: dict[str, Any]) -> Path:
        self.auditorias.append(dados)
        return Path(f"memoria://{carimbo}")


def test_pipeline_roda_inteiro_sobre_uma_porta_injetada(tmp_path: Path) -> None:
    """O pipeline completo funciona sem escrever um único arquivo de resultado."""
    destino = ArmazenamentoEmMemoria()
    config = montar_config(tmp_path, "rodada_1.json")

    resultado = Analyser(config, ModeloFake(), armazenamento=destino).executar()

    # Passou pelas seis etapas usando só a porta.
    assert destino.resumos, "nenhum resumo chegou ao destino injetado"
    assert destino.panoramas, "nenhum panorama chegou ao destino injetado"
    assert len(destino.auditorias) == 1
    assert resultado.panoramas

    # E nada foi parar em disco: se o pipeline tivesse um caminho direto ao arquivo,
    # estes existiriam mesmo com a porta injetada.
    assert not (tmp_path / "resumos_item.json").exists()
    assert not (tmp_path / "panoramas.json").exists()


def test_agrupamento_usa_a_consulta_por_janela(tmp_path: Path) -> None:
    """A etapa 3 pede só a janela, não a base inteira.

    Contra PostgreSQL a diferença é um `WHERE` em vez de trazer a tabela toda para o
    processo a cada recálculo. O teste registra a chamada para a otimização não ser
    desfeita por descuido numa refatoração futura.
    """
    destino = ArmazenamentoEmMemoria()
    chamadas: list[tuple[str, str]] = []
    original = destino.ler_resumos_da_janela

    def espiao(*, ator_id: str, janela: str) -> list[RegistroResumo]:
        chamadas.append((ator_id, janela))
        return original(ator_id=ator_id, janela=janela)

    destino.ler_resumos_da_janela = espiao  # type: ignore[method-assign]
    config = montar_config(tmp_path, "rodada_1.json")

    Analyser(config, ModeloFake(), armazenamento=destino).executar()

    assert chamadas, "a etapa 3 não usou ler_resumos_da_janela"
    assert all(janela for _, janela in chamadas)
