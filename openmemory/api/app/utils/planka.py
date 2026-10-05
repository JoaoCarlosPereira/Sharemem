"""HTTP client for the internal PLANKA sidecar (Spec mirror).

OpenMemory Spec tables remain the source of truth (ADR-005). This module is
the integration seam that projects Spec workspaces/tasks/documents onto PLANKA
Project/Board/List/Card IDs via ``spec_planka_id_map``. It never talks to
Qdrant and only calls the PLANKA REST API.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional, Protocol
from uuid import UUID

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    SpecDocument,
    SpecPlankaIdMap,
    SpecWorkspace,
    TaskCard,
    TaskCardStatus,
    parse_document_type,
)

logger = logging.getLogger(__name__)

DEFAULT_PLANKA_BASE_URL = "http://planka:1337"
DEFAULT_PLANKA_TIMEOUT_SECONDS = 5.0

# IDs PLANKA são snowflakes numéricos (bigint como string). Validar antes de
# interpolar em path evita path traversal/rota arbitrária a partir de webhook.
# ASCII explícito + fullmatch: ``\d`` aceitaria dígitos Unicode e ``$`` um "\n" final.
_PLANKA_ID_RE = re.compile(r"[0-9]{1,32}")


def is_planka_id(value: Any) -> bool:
    """``True`` se ``value`` é um ID PLANKA válido (1–32 dígitos ASCII)."""
    return value is not None and _PLANKA_ID_RE.fullmatch(str(value)) is not None


def _mapped_planka_id(row: Optional["SpecPlankaIdMap"]) -> Optional[str]:
    """ID de uma linha do mapa, ou ``None`` (com warning) se não for ID PLANKA válido.

    Linha corrompida no ``spec_planka_id_map`` não pode virar path (ex.: ``../users/me``);
    o espelho pula a operação sem chamar o PLANKA em vez de quebrar o fluxo Spec.
    """
    if row is None:
        return None
    if is_planka_id(row.planka_id):
        return row.planka_id
    logger.warning(
        "planka_mirror_invalid_mapped_id entity=%s spec_id=%s planka_id=%r — operação ignorada",
        row.entity_type,
        row.spec_id,
        row.planka_id,
    )
    return None

# Gap-based list positions (PLANKA convention).
_LIST_POSITION_STEP = 65536

# Spec Kanban column → PLANKA list display name (pipeline SDD).
SPEC_STATUS_TO_LIST_NAME: dict[str, str] = {
    TaskCardStatus.tasks.value: "Tasks",
    TaskCardStatus.em_andamento.value: "Em andamento",
    TaskCardStatus.revisao_codigo.value: "Revisão de código",
    TaskCardStatus.fase_teste.value: "Fase de teste",
    TaskCardStatus.concluido.value: "Concluído",
}

DOCUMENT_LIST_NAME = "SDD"
DOCUMENT_LIST_ENTITY = "list:documentos"
# Coluna SDD fica à esquerda das colunas de pipeline (padrão Spec antigo).
DOCUMENT_LIST_POSITION = _LIST_POSITION_STEP // 2
DOCUMENT_LIST_LEGACY_NAMES = ("Documentos", "SDD")

ENTITY_PROJECT = "project"
ENTITY_BOARD = "board"
ENTITY_TASK = "task"
ENTITY_DOCUMENT = "document"

class PlankaMirrorError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class PlankaMirrorNotFound(PlankaMirrorError):
    def __init__(self, detail: str = "recurso Spec não encontrado para espelho PLANKA"):
        super().__init__(404, detail)


class PlankaMirrorClient(Protocol):
    async def ensure_workspace_board(self, workspace_id: UUID) -> str: ...

    async def mirror_task(self, task_id: UUID) -> None: ...

    async def mirror_task_status(self, task_id: UUID) -> None: ...

    async def mirror_document(self, workspace_id: UUID, doc_type: str) -> None: ...

    async def mirror_comment(
        self, target_type: str, target_id: UUID, body: str, author: Optional[str] = None
    ) -> None: ...

    async def delete_task(self, task_id: UUID) -> None: ...

    async def set_project_lifecycle(
        self, workspace_id: UUID, *, is_archived: bool, is_completed: bool
    ) -> None: ...


def list_entity_type(status: str) -> str:
    """Entity type key for a Spec status list mapping."""
    return f"list:{status}"


def status_to_list_name(status: str) -> str:
    """Map Spec ``TaskCardStatus`` value to PLANKA list name."""
    key = (status or "").strip()
    if key not in SPEC_STATUS_TO_LIST_NAME:
        raise PlankaMirrorError(400, f"status Spec inválido para lista PLANKA: {status!r}")
    return SPEC_STATUS_TO_LIST_NAME[key]


class PlankaMirrorHttpClient:
    """Async PLANKA mirror client (pattern aligned with AgentRegistryHttpClient)."""

    def __init__(
        self,
        db: Session,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_PLANKA_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        raw_base_url = (
            base_url
            or os.getenv("PLANKA_BASE_URL")
            or os.getenv("PLANKA_INTERNAL_URL")
            or DEFAULT_PLANKA_BASE_URL
        )
        self.db = db
        self.base_url = raw_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    async def ensure_workspace_board(self, workspace_id: UUID) -> str:
        workspace = self.db.query(SpecWorkspace).filter(SpecWorkspace.id == workspace_id).first()
        if not workspace:
            raise PlankaMirrorNotFound(f"SpecWorkspace {workspace_id} não encontrado")

        existing_board = self._get_map(ENTITY_BOARD, workspace_id)
        if existing_board:
            await self._ensure_pipeline_lists(workspace_id, existing_board.planka_id)
            await self._ensure_document_list(workspace_id, existing_board.planka_id)
            self.db.commit()
            return existing_board.planka_id

        project_map = self._get_map(ENTITY_PROJECT, workspace_id)
        if project_map:
            project_id = project_map.planka_id
        else:
            project_payload = await self._request(
                "POST",
                "/api/projects",
                json={
                    "type": "shared",
                    "name": _truncate(workspace.name or workspace.slug, 128),
                    "description": f"Spec workspace {workspace.slug} ({workspace.project_id})",
                },
            )
            project_id = _item_id(project_payload)
            self._upsert_map(ENTITY_PROJECT, workspace_id, project_id)

        board_payload = await self._request(
            "POST",
            f"/api/projects/{project_id}/boards",
            json={
                "position": _LIST_POSITION_STEP,
                "name": _truncate(workspace.name or workspace.slug, 128),
            },
        )
        board_id = _item_id(board_payload)
        self._upsert_map(ENTITY_BOARD, workspace_id, board_id)
        await self._ensure_pipeline_lists(workspace_id, board_id)
        await self._ensure_document_list(workspace_id, board_id)
        self.db.commit()
        return board_id

    async def mirror_task(self, task_id: UUID) -> None:
        task = self.db.query(TaskCard).filter(TaskCard.id == task_id).first()
        if not task:
            raise PlankaMirrorNotFound(f"TaskCard {task_id} não encontrada")

        existing = self._get_map(ENTITY_TASK, task_id)
        if existing is not None and _mapped_planka_id(existing) is None:
            return
        board_id = await self.ensure_workspace_board(task.workspace_id)
        list_id = await self._list_id_for_status(task.workspace_id, board_id, task.status.value)
        position = float(task.position) if task.position is not None else float(_LIST_POSITION_STEP)
        body: dict[str, Any] = {
            "name": _truncate(task.title, 1024),
            "description": task.description,
            "type": "project",
            "position": position,
        }
        if task.due_at is not None:
            body["dueDate"] = task.due_at.isoformat()
        if existing:
            # PATCH permite limpar dueDate com null; create rejeita null explícito.
            if task.due_at is None:
                body["dueDate"] = None
            await self._request(
                "PATCH",
                f"/api/cards/{existing.planka_id}",
                json={**body, "listId": list_id},
            )
        else:
            created = await self._request(
                "POST",
                f"/api/lists/{list_id}/cards",
                json=body,
            )
            created_id = _item_id(created)
            # Commit imediato do vínculo para um evento humano não importar o card
            # como task nova. Corrida com import concorrente: a UniqueConstraint
            # barra o duplicado → rollback + PlankaMirrorError(409), nunca 500.
            try:
                self._upsert_map(ENTITY_TASK, task_id, created_id)
                self.db.commit()
            except IntegrityError as exc:
                self.db.rollback()
                logger.error(
                    "planka_mirror_link_conflict task=%s planka_card=%s "
                    "detail=card já vinculado a outra task (import concorrente)",
                    task_id,
                    created_id,
                )
                raise PlankaMirrorError(
                    409,
                    f"card PLANKA {created_id} já vinculado a outra task (import concorrente)",
                ) from exc
        card_id = _mapped_planka_id(self._get_map(ENTITY_TASK, task_id))
        if card_id:
            await self._mirror_task_checklists(task_id, card_id)
            await self._mirror_task_assignee(task, card_id)
        self.db.commit()

    async def mirror_task_status(self, task_id: UUID) -> None:
        task = self.db.query(TaskCard).filter(TaskCard.id == task_id).first()
        if not task:
            raise PlankaMirrorNotFound(f"TaskCard {task_id} não encontrada")

        existing = self._get_map(ENTITY_TASK, task_id)
        if not existing:
            # Card still missing — full mirror creates it in the right list.
            await self.mirror_task(task_id)
            return
        if _mapped_planka_id(existing) is None:
            return
        board_id = await self.ensure_workspace_board(task.workspace_id)
        list_id = await self._list_id_for_status(task.workspace_id, board_id, task.status.value)

        await self._request(
            "PATCH",
            f"/api/cards/{existing.planka_id}",
            json={
                "listId": list_id,
                # Status changes must not erase a position chosen in the UI.
                "position": float(task.position) if task.position is not None else _LIST_POSITION_STEP,
            },
        )
        await self._mirror_task_assignee(task, existing.planka_id)
        self.db.commit()

    async def mirror_comment(
        self, target_type: str, target_id: UUID, body: str, author: Optional[str] = None
    ) -> None:
        """Project a Spec comment onto its mapped PLANKA card with author identity."""
        if target_type != "task":
            return

        task_map = self._get_map(ENTITY_TASK, target_id)
        if not task_map:
            task = self.db.query(TaskCard).filter(TaskCard.id == target_id).first()
            if not task:
                raise PlankaMirrorNotFound(f"TaskCard {target_id} não encontrada")
            await self.mirror_task(target_id)
            task_map = self._get_map(ENTITY_TASK, target_id)

        if not task_map:
            raise PlankaMirrorError(502, "PLANKA card não mapeado para o task")
        if _mapped_planka_id(task_map) is None:
            return

        from app.utils.creator_identity import (
            identity_for_actor,
            resolve_actor_identities_with_db,
        )

        identity = identity_for_actor(
            author,
            resolve_actor_identities_with_db(self.db, [author]),
        )
        email = identity.email if identity else None
        if not email and author:
            email = normalize_assignee_email(author)
        name = identity.display_name if identity else None
        if not name and author:
            name = author.split("@", 1)[0] if "@" in author else author

        await self._request(
            "POST",
            f"/api/cards/{task_map.planka_id}/mem0-comments",
            json={
                "text": body,
                "email": email,
                "name": name,
                "picture": identity.avatar_url if identity else None,
            },
        )

    async def mirror_document(self, workspace_id: UUID, doc_type: str) -> None:
        workspace = self.db.query(SpecWorkspace).filter(SpecWorkspace.id == workspace_id).first()
        if not workspace:
            raise PlankaMirrorNotFound(f"SpecWorkspace {workspace_id} não encontrado")

        resolved = parse_document_type(doc_type)
        document = (
            self.db.query(SpecDocument)
            .filter(
                SpecDocument.workspace_id == workspace_id,
                SpecDocument.document_type == resolved,
            )
            .first()
        )
        if not document:
            raise PlankaMirrorNotFound(
                f"SpecDocument {resolved.value} não encontrado no workspace {workspace_id}"
            )

        board_id = await self.ensure_workspace_board(workspace_id)
        docs_list_id = await self._ensure_document_list(workspace_id, board_id)
        title = f"[{resolved.value}] {workspace.name}"
        # Full Spec bodies can be 30k+ chars; Planka card descriptions only need a
        # readable preview — keep payloads small so mirror stays within HTTP timeouts.
        body = {
            "name": _truncate(title, 1024),
            "description": _truncate(document.current_content, 8000),
            "type": "project",
        }
        existing = self._get_map(ENTITY_DOCUMENT, document.id)
        if existing:
            await self._request(
                "PATCH",
                f"/api/cards/{existing.planka_id}",
                json={**body, "listId": docs_list_id, "position": _LIST_POSITION_STEP},
            )
        else:
            created = await self._request(
                "POST",
                f"/api/lists/{docs_list_id}/cards",
                json={**body, "position": _LIST_POSITION_STEP},
            )
            self._upsert_map(ENTITY_DOCUMENT, document.id, _item_id(created))
        self.db.commit()

    async def delete_task(self, task_id: UUID) -> None:
        existing = self._get_map(ENTITY_TASK, task_id)
        if not existing or _mapped_planka_id(existing) is None:
            return
        try:
            await self._request("DELETE", f"/api/cards/{existing.planka_id}")
        except PlankaMirrorError as exc:
            if exc.status_code != 404:
                raise
        self.db.delete(existing)
        self.db.commit()

    async def set_project_lifecycle(
        self, workspace_id: UUID, *, is_archived: bool, is_completed: bool
    ) -> None:
        """Projeta ``SpecWorkspace.status`` como ``Project.isArchived/isCompleted``.

        No-op se o workspace ainda não tem projeto PLANKA mapeado (workspace
        criado com o mirror desligado, ou nunca aberto no Kanban) — nada a
        atualizar, e forçar ``ensure_workspace_board`` aqui recriaria o board de
        um workspace que talvez nunca deva aparecer no PLANKA.
        """
        project_map = self._get_map(ENTITY_PROJECT, workspace_id)
        if project_map is None:
            return
        try:
            await self._request(
                "PATCH",
                f"/api/projects/{project_map.planka_id}",
                json={"isArchived": is_archived, "isCompleted": is_completed},
                # Spec is the source of truth. Prevent this projection from
                # being interpreted by PLANKA as a human-originated change.
                headers={"X-Mem0-Mirror": "1"},
            )
        except PlankaMirrorError as exc:
            if exc.status_code == 404:
                # Projeto foi removido diretamente no PLANKA; mapa órfão, não retenta.
                return
            raise

    async def _mirror_task_assignee(self, task: TaskCard, planka_card_id: str) -> None:
        """Projeta ``TaskCard.assignee`` como card_membership PLANKA (avatar no card).

        PUT ``/api/cards/:id/mem0-assignee`` faz upsert do user por e-mail, garante
        board membership e deixa exatamente um membro (ou nenhum no release).
        """
        if not is_planka_id(planka_card_id):
            logger.warning("planka_mirror_invalid_card_id card=%r — assignee ignorado", planka_card_id)
            return
        from app.utils.creator_identity import (
            identity_for_actor,
            resolve_actor_identities_with_db,
        )

        email: Optional[str] = None
        name: Optional[str] = None
        picture: Optional[str] = None
        if task.assignee:
            identities = resolve_actor_identities_with_db(self.db, [task.assignee])
            identity = identity_for_actor(task.assignee, identities)
            # Prefer e-mail Google da máquina vinculada — evita user duplicado
            # s0293@mem0.local + joaocarlos@sysmo.com.br no mesmo board.
            if identity is not None and identity.email:
                email = identity.email
            else:
                email = normalize_assignee_email(task.assignee)
            if identity is not None:
                name = identity.display_name or None
                picture = identity.avatar_url or None
            if not name:
                name = (task.assignee.split("@", 1)[0] if "@" in task.assignee else task.assignee)[
                    :128
                ]

        await self._request(
            "PUT",
            f"/api/cards/{planka_card_id}/mem0-assignee",
            json={"email": email, "name": name, "picture": picture},
        )

    async def _mirror_task_checklists(self, task_id: UUID, planka_card_id: str) -> None:
        """Best-effort: projeta itens de checklist Spec como tasks do card PLANKA.

        PLANKA usa ``/api/cards/:id/tasks`` para checklist items. Falhas 4xx
        além de 404 são propagadas; ausência de API não deve quebrar o espelho
        principal (já feito via dueDate/position no card).
        """
        from app.models import TaskChecklist, TaskChecklistItem

        checklists = (
            self.db.query(TaskChecklist)
            .filter(TaskChecklist.task_id == task_id)
            .order_by(TaskChecklist.position.asc())
            .all()
        )
        for checklist in checklists:
            items = (
                self.db.query(TaskChecklistItem)
                .filter(TaskChecklistItem.checklist_id == checklist.id)
                .order_by(TaskChecklistItem.position.asc())
                .all()
            )
            for item in items:
                entity = "checklist_item"
                mapped = self._get_map(entity, item.id)
                body = {
                    "name": _truncate(item.title, 1024),
                    "isCompleted": bool(item.is_completed),
                    "position": float(item.position or _LIST_POSITION_STEP),
                }
                try:
                    if mapped:
                        await self._request(
                            "PATCH",
                            f"/api/tasks/{mapped.planka_id}",
                            json=body,
                        )
                    else:
                        created = await self._request(
                            "POST",
                            f"/api/cards/{planka_card_id}/tasks",
                            json=body,
                        )
                        self._upsert_map(entity, item.id, _item_id(created))
                except PlankaMirrorError as exc:
                    if exc.status_code in (404, 400, 405):
                        # Sidecar sem endpoint de tasks — campos ricos ficam só no Spec.
                        return
                    raise

    async def _ensure_pipeline_lists(self, workspace_id: UUID, board_id: str) -> None:
        for index, status in enumerate(SPEC_STATUS_TO_LIST_NAME):
            entity = list_entity_type(status)
            if self._get_map(entity, workspace_id):
                continue
            created = await self._request(
                "POST",
                f"/api/boards/{board_id}/lists",
                json={
                    "type": "active",
                    "position": _LIST_POSITION_STEP * (index + 1),
                    "name": SPEC_STATUS_TO_LIST_NAME[status],
                },
            )
            self._upsert_map(entity, workspace_id, _item_id(created))

    async def _ensure_document_list(self, workspace_id: UUID, board_id: str) -> str:
        """Garante a coluna SDD (PRD/TechSpec/ADRs/Tasks) à esquerda do board."""
        existing = self._get_map(DOCUMENT_LIST_ENTITY, workspace_id)
        if existing:
            await self._normalize_document_list(existing.planka_id)
            return existing.planka_id

        # Lista órfã renomeável (legado "Documentos" sem mapa).
        try:
            board_payload = await self._request("GET", f"/api/boards/{board_id}")
            included = board_payload.get("included") if isinstance(board_payload, dict) else None
            lists = (included or {}).get("lists") if isinstance(included, dict) else None
            if isinstance(lists, list):
                for item in lists:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "")
                    if name in DOCUMENT_LIST_LEGACY_NAMES and item.get("id") is not None:
                        planka_id = str(item["id"])
                        self._upsert_map(DOCUMENT_LIST_ENTITY, workspace_id, planka_id)
                        await self._normalize_document_list(planka_id)
                        return planka_id
        except PlankaMirrorError:
            pass

        created = await self._request(
            "POST",
            f"/api/boards/{board_id}/lists",
            json={
                "type": "active",
                "position": float(DOCUMENT_LIST_POSITION),
                "name": DOCUMENT_LIST_NAME,
            },
        )
        planka_id = _item_id(created)
        self._upsert_map(DOCUMENT_LIST_ENTITY, workspace_id, planka_id)
        return planka_id

    async def _normalize_document_list(self, planka_list_id: str) -> None:
        """Renomeia/reposiciona a coluna SDD (Documentos → SDD, 1ª coluna)."""
        try:
            await self._request(
                "PATCH",
                f"/api/lists/{planka_list_id}",
                json={
                    "name": DOCUMENT_LIST_NAME,
                    "position": float(DOCUMENT_LIST_POSITION),
                },
            )
        except PlankaMirrorError:
            # Best-effort: board ainda usável mesmo se rename falhar.
            return

    async def _list_id_for_status(
        self, workspace_id: UUID, board_id: str, status: str
    ) -> str:
        entity = list_entity_type(status)
        # Validate status early.
        status_to_list_name(status)
        mapped = self._get_map(entity, workspace_id)
        if mapped:
            return mapped.planka_id
        await self._ensure_pipeline_lists(workspace_id, board_id)
        mapped = self._get_map(entity, workspace_id)
        if not mapped:
            raise PlankaMirrorError(502, f"lista PLANKA ausente para status {status!r}")
        return mapped.planka_id

    def _get_map(self, entity_type: str, spec_id: UUID) -> Optional[SpecPlankaIdMap]:
        return (
            self.db.query(SpecPlankaIdMap)
            .filter(
                SpecPlankaIdMap.entity_type == entity_type,
                SpecPlankaIdMap.spec_id == spec_id,
            )
            .first()
        )

    def _upsert_map(self, entity_type: str, spec_id: UUID, planka_id: str) -> SpecPlankaIdMap:
        row = self._get_map(entity_type, spec_id)
        if row:
            row.planka_id = planka_id
        else:
            row = SpecPlankaIdMap(
                entity_type=entity_type,
                spec_id=spec_id,
                planka_id=planka_id,
            )
            self.db.add(row)
        self.db.flush()
        return row

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        request_headers = self._headers(headers=headers)
        client_kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise PlankaMirrorError(504, "PLANKA timeout") from exc
            except httpx.RequestError as exc:
                raise PlankaMirrorError(502, "PLANKA indisponível") from exc
        return _json_response(response)

    def _headers(self, *, headers: Optional[dict[str, str]] = None) -> dict[str, str]:
        merged = {"Accept": "application/json", "Content-Type": "application/json"}
        if headers:
            merged.update(headers)
        if token := _env_auth_token():
            merged["Authorization"] = f"Bearer {token}"
        return merged


def _item_id(payload: dict[str, Any]) -> str:
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("id") is None:
        raise PlankaMirrorError(502, "PLANKA retornou item sem id")
    # Nunca grava no mapa (nem usa em path) um ID que não seja snowflake numérico.
    if not is_planka_id(item["id"]):
        raise PlankaMirrorError(502, f"PLANKA retornou ID inválido: {item['id']!r}")
    return str(item["id"])


def _truncate(value: Optional[str], max_len: int) -> str:
    text = (value or "").strip() or "untitled"
    return text[:max_len]


def _json_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 404:
        raise PlankaMirrorError(404, "PLANKA recurso não encontrado")
    if response.status_code in (401, 403):
        raise PlankaMirrorError(response.status_code, "PLANKA negou acesso ao recurso")
    if response.status_code >= 500:
        raise PlankaMirrorError(502, f"PLANKA retornou HTTP {response.status_code}")
    if response.status_code >= 400:
        raise PlankaMirrorError(
            response.status_code, f"PLANKA retornou HTTP {response.status_code}"
        )
    if response.status_code == 204 or not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise PlankaMirrorError(502, "PLANKA retornou JSON inválido") from exc
    if not isinstance(data, dict):
        raise PlankaMirrorError(502, "PLANKA retornou payload inválido")
    return data


def _env_auth_token() -> Optional[str]:
    for name in (
        "PLANKA_INTERNAL_ACCESS_TOKEN",
        "INTERNAL_ACCESS_TOKEN",
        "PLANKA_BEARER_TOKEN",
        "PLANKA_AUTH_TOKEN",
    ):
        token = (os.getenv(name) or "").strip()
        if token:
            return token
    return None


def normalize_assignee_email(assignee: str) -> str:
    """Map Spec assignee (email or hostname/agent id) to PLANKA user email.

    Matches mem0-auth ``normalizeEmail``: real emails pass through; otherwise
    ``<sanitized>@mem0.local`` (e.g. ``Mini-PC`` → ``mini-pc@mem0.local``).
    """
    raw = (assignee or "").strip().lower()
    if raw and "@" in raw:
        return raw
    sanitized = "".join(ch if ch.isalnum() or ch in "._+-" else "-" for ch in raw) or "mem0-user"
    return f"{sanitized}@mem0.local"


__all__ = [
    "DOCUMENT_LIST_ENTITY",
    "DOCUMENT_LIST_NAME",
    "DEFAULT_PLANKA_BASE_URL",
    "DEFAULT_PLANKA_TIMEOUT_SECONDS",
    "ENTITY_BOARD",
    "ENTITY_DOCUMENT",
    "ENTITY_PROJECT",
    "ENTITY_TASK",
    "PlankaMirrorClient",
    "PlankaMirrorError",
    "PlankaMirrorHttpClient",
    "PlankaMirrorNotFound",
    "SPEC_STATUS_TO_LIST_NAME",
    "list_entity_type",
    "normalize_assignee_email",
    "status_to_list_name",
]
