"""Detect and merge duplicate MCP projects misclassified by LLM routing.

Merges are proposals until an admin approves them. Rules, consistency contract
and operations: ``openmemory/docs/runbooks/project-merge.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.database import SessionLocal, is_postgresql
from app.governance.merge_proposals import (
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_PENDING,
    MERGE_LOCK_TIMEOUT,
    ProposalError,
    count_proposals,
    get_proposal,
    lock_proposal,
    proposals_mentioning,
    record_proposals,
    transition_proposal,
)
from app.models import (
    GovernanceJob,
    GovernancePolicy,
    GovernanceSchedule,
    PartitionTier,
    Project,
    SpecWorkspace,
    TokenUsageLog,
    WriteAuditLog,
    WriteQueueJob,
    WriteQueueStatus,
)
from app.read_audit_log_model import ReadAuditLog
from app.utils.datetime_format import format_utc_iso
from app.utils.datetime_utc import utc_now_naive
from app.utils.governance_policy import is_process_enabled, resolve_policy
from app.utils.metrics import (
    PROJECT_MERGE_COMPENSATION_FAILURES,
    PROJECT_MERGE_INCONSISTENT_PROJECTS,
    PROJECT_MERGE_PENDING_PROPOSALS,
)
from app.utils.read_cache import read_cache
from app.utils.vector_stats import (
    VectorStoreUnavailable,
    count_project_memories,
    count_project_memories_strict,
    facet_project_counts,
    list_shared_memories,
)

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_SIZE = 5
DEFAULT_CONFIDENCE_THRESHOLD = 0.85
DEFAULT_BATCH_SIZE = 128


@dataclass
class ProjectProfile:
    name: str
    memory_count: int
    first_seen_hostname: Optional[str]
    samples: List[str]


@dataclass
class MergeGroup:
    canonical: str
    aliases: List[str]
    confidence: float
    reason: str


def collect_project_profiles(
    db: Session,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> List[ProjectProfile]:
    profiles: List[ProjectProfile] = []
    for project in db.query(Project).order_by(Project.name).all():
        count = count_project_memories(project.name)
        if count <= 0:
            continue
        try:
            listing = list_shared_memories(project=project.name, page=1, size=sample_size)
            samples = [
                (item.get("content") or "")[:240]
                for item in listing.get("items", [])
                if (item.get("content") or "").strip()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to sample memories for %s: %s", project.name, exc)
            samples = []
        profiles.append(
            ProjectProfile(
                name=project.name,
                memory_count=count,
                first_seen_hostname=project.first_seen_hostname,
                samples=samples,
            )
        )
    return profiles


def _parse_merge_groups(raw: Any, *, profiles: List[ProjectProfile]) -> List[MergeGroup]:
    known = {p.name for p in profiles}
    groups: List[MergeGroup] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip()
        aliases = [
            str(a).strip()
            for a in (item.get("aliases") or [])
            if str(a).strip() and str(a).strip() != canonical
        ]
        if not canonical or not aliases:
            continue
        if canonical not in known:
            continue
        aliases = [a for a in aliases if a in known and a != canonical]
        if not aliases:
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(item.get("reason") or "").strip()
        groups.append(
            MergeGroup(
                canonical=canonical,
                aliases=aliases,
                confidence=confidence,
                reason=reason,
            )
        )
    return groups


def detect_duplicate_groups_with_llm(
    profiles: List[ProjectProfile],
    llm_client,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[MergeGroup]:
    """Ask the LLM which catalog projects are the same real-world workspace."""
    if llm_client is None or len(profiles) < 2:
        return []

    catalog = [
        {
            "name": p.name,
            "memory_count": p.memory_count,
            "first_seen_hostname": p.first_seen_hostname,
            "sample_memories": p.samples,
        }
        for p in profiles
    ]
    prompt = (
        "You are a governance assistant for a multi-project memory system. "
        "Different project IDs sometimes refer to the SAME real software product, "
        "repository, or team workspace because an LLM or hostname heuristic "
        "misclassified writes (e.g. sysmovs, dsv-delphi-sysmovs, sysmovs-delphi).\n\n"
        "Given the project catalog JSON below, group projects that clearly represent "
        "the same workspace. Pick one canonical name per group (prefer the shortest, "
        "most general, or highest memory_count name). Only group when evidence is strong.\n\n"
        f"Catalog:\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        "Respond with JSON only:\n"
        '{"groups":[{"canonical":"name","aliases":["other"],"confidence":0.0,"reason":"..."}]}'
    )
    try:
        response = llm_client.generate_response(
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        
        cleaned_response = response.strip()
        if cleaned_response.startswith("```"):
            cleaned_response = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned_response)
            cleaned_response = re.sub(r"\n?```$", "", cleaned_response)
            cleaned_response = cleaned_response.strip()
            
        data = json.loads(cleaned_response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM project-merge detection failed: %s", exc)
        return []

    groups = _parse_merge_groups(data.get("groups"), profiles=profiles)
    return [g for g in groups if g.confidence >= confidence_threshold]


DEFAULT_PROJECT_NAME = "default"
TASK_PROJECT_PREFIX = "tarefa-"
_NUMERIC_PROJECT_RE = re.compile(r"^\d+$")
_ADVISORY_LOCK_NAMESPACE = "project_merge:"


class MergeRuleViolation(ValueError):
    """Deterministic refusal; retrying can't fix it (proposal goes ``failed``)."""


