"""Normalização canônica do nome de projeto (chave de escopo das memórias).

O SDK mem0 (``mem0/memory/main.py::_validate_and_trim_entity_id``) rejeita
``project`` com espaço interno. Como a escrita MCP é assíncrona (fila +
worker), um projeto como ``"PONTEIRO DE SPEC"`` era aceito no ack e o job
falhava no worker depois de esgotar as tentativas — a memória se perdia em
silêncio.

Regra (decisão do usuário): ``strip`` + qualquer sequência de whitespace
interno vira ``-``, PRESERVANDO maiúsculas/minúsculas. Aplicada na entrada
(MCP/REST/compat), na leitura (busca/listagem/filtro estrito) e,
defensivamente, no write worker para jobs já enfileirados com payload antigo.

Vazio/``None`` NÃO é tratado aqui: o valor é devolvido como veio para que cada
chamador mantenha o comportamento atual (erro "project not provided",
fallback ``default``, etc.).

Não confundir com :func:`app.utils.recency.normalize_project_name`, que é uma
comparação *fuzzy* (minúsculas, só alfanuméricos) para ranking/famílias e não
produz uma chave gravável.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_project(value: Optional[str]) -> Optional[str]:
    """Devolve a chave efetiva do projeto (sem whitespace, caixa preservada).

    ``None`` e strings vazias/só-espaço voltam inalteradas (comportamento
    atual de cada chamador). Idempotente.
    """
    if value is None:
        return None
    text = str(value)
    stripped = text.strip()
    if not stripped:
        return value
    return _WHITESPACE_RUN.sub("-", stripped)


def normalize_project_with_notice(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Como :func:`normalize_project`, mais um aviso quando houve troca de whitespace interno.

    O aviso só é emitido quando o nome mudou além do ``strip`` (que sempre foi
    silencioso), para que o agente saiba sob qual chave a memória ficou.
    """
    effective = normalize_project(value)
    if value is None or effective is None or effective == str(value).strip():
        return effective, None
    notice = (
        f"project normalized: whitespace replaced by '-' "
        f"({str(value).strip()!r} -> {effective!r}). Use {effective!r} in future calls; "
        "searches with the original spelling are normalized the same way."
    )
    return effective, notice
