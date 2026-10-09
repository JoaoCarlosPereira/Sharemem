"""Durable audit trail for memory reads (search/list/get).

MCP and compat reads hit Qdrant directly — they never touch the SQL ``memories``
table or the legacy ``memory_access_logs`` (FK-bound to SQL rows). This module
records every memory returned by a read path so the Apps dashboard can show
*Memórias Acessadas* for project-scoped (Qdrant-backed) apps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.database import SessionLocal
from app.models import Project, get_current_utc_time
from app.read_audit_log_model import ReadAuditLog
from sqlalchemy import false, func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Tag a naive ``accessed_at`` as UTC (the column stores UTC without tzinfo)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with an explicit offset, so clients never guess the timezone."""
    aware = as_utc(value)
    return aware.isoformat() if aware else None


def _normalize_memory_id(memory_id: Any) -> Optional[str]:
    if memory_id is None:
        return None
    text = str(memory_id).strip()
    return text or None


def _project_from_item(item: dict, fallback: Optional[str]) -> Optional[str]:
    if fallback:
        return fallback
    meta = item.get("metadata") or item.get("metadata_") or {}
    if isinstance(meta, dict):
        project = meta.get("project")
        if project:
            return str(project)
    project = item.get("project")
    return str(project) if project else None


def record_memory_reads(
    *,
    project: Optional[str],
    memory_ids: Iterable[Any],
    access_type: str,
    source: str,
    hostname: Optional[str] = None,
    client_name: Optional[str] = None,
    query: Optional[str] = None,
    items: Optional[list[dict]] = None,
) -> None:
    """Persist read-access rows; never raise to callers.

    When ``items`` is provided (search/list result dicts), ``project`` is taken
    from each item's payload when the top-level ``project`` is missing (global
    reads). ``memory_ids`` alone is enough for project-scoped reads.
    """
    rows: list[tuple[str, str]] = []

    if items:
        for item in items:
            mid = _normalize_memory_id(item.get("id"))
            if not mid:
                continue
            proj = _project_from_item(item, project)
            if not proj:
                continue
            rows.append((proj, mid))
    else:
        if not project:
            return
        for raw_id in memory_ids:
            mid = _normalize_memory_id(raw_id)
            if mid:
                rows.append((project, mid))

    if not rows:
        return

    db = SessionLocal()
    try:
        now = get_current_utc_time()
        touched_projects: set[str] = set()
        for proj, mid in rows:
            db.add(
                ReadAuditLog(
                    project=proj,
                    memory_id=mid,
                    access_type=access_type,
                    source=source,
                    hostname=hostname or "unknown",
                    client_name=client_name,
                    query=query[:500] if query else None,
                    accessed_at=now,
                )
            )
            touched_projects.add(proj)

        for proj in touched_projects:
            row = db.query(Project).filter(Project.name == proj).first()
            if row is not None:
                row.last_activity_at = now

        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not record read audit (project=%s source=%s)", project, source)
        db.rollback()
    finally:
        db.close()


def count_distinct_memories_accessed(db: Session, project: str) -> int:
    return (
        db.query(func.count(func.distinct(ReadAuditLog.memory_id)))
        .filter(ReadAuditLog.project == project)
        .scalar()
        or 0
    )


def project_access_stats(db: Session, project: str) -> tuple[int, Optional[datetime], Optional[datetime]]:
    row = (
        db.query(
            func.count(func.distinct(ReadAuditLog.memory_id)).label("distinct_memories"),
            func.min(ReadAuditLog.accessed_at).label("first_accessed"),
            func.max(ReadAuditLog.accessed_at).label("last_accessed"),
        )
        .filter(ReadAuditLog.project == project)
        .first()
    )
    if not row:
        return 0, None, None
    return int(row.distinct_memories or 0), as_utc(row.first_accessed), as_utc(row.last_accessed)


def _normalize_audit_hostname(hostname: Optional[str]) -> Optional[str]:
    host = (hostname or "").strip()
    if not host or host in {"unknown", "unknown-host"}:
        return None
    if host.startswith("ui:"):
        host = host[3:].strip()
    return host or None