class ProjectMergeError(RuntimeError):
    """Merge aborted after touching Qdrant; carries compensation outcome."""

    def __init__(self, message: str, *, compensation_failed_ids: Optional[List[str]] = None):
        super().__init__(message)
        self.compensation_failed_ids = list(compensation_failed_ids or [])


def merge_block_reason(name: str) -> Optional[str]:
    """Why ``name`` can never be merged/renamed (``default``, ``tarefa-*``, numeric)."""
    normalized = (name or "").strip().lower()
    if not normalized:
        return "empty"
    if normalized == DEFAULT_PROJECT_NAME:
        return "default"
    if normalized.startswith(TASK_PROJECT_PREFIX):
        return "tarefa"
    if _NUMERIC_PROJECT_RE.match(normalized):
        return "numeric"
    return None


def assert_merge_allowed(canonical: str, aliases: List[str]) -> None:
    blocked = {
        name: reason
        for name in [canonical, *aliases]
        if (reason := merge_block_reason(name)) is not None
    }
    if blocked:
        detail = ", ".join(f"{name} ({reason})" for name, reason in blocked.items())
        raise MergeRuleViolation(f"merge proibido para projetos protegidos: {detail}")


def filter_group_by_rules(group: MergeGroup) -> Optional[MergeGroup]:
    """Drop protected names from an LLM group; None when <2 names remain."""
    names = [n for n in [group.canonical, *group.aliases] if merge_block_reason(n) is None]
    names = list(dict.fromkeys(names))
    if len(names) < 2:
        return None
    canonical = group.canonical if group.canonical in names else names[0]
    return MergeGroup(
        canonical=canonical,
        aliases=[n for n in names if n != canonical],
        confidence=group.confidence,
        reason=group.reason,
    )


def is_merge_process_enabled(db: Session) -> bool:
    """Whether ``merge_projects`` is enabled in the global governance policy."""
    policy = resolve_policy("", session_factory=lambda: db)
    return is_process_enabled(policy, "merge_projects")


def project_exists(
    db: Session,
    name: str,
    *,
    count_fn: Optional[Callable[[str], int]] = None,
) -> bool:
    """In the catalog OR with points in Qdrant (strict count: errors raise)."""
    if db.query(Project).filter(Project.name == name).first() is not None:
        return True
    return (count_fn or count_project_memories_strict)(name) > 0


def lock_projects(db: Session, names: Iterable[str]) -> None:
    """Serialize merges sharing projects (PostgreSQL; no-op on SQLite).

    Advisory xact lock per name + ``FOR UPDATE`` on ``projects`` rows, in
    alphabetical order (no deadlocks), under a local ``lock_timeout``.
    """
    ordered = sorted({n for n in names if n})
    if not ordered or not is_postgresql(str(db.get_bind().url)):
        return
    db.execute(text(f"SET LOCAL lock_timeout = '{MERGE_LOCK_TIMEOUT}'"))
    for name in ordered:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{_ADVISORY_LOCK_NAMESPACE}{name}"},
        )
    (
        db.query(Project)
        .filter(Project.name.in_(ordered))
        .order_by(Project.name)
        .with_for_update()
        .all()
    )


