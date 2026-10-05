"""Router REST do espaço compartilhado de specs — workspaces e documentos (Tarefa 3).

Expõe criação/consulta de ``SpecWorkspace`` e gravação/consulta versionada de
``SpecDocument`` (PRD/TechSpec/Tasks). Toda leitura/escrita passa pela checagem
de ``AccessControl`` (``object_type="spec_workspace"``) reaproveitada de
``get_accessible_spec_workspace_ids``. A gravação delega o controle de conflito
para ``write_document_version`` (Tarefa 2), retornando 409 quando a versão
esperada está desatualizada (ADR-005). A mesma lógica é reaproveitada pelas
tools MCP (Tarefa 7), sem duplicação.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.database import get_db
from app.models import (
    CommentTargetType,
    DocumentOrigin,
    DocumentType,
    SpecAuditLog,
    SpecComment,
    SpecDocument,
    SpecDocumentVersion,
    SpecWorkspace,
    SpecWorkspaceStatus,
    TaskCard,
    TaskCardStatus,
    TaskStatusHistory,
    KanbanColumnPrompt,
)
from app.utils.kanban_pipeline import COLUMN_GUIDE
from app.utils.creator_identity import (
    CreatorIdentity,
    identity_for_actor,
    resolve_actor_identities_with_db,
)
from app.utils.permissions import get_accessible_spec_workspace_ids
from app.utils.projects import upsert_project
from app.utils.spec_auth import (
    is_legacy_spec_access_open,
    resolve_spec_actor,
    resolve_spec_subject,
)
from app.utils.claim_lease import TIMEOUT_ACTOR, claim_expires_at
from app.utils.spec_search import (
    index_completed_workspace,
    search_specs,
)
from app.utils.spec_versioning import write_document_version
from app.utils.task_lock import (
    TaskStatusPolicyError,
    archive_task,
    claim_task,
    release_task,
    unarchive_task,
    update_task_metadata,
    update_task_status,
)
from app.utils.workspace_lifecycle import apply_status_change
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1/specs", tags=["specs"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class WorkspaceCreate(BaseModel):
    project_id: str
    slug: str
    name: str
    status: Optional[SpecWorkspaceStatus] = None
    created_by: Optional[str] = None


class WorkspaceStatusUpdate(BaseModel):
    status: SpecWorkspaceStatus
    actor: Optional[str] = None


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: str
    slug: str
    name: str
    status: SpecWorkspaceStatus
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    archived_by: Optional[str] = None


class WorkspaceSummaryResponse(BaseModel):
    """Item do painel de Projeto: workspace + progresso resumido por status."""
    id: UUID
    project_id: str
    slug: str
    name: str
    status: SpecWorkspaceStatus
    task_counts: dict[str, int]


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    document_type: DocumentType
    current_version: int
    current_content: Optional[str] = None
    updated_by: Optional[str] = None
    updated_by_display_name: Optional[str] = None
    updated_by_avatar_url: Optional[str] = None
    updated_at: Optional[datetime] = None


class TaskLabelBrief(BaseModel):
    id: UUID
    name: str
    color: Optional[str] = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    title: str
    description: Optional[str] = None
    status: TaskCardStatus
    is_blocked: bool
    block_reason: Optional[str] = None
    assignee: Optional[str] = None
    assignee_display_name: Optional[str] = None
    assignee_avatar_url: Optional[str] = None
    version: int
    last_activity_at: Optional[datetime] = None
    # Prazo do lease do claim. Preenchido só em ``em_andamento``; ``None`` nas
    # demais colunas, sem atividade registrada, ou com a expiração desligada.
    claim_expires_at: Optional[datetime] = None
    branch_ref: Optional[str] = None
    due_at: Optional[datetime] = None
    position: float = 65536.0
    members: list[str] = []
    label_ids: list[UUID] = []
    labels: list[TaskLabelBrief] = []
    checklist_done: int = 0
    checklist_total: int = 0
    attachment_count: int = 0
    # Arquivamento não destrutivo: ``archived_at`` nulo = card ativo.
    archived_at: Optional[datetime] = None
    archived_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TaskHistoryResponse(BaseModel):
    """Uma mudança de coluna do card.

    ``by_timeout`` é derivado de ``changed_by`` e existe para o cliente não
    precisar conhecer o sentinela ``system:timeout``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    old_status: TaskCardStatus
    new_status: TaskCardStatus
    changed_by: Optional[str] = None
    changed_at: Optional[datetime] = None
    by_timeout: bool = False


class WorkspaceBoardResponse(BaseModel):
    workspace: WorkspaceResponse
    documents: list[DocumentResponse]
    tasks: list[TaskResponse]


class DocumentWriteRequest(BaseModel):
    content: str
    expected_version: Optional[int] = None
    author: Optional[str] = None


class DocumentWriteResponse(BaseModel):
    document_id: UUID
    version: int
    conflict: bool = False


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    content: str
    author: Optional[str] = None
    origin: DocumentOrigin
    created_at: Optional[datetime] = None


class TaskCreate(BaseModel):
    workspace_id: UUID
    title: str
    description: Optional[str] = None
    branch_ref: Optional[str] = None


class TaskUpdate(BaseModel):
    """Atualização parcial de metadados da task (título/descrição/branch/due/position)."""
    title: Optional[str] = None
    description: Optional[str] = None
    branch_ref: Optional[str] = None
    due_at: Optional[datetime] = None
    clear_due_at: bool = False
    position: Optional[float] = None
    members: Optional[list[str]] = None
    expected_version: int


class ClaimRequest(BaseModel):
    claimant: str


class ReleaseRequest(BaseModel):
    actor: Optional[str] = None
    reason: Optional[str] = None


class ArchiveRequest(BaseModel):
    expected_version: int
    actor: Optional[str] = None
    reason: Optional[str] = None


class StatusPatchRequest(BaseModel):
    expected_version: int
    new_status: Optional[TaskCardStatus] = None
    actor: Optional[str] = None
    is_blocked: Optional[bool] = None
    block_reason: Optional[str] = None


class CommentCreate(BaseModel):
    target_type: CommentTargetType
    target_id: UUID
    body: str
    author: Optional[str] = None


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_type: CommentTargetType
    target_id: UUID
    author: Optional[str] = None
    body: str
    created_at: Optional[datetime] = None


class SpecSearchResult(BaseModel):
    id: Optional[str] = None
    score: Optional[float] = None
    content: Optional[str] = None
    project: Optional[str] = None
    workspace_id: Optional[str] = None
    document_type: Optional[str] = None
    group_id: Optional[str] = None
    owner: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Kanban Column Prompts Schemas (Tarefa 02)
# --------------------------------------------------------------------------- #
class KanbanPromptUpdate(BaseModel):
    """Atualização parcial de um prompt de coluna Kanban."""

    prompt: Optional[str] = None
    is_enabled: Optional[bool] = None


class KanbanPromptRead(BaseModel):
    """Resposta com o estado completo de um prompt de coluna Kanban."""

    model_config = ConfigDict(from_attributes=True)

    column_status: str
    label: str
    prompt: Optional[str] = None
    is_enabled: bool
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


# --------------------------------------------------------------------------- #
# Kanban Column Prompts
# --------------------------------------------------------------------------- #
def _kanban_pipeline_statuses() -> list[str]:
    """Ordem de todos os status do pipeline Kanban (COLUMN_GUIDE keys)."""
    from app.models import TaskCardStatus

    return [
        TaskCardStatus.tasks.value,
        *[s.value for s in (TaskCardStatus.em_andamento, TaskCardStatus.revisao_codigo,
                            TaskCardStatus.fase_teste, TaskCardStatus.concluido)],
    ]


