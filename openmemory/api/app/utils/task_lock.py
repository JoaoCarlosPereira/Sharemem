"""Exclusividade de claim e mudança de status de tasks (ADR-003/ADR-005/ADR-007).

Lógica de domínio pura (recebe ``db: Session``, sem FastAPI ``Request``/
``Response``) reaproveitada pelo router REST (Tarefa 4), pelas tools MCP
(Tarefa 8) e pelo job de liberação por timeout (Tarefa 5). Toda operação usa a
mesma primitiva de concorrência otimista dos documentos: um
``UPDATE ... WHERE id = :id AND <guarda>`` atômico cujo ``rowcount == 0`` sinaliza
que a task já estava em outro estado (reivindicada ou em outra versão).
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    SpecAuditLog,
    TaskCard,
    TaskCardStatus,
    TaskStatusHistory,
    get_current_utc_time,
)
from app.utils.claim_lease import claim_expires_at
from app.utils.kanban_pipeline import KanbanSkipError, assert_no_forward_skip


@dataclass
class ClaimTaskResult:
    """Resultado de ``claim_task``/``release_task`` (ver TechSpec — Interfaces).

    ``expires_at`` é o prazo do lease do claim (ver ``app.utils.claim_lease``);
    ``None`` quando não se aplica — release, falha de claim ou expiração desligada.
    """
    claimed: bool
    current_assignee: str | None
    version: int
    expires_at: object | None = None
    # ``True`` quando o claim falhou porque o card está arquivado (e não por
    # exclusividade): o chamador deve desarquivar antes, não escolher outro card.
    archived: bool = False
    # Status efetivo após o claim (``em_andamento`` no claim normal/reassunção;
    # o status original na adoção de card sem dono fora do backlog).
    status: str | None = None


@dataclass
class UpdateTaskStatusResult:
    """Resultado de ``update_task_status`` (ClaimTaskResult-like, com conflito)."""
    updated: bool
    conflict: bool
    version: int
    status: str
    current_assignee: str | None


@dataclass
class UpdateTaskMetadataResult:
    """Resultado de ``update_task_metadata`` (concorrência otimista atômica)."""
    updated: bool
    conflict: bool
    version: int
    title: str
    description: str | None
    branch_ref: str | None
    due_at: object | None = None
    position: float | None = None


@dataclass
class ArchiveTaskResult:
    """Resultado de ``archive_task``/``unarchive_task`` (concorrência otimista)."""
    updated: bool
    conflict: bool
    version: int
    archived_at: object | None
    archived_by: str | None


class TaskStatusPolicyError(ValueError):
    """Transição de status inválida (use claim/release; exclusividade ADR-003)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _coerce_status(status: TaskCardStatus | str) -> TaskCardStatus:
    return status if isinstance(status, TaskCardStatus) else TaskCardStatus(status)


def _assert_status_policy(
    task: TaskCard,
    new_status: TaskCardStatus,
    actor: str | None,
    db: Session | None = None,
) -> None:
    """Garante exclusividade de claim: em_andamento só via claim; backlog só via release."""
    old_status = task.status
    if old_status == TaskCardStatus.tasks and new_status != TaskCardStatus.tasks:
        raise TaskStatusPolicyError(
            "use_claim",
            "Use claim_task para sair do backlog (tasks)",
        )
    if (
        new_status == TaskCardStatus.em_andamento
        and old_status != TaskCardStatus.em_andamento
    ):
        raise TaskStatusPolicyError(
            "use_claim",
            "Use claim_task para entrar em em_andamento",
        )
    if new_status == TaskCardStatus.tasks and old_status != TaskCardStatus.tasks:
        raise TaskStatusPolicyError(
            "use_release",
            "Use release_task para devolver a task ao backlog",
        )
    assignee = (
        _canonical_task_actor(db, task.assignee)
        if db is not None and task.assignee
        else task.assignee
    )
    if assignee and (not actor or actor.strip().casefold() != assignee.strip().casefold()):
        raise TaskStatusPolicyError(
            "not_assignee",
            f"Apenas o assignee ({task.assignee}) pode alterar o status",
        )
    try:
        assert_no_forward_skip(old_status, new_status)
    except KanbanSkipError as exc:
        raise TaskStatusPolicyError(exc.code, exc.message) from exc


