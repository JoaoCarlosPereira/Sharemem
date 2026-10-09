import logging
from datetime import UTC, datetime
from typing import List, Optional, Set
from uuid import UUID

from app.database import get_db
from app.models import (
    AccessControl,
    App,
    Category,
    Memory,
    MemoryAccessLog,
    MemoryState,
    MemoryStatusHistory,
    User,
)
from app.schemas import MemoryResponse
from app.utils.db import get_or_create_user
from app.utils.deletion_guard import DeletionBlockedError, assert_bulk_delete_allowed, assert_memory_delete_allowed
from app.utils.memory import get_memory_client
from app.utils.permissions import check_memory_access_permissions
from app.utils.project_name import normalize_project
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_pagination import Page, Params
from fastapi_pagination.ext.sqlalchemy import paginate as sqlalchemy_paginate
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


def get_memory_or_404(db: Session, memory_id: UUID) -> Memory:
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


def update_memory_state(db: Session, memory_id: UUID, new_state: MemoryState, user_id: UUID):
    memory = get_memory_or_404(db, memory_id)
    old_state = memory.state

    # Update memory state
    memory.state = new_state
    if new_state == MemoryState.archived:
        memory.archived_at = datetime.now(UTC)
    elif new_state == MemoryState.deleted:
        memory.deleted_at = datetime.now(UTC)

    # Record state change
    history = MemoryStatusHistory(
        memory_id=memory_id,
        changed_by=user_id,
        old_state=old_state,
        new_state=new_state
    )
    db.add(history)
    db.commit()
    return memory


def get_accessible_memory_ids(db: Session, app_id: UUID) -> Set[UUID]:
    """
    Get the set of memory IDs that the app has access to based on app-level ACL rules.
    Returns all memory IDs if no specific restrictions are found.
    """
    # Get app-level access controls
    app_access = db.query(AccessControl).filter(
        AccessControl.subject_type == "app",
        AccessControl.subject_id == app_id,
        AccessControl.object_type == "memory"
    ).all()

    # If no app-level rules exist, return None to indicate all memories are accessible
    if not app_access:
        return None

    # Initialize sets for allowed and denied memory IDs
    allowed_memory_ids = set()
    denied_memory_ids = set()

    # Process app-level rules
    for rule in app_access:
        if rule.effect == "allow":
            if rule.object_id:  # Specific memory access
                allowed_memory_ids.add(rule.object_id)
            else:  # All memories access
                return None  # All memories allowed
        elif rule.effect == "deny":
            if rule.object_id:  # Specific memory denied
                denied_memory_ids.add(rule.object_id)
            else:  # All memories denied
                return set()  # No memories accessible

    # Remove denied memories from allowed set
    if allowed_memory_ids:
        allowed_memory_ids -= denied_memory_ids

    return allowed_memory_ids


def _author_hostname_from_memory(memory) -> Optional[str]:
    """Hostname do autor (ADR-003): metadata do vetor tem prioridade sobre o dono SQL."""
    from app.utils.identity import resolve_hostname

    meta = getattr(memory, "metadata_", None) or {}
    if isinstance(meta, dict):
        raw = meta.get("hostname") or meta.get("user_id")
        if raw:
            return resolve_hostname(str(raw))
    user = getattr(memory, "user", None)
    if user is not None and getattr(user, "user_id", None):
        return resolve_hostname(user.user_id)
    return None


def ui_reader_actor(fallback_user_id: Optional[str]) -> Optional[str]:
    """Actor string for read-audit rows written by Web UI reads.

    The UI sends ``?user_id=`` from ``NEXT_PUBLIC_USER_ID`` — a *build-time*
    constant shared by every browser (``OPENMEMORY_UI_USER_ID``), so it named
    the same person for all viewers. Order:

    1. valid Google session JWT → ``ui:<sub>`` (``User.id``, resolved to a person);
    2. otherwise → ``ui:anonymous`` (never resolved to a person).

    ``fallback_user_id`` (the REST ``?user_id=``) is intentionally NOT recorded:
    it is client-supplied and, from the UI, always the shared build id — the
    reader cannot be inferred from it.
    """
    from app.utils.creator_identity import UI_ANONYMOUS_ACTOR
    from app.utils.logging_context import auth_method_var, auth_user_var

    del fallback_user_id  # kept in the signature for call-site readability
    if auth_method_var.get() == "session":
        person = (auth_user_var.get() or "").strip()
        if person:
            return f"ui:{person}"
    return UI_ANONYMOUS_ACTOR


