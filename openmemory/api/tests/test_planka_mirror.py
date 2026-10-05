"""Unit tests for Spec↔PLANKA mirror client and id_map (kanban-planka task_02)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    DocumentType,
    Project,
    SpecDocument,
    SpecPlankaIdMap,
    SpecWorkspace,
    TaskCard,
    TaskCardStatus,
)
from app.utils.planka import (
    DOCUMENT_LIST_ENTITY,
    DOCUMENT_LIST_NAME,
    ENTITY_BOARD,
    ENTITY_DOCUMENT,
    ENTITY_PROJECT,
    ENTITY_TASK,
    SPEC_STATUS_TO_LIST_NAME,
    PlankaMirrorError,
    PlankaMirrorHttpClient,
    list_entity_type,
    normalize_assignee_email,
    status_to_list_name,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk_workspace(db, *, project_name="mem0-shared", slug="kanban-planka") -> SpecWorkspace:
    if not db.query(Project).filter(Project.name == project_name).first():
        db.add(Project(name=project_name))
        db.commit()
    ws = SpecWorkspace(project_id=project_name, slug=slug, name="Kanban PLANKA")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class _PlankaRouter:
    """Minimal in-memory PLANKA API for httpx.MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.card_patch_calls: list[tuple[str, str, dict]] = []
        self.list_names: list[str] = []
        self.assignee_calls: list[tuple[str, dict]] = []
        self.comment_calls: list[tuple[str, dict]] = []
        self.project_patch_calls: list[tuple[str, dict]] = []
        self._seq = 1000
        self.fail_next: dict[str, int] | None = None
        self.timeout_paths: set[str] = set()

    def _next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        self.calls.append((method, path))

        if path in self.timeout_paths:
            raise httpx.TimeoutException("simulated timeout", request=request)

        if self.fail_next and self.fail_next.get("path") == path:
            code = int(self.fail_next["status"])
            self.fail_next = None
            return httpx.Response(code, json={"message": "boom"})

        if method == "POST" and path == "/api/projects":
            return _json_response(200, {"item": {"id": self._next_id(), "name": "p"}})

        if method == "POST" and path.startswith("/api/projects/") and path.endswith("/boards"):
            return _json_response(200, {"item": {"id": self._next_id(), "name": "b"}})

        if method == "POST" and path.startswith("/api/boards/") and path.endswith("/lists"):
            try:
                body = json.loads(request.content.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            name = str(body.get("name") or "l")
            self.list_names.append(name)
            return _json_response(200, {"item": {"id": self._next_id(), "name": name}})

        if method == "POST" and path.startswith("/api/lists/") and path.endswith("/cards"):
            return _json_response(200, {"item": {"id": self._next_id(), "name": "c"}})

        if method == "POST" and path.startswith("/api/cards/") and path.endswith("/mem0-comments"):
            try:
                body = json.loads(request.content.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            self.comment_calls.append((path, body))
            return _json_response(200, {"item": {"id": self._next_id(), "text": body.get("text")}})

        if method == "PUT" and path.endswith("/mem0-assignee"):
            try:
                body = json.loads(request.content.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            self.assignee_calls.append((path, body))
            return _json_response(
                200,
                {
                    "item": {
                        "cleared": body.get("email") is None,
                        "userId": None if body.get("email") is None else self._next_id(),
                        "email": body.get("email"),
                    }
                },
            )

        if method == "PATCH" and path.startswith("/api/cards/"):
            card_id = path.rsplit("/", 1)[-1]
            try:
                body = json.loads(request.content.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            self.card_patch_calls.append((method, path, body))
            return _json_response(200, {"item": {"id": card_id, "name": "c"}})

        if method == "PATCH" and path.startswith("/api/projects/"):
            try:
                body = json.loads(request.content.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            project_id = path.rsplit("/", 1)[-1]
            self.project_patch_calls.append((project_id, body))
            return _json_response(200, {"item": {"id": project_id, **body}})

        if method == "PATCH" and path.startswith("/api/lists/"):
            list_id = path.rsplit("/", 1)[-1]
            return _json_response(200, {"item": {"id": list_id, "name": DOCUMENT_LIST_NAME}})

        if method == "DELETE" and path.startswith("/api/cards/"):
            card_id = path.rsplit("/", 1)[-1]
            return _json_response(200, {"item": {"id": card_id}})

        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def planka_router():
    return _PlankaRouter()


@pytest.fixture
def client(db_session, planka_router, monkeypatch):
    monkeypatch.setenv("PLANKA_BASE_URL", "http://planka.test")
    monkeypatch.setenv("INTERNAL_ACCESS_TOKEN", "secret-token")
    transport = httpx.MockTransport(planka_router.handler)
    return PlankaMirrorHttpClient(
        db_session,
        base_url="http://planka.test",
        transport=transport,
    )


class TestStatusMapping:
    def test_maps_all_pipeline_statuses(self):
        assert set(SPEC_STATUS_TO_LIST_NAME) == {s.value for s in TaskCardStatus}
        assert status_to_list_name("em_andamento") == "Em andamento"
        assert list_entity_type("fase_teste") == "list:fase_teste"

    def test_invalid_status_raises(self):
        with pytest.raises(PlankaMirrorError) as exc:
            status_to_list_name("desconhecido")
        assert exc.value.status_code == 400


class TestSpecPlankaIdMapModel:
    def test_persists_and_unique_entity_spec(self, db_session):
        spec_id = uuid4()
        db_session.add(
            SpecPlankaIdMap(entity_type=ENTITY_BOARD, spec_id=spec_id, planka_id="111")
        )
        db_session.commit()
        found = db_session.query(SpecPlankaIdMap).one()
        assert found.planka_id == "111"

        db_session.add(
            SpecPlankaIdMap(entity_type=ENTITY_BOARD, spec_id=spec_id, planka_id="222")
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_unique_entity_planka_id(self, db_session):
        db_session.add(
            SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=uuid4(), planka_id="same")
        )
        db_session.commit()
        db_session.add(
            SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=uuid4(), planka_id="same")
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestEnsureWorkspaceBoard:
    @pytest.mark.asyncio
    async def test_creates_project_board_lists_and_id_map(
        self, db_session, client, planka_router
    ):
        ws = _mk_workspace(db_session)
        board_id = await client.ensure_workspace_board(ws.id)

        assert board_id
        board_row = (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_BOARD, spec_id=ws.id)
            .one()
        )
        assert board_row.planka_id == board_id
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_PROJECT, spec_id=ws.id)
            .count()
            == 1
        )
        for status in SPEC_STATUS_TO_LIST_NAME:
            assert (
                db_session.query(SpecPlankaIdMap)
                .filter_by(entity_type=list_entity_type(status), spec_id=ws.id)
                .count()
                == 1
            )
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=DOCUMENT_LIST_ENTITY, spec_id=ws.id)
            .count()
            == 1
        )
        assert DOCUMENT_LIST_NAME in planka_router.list_names

        methods = [m for m, _ in planka_router.calls]
        # project + board + pipeline lists + SDD list
        assert methods.count("POST") >= 1 + 1 + len(SPEC_STATUS_TO_LIST_NAME) + 1

        # Idempotent: second call does not recreate project/board (normalize may PATCH SDD).
        posts_before = sum(1 for m, _ in planka_router.calls if m == "POST")
        again = await client.ensure_workspace_board(ws.id)
        assert again == board_id
        posts_after = sum(1 for m, _ in planka_router.calls if m == "POST")
        assert posts_after == posts_before
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=DOCUMENT_LIST_ENTITY, spec_id=ws.id)
            .count()
            == 1
        )

    @pytest.mark.asyncio
    async def test_auth_header_sent(self, db_session, planka_router, monkeypatch):
        monkeypatch.setenv("INTERNAL_ACCESS_TOKEN", "tok-abc")
        seen: list[str | None] = []

        def capture(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return planka_router.handler(request)

        ws = _mk_workspace(db_session)
        client = PlankaMirrorHttpClient(
            db_session,
            base_url="http://planka.test",
            transport=httpx.MockTransport(capture),
        )
        await client.ensure_workspace_board(ws.id)
        assert any(h == "Bearer tok-abc" for h in seen)


class TestMirrorTask:
    @pytest.mark.asyncio
    async def test_mirror_task_creates_card_and_map(self, db_session, client):
        ws = _mk_workspace(db_session)
        task = TaskCard(
            workspace_id=ws.id,
            title="Implementar mirror",
            description="campos básicos",
            status=TaskCardStatus.tasks,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        await client.mirror_task(task.id)

        row = (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_TASK, spec_id=task.id)
            .one()
        )
        assert row.planka_id

        # Update path (PATCH) on second mirror.
        task.title = "Implementar mirror v2"
        db_session.commit()
        await client.mirror_task(task.id)
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_TASK, spec_id=task.id)
            .count()
            == 1
        )

    @pytest.mark.asyncio
    async def test_mirror_comment_posts_to_mapped_task_card(
        self, db_session, client, planka_router
    ):
        ws = _mk_workspace(db_session)
        task = TaskCard(
            workspace_id=ws.id,
            title="Comentário via MCP",
            status=TaskCardStatus.tasks,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        await client.mirror_task(task.id)
        await client.mirror_comment(
            "task",
            task.id,
            "Evidência do teste",
            "joao@example.com",
        )

        assert len(planka_router.comment_calls) == 1
        path, body = planka_router.comment_calls[0]
        assert path.startswith("/api/cards/") and path.endswith("/mem0-comments")
        assert body == {
            "text": "Evidência do teste",
            "email": "joao@example.com",
            "name": "joao",
            "picture": None,
        }

    @pytest.mark.asyncio
    async def test_mirror_task_syncs_assignee_membership(
        self, db_session, client, planka_router
    ):
        ws = _mk_workspace(db_session)
        task = TaskCard(
            workspace_id=ws.id,
            title="Com assignee",
            status=TaskCardStatus.em_andamento,
            assignee="Mini-PC",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        await client.mirror_task(task.id)

        assert planka_router.assignee_calls
        _path, body = planka_router.assignee_calls[-1]
        assert body["email"] == "mini-pc@mem0.local"
        assert body["name"] == "Mini-PC"

    @pytest.mark.asyncio
    async def test_mirror_task_status_clears_assignee_on_release(
        self, db_session, client, planka_router
    ):
        ws = _mk_workspace(db_session)
        task = TaskCard(
            workspace_id=ws.id,
            title="Release assignee",
            status=TaskCardStatus.em_andamento,
            assignee="joao@mem0.local",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        await client.mirror_task(task.id)
        task.assignee = None
        task.status = TaskCardStatus.tasks
        db_session.commit()
        await client.mirror_task_status(task.id)

        assert planka_router.assignee_calls
        _path, body = planka_router.assignee_calls[-1]
        assert body["email"] is None

    @pytest.mark.asyncio
    async def test_mirror_task_status_moves_list(self, db_session, client, planka_router):
        ws = _mk_workspace(db_session)
        task = TaskCard(
            workspace_id=ws.id,
            title="Mover status",
            status=TaskCardStatus.tasks,
            position=32768,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        await client.mirror_task(task.id)
        task.status = TaskCardStatus.em_andamento
        db_session.commit()
        await client.mirror_task_status(task.id)

        patch_calls = [
            (m, p) for m, p in planka_router.calls if m == "PATCH" and p.startswith("/api/cards/")
        ]
        assert patch_calls
        status_patch = next(
            body
            for method, path, body in planka_router.card_patch_calls
            if method == "PATCH" and path.startswith("/api/cards/")
        )
        assert status_patch["position"] == 32768


class TestNormalizeAssigneeEmail:
    def test_email_passthrough(self):
        assert normalize_assignee_email("Joao@Mem0.Local") == "joao@mem0.local"

    def test_hostname_to_local(self):
        assert normalize_assignee_email("Mini-PC") == "mini-pc@mem0.local"
        assert normalize_assignee_email("e2e-smoke-agent") == "e2e-smoke-agent@mem0.local"


@pytest.mark.asyncio
async def test_mirror_assignee_prefers_linked_google_email(db_session, monkeypatch):
    """Hostname Spec assignee → e-mail Google vinculado (não *@mem0.local)."""
    from types import SimpleNamespace

    from app.models import TaskCard, TaskCardStatus
    from app.utils.planka import PlankaMirrorHttpClient

    calls = []

    class CaptureClient(PlankaMirrorHttpClient):
        async def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {"item": {"id": "1"}}

        def _get_map(self, *_a, **_k):
            return None

        def _upsert_map(self, *_a, **_k):
            return None

    ws = _mk_workspace(db_session)
    task = TaskCard(
        workspace_id=ws.id,
        title="Assignee link",
        status=TaskCardStatus.tasks,
        assignee="S0293",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    monkeypatch.setattr(
        "app.utils.creator_identity.resolve_actor_identities_with_db",
        lambda _db, _actors: {
            "S0293": SimpleNamespace(
                display_name="João Carlos Pereira",
                avatar_url="https://example.com/j.jpg",
                email="joaocarlos@sysmo.com.br",
            )
        },
    )
    monkeypatch.setattr(
        "app.utils.creator_identity.identity_for_actor",
        lambda actor, identities: identities.get(actor),
    )

    client = CaptureClient(db_session)
    await client._mirror_task_assignee(task, "1001")
    assert calls
    body = calls[0][2]
    assert body["email"] == "joaocarlos@sysmo.com.br"
    assert body["name"] == "João Carlos Pereira"


class TestMirrorDocumentAndDelete:
    @pytest.mark.asyncio
    async def test_mirror_document(self, db_session, client):
        ws = _mk_workspace(db_session)
        doc = SpecDocument(
            workspace_id=ws.id,
            document_type=DocumentType.prd,
            current_version=1,
            current_content="# PRD",
        )
        db_session.add(doc)
        db_session.commit()
        db_session.refresh(doc)

        await client.mirror_document(ws.id, "prd")
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_DOCUMENT, spec_id=doc.id)
            .count()
            == 1
        )

    @pytest.mark.asyncio
    async def test_delete_task_removes_map(self, db_session, client):
        ws = _mk_workspace(db_session)
        task = TaskCard(workspace_id=ws.id, title="Apagar", status=TaskCardStatus.tasks)
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        await client.mirror_task(task.id)
        await client.delete_task(task.id)
        assert (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_TASK, spec_id=task.id)
            .count()
            == 0
        )


class TestSetProjectLifecycle:
    """Tarefa kanban-archive-lifecycle: espelho de isArchived/isCompleted."""

    @pytest.mark.asyncio
    async def test_noop_without_project_map(self, db_session, client, planka_router):
        ws = _mk_workspace(db_session)
        await client.set_project_lifecycle(ws.id, is_archived=True, is_completed=True)
        assert planka_router.project_patch_calls == []

    @pytest.mark.asyncio
    async def test_patches_mapped_project(self, db_session, client, planka_router):
        ws = _mk_workspace(db_session)
        board_id = await client.ensure_workspace_board(ws.id)
        assert board_id

        await client.set_project_lifecycle(ws.id, is_archived=True, is_completed=True)

        assert len(planka_router.project_patch_calls) == 1
        _project_id, body = planka_router.project_patch_calls[0]
        assert body == {"isArchived": True, "isCompleted": True}

    @pytest.mark.asyncio
    async def test_404_on_mapped_project_is_swallowed(
        self, db_session, client, planka_router
    ):
        ws = _mk_workspace(db_session)
        await client.ensure_workspace_board(ws.id)
        project_row = (
            db_session.query(SpecPlankaIdMap)
            .filter_by(entity_type=ENTITY_PROJECT, spec_id=ws.id)
            .one()
        )
        planka_router.fail_next = {"path": f"/api/projects/{project_row.planka_id}", "status": 404}
        # Não deve levantar — projeto removido direto no PLANKA, mapa órfão.
        await client.set_project_lifecycle(ws.id, is_archived=True, is_completed=True)


class TestPlankaErrors:
    @pytest.mark.asyncio
    async def test_5xx_raises_planka_mirror_error(
        self, db_session, planka_router, monkeypatch
    ):
        monkeypatch.setenv("INTERNAL_ACCESS_TOKEN", "t")
        planka_router.fail_next = {"path": "/api/projects", "status": 503}
        ws = _mk_workspace(db_session)
        client = PlankaMirrorHttpClient(
            db_session,
            base_url="http://planka.test",
            transport=httpx.MockTransport(planka_router.handler),
        )
        with pytest.raises(PlankaMirrorError) as exc:
            await client.ensure_workspace_board(ws.id)
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_timeout_raises(self, db_session, planka_router, monkeypatch):
        monkeypatch.setenv("INTERNAL_ACCESS_TOKEN", "t")
        planka_router.timeout_paths.add("/api/projects")
        ws = _mk_workspace(db_session)
        client = PlankaMirrorHttpClient(
            db_session,
            base_url="http://planka.test",
            transport=httpx.MockTransport(planka_router.handler),
        )
        with pytest.raises(PlankaMirrorError) as exc:
            await client.ensure_workspace_board(ws.id)
        assert exc.value.status_code == 504


class TestMigrationSqlite:
    @pytest.fixture
    def alembic_cfg(self, tmp_path, monkeypatch):
        from alembic.config import Config

        db_path = tmp_path / "planka_map.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            "[alembic]\n"
            "script_location = alembic\n"
            "sqlalchemy.url = driver://user:pass@localhost/dbname\n"
            "\n"
            "[loggers]\nkeys = root\n\n"
            "[handlers]\nkeys = console\n\n"
            "[formatters]\nkeys = generic\n\n"
            "[logger_root]\nlevel = WARN\nhandlers = console\n\n"
            "[handler_console]\nclass = StreamHandler\n"
            "args = (sys.stderr,)\nlevel = NOTSET\nformatter = generic\n\n"
            "[formatter_generic]\nformat = %(levelname)s %(message)s\n"
        )
        cfg = Config(str(ini))
        cfg.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[1] / "alembic"),
        )
        return cfg, f"sqlite:///{db_path}"

    def test_upgrade_creates_spec_planka_id_map(self, alembic_cfg):
        from alembic import command

        cfg, url = alembic_cfg
        command.upgrade(cfg, "k6f7a8b9c0d1")
        eng = create_engine(url)
        assert "spec_planka_id_map" not in set(sa.inspect(eng).get_table_names())
        eng.dispose()

        command.upgrade(cfg, "head")
        eng = create_engine(url)
        tables = set(sa.inspect(eng).get_table_names())
        assert "spec_planka_id_map" in tables
        uniques = {
            uc["name"] for uc in sa.inspect(eng).get_unique_constraints("spec_planka_id_map")
        }
        assert "uq_spec_planka_entity_spec" in uniques
        assert "uq_spec_planka_entity_planka" in uniques
        eng.dispose()
