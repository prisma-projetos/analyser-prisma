"""Carregamento de prompts a partir de `prompts/*.txt`, com hash SHA-256 registrado.

Nenhum prompt é hardcoded no código. O hash de cada arquivo efetivamente usado vai para o
registro de auditoria, de modo que uma execução antiga possa ser reproduzida sabendo-se
exatamente qual texto de prompt a produziu.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from analyser.config import FORMATO_POR_EIXO, ROTULO_NO_PROMPT, Eixo, faixa_de_temas


def _comparavel(nome: str) -> str:
    """Maiúsculas e sem acento, só para comparar nomes."""
    decomposto = unicodedata.normalize("NFD", nome.strip().upper())
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def descrever_ator(nome: str, tipo: str, apelidos: Sequence[str] = ()) -> str:
    """Como o alvo aparece nos prompts: nome, tipo e os outros nomes pelos quais é citado.

    O `nome` vem do TSE e raramente é o que a imprensa escreve. Os apelidos são o que
    permite ao modelo reconhecer "Eduardo Girão" como o mesmo "LUIS EDUARDO GRANGEIRO
    GIRÃO" — sem eles, o panorama tratou os dois como pessoas diferentes.
    """
    vistos = {_comparavel(nome)}
    outros: list[str] = []
    for apelido in apelidos:
        chave = _comparavel(apelido)
        if chave and chave not in vistos:
            vistos.add(chave)
            outros.append(apelido.strip())
    descricao = f"{nome} ({tipo})"
    if outros:
        descricao += f"; também citado como {', '.join(outros)}"
    return descricao


@dataclass(frozen=True, slots=True)
class PromptCarregado:
    """Texto de um prompt e o hash do arquivo de onde veio."""

    nome: str
    texto: str
    sha256: str


@dataclass(slots=True)
class Prompts:
    """Repositório de prompts de uma execução.

    Guarda os hashes de tudo que foi carregado, para a auditoria.
    """

    raiz: Path
    _cache: dict[str, PromptCarregado] = field(default_factory=dict)

    def carregar(self, nome: str) -> PromptCarregado:
        """Lê `<raiz>/<nome>` e devolve texto + hash, com cache por execução."""
        if nome in self._cache:
            return self._cache[nome]
        caminho = self.raiz / nome
        if not caminho.is_file():
            raise FileNotFoundError(f"Prompt não encontrado: {caminho}")
        texto = caminho.read_text(encoding="utf-8")
        carregado = PromptCarregado(
            nome=nome,
            texto=texto,
            sha256=hashlib.sha256(texto.encode("utf-8")).hexdigest(),
        )
        self._cache[nome] = carregado
        return carregado

    #: Todos os prompts do módulo. Pré-carregados para que a auditoria registre o hash de
    #: cada um mesmo numa execução que não teve nada a fazer — o que interessa auditar é o
    #: conjunto de prompts vigente, não só o que por acaso foi exercitado.
    ARQUIVOS: tuple[str, ...] = (
        "resumo_item.txt",
        "consolidacao_lote.txt",
        "consolidacao_final.txt",
        "formatos/negativo.txt",
        "formatos/positivo.txt",
        "formatos/neutro.txt",
    )

    def precarregar(self) -> None:
        """Carrega todos os prompts, para que os hashes fiquem completos na auditoria."""
        for nome in self.ARQUIVOS:
            self.carregar(nome)

    @property
    def hashes(self) -> dict[str, str]:
        """Hash de cada prompt desta execução, para o registro de auditoria."""
        return {nome: p.sha256 for nome, p in sorted(self._cache.items())}

    # ----------------------------------------------------------------------------------
    # Montagem — etapa 1 (map)
    # ----------------------------------------------------------------------------------

    def montar_resumo_item(
        self,
        *,
        ator_nome: str,
        ator_tipo: str,
        item_id: str,
        veiculo: str,
        data: str,
        titulo: str,
        texto: str,
        ator_apelidos: Sequence[str] = (),
        contexto: Sequence[str] = (),
    ) -> PromptCarregado:
        """Monta o prompt do resumo por item.

        Regra 1: o rótulo do classificador NÃO entra aqui. A função sequer recebe o
        rótulo como parâmetro — não há como vazá-lo por descuido. Isso mantém o resumo
        imparcial mesmo sendo exibido dentro de um agrupamento de classificação.

        A URL da matéria também não entra, desde 26/09/2026. O modelo tratava a manchete
        embutida nela como conteúdo: o resumo de um trecho sobre o partido Missão ganhou
        "para contestar a versão do governo Tarcísio sobre a ação da PM", tirado da URL de
        outra notícia, e um trecho só com "Romeu Zema (Novo) e Eduardo Girão (Novo)" virou
        "Flávio Dino" a partir de ".../registros-de-lula-flavio-e-zema".

        `contexto` são parágrafos vizinhos da mesma matéria que não citam o alvo — ver
        `analyser.enriquecimento.contexto_vizinho`. Entram separados dos trechos, e o
        prompt manda usá-los só para entender os trechos.
        """
        assert ROTULO_NO_PROMPT["map"] is False, "O map não pode receber o rótulo."
        base = self.carregar("resumo_item.txt")
        bloco_contexto = "\n\n".join(p for p in contexto if p.strip()) or "(nenhum)"
        rendido = (
            base.texto.replace("{ATOR}", descrever_ator(ator_nome, ator_tipo, ator_apelidos))
            .replace("{ITEM_ID}", item_id)
            .replace("{VEICULO}", veiculo)
            .replace("{DATA}", data)
            .replace("{TITULO}", titulo)
            .replace("{CONTEXTO}", bloco_contexto)
            .replace("{TEXTO}", texto)
        )
        return PromptCarregado(nome=base.nome, texto=rendido, sha256=base.sha256)

    # ----------------------------------------------------------------------------------
    # Montagem — etapa 4 (reduce)
    # ----------------------------------------------------------------------------------

    def montar_consolidacao_lote(
        self, *, eixo: Eixo, resumos: str, ator: str = "{ATOR}"
    ) -> PromptCarregado:
        """Monta o prompt do primeiro nível do reduce.

        `ator` é a descrição de `descrever_ator`. Sem ela o marcador fica à vista, como
        nos testes que só inspecionam as instruções.
        """
        base = self.carregar("consolidacao_lote.txt")
        rendido = (
            base.texto.replace("{EIXO}", eixo)
            .replace("{ATOR}", ator)
            .replace("{RESUMOS}", resumos)
        )
        return PromptCarregado(nome=base.nome, texto=rendido, sha256=base.sha256)

    def arquivo_formato(self, eixo: Eixo) -> str:
        """Caminho do fragmento de formato de um eixo.

        Cada eixo tem o seu fragmento, porque o parágrafo consolidado reúne coisas
        diferentes em cada recorte. O porquê da assimetria — e o que ela não afrouxou —
        está registrado em `FORMATO_POR_EIXO`.
        """
        return f"formatos/{FORMATO_POR_EIXO[eixo]}.txt"

    def instrucoes_finais(
        self,
        *,
        eixo: Eixo,
        ator_id: str = "{ATOR_ID}",
        janela: str = "{JANELA}",
        materias: int | None = None,
        ator: str = "{ATOR}",
    ) -> PromptCarregado:
        """Monta a parte de INSTRUÇÕES do prompt final: base única + fragmento de formato.

        Separada da montagem completa de propósito: é este texto — e não os dados das
        parciais — que o teste 2 inspeciona, porque "instruir tom" é uma propriedade das
        instruções. O eixo aparece aqui (`ROTULO_NO_PROMPT["reduce"] is True`), mas
        apenas para identificar o recorte.

        `ator` nomeia o alvo. Até 26/09/2026 só o número do título eleitoral chegava
        aqui: o modelo escrevia o panorama sem saber de quem era, e no Zema pôs o Cury
        como protagonista dos pontos positivos.
        """
        assert ROTULO_NO_PROMPT["reduce"] is True
        base = self.carregar("consolidacao_final.txt")
        fragmento = self.carregar(self.arquivo_formato(eixo))
        rendido = (
            base.texto.replace("{FORMATO}", fragmento.texto)
            .replace("{EIXO}", eixo)
            .replace("{ATOR_ID}", ator_id)
            .replace("{ATOR}", ator)
            .replace("{JANELA}", janela)
        )
        # A faixa de temas acompanha o volume de matérias: uma faixa fixa fragmenta um
        # eixo pequeno e mutila um grande. Sem o número, os marcadores ficam à vista —
        # é o que o teste 2 inspeciona, e ali não há rodada nenhuma para contar.
        if materias is not None:
            minimo, maximo = faixa_de_temas(materias)
            rendido = rendido.replace("{MIN_TEMAS}", str(minimo)).replace(
                "{MAX_TEMAS}", str(maximo)
            )
        return PromptCarregado(nome=base.nome, texto=rendido, sha256=base.sha256)

    def montar_consolidacao_final(
        self,
        *,
        eixo: Eixo,
        ator_id: str,
        janela: str,
        parciais: str,
        materias: int,
        ator: str,
    ) -> PromptCarregado:
        """Monta o prompt final completo: instruções + dados das parciais."""
        instrucoes = self.instrucoes_finais(
            eixo=eixo, ator_id=ator_id, janela=janela, materias=materias, ator=ator
        )
        rendido = instrucoes.texto.replace("{PARCIAIS}", parciais)
        return PromptCarregado(nome=instrucoes.nome, texto=rendido, sha256=instrucoes.sha256)

    def hash_formato(self, eixo: Eixo) -> str:
        """Hash do fragmento de formato usado por um eixo."""
        return self.carregar(self.arquivo_formato(eixo)).sha256