def _canonical_task_actor(db: Session | None, identity: str | None) -> str | None:
    """Resolve a linked machine hostname to its person's email for task ownership."""
    raw = (identity or "").strip()
    if not raw or db is None:
        return raw or None

    try:
        from app.models import Machine, MachineStatus, User
        from app.utils.logging_context import auth_method_var, auth_user_var

        with db.begin_nested():
            machine = (
                db.query(Machine)
                .filter(sa.func.lower(Machine.hostname) == raw.lower())
                .first()
            )
            if (
                machine is None
                or machine.status != MachineStatus.linked
                or machine.linked_user_id is None
            ):
                return raw
            if auth_method_var.get() in ("agent_token", "session"):
                authenticated_user = (auth_user_var.get() or "").strip()
                if authenticated_user != str(machine.linked_user_id):
                    return raw
            person = db.get(User, machine.linked_user_id)
            email = (person.email or "").strip() if person is not None else ""
            return email or raw
    except Exception:  # noqa: BLE001 — preserve legacy behavior if identity lookup is unavailable
        return raw


def claim_task(db: Session, task_id: uuid.UUID, claimant: str) -> ClaimTaskResult:
    """Reivindica uma task, movendo-a para ``em_andamento``.

    Dois caminhos, ambos terminando em ``em_andamento`` com o chamador como
    assignee:

    1. **Task livre** (coluna ``tasks``): claim normal.
    2. **Task que já é do chamador**, em qualquer coluna: idempotente. Reassume o
       card e renova o lease. É o que permite (a) devolver a ``em_andamento`` um
       card cuja verificação reprovou em ``revisao_codigo``/``fase_teste`` — o
       fluxo que o próprio bloco ``kanban`` instrui — e (b) renovar um claim antes
       que o timeout o devolva ao backlog. Antes, ambos eram impossíveis: o
       ``UPDATE`` exigia ``status == tasks``, então o assignee recebia
       ``claimed: false`` apontando ele mesmo como "outro responsável", e as
       únicas saídas eram ``release_task`` (que perde a atribuição e faz o card
       parecer abandonado) ou mentir sobre a coluna com ``is_blocked``.

    3. **Adoção de task sem dono fora do backlog** (``assignee is None`` e
       status ∉ {``tasks``, ``concluido``}) — ex.: card criado na UI do PLANKA
       direto em ``revisao_codigo`` e importado. Atribui o chamador e **mantém o
       status atual** (não é uma transição: nenhuma regra de pipeline/skip é
       contornada). Registra auditoria ``adopt_task``; sem linha em
       ``TaskStatusHistory`` porque o status não muda.

    Falha (``claimed=False``) apenas quando a task está ativa com assignee
    DIFERENTE — a exclusividade para terceiros continua intacta. Retorna o
    ``assignee`` vigente para o chamador reconciliar.
    """
    task = db.get(TaskCard, task_id)
    if task is None:
        raise ValueError(f"TaskCard {task_id} não encontrada")

    if task.archived_at is not None:
        # Card arquivado não volta ao fluxo por claim: desarquivar é explícito.
        return ClaimTaskResult(
            claimed=False,
            current_assignee=task.assignee,
            version=task.version,
            archived=True,
        )

    claimant = _canonical_task_actor(db, claimant) or ""
    assignee = _canonical_task_actor(db, task.assignee) if task.assignee else None

    now = get_current_utc_time()
    old_status = task.status
    # Reassunção do próprio card: qualquer coluna serve como origem, desde que o
    # assignee gravado seja o chamador. A guarda vai no WHERE (não num if sobre o
    # objeto lido) para manter a atomicidade — entre o SELECT e o UPDATE outro
    # processo pode ter liberado ou reatribuído a task.
    is_reclaim = (
        assignee is not None
        and assignee.casefold() == claimant.casefold()
        and old_status != TaskCardStatus.tasks
    )
    is_adopt = (
        not is_reclaim
        and task.assignee is None
        and old_status not in (TaskCardStatus.tasks, TaskCardStatus.concluido)
    )
    new_status = old_status if is_adopt else TaskCardStatus.em_andamento
    if is_reclaim:
        guard = sa.and_(
            TaskCard.id == task_id,
            TaskCard.assignee == task.assignee,
        )
    elif is_adopt:
        # Atômico: só adota se continua sem dono e na mesma coluna.
        guard = sa.and_(
            TaskCard.id == task_id,
            TaskCard.assignee.is_(None),
            TaskCard.status == old_status,
        )
    else:
        guard = sa.and_(
            TaskCard.id == task_id,
            TaskCard.status == TaskCardStatus.tasks,
        )

    result = db.execute(
        sa.update(TaskCard)
        .where(guard, TaskCard.archived_at.is_(None))
        .values(
            assignee=claimant,
            status=new_status,
            version=TaskCard.version + 1,
            last_activity_at=now,
            updated_at=now,
        )
    )

    if result.rowcount == 0:
        db.rollback()
        fresh = db.get(TaskCard, task_id)
        return ClaimTaskResult(
            claimed=False,
            current_assignee=fresh.assignee,
            version=fresh.version,
            archived=fresh.archived_at is not None,
        )

    if old_status != new_status:
        db.add(
            TaskStatusHistory(
                task_id=task_id,
                old_status=old_status,
                new_status=new_status,
                changed_by=claimant,
            )
        )
    action = "reclaim_task" if is_reclaim else ("adopt_task" if is_adopt else "claim_task")
    db.add(
        SpecAuditLog(
            workspace_id=task.workspace_id,
            actor=claimant,
            action=action,
            detail={
                "task_id": str(task_id),
                "from_status": old_status.value,
                "to_status": new_status.value,
            },
        )
    )
    db.commit()

    fresh = db.get(TaskCard, task_id)
    # Spec → PLANKA sync (no-op unless PLANKA_MIRROR_SYNC enabled).
    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return ClaimTaskResult(
        claimed=True,
        current_assignee=claimant,
        version=fresh.version,
        expires_at=claim_expires_at(fresh.last_activity_at),
        status=new_status.value,
    )


