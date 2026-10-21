"""Per-user and per-group analytics from write/read audit tables."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from typing import Literal, Optional
from uuid import UUID

from app.models import (
    DEFAULT_GROUP_NAME,
    Group,
    Machine,
    User,
    WriteAuditLog,
    WriteQueueJob,
    get_current_utc_time,
)
from app.read_audit_log_model import ReadAuditLog
from app.utils.datetime_utc import as_utc_naive
from app.utils.identity import DEFAULT_HOSTNAME
from app.utils.legacy_user_deletion import protected_user_ids
from app.utils.machine_resolver import legacy_hostname_variants, linked_legacy_ids_for_users
from app.utils.read_audit import read_audit_hostname_variants
from sqlalchemy import case, func
from sqlalchemy.orm import Session

UsageLevel = str  # online | offline
ContributorMetric = Literal["writes", "reads", "total"]
ContributorPeriod = Literal["24h", "7d", "30d", "all"]

PREVIEW_MAX_LEN = 160

CONTRIBUTOR_WRITE_WEIGHT = 0.7
CONTRIBUTOR_READ_WEIGHT = 0.3


def _truncate_preview(text: Optional[str], *, limit: int = PREVIEW_MAX_LEN) -> Optional[str]:
    if not text:
        return None
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


# Back-compat alias used across analytics helpers/tests.
_as_utc_naive = as_utc_naive


def _since(hours: Optional[int] = None, days: Optional[int] = None) -> Optional[datetime]:
    if hours is None and days is None:
        return None
    now = as_utc_naive(get_current_utc_time())
    if hours is not None:
        return now - timedelta(hours=hours)
    return now - timedelta(days=days)


def _period_since(period: ContributorPeriod) -> Optional[datetime]:
    if period == "24h":
        return _since(hours=24)
    if period == "7d":
        return _since(days=7)
    if period == "30d":
        return _since(days=30)
    return None


def is_system_contributor(hostname: str) -> bool:
    """True para contas de serviço (UI, fallback, usuários protegidos)."""
    key = (hostname or "").strip().lower()
    if not key:
        return True
    if key in {"unknown", DEFAULT_HOSTNAME.lower()}:
        return True
    blocked = {value.lower() for value in protected_user_ids()}
    blocked.add(os.getenv("OPENMEMORY_UI_USER_ID", "openmemory").strip().lower())
    return key in blocked


def _is_default_group(group: Group | None) -> bool:
    return group is not None and group.name.lower() == DEFAULT_GROUP_NAME.lower()


def is_real_contributor(user: User, hostname: str) -> bool:
    """Membros reais: fora do grupo Default e fora das contas de sistema."""
    if is_system_contributor(hostname):
        return False
    return not _is_default_group(user.group)


def visible_group_members(db: Session, group_id: UUID) -> list[tuple[User, str]]:
    """Return person accounts and unpaired legacy hosts with metric hostnames."""
    users = db.query(User).filter(User.group_id == group_id).all()
    user_ids = [user.id for user in users]
    linked_legacy_ids = linked_legacy_ids_for_users(db, user_ids)
    machines = (
        db.query(Machine).filter(Machine.linked_user_id.in_(user_ids)).all()
        if user_ids
        else []
    )
    machine_by_person = {
        machine.linked_user_id: machine.hostname
        for machine in machines
        if machine.linked_user_id is not None
    }
    visible = [
        (user, machine_by_person.get(user.id, user.user_id))
        for user in users
        if user.id not in linked_legacy_ids
    ]
    return sorted(visible, key=lambda item: item[1].lower())


def all_visible_members(
    db: Session,
    *,
    group_id: Optional[UUID] = None,
    real_users_only: bool = False,
) -> list[tuple[User, str]]:
    """Visible members across all groups, or restricted to one group."""
    if group_id is not None:
        members = visible_group_members(db, group_id)
    else:
        members = []
        groups = db.query(Group).order_by(Group.name).all()
        for group in groups:
            members.extend(visible_group_members(db, group.id))
    if real_users_only:
        members = [
            (user, hostname)
            for user, hostname in members
            if is_real_contributor(user, hostname)
        ]
    return members


def _audit_variants_for_hosts(db: Session, hostnames: list[str]) -> tuple[list[str], dict[str, str]]:
    """Expande hostnames para variantes de auditoria e mapeia de volta ao membro canônico."""
    all_variants: list[str] = []
    variant_to_member: dict[str, str] = {}
    for host in hostnames:
        keys = set(legacy_hostname_variants(db, host))
        for variant in read_audit_hostname_variants(host):
            keys.add(variant)
        for variant in keys:
            all_variants.append(variant)
            variant_to_member[variant] = host
    return all_variants, variant_to_member


def _write_stats_for_hostnames(
    db: Session,
    hostnames: list[str],
    *,
    since: Optional[datetime] = None,
    project: Optional[str] = None,
) -> dict[str, dict]:
    if not hostnames:
        return {}
    all_variants, variant_to_member = _audit_variants_for_hosts(db, hostnames)
    if not all_variants:
        return {}
    q = db.query(
        WriteAuditLog.hostname,
        func.count(WriteAuditLog.id).label("total"),
        func.count(func.distinct(WriteAuditLog.project)).label("projects"),
        func.max(WriteAuditLog.created_at).label("last_at"),
    ).filter(WriteAuditLog.hostname.in_(all_variants))
    if since is not None:
        q = q.filter(WriteAuditLog.created_at >= since)
    if project is not None:
        q = q.filter(WriteAuditLog.project == project)
    rows = q.group_by(WriteAuditLog.hostname).all()

    result: dict[str, dict] = {
        host: {"total": 0, "projects": 0, "last_at": None} for host in hostnames
    }
    project_sets: dict[str, set[str]] = {host: set() for host in hostnames}
    for row in rows:
        member = variant_to_member.get(row.hostname, row.hostname)
        if member not in result:
            continue
        bucket = result[member]
        bucket["total"] += int(row.total or 0)
        if row.last_at and (bucket["last_at"] is None or row.last_at > bucket["last_at"]):
            bucket["last_at"] = row.last_at

    project_rows = (
        db.query(WriteAuditLog.hostname, WriteAuditLog.project)
        .filter(WriteAuditLog.hostname.in_(all_variants))
        .distinct()
    )
    if since is not None:
        project_rows = project_rows.filter(WriteAuditLog.created_at >= since)
    if project is not None:
        project_rows = project_rows.filter(WriteAuditLog.project == project)
    for hostname, proj in project_rows:
        member = variant_to_member.get(hostname, hostname)
        if member in project_sets and proj:
            project_sets[member].add(proj)

    for host in hostnames:
        result[host]["projects"] = len(project_sets[host])
    return result


def _read_stats_for_hostnames(
    db: Session,
    hostnames: list[str],
    *,
    since: Optional[datetime] = None,
    project: Optional[str] = None,
) -> dict[str, dict]:
    if not hostnames:
        return {}

    all_variants, variant_to_member = _audit_variants_for_hosts(db, hostnames)
    if not all_variants:
        return {}

    canonical = case(
        (ReadAuditLog.hostname.like("ui:%"), func.substr(ReadAuditLog.hostname, 4)),
        else_=ReadAuditLog.hostname,
    )

    q = db.query(
        canonical.label("canonical"),
        func.count(ReadAuditLog.id).label("total"),
        func.count(func.distinct(ReadAuditLog.project)).label("projects"),
        func.count(func.distinct(ReadAuditLog.memory_id)).label("memories"),
        func.max(ReadAuditLog.accessed_at).label("last_at"),
    ).filter(ReadAuditLog.hostname.in_(all_variants))
    if since is not None:
        q = q.filter(ReadAuditLog.accessed_at >= since)
    if project is not None:
        q = q.filter(ReadAuditLog.project == project)
    rows = q.group_by(canonical).all()

    result: dict[str, dict] = {
        host: {"total": 0, "projects": 0, "memories": 0, "last_at": None} for host in hostnames
    }
    for row in rows:
        key = (row.canonical or "").strip()
        member = variant_to_member.get(key, key)
        if member not in result:
            continue
        bucket = result[member]
        bucket["total"] += int(row.total or 0)
        bucket["projects"] += int(row.projects or 0)
        bucket["memories"] += int(row.memories or 0)
        if row.last_at and (bucket["last_at"] is None or row.last_at > bucket["last_at"]):
            bucket["last_at"] = row.last_at
    return result


def _last_interaction_at(
    last_write_at: Optional[datetime],
    last_read_at: Optional[datetime],
) -> Optional[datetime]:
    candidates = [_as_utc_naive(dt) for dt in (last_write_at, last_read_at) if dt is not None]
    if not candidates:
        return None
    return max(candidates)


def classify_presence(
    *,
    writes_24h: int,
    reads_24h: int,
    last_write_at: Optional[datetime],
    last_read_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> tuple[UsageLevel, Optional[int]]:
    """Online = escrita ou leitura nas últimas 24h; offline = sem interação nesse período."""
    if writes_24h > 0 or reads_24h > 0:
        return "online", None

    last_at = _last_interaction_at(last_write_at, last_read_at)
    if last_at is None:
        return "offline", None

    reference = _as_utc_naive(now or get_current_utc_time())
    elapsed = reference - _as_utc_naive(last_at)
    offline_days = max(1, elapsed.days)
    return "offline", offline_days


def user_activity_stats(db: Session, hostname: str) -> dict:
    """Aggregate write/read counts for a single hostname."""
    writes_all = _write_stats_for_hostnames(db, [hostname])
    reads_all = _read_stats_for_hostnames(db, [hostname])
    writes_24h = _write_stats_for_hostnames(db, [hostname], since=_since(hours=24))
    reads_24h = _read_stats_for_hostnames(db, [hostname], since=_since(hours=24))
    writes_7d = _write_stats_for_hostnames(db, [hostname], since=_since(days=7))
    reads_7d = _read_stats_for_hostnames(db, [hostname], since=_since(days=7))
    writes_30d = _write_stats_for_hostnames(db, [hostname], since=_since(days=30))
    reads_30d = _read_stats_for_hostnames(db, [hostname], since=_since(days=30))

    w = writes_all.get(hostname, {})
    r = reads_all.get(hostname, {})
    w24 = writes_24h.get(hostname, {})
    r24 = reads_24h.get(hostname, {})
    w7 = writes_7d.get(hostname, {})
    r7 = reads_7d.get(hostname, {})
    w30 = writes_30d.get(hostname, {})
    r30 = reads_30d.get(hostname, {})

    writes_total = w.get("total", 0)
    reads_total = r.get("total", 0)
    writes_7d_count = w7.get("total", 0)
    reads_7d_count = r7.get("total", 0)
    writes_30d_count = w30.get("total", 0)
    reads_30d_count = r30.get("total", 0)
    last_write_at = w.get("last_at")
    last_read_at = r.get("last_at")
    usage_level, offline_days = classify_presence(
        writes_24h=w24.get("total", 0),
        reads_24h=r24.get("total", 0),
        last_write_at=last_write_at,
        last_read_at=last_read_at,
    )

    return {
        "writes_total": writes_total,
        "writes_24h": w24.get("total", 0),
        "writes_7d": writes_7d_count,
        "writes_30d": writes_30d_count,
        "reads_total": reads_total,
        "reads_24h": r24.get("total", 0),
        "reads_7d": reads_7d_count,
        "reads_30d": reads_30d_count,
        "distinct_projects_written": w.get("projects", 0),
        "distinct_projects_read": r.get("projects", 0),
        "distinct_memories_read": r.get("memories", 0),
        "last_write_at": last_write_at,
        "last_read_at": last_read_at,
        "usage_level": usage_level,
        "offline_days": offline_days,
    }


def group_activity_stats(db: Session, group_id: UUID) -> dict:
    """Roll up write/read stats across all members of a group."""
    users = db.query(User).filter(User.group_id == group_id).all()
    user_ids = [user.id for user in users]
    linked_legacy_ids = linked_legacy_ids_for_users(db, user_ids)
    machines = (
        db.query(Machine).filter(Machine.linked_user_id.in_(user_ids)).all()
        if user_ids
        else []
    )
    machine_by_person = {
        machine.linked_user_id: machine.hostname
        for machine in machines
        if machine.linked_user_id is not None
    }
    hostnames = [
        machine_by_person.get(user.id, user.user_id)
        for user in users
        if user.id not in linked_legacy_ids
    ]
    if not hostnames:
        return {
            "member_count": 0,
            "active_members_7d": 0,
            "writes_total": 0,
            "writes_24h": 0,
            "writes_7d": 0,
            "reads_total": 0,
            "reads_24h": 0,
            "reads_7d": 0,
        }

    writes_all = _write_stats_for_hostnames(db, hostnames)
    reads_all = _read_stats_for_hostnames(db, hostnames)
    writes_24h = _write_stats_for_hostnames(db, hostnames, since=_since(hours=24))
    reads_24h = _read_stats_for_hostnames(db, hostnames, since=_since(hours=24))
    writes_7d = _write_stats_for_hostnames(db, hostnames, since=_since(days=7))
    reads_7d = _read_stats_for_hostnames(db, hostnames, since=_since(days=7))

    writes_total = sum(v.get("total", 0) for v in writes_all.values())
    reads_total = sum(v.get("total", 0) for v in reads_all.values())
    writes_24h_total = sum(v.get("total", 0) for v in writes_24h.values())
    reads_24h_total = sum(v.get("total", 0) for v in reads_24h.values())
    writes_7d_total = sum(v.get("total", 0) for v in writes_7d.values())
    reads_7d_total = sum(v.get("total", 0) for v in reads_7d.values())

    active_7d = 0
    for host in hostnames:
        w7 = writes_7d.get(host, {}).get("total", 0)
        r7 = reads_7d.get(host, {}).get("total", 0)
        if w7 > 0 or r7 > 0:
            active_7d += 1

    return {
        "member_count": len(hostnames),
        "active_members_7d": active_7d,
        "writes_total": writes_total,
        "writes_24h": writes_24h_total,
        "writes_7d": writes_7d_total,
        "reads_total": reads_total,
        "reads_24h": reads_24h_total,
        "reads_7d": reads_7d_total,
    }


def recent_user_writes(db: Session, hostname: str, *, limit: int = 20) -> list[dict]:
    variants = legacy_hostname_variants(db, hostname)
    rows = (
        db.query(WriteAuditLog, WriteQueueJob)
        .outerjoin(WriteQueueJob, WriteAuditLog.job_id == WriteQueueJob.id)
        .filter(WriteAuditLog.hostname.in_(variants))
        .order_by(WriteAuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(audit.id),
            "job_id": str(audit.job_id) if audit.job_id else None,
            "project": audit.project,
            "client_name": audit.client_name,
            "action": audit.action,
            "created_at": audit.created_at,
            "text": job.text if job else None,
            "text_preview": _truncate_preview(job.text if job else None),
        }
        for audit, job in rows
    ]


def recent_user_reads(db: Session, hostname: str, *, limit: int = 20) -> list[dict]:
    from app.utils.vector_stats import get_shared_memories_by_ids

    variants: list[str] = []
    for host_variant in legacy_hostname_variants(db, hostname):
        variants.extend(read_audit_hostname_variants(host_variant))
    if not variants:
        return []
    rows = (
        db.query(ReadAuditLog)
        .filter(ReadAuditLog.hostname.in_(variants))
        .order_by(ReadAuditLog.accessed_at.desc())
        .limit(limit)
        .all()
    )
    memory_ids = [str(row.memory_id) for row in rows if row.memory_id]
    memories = get_shared_memories_by_ids(memory_ids)

    items: list[dict] = []
    for row in rows:
        memory = memories.get(str(row.memory_id)) or {}
        text = memory.get("text") or ""
        items.append(
            {
                "id": str(row.id),
                "project": row.project,
                "memory_id": row.memory_id,
                "access_type": row.access_type,
                "source": row.source,
                "client_name": row.client_name,
                "accessed_at": row.accessed_at,
                "memory_preview": _truncate_preview(text),
                "memory_text": text or None,
            }
        )
    return items


def contributor_score(writes: int, reads: int, *, max_writes: int, max_reads: int) -> float:
    """Score 0-100: escritas e leituras normalizadas pelo maior valor do recorte, pesos 70/30."""
    write_part = writes / max_writes if max_writes > 0 else 0.0
    read_part = reads / max_reads if max_reads > 0 else 0.0
    score = CONTRIBUTOR_WRITE_WEIGHT * write_part + CONTRIBUTOR_READ_WEIGHT * read_part
    return round(score * 100, 2)


def top_contributors(
    db: Session,
    *,
    metric: ContributorMetric = "total",
    period: ContributorPeriod = "7d",
    group_id: Optional[UUID] = None,
    project: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Rank visible members by write/read activity."""
    from app.utils.creator_identity import (
        identity_for_hostname,
        resolve_creator_identities_with_db,
    )

    capped_limit = max(1, min(limit, 50))
    since = _period_since(period)
    members = all_visible_members(db, group_id=group_id, real_users_only=True)
    if not members:
        return {
            "metric": metric,
            "period": period,
            "project": project,
            "group_id": str(group_id) if group_id else None,
            "items": [],
        }

    hostnames = [hostname for _, hostname in members]
    member_by_hostname = {hostname: user for user, hostname in members}

    writes = _write_stats_for_hostnames(db, hostnames, since=since, project=project)
    reads = _read_stats_for_hostnames(db, hostnames, since=since, project=project)

    max_writes = max((writes.get(host, {}).get("total", 0) for host in hostnames), default=0)
    max_reads = max((reads.get(host, {}).get("total", 0) for host in hostnames), default=0)

    ranked: list[dict] = []
    for hostname in hostnames:
        write_count = writes.get(hostname, {}).get("total", 0)
        read_count = reads.get(hostname, {}).get("total", 0)
        if metric == "writes":
            value = write_count
        elif metric == "reads":
            value = read_count
        else:
            value = contributor_score(
                write_count, read_count, max_writes=max_writes, max_reads=max_reads
            )
        if value <= 0:
            continue
        write_projects = writes.get(hostname, {}).get("projects", 0)
        read_projects = reads.get(hostname, {}).get("projects", 0)
        distinct_projects = 1 if project else max(write_projects, read_projects)
        ranked.append(
            {
                "user_id": hostname,
                "value": value,
                "writes": write_count,
                "reads": read_count,
                "distinct_projects": distinct_projects,
                "member": member_by_hostname[hostname],
            }
        )

    ranked.sort(key=lambda item: (-item["value"], -item["writes"], item["user_id"].lower()))
    top = ranked[:capped_limit]

    identities = resolve_creator_identities_with_db(db, [item["user_id"] for item in top])
    items: list[dict] = []
    for index, entry in enumerate(top, start=1):
        user = entry["member"]
        hostname = entry["user_id"]
        identity = identity_for_hostname(hostname, identities)
        group_name = user.group.name if user.group else None
        items.append(
            {
                "rank": index,
                "user_id": hostname,
                "display_name": (
                    identity.display_name
                    if identity
                    else user.display_name or user.name or user.email or hostname
                ),
                "avatar_url": identity.avatar_url if identity else user.avatar_url,
                "group_id": str(user.group_id) if user.group_id else None,
                "group_name": group_name,
                "value": entry["value"],
                "writes": entry["writes"],
                "reads": entry["reads"],
                "distinct_projects": entry["distinct_projects"],
            }
        )

    return {
        "metric": metric,
        "period": period,
        "project": project,
        "group_id": str(group_id) if group_id else None,
        "items": items,
    }