@router.get("/kanban-prompts", response_model=list[KanbanPromptRead])
def list_kanban_prompts(db: Session = Depends(get_db)) -> list[KanbanPromptRead]:
    """Lista prompts de coluna ordenados pelo status do pipeline.

    Retorna **todos** os status do pipeline (``COLUMN_GUIDE``). Quando não há
    registro personalizado no banco, retorna o guia padrão ``COLUMN_GUIDE.do_now``
    como ``prompt`` sugerido — o usuário pode editar ou manter.
    """
    rows_map: dict[str, KanbanColumnPrompt] = {
        r.column_status: r
        for r in db.query(KanbanColumnPrompt).order_by(KanbanColumnPrompt.column_status).all()
    }
    statuses = _kanban_pipeline_statuses()
    results: list[KanbanPromptRead] = []
    for status in statuses:
        row = rows_map.get(status)
        if row is not None:
            results.append(
                KanbanPromptRead(
                    column_status=row.column_status,
                    label=COLUMN_GUIDE.get(row.column_status, {}).get("label", row.column_status),
                    prompt=row.prompt,
                    is_enabled=row.is_enabled,
                    updated_at=row.updated_at,
                    updated_by=row.updated_by,
                )
            )
        else:
            guide = COLUMN_GUIDE.get(status, {})
            results.append(
                KanbanPromptRead(
                    column_status=status,
                    label=guide.get("label", status),
                    prompt=guide.get("do_now"),
                    is_enabled=True,
                    updated_at=None,
                    updated_by=None,
                )
            )
    return results


# --------------------------------------------------------------------------- #
# Kanban Column Prompts Endpoints (Tarefa 04)
# --------------------------------------------------------------------------- #
@router.get("/kanban-prompts/{status}", response_model=KanbanPromptRead)
def get_kanban_prompt_by_status(
    status: str,
    db: Session = Depends(get_db),
) -> KanbanPromptRead:
    """Retorna o prompt de uma coluna específica pelo ``column_status``.

    Retorna ``KanbanPromptRead`` com label derivado de ``COLUMN_GUIDE``.
    Se não houver registro, devolve um registro "vazio" com ``prompt=null``,
    ``is_enabled=True`` — **não** 404.
    """
    row = (
        db.query(KanbanColumnPrompt)
        .filter(KanbanColumnPrompt.column_status == status)
        .first()
    )
    # Deriva o label de COLUMN_GUIDE quando a coluna é conhecida
    guide = COLUMN_GUIDE.get(status)
    label = guide["label"] if guide else status

    if row is not None:
        return KanbanPromptRead(
            column_status=row.column_status,
            label=label,
            prompt=row.prompt,
            is_enabled=row.is_enabled,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )

    # Nenhum registro — retorna o guia padrão como sugestão (a UI permite criar)
    guide_do_now: str | None = None
    if guide is not None:
        guide_do_now = guide.get("do_now")
    return KanbanPromptRead(
        column_status=status,
        label=label,
        prompt=guide_do_now,
        is_enabled=True,
        updated_at=None,
        updated_by=None,
    )


# --------------------------------------------------------------------------- #
# Kanban Column Prompts Endpoints (Tarefa 05)
# --------------------------------------------------------------------------- #
@router.put("/kanban-prompts/{status}", response_model=KanbanPromptRead)
def update_kanban_prompt_by_status(
    status: str,
    payload: KanbanPromptUpdate,
    db: Session = Depends(get_db),
) -> KanbanPromptRead:
    """Atualiza parcialmente um prompt de coluna Kanban pelo ``column_status``.

    **Upsert**: se o registro não existir, cria um novo com os valores do
    ``payload``, registra ``updated_at`` e ``updated_by``, e invalida o cache.
    """
    row = (
        db.query(KanbanColumnPrompt)
        .filter(KanbanColumnPrompt.column_status == status)
        .first()
    )

    guide = COLUMN_GUIDE.get(status)
    label = guide["label"] if guide else status

    if row is None:
        # Upsert: cria novo registro
        row = KanbanColumnPrompt(column_status=status)
        db.add(row)

    updated_fields: list[str] = []
    if payload.prompt is not None:
        row.prompt = payload.prompt
        updated_fields.append("prompt")
    if payload.is_enabled is not None:
        row.is_enabled = payload.is_enabled
        updated_fields.append("is_enabled")

    if updated_fields:
        row.updated_at = datetime.now(timezone.utc)
        actor = resolve_spec_actor(db=db)
        if actor:
            row.updated_by = actor
        db.commit()
    else:
        # Nenhum campo mudou — apenas refresh para garantir timestamp atual
        db.refresh(row)

    # Invalidate cache after successful update
    try:
        from app.mcp_server import _invalidate_kanban_prompts_cache

        _invalidate_kanban_prompts_cache()
    except Exception:  # noqa: BLE001 — cache invalidation should not break the API
        pass

    return KanbanPromptRead(
        column_status=row.column_status,
        label=label,
        prompt=row.prompt,
        is_enabled=row.is_enabled,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _get_workspace_or_404(db: Session, workspace_id: UUID) -> SpecWorkspace:
    ws = db.query(SpecWorkspace).filter(SpecWorkspace.id == workspace_id).first()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado")
    return ws


def _assert_access(db: Session, workspace_id: UUID) -> None:
    """Valida regras ACL do usuário e garante o isolamento por grupo do workspace.

    O workspace deve pertencer ao mesmo grupo do ator autenticado.
    Se o workspace não tiver grupo, o acesso falha fechado (invisível para todos).
    """
    ws = db.query(SpecWorkspace.group_id).filter(SpecWorkspace.id == workspace_id).first()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado")
    if is_legacy_spec_access_open():
        return
    
    subject_type, subject_id = resolve_spec_subject()
    actor_group_id = None
    
    if subject_id:
        from app.models import User
        user = db.query(User.group_id).filter(User.id == subject_id).first()
        if user:
            actor_group_id = user[0]
            
    if not actor_group_id and subject_type == "user" and not subject_id:
        # Fallback para agent tokens legados na MCP (onde actor = hostname do payload)
        # O MCP pode usar um "actor" derivado
        actor = resolve_spec_actor()
        if actor:
            from app.utils.machine_resolver import find_legacy_host_user
            u = find_legacy_host_user(db, actor)
            if u and u.group_id:
                actor_group_id = u.group_id

    if ws[0] is None or not actor_group_id:
        raise HTTPException(status_code=403, detail="Workspace sem grupo ou usuário sem grupo")
    
    if ws[0] != actor_group_id:
        raise HTTPException(status_code=403, detail="Sem permissão para o grupo deste workspace")

    # 2. ACL baseada em objeto
    accessible = get_accessible_spec_workspace_ids(db, subject_type, subject_id)
    if accessible is not None and workspace_id not in accessible:
        raise HTTPException(status_code=403, detail="Sem permissão para este workspace")


def _policy_http(exc: TaskStatusPolicyError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"policy": True, "code": exc.code, "message": exc.message},
    )


