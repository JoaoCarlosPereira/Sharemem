"""Identidade do sujeito de AccessControl / ator de specs.

Nunca confiar em ``subject_id`` / ``claimant`` vindos do cliente para ACL: o
sujeito vem de ``auth_user_var`` (session JWT ou agent token). Sem identidade
autenticada, ``subject_id=None`` preserva o comportamento aberto por padrão
(sem regras → todos os workspaces), igual às memórias em modo legado.
"""

from __future__ import annotations

import os
from typing import Any, Optional
from uuid import UUID

from app.utils.identity import resolve_hostname
from app.utils.logging_context import (
    auth_email_var,
    auth_method_var,
    auth_user_var,
    machine_var,
)


def is_legacy_spec_access_open() -> bool:
    """True quando Specs opera sem autenticação/isolamento na LAN.

    ``AUTH_MODE=warn`` permite requisições sem token como ``legacy``;
    ``AUTH_MODE=off`` ignora o middleware e pode deixar o método vazio.
    Credenciais explicitamente inválidas continuam sendo rejeitadas na borda.
    """
    mode = (os.getenv("AUTH_MODE") or "warn").strip().lower()
    method = (auth_method_var.get() or "").strip().lower()
    return mode in ("off", "warn") and (
        method == "legacy" or (mode == "off" and not method)
    )


def resolve_spec_subject() -> tuple[str, Optional[UUID]]:
    """``(subject_type, subject_id)`` a partir do contexto de autenticação."""
    raw = (auth_user_var.get() or "").strip()
    if not raw:
        return "user", None
    try:
        return "user", UUID(raw)
    except ValueError:
        return "user", None


def resolve_spec_actor(
    *,
    body_actor: Optional[str] = None,
    db: Any | None = None,
) -> Optional[str]:
    """Hostname / ator para claim, audit e versionamento.

    Preferência: máquina vinculada ao agent token → pessoa da sessão →
    ``body_actor`` (UI legada) → None.
    """
    bound = (machine_var.get() or "").strip()
    am = auth_method_var.get()
    if am in ("agent_token", "legacy") and bound:
        return resolve_hostname(bound)
    if auth_method_var.get() == "session":
        raw_user_id = (auth_user_var.get() or "").strip()
        if db is not None and raw_user_id:
            try:
                from app.models import User

                user = db.query(User).filter(User.id == UUID(raw_user_id)).first()
                if user is not None:
                    actor = (user.display_name or user.name or user.email or "").strip()
                    if actor:
                        return actor
            except (TypeError, ValueError):
                pass
        email = (auth_email_var.get() or "").strip()
        if email:
            return email
    if body_actor and str(body_actor).strip():
        return resolve_hostname(str(body_actor).strip())
    return None


def _email_of_user_id(db: Any, raw_user_id: str) -> Optional[str]:
    try:
        from app.models import User

        user = db.query(User).filter(User.id == UUID(raw_user_id)).first()
    except (TypeError, ValueError):
        return None
    email = (getattr(user, "email", None) or "").strip() if user is not None else ""
    return email or None


def resolve_spec_creator_email(db: Any | None = None) -> Optional[str]:
    """E-mail autenticado da pessoa criadora, ou ``None``.

    Só identidade verificada: sessão JWT, ou agent token cuja máquina está
    ``linked`` ao dono do token. ``legacy`` nunca atribui (hostname do path
    MCP é forjável). Nunca levanta.
    """
    method = (auth_method_var.get() or "").strip()
    raw_user_id = (auth_user_var.get() or "").strip()

    if method == "session":
        email = (auth_email_var.get() or "").strip()
        if email:
            return email
        return _email_of_user_id(db, raw_user_id) if raw_user_id and db is not None else None

    if method != "agent_token" or not raw_user_id or db is None:
        return None
    bound = (machine_var.get() or "").strip()
    if not bound:
        return None
    try:
        from app.models import MachineStatus
        from app.utils.machine_resolver import find_machine

        machine = find_machine(db, bound)
        if (
            machine is None
            or machine.status != MachineStatus.linked
            or str(machine.linked_user_id) != str(UUID(raw_user_id))
        ):
            return None
    except Exception:  # noqa: BLE001 — atribuição é best-effort
        return None
    return _email_of_user_id(db, raw_user_id)