def relocate_project_memories(
    vs,
    *,
    source: str,
    target: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    moved_log: Optional[List[Tuple[str, str]]] = None,
) -> int:
    """Set ``payload.project = target`` (partial update) on every source point.

    Moved ids are appended to ``moved_log`` as ``(point_id, original_project)``.
    """
    filt = vs._create_filter({"project": source})
    offset = None
    moved = 0
    while True:
        records, offset = vs.client.scroll(
            collection_name=vs.collection_name,
            scroll_filter=filt,
            offset=offset,
            limit=batch_size,
            with_payload=False,
            with_vectors=False,
        )
        for rec in records or []:
            vs.update(str(rec.id), payload={"project": target})
            if moved_log is not None:
                moved_log.append((str(rec.id), source))
            moved += 1
        if offset is None:
            break
    return moved


def revert_relocated_memories(
    vs,
    moved_log: List[Tuple[str, str]],
    *,
    job_id: str,
) -> List[str]:
    """Best-effort: restore payload.project of moved points. Returns failed ids."""
    failed: List[str] = []
    for point_id, original in reversed(moved_log):
        try:
            vs.update(point_id, payload={"project": original})
        except Exception:  # noqa: BLE001
            failed.append(point_id)
            logger.exception(
                "project merge compensation failed for point %s (restore project=%s, job=%s)",
                point_id,
                original,
                job_id,
            )
    if failed:
        PROJECT_MERGE_COMPENSATION_FAILURES.inc(len(failed))
        logger.error(
            "project merge compensation INCOMPLETE (job=%s): %s/%s points still point "
            "to the merge target; ids=%s originals=%s",
            job_id,
            len(failed),
            len(moved_log),
            failed,
            {pid: orig for pid, orig in moved_log if pid in set(failed)},
        )
    elif moved_log:
        logger.warning(
            "project merge compensated (job=%s): %s points restored to original project",
            job_id,
            len(moved_log),
        )
    return failed


def _merge_governance_schedules(db: Session, *, canonical: str, alias: str) -> List[str]:
    """Merge alias schedule rows into canonical without PK collisions."""
    job_types: List[str] = []
    alias_rows = db.query(GovernanceSchedule).filter(GovernanceSchedule.scope == alias).all()
    for alias_row in alias_rows:
        job_types.append(alias_row.job_type.value)
        canonical_row = (
            db.query(GovernanceSchedule)
            .filter(
                GovernanceSchedule.job_type == alias_row.job_type,
                GovernanceSchedule.scope == canonical,
            )
            .first()
        )
        if canonical_row is None:
            alias_row.scope = canonical
            continue
        alias_ts = alias_row.last_run_at
        canonical_ts = canonical_row.last_run_at
        if alias_ts is not None and (canonical_ts is None or alias_ts > canonical_ts):
            canonical_row.last_run_at = alias_ts
        db.delete(alias_row)
    return job_types


def _assert_no_spec_slug_conflict(db: Session, *, canonical: str, alias: str) -> None:
    """``uq_spec_workspace_project_slug``: alias and canonical can't share a slug."""
    alias_slugs = {
        slug
        for (slug,) in db.query(SpecWorkspace.slug).filter(SpecWorkspace.project_id == alias)
    }
    if not alias_slugs:
        return
    clash = [
        slug
        for (slug,) in db.query(SpecWorkspace.slug).filter(
            SpecWorkspace.project_id == canonical,
            SpecWorkspace.slug.in_(alias_slugs),
        )
    ]
    if clash:
        raise MergeRuleViolation(
            f"merge {alias} -> {canonical} abortado: spec workspaces com slug "
            f"duplicado {sorted(clash)}"
        )


def _ids(db: Session, column, filter_column, value) -> List[str]:
    return [str(v) for (v,) in db.query(column).filter(filter_column == value)]


