"""Ranking por chave exata de tarefa (problema 2, medido em 01/09/2026).

Sem ``lexical_match_factor`` em ``rank_search_results`` estes testes falham: o
score semantico de um codigo de tarefa varia so 0,066 em 100 candidatos, entao a
ordenacao fica por recencia e a memoria da tarefa certa nao sobe.
"""

import datetime

from app.utils.recency import (
    SEARCH_LEXICAL_BOOST,
    extract_task_codes,
    lexical_match_factor,
    rank_search_results,
)


def _iso(dias_atras):
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=dias_atras)).isoformat()


def test_extrai_codigo_com_e_sem_cerquilha():
    assert extract_task_codes("/tarefa 371145") == ["371145"]
    assert extract_task_codes("ver #371145 e 372244") == ["371145", "372244"]
    assert extract_task_codes("TAREFA #371145 e de novo 371145") == ["371145"]


def test_nao_confunde_versao_ou_numero_de_outro_tamanho():
    assert extract_task_codes("versao 2.89.24 na porta 9443") == []
    assert extract_task_codes("id 1234567") == []
    assert extract_task_codes(None) == []


def test_fator_lexical_neutro_sem_codigo_na_consulta():
    assert lexical_match_factor({"memory": "371145"}, []) == 1.0


def test_fator_lexical_le_o_projeto_alem_do_texto():
    assert lexical_match_factor({"memory": "x", "project": "371145"}, ["371145"]) == SEARCH_LEXICAL_BOOST
    assert lexical_match_factor({"memory": "x", "metadata": {"project": "371145"}}, ["371145"]) == SEARCH_LEXICAL_BOOST
    assert lexical_match_factor({"memory": "TAREFA #372244"}, ["371145"]) == 1.0


def test_memoria_da_tarefa_vence_a_mais_recente_de_outro_assunto():
    """O caso de 01/09/2026: recall da 371145 devolvia o planejamento errado."""
    results = [
        {
            "id": "ruido-recente",
            "memory": "Planejamento de outra frente, sem relacao",
            "score": 0.81,
            "updated_at": _iso(0),
            "project": "sysmo-s1",
        },
        {
            "id": "certa",
            "memory": "Em 28/08/2026 a TAREFA #371145 foi para Liberacao com 33 commits",
            "score": 0.68,
            "updated_at": _iso(4),
            "project": "sysmo-api-tributacao",
        },
    ]
    rank_search_results(
        results,
        preferred_project="sysmo-api-tributacao",
        query="/tarefa 371145",
        annotate=True,
    )
    assert [r["id"] for r in results] == ["certa", "ruido-recente"]
    assert results[0]["ranking_factors"]["lexical"] == SEARCH_LEXICAL_BOOST
    assert results[1]["ranking_factors"]["lexical"] == 1.0


def test_sem_codigo_a_ordem_anterior_e_preservada():
    """Consulta sem chave exata nao pode mudar de comportamento."""
    base = [
        {"id": "a", "memory": "assunto A", "score": 0.80, "updated_at": _iso(0)},
        {"id": "b", "memory": "assunto B", "score": 0.70, "updated_at": _iso(0)},
    ]
    sem_query = [dict(r) for r in base]
    com_query = [dict(r) for r in base]
    rank_search_results(sem_query)
    rank_search_results(com_query, query="assunto qualquer sem codigo")
    assert [r["id"] for r in sem_query] == [r["id"] for r in com_query]