def canonical_audit_hostname(hostname: Optional[str]) -> Optional[str]:
    """Return the user hostname for analytics (strips ``ui:`` prefix)."""
    return _normalize_audit_hostname(hostname)


def read_audit_hostname_variants(hostname: str) -> list[str]:
    """All ``read_audit_logs.hostname`` values that belong to one user."""
    host = (hostname or "").strip()
    if not host:
        return []
    if host.startswith("ui:"):
        bare = host[3:].strip()
        return [bare, host] if bare else [host]
    return [host, f"ui:{host}"]


def audit_log_display_name(
    *,
    client_name: Optional[str],
    source: Optional[str],
) -> str:
    """Map audit row to a UI ``app_name`` key (see ``source-app.tsx``)."""
    name = (client_name or "").strip()
    if name and name not in {"unknown-client", "unknown"}:
        return name
    src = (source or "").strip().lower()
    if src in {"mcp", "api", "compat_v3", "admin"}:
        return "openmemory"
    return src or "default"


def audit_log_display_label(
    *,
    client_name: Optional[str],
    hostname: Optional[str],
    source: Optional[str],
) -> str:
    """Human-readable actor for access-log rows (hostname / client / fallback)."""
    host = _normalize_audit_hostname(hostname)
    if host:
        return host
    client = (client_name or "").strip()
    if client and client not in {"unknown-client", "unknown"}:
        return client
    src = (source or "").strip().lower()
    if src == "api":
        return "Interface Web"
    if src in {"mcp", "admin", "compat_v3"}:
        return "ShareMem"
    return src or "Desconhecido"


# --------------------------------------------------------------------------- #
# Access-log display grouping
#
# Every UI open/reload and every MCP search writes one ``read_audit_logs`` row
# (the table is an append-only audit trail and is NEVER pruned here). For the
# per-memory "Log de Acesso" we collapse *consecutive* rows that share the same
# actor (hostname + source + client) and access type inside a time window into
# a single display entry carrying ``count`` and the first/last timestamps.
#
# The window is anchored on the newest row of each group (``newest - row <=
# window``), so a group never spans more than the window even under periodic
# polling. ``ACCESS_LOG_GROUP_WINDOW_SECONDS`` (default 300 = 5 min) tunes it;
# ``0`` disables grouping. Grouping is computed over the memory's full history
# (up to ``ACCESS_LOG_GROUP_SCAN_LIMIT`` newest rows) *before* pagination so
# pages stay coherent: ``total`` counts display entries, ``raw_total`` rows.
# --------------------------------------------------------------------------- #
DEFAULT_ACCESS_LOG_GROUP_WINDOW_SECONDS = 300
# Bounded scan: rows beyond this are not grouped (``grouping_truncated``).
DEFAULT_ACCESS_LOG_GROUP_SCAN_LIMIT = 3000


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    import os

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, value)


def access_log_group_window_seconds() -> int:
    return _env_int("ACCESS_LOG_GROUP_WINDOW_SECONDS", DEFAULT_ACCESS_LOG_GROUP_WINDOW_SECONDS)


def access_log_group_scan_limit() -> int:
    return _env_int("ACCESS_LOG_GROUP_SCAN_LIMIT", DEFAULT_ACCESS_LOG_GROUP_SCAN_LIMIT, minimum=1)


# source → (channel, human label). ``api``/``admin`` are the Next.js UI.
_CHANNELS: dict[str, tuple[str, str]] = {
    "api": ("web", "Interface Web"),
    "admin": ("web", "Interface Web (admin)"),
    "mcp": ("mcp", "MCP"),
    "compat_v3": ("api", "API (compat v3)"),
}


def access_channel(source: Optional[str], hostname: Optional[str] = None) -> tuple[str, str]:
    """Return ``(channel, label)`` — ``web`` | ``mcp`` | ``api`` | ``other``."""
    src = (source or "").strip().lower()
    if src in _CHANNELS:
        return _CHANNELS[src]
    if (hostname or "").strip().startswith("ui:"):
        return _CHANNELS["api"]
    return "other", src or "Desconhecido"


def _actor_group_key(row: Any) -> tuple:
    return (
        (row.hostname or "").strip(),
        (row.source or "").strip().lower(),
        (row.client_name or "").strip().lower(),
        (row.access_type or "").strip().lower(),
    )


