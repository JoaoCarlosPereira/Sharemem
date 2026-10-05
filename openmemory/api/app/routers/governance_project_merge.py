"""Governance endpoints for LLM-assisted duplicate project merge."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.governance.merge_proposals import (
    APPROVABLE_STATUSES,
    PROPOSAL_STATUSES,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ProposalError,
    count_proposals,
    get_proposal,
    list_proposals,
    transition_proposal,
)
from app.governance.project_merge import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    MergeRuleViolation,
    assert_merge_allowed,
    detect_merge_inconsistencies,
    is_merge_process_enabled,
    preview_project_merges,
    run_merge_projects_job,
)
from app.utils.admin_auth import require_admin
from app.utils.datetime_utc import utc_now_naive
from app.utils.governance_queue import governance_queue
from app.utils.logging_context import auth_email_var, auth_method_var

router = APIRouter(prefix="/admin/governance", tags=["governance"])


class MergeGroupSpec(BaseModel):
    canonical: str
    aliases: List[str] = Field(default_factory=list)
    confidence: float = 1.0
    reason: str = "manual"


class MergeProjectsRequest(BaseModel):
    dry_run: bool = False
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    groups: Optional[List[MergeGroupSpec]] = None


def _assert_merge_enabled(db: Session) -> None:
    if not is_merge_process_enabled(db):
        raise HTTPException(
            status_code=409,
            detail="processo de governança 'merge_projects' está desabilitado",
        )


@router.get("/projects/merge-preview")
def merge_projects_preview(
    confidence_threshold: float = Query(DEFAULT_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    """Return LLM-suggested duplicate project groups without applying merges."""
    _assert_merge_enabled(db)
    try:
        groups = preview_project_merges(confidence_threshold=confidence_threshold)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"groups": groups, "count": len(groups)}


@router.post("/projects/merge", status_code=202)
def enqueue_merge_projects(
    body: MergeProjectsRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """Enqueue a job that records merge *proposals* (never applies them)."""
    _assert_merge_enabled(db)
    payload: Dict[str, Any] = {
        "manual": True,
        "dry_run": body.dry_run,
        "confidence_threshold": body.confidence_threshold,
        "limit": 50,
    }
    if body.groups is not None:
        payload["groups"] = [g.model_dump() for g in body.groups]

    job_id = governance_queue.enqueue("merge_projects", payload=payload)
    return {
        "job_id": job_id,
        "job_type": "merge_projects",
        "status": "queued",
        "dry_run": body.dry_run,
    }


@router.post("/projects/merge-now")
def merge_projects_now(
    body: MergeProjectsRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """Detect/record merge proposals synchronously (no data is moved)."""
    _assert_merge_enabled(db)
    payload: Dict[str, Any] = {
        "dry_run": body.dry_run,
        "confidence_threshold": body.confidence_threshold,
    }
    if body.groups is not None:
        payload["groups"] = [g.model_dump() for g in body.groups]

    try:
        actions = run_merge_projects_job(
            project=None,
            job_id="manual-sync",
            limit=50,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    preview: List[Dict[str, Any]] = []
    if body.dry_run and body.groups is None:
        preview = preview_project_merges(confidence_threshold=body.confidence_threshold)

    return {
        "actions": actions,
        "proposals_recorded": 0 if body.dry_run else actions,
        "dry_run": body.dry_run,
        "groups": preview,
    }


class ProposalDecision(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


def _decided_by() -> str:
    email = (auth_email_var.get() or "").strip()
    if email:
        return email
    return auth_method_var.get() or "admin_token"


def _proposal_http_error(exc: ProposalError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/projects/merge-proposals")
def list_merge_proposals(
    status: Optional[str] = Query(
        None, description="pending|approved|rejected|applied|failed"
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """List merge proposals, newest first (full history, paginated)."""
    if status is not None and status not in PROPOSAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status inválido: {status}")
    items = list_proposals(db, status=status, limit=limit, offset=offset)
    return {"items": items, "count": count_proposals(db, status=status)}


@router.get("/projects/merge-proposals/{proposal_id}")
def get_merge_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    proposal = get_proposal(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal '{proposal_id}' not found")
    return proposal


@router.post("/projects/merge-proposals/{proposal_id}/approve", status_code=202)
def approve_merge_proposal(
    proposal_id: str,
    body: Optional[ProposalDecision] = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """Approve a ``pending``/``failed`` proposal and enqueue its job (409 if paused or lost race)."""
    _assert_merge_enabled(db)
    proposal = get_proposal(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal '{proposal_id}' not found")
    try:
        assert_merge_allowed(proposal["canonical"], list(proposal.get("aliases") or []))
    except MergeRuleViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job_id = str(uuid.uuid4())
    try:
        proposal = transition_proposal(
            db,
            proposal_id,
            expected=APPROVABLE_STATUSES,
            new_status=STATUS_APPROVED,
            decided_at=utc_now_naive(),
            decided_by=_decided_by(),
            decision_note=(body.note if body else None),
            apply_job_id=job_id,
            error=None,
        )
    except ProposalError as exc:
        raise _proposal_http_error(exc) from exc
    try:
        governance_queue.enqueue(
            "merge_projects",
            payload={"manual": True, "proposal_id": proposal_id, "limit": 1},
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Approved without a job would be stuck; ``failed`` lets the admin retry.
        transition_proposal(
            db,
            proposal_id,
            expected=STATUS_APPROVED,
            new_status=STATUS_FAILED,
            error=f"enqueue failed: {exc}"[:2000],
        )
        raise HTTPException(status_code=503, detail=f"enqueue failed: {exc}") from exc
    return {"proposal": proposal, "job_id": job_id, "status": "queued"}


@router.post("/projects/merge-proposals/{proposal_id}/reject")
def reject_merge_proposal(
    proposal_id: str,
    body: Optional[ProposalDecision] = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """Reject a pending, failed or approved-but-not-yet-applied proposal."""
    try:
        updated = transition_proposal(
            db,
            proposal_id,
            expected=(STATUS_PENDING, STATUS_APPROVED, STATUS_FAILED),
            new_status=STATUS_REJECTED,
            decided_at=utc_now_naive(),
            decided_by=_decided_by(),
            decision_note=(body.note if body else None),
        )
    except ProposalError as exc:
        raise _proposal_http_error(exc) from exc
    return {"proposal": updated}


@router.get("/projects/merge-inconsistencies")
def merge_inconsistencies(
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Dict[str, Any]:
    """Read-only reconciliation report (see runbook)."""
    try:
        report = detect_merge_inconsistencies(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**report, "count": len(report["items"])}
