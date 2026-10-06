"""Fail-closed auth for destructive ``/admin/*`` mutations.

Border ``AUTH_MODE=warn`` still allows anonymous GETs; restore/purge/requeue-done
and similar mutations require an authenticated admin even in warn:

1. ``X-Admin-Token`` / ``Authorization: Bearer`` matching ``ADMIN_TOKEN``, or
2. Valid session JWT (``auth_method=session``) whose email is in
   ``AUTH_ADMIN_EMAILS`` (comma-separated). When the allowlist is empty, any
   valid session is accepted (UI operators after Google login).

Legacy / ``Bearer local`` / unknown team tokens are never sufficient.
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import HTTPException, Request

from app.utils.logging_context import auth_email_var, auth_method_var


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def admin_token_configured() -> str:
    return _env("ADMIN_TOKEN")


def admin_email_allowlist() -> set[str]:
    raw = _env("AUTH_ADMIN_EMAILS")
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _extract_admin_token(request: Request) -> Optional[str]:
    header = request.headers.get("x-admin-token")
    if header and header.strip():
        return header.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def require_admin(request: Request) -> None:
    """FastAPI dependency: raise 401/403 unless caller is an authenticated admin."""
    expected = admin_token_configured()
    provided = _extract_admin_token(request)
    if expected and provided and hmac.compare_digest(provided, expected):
        return

    method = auth_method_var.get() or ""
    if method in ("session", "admin_token"):
        if method == "session":
            email = (auth_email_var.get() or "").strip().lower()
            allowlist = admin_email_allowlist()
            if allowlist and email not in allowlist:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "admin access denied: session email not in AUTH_ADMIN_EMAILS"
                    ),
                )
        return

    if expected:
        raise HTTPException(
            status_code=401,
            detail=(
                "admin credentials required: send X-Admin-Token (ADMIN_TOKEN) "
                "or a valid session JWT"
            ),
        )
    raise HTTPException(
        status_code=401,
        detail=(
            "admin credentials required: set ADMIN_TOKEN or sign in "
            "(session JWT). Legacy/anonymous access is not allowed for this endpoint."
        ),
    )