def _get_document_or_404(
    db: Session, workspace_id: UUID, document_type: DocumentType
) -> SpecDocument:
    doc = (
        db.query(SpecDocument)
        .filter(
            SpecDocument.workspace_id == workspace_id,
            SpecDocument.document_type == document_type,
        )
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    return doc


def _document_response(
    doc: SpecDocument,
    identities: Optional[dict[str, CreatorIdentity]] = None,
) -> DocumentResponse:
    data = DocumentResponse.model_validate(doc)
    identity = identity_for_actor(doc.updated_by, identities or {})
    if identity is not None:
        data.updated_by_display_name = identity.display_name
        data.updated_by_avatar_url = identity.avatar_url
    return data


def _task_members(db: Session, task_id: UUID) -> list[str]:
    from app.models import TaskMember

    rows = (
        db.query(TaskMember.member)
        .filter(TaskMember.task_id == task_id)
        .order_by(TaskMember.created_at.asc())
        .all()
    )
    return [r[0] for r in rows]


def _task_labels(db: Session, task_id: UUID) -> list[TaskLabelBrief]:
    from app.models import TaskCardLabel, TaskLabel

    rows = (
        db.query(TaskLabel)
        .join(TaskCardLabel, TaskCardLabel.label_id == TaskLabel.id)
        .filter(TaskCardLabel.task_id == task_id)
        .order_by(TaskLabel.name.asc())
        .all()
    )
    return [TaskLabelBrief(id=r.id, name=r.name, color=r.color) for r in rows]


def _task_checklist_counts(db: Session, task_id: UUID) -> tuple[int, int]:
    from app.models import TaskChecklist, TaskChecklistItem

    checklist_ids = [
        c.id
        for c in db.query(TaskChecklist.id).filter(TaskChecklist.task_id == task_id).all()
    ]
    if not checklist_ids:
        return 0, 0
    total = (
        db.query(func.count(TaskChecklistItem.id))
        .filter(TaskChecklistItem.checklist_id.in_(checklist_ids))
        .scalar()
        or 0
    )
    done = (
        db.query(func.count(TaskChecklistItem.id))
        .filter(
            TaskChecklistItem.checklist_id.in_(checklist_ids),
            TaskChecklistItem.is_completed.is_(True),
        )
        .scalar()
        or 0
    )
    return int(done), int(total)


def _task_attachment_count(db: Session, task_id: UUID) -> int:
    from app.models import TaskAttachment

    return int(
        db.query(func.count(TaskAttachment.id))
        .filter(TaskAttachment.task_id == task_id)
        .scalar()
        or 0
    )


def _task_response(
    task: TaskCard,
    identities: Optional[dict[str, CreatorIdentity]] = None,
    *,
    members: Optional[list[str]] = None,
    labels: Optional[list[TaskLabelBrief]] = None,
    checklist_done: Optional[int] = None,
    checklist_total: Optional[int] = None,
    attachment_count: Optional[int] = None,
) -> TaskResponse:
    data = TaskResponse.model_validate(task)
    identity = identity_for_actor(task.assignee, identities or {})
    if identity is not None:
        data.assignee_display_name = identity.display_name
        data.assignee_avatar_url = identity.avatar_url
    if members is not None:
        data.members = members
    if labels is not None:
        data.labels = labels
        data.label_ids = [label.id for label in labels]
    if checklist_done is not None:
        data.checklist_done = checklist_done
    if checklist_total is not None:
        data.checklist_total = checklist_total
    if attachment_count is not None:
        data.attachment_count = attachment_count
    # Só faz sentido para card ativo: nas demais colunas não há lease correndo.
    if task.status == TaskCardStatus.em_andamento:
        data.claim_expires_at = claim_expires_at(task.last_activity_at)
    return data


def _enrich_task(db: Session, task: TaskCard) -> TaskResponse:
    identities = resolve_actor_identities_with_db(db, [task.assignee])
    labels = _task_labels(db, task.id)
    done, total = _task_checklist_counts(db, task.id)
    return _task_response(
        task,
        identities,
        members=_task_members(db, task.id),
        labels=labels,
        checklist_done=done,
        checklist_total=total,
        attachment_count=_task_attachment_count(db, task.id),
    )


def get_or_create_workspace(
    db: Session,
    *,
    project_id: str,
    slug: str,
    name: str,
    created_by: Optional[str] = None,
    status: Optional[SpecWorkspaceStatus] = None,
    group_id: Optional[UUID] = None,
) -> tuple[SpecWorkspace, bool]:
    """Cria ou retorna o workspace de ``(project_id, slug)`` — idempotente.

    Lógica compartilhada entre o endpoint REST e a tool MCP (sem duplicação).
    Retorna ``(workspace, created)``; garante o Project no catálogo via
    ``upsert_project``. Em corrida, o perdedor do UNIQUE re-consulta o vencedor.
    """
    upsert_project(project_id, session=db)
    existing = (
        db.query(SpecWorkspace)
        .filter(SpecWorkspace.project_id == project_id, SpecWorkspace.slug == slug)
        .first()
    )
    if existing is not None:
        return existing, False

    ws = SpecWorkspace(
        project_id=project_id,
        slug=slug,
        name=name,
        status=status or SpecWorkspaceStatus.planejamento,
        created_by=created_by,
        group_id=group_id,
    )
    db.add(ws)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(SpecWorkspace)
            .filter(SpecWorkspace.project_id == project_id, SpecWorkspace.slug == slug)
            .first()
        )
        if existing is not None:
            return existing, False
        raise
    db.refresh(ws)
    return ws, True


