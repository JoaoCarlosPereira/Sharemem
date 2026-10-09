"""Recency-weighted ordering for read paths (ADR-003 applied at read time).

Shared by ``app.mcp_server`` (MCP tools) and ``app.routers.compat_v3`` (the REST
shim the plugin hooks call) so both read paths order results the same way and
share a single calibration point (the env vars below).

Recency is measured from ``updated_at`` (the last time the fact changed) and falls
back to ``created_at``. A fact created long ago but updated recently is therefore
treated as recent, so a newer correction outranks the stale version it supersedes
— even when the older one is a closer semantic match.

Tunable via env:
  MEM0_SEARCH_RECENCY_HALFLIFE_DAYS — days for the recency weight to halve (time scale)
  MEM0_SEARCH_RECENCY_WEIGHT        — how strongly recency influences order
                                      (0.0 = pure semantic relevance, previous behavior)
  MEM0_SEARCH_PROJECT_BOOST_EXACT   — multiplicative boost when project names match exactly
  MEM0_SEARCH_PROJECT_BOOST_FUZZY   — smaller boost when normalized names overlap/substring-match
  MEM0_SEARCH_LEXICAL_BOOST         — boost when an exact task code from the query appears in the result
"""

from __future__ import annotations

import datetime
import os
import re
from typing import Optional

from app.utils.groups import group_of_hostname

# Meia-vida da recencia: 45 dias (era 90, alterado em 01/09/2026 COM medicao).
#
# Motivo: com o fator lexical abaixo assumindo a discriminacao de ASSUNTO, a
# recencia fica livre para fazer o que ela realmente sabe fazer - distinguir
# VERSAO do mesmo fato. E a 90 dias ela nao fazia nem isso: um par adversarial
# real (fato revogado de 26 dias com score 0,870 contra o fato correto de 4 dias
# com score 0,700) era vencido pelo REVOGADO, porque 22 dias de diferenca so
# valiam 1,18x contra os 1,24x da vantagem semantica.
#
# Medido nas mesmas 4 consultas reais da calibracao do fator lexical, precisao@5:
#
#   meia-vida   q1    q2    q3    q4     par adversarial
#     90 (era)  5/5   2/5   5/5   1/5    revogado vence
#     60        5/5   2/5   5/5   1/5    correto vence (margem 4%)
#     45  <-    5/5   2/5   5/5   1/5    correto vence (margem 13%)
#     30        5/5   2/5   4/5   1/5    correto vence
#     21        5/5   2/5   4/5   1/5    correto vence
#
# 45 e o ponto mais afiado que nao custa nada na precisao (o degrau esta em 30,
# onde q3 cai) e ainda deixa margem no criterio de aceite. Ver
# tests/test_ranking_regression_obsoletas.py: memoria revogada nao pode subir.
#
# ATENCAO ao mexer aqui: o remedio "obvio" para o ranking - mais peso semantico
# e curva de recencia ACHATADA - vai na direcao contraria e ja foi medido como
# nocivo. Em 01/09/2026 a memoria 558075ce, com um contrato de API revogado,
# tinha o MAIOR score semantico dos 20 resultados (0,878) e so nao subiu porque
# a recencia dela era 0,769. Achatar a recencia teria promovido dado errado.
SEARCH_RECENCY_HALFLIFE_DAYS = float(os.getenv("MEM0_SEARCH_RECENCY_HALFLIFE_DAYS", "45"))
SEARCH_RECENCY_WEIGHT = float(os.getenv("MEM0_SEARCH_RECENCY_WEIGHT", "1.0"))
SEARCH_PROJECT_BOOST_EXACT = float(os.getenv("MEM0_SEARCH_PROJECT_BOOST_EXACT", "0.1"))
SEARCH_PROJECT_BOOST_FUZZY = float(os.getenv("MEM0_SEARCH_PROJECT_BOOST_FUZZY", "0.05"))

# Multiplicador forte aplicado quando o autor da memória pertence ao mesmo grupo do
# solicitante (ADR-003). Forte por padrão: prioriza o contexto da própria equipe sem
# remover memórias de outros grupos. Calibrável por env, sem alteração de código.
SEARCH_GROUP_BOOST = float(os.getenv("MEM0_SEARCH_GROUP_BOOST", "2.5"))


# Boost lexical para chave exata de tarefa (medido em 01/09/2026).
#
# O embedding nao codifica numero de tarefa. Medido em 4 consultas reais contra
# 100 candidatos cada:
#
#   consulta                                 amplitude do score semantico
#   "371145"                                 0,511 - 0,577   (0,066)
#   "371145 aliquotas efetivas ... MR"       0,668 - 0,819   (0,151)
#   recencia (meia-vida 90d) no mesmo pool   0,606 - 1,000   (0,394)
#
# Ou seja: a recencia varre uma faixa 2,6x maior que o sinal semantico, e no caso
# do codigo puro, 6x. Reponderar semantico/recencia/projeto foi medido e nao
# resolve - amplificar um sinal que nao existe nao cria discriminacao (precisao@5
# em "371145 aliquotas...": 3/5 hoje, 2/5 com semantico^3, 2/5 com recencia
# achatada, 3/5 com projeto forte). O que resolve e casar a chave exata: com este
# fator a mesma consulta vai a 5/5, e as 6 memorias da tarefa presentes no pool
# ficam nas 6 primeiras posicoes.
#
# Por isso NAO mexemos em SEARCH_RECENCY_* nem em SEARCH_PROJECT_BOOST_*: os
# numeros nao sustentam a mudanca.
SEARCH_LEXICAL_BOOST = float(os.getenv("MEM0_SEARCH_LEXICAL_BOOST", "3.0"))

