"""Criterio de aceite do ranking: memoria revogada NAO pode subir (rodada 2, item 1).

O diagnostico da rodada 1 continua valendo - o acervo ordena essencialmente por
recencia e o assunto pesa pouco. Mas o remedio obvio (mais peso semantico, curva
de recencia achatada) tem efeito colateral medido: em 01/09/2026 a memoria
558075ce-918c-4122-b0c3-26dc873fdd10, de 29/07/2026 e com um contrato de API JA
REVOGADO, teve o MAIOR score semantico dos 20 resultados (0,878). Ela so nao
apareceu no topo porque a recencia dela era 0,769 contra 0,94-0,99 das demais.

Ou seja: naquele caso a recencia foi o mecanismo que segurou o dado errado.

Este modulo e o teste de regressao que qualquer mudanca de peso tem de passar,
alem do antes/depois de precisao: **o fato correto vence o revogado, sempre**.
Se um ajuste de formula reprovar aqui, ajuste a formula - nao o teste.
"""

import datetime

import pytest

from app.utils.recency import rank_search_results


def _iso(dias_atras):
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=dias_atras)).isoformat()


# Pares conhecidos (revogado, correto) tirados do acervo real. Em todos, o
# revogado e o MAIS ANTIGO e - de proposito - o de MAIOR score semantico, que e
# a configuracao adversarial: e assim que ele ganha se a recencia perder peso.
PARES = [
    pytest.param(
        {
            "id": "revogado-contrato-endpoint",
            "memory": (
                "O endpoint retorna cClassTrib, IBGE e as aliquotas efetivas ao Pricing"
            ),
            "score": 0.878,
            "updated_at": _iso(34),
            "project": "sysmo-api-tributacao",
        },
        {
            "id": "correto-contrato-endpoint",
            "memory": (
                "O MS persiste as aliquotas efetivas em tabela e NAO retorna a "
                "relacao cClassTrib x IBGE ao Pricing"
            ),
            "score": 0.812,
            "updated_at": _iso(13),
            "project": "sysmo-api-tributacao",
        },
        id="contrato-do-endpoint-de-aliquotas",
    ),
    pytest.param(
        {
            "id": "revogado-371145-planejamento",
            "memory": "TAREFA #371145 esta em planejamento, sem entrega definida",
            "score": 0.870,
            "updated_at": _iso(26),
            "project": "sysmo-s1",
        },
        {
            "id": "correto-371145-liberacao",
            "memory": "TAREFA #371145 esta em Liberacao, com 33 commits e teste aprovado",
            "score": 0.700,
            "updated_at": _iso(4),
            "project": "sysmo-api-tributacao",
        },
        id="estado-da-371145",
    ),
]


@pytest.mark.parametrize("revogado,correto", PARES)
@pytest.mark.parametrize(
    "query",
    [
        "371145 estado da entrega",          # com codigo de tarefa: fator lexical ativo
        "estado do contrato do endpoint",    # sem codigo: so o blend de sempre
    ],
)
def test_o_fato_correto_sempre_vence_o_revogado(revogado, correto, query):
    results = [dict(revogado), dict(correto)]
    rank_search_results(
        results,
        preferred_project="sysmo-api-tributacao",
        query=query,
        annotate=True,
    )
    assert results[0]["id"] == correto["id"], (
        "o revogado subiu: efetivos %s"
        % [(r["id"], round(r["effective_score"], 4)) for r in results]
    )


@pytest.mark.parametrize("revogado,correto", PARES)
def test_o_boost_lexical_nao_favorece_o_revogado(revogado, correto):
    """Ambos citam a tarefa, entao o fator lexical tem de ser igual nos dois.

    Se o boost por chave exata caisse so em um lado do par, ele viraria um jeito
    novo de promover dado velho - o oposto do que ele existe para fazer.
    """
    revogado = dict(revogado, memory="TAREFA #371145 " + revogado["memory"])
    correto = dict(correto, memory="TAREFA #371145 " + correto["memory"])
    results = [revogado, correto]
    rank_search_results(results, query="/tarefa 371145", annotate=True)
    fatores = {r["id"]: r["ranking_factors"]["lexical"] for r in results}
    assert len(set(fatores.values())) == 1, fatores
    assert results[0]["id"] == correto["id"]


def test_memoria_obsoleta_nunca_e_ranqueada_no_caminho_padrao():
    """Rede de seguranca: obsoleto e filtrado antes do ranking, nao depois.

    O ranking nao le ``state`` - quem esconde obsoleto e o filtro da busca
    (``include_obsolete=false``). Este teste documenta a fronteira: se algum dia
    um obsoleto chegar ate aqui, ele CONCORRE, e o filtro e que regrediu.
    """
    obsoleta = {
        "id": "obsoleta",
        "memory": "TAREFA #371145 contrato antigo",
        "score": 0.99,
        "updated_at": _iso(0),
        "state": "obsolete",
    }
    ativa = {
        "id": "ativa",
        "memory": "TAREFA #371145 contrato novo",
        "score": 0.60,
        "updated_at": _iso(0),
        "state": "active",
    }
    results = [obsoleta, ativa]
    rank_search_results(results, query="371145", annotate=True)
    assert results[0]["id"] == "obsoleta"
