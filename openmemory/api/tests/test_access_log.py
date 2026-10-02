"""Per-memory access log (card 01ada614): identity, grouping, channels, MCP path."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_module
from app.database import Base, get_db
from app.models import USER_TYPE_LEGACY_HOST, USER_TYPE_PERSON, Machine, MachineStatus, User
from app.read_audit_log_model import ReadAuditLog
from app.utils.creator_identity import (
    UI_ANONYMOUS_ACTOR,
    UI_ANONYMOUS_LABEL,
    enrich_reader_items,
    is_anonymous_ui_actor,
    session_user_pk,
    resolve_reader_identities_with_db,
    split_ui_actor,
)
from app.utils.read_audit import (
    access_channel,
    group_read_audit_rows,
    list_memory_read_audit_page,
)

BASE = datetime(2026, 10, 2, 13, 0, 0)


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "SessionLocal", factory)
    monkeypatch.setattr("app.utils.read_audit.SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def people(db_factory):
    """S0293 linked to Ana (Google person); S0258 linked to Bruno."""
    db = db_factory()
    ana = User(
        user_id="google-ana",
        google_sub="google-ana",
        email="ana@sysmo.com.br",
        display_name="Ana Souza",
        avatar_url="https://example.com/ana.png",
        user_type=USER_TYPE_PERSON,
    )
    bruno = User(
        user_id="google-bruno",
        google_sub="google-bruno",
        display_name="Bruno Lima",
        avatar_url="https://example.com/bruno.png",
        user_type=USER_TYPE_PERSON,
    )
    legacy_a = User(user_id="S0293", user_type=USER_TYPE_LEGACY_HOST)
    legacy_b = User(user_id="S0258", user_type=USER_TYPE_LEGACY_HOST)
    db.add_all([ana, bruno, legacy_a, legacy_b])
    db.flush()
    db.add_all(
        [
            Machine(hostname="S0293", linked_user_id=ana.id, legacy_user_id=legacy_a.id, status=MachineStatus.linked),
            Machine(hostname="S0258", linked_user_id=bruno.id, legacy_user_id=legacy_b.id, status=MachineStatus.linked),
        ]
    )
    db.commit()
    ids = {"ana": str(ana.id), "bruno": str(bruno.id)}
    db.close()
    return ids


def _add_rows(db_factory, memory_id: str, rows: list[tuple]) -> None:
    """rows: (seconds_offset, source, access_type, hostname, client, query)."""
    db = db_factory()
    for offset, source, access_type, hostname, client, query in rows:
        db.add(
            ReadAuditLog(
                project="sysmovs",
                memory_id=memory_id,
                access_type=access_type,
                source=source,
                hostname=hostname,
                client_name=client,
                query=query,
                accessed_at=BASE + timedelta(seconds=offset),
            )
        )
    db.commit()
    db.close()


def _client(db_factory) -> TestClient:
    from app.routers.memories import router as memories_router

    app = FastAPI()
    app.include_router(memories_router)

    def _override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


# --------------------------------------------------------------------------- #
# creator_identity: ``ui:<user_id>`` actors
# --------------------------------------------------------------------------- #
def test_split_ui_actor_and_lookup_key():
    assert split_ui_actor("ui:S0293") == "S0293"
    assert split_ui_actor("S0293") is None
    assert split_ui_actor("ui:") is None
    pk = uuid.uuid4()
    assert session_user_pk(f"ui:{pk}") == pk
    assert session_user_pk(str(pk)) is None  # bare UUID is not a session actor
    assert session_user_pk("ui:S0293") is None
    assert session_user_pk(UI_ANONYMOUS_ACTOR) is None


def test_is_anonymous_ui_actor_only_session_uuid_names_a_person(monkeypatch):
    monkeypatch.setenv("OPENMEMORY_UI_USER_ID", "S0293")
    assert is_anonymous_ui_actor(UI_ANONYMOUS_ACTOR)
    assert is_anonymous_ui_actor("ui:S0293")  # historical build id
    assert is_anonymous_ui_actor("ui:s0293")
    assert is_anonymous_ui_actor("ui:openmemory")
    assert is_anonymous_ui_actor("ui:default_user")
    assert is_anonymous_ui_actor("ui:")
    # Even when the API container lacks OPENMEMORY_UI_USER_ID (compose only
    # passes it to the UI), a non-UUID ui: actor is not a session person.
    monkeypatch.delenv("OPENMEMORY_UI_USER_ID")
    assert is_anonymous_ui_actor("ui:S0293")
    assert not is_anonymous_ui_actor(f"ui:{uuid.uuid4()}")
    assert not is_anonymous_ui_actor("S0293")  # MCP machine hostname
    assert not is_anonymous_ui_actor(None)


def test_resolve_reader_identities_session_person_and_mcp(db_factory, people):
    bruno_ui = f"ui:{people['bruno']}"
    db = db_factory()
    try:
        identities = resolve_reader_identities_with_db(
            db,
            [
                ("ui:S0293", "api"),
                (UI_ANONYMOUS_ACTOR, "api"),
                (bruno_ui, "api"),
                ("S0258", "mcp"),
                (f"ui:{uuid.uuid4()}", "api"),
                ("OTHER", "mcp"),
            ],
        )
    finally:
        db.close()
    # Web UI ui:<User.id> (Google session JWT ``sub``) → person
    assert identities[(bruno_ui, True)].display_name == "Bruno Lima"
    # bare MCP hostname still resolves through the linked machine
    assert identities[("S0258", False)].display_name == "Bruno Lima"
    # B1: the shared build id (S0293 is Ana's machine) must NOT become Ana.
    assert ("ui:S0293", True) not in identities
    assert (UI_ANONYMOUS_ACTOR, True) not in identities
    assert ("OTHER", False) not in identities


@pytest.mark.parametrize("source", ["mcp", "compat_v3", "", None, "other"])
def test_non_web_channels_never_resolve_session_email_or_user_id(db_factory, people, source):
    """B2: client-asserted actors only resolve as a linked machine hostname."""
    forged = [
        f"ui:{people['bruno']}",  # session shape
        people["bruno"],  # bare User.id
        f"ui:{people['bruno'].upper()}",
        "google-bruno",  # User.user_id
        "ui:google-bruno",
        "bruno@sysmo.com.br",  # e-mail
        "ui:ana@sysmo.com.br",
        "ui:S0258",  # prefixed hostname is not the hostname
    ]
    db = db_factory()
    try:
        identities = resolve_reader_identities_with_db(db, [(actor, source) for actor in forged + ["S0258"]])
    finally:
        db.close()
    assert set(identities) == {("S0258", False)}


def test_web_channel_does_not_resolve_email_or_user_id(db_factory, people):
    db = db_factory()
    try:
        identities = resolve_reader_identities_with_db(
            db,
            [("ui:google-bruno", "api"), ("ui:bruno@sysmo.com.br", "admin"), (people["bruno"], "api")],
        )
    finally:
        db.close()
    assert identities == {}


def test_legacy_host_user_without_display_is_not_used(db_factory, people):
    """Bare legacy host users have no name/avatar → caller keeps hostname fallback."""
    db = db_factory()
    db.add(User(user_id="S0999", user_type=USER_TYPE_LEGACY_HOST))
    db.commit()
    try:
        assert resolve_reader_identities_with_db(db, [("S0999", "mcp")]) == {}
    finally:
        db.close()


def test_enrich_reader_items_attaches_display(db_factory, people):
    items = [
        {"hostname": "ui:S0293", "source": "api", "display_name": "S0293", "avatar_url": "https://stale"},
        {"hostname": "S0258", "source": "mcp"},
        {"hostname": "S0777", "source": "mcp"},
        {"hostname": f"ui:{people['ana']}", "source": "api"},
        {"hostname": f"ui:{people['ana']}", "source": "mcp", "display_name": "x"},
    ]
    enrich_reader_items(items)
    # Historical build-id rows: neutral label, no person, no avatar.
    assert items[0]["display_name"] == UI_ANONYMOUS_LABEL
    assert "avatar_url" not in items[0] and items[0]["anonymous"] is True
    assert items[1]["display_name"] == "Bruno Lima"
    assert "avatar_url" not in items[2]
    assert items[3]["display_name"] == "Ana Souza"
    assert items[3]["avatar_url"] == "https://example.com/ana.png"
    assert "anonymous" not in items[3]
    # Same string over MCP: not a session, not anonymous-web — left as declared.
    assert items[4]["display_name"] == "x" and "avatar_url" not in items[4]
    assert "anonymous" not in items[4]


# --------------------------------------------------------------------------- #
# Grouping (pure)
# --------------------------------------------------------------------------- #
def _row(offset, hostname="ui:S0293", source="api", access_type="get", client="openmemory"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        hostname=hostname,
        source=source,
        client_name=client,
        access_type=access_type,
        query=None,
        accessed_at=BASE + timedelta(seconds=offset),
    )


def test_group_collapses_same_actor_type_within_window_even_if_interleaved():
    rows = [
        _row(100),
        _row(99, access_type="list"),
        _row(90),
        _row(80, access_type="list"),
        _row(50, hostname="S0258", source="mcp", access_type="search", client="claude-code"),
        _row(10),
    ]
    groups = group_read_audit_rows(rows, 300)
    sizes = [(g[0].access_type, g[0].hostname, len(g)) for g in groups]
    assert sizes == [
        ("get", "ui:S0293", 3),
        ("list", "ui:S0293", 2),
        ("search", "S0258", 1),
    ]
    assert sum(len(g) for g in groups) == len(rows)  # nothing dropped


def test_group_window_is_anchored_on_newest_row():
    rows = [_row(1000), _row(800), _row(700), _row(650)]  # 1000-650 = 350 > 300
    groups = group_read_audit_rows(rows, 300)
    assert [len(g) for g in groups] == [3, 1]


def test_group_disabled_with_zero_window():
    rows = [_row(3), _row(2), _row(1)]
    assert [len(g) for g in group_read_audit_rows(rows, 0)] == [1, 1, 1]


def test_access_channel_distinguishes_web_mcp_api():
    assert access_channel("api") == ("web", "Interface Web")
    assert access_channel("admin")[0] == "web"
    assert access_channel("mcp") == ("mcp", "MCP")
    assert access_channel("compat_v3")[0] == "api"
    assert access_channel(None, "ui:S0293")[0] == "web"


# --------------------------------------------------------------------------- #
# Endpoint: grouping + pagination + identity + channel filter
# --------------------------------------------------------------------------- #
def test_access_log_groups_ui_noise_and_keeps_mcp_visible(db_factory, people):
    mem = str(uuid.uuid4())
    # Symptom from the card: viewer reloads the page 6× in a minute, burying MCP reads.
    ui_rows = [(600 + i * 10, "api", "get", "ui:S0293", "openmemory", None) for i in range(6)]
    ui_rows += [(600 + i * 10, "api", "list", "ui:S0293", "openmemory", None) for i in range(6)]
    mcp_rows = [
        (300, "mcp", "search", "S0258", "claude-code", "regra de frete"),
        (200, "compat_v3", "search", "S0258", None, "frete ecv212"),
        (100, "mcp", "list", "S0258", "cursor", None),
    ]
    _add_rows(db_factory, mem, ui_rows + mcp_rows)

    resp = _client(db_factory).get(f"/api/v1/memories/{mem}/access-log?page=1&page_size=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw_total"] == 15
    assert body["total"] == 5
    assert body["grouped"] is True and body["group_window_seconds"] == 300
    assert body["channel_counts"] == {"web": 12, "mcp": 2, "api": 1, "other": 0}

    logs = body["logs"]
    first = logs[0]
    assert first["count"] == 6 and first["channel"] == "web"
    assert first["channel_label"] == "Interface Web"
    # B1: ``ui:S0293`` was the shared build id, not Ana → neutral, no avatar.
    assert first["display_name"] == UI_ANONYMOUS_LABEL
    assert "avatar_url" not in first
    assert first["anonymous"] is True
    assert "queries" not in first and "entry_ids" not in first  # M5: lean payload
    assert body["grouping_truncated"] is False
    assert first["first_accessed_at"] < first["last_accessed_at"] == first["accessed_at"]

    mcp = [log for log in logs if log["channel"] == "mcp"]
    assert {(m["access_type"], m["client_name"]) for m in mcp} == {("search", "claude-code"), ("list", "cursor")}
    assert all(m["display_name"] == "Bruno Lima" for m in mcp)
    search = next(m for m in mcp if m["access_type"] == "search")
    assert search["query"] == "regra de frete" and search["count"] == 1
    compat = next(log for log in logs if log["channel"] == "api")
    assert compat["source"] == "compat_v3"

    # Raw rows are untouched by grouping.
    db = db_factory()
    try:
        assert db.query(ReadAuditLog).filter(ReadAuditLog.memory_id == mem).count() == 15
    finally:
        db.close()


def test_access_log_pagination_is_over_grouped_entries(db_factory, people):
    mem = str(uuid.uuid4())
    # 4 distinct readers × 3 reads each, 20 min apart → 12 groups (window 5 min).
    rows = []
    for i in range(12):
        host = f"S0{300 + (i % 4)}"
        rows.append((i * 1200, "mcp", "search", host, "claude-code", f"q{i}"))
        rows.append((i * 1200 + 5, "mcp", "search", host, "claude-code", f"q{i}"))
    _add_rows(db_factory, mem, rows)
    client = _client(db_factory)

    seen: list[str] = []
    for page in (1, 2, 3):
        body = client.get(f"/api/v1/memories/{mem}/access-log?page={page}&page_size=5").json()
        assert body["total"] == 12 and body["raw_total"] == 24
        seen.extend(log["id"] for log in body["logs"])
        assert all(log["count"] == 2 for log in body["logs"])
    assert len(seen) == 12 and len(set(seen)) == 12  # no overlap, nothing lost
    stamps = [
        log["accessed_at"]
        for page in (1, 2, 3)
        for log in client.get(f"/api/v1/memories/{mem}/access-log?page={page}&page_size=5").json()["logs"]
    ]
    assert stamps == sorted(stamps, reverse=True)


def test_access_log_ungrouped_and_channel_filter(db_factory, people):
    mem = str(uuid.uuid4())
    rows = [(i, "api", "get", "ui:S0293", "openmemory", None) for i in range(5)]
    rows.append((100, "mcp", "search", "S0258", "claude-code", "x"))
    _add_rows(db_factory, mem, rows)
    client = _client(db_factory)

    raw = client.get(f"/api/v1/memories/{mem}/access-log?grouped=false").json()
    assert raw["total"] == 6 and len(raw["logs"]) == 6 and raw["grouped"] is False
    assert all(log["count"] == 1 for log in raw["logs"])

    only_mcp = client.get(f"/api/v1/memories/{mem}/access-log?channel=mcp").json()
    assert only_mcp["total"] == 1 and only_mcp["raw_total"] == 1
    assert only_mcp["logs"][0]["display_name"] == "Bruno Lima"
    assert only_mcp["channel_counts"]["web"] == 5  # counts stay global for the toggle

    rows_api = [(200, "compat_v3", "search", "S0258", None, "y")]
    _add_rows(db_factory, mem, rows_api)
    agents = client.get(f"/api/v1/memories/{mem}/access-log?channel=agents").json()
    assert agents["raw_total"] == 2
    assert {log["channel"] for log in agents["logs"]} == {"mcp", "api"}

    assert client.get(f"/api/v1/memories/{mem}/access-log?channel=bogus").status_code == 422


def test_access_log_window_env_override(db_factory, people, monkeypatch):
    mem = str(uuid.uuid4())
    _add_rows(db_factory, mem, [(0, "api", "get", "ui:S0293", "openmemory", None), (120, "api", "get", "ui:S0293", "openmemory", None)])
    monkeypatch.setenv("ACCESS_LOG_GROUP_WINDOW_SECONDS", "60")
    db = db_factory()
    try:
        result = list_memory_read_audit_page(db, mem)
    finally:
        db.close()
    assert result["total"] == 2 and result["group_window_seconds"] == 60


def test_access_log_grouping_truncated_flag(db_factory, people, monkeypatch):
    mem = str(uuid.uuid4())
    _add_rows(db_factory, mem, [(i * 1000, "mcp", "search", "S0258", "cursor", None) for i in range(5)])
    monkeypatch.setenv("ACCESS_LOG_GROUP_SCAN_LIMIT", "3")
    body = _client(db_factory).get(f"/api/v1/memories/{mem}/access-log").json()
    assert body["raw_total"] == 5
    assert body["grouping_truncated"] is True
    assert body["total"] == 3  # only the 3 newest rows were grouped
    monkeypatch.setenv("ACCESS_LOG_GROUP_SCAN_LIMIT", "10")
    assert _client(db_factory).get(f"/api/v1/memories/{mem}/access-log").json()["grouping_truncated"] is False


def test_access_log_default_scan_limit_is_bounded(monkeypatch):
    from app.utils.read_audit import access_log_group_scan_limit

    monkeypatch.delenv("ACCESS_LOG_GROUP_SCAN_LIMIT", raising=False)
    assert 2000 <= access_log_group_scan_limit() <= 5000


def test_access_log_unknown_hostname_falls_back_to_label(db_factory, people):
    mem = str(uuid.uuid4())
    _add_rows(db_factory, mem, [(0, "mcp", "search", "S0555", "claude-code", "q")])
    log = _client(db_factory).get(f"/api/v1/memories/{mem}/access-log").json()["logs"][0]
    assert log["display_name"] == "S0555"
    assert "avatar_url" not in log


# --------------------------------------------------------------------------- #
# UI reader attribution
# --------------------------------------------------------------------------- #
def test_ui_reader_actor_prefers_session_person_else_anonymous():
    from app.routers.memories import ui_reader_actor
    from app.utils.logging_context import auth_method_var, auth_user_var

    # No session: the client-supplied ?user_id= (shared build id) is ignored.
    assert ui_reader_actor("openmemory") == UI_ANONYMOUS_ACTOR
    assert ui_reader_actor("S0293") == UI_ANONYMOUS_ACTOR
    assert ui_reader_actor(None) == UI_ANONYMOUS_ACTOR
    t1 = auth_method_var.set("team")
    t2 = auth_user_var.set("S0293")
    try:
        assert ui_reader_actor("S0293") == UI_ANONYMOUS_ACTOR  # team token ≠ person
    finally:
        auth_user_var.reset(t2)
        auth_method_var.reset(t1)
    t1 = auth_method_var.set("session")
    t2 = auth_user_var.set("1f0c-person")
    try:
        assert ui_reader_actor("openmemory") == "ui:1f0c-person"
    finally:
        auth_user_var.reset(t2)
        auth_method_var.reset(t1)


# --------------------------------------------------------------------------- #
# B1 endpoints: GET /{id} and POST /shared-filter with / without a session
# --------------------------------------------------------------------------- #
JWT_SECRET = "access-log-test-secret-0123456789abcdef"


def _ui_client(db_factory) -> TestClient:
    """Memories router behind the real auth middleware (session JWT → contextvars)."""
    from app.middleware.team_auth import AuthMiddleware
    from app.routers.memories import router as memories_router

    app = FastAPI()
    app.add_middleware(AuthMiddleware, mode="warn", token_to_team={})
    app.include_router(memories_router)

    def _override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _shared_memory(mem_id: str) -> dict:
    return {
        "id": mem_id,
        "content": "regra de frete",
        "created_at": "2026-10-02T13:00:00+00:00",
        "state": "active",
        "app_id": None,
        "app_name": "sysmovs",
        "created_by_hostname": "S0176",
        "created_by_client": "cursor",
        "categories": [],
        "metadata_": {"project": "sysmovs"},
    }


def _reads(db_factory, mem_id: str) -> list[ReadAuditLog]:
    db = db_factory()
    try:
        return db.query(ReadAuditLog).filter(ReadAuditLog.memory_id == mem_id).all()
    finally:
        db.close()


def _ui_reads(client: TestClient, mem_id: str, headers: dict) -> None:
    page = {"items": [_shared_memory(mem_id)], "total": 1, "page": 1, "size": 10, "pages": 1}
    with (
        patch("app.utils.vector_stats.get_shared_memory_by_id", return_value=_shared_memory(mem_id)),
        patch("app.utils.vector_stats.list_shared_memories", return_value=page),
    ):
        assert client.get(f"/api/v1/memories/{mem_id}?user_id=S0293", headers=headers).status_code == 200
        resp = client.post(
            "/api/v1/memories/shared-filter",
            json={"user_id": "S0293", "project": "sysmovs"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text


@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("OPENMEMORY_UI_USER_ID", "S0293")


def test_ui_reads_without_session_are_anonymous(db_factory, people, jwt_env):
    mem = str(uuid.uuid4())
    client = _ui_client(db_factory)
    _ui_reads(client, mem, headers={"x-client-name": "openmemory-ui"})

    rows = _reads(db_factory, mem)
    assert {(r.access_type, r.hostname) for r in rows} == {("get", UI_ANONYMOUS_ACTOR), ("list", UI_ANONYMOUS_ACTOR)}

    body = client.get(f"/api/v1/memories/{mem}/access-log?grouped=false").json()
    assert body["raw_total"] == 2
    for log in body["logs"]:
        assert log["display_name"] == UI_ANONYMOUS_LABEL
        assert "avatar_url" not in log
        assert "Ana" not in json.dumps(log)


def test_ui_reads_with_session_record_and_resolve_person(db_factory, people, jwt_env):
    from app.utils.session_jwt import issue_session_jwt

    mem = str(uuid.uuid4())
    token = issue_session_jwt(user_id=people["bruno"], email="bruno@sysmo.com.br")
    client = _ui_client(db_factory)
    _ui_reads(client, mem, headers={"Authorization": f"Bearer {token}", "x-client-name": "openmemory-ui"})

    rows = _reads(db_factory, mem)
    assert {r.hostname for r in rows} == {f"ui:{people['bruno']}"}

    body = client.get(
        f"/api/v1/memories/{mem}/access-log?grouped=false",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["raw_total"] == 2
    for log in body["logs"]:
        assert log["display_name"] == "Bruno Lima"
        assert log["avatar_url"] == "https://example.com/bruno.png"
        assert "anonymous" not in log


# --------------------------------------------------------------------------- #
# M2: admin project-memories wrapper actually runs on the registered route
# --------------------------------------------------------------------------- #
def test_admin_project_memories_route_is_audited(monkeypatch):
    import app.routers.memories  # noqa: F401 — installs the wrapper
    from app.routers import admin as admin_mod

    route = next(r for r in admin_mod.router.routes if r.path == "/admin/projects/{project}/memories")
    assert getattr(route.dependant.call, "_read_audit_wrapped", False)
    assert getattr(route.endpoint, "_read_audit_wrapped", False)

    monkeypatch.setenv("ADMIN_TOKEN", "adm-test")
    point = SimpleNamespace(id="m-1", payload={"data": "x", "project": "p"})
    vector_client = MagicMock()
    vector_client.vector_store.list.return_value = ([point], None)
    app = FastAPI()
    app.include_router(admin_mod.router)
    with (
        patch("app.utils.memory.get_memory_client_safe", return_value=vector_client),
        patch("app.utils.partitioning.bind_active_collection"),
        patch("app.utils.read_audit.record_memory_reads") as record,
    ):
        client = TestClient(app)
        resp = client.get("/admin/projects/p/memories", headers={"X-Admin-Token": "adm-test"})
        assert resp.status_code == 200
        assert client.get("/admin/projects/p/memories?limit=9999", headers={"X-Admin-Token": "adm-test"}).status_code == 422
    assert record.call_count == 1
    kwargs = record.call_args.kwargs
    assert kwargs["source"] == "admin" and kwargs["memory_ids"] == ["m-1"]
    assert kwargs["hostname"] == UI_ANONYMOUS_ACTOR


@pytest.mark.asyncio
async def test_drain_pending_read_audits_timeout():
    import asyncio

    from app.utils import mcp_read_wrappers

    blocker = asyncio.get_running_loop().create_future()
    mcp_read_wrappers._pending.add(blocker)
    try:
        assert await mcp_read_wrappers.drain_pending_read_audits(timeout=0.05) is False
    finally:
        mcp_read_wrappers._pending.discard(blocker)
        blocker.cancel()
    assert await mcp_read_wrappers.drain_pending_read_audits(timeout=1) is True


# --------------------------------------------------------------------------- #
# MCP path end-to-end: search + list land in the memory's access log
# --------------------------------------------------------------------------- #
def _registered(name):
    from app import mcp_server

    import app.routers.memories  # noqa: F401 — importing installs the wrappers

    return mcp_server.mcp._tool_manager.get_tool(name)


@pytest.mark.asyncio
async def test_mcp_search_and_list_appear_in_memory_access_log(db_factory, people):
    from app import mcp_server
    from app.utils.mcp_read_wrappers import drain_pending_read_audits

    mem_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    cached = [
        {"id": mem_id, "memory": "regra de frete", "project": "sysmovs", "owner": "S0176", "score": 0.9, "state": "active"},
        {"id": other_id, "memory": "outra", "project": "sysmovs", "owner": "S0176", "score": 0.5, "state": "active"},
    ]
    point = MagicMock()

    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=MagicMock()),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server, "requester_group_for_mcp", return_value=None),
        patch.object(mcp_server.read_cache, "get_search", return_value=cached),
        patch.object(mcp_server, "_scroll_project_points", return_value=[point]),
        patch.object(
            mcp_server,
            "_point_to_memory_result",
            return_value={"id": mem_id, "memory": "regra de frete", "project": "sysmovs"},
        ),
    ):
        mcp_server.user_id_var.set("S0258")
        mcp_server.client_name_var.set("claude-code")
        out = await _registered("search_memory").fn("frete", project="sysmovs")
        assert json.loads(out)["results"]
        mcp_server.client_name_var.set("cursor")
        await _registered("list_memories").fn("sysmovs")
        await drain_pending_read_audits()

    body = _client(db_factory).get(f"/api/v1/memories/{mem_id}/access-log").json()
    assert body["raw_total"] == 2
    by_type = {log["access_type"]: log for log in body["logs"]}
    assert set(by_type) == {"search", "list"}
    assert by_type["search"]["channel"] == "mcp"
    assert by_type["search"]["client_name"] == "claude-code"
    assert by_type["search"]["query"] == "frete"
    assert by_type["list"]["client_name"] == "cursor"
    for log in body["logs"]:
        assert log["hostname"] == "S0258"
        assert log["display_name"] == "Bruno Lima"
        assert log["avatar_url"] == "https://example.com/bruno.png"

    # Batched per result: the other hit got its own row in the same write.
    db = db_factory()
    try:
        assert db.query(ReadAuditLog).filter(ReadAuditLog.memory_id == other_id).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mcp_audit_does_not_block_or_break_on_db_failure(db_factory):
    """A failing audit write must neither raise into the tool nor hang the reply."""
    from app import mcp_server
    from app.utils.mcp_read_wrappers import drain_pending_read_audits

    cached = [{"id": str(uuid.uuid4()), "memory": "m", "project": "p", "score": 0.9, "state": "active"}]
    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=MagicMock()),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server, "requester_group_for_mcp", return_value=None),
        patch.object(mcp_server.read_cache, "get_search", return_value=cached),
        patch("app.utils.read_audit.SessionLocal", side_effect=RuntimeError("db down")),
    ):
        mcp_server.user_id_var.set("S0258")
        out = await _registered("search_memory").fn("q", project="p")
        await drain_pending_read_audits()
    assert json.loads(out)["results"]


# --------------------------------------------------------------------------- #
# B2: forging a person through client-asserted channels (MCP path / compat header)
# --------------------------------------------------------------------------- #
def _forged_actors(people) -> list[str]:
    bruno = people["bruno"]
    return [
        f"ui:{bruno}",  # session-shaped actor
        bruno,  # bare User.id
        "google-bruno",  # User.user_id
        "bruno@sysmo.com.br",  # e-mail (Bruno has none; Ana's below)
        "ana@sysmo.com.br",
    ]


def _assert_no_person(body: dict) -> None:
    assert body["logs"], body
    for log in body["logs"]:
        assert log["display_name"] not in {"Bruno Lima", "Ana Souza"}, log
        assert "avatar_url" not in log, log
        assert "anonymous" not in log, log


@pytest.mark.asyncio
async def test_mcp_cannot_forge_person_via_path_user_id(db_factory, people):
    from app import mcp_server
    from app.utils.mcp_read_wrappers import drain_pending_read_audits

    for forged in _forged_actors(people):
        mem_id = str(uuid.uuid4())
        cached = [{"id": mem_id, "memory": "m", "project": "sysmovs", "score": 0.9, "state": "active"}]
        with (
            patch.object(mcp_server, "get_memory_client_safe", return_value=MagicMock()),
            patch.object(mcp_server, "bind_active_collection"),
            patch.object(mcp_server, "requester_group_for_mcp", return_value=None),
            patch.object(mcp_server.read_cache, "get_search", return_value=cached),
        ):
            mcp_server.user_id_var.set(forged)  # MCP route ``/mcp/<client>/sse/<user_id>``
            mcp_server.client_name_var.set("cursor")
            await _registered("search_memory").fn("q", project="sysmovs")
            await drain_pending_read_audits()

        rows = _reads(db_factory, mem_id)
        assert len(rows) == 1 and rows[0].source == "mcp"
        body = _client(db_factory).get(f"/api/v1/memories/{mem_id}/access-log").json()
        _assert_no_person(body)
        assert body["logs"][0]["channel"] == "mcp"


def test_compat_v3_cannot_forge_person_via_header_or_user_id(db_factory, people, monkeypatch):
    from app.routers import compat_v3

    hit = SimpleNamespace(id=None, score=0.9, payload={"data": "m", "project": "sysmovs"})
    fake = MagicMock()
    fake.embedding_model.embed.return_value = [0.1, 0.2]
    fake.vector_store.search.side_effect = lambda **kw: [hit]
    monkeypatch.setattr(compat_v3, "get_memory_client", lambda: fake)
    monkeypatch.setattr(compat_v3, "bind_active_collection", lambda *a, **k: None)
    monkeypatch.setattr(compat_v3, "requester_group_for_mcp", lambda *a, **k: None)
    cache = MagicMock()
    cache.get_search.return_value = None
    cache.get_embedding.return_value = None
    monkeypatch.setattr(compat_v3, "read_cache", cache)

    app = FastAPI()
    app.include_router(compat_v3.router)
    compat = TestClient(app)
    for forged in _forged_actors(people):
        for mode in ("header", "user_id"):
            mem_id = str(uuid.uuid4())
            hit.id = mem_id
            body = {"query": "frete"}
            headers = {"x-client-name": "claude-code"}
            if mode == "header":
                headers["x-openmemory-host"] = forged
            else:
                body["user_id"] = forged
            resp = compat.post("/v3/memories/search/", json=body, headers=headers)
            assert resp.status_code == 200, resp.text
            rows = _reads(db_factory, mem_id)
            assert len(rows) == 1 and rows[0].source == "compat_v3", (forged, mode)
            log_body = _client(db_factory).get(f"/api/v1/memories/{mem_id}/access-log").json()
            _assert_no_person(log_body)
            assert log_body["logs"][0]["channel"] == "api"


def test_compat_v3_real_linked_hostname_still_resolves(db_factory, people, monkeypatch):
    """Pre-card behaviour preserved: a linked machine hostname names its owner."""
    mem = str(uuid.uuid4())
    _add_rows(db_factory, mem, [(0, "compat_v3", "search", "S0258", "claude-code", "q")])
    log = _client(db_factory).get(f"/api/v1/memories/{mem}/access-log").json()["logs"][0]
    assert log["display_name"] == "Bruno Lima"
    assert log["avatar_url"] == "https://example.com/bruno.png"


def test_forged_session_actor_rows_on_non_web_sources(db_factory, people):
    """Rows stored directly (any shape) under mcp/compat_v3 never resolve a person."""
    mem = str(uuid.uuid4())
    rows = []
    for i, forged in enumerate(_forged_actors(people) + [f"ui:{people['bruno'].upper()}"]):
        rows.append((i * 1000, "mcp", "search", forged, "cursor", None))
        rows.append((i * 1000 + 500, "compat_v3", "search", forged, None, None))
    _add_rows(db_factory, mem, rows)
    body = _client(db_factory).get(f"/api/v1/memories/{mem}/access-log?grouped=false&page_size=100").json()
    assert body["raw_total"] == len(rows)
    _assert_no_person(body)
