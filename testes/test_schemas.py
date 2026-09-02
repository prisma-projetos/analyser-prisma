"""Teste 6 — saída estruturada obrigatória.

Regra 7: nada que venha do modelo é usado antes de passar pelo Pydantic. Texto solto não
passa; falha de validação dá direito a uma tentativa, depois vira erro registrado.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from conftest import montar_config
from pydantic import ValidationError

from analyser.modelos import (
    ErroDeValidacao,
    ModeloFake,
    RespostaBruta,
    extrair_json,
    gerar_validado,
)
from analyser.pipeline import Analyser
from analyser.schemas import Panorama, RegistroResumo, ResumoItem


class ModeloDesobediente:
    """Modelo que responde qualquer coisa menos o JSON pedido."""

    def __init__(self, respostas: list[str]) -> None:
        self.nome = "desobediente"
        self.chamadas = 0
        self.chamadas_por_tarefa: Counter[str] = Counter()
        self._respostas = respostas
        self.prompts_recebidos: list[str] = []
        self.esquemas_recebidos: list[dict[str, Any] | None] = []

    def gerar(
        self,
        *,
        prompt: str,
        contexto: dict[str, Any],
        tarefa: str,
        esquema: dict[str, Any] | None = None,
    ) -> RespostaBruta:
        self.chamadas += 1
        self.chamadas_por_tarefa[tarefa] += 1
        self.prompts_recebidos.append(prompt)
        self.esquemas_recebidos.append(esquema)
        indice = min(self.chamadas - 1, len(self._respostas) - 1)
        return RespostaBruta(texto=self._respostas[indice])


def test_texto_solto_nao_passa() -> None:
    """Uma resposta em prosa é rejeitada, mesmo parecendo um resumo perfeito."""
    modelo = ModeloDesobediente(["Claro! A matéria trata da reforma do mercado municipal."])

    with pytest.raises(ErroDeValidacao):
        gerar_validado(
            modelo, prompt="p", schema=ResumoItem, tarefa="resumo_item", contexto={}
        )


def test_json_com_campo_faltando_nao_passa() -> None:
    """JSON bem formado mas incompleto também é rejeitado."""
    modelo = ModeloDesobediente(['{"resumo": "duas frases aqui."}'])

    with pytest.raises(ErroDeValidacao):
        gerar_validado(
            modelo, prompt="p", schema=ResumoItem, tarefa="resumo_item", contexto={}
        )


def test_uma_tentativa_de_reparo_e_so() -> None:
    """Exatamente duas chamadas: a original e um retry. Depois, erro."""
    modelo = ModeloDesobediente(["nada de json"])

    with pytest.raises(ErroDeValidacao):
        gerar_validado(
            modelo, prompt="p", schema=ResumoItem, tarefa="resumo_item", contexto={}
        )

    assert modelo.chamadas == 2
    assert modelo.prompts_recebidos[0] != modelo.prompts_recebidos[1]
    assert "apenas com o objeto JSON" in modelo.prompts_recebidos[1]


def test_retry_bem_sucedido_e_aceito() -> None:
    """Se a segunda tentativa vem correta, ela vale — e o número de tentativas é anotado."""
    modelo = ModeloDesobediente(
        [
            "desculpe, aqui vai:",
            '{"resumo": "Duas frases. Bem curtas.", "temas": ["obra"], "fonte_id": "not_001"}',
        ]
    )

    resultado = gerar_validado(
        modelo, prompt="p", schema=ResumoItem, tarefa="resumo_item", contexto={}
    )

    assert resultado.tentativas == 2
    assert resultado.dados.fonte_id == "not_001"


def test_cerca_de_codigo_e_tolerada() -> None:
    """Cerca de markdown não é conteúdo malformado — é enfeite, e é descascada."""
    bruto = '```json\n{"resumo": "Ok. Certo.", "temas": ["a"], "fonte_id": "not_1"}\n```'
    assert extrair_json(bruto)["fonte_id"] == "not_1"


def test_schema_unico_serve_aos_tres_eixos() -> None:
    """Os três eixos preenchem exatamente os mesmos campos.

    É o que permite ao frontend ter um contrato só. Desde que o panorama passou a
    entregar um parágrafo consolidado, nenhum campo é exclusivo de um formato: o que
    muda entre os eixos é o que o fragmento manda reunir no `texto`, não a forma.
    """
    tema = {"tema": "obra parada", "ocorrencias": 1, "fontes": ["not_009"]}

    neutro = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="neutro",
        total_itens=2,
        temas=[tema],
        fontes=["not_009"],
        texto="A cobertura registra a audiência pública (not_009).",
    )

    negativo = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="negativo",
        total_itens=2,
        temas=[tema],
        fontes=["not_001"],
        texto="As matérias apontam a obra parada há cinco meses (not_001).",
    )

    # Nenhum nome de campo sinaliza polaridade: os dois eixos usam o mesmo contrato.
    assert set(neutro.model_dump()) == set(negativo.model_dump())


def test_panorama_sem_texto_e_rejeitado() -> None:
    """O parágrafo é o conteúdo do panorama, então não pode ser omissível.

    Mesmo motivo de `temas`: um campo com default é tratado como dispensável pela
    decodificação restrita, e aqui o dispensável seria justamente o que o cidadão lê.
    """
    tema = {"tema": "obra parada", "ocorrencias": 1, "fontes": ["not_009"]}
    for texto in ({}, {"texto": ""}):
        with pytest.raises(ValidationError):
            Panorama(
                ator_id="ator_001",
                janela="2026-08",
                eixo="neutro",
                total_itens=1,
                temas=[tema],
                fontes=["not_009"],
                **texto,
            )


def test_panorama_sem_temas_e_rejeitado() -> None:
    """Nenhum formato dispensa temas, então o schema não pode dispensá-los.

    Enquanto o campo teve default, a decodificação restrita o tratou como omissível e o
    formato compacto devolveu panorama sem tema nenhum — sem que nada reclamasse.
    """
    with pytest.raises(ValidationError):
        Panorama(
            ator_id="ator_001",
            janela="2026-08",
            eixo="neutro",
            total_itens=1,
            fontes=["not_009"],
            texto="Duas frases.",
        )


def test_falha_de_validacao_vira_erro_registrado_sem_gravar_nada(tmp_path: Path) -> None:
    """No pipeline, o item que falha na validação é registrado como erro e não é gravado."""
    modelo = ModeloDesobediente(["isto não é json"])
    config = montar_config(tmp_path, "rodada_1.json")

    resultado = Analyser(config, modelo).executar()

    assert len(resultado.erros) == 10
    assert resultado.ids_resumidos_agora == []
    assert not (tmp_path / "resumos_item.json").exists()

    # O erro chegou à auditoria, em vez de sumir.
    auditoria = next(tmp_path.glob("auditoria_*.json"))
    assert '"erros"' in auditoria.read_text(encoding="utf-8")


def test_o_fake_sempre_produz_saida_valida() -> None:
    """O modelo padrão precisa passar no schema em todas as tarefas."""
    modelo = ModeloFake()

    resumo = gerar_validado(
        modelo,
        prompt="p",
        schema=ResumoItem,
        tarefa="resumo_item",
        contexto={"item_id": "not_001", "titulo": "Título", "texto": "Uma frase. Outra frase."},
    )
    assert resumo.dados.fonte_id == "not_001"
    assert modelo.chamadas == 1


def test_id_vazado_no_paragrafo_vira_aviso() -> None:
    """O parágrafo corre sem códigos; um id na prosa é o formato desobedecido.

    A rastreabilidade mora em `temas[].fontes` e em `fontes`. Se o modelo insiste em
    escrever "(art_408)" no meio do texto, o cidadão veria isso na tela — então a etapa 6
    registra o vazamento em vez de deixá-lo passar.
    """
    tema = {"tema": "obra parada", "ocorrencias": 1, "fontes": ["not_001"]}
    resumos = [
        RegistroResumo(
            item_id="not_001",
            ator_id="ator_001",
            janela="2026-08",
            rotulo="negativo",
            veiculo="Jornal",
            url="https://exemplo.invalido/a",
            data="2026-08-01",
            titulo="Título",
            resumo="Uma frase.",
            temas=["obra"],
            fonte_id="not_001",
            modelo="fake",
            hash_prompt_resumo="x",
            criado_em="2026-08-01T00:00:00+00:00",
        )
    ]

    limpo = Panorama(
        ator_id="ator_001", janela="2026-08", eixo="negativo", total_itens=1,
        temas=[tema], fontes=["not_001"],
        texto="As críticas registradas apontam a obra parada há cinco meses.",
    )
    assert Analyser._etapa_6_verificar(limpo, resumos) == []

    vazado = Panorama(
        ator_id="ator_001", janela="2026-08", eixo="negativo", total_itens=1,
        temas=[tema], fontes=["not_001"],
        texto="As críticas apontam a obra parada (not_001).",
    )
    avisos = Analyser._etapa_6_verificar(vazado, resumos)
    assert any("cita ids entre parênteses" in a and "not_001" in a for a in avisos)


def test_contagem_divergente_e_derivada_e_registrada() -> None:
    """A contagem gravada vem da lista, e a divergência não desaparece.

    `ocorrencias` e `total_itens` são o tamanho de listas que o modelo escreveu na mesma
    resposta — não afirmações sobre a cobertura. São derivados em vez de perguntados,
    mas toda substituição vira aviso, para que a auditoria continue mostrando que o
    modelo discordou. Não é conserto silencioso: é conserto declarado.
    """
    panorama = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="negativo",
        total_itens=99,
        temas=[{"tema": "reforma da rodoviária", "ocorrencias": 3,
                "fontes": ["not_005", "not_006"]}],
        fontes=["not_005", "not_006"],
        texto="As matérias registram a suspensão da licitação (not_005, not_006).",
    )

    avisos = Analyser._normalizar_contagens(panorama)

    assert panorama.temas[0].ocorrencias == 2
    assert panorama.total_itens == 2
    assert len(avisos) == 2
    assert "gravado 2" in avisos[0]
    assert "reforma da rodoviária" in avisos[0]


def test_contagem_correta_nao_gera_aviso() -> None:
    """Quando o modelo acerta, nada é registrado — o aviso só existe para divergência."""
    panorama = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="neutro",
        total_itens=2,
        temas=[{"tema": "audiência pública", "ocorrencias": 2,
                "fontes": ["not_009", "not_010"]}],
        fontes=["not_009", "not_010"],
        texto="Duas frases.",
    )

    assert Analyser._normalizar_contagens(panorama) == []


def test_faixa_de_temas_acompanha_o_volume() -> None:
    """A faixa cresce com o número de matérias, entre limites de legibilidade.

    Uma faixa fixa não serve a um eixo que tem 2 matérias num mês e 60 em outro: no
    primeiro caso ela fragmenta, no segundo mutila — e quando faltam vagas o modelo funde
    assuntos distintos num rótulo composto em vez de descartar.
    """
    from analyser.config import faixa_de_temas

    for materias in (1, 2, 5, 10, 20, 60, 100):
        minimo, maximo = faixa_de_temas(materias)
        assert 2 <= minimo, "nenhum panorama útil tem menos de dois temas"
        assert maximo <= 12, "um panorama é lido por um cidadão, não é um índice"
        assert maximo >= minimo + 2, "a faixa precisa dar folga de escolha ao modelo"

    # Cresce: um eixo grande admite mais temas que um pequeno.
    assert faixa_de_temas(60)[1] > faixa_de_temas(2)[1]
    assert faixa_de_temas(60)[0] > faixa_de_temas(2)[0]


def test_id_do_tema_e_derivado_do_nome_e_nao_do_modelo() -> None:
    """O id do tema é preenchido em Python, legível e estável para o mesmo nome.

    Pedir identificador a um gerador de texto livre devolveria valor diferente a cada
    execução — um id que muda sozinho não identifica nada. Derivando do nome, o mesmo
    assunto mantém o mesmo id entre versões do panorama e entre janelas.
    """
    panorama = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="positivo",
        total_itens=1,
        temas=[
            {"tema": "campanha de reeleição de Lula", "ocorrencias": 1, "fontes": ["not_001"]},
            {"tema": "pesquisa eleitoral da BTG/Nexus", "ocorrencias": 1, "fontes": ["not_001"]},
        ],
        fontes=["not_001"],
        texto="Os elogios registrados pela cobertura concentram-se na campanha.",
    )

    # O modelo não devolve o campo: ele nasce vazio e é preenchido depois.
    assert [t.id for t in panorama.temas] == ["", ""]

    Analyser._atribuir_ids_de_tema(panorama)

    assert [t.id for t in panorama.temas] == [
        "campanha-de-reeleicao-de-lula",
        "pesquisa-eleitoral-da-btg-nexus",
    ]


def test_temas_com_mesmo_slug_nao_repetem_id() -> None:
    """Dois nomes diferentes podem cair no mesmo slug; o id continua único."""
    tema = lambda nome: {"tema": nome, "ocorrencias": 1, "fontes": ["not_001"]}
    panorama = Panorama(
        ator_id="ator_001",
        janela="2026-08",
        eixo="neutro",
        total_itens=1,
        temas=[tema("Ciro Gomes"), tema("ciro gomes"), tema("CIRO GOMES")],
        fontes=["not_001"],
        texto="A cobertura registra menções ao candidato.",
    )

    Analyser._atribuir_ids_de_tema(panorama)

    ids = [t.id for t in panorama.temas]
    assert ids == ["ciro-gomes", "ciro-gomes-2", "ciro-gomes-3"]
    assert len(set(ids)) == 3
