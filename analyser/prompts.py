"""Carregamento de prompts a partir de `prompts/*.txt`, com hash SHA-256 registrado.

Nenhum prompt é hardcoded no código. O hash de cada arquivo efetivamente usado vai para o
registro de auditoria, de modo que uma execução antiga possa ser reproduzida sabendo-se
exatamente qual texto de prompt a produziu.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from analyser.config import FORMATO_POR_EIXO, ROTULO_NO_PROMPT, Eixo, faixa_de_temas


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
        url: str,
        titulo: str,
        texto: str,
    ) -> PromptCarregado:
        """Monta o prompt do resumo por item.

        Regra 1: o rótulo do classificador NÃO entra aqui. A função sequer recebe o
        rótulo como parâmetro — não há como vazá-lo por descuido. Isso mantém o resumo
        imparcial mesmo sendo exibido dentro de um agrupamento de classificação.
        """
        assert ROTULO_NO_PROMPT["map"] is False, "O map não pode receber o rótulo."
        base = self.carregar("resumo_item.txt")
        rendido = (
            base.texto.replace("{ATOR_NOME}", ator_nome)
            .replace("{ATOR_TIPO}", ator_tipo)
            .replace("{ITEM_ID}", item_id)
            .replace("{VEICULO}", veiculo)
            .replace("{DATA}", data)
            .replace("{URL}", url)
            .replace("{TITULO}", titulo)
            .replace("{TEXTO}", texto)
        )
        return PromptCarregado(nome=base.nome, texto=rendido, sha256=base.sha256)

    # ----------------------------------------------------------------------------------
    # Montagem — etapa 4 (reduce)
    # ----------------------------------------------------------------------------------

    def montar_consolidacao_lote(self, *, eixo: Eixo, resumos: str) -> PromptCarregado:
        """Monta o prompt do primeiro nível do reduce."""
        base = self.carregar("consolidacao_lote.txt")
        rendido = base.texto.replace("{EIXO}", eixo).replace("{RESUMOS}", resumos)
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
    ) -> PromptCarregado:
        """Monta a parte de INSTRUÇÕES do prompt final: base única + fragmento de formato.

        Separada da montagem completa de propósito: é este texto — e não os dados das
        parciais — que o teste 2 inspeciona, porque "instruir tom" é uma propriedade das
        instruções. O eixo aparece aqui (`ROTULO_NO_PROMPT["reduce"] is True`), mas
        apenas para identificar o recorte.
        """
        assert ROTULO_NO_PROMPT["reduce"] is True
        base = self.carregar("consolidacao_final.txt")
        fragmento = self.carregar(self.arquivo_formato(eixo))
        rendido = (
            base.texto.replace("{FORMATO}", fragmento.texto)
            .replace("{EIXO}", eixo)
            .replace("{ATOR_ID}", ator_id)
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
        self, *, eixo: Eixo, ator_id: str, janela: str, parciais: str, materias: int
    ) -> PromptCarregado:
        """Monta o prompt final completo: instruções + dados das parciais."""
        instrucoes = self.instrucoes_finais(
            eixo=eixo, ator_id=ator_id, janela=janela, materias=materias
        )
        rendido = instrucoes.texto.replace("{PARCIAIS}", parciais)
        return PromptCarregado(nome=instrucoes.nome, texto=rendido, sha256=instrucoes.sha256)

    def hash_formato(self, eixo: Eixo) -> str:
        """Hash do fragmento de formato usado por um eixo."""
        return self.carregar(self.arquivo_formato(eixo)).sha256
