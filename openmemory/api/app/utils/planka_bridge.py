"""Apply human PLANKA card moves onto Spec SoT (ADR-007)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    SpecPlankaIdMap,
    SpecWorkspace,
    SpecWorkspaceStatus,
    TaskCard,
    TaskCardStatus,
    get_current_utc_time,
)
from app.utils.planka import ENTITY_PROJECT, ENTITY_TASK
from app.utils.task_lock import (
    ClaimTaskResult,
    TaskStatusPolicyError,
    UpdateTaskStatusResult,
    claim_task,
    release_task,
    update_task_status,
)

logger = logging.getLogger(__name__)


class PlankaBridgeError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(detail)


def apply_planka_card_update(
    db: Session,
    *,
    planka_card_id: str,
    changed_fields: set[str],
    name: Optional[str] = None,
    description: Optional[str] = None,
    due_date: Optional[datetime] = None,
    position: Optional[float] = None,
) -> dict:
    """Persist human PLANKA metadata edits in the TaskCard read by MCP."""
    task_map = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_TASK,
            SpecPlankaIdMap.planka_id == planka_card_id,
        )
        .first()
    )
    if task_map is None:
        return {"applied": False, "reason": "not_mapped"}
    task = db.get(TaskCard, task_map.spec_id)
    if task is None:
        raise PlankaBridgeError(404, "task_missing", "Task Spec ausente para card PLANKA")

    allowed = {"name", "description", "dueDate", "position"}
    fields = changed_fields & allowed
    values: dict = {"version": TaskCard.version + 1, "updated_at": get_current_utc_time()}
    if "name" in fields:
        if not name or not name.strip():
            raise PlankaBridgeError(422, "invalid_name", "Título do card não pode ser vazio")
        values["title"] = name
    if "description" in fields:
        values["description"] = description
    if "dueDate" in fields:
        values["due_at"] = due_date
    if "position" in fields and position is not None:
        values["position"] = position
    if len(values) == 2:
        return {"applied": False, "reason": "no_supported_fields", "task_id": str(task.id)}

    db.execute(sa.update(TaskCard).where(TaskCard.id == task.id).values(**values))
    db.commit()
    db.refresh(task)
    return {"applied": True, "task_id": str(task.id), "version": task.version}


def reconcile_planka_board_lifecycle(
    db: Session,
    *,
    planka_card_id: str,
    actor: str,
) -> Optional[dict]:
    """Conclui/reabre o workspace pela posição real dos cards no PLANKA."""
    try:
        state = db.execute(
            sa.text(
                """
                SELECT b.project_id::text AS project_id,
                       count(*) AS total,
                       count(*) FILTER (
                         WHERE lower(coalesce(l.name, '')) NOT IN ('sdd', 'concluído', 'concluido')
                       ) AS outside_allowed
                  FROM planka.card moved
                  JOIN planka.list moved_list ON moved_list.id = moved.list_id
                  JOIN planka.board b ON b.id = moved_list.board_id
                  JOIN planka.list l ON l.board_id = b.id AND l.type = 'active'
                  JOIN planka.card c ON c.list_id = l.id
                 WHERE moved.id::text = :card_id
                 GROUP BY b.project_id
                """
            ),
            {"card_id": planka_card_id},
        ).mappings().first()
    except Exception:  # SQLite/unit tests and installations without the PLANKA schema
        logger.debug("PLANKA board lifecycle query unavailable", exc_info=True)
        return None
    if not state or int(state["total"] or 0) == 0:
        return None

    project_map = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_PROJECT,
            SpecPlankaIdMap.planka_id == state["project_id"],
        )
        .first()
    )
    if project_map is None:
        return None
    workspace = db.get(SpecWorkspace, project_map.spec_id)
    if workspace is None or workspace.status == SpecWorkspaceStatus.arquivado:
        return None

    completed = int(state["outside_allowed"] or 0) == 0
    if completed:
        target = SpecWorkspaceStatus.concluido
    elif workspace.status == SpecWorkspaceStatus.concluido:
        target = SpecWorkspaceStatus.ativo
    else:
        return {
            "workspace_id": str(workspace.id),
            "status": workspace.status.value,
            "completed": False,
        }
    if workspace.status != target:
        from app.utils.workspace_lifecycle import apply_status_change

        apply_status_change(db, workspace, target, actor=actor or "planka-ui")
    return {"workspace_id": str(workspace.id), "status": target.value, "completed": completed}


def _status_for_list_id(db: Session, planka_list_id: str) -> Optional[str]:
    row = (
        db.query(SpecPlankaIdMap)
        .filter(SpecPlankaIdMap.planka_id == planka_list_id)
        .first()
    )
    if row is None:
        return None
    # entity_type is list:<status>
    if not row.entity_type.startswith("list:"):
        return None
    status = row.entity_type.split(":", 1)[1]
    if status == "documentos":
        return None
    return status


def _is_document_list(db: Session, planka_list_id: str) -> bool:
    row = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == "list:documentos",
            SpecPlankaIdMap.planka_id == planka_list_id,
        )
        .first()
    )
    return row is not None


def apply_planka_card_move(
    db: Session,
    *,
    planka_card_id: str,
    planka_list_id: str,
    actor: str,
) -> dict:
    """Map a PLANKA list change to claim / release / update_task_status."""
    task_map = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_TASK,
            SpecPlankaIdMap.planka_id == planka_card_id,
        )
        .first()
    )
    if task_map is None:
        # Non-Spec card (or not yet mapped) — ignore.
        return {"applied": False, "reason": "not_mapped"}

    if _is_document_list(db, planka_list_id):
        # SDD is a documentation view, not a TaskCard status column.
        return {"applied": False, "reason": "document_list", "task_id": str(task_map.spec_id)}

    target_status = _status_for_list_id(db, planka_list_id)
    if target_status is None:
        raise PlankaBridgeError(
            400,
            "unknown_list",
            f"Lista PLANKA {planka_list_id} não mapeada a status Spec",
        )

    task = db.query(TaskCard).filter(TaskCard.id == task_map.spec_id).first()
    if task is None:
        raise PlankaBridgeError(404, "task_missing", "Task Spec ausente para card PLANKA")

    if task.archived_at is not None:
        # Card arquivado não deveria estar no PLANKA (o espelho o remove); se
        # sobrou por falha do espelho, o movimento é ignorado em vez de
        # reativar o card sem unarchive_task.
        return {"applied": False, "reason": "archived", "task_id": str(task.id)}

    actor_id = (actor or "").strip() or "ui-user"
    current = task.status.value if hasattr(task.status, "value") else str(task.status)

    if current == target_status:
        return {
            "applied": False,
            "reason": "noop",
            "task_id": str(task.id),
            "status": current,
            "version": task.version,
        }

    try:
        if current == TaskCardStatus.tasks.value and target_status == TaskCardStatus.em_andamento.value:
            result: ClaimTaskResult = claim_task(db, task.id, actor_id)
            if not result.claimed:
                raise PlankaBridgeError(
                    409,
                    "claim_conflict",
                    f"Já assumida por {result.current_assignee}",
                )
            db.refresh(task)
            return {
                "applied": True,
                "action": "claim",
                "task_id": str(task.id),
                "status": task.status.value,
                "version": result.version,
            }

        if target_status == TaskCardStatus.tasks.value:
            release_task(db, task.id, actor_id, reason="release via PLANKA canvas")
            db.refresh(task)
            return {
                "applied": True,
                "action": "release",
                "task_id": str(task.id),
                "status": task.status.value,
                "version": task.version,
            }

        upd: UpdateTaskStatusResult = update_task_status(
            db,
            task.id,
            TaskCardStatus(target_status),
            task.version,
            actor_id,
            # The internal Planka bridge is the trusted UI path. Its actor is
            # the logged-in Planka user, which may differ from the Spec assignee.
            enforce_policy=False,
        )
        if upd.conflict or not upd.updated:
            raise PlankaBridgeError(
                409,
                "status_conflict",
                "Conflito OCC ao aplicar status Spec",
            )
        db.refresh(task)
        return {
            "applied": True,
            "action": "status",
            "task_id": str(task.id),
            "status": upd.status,
            "version": upd.version,
        }
    except TaskStatusPolicyError as exc:
        raise PlankaBridgeError(409, exc.code, exc.message) from exc
