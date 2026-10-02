"""Synchronous Spec → PLANKA mirror hooks (ADR-006).

Enabled when ``PLANKA_MIRROR_SYNC`` is truthy (compose sets ``1``). When disabled,
mutations succeed without calling PLANKA (tests / local without sidecar).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.utils.planka import PlankaMirrorError, PlankaMirrorHttpClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


def mirror_sync_enabled() -> bool:
    raw = (os.getenv("PLANKA_MIRROR_SYNC") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


# Cap how long a sync mirror may block the request thread. Individual HTTP calls
# already use DEFAULT_PLANKA_TIMEOUT_SECONDS; this bounds the whole multi-call op
# (ensure board + lists + card) so a wedged sidecar cannot hang MCP/REST forever.
_MIRROR_OP_TIMEOUT_SECONDS = 30.0


def _run_async(coro: Awaitable[T], *, timeout: float = _MIRROR_OP_TIMEOUT_SECONDS) -> T:
    async def _bounded() -> T:
        return await asyncio.wait_for(coro, timeout=timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded())
    # Already inside an event loop (e.g. MCP async tool): run in a fresh loop thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_bounded())).result(timeout=timeout + 1.0)


async def _call(
    db: Session,
    op: Callable[[PlankaMirrorHttpClient], Awaitable[T]],
) -> T:
    client = PlankaMirrorHttpClient(db)
    return await op(client)


def run_mirror(
    db: Session,
    op: Callable[[PlankaMirrorHttpClient], Awaitable[T]],
    *,
    action: str,
) -> Optional[T]:
    """Run a mirror op or no-op when disabled. Raises HTTPException 502 on failure."""
    if not mirror_sync_enabled():
        return None
    try:
        return _run_async(_call(db, op))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        logger.error("planka_mirror_failed action=%s status=504 detail=op_timeout", action)
        raise HTTPException(
            status_code=502,
            detail={
                "mirror_failed": True,
                "action": action,
                "planka_status": 504,
                "detail": "PLANKA mirror operation timed out",
            },
        ) from exc
    except PlankaMirrorError as exc:
        logger.error("planka_mirror_failed action=%s status=%s detail=%s", action, exc.status_code, exc.detail)
        raise HTTPException(
            status_code=502,
            detail={
                "mirror_failed": True,
                "action": action,
                "planka_status": exc.status_code,
                "detail": exc.detail,
            },
        ) from exc


def run_mirror_best_effort(
    db: Session,
    op: Callable[[PlankaMirrorHttpClient], Awaitable[T]],
    *,
    action: str,
) -> Optional[T]:
    """Like ``run_mirror`` but logs failures instead of raising (MCP / background)."""
    if not mirror_sync_enabled():
        return None
    try:
        return _run_async(_call(db, op))
    except Exception:  # noqa: BLE001 — caller already persisted Spec state
        logger.warning("planka_mirror_best_effort_failed action=%s", action, exc_info=True)
        return None


def mirror_task(db: Session, task_id: UUID) -> None:
    run_mirror(db, lambda c: c.mirror_task(task_id), action="mirror_task")


def mirror_task_status(db: Session, task_id: UUID) -> None:
    run_mirror(db, lambda c: c.mirror_task_status(task_id), action="mirror_task_status")


def mirror_document(db: Session, workspace_id: UUID, doc_type: str) -> None:
    run_mirror(
        db,
        lambda c: c.mirror_document(workspace_id, doc_type),
        action="mirror_document",
    )


def mirror_document_best_effort(db: Session, workspace_id: UUID, doc_type: str) -> None:
    run_mirror_best_effort(
        db,
        lambda c: c.mirror_document(workspace_id, doc_type),
        action="mirror_document",
    )


def mirror_comment_best_effort(
    db: Session,
    target_type: str,
    target_id: UUID,
    body: str,
    author: Optional[str] = None,
) -> None:
    run_mirror_best_effort(
        db,
        lambda c: c.mirror_comment(target_type, target_id, body, author),
        action="mirror_comment",
    )


def mirror_ensure_workspace(db: Session, workspace_id: UUID) -> None:
    run_mirror(
        db,
        lambda c: c.ensure_workspace_board(workspace_id),
        action="ensure_workspace_board",
    )


def mirror_delete_task(db: Session, task_id: UUID) -> None:
    run_mirror(db, lambda c: c.delete_task(task_id), action="delete_task")


def mirror_archive_task_best_effort(db: Session, task_id: UUID, *, archived: bool) -> None:
    """Projeta o arquivamento no PLANKA: tira o card do quadro ou o recria.

    Spec é a fonte de verdade — o card, o histórico e os comentários continuam
    no banco. No PLANKA o card arquivado simplesmente deixa de existir; ao
    desarquivar, ``mirror_task`` o recria na coluna atual. Best-effort porque o
    estado Spec já foi gravado quando isto roda.
    """
    if archived:
        run_mirror_best_effort(db, lambda c: c.delete_task(task_id), action="archive_task")
    else:
        run_mirror_best_effort(db, lambda c: c.mirror_task(task_id), action="unarchive_task")


def _lifecycle_flags(db: Session, workspace_id: UUID) -> tuple[bool, bool]:
    """Deriva ``(is_archived, is_completed)`` do ``SpecWorkspace.status`` atual.

    Fonte única de verdade: um workspace ``arquivado`` conta como completo E
    arquivado (evita ficar preso em "Concluídos" depois de arquivado); os
    demais status contam como ``(False, False)``.
    """
    from app.models import SpecWorkspace, SpecWorkspaceStatus

    ws = db.query(SpecWorkspace).filter(SpecWorkspace.id == workspace_id).first()
    if ws is None:
        return (False, False)
    is_archived = ws.status == SpecWorkspaceStatus.arquivado
    is_completed = ws.status in (SpecWorkspaceStatus.concluido, SpecWorkspaceStatus.arquivado)
    return (is_archived, is_completed)


def mirror_set_project_lifecycle(db: Session, workspace_id: UUID) -> None:
    is_archived, is_completed = _lifecycle_flags(db, workspace_id)
    run_mirror(
        db,
        lambda c: c.set_project_lifecycle(
            workspace_id, is_archived=is_archived, is_completed=is_completed
        ),
        action="set_project_lifecycle",
    )


def mirror_set_project_lifecycle_best_effort(db: Session, workspace_id: UUID) -> None:
    is_archived, is_completed = _lifecycle_flags(db, workspace_id)
    run_mirror_best_effort(
        db,
        lambda c: c.set_project_lifecycle(
            workspace_id, is_archived=is_archived, is_completed=is_completed
        ),
        action="set_project_lifecycle",
    )