def group_read_audit_rows(rows: list[Any], window_seconds: int) -> list[list[Any]]:
    """Collapse same-actor/same-type rows inside a window (input newest first).

    A row joins the open group with the same key when it is within
    ``window_seconds`` of that group's newest row. Rows of *other* keys in
    between do not split the group — the UI fires ``list`` and ``get`` reads
    interleaved on every page open, and strict adjacency would leave the
    repetition visible. Output keeps newest-first order (by each group's
    newest row). Deterministic and pure: no rows are dropped.
    """
    if window_seconds <= 0:
        return [[row] for row in rows]
    groups: list[list[Any]] = []
    open_groups: dict[tuple, tuple[int, Optional[datetime]]] = {}
    for row in rows:
        key = _actor_group_key(row)
        ts = as_utc(row.accessed_at)
        current = open_groups.get(key)
        if current is not None:
            index, anchor = current
            if anchor is not None and ts is not None and (anchor - ts).total_seconds() <= window_seconds:
                groups[index].append(row)
                continue
        groups.append([row])
        open_groups[key] = (len(groups) - 1, ts)
    return groups


def _group_to_log(group: list[Any]) -> dict:
    newest = group[0]
    oldest = group[-1]
    channel, channel_label = access_channel(newest.source, newest.hostname)
    return {
        "id": str(newest.id),
        "app_name": audit_log_display_name(
            client_name=newest.client_name,
            source=newest.source,
        ),
        "display_name": audit_log_display_label(
            client_name=newest.client_name,
            hostname=newest.hostname,
            source=newest.source,
        ),
        "client_name": newest.client_name,
        "accessed_at": utc_isoformat(newest.accessed_at),
        "access_type": newest.access_type,
        "source": newest.source,
        "hostname": newest.hostname,
        "query": newest.query,
        # Additive fields (card 01ada614) — older clients ignore them.
        "channel": channel,
        "channel_label": channel_label,
        "count": len(group),
        "first_accessed_at": utc_isoformat(oldest.accessed_at),
        "last_accessed_at": utc_isoformat(newest.accessed_at),
    }


def list_memory_read_audit(
    db: Session,
    memory_id: str,
    *,
    page: int = 1,
    page_size: int = 10,
    grouped: bool = True,
    window_seconds: Optional[int] = None,
) -> tuple[int, list[dict]]:
    """Return paginated read-audit entries for a single memory (Qdrant/MCP path).

    With ``grouped`` (default) consecutive repeats are collapsed for display
    (see module notes); ``total`` is then the number of display entries.
    Returns ``(total, logs)``; use :func:`list_memory_read_audit_page` for the
    extra metadata (``raw_total``, window, truncation).
    """
    result = list_memory_read_audit_page(
        db,
        memory_id,
        page=page,
        page_size=page_size,
        grouped=grouped,
        window_seconds=window_seconds,
    )
    return result["total"], result["logs"]


def _channel_sources(channel: str) -> list[str]:
    """Sources for a channel filter; ``agents`` = every non-UI channel (mcp + api)."""
    wanted = {"mcp", "api"} if channel == "agents" else {channel}
    return [src for src, (ch, _label) in _CHANNELS.items() if ch in wanted]


def read_audit_channel_counts(db: Session, memory_id: str) -> dict[str, int]:
    """Raw row counts per channel (``web``/``mcp``/``api``/``other``) for a memory."""
    rows = (
        db.query(ReadAuditLog.source, func.count(ReadAuditLog.id))
        .filter(ReadAuditLog.memory_id == str(memory_id))
        .group_by(ReadAuditLog.source)
        .all()
    )
    counts: dict[str, int] = {"web": 0, "mcp": 0, "api": 0, "other": 0}
    for source, count in rows:
        channel, _ = access_channel(source)
        counts[channel] = counts.get(channel, 0) + int(count or 0)
    return counts