def _project_snapshot(row: Optional[Project]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    snapshot: Dict[str, Any] = {}
    for col in Project.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, PartitionTier):
            value = value.value
        elif hasattr(value, "isoformat"):
            value = format_utc_iso(value)
        snapshot[col.name] = value
    return snapshot


def _merge_sql_references(db: Session, *, canonical: str, alias: str) -> Dict[str, Any]:
    """Repoint every SQL reference from ``alias`` to ``canonical``; returns undo info.

    FK children are repointed and flushed before the alias ``projects`` row is
    deleted (Postgres checks FKs immediately).
    """
    _assert_no_spec_slug_conflict(db, canonical=canonical, alias=alias)
    alias_row = db.query(Project).filter(Project.name == alias).first()
    undo: Dict[str, Any] = {
        "alias": alias,
        "project_row": _project_snapshot(alias_row),
        "write_queue_ids": _ids(db, WriteQueueJob.id, WriteQueueJob.project, alias),
        "write_audit_log_ids": _ids(db, WriteAuditLog.id, WriteAuditLog.project, alias),
        "governance_job_ids": _ids(db, GovernanceJob.id, GovernanceJob.project, alias),
        "spec_workspace_ids": _ids(db, SpecWorkspace.id, SpecWorkspace.project_id, alias),
        "read_audit_logs": db.query(ReadAuditLog).filter(ReadAuditLog.project == alias).count(),
        "token_usage_logs": db.query(TokenUsageLog)
        .filter(TokenUsageLog.project == alias)
        .count(),
        "governance_policy_overrides": None,
    }
    for model, column in (
        (WriteQueueJob, WriteQueueJob.project),
        (WriteAuditLog, WriteAuditLog.project),
        (GovernanceJob, GovernanceJob.project),
        (ReadAuditLog, ReadAuditLog.project),
        (TokenUsageLog, TokenUsageLog.project),
        (SpecWorkspace, SpecWorkspace.project_id),
    ):
        db.query(model).filter(column == alias).update(
            {column: canonical},
            synchronize_session=False,
        )
    undo["governance_schedule_job_types"] = _merge_governance_schedules(
        db, canonical=canonical, alias=alias
    )

    alias_policy = (
        db.query(GovernancePolicy).filter(GovernancePolicy.project_name == alias).first()
    )
    if alias_policy is not None:
        undo["governance_policy_overrides"] = dict(alias_policy.overrides or {})
        canonical_policy = (
            db.query(GovernancePolicy)
            .filter(GovernancePolicy.project_name == canonical)
            .first()
        )
        if canonical_policy is None:
            alias_policy.project_name = canonical
        else:
            merged = {**(canonical_policy.overrides or {}), **(alias_policy.overrides or {})}
            canonical_policy.overrides = merged
            db.delete(alias_policy)

    # Children first, then the parent row (no ORM relationship orders this for us).
    db.flush()
    if alias_row is not None:
        db.delete(alias_row)
    return undo


def _ensure_canonical_row(db: Session, canonical: str) -> Project:
    """Create the canonical row in the caller's transaction (no commit)."""
    row = db.query(Project).filter(Project.name == canonical).first()
    if row is None:
        row = Project(name=canonical, last_activity_at=utc_now_naive())
        db.add(row)
        db.flush()
    return row