def get_or_create_document(
    db: Session, workspace_id: UUID, document_type: DocumentType
) -> SpecDocument:
    """Retorna o ``SpecDocument`` do tipo dado no workspace, criando-o se ausente."""
    doc = (
        db.query(SpecDocument)
        .filter(
            SpecDocument.workspace_id == workspace_id,
            SpecDocument.document_type == document_type,
        )
        .first()
    )
    if doc is not None:
        return doc

    doc = SpecDocument(workspace_id=workspace_id, document_type=document_type)
    db.add(doc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        doc = (
            db.query(SpecDocument)
            .filter(
                SpecDocument.workspace_id == workspace_id,
                SpecDocument.document_type == document_type,
            )
            .first()
        )
        if doc is None:
            raise
        return doc
    db.refresh(doc)
    return doc


def _get_task_or_404(db: Session, task_id: UUID) -> TaskCard:
    task = db.query(TaskCard).filter(TaskCard.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task não encontrada")
    return task


def _resolve_comment_target_workspace(
    db: Session, target_type: CommentTargetType, target_id: UUID
) -> UUID:
    """Valida que o alvo existe e devolve o ``workspace_id`` para a checagem de acesso."""
    if target_type == CommentTargetType.workspace:
        ws = db.query(SpecWorkspace).filter(SpecWorkspace.id == target_id).first()
        if ws is None:
            raise HTTPException(status_code=404, detail="Alvo do comentário não encontrado")
        return ws.id
    if target_type == CommentTargetType.document:
        doc = db.query(SpecDocument).filter(SpecDocument.id == target_id).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="Alvo do comentário não encontrado")
        return doc.workspace_id
    task = db.query(TaskCard).filter(TaskCard.id == target_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Alvo do comentário não encontrado")
    return task.workspace_id


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #
@router.post("/workspaces", response_model=WorkspaceResponse)
def create_workspace(
    payload: WorkspaceCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    """Cria um workspace. Idempotente por ``(project_id, slug)``."""
    actor = resolve_spec_actor(body_actor=payload.created_by)
    subject_type, subject_id = resolve_spec_subject()
    group_id = None
    
    if subject_id:
        from app.models import User
        user = db.query(User.group_id).filter(User.id == subject_id).first()
        if user:
            group_id = user[0]
            
    if not group_id and subject_type == "user" and not subject_id:
        if actor:
            from app.utils.machine_resolver import find_legacy_host_user
            u = find_legacy_host_user(db, actor)
            if u and u.group_id:
                group_id = u.group_id

    ws, created = get_or_create_workspace(
        db,
        project_id=payload.project_id,
        slug=payload.slug,
        name=payload.name,
        created_by=actor,
        status=payload.status,
        group_id=group_id,
    )
    response.status_code = 201 if created else 200
    from app.utils.planka_hooks import mirror_ensure_workspace

    try:
        mirror_ensure_workspace(db, ws.id)
    except HTTPException:
        # Workspace Spec já existe; espelho é obrigatório quando sync ligado.
        raise
    return ws


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def update_workspace_status(
    workspace_id: UUID,
    payload: WorkspaceStatusUpdate,
    db: Session = Depends(get_db),
) -> WorkspaceResponse:
    """Atualiza o status do workspace. Em transição para ``concluido``, indexa docs.

    Transições para ``concluido``/``arquivado`` gravam ``completed_at``/
    ``archived_at`` e espelham o resultado no PLANKA (Tarefa
    kanban-archive-lifecycle) — ver ``apply_status_change``.
    """
    ws = _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    previous = ws.status
    ws = apply_status_change(
        db, ws, payload.status, actor=resolve_spec_actor(body_actor=payload.actor)
    )

    if (
        payload.status == SpecWorkspaceStatus.concluido
        and previous != SpecWorkspaceStatus.concluido
    ):
        try:
            index_completed_workspace(db, ws)
        except Exception:  # noqa: BLE001 — indexação não deve falhar o PATCH
            import logging

            logging.getLogger(__name__).exception(
                "falha ao indexar workspace concluído %s", workspace_id
            )

    return ws


def _build_summaries(
    db: Session, workspaces: list[SpecWorkspace]
) -> list[WorkspaceSummaryResponse]:
    """Monta os resumos (workspace + contagem de tasks por status).

    Contagem computada em uma única query agregada (``GROUP BY workspace_id,
    status``) — sem N+1, independentemente de quantos workspaces.
    """
    ws_ids = [w.id for w in workspaces]
    counts: dict[UUID, dict[str, int]] = {}
    if ws_ids:
        rows = (
            db.query(TaskCard.workspace_id, TaskCard.status, func.count().label("c"))
            .filter(TaskCard.workspace_id.in_(ws_ids), TaskCard.archived_at.is_(None))
            .group_by(TaskCard.workspace_id, TaskCard.status)
            .all()
        )
        for ws_id, status, count in rows:
            counts.setdefault(ws_id, {})[status.value] = count

    return [
        WorkspaceSummaryResponse(
            id=w.id,
            project_id=w.project_id,
            slug=w.slug,
            name=w.name,
            status=w.status,
            task_counts=counts.get(w.id, {}),
        )
        for w in workspaces
    ]


def _actor_group_id(db: Session) -> Optional[UUID]:
    """Resolve o grupo do ator autenticado (fail-closed).

    Ordem: subject da sessão (``auth_user_var``) → fallback para hostname
    legado/agent (``resolve_spec_actor`` + ``find_legacy_host_user``). Retorna
    ``None`` quando não há grupo resolvido (o chamador bloqueia o acesso).
    """
    subject_type, subject_id = resolve_spec_subject()
    from app.models import DEFAULT_GROUP_NAME, Group, User
    from app.utils.logging_context import auth_email_var, auth_user_var
    from app.utils.logging_context import auth_method_var

    if subject_id:
        user = db.query(User.group_id).filter(User.id == subject_id).first()
        if user and user[0] is not None:
            return user[0]
    # Alguns emissores de sessão legados usam o e-mail/``user_id`` como sub,
    # em vez do UUID técnico. Resolva ambos sem relaxar o fail-closed.
    identity_values = {
        value.strip()
        for value in (auth_user_var.get(), auth_email_var.get())
        if value and value.strip()
    }
    if identity_values:
        user = (
            db.query(User.group_id)
            .filter(
                (User.email.in_(identity_values))
                | (User.user_id.in_(identity_values))
            )
            .first()
        )
        if user:
            return user[0]
    actor = resolve_spec_actor()
    if actor:
        from app.utils.machine_resolver import find_legacy_host_user

        u = find_legacy_host_user(db, actor)
        if u and u.group_id:
            return u.group_id
    # A UI LAN sem Google usa ``Bearer local``. Nesse modo não há UUID/e-mail
    # no contexto; o grupo Default é a identidade explícita do ambiente legado.
    if auth_method_var.get() == "legacy":
        default_group = (
            db.query(Group).filter(Group.name.ilike(DEFAULT_GROUP_NAME)).first()
        )
        if default_group:
            return default_group.id
    return None


def accessible_workspace_ids_by_group(db: Session) -> Optional[set]:
    """IDs de ``SpecWorkspace`` visíveis ao ator: interseção grupo ∩ ACL.

    Fail-closed: ator sem grupo resolvido → ``set()`` (nada visível). A restrição
    por grupo é a fonte da verdade do isolamento (kanban-board-group-isolation);
    a ACL por usuário (``get_accessible_spec_workspace_ids``) refina quando há
    regras (``None`` = aberto).
    """
    if is_legacy_spec_access_open():
        return None
    group_id = _actor_group_id(db)
    if group_id is None:
        return set()
    visible = {
        ws_id
        for (ws_id,) in db.query(SpecWorkspace.id).filter(SpecWorkspace.group_id == group_id)
    }
    subject_type, subject_id = resolve_spec_subject()
    accessible = get_accessible_spec_workspace_ids(db, subject_type, subject_id)
    if accessible is not None:
        visible &= accessible
    return visible


def _filter_accessible(
    db: Session,
    workspaces: list[SpecWorkspace],
) -> list[SpecWorkspace]:
    allowed = accessible_workspace_ids_by_group(db)
    if allowed is None:
        return workspaces
    return [w for w in workspaces if w.id in allowed]


@router.get("/workspaces", response_model=list[WorkspaceSummaryResponse])
def list_all_workspaces(
    slug: Optional[str] = Query(
        None, description="Filtra por slug, em qualquer projeto"
    ),
    db: Session = Depends(get_db),
) -> list[WorkspaceSummaryResponse]:
    """Índice global: todos os workspaces acessíveis (de todos os projetos).

    Alimenta a tela inicial de Specs (lista os quadros agrupados por projeto).

    Com ``slug``, resolve a descoberta entre projetos: o ``project_id`` do cliente
    segue o nome do diretório de trabalho, então uma feature que toca quatro
    repositórios tem o workspace sob um único ``project_id`` e, nos outros três, a
    busca por projeto devolve vazio — silenciosamente, levando o agente a concluir
    que não há spec (ou pior, a criar um segundo workspace e fragmentar a spec).
    """
    query = db.query(SpecWorkspace)
    if slug:
        query = query.filter(SpecWorkspace.slug == slug)
    workspaces = query.order_by(SpecWorkspace.project_id).all()
    workspaces = _filter_accessible(db, workspaces)
    return _build_summaries(db, workspaces)


@router.get(
    "/projects/{project_id}/workspaces",
    response_model=list[WorkspaceSummaryResponse],
)
def list_project_workspaces(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[WorkspaceSummaryResponse]:
    """Painel de Projeto: workspaces + contagem de tasks por status.

    Progresso resumido computado em uma única query agregada
    (``GROUP BY workspace_id, status``) — sem N+1.
    """
    workspaces = (
        db.query(SpecWorkspace)
        .filter(SpecWorkspace.project_id == project_id)
        .all()
    )
    workspaces = _filter_accessible(db, workspaces)
    return _build_summaries(db, workspaces)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceBoardResponse)
def get_workspace_board(
    workspace_id: UUID,
    db: Session = Depends(get_db),
) -> WorkspaceBoardResponse:
    """Quadro completo do workspace: documentos + tasks."""
    ws = _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    documents = (
        db.query(SpecDocument)
        .filter(SpecDocument.workspace_id == workspace_id)
        .all()
    )
    tasks = (
        db.query(TaskCard)
        .filter(TaskCard.workspace_id == workspace_id, TaskCard.archived_at.is_(None))
        .order_by(TaskCard.position.asc(), TaskCard.created_at.asc())
        .all()
    )
    identities = resolve_actor_identities_with_db(
        db,
        [t.assignee for t in tasks] + [d.updated_by for d in documents],
    )
    return WorkspaceBoardResponse(
        workspace=ws,
        documents=[_document_response(d, identities) for d in documents],
        tasks=[_enrich_task(db, t) for t in tasks],
    )


class PlankaEmbedResponse(BaseModel):
    workspace_id: UUID
    board_id: str
    project_id: Optional[str] = None
    embed_url: str
    access_token: str


class KanbanHomeResponse(BaseModel):
    embed_url: str
    access_token: str


class KanbanBoardResponse(BaseModel):
    board_id: str
    embed_url: str
    access_token: str


def _kanban_public_base() -> str:
    import os

    return (
        os.getenv("PLANKA_PUBLIC_URL")
        or os.getenv("PLANKA_BASE_URL")
        or "http://127.0.0.1:8765/planka"
    ).rstrip("/")


def _kanban_board_id_ok(board_id: str) -> bool:
    return bool(board_id) and board_id.isdigit() and len(board_id) <= 32


def _issue_kanban_access_token(db: Session) -> str:
    """JWT Mem0 para o SPA Kanban (nome/e-mail/foto da pessoa logada).

    O embed dura pelo menos o TTL da sessão da UI mais uma hora; isso evita que
    o quadro expire antes da sessão principal. ``PLANKA_EMBED_TOKEN_TTL_SECONDS``
    pode aumentar esse prazo, nunca reduzi-lo abaixo do TTL da UI.
    """
    import os

    def _positive_seconds(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    ui_ttl = _positive_seconds("AUTH_JWT_TTL_SECONDS", 7 * 24 * 60 * 60)
    embed_ttl = _positive_seconds("PLANKA_EMBED_TOKEN_TTL_SECONDS", 8 * 24 * 60 * 60)
    embed_ttl = max(embed_ttl, ui_ttl + 60 * 60)

    import jwt as pyjwt

    from app.models import User as AppUser
    from app.utils.logging_context import auth_email_var, auth_method_var, auth_user_var

    legacy_shared_mode = auth_method_var.get() == "legacy"
    actor_group_id = None if legacy_shared_mode else _actor_group_id(db)
    # Com autenticação de pessoa, o isolamento continua fail-closed. No modo
    # legado da LAN, ``*`` instrui o PLANKA a exibir todos os quadros.
    if not legacy_shared_mode and actor_group_id is None:
        raise HTTPException(status_code=403, detail="Usuário sem grupo associado")

    actor = resolve_spec_actor() or "ui-user"
    email = (auth_email_var.get() or "").strip()
    name = ""
    picture = ""

    person_id = (auth_user_var.get() or "").strip()
    user = None
    if person_id:
        try:
            from uuid import UUID as _UUID

            user = db.query(AppUser).filter(AppUser.id == _UUID(person_id)).first()
        except (ValueError, TypeError):
            user = None
        if user is None:
            user = (
                db.query(AppUser)
                .filter(
                    (AppUser.user_id == person_id)
                    | (AppUser.email == person_id)
                )
                .first()
            )
    if user is None and email:
        user = db.query(AppUser).filter(AppUser.email == email).first()
    if user is not None:
        email = (user.email or email or "").strip()
        name = (user.display_name or user.name or email or actor).strip()
        picture = (user.avatar_url or "").strip()
    if not name:
        name = (email.split("@")[0] if email else actor).strip() or actor

    secret = (
        os.getenv("AUTH_JWT_SECRET") or os.getenv("NEXTAUTH_SECRET") or ""
    ).strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="AUTH_JWT_SECRET necessário para embed Kanban (não usar INTERNAL)",
        )

    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    sub = email or person_id or actor
    access_token = pyjwt.encode(
        {
            "sub": sub,
            "email": email or f"{sub}@mem0.local",
            "name": name,
            "picture": picture,
            "group": "*" if legacy_shared_mode else str(actor_group_id),
            # Marca embed Mem0: current-user do PLANKA não trata como sessão nativa.
            "mem0": True,
            "iat": now,
            "exp": now + timedelta(seconds=embed_ttl),
        },
        secret,
        algorithm="HS256",
    )
    if isinstance(access_token, bytes):
        access_token = access_token.decode("utf-8")
    return access_token


@router.get("/kanban-home", response_model=KanbanHomeResponse)
def get_kanban_home(db: Session = Depends(get_db)) -> KanbanHomeResponse:
    """Home do SPA Kanban (ADR-008) — raiz do board, não um workspace isolado."""
    return KanbanHomeResponse(
        embed_url=f"{_kanban_public_base()}/",
        access_token=_issue_kanban_access_token(db),
    )


@router.get("/kanban-boards/{board_id}", response_model=KanbanBoardResponse)
def get_kanban_board(board_id: str, db: Session = Depends(get_db)) -> KanbanBoardResponse:
    """Deep-link de um quadro Kanban (URL compartilhável /docs/boards/:id)."""
    if not _kanban_board_id_ok(board_id):
        raise HTTPException(status_code=400, detail="board_id inválido")
        
    from app.models import SpecPlankaIdMap
    from app.utils.planka import ENTITY_BOARD
    
    row = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_BOARD,
            SpecPlankaIdMap.planka_id == board_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Quadro Kanban não mapeado")
        
    _assert_access(db, row.spec_id)
        
    return KanbanBoardResponse(
        board_id=board_id,
        embed_url=f"{_kanban_public_base()}/boards/{board_id}",
        access_token=_issue_kanban_access_token(db),
    )


@router.get(
    "/workspaces/{workspace_id}/planka-embed",
    response_model=PlankaEmbedResponse,
)
def get_planka_embed(
    workspace_id: UUID,
    db: Session = Depends(get_db),
) -> PlankaEmbedResponse:
    """URL + token para deep-link de um board Spec no Kanban (compat ADR-007)."""
    from app.models import SpecPlankaIdMap
    from app.utils.planka import (
        ENTITY_BOARD,
        ENTITY_PROJECT,
        PlankaMirrorError,
        PlankaMirrorHttpClient,
    )
    from app.utils.planka_hooks import _run_async, mirror_sync_enabled

    _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    if mirror_sync_enabled():
        client = PlankaMirrorHttpClient(db)
        try:
            board_id = _run_async(client.ensure_workspace_board(workspace_id))
        except PlankaMirrorError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "mirror_failed": True,
                    "action": "ensure_workspace_board",
                    "planka_status": exc.status_code,
                    "detail": exc.detail,
                },
            ) from exc
    else:
        row = (
            db.query(SpecPlankaIdMap)
            .filter(
                SpecPlankaIdMap.entity_type == ENTITY_BOARD,
                SpecPlankaIdMap.spec_id == workspace_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Board Kanban ainda não mapeado. Ative PLANKA_MIRROR_SYNC=1 "
                    "ou rode POST /admin/planka/resync."
                ),
            )
        board_id = row.planka_id

    project_row = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_PROJECT,
            SpecPlankaIdMap.spec_id == workspace_id,
        )
        .first()
    )

    return PlankaEmbedResponse(
        workspace_id=workspace_id,
        board_id=board_id,
        project_id=project_row.planka_id if project_row else None,
        embed_url=f"{_kanban_public_base()}/boards/{board_id}",
        access_token=_issue_kanban_access_token(db),
    )


