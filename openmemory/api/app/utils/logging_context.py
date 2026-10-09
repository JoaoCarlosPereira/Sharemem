"""Structured logging context (request_id / job_id correlation)."""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

LOG_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def configure_process_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")
team_var: contextvars.ContextVar[str] = contextvars.ContextVar("team", default="")

# Identidade resolvida pelo AuthMiddleware (feature auth Google, ADR-006).
# ``auth_method``: session | agent_token | team | legacy; ``auth_user``: UUID da
# pessoa; ``machine``: hostname da URL MCP validado. Padrão set/reset por token
# (mesmo contrato de ``team_var``).
auth_method_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "auth_method", default=""
)
auth_user_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "auth_user", default=""
)
auth_email_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "auth_email", default=""
)
machine_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "machine", default=""
)

# Credencial em query string nunca aparece em log (ADR-003): mascara qualquer
# ``token=<valor>`` na mensagem final do record.
_TOKEN_MASK_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)


class StructuredContextFilter(logging.Filter):
    """Inject ``request_id`` and ``job_id`` into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.job_id = job_id_var.get() or "-"
        record.trace_id = _safe_trace_id()
        record.auth_method = auth_method_var.get() or "-"
        record.auth_user = auth_user_var.get() or "-"
        return True


class TokenMaskingFilter(logging.Filter):
    """Mascara ``token=...`` em toda mensagem de log (fail-safe, nunca levanta)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — log defeituoso não pode derrubar o fluxo
            return True
        masked = _TOKEN_MASK_RE.sub(r"\1***", message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def _safe_trace_id() -> str:
    """``trace_id`` do span OTel corrente para pivô log↔trace (``-`` se ausente)."""
    try:
        from app.utils.tracing import current_trace_id

        return current_trace_id() or "-"
    except Exception:  # noqa: BLE001
        return "-"


def install_structured_logging() -> None:
    """Attach the context and masking filters to the root logger once."""
    root = logging.getLogger()
    if any(isinstance(f, StructuredContextFilter) for f in root.filters):
        return
    root.addFilter(StructuredContextFilter())
    root.addFilter(TokenMaskingFilter())
    for handler in root.handlers:
        handler.addFilter(StructuredContextFilter())
        handler.addFilter(TokenMaskingFilter())


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
