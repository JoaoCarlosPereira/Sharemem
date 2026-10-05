"""Propostas de unificação de projetos (tabela ``project_merge_proposals``).

Transições atômicas (``UPDATE ... WHERE status IN (:esperados)``); ciclo de vida
em ``openmemory/docs/runbooks/project-merge.md``.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.models import ProjectMergeProposal
from app.utils.datetime_format import format_utc_iso
from app.utils.datetime_utc import utc_now_naive

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
PROPOSAL_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_APPLIED,
    STATUS_FAILED,
)
OPEN_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_FAILED)
APPROVABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED)
# Fail fast instead of queueing behind long transactions (PostgreSQL only).
MERGE_LOCK_TIMEOUT = "15s"


class ProposalError(Exception):
    """Transição de estado inválida (mapeada para HTTP 404/409 pelo router)."""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _iso(value) -> Optional[str]:
    return format_utc_iso(value) if value is not None else None


def to_dict(row: ProjectMergeProposal, *, include_undo: bool = False) -> Dict[str, Any]:
    undo = row.undo_info or {}
    data = {
        "id": str(row.id),
        "status": row.status,
        "canonical": row.canonical,
        "aliases": list(row.aliases or []),
        "confidence": float(row.confidence or 0.0),
        "reason": row.reason or "",
        "origin": row.origin,
        "memory_counts": row.memory_counts or {},
        "source_job_id": row.source_job_id,
        "apply_job_id": row.apply_job_id,
        "decided_by": row.decided_by,
        "decided_at": _iso(row.decided_at),
        "decision_note": row.decision_note,
        "applied_at": _iso(row.applied_at),
        "moved_memories": undo.get("moved_memories"),
        "last_error": row.error,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if include_undo:
        data["undo_info"] = row.undo_info
    return data


def _parse_id(proposal_id: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(proposal_id))
    except (TypeError, ValueError):
        return None


def _get_row(db: Session, proposal_id: str) -> Optional[ProjectMergeProposal]:
    pid = _parse_id(proposal_id)
    if pid is None:
        return None
    return db.get(ProjectMergeProposal, pid)


def lock_proposal(db: Session, proposal_id: str) -> Optional[ProjectMergeProposal]:
    """Load the row fresh, ``FOR UPDATE`` on PostgreSQL (no-op on SQLite).

    ``SET LOCAL lock_timeout`` runs first so the row wait is bounded too.
    """
    pid = _parse_id(proposal_id)
    if pid is None:
        return None
    query = db.query(ProjectMergeProposal).filter(ProjectMergeProposal.id == pid)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = '{MERGE_LOCK_TIMEOUT}'"))
        query = query.with_for_update()
    return query.populate_existing().first()


def _group_key(canonical: str, aliases: Iterable[str]) -> frozenset:
    return frozenset([canonical, *aliases])


def list_proposals(
    db: Session,
    *,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    query = db.query(ProjectMergeProposal)
    if status:
        query = query.filter(ProjectMergeProposal.status == status)
    query = query.order_by(ProjectMergeProposal.created_at.desc())
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)
    return [to_dict(r) for r in query.all()]


def count_proposals(db: Session, *, status: Optional[str] = None) -> int:
    query = db.query(ProjectMergeProposal)
    if status:
        query = query.filter(ProjectMergeProposal.status == status)
    return query.count()


def get_proposal(db: Session, proposal_id: str) -> Optional[Dict[str, Any]]:
    row = _get_row(db, proposal_id)
    return to_dict(row, include_undo=True) if row is not None else None


def record_proposals(
    db: Session,
    groups: List[Dict[str, Any]],
    *,
    source_job_id: Optional[str],
    origin: str = "llm",
) -> List[Dict[str, Any]]:
    """Grava novas propostas ``pending`` (idempotente por conjunto de nomes).

    Um grupo cujo conjunto de nomes coincide com uma proposta aberta
    (pending/approved/failed) não é duplicado; só ``memory_counts`` é
    atualizado. Propostas rejeitadas só voltam se a sugestão reaparecer (novo
    id), e o admin mantém a palavra final.
    """
    open_rows = (
        db.query(ProjectMergeProposal)
        .filter(ProjectMergeProposal.status.in_(OPEN_STATUSES))
        .all()
    )
    open_by_key = {_group_key(r.canonical, r.aliases or []): r for r in open_rows}
    created: List[ProjectMergeProposal] = []
    for group in groups:
        key = _group_key(group["canonical"], group["aliases"])
        existing = open_by_key.get(key)
        if existing is not None:
            if group.get("memory_counts"):
                existing.memory_counts = dict(group["memory_counts"])
            continue
        row = ProjectMergeProposal(
            id=uuid.uuid4(),
            status=STATUS_PENDING,
            canonical=group["canonical"],
            aliases=list(group["aliases"]),
            confidence=float(group.get("confidence") or 0.0),
            reason=str(group.get("reason") or ""),
            origin=origin,
            memory_counts=dict(group.get("memory_counts") or {}),
            source_job_id=source_job_id,
            created_at=utc_now_naive(),
            updated_at=utc_now_naive(),
        )
        db.add(row)
        open_by_key[key] = row
        created.append(row)
    db.commit()
    return [to_dict(r) for r in created]


def find_open_proposal(db: Session, canonical: str, aliases: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Open proposal (pending/approved/failed) for exactly this set of names."""
    key = _group_key(canonical, aliases)
    for row in (
        db.query(ProjectMergeProposal)
        .filter(ProjectMergeProposal.status.in_(OPEN_STATUSES))
        .all()
    ):
        if _group_key(row.canonical, row.aliases or []) == key:
            return to_dict(row)
    return None


def transition_proposal(
    db: Session,
    proposal_id: str,
    *,
    expected: str | Iterable[str],
    new_status: str,
    **changes: Any,
) -> Dict[str, Any]:
    """Transição atômica: ``UPDATE ... WHERE id = :id AND status IN (:esperado)``.

    Se nenhuma linha for afetada, outra decisão venceu (ou o id não existe) e
    ``ProposalError`` é levantado — 404 se não existe, 409 caso contrário.
    """
    pid = _parse_id(proposal_id)
    if pid is None:
        raise ProposalError(f"proposal '{proposal_id}' not found", status_code=404)
    expected_set = [expected] if isinstance(expected, str) else list(expected)
    values = {"status": new_status, "updated_at": utc_now_naive(), **changes}
    result = db.execute(
        update(ProjectMergeProposal)
        .where(ProjectMergeProposal.id == pid)
        .where(ProjectMergeProposal.status.in_(expected_set))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        row = db.get(ProjectMergeProposal, pid)
        if row is None:
            raise ProposalError(f"proposal '{proposal_id}' not found", status_code=404)
        raise ProposalError(
            f"proposal '{proposal_id}' is '{row.status}', expected one of {expected_set}"
        )
    db.commit()
    row = db.get(ProjectMergeProposal, pid)
    db.refresh(row)
    return to_dict(row)


def proposals_mentioning(db: Session, name: str) -> List[Dict[str, Any]]:
    """Propostas abertas (pending/approved/failed) cujo canonical ou aliases citam ``name``."""
    rows = (
        db.query(ProjectMergeProposal)
        .filter(ProjectMergeProposal.status.in_(OPEN_STATUSES))
        .all()
    )
    return [
        to_dict(r) for r in rows if r.canonical == name or name in (r.aliases or [])
    ]