class PlankaCardMoveRequest(BaseModel):
    planka_card_id: str
    planka_list_id: str
    actor: Optional[str] = None


class PlankaCardMoveResponse(BaseModel):
    applied: bool
    reason: Optional[str] = None
    action: Optional[str] = None
    task_id: Optional[str] = None
    status: Optional[str] = None
    version: Optional[int] = None


class PlankaCardUpdateRequest(BaseModel):
    planka_card_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    position: Optional[float] = None
    changed_fields: list[str]
    actor: Optional[str] = None


class PlankaCardUpdateResponse(BaseModel):
    applied: bool
    reason: Optional[str] = None
    task_id: Optional[str] = None
    version: Optional[int] = None


def _assert_planka_bridge_token(authorization: Optional[str]) -> None:
    import os

    expected = (
        os.getenv("PLANKA_INTERNAL_ACCESS_TOKEN")
        or os.getenv("INTERNAL_ACCESS_TOKEN")
        or ""
    ).strip()
    if not expected:
        raise HTTPException(status_code=503, detail="bridge token não configurado")
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw or raw != expected:
        raise HTTPException(status_code=401, detail="bridge unauthorized")


@router.post(
    "/planka/card-moved",
    response_model=PlankaCardMoveResponse,
)
def planka_card_moved(
    payload: PlankaCardMoveRequest,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> PlankaCardMoveResponse:
    """Bridge PLANKA → Spec (ADR-007): aplica claim/release/status após move humano."""
    from app.utils.planka_bridge import (
        PlankaBridgeError,
        apply_planka_card_move,
        reconcile_planka_board_lifecycle,
    )

    _assert_planka_bridge_token(authorization)
    actor = (payload.actor or resolve_spec_actor() or "ui-user").strip()
    try:
        result = apply_planka_card_move(
            db,
            planka_card_id=payload.planka_card_id.strip(),
            planka_list_id=payload.planka_list_id.strip(),
            actor=actor,
        )
    except PlankaBridgeError as exc:
        lifecycle = reconcile_planka_board_lifecycle(
            db,
            planka_card_id=payload.planka_card_id.strip(),
            actor=actor,
        )
        if lifecycle and lifecycle["completed"]:
            return PlankaCardMoveResponse(
                applied=True,
                reason="workspace_completed",
                status="concluido",
            )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "detail": exc.detail},
        ) from exc
    reconcile_planka_board_lifecycle(
        db,
        planka_card_id=payload.planka_card_id.strip(),
        actor=actor,
    )
    return PlankaCardMoveResponse(**result)


