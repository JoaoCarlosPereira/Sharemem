"""Import PLANKA → Spec de cards criados por uma pessoa na tela do PLANKA.

Spec continua a fonte de verdade (ADR-005). Cards criados pelo espelho Spec →
PLANKA já nascem com vínculo em ``spec_planka_id_map`` (e o PLANKA só notifica
criações de sessão JWT); este módulo cobre o caminho inverso — webhook
``card-created`` — transformando o card em ``TaskCard`` do workspace dono da lista.

- ``status`` = coluna do card; sem claim (``assignee`` vazio). Colunas não
  mapeadas e a coluna SDD são ignoradas.
- Idempotente: card já mapeado (task ou documento) nunca é reimportado.
- Nunca apaga nada, nem no Spec nem no PLANKA, e não escreve no PLANKA.
Ver ``docs/runbooks/planka-card-import.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    SpecAuditLog,
    SpecPlankaIdMap,
    SpecWorkspace,
    TaskCard,
    TaskCardStatus,
)
from app.utils.planka import (
    DOCUMENT_LIST_ENTITY,
    ENTITY_DOCUMENT,
    ENTITY_TASK,
    SPEC_STATUS_TO_LIST_NAME,
    is_planka_id,
)

logger = logging.getLogger(__name__)

IMPORT_ACTION = "import_planka_card"
_DEFAULT_POSITION = 65536.0


def _task_map_for_card(db: Session, planka_card_id: str) -> Optional[SpecPlankaIdMap]:
    return (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_TASK,
            SpecPlankaIdMap.planka_id == planka_card_id,
        )
        .first()
    )


def _is_document_card(db: Session, planka_card_id: str) -> bool:
    return (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_DOCUMENT,
            SpecPlankaIdMap.planka_id == planka_card_id,
        )
        .first()
        is not None
    )


def resolve_list(db: Session, planka_list_id: Optional[str]) -> tuple[Optional[UUID], Optional[str], str]:
    """``(workspace_id, status, reason)`` para uma lista PLANKA.

    ``status`` é ``None`` quando a lista não deve gerar task (``reason`` explica:
    ``document_list`` ou ``unknown_list``).
    """
    if not planka_list_id:
        return None, None, "unknown_list"
    row = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.planka_id == str(planka_list_id),
            SpecPlankaIdMap.entity_type.like("list:%"),
        )
        .first()
    )
    if row is None:
        return None, None, "unknown_list"
    if row.entity_type == DOCUMENT_LIST_ENTITY:
        return row.spec_id, None, "document_list"
    status = row.entity_type.split(":", 1)[1]
    if status not in SPEC_STATUS_TO_LIST_NAME:
        return row.spec_id, None, "unknown_list"
    return row.spec_id, status, "ok"


def _parse_due(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_position(raw: Any) -> float:
    try:
        return float(raw) if raw is not None else _DEFAULT_POSITION
    except (TypeError, ValueError):
        return _DEFAULT_POSITION


def import_planka_card(
    db: Session,
    *,
    planka_card_id: str,
    planka_list_id: Optional[str],
    name: Optional[str],
    description: Optional[str] = None,
    due_date: Any = None,
    position: Any = None,
    actor: Optional[str] = None,
    source: str = "card_created",
) -> dict[str, Any]:
    """Cria a ``TaskCard`` de um card PLANKA ainda não mapeado. Idempotente.

    Retorna ``{"applied": bool, "reason"?, "action"?, "task_id"?, "status"?, "version"?}``
    no mesmo formato dos webhooks card-moved/card-updated. Corrida entre dois
    eventos do mesmo card é resolvida pela UniqueConstraint ``(entity_type, planka_id)``.
    """
    card_id = str(planka_card_id)
    # Defesa em profundidade: o ID vira path no espelho (PATCH /api/cards/:id).
    if not is_planka_id(card_id) or (planka_list_id is not None and not is_planka_id(planka_list_id)):
        return {"applied": False, "reason": "invalid_id"}
    existing = _task_map_for_card(db, card_id)
    if existing is not None:
        return {"applied": False, "reason": "already_mapped", "task_id": str(existing.spec_id)}
    if _is_document_card(db, card_id):
        return {"applied": False, "reason": "document_card"}

    workspace_id, status, reason = resolve_list(db, planka_list_id)
    if status is None:
        return {"applied": False, "reason": reason if workspace_id else "not_mapped"}

    workspace = db.get(SpecWorkspace, workspace_id)
    if workspace is None:
        return {"applied": False, "reason": "not_mapped"}

    title = (name or "").strip()[:1024] or "untitled"
    task = TaskCard(
        workspace_id=workspace_id,
        title=title,
        description=description,
        status=TaskCardStatus(status),
        assignee=None,
        due_at=_parse_due(due_date),
        position=_parse_position(position),
    )
    db.add(task)
    db.flush()
    db.add(SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=task.id, planka_id=card_id))
    db.add(
        SpecAuditLog(
            workspace_id=workspace_id,
            actor=(actor or "planka-ui").strip() or "planka-ui",
            action=IMPORT_ACTION,
            detail={
                "task_id": str(task.id),
                "planka_card_id": card_id,
                "planka_list_id": str(planka_list_id),
                "status": status,
                "source": source,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Outro evento do mesmo card venceu a corrida: descarta esta cópia.
        db.rollback()
        winner = _task_map_for_card(db, card_id)
        if winner is not None:
            return {"applied": False, "reason": "already_mapped", "task_id": str(winner.spec_id)}
        raise
    db.refresh(task)
    logger.info(
        "planka_card_imported card=%s task=%s workspace=%s status=%s source=%s",
        card_id,
        task.id,
        workspace_id,
        status,
        source,
    )
    return {
        "applied": True,
        "action": "import",
        "task_id": str(task.id),
        "status": status,
        "version": task.version,
    }


__all__ = ["IMPORT_ACTION", "import_planka_card", "resolve_list"]