def apply_project_merge(
    db: Session,
    vs,
    *,
    canonical: str,
    aliases: List[str],
    job_id: str,
    require_existing: bool = False,
    require_new_canonical: bool = False,
    count_fn: Optional[Callable[[str], int]] = None,
    finalize: Optional[Callable[[int, Dict[str, Any]], None]] = None,
    commit_landed: Optional[Callable[[Session], bool]] = None,
) -> int:
    """Move SQL references, then Qdrant points, from aliases into canonical.

    - ``require_existing``: canonical and >=1 alias must exist (approved proposals).
    - ``require_new_canonical``: plain rename; target must still be absent.
    - ``finalize(moved, undo_info)`` runs in the transaction right before commit.
    - ``commit_landed(fresh_session)`` decides, after an ambiguous commit error,
      whether the commit actually happened (default: alias rows are gone).

    SQL-first with Qdrant compensation; see the runbook for the full contract.
    """
    aliases = [a for a in dict.fromkeys(aliases) if a and a != canonical]
    assert_merge_allowed(canonical, aliases)

    moved_log: List[Tuple[str, str]] = []
    undo_info: Dict[str, Any] = {
        "canonical": canonical,
        "job_id": job_id,
        "aliases": [],
        "skipped_aliases": [],
    }
    try:
        lock_projects(db, [canonical, *aliases])
        if require_new_canonical and (
            db.query(Project).filter(Project.name == canonical).first() is not None
        ):
            raise MergeRuleViolation(
                f"destino '{canonical}' passou a existir; a fusão exige proposta aprovada"
            )
        if require_existing:
            if not project_exists(db, canonical, count_fn=count_fn):
                raise MergeRuleViolation(f"projeto canônico '{canonical}' não existe")
            present = [a for a in aliases if project_exists(db, a, count_fn=count_fn)]
            if not present:
                raise MergeRuleViolation(
                    f"nenhum alias de {aliases} existe; nada a unificar em '{canonical}'"
                )
            undo_info["skipped_aliases"] = [a for a in aliases if a not in present]
            aliases = present

        canonical_row = _ensure_canonical_row(db, canonical)
        eligible: List[str] = []
        per_alias: Dict[str, Dict[str, Any]] = {}
        for alias in aliases:
            alias_row = db.query(Project).filter(Project.name == alias).first()
            if alias_row is not None and (
                alias_row.partition_tier != PartitionTier.shared
                or canonical_row.partition_tier != PartitionTier.shared
            ):
                logger.warning(
                    "skip project merge %s -> %s: dedicated partition not supported",
                    alias,
                    canonical,
                )
                undo_info["skipped_aliases"].append(alias)
                continue
            per_alias[alias] = _merge_sql_references(db, canonical=canonical, alias=alias)
            eligible.append(alias)
        if not eligible:
            raise MergeRuleViolation(
                f"nenhum alias elegível para unificar em '{canonical}' "
                f"(ignorados: {undo_info['skipped_aliases']})"
            )
        catalog_aliases = [a for a in eligible if per_alias[a]["project_row"] is not None]

        canonical_row.last_activity_at = utc_now_naive()
        # Surface FK / unique violations now, while Qdrant is still untouched.
        db.flush()
    except Exception:
        db.rollback()
        raise

    moved_total = 0
    try:
        for alias in eligible:
            start = len(moved_log)
            moved = relocate_project_memories(
                vs, source=alias, target=canonical, moved_log=moved_log
            )
            moved_total += moved
            per_alias[alias]["memory_count"] = moved
            per_alias[alias]["qdrant_point_ids"] = [pid for pid, _ in moved_log[start:]]
            logger.info(
                "merged project %s into %s (%s memories, job=%s)",
                alias,
                canonical,
                moved,
                job_id,
            )
        undo_info["aliases"] = [per_alias[a] for a in eligible]
        undo_info["moved_memories"] = moved_total
        if finalize is not None:
            finalize(moved_total, undo_info)
    except Exception as exc:
        _rollback_and_compensate(db, vs, moved_log, job_id, aliases, canonical, exc)

    try:
        db.commit()
    except Exception as exc:
        if not _is_ambiguous_commit_error(exc):
            _rollback_and_compensate(db, vs, moved_log, job_id, aliases, canonical, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("rollback after failed commit raised", exc_info=True)
        check = commit_landed or (
            lambda s: _rename_or_merge_landed(
                s, catalog_aliases, canonical if require_new_canonical else None
            )
        )
        try:
            landed = _check_in_fresh_session(db, check)
        except Exception as verify_exc:  # noqa: BLE001
            logger.error(
                "project merge %s -> %s (job=%s): ambiguous commit and verification failed "
                "(%s); NOT compensating — run the merge-inconsistencies report",
                aliases,
                canonical,
                job_id,
                verify_exc,
            )
            raise ProjectMergeError(
                f"commit ambíguo no merge {aliases} -> {canonical}; Qdrant não compensado"
            ) from exc
        if not landed:
            _rollback_and_compensate(db, vs, moved_log, job_id, aliases, canonical, exc)
        logger.warning(
            "project merge %s -> %s (job=%s): commit error but the commit landed; "
            "keeping Qdrant as is",
            aliases,
            canonical,
            job_id,
        )

    for alias in eligible:
        read_cache.invalidate_search(alias)
    read_cache.invalidate_search(canonical)
    return moved_total


def _is_ambiguous_commit_error(exc: Exception) -> bool:
    """Connection lost during COMMIT: the server may or may not have committed."""
    return isinstance(exc, OperationalError) or (
        isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)
    )