@router.post("/planka/card-updated", response_model=PlankaCardUpdateResponse)
def planka_card_updated(
    payload: PlankaCardUpdateRequest,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> PlankaCardUpdateResponse:
    """Bridge PLANKA → Spec para conteúdo editado por uma pessoa na UI."""
    from app.utils.planka_bridge import PlankaBridgeError, apply_planka_card_update

    _assert_planka_bridge_token(authorization)
    try:
        result = apply_planka_card_update(
            db,
            planka_card_id=payload.planka_card_id.strip(),
            changed_fields=set(payload.changed_fields),
            name=payload.name,
            description=payload.description,
            due_date=payload.due_date,
            position=payload.position,
        )
    except PlankaBridgeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "detail": exc.detail},
        ) from exc
    return PlankaCardUpdateResponse(**result)


class PlankaCardCreateRequest(BaseModel):
    planka_card_id: str
    planka_list_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    position: Optional[float] = None
    actor: Optional[str] = None


def planka_ui_import_enabled() -> bool:
    """Kill switch do import PLANKA → Spec (``PLANKA_IMPORT_UI_CARDS=0`` desliga)."""
    import os

    raw = (os.getenv("PLANKA_IMPORT_UI_CARDS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


@router.post("/planka/card-created", response_model=PlankaCardMoveResponse)
def planka_card_created(
    payload: PlankaCardCreateRequest,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> PlankaCardMoveResponse:
    """Bridge PLANKA → Spec: card criado por uma pessoa (sessão JWT) na tela do PLANKA.

    Cria a ``TaskCard`` no workspace dono da lista, com status = coluna e sem
    assignee. Idempotente: card já mapeado → ``already_mapped``. Listas não
    mapeadas/SDD → ``applied=False``.
    """
    from app.utils.planka import is_planka_id
    from app.utils.planka_import import import_planka_card

    _assert_planka_bridge_token(authorization)
    if not planka_ui_import_enabled():
        return PlankaCardMoveResponse(applied=False, reason="import_disabled")
    # Valida o valor bruto (sem strip): o PLANKA envia String(id); "12\n" é inválido.
    if not (is_planka_id(payload.planka_card_id) and is_planka_id(payload.planka_list_id)):
        return PlankaCardMoveResponse(applied=False, reason="invalid_id")
    result = import_planka_card(
        db,
        planka_card_id=payload.planka_card_id,
        planka_list_id=payload.planka_list_id,
        name=payload.name,
        description=payload.description,
        due_date=payload.due_date,
        position=payload.position,
        actor=(payload.actor or resolve_spec_actor() or "planka-ui").strip(),
    )
    return PlankaCardMoveResponse(**result)


class PlankaProjectLifecycleRequest(BaseModel):
    planka_project_id: str
    is_archived: bool
    is_completed: bool
    actor: Optional[str] = None


class PlankaProjectLifecycleResponse(BaseModel):
    applied: bool
    skipped: bool = False
    workspace_id: Optional[str] = None
    status: Optional[str] = None


@router.post(
    "/planka/project-lifecycle",
    response_model=PlankaProjectLifecycleResponse,
)
def planka_project_lifecycle(
    payload: PlankaProjectLifecycleRequest,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> PlankaProjectLifecycleResponse:
    """Bridge PLANKA → Spec (Tarefa kanban-archive-lifecycle).

    Chamado por ``notify-spec-project-lifecycle.js`` depois que um humano
    arquiva/desarquiva ou marca um projeto como concluído direto no board
    PLANKA. Resolve o ``SpecWorkspace`` pelo mapa inverso e aplica a mesma
    ``apply_status_change`` do PATCH — Spec continua a única fonte de verdade,
    isso só traduz a ação humana de volta para o status Spec.
    """
    from app.models import SpecPlankaIdMap
    from app.utils.planka import ENTITY_PROJECT

    _assert_planka_bridge_token(authorization)

    project_map = (
        db.query(SpecPlankaIdMap)
        .filter(
            SpecPlankaIdMap.entity_type == ENTITY_PROJECT,
            SpecPlankaIdMap.planka_id == payload.planka_project_id.strip(),
        )
        .first()
    )
    if project_map is None:
        # Projeto PLANKA sem workspace Spec mapeado (ex.: criado direto no
        # PLANKA fora do fluxo Spec) — ignora, não há o que sincronizar.
        return PlankaProjectLifecycleResponse(applied=False, skipped=True)

    ws = _get_workspace_or_404(db, project_map.spec_id)
    actor = (payload.actor or resolve_spec_actor() or "planka-ui").strip()

    if payload.is_archived:
        new_status = SpecWorkspaceStatus.arquivado
    elif payload.is_completed:
        new_status = SpecWorkspaceStatus.concluido
    else:
        new_status = SpecWorkspaceStatus.ativo

    if new_status != ws.status:
        ws = apply_status_change(db, ws, new_status, actor=actor)

    return PlankaProjectLifecycleResponse(
        applied=True, workspace_id=str(ws.id), status=ws.status.value
    )


# --------------------------------------------------------------------------- #
# Documentos
# --------------------------------------------------------------------------- #
@router.put(
    "/workspaces/{workspace_id}/documents/{document_type}",
    response_model=DocumentWriteResponse,
)
def write_workspace_document(
    workspace_id: UUID,
    document_type: DocumentType,
    payload: DocumentWriteRequest,
    db: Session = Depends(get_db),
) -> DocumentWriteResponse:
    """Grava uma nova versão do documento. 409 em conflito de versão (ADR-005)."""
    # Sem atribuição: a função existe pelo 404 que ela levanta. O ``ws`` era usado
    # pela indexação síncrona, que saiu do caminho de request em ad77a0e9.
    _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    doc = get_or_create_document(db, workspace_id, document_type)

    author = resolve_spec_actor(body_actor=payload.author)
    result = write_document_version(
        db,
        doc.id,
        payload.content,
        payload.expected_version,
        author,
        DocumentOrigin.api,
    )

    if result.conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "current_version": result.version,
                "current_content": result.current_content,
            },
        )

    # Index off the request path (Ollama embed can take >30s). PLANKA mirror stays
    # synchronous so API clients still get 502 when the sidecar is down (ADR-006).
    from app.utils.spec_side_effects import schedule_document_post_write

    schedule_document_post_write(workspace_id, document_type.value, mirror=False)

    from app.utils.planka_hooks import mirror_document

    mirror_document(db, workspace_id, document_type.value)

    return DocumentWriteResponse(
        document_id=result.document_id,
        version=result.version,
        conflict=False,
    )


@router.get(
    "/workspaces/{workspace_id}/documents/{document_type}/versions",
    response_model=list[VersionResponse],
)
def list_document_versions(
    workspace_id: UUID,
    document_type: DocumentType,
    db: Session = Depends(get_db),
) -> list[VersionResponse]:
    """Histórico de versões (snapshots) de um documento, em ordem crescente."""
    _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)
    doc = _get_document_or_404(db, workspace_id, document_type)

    return (
        db.query(SpecDocumentVersion)
        .filter(SpecDocumentVersion.document_id == doc.id)
        .order_by(SpecDocumentVersion.version.asc())
        .all()
    )


@router.delete(
    "/workspaces/{workspace_id}/documents/{document_type}",
    status_code=204,
)
def delete_workspace_document(
    workspace_id: UUID,
    document_type: DocumentType,
    db: Session = Depends(get_db),
) -> Response:
    """Remove o documento do tipo dado e todo o histórico de versões."""
    _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)
    doc = _get_document_or_404(db, workspace_id, document_type)

    db.query(SpecComment).filter(
        SpecComment.target_type == CommentTargetType.document,
        SpecComment.target_id == doc.id,
    ).delete()
    db.query(SpecDocumentVersion).filter(
        SpecDocumentVersion.document_id == doc.id
    ).delete()
    db.delete(doc)
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
@router.post("/tasks", response_model=TaskResponse, status_code=201)
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Cria uma task; nasce na coluna ``tasks`` (backlog)."""
    _get_workspace_or_404(db, payload.workspace_id)
    _assert_access(db, payload.workspace_id)

    task = TaskCard(
        workspace_id=payload.workspace_id,
        title=payload.title,
        description=payload.description,
        branch_ref=payload.branch_ref,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    from app.utils.planka_hooks import mirror_task

    try:
        mirror_task(db, task.id)
    except HTTPException:
        # Compensação: remove o card Spec se o espelho falhar (ADR-006).
        db.query(TaskStatusHistory).filter(TaskStatusHistory.task_id == task.id).delete()
        db.delete(task)
        db.commit()
        raise
    return _enrich_task(db, task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Card completo, incluindo ``description`` e ``version``.

    O corpo enriquecido da tarefa (requisitos, subtarefas, arquivos relevantes,
    entregáveis, casos de teste, critérios de aceite) vive em ``description``. Sem
    esta leitura o card é gravável e movível, mas não legível: quem não chamou o
    ``create_task`` não tem acesso a nada disso.
    """
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)
    return _enrich_task(db, task)


