"""Transição de ``SpecWorkspace.status`` compartilhada (Tarefa kanban-archive-lifecycle).

Ponto único que grava ``completed_at``/``archived_at``/``archived_by`` e
espelha o resultado no PLANKA (``Project.isArchived``/``isCompleted``) — usado
tanto pelo endpoint ``PATCH /workspaces/{id}`` (ação humana/MCP) quanto pelo
worker de auto-arquivamento (``spec_workspace_archive_worker``), para as duas
vias nunca divergirem sobre quando os timestamps são setados/limpos.

Espelho é best-effort: uma falha no PLANKA não deve impedir a transição de
status no Spec, que é sempre a fonte de verdade (ADR-005/ADR-006).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    SpecDocument,
    SpecWorkspace,
    SpecWorkspaceStatus,
    TaskCard,
    TaskCardStatus,
    get_current_utc_time,
)

logger = logging.getLogger(__name__)

# Status "abertos" — reabrir um workspace concluído/arquivado para qualquer um
# destes limpa os timestamps de conclusão/arquivamento correspondentes.
_OPEN_STATUSES = (SpecWorkspaceStatus.planejamento, SpecWorkspaceStatus.ativo)


def reconcile_workspace_completion_from_tasks(
    db: Session,
    workspace_id,
    *,
    actor: Optional[str] = None,
) -> SpecWorkspace | None:
    """Deriva o lifecycle do quadro a partir de seus cards de trabalho.

    Cards de documentos ficam sempre na coluna SDD e não são ``TaskCard``.
    Portanto, quando não resta task fora de ``concluido``, todos os cards do
    quadro estão em SDD e/ou Concluído e o workspace deve ser concluído.
    """
    workspace = db.get(SpecWorkspace, workspace_id)
    if workspace is None or workspace.status == SpecWorkspaceStatus.arquivado:
        return workspace

    # Cards arquivados saíram do fluxo (cancelados/obsoletos): não seguram o
    # workspace aberto nem contam como card do quadro.
    has_open_tasks = (
        db.query(TaskCard.id)
        .filter(
            TaskCard.workspace_id == workspace_id,
            TaskCard.archived_at.is_(None),
            TaskCard.status != TaskCardStatus.concluido,
        )
        .first()
        is not None
    )
    has_any_cards = (
        db.query(TaskCard.id)
        .filter(TaskCard.workspace_id == workspace_id, TaskCard.archived_at.is_(None))
        .first()
        is not None
        or db.query(SpecDocument.id).filter(SpecDocument.workspace_id == workspace_id).first()
        is not None
    )
    if not has_any_cards:
        return workspace
    if has_open_tasks:
        if workspace.status != SpecWorkspaceStatus.concluido:
            return workspace
        target = SpecWorkspaceStatus.ativo
    else:
        target = SpecWorkspaceStatus.concluido
    if workspace.status == target:
        return workspace
    return apply_status_change(db, workspace, target, actor=actor or "kanban-auto")


def apply_status_change(
    db: Session,
    workspace: SpecWorkspace,
    new_status: SpecWorkspaceStatus,
    *,
    actor: Optional[str] = None,
) -> SpecWorkspace:
    """Aplica a transição de status, ajusta timestamps e espelha no PLANKA.

    Idempotente: chamar com o status atual não altera timestamps já setados.
    """
    previous = workspace.status
    now = get_current_utc_time()

    workspace.status = new_status

    if new_status == SpecWorkspaceStatus.concluido:
        if workspace.completed_at is None:
            workspace.completed_at = now
    elif new_status == SpecWorkspaceStatus.arquivado:
        if workspace.completed_at is None:
            # Arquivamento direto (sem passar por "concluido") também conta
            # como conclusão — evita um workspace arquivado sem completed_at.
            workspace.completed_at = now
        if workspace.archived_at is None:
            workspace.archived_at = now
            workspace.archived_by = actor
    elif new_status in _OPEN_STATUSES and previous != new_status:
        workspace.completed_at = None
        workspace.archived_at = None
        workspace.archived_by = None

    db.commit()
    db.refresh(workspace)

    try:
        from app.utils.planka_hooks import mirror_set_project_lifecycle_best_effort

        mirror_set_project_lifecycle_best_effort(db, workspace.id)
    except Exception:  # noqa: BLE001 — espelho nunca deve derrubar a transição de status
        logger.exception(
            "falha ao espelhar lifecycle do workspace %s no PLANKA", workspace.id
        )

    return workspace
