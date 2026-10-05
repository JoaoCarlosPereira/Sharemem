"""PLANKA → Spec: cards criados na tela do PLANKA (sessão JWT) viram tasks Spec.

Cobre o webhook card-created (auth, idempotência, validação de IDs), a não
duplicação de cards criados pelo espelho Spec → PLANKA, o fluxo created →
updated → moved no mesmo card e a adoção no ``claim_task`` de task sem dono.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Project,
    SpecAuditLog,
    SpecPlankaIdMap,
    SpecWorkspace,
    TaskCard,
    TaskCardStatus,
)
from app.routers.specs import router as specs_router
from app.utils.planka import (
    DOCUMENT_LIST_ENTITY,
    ENTITY_BOARD,
    ENTITY_DOCUMENT,
    ENTITY_TASK,
    PlankaMirrorHttpClient,
    list_entity_type,
)
from app.utils.planka_import import import_planka_card

BOARD = "9001"
LISTS = {
    "tasks": "8001",
    "em_andamento": "8002",
    "revisao_codigo": "8003",
    "fase_teste": "8004",
    "concluido": "8005",
}
DOC_LIST = "8006"
TOKEN = "bridge-secret"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def db(factory):
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _seed_board(db) -> SpecWorkspace:
    if not db.query(Project).filter(Project.name == "mem0-shared").first():
        db.add(Project(name="mem0-shared"))
        db.commit()
    ws = SpecWorkspace(project_id="mem0-shared", slug=f"ws-{uuid4().hex[:6]}", name="Melhorias")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    rows = [SpecPlankaIdMap(entity_type=ENTITY_BOARD, spec_id=ws.id, planka_id=BOARD)]
    rows += [
        SpecPlankaIdMap(entity_type=list_entity_type(status), spec_id=ws.id, planka_id=list_id)
        for status, list_id in LISTS.items()
    ]
    rows.append(SpecPlankaIdMap(entity_type=DOCUMENT_LIST_ENTITY, spec_id=ws.id, planka_id=DOC_LIST))
    db.add_all(rows)
    db.commit()
    return ws


def _tasks(db, ws) -> list[TaskCard]:
    db.expire_all()
    return db.query(TaskCard).filter(TaskCard.workspace_id == ws.id).all()


@pytest.fixture
def http(factory, monkeypatch):
    monkeypatch.setenv("PLANKA_MIRROR_SYNC", "0")
    monkeypatch.setenv("PLANKA_INTERNAL_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.delenv("INTERNAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PLANKA_IMPORT_UI_CARDS", raising=False)

    app = FastAPI()
    app.include_router(specs_router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Criação pela tela (card-created)
# --------------------------------------------------------------------------- #
def test_card_created_in_ui_becomes_spec_task(http, db):
    client = http
    ws = _seed_board(db)
    resp = client.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={
            "planka_card_id": "7001",
            "planka_list_id": LISTS["tasks"],
            "name": "Card criado no Planka",
            "description": "detalhes",
            "position": 131072,
            "actor": "joaocarlos@sysmo.com.br",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["action"] == "import"
    assert body["status"] == "tasks"

    [task] = _tasks(db, ws)
    assert str(task.id) == body["task_id"]
    assert task.title == "Card criado no Planka"
    assert task.description == "detalhes"
    assert task.status == TaskCardStatus.tasks
    assert task.assignee is None
    assert task.position == 131072
    mapping = (
        db.query(SpecPlankaIdMap)
        .filter(SpecPlankaIdMap.entity_type == ENTITY_TASK, SpecPlankaIdMap.planka_id == "7001")
        .one()
    )
    assert mapping.spec_id == task.id
    audit = db.query(SpecAuditLog).filter(SpecAuditLog.action == "import_planka_card").one()
    assert audit.actor == "joaocarlos@sysmo.com.br"

    # Visível pela API de listagem usada pelo MCP list_tasks.
    listed = client.get(f"/api/v1/specs/workspaces/{ws.id}/tasks")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(task.id)]


def test_card_created_status_follows_list_without_claim(http, db):
    client = http
    ws = _seed_board(db)
    resp = client.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={"planka_card_id": "7002", "planka_list_id": LISTS["revisao_codigo"], "name": "R"},
    )
    assert resp.json()["status"] == "revisao_codigo"
    [task] = _tasks(db, ws)
    assert task.status == TaskCardStatus.revisao_codigo
    assert task.assignee is None
    assert task.last_activity_at is None  # timeout worker não libera o que nunca foi claimado


def test_card_created_twice_is_idempotent(http, db):
    client = http
    ws = _seed_board(db)
    payload = {"planka_card_id": "7003", "planka_list_id": LISTS["tasks"], "name": "Dup"}
    first = client.post("/api/v1/specs/planka/card-created", headers=AUTH, json=payload).json()
    second = client.post("/api/v1/specs/planka/card-created", headers=AUTH, json=payload).json()
    assert first["applied"] is True
    assert second == {**second, "applied": False, "reason": "already_mapped"}
    assert second["task_id"] == first["task_id"]
    assert len(_tasks(db, ws)) == 1


@pytest.mark.parametrize(
    ("list_id", "reason"),
    [(DOC_LIST, "document_list"), ("8999", "not_mapped")],
)
def test_card_created_in_unmapped_or_sdd_list_is_ignored(http, db, list_id, reason):
    client = http
    ws = _seed_board(db)
    resp = client.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={"planka_card_id": "7004", "planka_list_id": list_id, "name": "X"},
    )
    assert resp.status_code == 200
    assert resp.json() == {**resp.json(), "applied": False, "reason": reason}
    assert _tasks(db, ws) == []


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer errado"}])
def test_card_created_requires_bridge_token(http, db, headers):
    ws = _seed_board(db)
    resp = http.post(
        "/api/v1/specs/planka/card-created",
        headers=headers,
        json={"planka_card_id": "7008", "planka_list_id": LISTS["tasks"]},
    )
    assert resp.status_code == 401
    assert _tasks(db, ws) == []


def test_card_created_kill_switch(http, db, monkeypatch):
    client = http
    ws = _seed_board(db)
    monkeypatch.setenv("PLANKA_IMPORT_UI_CARDS", "0")
    resp = client.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={"planka_card_id": "7005", "planka_list_id": LISTS["tasks"], "name": "off"},
    )
    assert resp.json()["reason"] == "import_disabled"
    assert _tasks(db, ws) == []


@pytest.mark.parametrize("bad_id", ["abc", "../users/me", "1" * 33, "12\n", "١٢٣"])
@pytest.mark.parametrize("field", ["planka_card_id", "planka_list_id"])
def test_card_created_rejects_invalid_ids(http, db, field, bad_id):
    client = http
    ws = _seed_board(db)
    payload = {"planka_card_id": "7010", "planka_list_id": LISTS["tasks"], "name": "X", field: bad_id}
    resp = client.post("/api/v1/specs/planka/card-created", headers=AUTH, json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {**resp.json(), "applied": False, "reason": "invalid_id"}
    assert _tasks(db, ws) == []
    db.expire_all()
    assert db.query(SpecPlankaIdMap).filter(SpecPlankaIdMap.entity_type == ENTITY_TASK).count() == 0
    assert db.query(SpecAuditLog).filter(SpecAuditLog.action == "import_planka_card").count() == 0


def test_card_created_accepts_32_digit_id(http, db):
    client = http
    ws = _seed_board(db)
    card_id = "9" * 32
    resp = client.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={"planka_card_id": card_id, "planka_list_id": LISTS["tasks"], "name": "Longo"},
    )
    assert resp.json()["applied"] is True
    [task] = _tasks(db, ws)
    mapping = db.query(SpecPlankaIdMap).filter(SpecPlankaIdMap.planka_id == card_id).one()
    assert mapping.spec_id == task.id


@pytest.mark.parametrize(
    ("card_id", "list_id"),
    [("abc", LISTS["tasks"]), ("../users/me", LISTS["tasks"]), ("1" * 33, LISTS["tasks"]), ("7011", "../x")],
)
def test_import_planka_card_rejects_invalid_ids(db, card_id, list_id):
    ws = _seed_board(db)
    result = import_planka_card(db, planka_card_id=card_id, planka_list_id=list_id, name="X")
    assert result == {"applied": False, "reason": "invalid_id"}
    assert _tasks(db, ws) == []
    assert db.query(SpecPlankaIdMap).filter(SpecPlankaIdMap.entity_type == ENTITY_TASK).count() == 0


def test_card_created_for_document_card_is_noop(db):
    ws = _seed_board(db)
    db.add(SpecPlankaIdMap(entity_type=ENTITY_DOCUMENT, spec_id=uuid4(), planka_id="7006"))
    db.commit()
    result = import_planka_card(db, planka_card_id="7006", planka_list_id=LISTS["tasks"], name="[prd]")
    assert result == {"applied": False, "reason": "document_card"}
    assert _tasks(db, ws) == []


# --------------------------------------------------------------------------- #
# Cards criados pelo espelho (Bearer INTERNAL) não geram import
# --------------------------------------------------------------------------- #
class _MirrorPlanka:
    def __init__(self):
        self.seq = 5000
        self.card_creates: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/cards"):
            self.card_creates.append(request)
            self.seq += 1
            return httpx.Response(200, json={"item": {"id": str(self.seq)}})
        if method == "PUT" and path.endswith("/mem0-assignee"):
            return httpx.Response(200, json={"item": {}})
        return httpx.Response(200, json={"item": {"id": "x"}})


@pytest.mark.asyncio
async def test_mirror_created_card_is_linked_and_never_imported(db):
    """espelho (``internal``) não dispara card-created; eco eventual → already_mapped."""
    ws = _seed_board(db)
    task = TaskCard(workspace_id=ws.id, title="Criada pelo MCP")
    db.add(task)
    db.commit()
    planka = _MirrorPlanka()
    mirror = PlankaMirrorHttpClient(db, base_url="http://planka", transport=httpx.MockTransport(planka.handler))
    await mirror.mirror_task(task.id)

    assert len(planka.card_creates) == 1
    planka_card_id = str(planka.seq)
    # Vínculo já commitado logo após o POST (outra sessão enxerga).
    other = sessionmaker(bind=db.get_bind())()
    try:
        row = other.query(SpecPlankaIdMap).filter_by(entity_type=ENTITY_TASK, planka_id=planka_card_id).one()
        assert row.spec_id == task.id
    finally:
        other.close()

    result = import_planka_card(
        db, planka_card_id=planka_card_id, planka_list_id=LISTS["tasks"], name="Criada pelo MCP"
    )
    assert result == {"applied": False, "reason": "already_mapped", "task_id": str(task.id)}
    assert len(_tasks(db, ws)) == 1


def test_concurrent_import_loses_to_existing_mapping(db, monkeypatch):
    """Corrida: o map aparece entre a checagem e o commit → sem task duplicada."""
    ws = _seed_board(db)
    winner = TaskCard(workspace_id=ws.id, title="vencedor")
    db.add(winner)
    db.commit()

    import app.utils.planka_import as mod

    real = mod._task_map_for_card
    calls = {"n": 0}

    def racy(session, card_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # Outro worker grava o vínculo logo após a nossa checagem.
            other = sessionmaker(bind=session.get_bind())()
            other.add(SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=winner.id, planka_id="7007"))
            other.commit()
            other.close()
            return None
        return real(session, card_id)

    monkeypatch.setattr(mod, "_task_map_for_card", racy)
    result = import_planka_card(db, planka_card_id="7007", planka_list_id=LISTS["tasks"], name="perdedor")
    assert result == {"applied": False, "reason": "already_mapped", "task_id": str(winner.id)}
    assert [t.title for t in _tasks(db, ws)] == ["vencedor"]


@pytest.mark.asyncio
async def test_mirror_link_integrity_error_is_non_fatal_mirror_error(db):
    """card recém-criado já vinculado a outra task → PlankaMirrorError 409, sem 500."""
    from app.utils.planka import PlankaMirrorError

    ws = _seed_board(db)
    winner = TaskCard(workspace_id=ws.id, title="importada pelo card-created")
    task = TaskCard(workspace_id=ws.id, title="Criada pelo MCP")
    db.add_all([winner, task])
    db.commit()
    planka = _MirrorPlanka()
    # O próximo card criado pelo mock será "5001", já vinculado ao vencedor.
    db.add(SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=winner.id, planka_id=str(planka.seq + 1)))
    db.commit()
    mirror = PlankaMirrorHttpClient(db, base_url="http://planka", transport=httpx.MockTransport(planka.handler))
    with pytest.raises(PlankaMirrorError) as exc_info:
        await mirror.mirror_task(task.id)
    assert exc_info.value.status_code == 409
    db.expire_all()
    rows = db.query(SpecPlankaIdMap).filter(SpecPlankaIdMap.entity_type == ENTITY_TASK).all()
    assert [(r.spec_id, r.planka_id) for r in rows] == [(winner.id, "5001")]


def test_mirror_link_conflict_maps_to_502_mirror_failed(db, monkeypatch):
    """via ``run_mirror`` o conflito vira 502 mirror_failed (fluxo existente)."""
    from fastapi import HTTPException

    from app.utils import planka_hooks
    from app.utils.planka import PlankaMirrorError

    monkeypatch.setenv("PLANKA_MIRROR_SYNC", "1")

    async def boom(_db, _op):
        raise PlankaMirrorError(409, "card PLANKA 1 já vinculado a outra task")

    monkeypatch.setattr(planka_hooks, "_call", boom)
    with pytest.raises(HTTPException) as exc_info:
        planka_hooks.mirror_task(db, uuid4())
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["planka_status"] == 409


# --------------------------------------------------------------------------- #
# Fluxo created → updated → moved no mesmo card
# --------------------------------------------------------------------------- #
def test_created_then_updated_then_moved_keeps_single_task(http, db):
    ws = _seed_board(db)
    created = http.post(
        "/api/v1/specs/planka/card-created",
        headers=AUTH,
        json={"planka_card_id": "7100", "planka_list_id": LISTS["tasks"], "name": "Original"},
    ).json()
    assert created["applied"] is True

    updated = http.post(
        "/api/v1/specs/planka/card-updated",
        headers=AUTH,
        json={"planka_card_id": "7100", "changed_fields": ["name"], "name": "Renomeado"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["task_id"] == created["task_id"]

    moved = http.post(
        "/api/v1/specs/planka/card-moved",
        headers=AUTH,
        json={"planka_card_id": "7100", "planka_list_id": LISTS["em_andamento"], "actor": "ana@x.y"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["task_id"] == created["task_id"]

    [task] = _tasks(db, ws)
    assert str(task.id) == created["task_id"]
    assert task.title == "Renomeado"
    assert task.status == TaskCardStatus.em_andamento
    assert db.query(SpecPlankaIdMap).filter_by(entity_type=ENTITY_TASK).count() == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("card-moved", {"planka_card_id": "7200", "planka_list_id": LISTS["em_andamento"]}),
        ("card-updated", {"planka_card_id": "7200", "changed_fields": ["name"], "name": "X"}),
    ],
)
def test_moved_or_updated_of_unmapped_card_is_not_imported(http, db, path, body):
    """sem card-created entregue, moved/updated não importam: só o backfill admin recupera."""
    ws = _seed_board(db)
    resp = http.post(f"/api/v1/specs/planka/{path}", headers=AUTH, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {**resp.json(), "applied": False, "reason": "not_mapped"}
    assert _tasks(db, ws) == []


# --------------------------------------------------------------------------- #
# Claim de task importada sem dono fora do backlog
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["em_andamento", "revisao_codigo", "fase_teste"])
def test_claim_adopts_unassigned_task_keeping_column(db, status):
    from app.models import TaskStatusHistory
    from app.utils.task_lock import claim_task

    ws = _seed_board(db)
    task = TaskCard(workspace_id=ws.id, title="Importada", status=TaskCardStatus(status))
    db.add(task)
    db.commit()
    v0 = task.version

    res = claim_task(db, task.id, "DESKTOP-01")
    assert res.claimed is True and res.status == status
    db.refresh(task)
    assert task.assignee == "DESKTOP-01"
    assert task.status == TaskCardStatus(status)  # coluna mantida: não é transição
    assert task.version == v0 + 1
    assert task.last_activity_at is not None  # entra no lease a partir de agora
    assert db.query(TaskStatusHistory).filter_by(task_id=task.id).count() == 0
    audit = db.query(SpecAuditLog).filter(SpecAuditLog.action == "adopt_task").one()
    assert audit.detail["from_status"] == status and audit.detail["to_status"] == status

    # Já com dono: terceiro continua barrado pela exclusividade.
    other = claim_task(db, task.id, "DESKTOP-02")
    assert other.claimed is False and other.current_assignee == "DESKTOP-01"


def test_claim_of_unassigned_concluido_task_is_refused(db):
    from app.utils.task_lock import claim_task

    ws = _seed_board(db)
    task = TaskCard(workspace_id=ws.id, title="Feita", status=TaskCardStatus.concluido)
    db.add(task)
    db.commit()
    res = claim_task(db, task.id, "DESKTOP-01")
    assert res.claimed is False
    db.refresh(task)
    assert task.assignee is None and task.status == TaskCardStatus.concluido


def test_claim_from_backlog_still_moves_to_em_andamento(db):
    from app.utils.task_lock import claim_task

    ws = _seed_board(db)
    task = TaskCard(workspace_id=ws.id, title="Backlog", status=TaskCardStatus.tasks)
    db.add(task)
    db.commit()
    res = claim_task(db, task.id, "DESKTOP-01")
    assert res.claimed is True and res.status == "em_andamento"
    db.refresh(task)
    assert task.status == TaskCardStatus.em_andamento


# --------------------------------------------------------------------------- #
# Espelho: vínculo corrompido no mapa nunca vira path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../users/me", "abc", "12\n", "١٢٣"])
async def test_mirror_skips_invalid_mapped_card_id_without_http(db, bad, monkeypatch):
    import app.utils.planka as planka_mod

    ws = _seed_board(db)
    task = TaskCard(workspace_id=ws.id, title="Vínculo ruim")
    db.add(task)
    db.commit()
    db.add(SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=task.id, planka_id=bad))
    db.commit()
    calls: list[str] = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"item": {"id": "1"}})

    warnings: list[str] = []
    monkeypatch.setattr(planka_mod.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    mirror = PlankaMirrorHttpClient(db, base_url="http://planka", transport=httpx.MockTransport(handler))
    await mirror.mirror_task(task.id)
    await mirror.mirror_task_status(task.id)
    await mirror.mirror_comment("task", task.id, "oi", author="a@b.c")
    await mirror.delete_task(task.id)
    await mirror._mirror_task_assignee(task, bad)
    assert calls == []
    assert sum("planka_mirror_invalid_mapped_id" in w for w in warnings) == 4
    assert any("planka_mirror_invalid_card_id" in w for w in warnings)
    # O vínculo ruim não é apagado nem sobrescrito (só ignorado).
    db.expire_all()
    assert db.query(SpecPlankaIdMap).filter(SpecPlankaIdMap.spec_id == task.id).one().planka_id == bad


def test_item_id_rejects_non_numeric_planka_ids():
    from app.utils.planka import PlankaMirrorError, _item_id

    assert _item_id({"item": {"id": 123}}) == "123"
    for bad in ("../users/me", "x", "1" * 33):
        with pytest.raises(PlankaMirrorError) as exc_info:
            _item_id({"item": {"id": bad}})
        assert exc_info.value.status_code == 502