def list_memory_read_audit_page(
    db: Session,
    memory_id: str,
    *,
    page: int = 1,
    page_size: int = 10,
    grouped: bool = True,
    window_seconds: Optional[int] = None,
    channel: Optional[str] = None,
) -> dict:
    """Paginated (optionally grouped) access log for one memory.

    ``channel`` (``web`` | ``mcp`` | ``api`` | ``agents``) restricts rows to one
    access channel (``agents`` = MCP + compat API) — e.g. ``mcp`` hides the viewer's own Web UI reads, which otherwise
    dominate the log of any memory people browse.
    """
    # Only the columns the log needs (no ORM entities): grouping scans up to
    # ``ACCESS_LOG_GROUP_SCAN_LIMIT`` rows per request.
    columns = (
        ReadAuditLog.id,
        ReadAuditLog.hostname,
        ReadAuditLog.source,
        ReadAuditLog.client_name,
        ReadAuditLog.access_type,
        ReadAuditLog.query,
        ReadAuditLog.accessed_at,
    )
    conditions = [ReadAuditLog.memory_id == str(memory_id)]
    if channel:
        sources = _channel_sources(channel)
        conditions.append(ReadAuditLog.source.in_(sources) if sources else false())
    raw_total = db.query(func.count(ReadAuditLog.id)).filter(*conditions).scalar() or 0
    base = db.query(*columns).filter(*conditions)
    window = access_log_group_window_seconds() if window_seconds is None else max(0, window_seconds)
    if not grouped:
        window = 0
    ordered = base.order_by(ReadAuditLog.accessed_at.desc(), ReadAuditLog.id.desc())
    truncated = False

    if window <= 0:
        rows = ordered.offset((page - 1) * page_size).limit(page_size).all()
        groups = [[row] for row in rows]
        total = raw_total
    else:
        limit = access_log_group_scan_limit()
        rows = ordered.limit(limit).all() if raw_total else []
        truncated = raw_total > limit
        all_groups = group_read_audit_rows(rows, window)
        total = len(all_groups)
        start = (page - 1) * page_size
        groups = all_groups[start : start + page_size]

    logs = [_group_to_log(group) for group in groups]
    from app.utils.creator_identity import enrich_reader_items

    enrich_reader_items(logs)
    return {
        "total": total,
        "raw_total": raw_total,
        "logs": logs,
        "grouped": window > 0,
        "group_window_seconds": window,
        "grouping_truncated": truncated,
        "channel": channel or None,
        "channel_counts": read_audit_channel_counts(db, memory_id),
    }


def list_project_accessed_memories(
    db: Session,
    project: str,
    *,
    page: int = 1,
    page_size: int = 10,
) -> tuple[int, list[dict]]:
    """Return memories accessed for a project with per-memory access counts."""
    from app.utils.vector_stats import get_shared_memory_by_id

    base = (
        db.query(
            ReadAuditLog.memory_id,
            func.count(ReadAuditLog.id).label("access_count"),
            func.max(ReadAuditLog.accessed_at).label("last_accessed"),
        )
        .filter(ReadAuditLog.project == project)
        .group_by(ReadAuditLog.memory_id)
    )
    total = base.count()
    rows = (
        base.order_by(func.max(ReadAuditLog.accessed_at).desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    memories: list[dict] = []
    for memory_id, access_count, last_accessed in rows:
        shared = get_shared_memory_by_id(str(memory_id)) or {}
        latest_access = (
            db.query(ReadAuditLog)
            .filter(ReadAuditLog.memory_id == str(memory_id))
            .order_by(ReadAuditLog.accessed_at.desc())
            .first()
        )
        latest_client = latest_access.client_name if latest_access else None
        if latest_access and latest_access.source == "api" and latest_client == "openmemory":
            latest_client = "Interface"
        memories.append(
            {
                "memory": {
                    "id": str(memory_id),
                    "content": shared.get("text") or shared.get("content") or "",
                    "created_at": shared.get("created_at"),
                    "state": shared.get("state") or "active",
                    "app_id": None,
                    "app_name": project,
                    "created_by_hostname": shared.get("created_by_hostname"),
                    "created_by_client": shared.get("created_by_client"),
                    "categories": shared.get("categories") or [],
                    "metadata_": shared.get("metadata_") or {},
                },
                "access_count": int(access_count or 0),
                "last_accessed": as_utc(last_accessed),
                "accessed_by_client": latest_client,
            }
        )
    return total, memories