def release_task(
    db: Session,
    task_id: uuid.UUID,
    actor: str | None,
    reason: str | None = None,
    expected_version: int | None = None,
) -> ClaimTaskResult:
    """Libera uma task manualmente (ou via job de timeout — Tarefa 5).

    Volta o status para ``tasks``, limpa ``assignee`` e o marcador de bloqueio
    (``is_blocked``/``block_reason``) e registra ``TaskStatusHistory``. Bump de
    ``version`` invalida qualquer gravação otimista em voo.

    ``expected_version``:
    - ``None`` (release manual): incondicional — sempre aplica. ``claimed=False``.
    - inteiro (job de timeout): usa ``UPDATE ... WHERE version = :expected`` para
      ser idempotente entre réplicas — só uma consegue liberar. ``claimed=True``
      quando ESTA chamada aplicou a liberação; ``claimed=False`` quando outra
      réplica já a fez (no-op).
    """
    task = db.get(TaskCard, task_id)
    if task is None:
        raise ValueError(f"TaskCard {task_id} não encontrada")
    actor = _canonical_task_actor(db, actor)
    if task.archived_at is not None:
        raise TaskStatusPolicyError(
            "archived",
            "Card arquivado: use unarchive_task antes de devolvê-lo ao backlog",
        )

    old_status = task.status
    now = get_current_utc_time()

    if expected_version is not None:
        result = db.execute(
            sa.update(TaskCard)
            .where(
                TaskCard.id == task_id,
                TaskCard.version == expected_version,
                TaskCard.status == TaskCardStatus.em_andamento,
            )
            .values(
                status=TaskCardStatus.tasks,
                assignee=None,
                is_blocked=False,
                block_reason=None,
                version=TaskCard.version + 1,
                last_activity_at=now,
                updated_at=now,
            )
        )
        if result.rowcount == 0:
            # Outra réplica já liberou (ou a versão mudou): no-op idempotente.
            db.rollback()
            fresh = db.get(TaskCard, task_id)
            return ClaimTaskResult(
                claimed=False,
                current_assignee=fresh.assignee,
                version=fresh.version,
            )
        applied = True
    else:
        task.status = TaskCardStatus.tasks
        task.assignee = None
        task.is_blocked = False
        task.block_reason = None
        task.version = task.version + 1
        task.last_activity_at = now
        task.updated_at = now
        applied = False  # release manual não é um "claim"

    db.add(
        TaskStatusHistory(
            task_id=task_id,
            old_status=old_status,
            new_status=TaskCardStatus.tasks,
            changed_by=actor,
        )
    )
    db.add(
        SpecAuditLog(
            workspace_id=task.workspace_id,
            actor=actor,
            action="release_task",
            detail={"reason": reason} if reason else {},
        )
    )
    db.commit()
    fresh = db.get(TaskCard, task_id)

    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return ClaimTaskResult(
        claimed=applied,
        current_assignee=None,
        version=fresh.version,
    )