@router.get("/tasks/{task_id}/history", response_model=list[TaskHistoryResponse])
def list_task_history(
    task_id: UUID,
    db: Session = Depends(get_db),
) -> list[TaskHistoryResponse]:
    """Histórico de mudanças de coluna do card, em ordem cronológica.

    Torna observável a liberação automática por inatividade: ela grava
    ``changed_by`` = ``system:timeout``, enquanto um ``release_task`` manual grava
    o ator humano. Sem esta leitura, um card que voltou sozinho ao backlog era
    indistinguível de um que alguém devolveu — o assignee só descobria ao tentar
    avançar e receber ``use_claim``.
    """
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    rows = (
        db.query(TaskStatusHistory)
        .filter(TaskStatusHistory.task_id == task_id)
        .order_by(TaskStatusHistory.changed_at.asc())
        .all()
    )
    return [
        TaskHistoryResponse(
            id=r.id,
            old_status=r.old_status,
            new_status=r.new_status,
            changed_by=r.changed_by,
            changed_at=r.changed_at,
            by_timeout=(r.changed_by == TIMEOUT_ACTOR),
        )
        for r in rows
    ]


@router.get("/workspaces/{workspace_id}/tasks", response_model=list[TaskResponse])
def list_workspace_tasks(
    workspace_id: UUID,
    status: Optional[TaskCardStatus] = Query(
        None, description="Filtra por coluna do Kanban"
    ),
    include_archived: bool = Query(
        False, description="Inclui cards arquivados (escondidos por padrão)"
    ),
    db: Session = Depends(get_db),
) -> list[TaskResponse]:
    """Cards do workspace, opcionalmente filtrados por coluna.

    Caminho programático do workspace até um card. Sem isto o ``task_id`` só chega
    ao agente se um humano copiar da UI web, e ``claim_task`` exige um.

    ``version`` vem em cada item de propósito: ``update_task_status`` exige
    ``expected_version``, e sem ele aqui todo avanço de coluna custaria uma leitura
    extra.
    """
    _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    query = db.query(TaskCard).filter(TaskCard.workspace_id == workspace_id)
    if not include_archived:
        query = query.filter(TaskCard.archived_at.is_(None))
    if status is not None:
        query = query.filter(TaskCard.status == status)
    tasks = query.order_by(TaskCard.created_at.asc()).all()
    return [_enrich_task(db, t) for t in tasks]


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Atualiza título/descrição/branch/due/position com OCC atômica (ADR-005)."""
    from app.models import TaskMember

    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    # Members: aplicar após OCC bem-sucedido (mesma versão esperada).
    # due_at só muda se veio no payload ou clear_due_at=true (sentinel ...).
    due_kwargs: dict = {}
    if payload.clear_due_at:
        due_kwargs["clear_due_at"] = True
    elif "due_at" in payload.model_fields_set:
        due_kwargs["due_at"] = payload.due_at

    result = update_task_metadata(
        db,
        task_id,
        payload.expected_version,
        title=payload.title,
        description=payload.description,
        branch_ref=payload.branch_ref,
        position=payload.position,
        **due_kwargs,
    )
    if result.conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "current_version": result.version,
                "title": result.title,
                "description": result.description,
            },
        )

    if payload.members is not None:
        db.query(TaskMember).filter(TaskMember.task_id == task_id).delete()
        for member in payload.members:
            name = (member or "").strip()
            if not name:
                continue
            db.add(TaskMember(task_id=task_id, member=name))
        db.commit()
        from app.utils.planka_hooks import mirror_task

        mirror_task(db, task_id)

    db.refresh(task)
    return _enrich_task(db, task)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Remove a task e o histórico de status associado."""
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    from app.models import (
        TaskAttachment,
        TaskCardLabel,
        TaskChecklist,
        TaskChecklistItem,
        TaskMember,
    )
    from app.utils.planka_hooks import mirror_delete_task
    from app.utils.spec_attachments import delete_file

    mirror_delete_task(db, task_id)
    checklist_ids = [
        c.id
        for c in db.query(TaskChecklist.id).filter(TaskChecklist.task_id == task_id).all()
    ]
    if checklist_ids:
        db.query(TaskChecklistItem).filter(
            TaskChecklistItem.checklist_id.in_(checklist_ids)
        ).delete(synchronize_session=False)
    db.query(TaskChecklist).filter(TaskChecklist.task_id == task_id).delete()
    db.query(TaskCardLabel).filter(TaskCardLabel.task_id == task_id).delete()
    db.query(TaskMember).filter(TaskMember.task_id == task_id).delete()
    for att in db.query(TaskAttachment).filter(TaskAttachment.task_id == task_id).all():
        try:
            delete_file(att.storage_key)
        except OSError:
            pass
        db.delete(att)
    db.query(TaskStatusHistory).filter(TaskStatusHistory.task_id == task_id).delete()
    db.query(SpecComment).filter(
        SpecComment.target_type == CommentTargetType.task,
        SpecComment.target_id == task_id,
    ).delete()
    db.delete(task)
    db.commit()
    return Response(status_code=204)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(
    workspace_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Exclui definitivamente uma Tarefa (workspace) e TODOS os seus filhos:
    documentos, versões, cards de task, histórico, auditoria e comentários.

    Irreversível. Atinge SOMENTE as tabelas de specs — memórias/Qdrant intactas.
    """
    ws = _get_workspace_or_404(db, workspace_id)
    _assert_access(db, workspace_id)

    doc_ids = [
        d.id
        for d in db.query(SpecDocument.id)
        .filter(SpecDocument.workspace_id == workspace_id)
        .all()
    ]
    task_ids = [
        t.id
        for t in db.query(TaskCard.id)
        .filter(TaskCard.workspace_id == workspace_id)
        .all()
    ]

    # Ordem FK-safe: filhos antes dos pais.
    if doc_ids:
        db.query(SpecDocumentVersion).filter(
            SpecDocumentVersion.document_id.in_(doc_ids)
        ).delete(synchronize_session=False)
    db.query(SpecDocument).filter(
        SpecDocument.workspace_id == workspace_id
    ).delete(synchronize_session=False)

    if task_ids:
        db.query(TaskStatusHistory).filter(
            TaskStatusHistory.task_id.in_(task_ids)
        ).delete(synchronize_session=False)
    db.query(TaskCard).filter(
        TaskCard.workspace_id == workspace_id
    ).delete(synchronize_session=False)

    db.query(SpecAuditLog).filter(
        SpecAuditLog.workspace_id == workspace_id
    ).delete(synchronize_session=False)
    db.query(SpecComment).filter(
        SpecComment.target_id.in_([workspace_id, *doc_ids, *task_ids])
    ).delete(synchronize_session=False)

    db.delete(ws)
    db.commit()
    return Response(status_code=204)


@router.post("/tasks/{task_id}/claim", response_model=TaskResponse)
def claim_task_endpoint(
    task_id: UUID,
    payload: ClaimRequest,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Assume a task. 409 se já ativa com outro responsável (ADR-003)."""
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    claimant = resolve_spec_actor(body_actor=payload.claimant)
    if not claimant:
        raise HTTPException(status_code=400, detail="claimant obrigatório")
    result = claim_task(db, task_id, claimant)
    if not result.claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "claimed": False,
                "current_assignee": result.current_assignee,
                "version": result.version,
                "archived": result.archived,
            },
        )
    db.refresh(task)
    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return _enrich_task(db, task)


