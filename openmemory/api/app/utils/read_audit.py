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
from sqlalchemy import func
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


def list_memory_read_audit(
    db: Session,
    memory_id: str,
    *,
    page: int = 1,
    page_size: int = 10,
) -> tuple[int, list[dict]]:
    """Return paginated read-audit rows for a single memory (Qdrant/MCP path)."""
    base = db.query(ReadAuditLog).filter(ReadAuditLog.memory_id == str(memory_id))
    total = base.count()
    rows = (
        base.order_by(ReadAuditLog.accessed_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    logs = [
        {
            "id": str(row.id),
            "app_name": audit_log_display_name(
                client_name=row.client_name,
                source=row.source,
            ),
            "display_name": audit_log_display_label(
                client_name=row.client_name,
                hostname=row.hostname,
                source=row.source,
            ),
            "client_name": row.client_name,
            "accessed_at": utc_isoformat(row.accessed_at),
            "access_type": row.access_type,
            "source": row.source,
            "hostname": row.hostname,
            "query": row.query,
        }
        for row in rows
    ]
    from app.utils.creator_identity import enrich_actor_items

    enrich_actor_items(logs)
    return total, logs


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