def update_task_status(
    db: Session,
    task_id: uuid.UUID,
    new_status: TaskCardStatus | str,
    expected_version: int,
    actor: str | None,
    is_blocked: bool | None = None,
    block_reason: str | None = None,
    enforce_policy: bool = True,
) -> UpdateTaskStatusResult:
    """Muda o status (coluna) de uma task com concorrência otimista.

    ``expected_version`` desatualizado retorna ``conflict=True`` sem alterar
    nada. ``is_blocked``/``block_reason`` são opcionais e ortogonais à coluna —
    reportar bloqueio = chamar com ``new_status`` igual ao atual e
    ``is_blocked=True`` (ver ADR-007). Registra ``TaskStatusHistory`` na mudança.
    """
    new_status = _coerce_status(new_status)

    task = db.get(TaskCard, task_id)
    if task is None:
        raise ValueError(f"TaskCard {task_id} não encontrada")
    actor = _canonical_task_actor(db, actor)

    if task.archived_at is not None:
        # Fora da política (vale também para enforce_policy=False do bridge
        # PLANKA): card arquivado só volta ao fluxo por unarchive_task.
        raise TaskStatusPolicyError(
            "archived",
            "Card arquivado: use unarchive_task antes de mover de coluna",
        )
    if enforce_policy:
        _assert_status_policy(task, new_status, actor, db=db)

    old_status = task.status
    now = get_current_utc_time()

    values = {
        "status": new_status,
        "version": TaskCard.version + 1,
        "last_activity_at": now,
        "updated_at": now,
    }
    if is_blocked is not None:
        values["is_blocked"] = is_blocked
        values["block_reason"] = block_reason

    result = db.execute(
        sa.update(TaskCard)
        .where(
            TaskCard.id == task_id,
            TaskCard.version == expected_version,
        )
        .values(**values)
    )

    if result.rowcount == 0:
        db.rollback()
        fresh = db.get(TaskCard, task_id)
        return UpdateTaskStatusResult(
            updated=False,
            conflict=True,
            version=fresh.version,
            status=fresh.status.value,
            current_assignee=fresh.assignee,
        )

    if new_status != old_status:
        db.add(
            TaskStatusHistory(
                task_id=task_id,
                old_status=old_status,
                new_status=new_status,
                changed_by=actor,
            )
        )
    db.add(
        SpecAuditLog(
            workspace_id=task.workspace_id,
            actor=actor,
            action="update_task_status",
            detail={
                "old_status": old_status.value,
                "new_status": new_status.value,
                "is_blocked": is_blocked,
            },
        )
    )
    db.commit()

    fresh = db.get(TaskCard, task_id)
    from app.utils.workspace_lifecycle import reconcile_workspace_completion_from_tasks

    reconcile_workspace_completion_from_tasks(
        db,
        fresh.workspace_id,
        actor=actor or "kanban-auto",
    )
    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return UpdateTaskStatusResult(
        updated=True,
        conflict=False,
        version=fresh.version,
        status=fresh.status.value,
        current_assignee=fresh.assignee,
    )


def update_task_metadata(
    db: Session,
    task_id: uuid.UUID,
    expected_version: int,
    *,
    title: str | None = None,
    description: str | None = None,
    branch_ref: str | None = None,
    due_at: object | None = ...,
    position: float | None = None,
    clear_due_at: bool = False,
) -> UpdateTaskMetadataResult:
    """Atualiza metadados com ``UPDATE … WHERE version = :expected`` atômico.

    Também renova ``last_activity_at`` para que edições contem como atividade
    perante o timeout worker. ``due_at`` usa sentinel ``...`` para "não alterar";
    ``clear_due_at=True`` zera o prazo.
    """
    task = db.get(TaskCard, task_id)
    if task is None:
        raise ValueError(f"TaskCard {task_id} não encontrada")

    now = get_current_utc_time()
    values: dict = {
        "version": TaskCard.version + 1,
        "last_activity_at": now,
        "updated_at": now,
    }
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description
    if branch_ref is not None:
        values["branch_ref"] = branch_ref
    if clear_due_at:
        values["due_at"] = None
    elif due_at is not ...:
        values["due_at"] = due_at
    if position is not None:
        values["position"] = position

    result = db.execute(
        sa.update(TaskCard)
        .where(
            TaskCard.id == task_id,
            TaskCard.version == expected_version,
        )
        .values(**values)
    )

    if result.rowcount == 0:
        db.rollback()
        fresh = db.get(TaskCard, task_id)
        return UpdateTaskMetadataResult(
            updated=False,
            conflict=True,
            version=fresh.version,
            title=fresh.title,
            description=fresh.description,
            branch_ref=fresh.branch_ref,
            due_at=fresh.due_at,
            position=fresh.position,
        )

    db.commit()
    fresh = db.get(TaskCard, task_id)
    from app.utils.planka_hooks import mirror_task

    mirror_task(db, task_id)
    return UpdateTaskMetadataResult(
        updated=True,
        conflict=False,
        version=fresh.version,
        title=fresh.title,
        description=fresh.description,
        branch_ref=fresh.branch_ref,
        due_at=fresh.due_at,
        position=fresh.position,
    )


