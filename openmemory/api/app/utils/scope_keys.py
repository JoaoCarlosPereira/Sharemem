"""Chaves de escopo do payload: tarefa e procedencia da gravacao.

Dois campos proprios, ambos opcionais e ambos indexados como keyword no Qdrant:

``task``
    Codigo da tarefa (6 digitos). Nasce de ``project`` acumular dois papeis
    incompativeis - repositorio (`sysmo-api-tributacao`) e numero de tarefa
    (`374954`). Levantado em 01/09/2026: 176 tarefas identificaveis no acervo, 22
    espalhadas por mais de um ``project`` (a 371145 em dois, a 370664 em cinco).
    Com ``task``, "tudo da tarefa 371145" vira filtro exato em vez de aposta
    semantica. ``project`` volta a ser so repositorio/produto.

``ingest_id``
    Procedencia: identificador comum a todos os fatos extraidos do MESMO texto.
    A extracao e fan-out - uma gravacao de correcao feita em 01/09/2026 virou
    tres fatos atomicos independentes (67737eaf, 4702f46c, 32015e8e), cada um
    com id, embedding e ciclo de vida proprios. Sem procedencia, superar a
    decisao alcanca um dos tres e deixa dois ativos. O valor e o id do job de
    escrita, que ja e unico por submissao.

Os dois convivem: ``task`` agrupa por assunto ao longo do tempo, ``ingest_id``
agrupa por ato de gravacao. Nenhum dos dois substitui ``project``.
"""

from __future__ import annotations

import re

# Codigo de tarefa do Redmine: 6 digitos, com ou sem "#", sem digito colado.
_TASK_IN_TEXT = re.compile(r"(?i)tarefas?\s*#?\s*(\d{6})(?!\d)")
_TASK_EXACT = re.compile(r"^#?(\d{6})$")


def normalize_task(value) -> str | None:
    """Normaliza uma chave de tarefa informada pelo chamador; None se nao for uma."""
    if value is None:
        return None
    m = _TASK_EXACT.match(str(value).strip())
    return m.group(1) if m else None


def infer_task_from_text(text) -> str | None:
    """Chave de tarefa citada explicitamente no texto (``TAREFA #NNNNNN``).

    Deliberadamente restrito a forma explicita: um numero de 6 digitos solto no
    meio de uma frase nao e chave de nada, e um falso positivo aqui contamina o
    filtro exato - que so vale a pena se for confiavel.
    """
    if not text:
        return None
    m = _TASK_IN_TEXT.search(str(text))
    return m.group(1) if m else None


def resolve_task(explicit, text) -> str | None:
    """A chave informada pelo chamador vence; senao tenta inferir do texto."""
    return normalize_task(explicit) or infer_task_from_text(text)
