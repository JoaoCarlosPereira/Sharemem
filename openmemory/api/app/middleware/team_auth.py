"""Autenticação unificada na borda (ADR-006 da feature auth Google).

Evolução do ``TeamAuthMiddleware`` (task_11/ADR-006 de prontidão-produção) para
um ponto único que resolve QUATRO métodos de credencial:

- ``agent_token`` — token opaco do agente MCP, via ``?token=`` na URL (ADR-003)
  ou header com prefixo ``omtk_``; hash validado em ``agent_tokens`` (cache
  Redis). Credencial explícita inválida/revogada ⇒ 401 em qualquer modo.
- ``session``     — JWT de sessão da UI (``Authorization: Bearer``, formato
  JWT). Inválido/expirado ⇒ 401 em qualquer modo.
- ``team``        — tokens de equipe existentes (``X-API-Key``/Bearer opaco).
  Comportamento 100% preservado nos modos ``off|warn|enforce``.
- ``internal``    — ``PLANKA_INTERNAL_ACCESS_TOKEN`` / ``INTERNAL_ACCESS_TOKEN``
  (bridge PLANKA → Spec ``/api/v1/specs/planka/card-moved``, ADR-007).
- ``legacy``      — sem credencial, ou ``Bearer local`` (shim OAuth): passa em
  ``warn`` (default) e é rejeitado apenas em ``enforce`` — o fluxo por hostname
  continua intacto (Fase 1). Bearer presente mas inválido ⇒ 401 em qualquer modo.

A identidade resolvida vai para contextvars (``auth_method_var``,
``auth_user_var``, ``auth_email_var``, ``machine_var`` — ``app.utils.logging_context``)
para consumo por auditoria, métricas e MCP. O valor de ``?token=`` nunca aparece
em log (``TokenMaskingFilter`` + logs deste módulo usam só o path).

Modos (env ``AUTH_MODE``): ``off`` (bypass), ``warn`` (default), ``enforce``.
Tokens de equipe: secret ``AUTH_TOKENS_FILE`` ou env ``AUTH_TOKENS``.

Implementação ASGI pura (não ``BaseHTTPMiddleware``): o MCP SSE/streamable
escreve direto em ``request._send``; buffering do BaseHTTPMiddleware causava
``AssertionError: Unexpected message ... http.response.start`` (corpo ``null``)
e derrubava a sessão — Cursor então reportava ``MCP server mem0 is not connected``.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.utils.agent_tokens import AGENT_TOKEN_PREFIX, resolve_agent_token
from app.utils.logging_context import (
    auth_email_var,
    auth_method_var,
    auth_user_var,
    machine_var,
    team_var,
)
from app.utils.metrics import AUTH_DENIED_TOTAL, AUTH_OK_TOTAL
from app.utils.session_jwt import SessionJwtError, decode_session_jwt

logger = logging.getLogger(__name__)

# Alinhado a ``mcp_oauth_compat.LEGACY_MCP_BEARER`` / ``MEM0_API_KEY=local``.
LEGACY_MCP_BEARER = "local"

_SKIP_PREFIXES = (
    "/health",
    "/metrics",
    "/docs",
    "/openapi",
    "/redoc",
    "/.well-known/oauth-authorization-server",
    "/token",
)

# Hostname posicional das rotas MCP: /mcp/{client}/(sse|http)/{hostname}
_MCP_HOST_RE = re.compile(r"^/mcp/[^/]+/(?:sse|http)/([^/?]+)")


@dataclass
class AuthContext:
    """Identidade resolvida para a requisição corrente."""

    method: str  # "session" | "agent_token" | "team" | "admin_token" | "internal" | "legacy"
    user_id: Optional[str] = None
    email: Optional[str] = None
    machine_hostname: Optional[str] = None
    team: Optional[str] = None


def load_team_tokens() -> Dict[str, str]:
    """Carrega o mapa ``token -> team`` de um secret (arquivo) ou env.

    Prioridade: ``AUTH_TOKENS_FILE`` (JSON ``{team: token}`` ou linhas
    ``team:token``) > ``AUTH_TOKENS`` (``team1:tok1,team2:tok2``) > vazio.
    """
    path = os.getenv("AUTH_TOKENS_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
        try:
            data = json.loads(raw)
            return {tok: team for team, tok in data.items()}
        except json.JSONDecodeError:
            return _parse_pairs(raw.replace("\n", ","))
    inline = os.getenv("AUTH_TOKENS")
    if inline:
        return _parse_pairs(inline)
    return {}


def _parse_pairs(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        team, tok = pair.split(":", 1)
        out[tok.strip()] = team.strip()
    return out


def _extract_token(request: Request) -> Optional[str]:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _mcp_hostname(path: str) -> Optional[str]:
    match = _MCP_HOST_RE.match(path)
    return match.group(1) if match else None


def resolve_bound_machine_hostname(
    person_id: str, path_hostname: Optional[str]
) -> Optional[str]:
    """Prefer máquina vinculada à pessoa sobre hostname spoofável na URL MCP.

    Se o path bater com uma máquina ``linked`` da pessoa, usa o path. Caso
    contrário, se houver vínculo(s), usa o primeiro hostname vinculado
    (ordenado) em vez do path. Sem vínculos, cai no path (legado).
    """
    if not person_id:
        return path_hostname
    try:
        import uuid as _uuid

        from app.database import SessionLocal
        from app.models import Machine, MachineStatus
        from app.utils.identity import resolve_hostname

        db = SessionLocal()
        try:
            linked = sorted(
                m.hostname
                for m in db.query(Machine)
                .filter(
                    Machine.linked_user_id == _uuid.UUID(person_id),
                    Machine.status == MachineStatus.linked,
                )
                .all()
                if m.hostname
            )
        finally:
            db.close()
        if not linked:
            return path_hostname
        if path_hostname:
            key = resolve_hostname(path_hostname)
            if key in linked:
                return key
            logger.warning(
                "hostname da URL %s ignorado; usando máquina vinculada %s "
                "(pessoa %s)",
                key,
                linked[0],
                person_id,
            )
        return linked[0]
    except Exception:  # noqa: BLE001 — best-effort; não derruba auth
        logger.debug("resolve_bound_machine_hostname failed", exc_info=True)
        return path_hostname


class AuthMiddleware:
    """Pure ASGI auth middleware — safe for MCP SSE / streamable HTTP."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mode: Optional[str] = None,
        token_to_team: Optional[Dict[str, str]] = None,
    ):
        self.app = app
        self._mode = (mode or os.getenv("AUTH_MODE", "warn")).strip().lower()
        self._tokens = token_to_team if token_to_team is not None else load_team_tokens()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if any(request.url.path.startswith(p) for p in _SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        path = request.url.path

        if self._mode == "off":
            # Bypass de credencial, mas a identidade ainda precisa cair em
            # ``legacy`` nas contextvars — do contrário ``auth_method_var``
            # fica com o default ("") e código downstream (ex.: emissão do
            # token do Kanban/PLANKA em ``specs.py``) não reconhece o modo
            # compartilhado, resolve grupo == None e responde 403 "Usuário
            # sem grupo associado" mesmo com AUTH_MODE=off.
            ctx = AuthContext(method="legacy", machine_hostname=_mcp_hostname(path))
            await self._call_with_context(scope, receive, send, ctx)
            return

        # 1) ?token= nas rotas MCP — credencial explícita de agente (ADR-003).
        query_token = (
            request.query_params.get("token") if path.startswith("/mcp") else None
        )
        if query_token:
            await self._handle_agent_token(scope, receive, send, request, query_token)
            return

        header_token = _extract_token(request)
        if header_token:
            # 2) JWT de sessão da UI — credencial explícita de pessoa.
            if _looks_like_jwt(header_token):
                try:
                    claims = decode_session_jwt(header_token)
                except SessionJwtError:
                    AUTH_DENIED_TOTAL.labels(mode=self._mode).inc()
                    await self._send_unauthorized(
                        scope, receive, send, request, "sessão inválida ou expirada"
                    )
                    return
                AUTH_OK_TOTAL.labels(method="session").inc()
                ctx = AuthContext(
                    method="session",
                    user_id=str(claims.get("sub") or ""),
                    email=str(claims.get("email") or ""),
                )
                await self._call_with_context(scope, receive, send, ctx)
                return

            # 3) Token de agente enviado por header (prefixo identificável).
            if header_token.startswith(AGENT_TOKEN_PREFIX):
                await self._handle_agent_token(
                    scope, receive, send, request, header_token
                )
                return

            # 4) Token de equipe (comportamento original preservado).
            team = self._tokens.get(header_token)
            if team is not None:
                AUTH_OK_TOTAL.labels(method="team").inc()
                ctx = AuthContext(method="team", team=team)
                await self._call_with_context(scope, receive, send, ctx)
                return

            # 4b) ADMIN_TOKEN via Bearer (mutações /admin — mesmo valor que X-Admin-Token).
            admin = (os.getenv("ADMIN_TOKEN") or "").strip()
            if admin and hmac.compare_digest(header_token, admin):
                AUTH_OK_TOTAL.labels(method="admin_token").inc()
                ctx = AuthContext(method="admin_token")
                await self._call_with_context(scope, receive, send, ctx)
                return

            # 4c) PLANKA ↔ Spec bridge (ADR-007): Bearer INTERNAL / PLANKA_INTERNAL.
            # Sem isto, POST /api/v1/specs/planka/card-moved recebe 401 em AUTH_MODE
            # enforce/warn (Bearer presente mas não-team), e o DnD humano reverte
            # com E_FORBIDDEN.
            internal = (
                os.getenv("PLANKA_INTERNAL_ACCESS_TOKEN")
                or os.getenv("INTERNAL_ACCESS_TOKEN")
                or ""
            ).strip()
            if internal and hmac.compare_digest(header_token, internal):
                AUTH_OK_TOTAL.labels(method="internal").inc()
                ctx = AuthContext(method="internal", team="planka-bridge")
                await self._call_with_context(scope, receive, send, ctx)
                return

            # 5) Bearer local (shim OAuth) — legado documentado; só em warn/off.
            if header_token == LEGACY_MCP_BEARER:
                await self._handle_legacy(
                    scope, receive, send, request, path, reason="Bearer local"
                )
                return

            # Credencial Bearer/X-API-Key presente mas inválida ⇒ 401 (mesmo em warn).
            AUTH_DENIED_TOTAL.labels(mode=self._mode).inc()
            await self._send_unauthorized(
                scope, receive, send, request, "invalid or missing team token"
            )
            return

        # 6) Sem header de credencial — caminho legado.
        await self._handle_legacy(
            scope, receive, send, request, path, reason="sem token"
        )

    async def _handle_legacy(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        path: str,
        *,
        reason: str,
    ) -> None:
        AUTH_DENIED_TOTAL.labels(mode=self._mode).inc()
        if self._mode == "enforce":
            await self._send_unauthorized(
                scope, receive, send, request, "invalid or missing team token"
            )
            return
        logger.warning("auth warn: requisição %s em %s", reason, path)
        ctx = AuthContext(method="legacy", machine_hostname=_mcp_hostname(path))
        await self._call_with_context(scope, receive, send, ctx)

    async def _handle_agent_token(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        raw_token: str,
    ) -> None:
        """Valida token de agente; explícito e inválido ⇒ 401 em qualquer modo."""
        user_id = resolve_agent_token(raw_token)
        if user_id is None:
            AUTH_DENIED_TOTAL.labels(mode=self._mode).inc()
            await self._send_unauthorized(
                scope, receive, send, request, "token de agente inválido ou revogado"
            )
            return
        AUTH_OK_TOTAL.labels(method="agent_token").inc()
        path_host = _mcp_hostname(request.url.path)
        ctx = AuthContext(
            method="agent_token",
            user_id=user_id,
            machine_hostname=resolve_bound_machine_hostname(user_id, path_host),
        )
        await self._call_with_context(scope, receive, send, ctx)

    async def _call_with_context(
        self, scope: Scope, receive: Receive, send: Send, ctx: AuthContext
    ) -> None:
        """Popula as contextvars de identidade com reset garantido no finally."""
        tokens = [
            (auth_method_var, auth_method_var.set(ctx.method)),
            (auth_user_var, auth_user_var.set(ctx.user_id or "")),
            (auth_email_var, auth_email_var.set(ctx.email or "")),
            (machine_var, machine_var.set(ctx.machine_hostname or "")),
        ]
        if ctx.team:
            tokens.append((team_var, team_var.set(ctx.team)))
        try:
            await self.app(scope, receive, send)
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    async def _send_unauthorized(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        detail: str,
    ) -> None:
        response = self._unauthorized(request, detail)
        await response(scope, receive, send)

    def _unauthorized(self, request: Request, detail: str) -> JSONResponse:
        """401 com headers CORS ecoados — o middleware roda fora do CORSMiddleware."""
        response = JSONResponse(status_code=401, content={"detail": detail})
        origin = request.headers.get("origin")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response


# Alias de compatibilidade: consumidores existentes (main.py, testes) continuam
# importando ``TeamAuthMiddleware``.
TeamAuthMiddleware = AuthMiddleware