# Codigo de tarefa do Redmine: 6 digitos, com ou sem "#", sem digito colado.
_TASK_CODE_RE = re.compile(r"(?<!\d)#?(\d{6})(?!\d)")


def extract_task_codes(query) -> list:
    """Codigos de tarefa citados na consulta, sem repetir e em ordem estavel."""
    if not query:
        return []
    seen = []
    for m in _TASK_CODE_RE.finditer(str(query)):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def lexical_match_factor(result, codes) -> float:
    """Boost quando a chave exata da consulta aparece literalmente no resultado.

    Nunca penaliza: sem codigo na consulta, ou sem o codigo no resultado, o fator
    e 1.0 e a ordem continua sendo a do blend semantico/recencia/projeto/grupo.
    """
    if not codes:
        return 1.0
    haystack = str(result.get("memory") or "")
    project = result.get("project")
    if not project:
        meta = result.get("metadata")
        if isinstance(meta, dict):
            project = meta.get("project")
    haystack = haystack + " " + str(project or "")
    return SEARCH_LEXICAL_BOOST if any(c in haystack for c in codes) else 1.0


def parse_ts(value):
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def recency_factor(result, now):
    """Exponential decay in (0, 1] from a fact's last-change time.

    Uses ``updated_at`` and falls back to ``created_at``. Returns 1.0 (neutral, no
    penalty) when neither timestamp is parseable or recency is disabled, so undated
    facts rank by pure relevance instead of being unfairly buried.
    """
    if SEARCH_RECENCY_HALFLIFE_DAYS <= 0:
        return 1.0
    ts = parse_ts(result.get("updated_at")) or parse_ts(result.get("created_at"))
    if ts is None:
        return 1.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / SEARCH_RECENCY_HALFLIFE_DAYS)


def normalize_project_name(name) -> str:
    """Normalize project identifiers for fuzzy comparison (case/punctuation insensitive)."""
    if not name:
        return ""
    return "".join(c for c in str(name).lower() if c.isalnum())


def project_match_factor(result_project, preferred_project) -> float:
    """Small boost for matching project names; never penalizes mismatches.

    Sibling repositories configured as one family (MEM0_PROJECT_GROUPS) earn the
    same boost as an exact match: a task that spans the app, the DLL and the
    database repo is one subject, and the hint should not favour whichever
    directory the agent happened to be sitting in.
    """
    if not preferred_project:
        return 1.0
    result_norm = normalize_project_name(result_project)
    preferred_norm = normalize_project_name(preferred_project)
    if not preferred_norm:
        return 1.0
    if result_norm == preferred_norm:
        return 1.0 + SEARCH_PROJECT_BOOST_EXACT

    # Imported lazily: project_groups builds on normalize_project_name from here.
    from app.utils.project_groups import same_project_family

    if same_project_family(result_project, preferred_project):
        return 1.0 + SEARCH_PROJECT_BOOST_EXACT
    if result_norm and (result_norm in preferred_norm or preferred_norm in result_norm):
        return 1.0 + SEARCH_PROJECT_BOOST_FUZZY
    return 1.0


def group_match_factor(author_group, requester_group) -> float:
    """Boost forte quando autor e solicitante são do mesmo grupo; neutro caso contrário.

    Retorna :data:`SEARCH_GROUP_BOOST` somente quando ambos os grupos estão presentes e
    são iguais. Resultados sem grupo de autor resolvível (ex.: memórias legadas sem
    ``hostname`` no payload) ou solicitante sem grupo recebem ``1.0`` — nunca penalizados.
    """
    if not requester_group or not author_group:
        return 1.0
    return SEARCH_GROUP_BOOST if author_group == requester_group else 1.0


def rank_search_results(
    results, preferred_project=None, requester_group=None, annotate=False, query=None
):
    """Order results by semantic score blended with recency, project, group and exact-key boost.

    Relevance and recency dominate ordering; ``preferred_project`` applies a small
    multiplicative boost when names match. When ``requester_group`` is provided, results
    whose author belongs to the same group receive a strong multiplicative boost
    (ADR-003). The author group is resolved dynamically from each result's ``owner``
    (the author hostname); reads remain global — group only affects ordering. Missing
    project/owner/group never penalizes a result.

    With ``annotate=True`` each result also carries the numbers behind its position
    (``effective_score`` plus the individual factors). Without it the response shows
    only the raw semantic ``score`` while the order comes from the blended value —
    which makes an ordering impossible to explain or calibrate from the outside.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    # Chave exata da consulta (codigo de tarefa). Extraida uma vez, nao por item.
    task_codes = extract_task_codes(query)

    def _project_name(result) -> Optional[str]:
        name = result.get("project")
        if name:
            return str(name)
        meta = result.get("metadata")
        if isinstance(meta, dict) and meta.get("project"):
            return str(meta["project"])
        return None

    def _author_group(result) -> Optional[str]:
        if not requester_group:
            return None  # sem solicitante com grupo, evita lookups desnecessários
        return group_of_hostname(result.get("owner"))

    def key(r):
        score = r.get("score")
        score = score if isinstance(score, (int, float)) else 0.0
        recency = recency_factor(r, now) ** SEARCH_RECENCY_WEIGHT
        project = project_match_factor(_project_name(r), preferred_project)
        group = group_match_factor(_author_group(r), requester_group)
        lexical = lexical_match_factor(r, task_codes)
        effective = score * recency * project * group * lexical
        if annotate:
            r["effective_score"] = effective
            r["ranking_factors"] = {
                "recency": recency,
                "project": project,
                "group": group,
                "lexical": lexical,
            }
        return effective

    results.sort(key=key, reverse=True)
    return results


def recency_weighted_sort(results):
    """Order results in place by semantic score blended with recency.

    Backward-compatible alias for ``rank_search_results`` without project/group boost.
    """
    return rank_search_results(results)