def _check_in_fresh_session(db: Session, check: Callable[[Session], bool]) -> bool:
    fresh = Session(bind=db.get_bind())
    try:
        return bool(check(fresh))
    finally:
        fresh.close()


def _rename_or_merge_landed(
    db: Session, aliases: List[str], new_canonical: Optional[str]
) -> bool:
    """Landed iff the deleted alias rows are gone (or the new rename target exists)."""
    if aliases:
        return db.query(Project).filter(Project.name.in_(aliases)).count() == 0
    if new_canonical:
        return db.query(Project).filter(Project.name == new_canonical).count() > 0
    return False


def _rollback_and_compensate(db, vs, moved_log, job_id, aliases, canonical, exc):
    db.rollback()
    failed = revert_relocated_memories(vs, moved_log, job_id=job_id)
    message = f"project merge {aliases} -> {canonical} failed: {exc}"
    if failed:
        message += f" (compensation failed for {len(failed)} points; see logs)"
    raise ProjectMergeError(message, compensation_failed_ids=failed) from exc


def detect_merge_inconsistencies(
    db: Session,
    *,
    count_fn: Optional[Callable[[str], int]] = None,
    facet_fn: Optional[Callable[[], Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """Read-only reconciliation report (never fixes anything; see runbook).

    ``items`` = catalog projects with 0 points but SQL references,
    ``orphan_qdrant_projects`` = ``payload.project`` values missing from the
    catalog (one facet call), ``qdrant_errors`` = counts that failed (never 0).
    """
    facet: Optional[Dict[str, int]] = None
    orphan_scan = "skipped"
    if facet_fn is not None or count_fn is None:
        try:
            facet = (facet_fn or facet_project_counts)()
            orphan_scan = "ok"
        except VectorStoreUnavailable as exc:
            logger.warning("project facet unavailable; falling back to per-project count: %s", exc)
            orphan_scan = "unavailable"
    if count_fn is None:
        count_fn = (lambda n: facet.get(n, 0)) if facet is not None else count_project_memories_strict

    catalog = db.query(Project).order_by(Project.name).all()
    catalog_names = {p.name for p in catalog}
    items: List[Dict[str, Any]] = []
    qdrant_errors: List[Dict[str, str]] = []
    for project in catalog:
        name = project.name
        try:
            if count_fn(name) > 0:
                continue
        except VectorStoreUnavailable as exc:
            qdrant_errors.append({"project": name, "error": str(exc)[:500]})
            continue
        refs = {
            "write_queue_done": db.query(WriteQueueJob)
            .filter(WriteQueueJob.project == name, WriteQueueJob.status == WriteQueueStatus.done)
            .count(),
            "write_audit_logs": db.query(WriteAuditLog)
            .filter(WriteAuditLog.project == name)
            .count(),
            "read_audit_logs": db.query(ReadAuditLog).filter(ReadAuditLog.project == name).count(),
            "spec_workspaces": db.query(SpecWorkspace)
            .filter(SpecWorkspace.project_id == name)
            .count(),
        }
        if not any(refs.values()):
            continue
        related = [
            {"id": p.get("id"), "status": p.get("status"), "canonical": p.get("canonical")}
            for p in proposals_mentioning(db, name)
        ]
        items.append(
            {
                "project": name,
                "qdrant_points": 0,
                "sql_references": refs,
                "related_proposals": related,
                "suspected_half_merge": bool(refs["write_queue_done"]) or bool(related),
            }
        )

    orphans = [
        {"project": name, "qdrant_points": count}
        for name, count in sorted((facet or {}).items())
        if count > 0 and name not in catalog_names
    ]
    PROJECT_MERGE_INCONSISTENT_PROJECTS.set(len(items) + len(orphans))
    for item in items:
        logger.warning(
            "project %s has 0 Qdrant points but SQL references %s (possible half-applied "
            "merge; not auto-finalized)",
            item["project"],
            item["sql_references"],
        )
    for orphan in orphans:
        logger.warning(
            "Qdrant payload.project=%s (%s points) has no catalog row",
            orphan["project"],
            orphan["qdrant_points"],
        )
    return {
        "items": items,
        "orphan_qdrant_projects": orphans,
        "orphan_scan": orphan_scan,
        "qdrant_errors": qdrant_errors,
    }


def _group_to_dict(group: MergeGroup) -> Dict[str, Any]:
    return {
        "canonical": group.canonical,
        "aliases": list(group.aliases),
        "confidence": group.confidence,
        "reason": group.reason,
        "memory_counts": {
            name: count_project_memories(name) for name in [group.canonical, *group.aliases]
        },
    }


def _apply_rules(groups: List[MergeGroup]) -> List[MergeGroup]:
    allowed: List[MergeGroup] = []
    for group in groups:
        filtered = filter_group_by_rules(group)
        if filtered is None:
            logger.info(
                "project merge group dropped by protection rules: %s <- %s",
                group.canonical,
                group.aliases,
            )
            continue
        allowed.append(filtered)
    return allowed


def preview_project_merges(
    *,
    session_factory=SessionLocal,
    memory_client_provider: Optional[Callable] = None,
    llm_provider: Optional[Callable] = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[Dict[str, Any]]:
    if memory_client_provider is None:
        from app.utils.memory import get_memory_client_safe

        memory_client_provider = get_memory_client_safe

    client = memory_client_provider()
    if client is None:
        return []

    llm = llm_provider() if llm_provider else getattr(client, "llm", None)
    db = session_factory()
    try:
        profiles = collect_project_profiles(db)
        groups = detect_duplicate_groups_with_llm(
            profiles,
            llm,
            confidence_threshold=confidence_threshold,
        )
        return [_group_to_dict(g) for g in _apply_rules(groups)]
    finally:
        db.close()


def _refresh_pending_gauge(db: Session) -> None:
    try:
        PROJECT_MERGE_PENDING_PROPOSALS.set(count_proposals(db, status=STATUS_PENDING))
    except Exception:  # noqa: BLE001
        logger.debug("failed to refresh pending merge proposals gauge", exc_info=True)


def _mark_proposal_failed(db: Session, proposal_id: str, exc: Exception) -> None:
    try:
        transition_proposal(
            db,
            proposal_id,
            expected=(STATUS_APPROVED, STATUS_FAILED),
            new_status=STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
    except ProposalError as err:
        logger.warning("could not mark merge proposal %s failed: %s", proposal_id, err)


def apply_approved_proposal(
    db: Session,
    vs,
    *,
    proposal_id: str,
    job_id: str,
    count_fn: Optional[Callable[[str], int]] = None,
) -> int:
    """Apply one approved proposal; ``applied`` commits with the SQL changes.

    :class:`MergeRuleViolation` → ``failed`` and returns 0 (no retry); other
    errors → ``failed`` and re-raised (worker retry resumes this job).
    """
    row = lock_proposal(db, proposal_id)
    if row is None:
        raise ProposalError(f"proposal '{proposal_id}' not found", status_code=404)
    status = row.status
    if status == STATUS_APPLIED:
        logger.info("merge proposal %s already applied (job=%s)", proposal_id, job_id)
        return int((row.undo_info or {}).get("moved_memories") or 0)
    if status == STATUS_FAILED and row.apply_job_id == job_id:
        row.status = STATUS_APPROVED
    elif status != STATUS_APPROVED:
        # rejected / pending / failed-and-superseded: nothing to do, job ends done.
        logger.info(
            "merge proposal %s is '%s'; skipping (job=%s)", proposal_id, status, job_id
        )
        db.rollback()
        return 0

    canonical = row.canonical
    aliases = list(row.aliases or [])

    def _landed(fresh: Session) -> bool:
        stored = get_proposal(fresh, proposal_id)
        return bool(
            stored and stored["status"] == STATUS_APPLIED and stored["apply_job_id"] == job_id
        )

    def _finalize(moved: int, undo_info: Dict[str, Any]) -> None:
        row.status = STATUS_APPLIED
        row.applied_at = utc_now_naive()
        row.apply_job_id = job_id
        row.undo_info = undo_info
        row.error = None
        row.updated_at = utc_now_naive()

    try:
        moved = apply_project_merge(
            db,
            vs,
            canonical=canonical,
            aliases=aliases,
            job_id=job_id,
            require_existing=True,
            count_fn=count_fn or (lambda name: count_project_memories_strict(name, vs)),
            finalize=_finalize,
            commit_landed=_landed,
        )
    except MergeRuleViolation as exc:
        db.rollback()
        logger.warning("merge proposal %s refused (job=%s): %s", proposal_id, job_id, exc)
        _mark_proposal_failed(db, proposal_id, exc)
        _refresh_pending_gauge(db)
        return 0
    except Exception as exc:
        db.rollback()
        _mark_proposal_failed(db, proposal_id, exc)
        raise
    _refresh_pending_gauge(db)
    return moved


def run_merge_projects_job(
    *,
    project: Optional[str],
    job_id: str,
    limit: int = 500,
    session_factory=SessionLocal,
    memory_client_provider: Optional[Callable] = None,
    llm_provider: Optional[Callable] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """Governance handler for ``merge_projects``.

    ``payload.proposal_id`` applies that approved proposal (the only mutating
    path); otherwise manual ``groups`` or LLM detection record pending proposals.
    """
    if memory_client_provider is None:
        from app.utils.memory import get_memory_client_safe

        memory_client_provider = get_memory_client_safe

    db = session_factory()
    try:
        if payload is None:
            job = db.query(GovernanceJob).filter(GovernanceJob.id == UUID(str(job_id))).first()
            payload = dict(job.payload or {}) if job else {}
        else:
            payload = dict(payload)

        proposal_id = payload.get("proposal_id")
        if proposal_id:
            client = memory_client_provider()
            if client is None:
                raise RuntimeError("memory client unavailable")
            moved = apply_approved_proposal(
                db, client.vector_store, proposal_id=str(proposal_id), job_id=job_id
            )
            logger.info("merge proposal %s job %s moved %s memories", proposal_id, job_id, moved)
            stored = get_proposal(db, str(proposal_id))
            return 1 if stored and stored["status"] == STATUS_APPLIED else 0

        dry_run = bool(payload.get("dry_run"))
        confidence_threshold = float(
            payload.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
        )
        manual_groups = payload.get("groups")

        if manual_groups:
            groups = _parse_merge_groups(
                manual_groups,
                profiles=[
                    ProjectProfile(
                        name=row.name,
                        memory_count=0,
                        first_seen_hostname=row.first_seen_hostname,
                        samples=[],
                    )
                    for row in db.query(Project).all()
                ],
            )
        else:
            client = memory_client_provider()
            if client is None:
                raise RuntimeError("memory client unavailable")
            llm = llm_provider() if llm_provider else getattr(client, "llm", None)
            profiles = collect_project_profiles(db)
            if project:
                profiles = [p for p in profiles if p.name == project or project in p.name]
            groups = detect_duplicate_groups_with_llm(
                profiles,
                llm,
                confidence_threshold=confidence_threshold,
            )

        groups = _apply_rules(groups)[: max(1, limit)]
        if dry_run:
            return len(groups)

        created = record_proposals(
            db,
            [_group_to_dict(g) for g in groups],
            source_job_id=job_id,
            origin="manual" if manual_groups else "llm",
        )
        for proposal in created:
            logger.info(
                "project merge proposal %s pending admin approval: %s <- %s (job=%s)",
                proposal["id"],
                proposal["canonical"],
                proposal["aliases"],
                job_id,
            )
        _refresh_pending_gauge(db)
        if not manual_groups:
            try:
                detect_merge_inconsistencies(db)
            except Exception:  # noqa: BLE001
                logger.exception("project merge reconciliation scan failed (job=%s)", job_id)
        return len(created)
    finally:
        db.close()