@router.post("/tasks/{task_id}/release", response_model=TaskResponse)
def release_task_endpoint(
    task_id: UUID,
    payload: ReleaseRequest,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Libera a task manualmente: volta a ``tasks`` e limpa assignee/bloqueio."""
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    actor = resolve_spec_actor(body_actor=payload.actor)
    try:
        release_task(db, task_id, actor, payload.reason)
    except TaskStatusPolicyError as exc:
        raise _policy_http(exc) from exc
    db.refresh(task)
    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return _enrich_task(db, task)


def _apply_archive(
    db: Session, task_id: UUID, payload: ArchiveRequest, *, archive: bool
) -> TaskResponse:
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    actor = resolve_spec_actor(body_actor=payload.actor)
    try:
        if archive:
            result = archive_task(
                db, task_id, payload.expected_version, actor, reason=payload.reason
            )
        else:
            result = unarchive_task(db, task_id, payload.expected_version, actor)
    except TaskStatusPolicyError as exc:
        raise _policy_http(exc) from exc
    if result.conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "current_version": result.version,
                "archived_at": (
                    result.archived_at.isoformat() if result.archived_at else None
                ),
            },
        )
    db.refresh(task)
    return _enrich_task(db, task)


@router.post("/tasks/{task_id}/archive", response_model=TaskResponse)
def archive_task_endpoint(
    task_id: UUID,
    payload: ArchiveRequest,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Arquiva o card sem apagar nada (alternativa não destrutiva ao DELETE).

    Some da listagem padrão e do quadro; histórico de status e comentários são
    preservados. 409 em conflito de versão, card já arquivado, ou card ativo de
    outro assignee (exclusividade do claim — ADR-003).
    """
    return _apply_archive(db, task_id, payload, archive=True)


@router.post("/tasks/{task_id}/unarchive", response_model=TaskResponse)
def unarchive_task_endpoint(
    task_id: UUID,
    payload: ArchiveRequest,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Desarquiva o card: volta à listagem na mesma coluna em que estava."""
    return _apply_archive(db, task_id, payload, archive=False)


@router.patch("/tasks/{task_id}/status", response_model=TaskResponse)
def patch_task_status(
    task_id: UUID,
    payload: StatusPatchRequest,
    db: Session = Depends(get_db),
) -> TaskResponse:
    """Muda a coluna e/ou o marcador de bloqueio com concorrência otimista.

    ``new_status`` omitido mantém a coluna atual (usado para reportar bloqueio =
    ``is_blocked=true`` sem mudar de coluna — ADR-007). 409 em conflito de versão
    ou violação de política (use claim/release para entrar/sair de em_andamento).
    """
    task = _get_task_or_404(db, task_id)
    _assert_access(db, task.workspace_id)

    target_status = payload.new_status or task.status
    actor = resolve_spec_actor(body_actor=payload.actor)
    try:
        result = update_task_status(
            db,
            task_id,
            target_status,
            payload.expected_version,
            actor,
            is_blocked=payload.is_blocked,
            block_reason=payload.block_reason,
        )
    except TaskStatusPolicyError as exc:
        raise _policy_http(exc) from exc
    if result.conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "current_version": result.version,
                "current_status": result.status,
            },
        )
    db.refresh(task)
    from app.utils.planka_hooks import mirror_task_status

    mirror_task_status(db, task_id)
    return _enrich_task(db, task)


# --------------------------------------------------------------------------- #
# Comentários
# --------------------------------------------------------------------------- #
@router.post("/comments", response_model=CommentResponse, status_code=201)
def create_comment(
    payload: CommentCreate,
    db: Session = Depends(get_db),
) -> CommentResponse:
    """Adiciona comentário a workspace/documento/task (valida o alvo antes)."""
    workspace_id = _resolve_comment_target_workspace(db, payload.target_type, payload.target_id)
    _assert_access(db, workspace_id)

    comment = SpecComment(
        target_type=payload.target_type,
        target_id=payload.target_id,
        author=resolve_spec_actor(body_actor=payload.author),
        body=payload.body,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    from app.utils.planka_hooks import mirror_comment_best_effort

    mirror_comment_best_effort(
        db,
        payload.target_type.value,
        payload.target_id,
        payload.body,
        comment.author,
    )
    return comment


@router.get("/comments/{target_type}/{target_id}", response_model=list[CommentResponse])
def list_comments(
    target_type: CommentTargetType,
    target_id: UUID,
    db: Session = Depends(get_db),
) -> list[CommentResponse]:
    """Comentários de um workspace/documento/task, em ordem cronológica.

    Contraparte de leitura do ``create_comment``: sem ela, as notas de revisão e a
    evidência de teste que o fluxo manda registrar no card ficam gravadas e nunca
    podem ser recuperadas, o que inviabiliza o ciclo revisão → correção pelo quadro.
    """
    workspace_id = _resolve_comment_target_workspace(db, target_type, target_id)
    _assert_access(db, workspace_id)

    return (
        db.query(SpecComment)
        .filter(
            SpecComment.target_type == target_type,
            SpecComment.target_id == target_id,
        )
        .order_by(SpecComment.created_at.asc())
        .all()
    )


# --------------------------------------------------------------------------- #
# Busca semântica
# --------------------------------------------------------------------------- #
@router.get("/search", response_model=list[SpecSearchResult])
def search_specs_endpoint(
    q: str = Query(..., description="Consulta semântica"),
    project_id: Optional[str] = Query(None, description="Filtro opcional por projeto"),
    group: Optional[str] = Query(None, description="Grupo do solicitante (boost)"),
    status: Optional[list[str]] = Query(
        None,
        description="Status de workspace a incluir; omitido = só concluido, '*' = todos",
    ),
    db: Session = Depends(get_db),
) -> list[SpecSearchResult]:
    """Busca semântica em specs, ordenada por relevância (ADR-006).

    Por padrão só specs concluídas, como antes; ``status`` permite alcançar
    trabalho em andamento.
    """
    # Isolamento por grupo (fail-closed): só specs de workspaces do grupo do
    # ator, refinadas pela ACL quando houver regras.
    valid_ws_ids = accessible_workspace_ids_by_group(db)
    if valid_ws_ids == set():
        return []

    return search_specs(
        q,
        project_id=project_id,
        requester_group=group,
        accessible_workspace_ids=valid_ws_ids,
        statuses=status,
    )