def memory_group_name(memory) -> Optional[str]:
    """Grupo (equipe) do autor da memória via hostname → User → Group (task_09).

    Para memórias compartilhadas (Qdrant), o autor é o ``hostname`` no payload,
    não o usuário SQL dono da linha. Retorna None quando o grupo não é resolvível.
    """
    from app.utils.groups import group_of_hostname

    hostname = _author_hostname_from_memory(memory)
    if hostname:
        group = group_of_hostname(hostname)
        if group:
            return group
    user = getattr(memory, "user", None)
    group = getattr(user, "group", None) if user is not None else None
    return group.name if group is not None else None


def group_name_for_hostname(hostname: Optional[str]) -> Optional[str]:
    """Resolve o grupo do autor a partir do hostname (listagem Qdrant / shared-filter)."""
    from app.utils.groups import group_of_hostname
    from app.utils.identity import resolve_hostname

    if not hostname:
        return None
    return group_of_hostname(resolve_hostname(hostname))


# List all memories with filtering
@router.get("/", response_model=Page[MemoryResponse])
async def list_memories(
    user_id: str,
    app_id: Optional[UUID] = None,
    from_date: Optional[int] = Query(
        None,
        description="Filter memories created after this date (timestamp)",
        examples=[1718505600]
    ),
    to_date: Optional[int] = Query(
        None,
        description="Filter memories created before this date (timestamp)",
        examples=[1718505600]
    ),
    categories: Optional[str] = None,
    params: Params = Depends(),
    search_query: Optional[str] = None,
    sort_column: Optional[str] = Query(None, description="Column to sort by (memory, categories, app_name, created_at)"),
    sort_direction: Optional[str] = Query(None, description="Sort direction (asc or desc)"),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Build base query
    query = db.query(Memory).filter(
        Memory.user_id == user.id,
        Memory.state != MemoryState.deleted,
        Memory.state != MemoryState.archived,
        Memory.content.ilike(f"%{search_query}%") if search_query else True
    )

    # Apply filters
    if app_id:
        query = query.filter(Memory.app_id == app_id)

    if from_date:
        from_datetime = datetime.fromtimestamp(from_date, tz=UTC)
        query = query.filter(Memory.created_at >= from_datetime)

    if to_date:
        to_datetime = datetime.fromtimestamp(to_date, tz=UTC)
        query = query.filter(Memory.created_at <= to_datetime)

    # Add joins for app and categories after filtering
    query = query.outerjoin(App, Memory.app_id == App.id)
    query = query.outerjoin(Memory.categories)

    # Apply category filter if provided
    if categories:
        category_list = [c.strip() for c in categories.split(",")]
        query = query.filter(Category.name.in_(category_list))

    # Apply sorting if specified
    if sort_column:
        sort_field = getattr(Memory, sort_column, None)
        if sort_field:
            if sort_direction == "desc":
                query = query.order_by(Memory.id, sort_field.desc())
            else:
                query = query.order_by(Memory.id, sort_field.asc())
    else:
        query = query.order_by(Memory.id, Memory.created_at.desc())

    # Add eager loading for app and categories
    query = query.options(
        joinedload(Memory.app),
        joinedload(Memory.categories),
        joinedload(Memory.user).joinedload(User.group),
    ).distinct(Memory.id)

    # Get paginated results with transformer
    return sqlalchemy_paginate(
        query,
        params,
        transformer=lambda items: [
            MemoryResponse(
                id=memory.id,
                content=memory.content,
                created_at=memory.created_at,
                state=memory.state.value,
                app_id=memory.app_id,
                app_name=memory.app.name if memory.app else None,
                categories=[category.name for category in memory.categories],
                metadata_=memory.metadata_,
                group=memory_group_name(memory),
            )
            for memory in items
            if check_memory_access_permissions(db, memory, app_id)
        ]
    )


@router.get("/deletion-policy")
async def memory_deletion_policy():
    """Read-only delete guard status for the memories UI (fail-closed by default)."""
    from app.utils.deletion_guard import deletion_guard_status

    status = deletion_guard_status()
    allowed = status["memory_delete_allowed"]
    return {
        **status,
        "message": (
            "Exclusão de memórias desabilitada neste servidor (proteção fail-closed). "
            "Defina MEM0_ALLOW_MEMORY_DELETE=1 para habilitar exclusões deliberadas."
            if not allowed
            else "Exclusão habilitada — confirme cada ação na interface antes de remover."
        ),
    }


# Get all categories
@router.get("/categories")
async def get_categories(
    user_id: str,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get unique categories associated with the user's memories
    # Get all memories
    memories = db.query(Memory).filter(Memory.user_id == user.id, Memory.state != MemoryState.deleted, Memory.state != MemoryState.archived).all()
    # Get all categories from memories
    categories = [category for memory in memories for category in memory.categories]
    # Get unique categories
    unique_categories = list(set(categories))

    return {
        "categories": unique_categories,
        "total": len(unique_categories)
    }


class CreateMemoryRequest(BaseModel):
    user_id: str
    text: str
    metadata: dict = {}
    infer: bool = True
    app: str = "openmemory"


# Create new memory
@router.post("/")
async def create_memory(
    request: CreateMemoryRequest,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.user_id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # ``metadata.project`` (quando enviado) usa a mesma chave efetiva do caminho
    # compartilhado: whitespace interno -> '-' (ver app.utils.project_name).
    if isinstance((request.metadata or {}).get("project"), str):
        request.metadata = {
            **request.metadata,
            "project": normalize_project(request.metadata["project"]),
        }
    # Get or create app
    app_obj = db.query(App).filter(App.name == request.app,
                                   App.owner_id == user.id).first()
    if not app_obj:
        app_obj = App(name=request.app, owner_id=user.id)
        db.add(app_obj)
        db.commit()
        db.refresh(app_obj)

    # Check if app is active
    if not app_obj.is_active:
        raise HTTPException(status_code=403, detail=f"App {request.app} is currently paused on ShareMem. Cannot create new memories.")

    # Log what we're about to do
    logging.info(f"Creating memory for user_id: {request.user_id} with app: {request.app}")
    
    # Try to get memory client safely
    try:
        memory_client = get_memory_client()
        if not memory_client:
            raise Exception("Memory client is not available")
    except Exception as client_error:
        logging.warning(f"Memory client unavailable: {client_error}. Creating memory in database only.")
        # Return a json response with the error
        return {
            "error": str(client_error)
        }

    # Try to save to Qdrant via memory_client
    try:
        qdrant_response = memory_client.add(
            request.text,
            user_id=request.user_id,  # Use string user_id to match search
            metadata={
                "source_app": "openmemory",
                "mcp_client": request.app,
            },
            infer=request.infer
        )
        
        # Log the response for debugging
        logging.info(f"Qdrant response: {qdrant_response}")
        
        # Process Qdrant response
        if isinstance(qdrant_response, dict) and 'results' in qdrant_response:
            created_memories = []
            
            for result in qdrant_response['results']:
                if result['event'] == 'ADD':
                    # Get the Qdrant-generated ID
                    memory_id = UUID(result['id'])
                    
                    # Check if memory already exists
                    existing_memory = db.query(Memory).filter(Memory.id == memory_id).first()
                    
                    if existing_memory:
                        # Update existing memory
                        existing_memory.state = MemoryState.active
                        existing_memory.content = result['memory']
                        memory = existing_memory
                    else:
                        # Create memory with the EXACT SAME ID from Qdrant
                        memory = Memory(
                            id=memory_id,  # Use the same ID that Qdrant generated
                            user_id=user.id,
                            app_id=app_obj.id,
                            content=result['memory'],
                            metadata_=request.metadata,
                            state=MemoryState.active
                        )
                        db.add(memory)
                    
                    # Create history entry
                    history = MemoryStatusHistory(
                        memory_id=memory_id,
                        changed_by=user.id,
                        old_state=MemoryState.deleted if existing_memory else MemoryState.deleted,
                        new_state=MemoryState.active
                    )
                    db.add(history)
                    
                    created_memories.append(memory)
            
            # Commit all changes at once
            if created_memories:
                db.commit()
                for memory in created_memories:
                    db.refresh(memory)
                
                # Return the first memory (for API compatibility)
                # but all memories are now saved to the database
                return created_memories[0]
    except Exception as qdrant_error:
        logging.warning(f"Qdrant operation failed: {qdrant_error}.")
        # Return a json response with the error
        return {
            "error": str(qdrant_error)
        }




# Get memory by ID
@router.get("/{memory_id}")
async def get_memory(
    memory_id: UUID,
    user_id: str | None = Query(None),
    db: Session = Depends(get_db)
):
    memory = (
        db.query(Memory)
        .options(joinedload(Memory.user), joinedload(Memory.app))
        .filter(Memory.id == memory_id)
        .first()
    )
    if memory:
        from app.utils.creator_identity import enrich_memory_attribution, resolve_creator_identities

        hostname = memory.user.user_id if memory.user else None
        identities = resolve_creator_identities([hostname])
        payload = {
            "id": memory.id,
            "text": memory.content,
            "created_at": int(memory.created_at.timestamp()),
            "state": memory.state.value,
            "app_id": memory.app_id,
            "app_name": memory.app.name if memory.app else None,
            "created_by_hostname": hostname,
            "created_by_client": memory.app.name if memory.app else None,
            "categories": [category.name for category in memory.categories],
            "metadata_": memory.metadata_,
            "group": memory_group_name(memory),
        }
        enrich_memory_attribution(payload, identities)
        return payload

    from app.utils.vector_stats import get_shared_memory_by_id

    shared = get_shared_memory_by_id(str(memory_id))
    if shared:
        from app.utils.read_audit import record_memory_reads

        proj = (shared.get("metadata_") or {}).get("project") or shared.get("app_name")
        record_memory_reads(
            project=str(proj) if proj else None,
            memory_ids=[str(memory_id)],
            access_type="get",
            source="api",
            hostname=ui_reader_actor(user_id),
            client_name="openmemory",
            items=[{"id": str(memory_id), "project": proj, "metadata_": shared.get("metadata_")}],
        )
        from app.utils.creator_identity import enrich_memory_attribution, resolve_creator_identities

        identities = resolve_creator_identities([shared.get("created_by_hostname")])
        enrich_memory_attribution(shared, identities)
        shared["group"] = group_name_for_hostname(shared.get("created_by_hostname"))
        return shared

    raise HTTPException(status_code=404, detail="Memory not found")


class DeleteMemoriesRequest(BaseModel):
    memory_ids: List[UUID]
    user_id: str


def _delete_memories_impl(db: Session, memory_ids: List[UUID], user_id: str) -> int:
    """Delete memories from Qdrant and mark deleted in SQL when a row exists."""
    from app.utils.memory import get_memory_client_safe

    if len(memory_ids) > 1:
        try:
            assert_bulk_delete_allowed("bulk_delete")
        except DeletionBlockedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    else:
        try:
            assert_memory_delete_allowed("delete")
        except DeletionBlockedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    user = get_or_create_user(db, user_id)
    memory_client = get_memory_client_safe()
    if not memory_client:
        raise HTTPException(status_code=503, detail="Memory client is not available")

    deleted = 0
    for memory_id in memory_ids:
        vector_ok = False
        try:
            memory_client.delete(str(memory_id))
            vector_ok = True
        except Exception as delete_error:
            logging.warning(
                "Failed to delete memory %s from vector store: %s",
                memory_id,
                delete_error,
            )

        memory = db.query(Memory).filter(Memory.id == memory_id).first()
        if memory:
            update_memory_state(db, memory_id, MemoryState.deleted, user.id)
            deleted += 1
        elif vector_ok:
            deleted += 1

    return deleted


# Delete multiple memories (legacy DELETE — prefer POST /actions/delete through UI proxy)
@router.delete("/")
async def delete_memories(
    request: DeleteMemoriesRequest,
    db: Session = Depends(get_db)
):
    deleted = _delete_memories_impl(db, request.memory_ids, request.user_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No memories were deleted")
    return {"message": f"Successfully deleted {deleted} memories"}


@router.post("/actions/delete")
async def delete_memories_action(
    request: DeleteMemoriesRequest,
    db: Session = Depends(get_db)
):
    """Delete memories (POST for Next.js api-proxy — DELETE bodies are not forwarded)."""
    deleted = _delete_memories_impl(db, request.memory_ids, request.user_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No memories were deleted")
    return {"message": f"Successfully deleted {deleted} memories"}


# Archive memories
@router.post("/actions/archive")
async def archive_memories(
    memory_ids: List[UUID],
    user_id: UUID,
    db: Session = Depends(get_db)
):
    for memory_id in memory_ids:
        update_memory_state(db, memory_id, MemoryState.archived, user_id)
    return {"message": f"Successfully archived {len(memory_ids)} memories"}


class PauseMemoriesRequest(BaseModel):
    memory_ids: Optional[List[UUID]] = None
    category_ids: Optional[List[UUID]] = None
    app_id: Optional[UUID] = None
    all_for_app: bool = False
    global_pause: bool = False
    state: Optional[MemoryState] = None
    user_id: str

# Pause access to memories
@router.post("/actions/pause")
async def pause_memories(
    request: PauseMemoriesRequest,
    db: Session = Depends(get_db)
):
    
    global_pause = request.global_pause
    all_for_app = request.all_for_app
    app_id = request.app_id
    memory_ids = request.memory_ids
    category_ids = request.category_ids
    state = request.state or MemoryState.paused

    user = db.query(User).filter(User.user_id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_id = user.id
    
    if global_pause:
        # Pause all memories
        memories = db.query(Memory).filter(
            Memory.state != MemoryState.deleted,
            Memory.state != MemoryState.archived
        ).all()
        for memory in memories:
            update_memory_state(db, memory.id, state, user_id)
        return {"message": "Successfully paused all memories"}

    if app_id:
        # Pause all memories for an app
        memories = db.query(Memory).filter(
            Memory.app_id == app_id,
            Memory.user_id == user.id,
            Memory.state != MemoryState.deleted,
            Memory.state != MemoryState.archived
        ).all()
        for memory in memories:
            update_memory_state(db, memory.id, state, user_id)
        return {"message": f"Successfully paused all memories for app {app_id}"}
    
    if all_for_app and memory_ids:
        # Pause all memories for an app
        memories = db.query(Memory).filter(
            Memory.user_id == user.id,
            Memory.state != MemoryState.deleted,
            Memory.id.in_(memory_ids)
        ).all()
        for memory in memories:
            update_memory_state(db, memory.id, state, user_id)
        return {"message": "Successfully paused all memories"}

    if memory_ids:
        # Pause specific memories
        for memory_id in memory_ids:
            update_memory_state(db, memory_id, state, user_id)
        return {"message": f"Successfully paused {len(memory_ids)} memories"}

    if category_ids:
        # Pause memories by category
        memories = db.query(Memory).join(Memory.categories).filter(
            Category.id.in_(category_ids),
            Memory.state != MemoryState.deleted,
            Memory.state != MemoryState.archived
        ).all()
        for memory in memories:
            update_memory_state(db, memory.id, state, user_id)
        return {"message": f"Successfully paused memories in {len(category_ids)} categories"}

    raise HTTPException(status_code=400, detail="Invalid pause request parameters")


# Get memory access logs
@router.get("/{memory_id}/access-log")
async def get_memory_access_log(
    memory_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    grouped: bool = Query(True, description="Collapse repeated reads by the same actor/type within the window"),
    channel: Optional[str] = Query(
        None,
        pattern="^(web|mcp|api|agents)$",
        description="Restrict to one channel: web (UI), mcp, api (compat v3), agents (mcp + api)",
    ),
    db: Session = Depends(get_db)
):
    from app.utils.read_audit import list_memory_read_audit_page, utc_isoformat

    mid = str(memory_id)

    # Qdrant/MCP reads land in read_audit_logs (no SQL memories FK).
    audit = list_memory_read_audit_page(
        db, mid, page=page, page_size=page_size, grouped=grouped, channel=channel
    )
    if audit["raw_total"] or channel or sum(audit["channel_counts"].values()):
        return {
            "total": audit["total"],
            "page": page,
            "page_size": page_size,
            "logs": audit["logs"],
            # Additive (card 01ada614): raw rows vs display entries + grouping info.
            "raw_total": audit["raw_total"],
            "grouped": audit["grouped"],
            "group_window_seconds": audit["group_window_seconds"],
            "grouping_truncated": audit["grouping_truncated"],
            "channel": audit["channel"],
            "channel_counts": audit["channel_counts"],
        }

    # Legacy SQL-backed memories still use memory_access_logs.
    query = db.query(MemoryAccessLog).filter(MemoryAccessLog.memory_id == memory_id)
    total = query.count()
    legacy_rows = (
        query.order_by(MemoryAccessLog.accessed_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    logs = []
    for log in legacy_rows:
        app = db.query(App).filter(App.id == log.app_id).first()
        logs.append(
            {
                "id": str(log.id),
                "app_name": app.name if app else "unknown",
                "display_name": app.name if app else "Desconhecido",
                "accessed_at": utc_isoformat(log.accessed_at),
                "access_type": log.access_type,
            }
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "logs": logs,
    }


class UpdateMemoryRequest(BaseModel):
    memory_content: str
    user_id: str

# Update a memory
@router.put("/{memory_id}")
async def update_memory(
    memory_id: UUID,
    request: UpdateMemoryRequest,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.user_id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    memory = get_memory_or_404(db, memory_id)
    memory.content = request.memory_content
    db.commit()
    db.refresh(memory)
    return memory

class FilterMemoriesRequest(BaseModel):
    user_id: str
    page: int = 1
    size: int = 10
    search_query: Optional[str] = None
    app_ids: Optional[List[UUID]] = None
    category_ids: Optional[List[UUID]] = None
    sort_column: Optional[str] = None
    sort_direction: Optional[str] = None
    from_date: Optional[int] = None
    to_date: Optional[int] = None
    show_archived: Optional[bool] = False
    project: Optional[str] = None
    source: Optional[str] = None  # "sql" (default) or "shared" (Qdrant/MCP path)


class SharedMemoryResponse(BaseModel):
    id: str
    content: str
    created_at: str | int
    state: str
    app_id: Optional[UUID] = None
    app_name: str
    created_by_hostname: Optional[str] = None
    created_by_client: Optional[str] = None
    created_by_display_name: Optional[str] = None
    created_by_avatar_url: Optional[str] = None
    categories: List[str] = Field(default_factory=list)
    metadata_: Optional[dict] = None
    group: Optional[str] = None


class SharedMemoriesPage(BaseModel):
    items: List[SharedMemoryResponse]
    total: int
    page: int
    size: int
    pages: int


@router.post("/shared-filter", response_model=SharedMemoriesPage)
async def filter_shared_memories(request: FilterMemoriesRequest):
    """List project-scoped memories from Qdrant (MCP write path)."""
    from app.utils.vector_stats import list_shared_memories

    # Filtro por projeto com a mesma chave da escrita ("A B" acha "A-B").
    request.project = normalize_project(request.project)
    try:
        data = list_shared_memories(
            search=request.search_query,
            project=request.project,
            page=request.page,
            size=request.size,
            sort_direction=request.sort_direction or "desc",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    from app.utils.creator_identity import enrich_memory_items

    raw_items = list(data["items"])
    enrich_memory_items(raw_items)

    items = [
        SharedMemoryResponse(
            id=str(item["id"]),
            content=item["content"],
            created_at=item["created_at"],
            state=item["state"],
            app_id=item["app_id"],
            app_name=item["app_name"],
            created_by_hostname=item.get("created_by_hostname"),
            created_by_client=item.get("created_by_client"),
            created_by_display_name=item.get("created_by_display_name"),
            created_by_avatar_url=item.get("created_by_avatar_url"),
            categories=item["categories"],
            metadata_=item.get("metadata_"),
            group=group_name_for_hostname(item.get("created_by_hostname")),
        )
        for item in raw_items
    ]

    from app.utils.read_audit import record_memory_reads

    audit_items = [
        {
            "id": item.id,
            "project": (item.metadata_ or {}).get("project") or item.app_name,
            "metadata_": item.metadata_,
        }
        for item in items
    ]
    record_memory_reads(
        project=request.project,
        memory_ids=[i["id"] for i in audit_items],
        access_type="search" if request.search_query else "list",
        source="api",
        hostname=ui_reader_actor(request.user_id),
        client_name="openmemory",
        query=request.search_query,
        items=audit_items,
    )

    return SharedMemoriesPage(
        items=items,
        total=data["total"],
        page=data["page"],
        size=data["size"],
        pages=data["pages"],
    )


@router.post("/filter", response_model=Page[MemoryResponse])
async def filter_memories(
    request: FilterMemoriesRequest,
    db: Session = Depends(get_db)
):
    if (request.source or "").lower() == "shared":
        shared = await filter_shared_memories(request)
        return Page.create(shared.items, total=shared.total, params=Params(page=shared.page, size=shared.size))
    user = db.query(User).filter(User.user_id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Build base query
    query = db.query(Memory).filter(
        Memory.user_id == user.id,
        Memory.state != MemoryState.deleted,
    )

    # Filter archived memories based on show_archived parameter
    if not request.show_archived:
        query = query.filter(Memory.state != MemoryState.archived)

    # Apply search filter
    if request.search_query:
        query = query.filter(Memory.content.ilike(f"%{request.search_query}%"))

    # Apply app filter
    if request.app_ids:
        query = query.filter(Memory.app_id.in_(request.app_ids))

    # Add joins for app and categories
    query = query.outerjoin(App, Memory.app_id == App.id)

    # Apply category filter
    if request.category_ids:
        query = query.join(Memory.categories).filter(Category.id.in_(request.category_ids))
    else:
        query = query.outerjoin(Memory.categories)

    # Apply date filters
    if request.from_date:
        from_datetime = datetime.fromtimestamp(request.from_date, tz=UTC)
        query = query.filter(Memory.created_at >= from_datetime)

    if request.to_date:
        to_datetime = datetime.fromtimestamp(request.to_date, tz=UTC)
        query = query.filter(Memory.created_at <= to_datetime)

    # Apply sorting
    if request.sort_column and request.sort_direction:
        sort_direction = request.sort_direction.lower()
        if sort_direction not in ['asc', 'desc']:
            raise HTTPException(status_code=400, detail="Invalid sort direction")

        sort_mapping = {
            'memory': Memory.content,
            'app_name': App.name,
            'created_at': Memory.created_at
        }

        if request.sort_column not in sort_mapping:
            raise HTTPException(status_code=400, detail="Invalid sort column")

        sort_field = sort_mapping[request.sort_column]
        if sort_direction == 'desc':
            query = query.order_by(Memory.id, sort_field.desc())
        else:
            query = query.order_by(Memory.id, sort_field.asc())
    else:
        # Default sorting — Memory.id first for PostgreSQL DISTINCT ON
        query = query.order_by(Memory.id, Memory.created_at.desc())

    # Add eager loading for categories and make the query distinct
    query = query.options(
        joinedload(Memory.categories)
    ).distinct(Memory.id)

    # Use fastapi-pagination's paginate function
    return sqlalchemy_paginate(
        query,
        Params(page=request.page, size=request.size),
        transformer=lambda items: [
            MemoryResponse(
                id=memory.id,
                content=memory.content,
                created_at=memory.created_at,
                state=memory.state.value,
                app_id=memory.app_id,
                app_name=memory.app.name if memory.app else None,
                categories=[category.name for category in memory.categories],
                metadata_=memory.metadata_,
                group=memory_group_name(memory),
            )
            for memory in items
        ]
    )


@router.get("/{memory_id}/related", response_model=Page[MemoryResponse])
async def get_related_memories(
    memory_id: UUID,
    user_id: str,
    params: Params = Depends(),
    db: Session = Depends(get_db)
):
    # Validate user
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get the source memory (SQL catalog or Qdrant shared store)
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        from app.utils.vector_stats import get_shared_memory_by_id

        if get_shared_memory_by_id(str(memory_id)):
            return Page.create([], total=0, params=params)
        raise HTTPException(status_code=404, detail="Memory not found")
    
    # Extract category IDs from the source memory
    category_ids = [category.id for category in memory.categories]
    
    if not category_ids:
        return Page.create([], total=0, params=params)

    # Build query for related memories
    query = db.query(Memory).distinct(Memory.id).filter(
        Memory.user_id == user.id,
        Memory.id != memory_id,
        Memory.state != MemoryState.deleted
    ).join(Memory.categories).filter(
        Category.id.in_(category_ids)
    ).options(
        joinedload(Memory.categories),
        joinedload(Memory.app)
    ).order_by(
        func.count(Category.id).desc(),
        Memory.created_at.desc()
    ).group_by(Memory.id)
    
    # ⚡ Force page size to be 5
    params = Params(page=params.page, size=5)
    
    return sqlalchemy_paginate(
        query,
        params,
        transformer=lambda items: [
            MemoryResponse(
                id=memory.id,
                content=memory.content,
                created_at=memory.created_at,
                state=memory.state.value,
                app_id=memory.app_id,
                app_name=memory.app.name if memory.app else None,
                categories=[category.name for category in memory.categories],
                metadata_=memory.metadata_,
                group=memory_group_name(memory),
            )
            for memory in items
        ]
    )


def _rebind_route_endpoint(router, original, wrapped) -> int:
    """Point already-registered routes at ``wrapped``; returns how many changed.

    ``@router.get`` builds the route's ``Dependant`` (and request handler) from
    the function object at import time, so rebinding the module attribute alone
    leaves dispatch on the unwrapped function — the admin reads went unaudited.
    """
    from fastapi.routing import APIRoute, request_response

    changed = 0
    for route in getattr(router, "routes", []):
        if not isinstance(route, APIRoute) or route.endpoint is not original:
            continue
        route.endpoint = wrapped
        route.dependant.call = wrapped
        route.app = request_response(route.get_route_handler())
        changed += 1
    return changed


def install_admin_read_audit() -> None:
    try:
        from functools import wraps

        from app.routers import admin as admin_mod
        from app.utils import read_audit as read_audit_mod

        if getattr(admin_mod.project_memories, "_read_audit_wrapped", False):
            return

        original = admin_mod.project_memories

        # ``wraps`` keeps the original signature (Query bounds) for FastAPI.
        @wraps(original)
        def wrapped(project: str, search=None, limit: int = 100):
            result = original(project=project, search=search, limit=limit)
            items = result.get("items") or []
            read_audit_mod.record_memory_reads(
                project=project,
                memory_ids=[i.get("id") for i in items],
                access_type="search" if search else "list",
                source="admin",
                hostname=ui_reader_actor(None),
                query=search,
                items=[
                    {"id": i.get("id"), "project": project, "metadata": {"project": project}}
                    for i in items
                ],
            )
            return result

        wrapped._read_audit_wrapped = True  # type: ignore[attr-defined]
        admin_mod.project_memories = wrapped
        if not _rebind_route_endpoint(admin_mod.router, original, wrapped):
            logging.getLogger(__name__).error(
                "admin read-audit: route /admin/projects/{project}/memories not rebound — reads unaudited"
            )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("admin read-audit wrapper not installed", exc_info=True)


from app.utils.mcp_read_wrappers import install_mcp_read_audit

install_mcp_read_audit()
install_admin_read_audit()