_ACTIVE_COLUMNS = (
    TaskCardStatus.em_andamento,
    TaskCardStatus.revisao_codigo,
    TaskCardStatus.fase_teste,
)


def _set_archive_state(
    db: Session,
    task_id: uuid.UUID,
    expected_version: int,
    actor: str | None,
    *,
    archive: bool,
    reason: str | None = None,
) -> ArchiveTaskResult:
    task = db.get(TaskCard, task_id)
    if task is None:
        raise ValueError(f"TaskCard {task_id} não encontrada")

    if archive:
        if task.archived_at is not None:
            raise TaskStatusPolicyError("already_archived", "Card já está arquivado")
        # Exclusividade do claim (ADR-003): um card ativo de outra pessoa não
        # pode sumir do quadro por ação de terceiro.
        if (
            task.status in _ACTIVE_COLUMNS
            and task.assignee
            and (not actor or actor != task.assignee)
        ):
            raise TaskStatusPolicyError(
                "not_assignee",
                f"Card ativo com {task.assignee}: só o assignee pode arquivá-lo",
            )
    elif task.archived_at is None:
        raise TaskStatusPolicyError("not_archived", "Card não está arquivado")

    now = get_current_utc_time()
    values: dict = {
        "version": TaskCard.version + 1,
        "updated_at": now,
        "archived_at": now if archive else None,
        "archived_by": actor if archive else None,
    }
    if not archive:
        # Desarquivar conta como atividade: sem isto um card em em_andamento
        # arquivado há dias seria devolvido ao backlog pelo timeout na hora.
        values["last_activity_at"] = now

    result = db.execute(
        sa.update(TaskCard)
        .where(TaskCard.id == task_id, TaskCard.version == expected_version)
        .values(**values)
    )
    if result.rowcount == 0:
        db.rollback()
        fresh = db.get(TaskCard, task_id)
        return ArchiveTaskResult(
            updated=False,
            conflict=True,
            version=fresh.version,
            archived_at=fresh.archived_at,
            archived_by=fresh.archived_by,
        )

    detail: dict = {"task_id": str(task_id), "status": task.status.value}
    if reason:
        detail["reason"] = reason
    db.add(
        SpecAuditLog(
            workspace_id=task.workspace_id,
            actor=actor,
            action="archive_task" if archive else "unarchive_task",
            detail=detail,
        )
    )
    db.commit()

    fresh = db.get(TaskCard, task_id)
    from app.utils.planka_hooks import mirror_archive_task_best_effort
    from app.utils.workspace_lifecycle import reconcile_workspace_completion_from_tasks

    mirror_archive_task_best_effort(db, task_id, archived=archive)
    reconcile_workspace_completion_from_tasks(
        db,
        fresh.workspace_id,
        actor=actor or "kanban-auto",
    )
    fresh = db.get(TaskCard, task_id)
    return ArchiveTaskResult(
        updated=True,
        conflict=False,
        version=fresh.version,
        archived_at=fresh.archived_at,
        archived_by=fresh.archived_by,
    )


def archive_task(
    db: Session,
    task_id: uuid.UUID,
    expected_version: int,
    actor: str | None,
    reason: str | None = None,
) -> ArchiveTaskResult:
    """Arquiva um card sem apagar nada (alternativa não destrutiva ao delete).

    O card some da listagem padrão e do quadro, mas preserva coluna, assignee,
    histórico de status e comentários. ``expected_version`` desatualizado
    devolve ``conflict=True`` sem alterar nada (ADR-005). Card ativo de outro
    assignee é recusado com ``not_assignee`` (ADR-003).
    """
    return _set_archive_state(
        db, task_id, expected_version, actor, archive=True, reason=reason
    )


def unarchive_task(
    db: Session,
    task_id: uuid.UUID,
    expected_version: int,
    actor: str | None,
) -> ArchiveTaskResult:
    """Desfaz o arquivamento: o card volta à listagem na mesma coluna em que estava."""
    return _set_archive_state(db, task_id, expected_version, actor, archive=False)
