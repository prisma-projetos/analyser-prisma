"""Nome do ator nos prompts do panorama, resumo sem URL e contexto dos parágrafos vizinhos.

Os três ajustes vieram do diagnóstico de 25/09/2026:

* sem o nome do ator, a consolidação não sabia de quem era o panorama — no Zema pôs o Cury
  como protagonista, no Girão tratou "Eduardo Girão" e "Luis Eduardo Grangeiro Girão" como
  duas pessoas, e no Lula não tinha como separar crítica AO ator de crítica feita POR ele;
* com a URL no prompt, o resumo tratava a manchete embutida nela como conteúdo ("ação da
  PM" no Missão, "Flávio Dino" no Girão);
* recebendo só os trechos do ator, o resumo do Missão falou de "a escola" sem dizer qual,
  embora o parágrafo anterior, gravado para o Lula e o PT, dissesse.

Nenhum destes testes usa banco: o enriquecimento roda contra um banco falso que responde
às mesmas consultas que o `Enriquecedor` faz.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from analyser.enriquecimento import Enriquecedor, contexto_vizinho
from analyser.prompts import Prompts, descrever_ator
from analyser.schemas import NotificacaoClassifier

RADICAIS_ROTULO = ("positiv", "negativ", "neutr")

# --------------------------------------------------------------------------------------
# Escolha dos parágrafos vizinhos
# --------------------------------------------------------------------------------------

PARAGRAFOS = {
    1: "um",
    3: "tres",
    4: "quatro",
    5: "cinco",
    6: "seis",
    9: "nove",
}


def test_contexto_pega_vizinhos_e_deixa_de_fora_os_do_proprio_ator() -> None:
    contexto = contexto_vizinho(PARAGRAFOS, centros={5}, excluir={5})

    assert contexto == ["tres", "quatro", "seis"]


def test_contexto_ignora_paragrafo_longe_demais() -> None:
    contexto = contexto_vizinho(PARAGRAFOS, centros={5}, excluir={5}, distancia=1)

    assert contexto == ["quatro", "seis"]


def test_trecho_do_outro_recorte_do_ator_nao_volta_como_contexto() -> None:
    """Uma matéria com os dois sinais vira dois recortes; um não vaza para o outro."""
    contexto = contexto_vizinho(PARAGRAFOS, centros={5}, excluir={4, 5})

    assert "quatro" not in contexto


def test_teto_mantem_os_mais_proximos_na_ordem_da_materia() -> None:
    contexto = contexto_vizinho(PARAGRAFOS, centros={5}, excluir={5}, maximo=2)

    assert contexto == ["quatro", "seis"]


def test_texto_repetido_entra_uma_vez_e_texto_do_ator_nao_entra() -> None:
    paragrafos = {4: "bloco lateral", 5: "trecho do ator", 6: "bloco lateral", 7: "trecho do ator"}

    contexto = contexto_vizinho(paragrafos, centros={5}, excluir={5})

    assert contexto == ["bloco lateral"]


def test_sem_trechos_nao_ha_contexto() -> None:
    assert contexto_vizinho(PARAGRAFOS, centros=set(), excluir=set()) == []


# --------------------------------------------------------------------------------------
# Como o ator aparece nos prompts
# --------------------------------------------------------------------------------------


def test_ator_aparece_com_os_outros_nomes() -> None:
    descricao = descrever_ator(
        "LUIS EDUARDO GRANGEIRO GIRÃO", "politico", ["LUIS EDUARDO GRANGEIRO GIRAO", "EDUARDO GIRÃO"]
    )

    # O próprio nome, repetido sem acento, não vira apelido.
    assert descricao == (
        "LUIS EDUARDO GRANGEIRO GIRÃO (politico); também citado como EDUARDO GIRÃO"
    )


def test_ator_sem_apelidos() -> None:
    assert descrever_ator("AVANTE", "partido") == "AVANTE (partido)"


# --------------------------------------------------------------------------------------
# Prompts renderizados
# --------------------------------------------------------------------------------------


def _resumo(prompts: Prompts, **extra: Any) -> str:
    return prompts.montar_resumo_item(
        ator_nome="PARTIDO MISSÃO",
        ator_tipo="partido",
        # Sem o sufixo do eixo que o `item_id` real carrega: ver o aviso sobre esse
        # vazamento no relatório desta mudança.
        item_id="art_3242__partido_14",
        veiculo="veronoticias.com",
        data="2026-09-01",
        titulo="Artigo 3242 — trechos classificados sobre PARTIDO MISSÃO",
        texto="A decisão ocorre em ação do partido Missão contra Lula, o PT e a agremiação.",
        **extra,
    ).texto


def test_resumo_nao_tem_url(prompts: Prompts) -> None:
    import inspect

    assert "url" not in inspect.signature(prompts.montar_resumo_item).parameters

    texto = _resumo(prompts)

    assert "url:" not in texto
    assert "{URL}" not in texto
    assert "http" not in texto


def test_resumo_leva_contexto_separado_dos_trechos(prompts: Prompts) -> None:
    escola = (
        "A ministra Estela Aranha, do TSE, determinou que o Instagram forneça dados "
        "cadastrais do presidente da Acadêmicos de Niterói."
    )
    texto = _resumo(prompts, contexto=[escola], ator_apelidos=["MISSÃO"])

    trechos, _, contexto = texto.partition("\ncontexto:\n")
    assert "partido Missão contra Lula" in trechos
    assert escola in contexto
    assert "alvo: PARTIDO MISSÃO (partido)" in texto
    assert "{CONTEXTO}" not in texto and "{ATOR}" not in texto


def test_resumo_sem_contexto_diz_que_nao_ha(prompts: Prompts) -> None:
    texto = _resumo(prompts)

    assert "contexto:\n(nenhum)" in texto


def test_resumo_com_contexto_continua_sem_rotulo(prompts: Prompts) -> None:
    """Regra 1 vale também para o que entrou agora no prompt do resumo."""
    texto = _resumo(prompts, contexto=["Parágrafo vizinho qualquer."], ator_apelidos=["MISSÃO"])

    base = texto.lower()
    assert not [r for r in RADICAIS_ROTULO if r in base]


def test_consolidacoes_recebem_o_ator(prompts: Prompts) -> None:
    ator = descrever_ator("LUIZ INÁCIO LULA DA SILVA", "politico", ["LULA"])

    lote = prompts.montar_consolidacao_lote(eixo="negativo", resumos="[]", ator=ator).texto
    final = prompts.montar_consolidacao_final(
        eixo="negativo",
        ator_id="122418060191",
        ator=ator,
        janela="2026-08",
        parciais="[]",
        materias=18,
    ).texto

    for texto in (lote, final):
        assert f"Ator analisado: {ator}" in texto
        assert "{ATOR}" not in texto
    # No panorama, a regra que separa crítica AO ator de crítica feita POR ele.
    assert "que o próprio ator fez a" in final
    assert "quem falou de" in final


# --------------------------------------------------------------------------------------
# Enriquecimento contra um banco falso
# --------------------------------------------------------------------------------------

HADDAD, LULA = "095327680116", "122418060191"
PT, MISSAO, PL = 13, 14, 22

#: A página do veronoticias do caso Missão: parágrafo -> (texto, entidades citadas).
MATERIA = {
    3: ("Fernando Haddad (PT) apresentou um laudo sobre a abordagem ao seu motorista.",
        [("politico", HADDAD), ("partido", PT)]),
    4: ("A ministra Estela Aranha, do TSE, determinou que o Instagram forneça dados "
        "cadastrais do presidente da Acadêmicos de Niterói, escola de samba que "
        "homenageou o presidente Lula (PT) no Carnaval deste ano.",
        [("politico", LULA), ("partido", PT)]),
    5: ("A decisão ocorre em ação do partido Missão contra Lula, o PT e a agremiação. "
        "Como a escola não foi localizada nos endereços disponíveis, a legenda pediu "
        "informações vinculadas ao perfil de Hugo Júnior.",
        [("politico", LULA), ("partido", PT), ("partido", MISSAO)]),
    6: ("O presidente Lula (PT) recebe nesta segunda-feira convidados para um jantar.",
        [("politico", LULA)]),
    9: ("A defesa de Jair Bolsonaro pediu ao STF autorização para visitas.",
        [("partido", PL)]),
}

CADASTRO = {
    ("partido", str(MISSAO)): ("PARTIDO MISSÃO", ["MISSÃO"]),
    ("politico", LULA): ("LUIZ INÁCIO LULA DA SILVA", ["LULA"]),
}


class _Resultado:
    def __init__(self, linhas: list[Any]) -> None:
        self._linhas = linhas

    def fetchall(self) -> list[Any]:
        return self._linhas

    def fetchone(self) -> Any:
        return self._linhas[0] if self._linhas else None


class BancoFalso:
    """Responde às consultas do `Enriquecedor` a partir de `MATERIA` e `CADASTRO`."""

    def __init__(self) -> None:
        # Linhas de `tabela_classificacao` na ordem de inserção do classificador:
        # parágrafo a parágrafo, uma linha por entidade citada.
        self.linhas: list[dict[str, Any]] = []
        for indice in sorted(MATERIA):
            texto, entidades = MATERIA[indice]
            for tipo, eid in entidades:
                self.linhas.append(
                    {
                        "id_classificacao": len(self.linhas) + 1,
                        "nr_titulo_eleitoral_candidato": eid if tipo == "politico" else None,
                        "nr_partido": eid if tipo == "partido" else None,
                        "paragrafo_analisado": texto,
                        "classificacao_sentimento": "neutro",
                        "score_confianca": 0.0,
                        "link_noticia": "https://veronoticias.com/politica/haddad-apresenta-laudo",
                        "data_publicacao_noticia": datetime(2026, 9, 1, tzinfo=timezone.utc),
                        "id_midia": 1,
                        "nome_midia": "veronoticias.com",
                    }
                )

    def cursor(self, row_factory: Any = None) -> "BancoFalso":
        return self

    def __enter__(self) -> "BancoFalso":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Resultado:
        if "FROM entidade_matching" in sql:
            chave = (params["tipo"], str(params["e"]))
            return _Resultado([CADASTRO[chave]] if chave in CADASTRO else [])
        if "c.nr_titulo_eleitoral_candidato = %(entidade)s" in sql:
            return _Resultado(
                [l for l in self.linhas if l["nr_titulo_eleitoral_candidato"] == params["entidade"]]
            )
        if "c.nr_partido = %(entidade)s" in sql:
            return _Resultado([l for l in self.linhas if l["nr_partido"] == params["entidade"]])
        return _Resultado(self.linhas)  # a matéria inteira


def _evento() -> NotificacaoClassifier:
    return NotificacaoClassifier.model_validate(
        {
            "schema_version": 1,
            "article_id": 3242,
            "job_id": 1,
            "pipeline_version": "v1",
            "link_noticia": "https://veronoticias.com/politica/haddad-apresenta-laudo",
            "data_publicacao_noticia": "2026-09-01T12:00:00Z",
            "notificado_em": "2026-09-01T12:05:00Z",
            "classificacoes": [
                {
                    "entity_type": tipo,
                    "entity_id": eid,
                    "paragraph_index": indice,
                    "classificacao_sentimento": "neutro",
                    "score_confianca": 0.0,
                }
                for indice, (_, entidades) in MATERIA.items()
                for tipo, eid in entidades
            ],
        }
    )


@pytest.fixture
def itens() -> dict[str, Any]:
    enriquecidos = Enriquecedor(BancoFalso()).enriquecer(_evento())  # type: ignore[arg-type]
    return {f"{i.entity_type}:{i.entity_id}": i for i in enriquecidos}


def test_resumo_do_missao_recebe_o_paragrafo_que_diz_qual_e_a_escola(itens) -> None:
    missao = itens[f"partido:{MISSAO}"]

    assert missao.texto == MATERIA[5][0]
    assert MATERIA[4][0] in missao.contexto  # gravado para o Lula e o PT, não para o Missão
    assert MATERIA[5][0] not in missao.contexto  # o próprio trecho não vira contexto
    assert MATERIA[9][0] not in missao.contexto  # longe demais
    assert missao.ator_nome == "PARTIDO MISSÃO"
    assert missao.ator_apelidos == ["MISSÃO"]


def test_contexto_de_cada_ator_sao_os_paragrafos_dos_outros(itens) -> None:
    lula = itens[f"politico:{LULA}"]

    # Os trechos do Lula são os parágrafos 4, 5 e 6; o vizinho de fora é o 3.
    assert lula.contexto == [MATERIA[3][0]]


def test_ator_fora_do_cadastro_fica_sem_apelidos(itens) -> None:
    assert itens[f"politico:{HADDAD}"].ator_apelidos == []


def test_hash_do_item_muda_com_o_contexto() -> None:
    """O contexto entra no prompt do resumo, então entra no hash do insumo."""
    com_contexto = Enriquecedor(BancoFalso()).enriquecer(_evento())  # type: ignore[arg-type]

    banco = BancoFalso()
    # Sem os outros atores, o Missão fica sem vizinhos: mesmo texto, sem contexto.
    banco.linhas = [l for l in banco.linhas if l["nr_partido"] == MISSAO]
    evento = _evento()
    evento.classificacoes = [c for c in evento.classificacoes if str(c.entity_id) == str(MISSAO)]
    sem_contexto = Enriquecedor(banco).enriquecer(evento)  # type: ignore[arg-type]

    missao_com = next(i for i in com_contexto if i.entity_id == str(MISSAO))
    (missao_sem,) = sem_contexto
    assert missao_sem.contexto == []
    assert missao_com.texto == missao_sem.texto
    assert missao_com.conteudo_hash != missao_sem.conteudo_hash
