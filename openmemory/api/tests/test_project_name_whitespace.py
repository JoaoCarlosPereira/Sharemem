"""Regressão: projeto com espaço interno (card "Erro ao subir memoria com projeto contendo espacos").

Antes: ``add_memories(project="PONTEIRO DE SPEC")`` respondia ``accepted``, o
worker chamava ``client.add(project=...)`` e o SDK mem0
(``_validate_and_trim_entity_id``) levantava ``ValueError`` — o job falhava
depois das tentativas e a memória se perdia em silêncio.

Agora: strip + whitespace interno -> '-' (caixa preservada) na entrada (MCP,
REST, compat_v3), na leitura (search/list/filtro estrito) e, defensivamente,
no worker para jobs antigos já enfileirados.
"""

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_module
from app import mcp_server
from app.database import Base, get_db
from app.mcp_server import add_memories, list_memories, search_memory
from app.models import WriteQueueJob as WriteQueueModel
from app.models import WriteQueueStatus
from app.models import User
from app.utils.project_name import normalize_project, normalize_project_with_notice
from app.utils.write_queue import WriteJob, WriteQueue
from app.workers.write_worker import WriteWorker
from mem0.memory.main import _validate_and_trim_entity_id

from tests.test_compat_v3 import _FakeClient, _Hit, _MemReadCache, compat_v3
from tests.test_mcp_read_project import _hit, _make_client, _point
from tests.test_mcp_write_enqueue import (
    _audit_factory,
    _FakeQueue,
    _linked_session_factory,
    _set_ctx,
)

RAW = "PONTEIRO DE SPEC"
EFFECTIVE = "PONTEIRO-DE-SPEC"


# --------------------------------------------------------------------------- #
# Função canônica
# --------------------------------------------------------------------------- #
class TestNormalizeProject:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("PONTEIRO DE SPEC", "PONTEIRO-DE-SPEC"),
            ("  Ponteiro   de\tSpec \n", "Ponteiro-de-Spec"),  # caixa preservada
            ("a\u00a0b", "a-b"),  # NBSP também é whitespace
            ("mem0-shared", "mem0-shared"),  # inalterado
            ("  alpha  ", "alpha"),
        ],
    )
    def test_whitespace_runs_become_dash(self, raw, expected):
        assert normalize_project(raw) == expected

    def test_idempotent(self):
        assert normalize_project(normalize_project(RAW)) == EFFECTIVE

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_keeps_current_behavior(self, raw):
        assert normalize_project(raw) == raw

    def test_result_is_accepted_by_mem0_sdk(self):
        # O contrato que importa: a chave efetiva passa pela validação do SDK.
        assert _validate_and_trim_entity_id(normalize_project(RAW), "project") == EFFECTIVE
        with pytest.raises(ValueError, match="whitespace"):
            _validate_and_trim_entity_id(RAW, "project")

    def test_notice_only_when_internal_whitespace_changed(self):
        assert normalize_project_with_notice("  alpha ") == ("alpha", None)
        eff, notice = normalize_project_with_notice(RAW)
        assert eff == EFFECTIVE
        assert notice and EFFECTIVE in notice


# --------------------------------------------------------------------------- #
# MCP add_memories
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_queue(monkeypatch):
    q = _FakeQueue()
    monkeypatch.setattr(database_module, "SessionLocal", _linked_session_factory("maqA"))
    with patch.object(mcp_server, "write_queue", q), \
            patch.object(mcp_server, "SessionLocal", _audit_factory()):
        yield q


class TestMcpAddNormalizes:
    @pytest.mark.asyncio
    async def test_enqueues_effective_project_and_reports_it(self, fake_queue):
        _set_ctx()
        data = json.loads(await add_memories("remember X", project=RAW))

        assert data["status"] == "accepted"
        assert data["project"] == EFFECTIVE
        assert data["project_requested"] == RAW
        assert EFFECTIVE in data["warning"]
        assert fake_queue.jobs[0].project == EFFECTIVE

    @pytest.mark.asyncio
    async def test_audit_row_uses_effective_project(self, fake_queue):
        _set_ctx()
        await add_memories("remember X", project=RAW)
        from app.models import WriteAuditLog

        db = mcp_server.SessionLocal()
        try:
            assert db.query(WriteAuditLog).one().project == EFFECTIVE
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_no_warning_for_plain_project(self, fake_queue):
        _set_ctx()
        data = json.loads(await add_memories("x", project="  alpha  "))
        assert data["project"] == "alpha"
        assert "warning" not in data and "project_requested" not in data

    @pytest.mark.asyncio
    async def test_blank_project_still_rejected(self, fake_queue):
        _set_ctx()
        assert await add_memories("x", project="   ") == "Error: project not provided"
        assert fake_queue.jobs == []


# --------------------------------------------------------------------------- #
# MCP search_memory / list_memories
# --------------------------------------------------------------------------- #
@pytest.fixture
def patched_client():
    client = _make_client()
    client.embedding_model.model = "test-embed-model"
    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=client),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server.read_cache, "get_search", return_value=None) as get_s,
        patch.object(mcp_server.read_cache, "set_search"),
        patch.object(mcp_server.read_cache, "get_embedding", return_value=None),
        patch.object(mcp_server.read_cache, "set_embedding"),
    ):
        yield client, get_s


class TestMcpReadNormalizes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", [RAW, EFFECTIVE])
    async def test_strict_search_finds_normalized_with_any_spelling(self, patched_client, spelling):
        client, get_search = patched_client
        client.vector_store.search.return_value = [_hit("1", "fato", EFFECTIVE)]
        out = json.loads(await search_memory("fato", project=spelling, strict_project=True))

        assert client.vector_store.search.call_args.kwargs["filters"] == {"project": EFFECTIVE}
        assert out["results"][0]["project"] == EFFECTIVE
        # Cache keyed by the effective project (both spellings share the entry).
        assert get_search.call_args.args[0] == EFFECTIVE

    @pytest.mark.asyncio
    async def test_soft_hint_boosts_normalized_project(self, patched_client):
        client, _ = patched_client
        client.vector_store.search.return_value = [_hit("1", "fato", EFFECTIVE)]
        out = json.loads(await search_memory("fato", project=RAW))
        assert out["results"][0]["project"] == EFFECTIVE

    @pytest.mark.asyncio
    async def test_strict_search_family_members_normalized(self, patched_client):
        client, _ = patched_client
        with patch.object(mcp_server, "projects_in_group", return_value=["Grupo A", "b"]):
            await search_memory("fato", project="Grupo A", strict_project=True)
        assert client.vector_store.search.call_args.kwargs["filters"] == {
            "project": {"in": ["Grupo-A", "b"]}
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", [RAW, EFFECTIVE])
    async def test_list_filters_by_effective_project(self, patched_client, spelling):
        client, _ = patched_client
        client.vector_store.client.scroll.return_value = ([_point("1", "m1", EFFECTIVE)], None)
        out = json.loads(await list_memories(project=spelling))

        scroll_filter = client.vector_store.client.scroll.call_args.kwargs["scroll_filter"]
        assert EFFECTIVE in json.dumps(scroll_filter)
        assert RAW not in json.dumps(scroll_filter)
        assert out["project"] == EFFECTIVE
        assert out["total"] == 1


# --------------------------------------------------------------------------- #
# Write worker — payload antigo (enfileirado antes da correção)
# --------------------------------------------------------------------------- #
@pytest.fixture
def queue_and_path(tmp_path):
    db_path = str(tmp_path / "ws_worker.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    WriteQueueModel.__table__.create(bind=engine, checkfirst=True)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield WriteQueue(session_factory=factory), factory
    engine.dispose()


def _sdk_validating_client():
    """Fake client whose ``add`` applies the REAL mem0 project validation."""
    client = MagicMock()

    def _add(text, **kwargs):
        _validate_and_trim_entity_id(kwargs.get("project"), "project")
        return {"results": [{"id": str(uuid.uuid4()), "memory": text, "event": "ADD"}]}

    client.add = MagicMock(side_effect=_add)
    return client


class TestWorkerLegacyPayload:
    @pytest.mark.asyncio
    async def test_old_job_with_spaces_is_done_not_failed(self, queue_and_path):
        queue, factory = queue_and_path
        client = _sdk_validating_client()
        upserts, invalidated = [], []
        worker = WriteWorker(
            queue=queue,
            client_provider=lambda: client,
            upsert_project=lambda name, hostname: upserts.append(name),
            max_attempts=1,  # any failure would be terminal on the first pass
        )
        job_id = queue.enqueue(
            WriteJob(id="", project=RAW, hostname="maqA", client_name="cursor",
                     text="fato antigo", created_at="")
        )
        with patch("app.workers.write_worker.read_cache") as rc, \
                patch("app.workers.write_worker.bind_active_collection"):
            rc.invalidate_search.side_effect = invalidated.append
            assert await worker.process_once() == 1

        kwargs = client.add.call_args.kwargs
        assert kwargs["project"] == EFFECTIVE
        assert kwargs["metadata"]["project"] == EFFECTIVE
        assert upserts == [EFFECTIVE]
        assert invalidated == [EFFECTIVE]

        db = factory()
        try:
            row = db.query(WriteQueueModel).filter(
                WriteQueueModel.id == uuid.UUID(job_id)
            ).one()
            assert row.status == WriteQueueStatus.done
            assert row.error is None
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# compat_v3 (/v3/memories)
# --------------------------------------------------------------------------- #
@pytest.fixture
def compat_fake(monkeypatch):
    monkeypatch.setattr(compat_v3, "check_write_allowed", lambda *a, **k: None)
    monkeypatch.setattr(compat_v3, "ensure_user_registered", lambda *a, **k: None)
    monkeypatch.setattr(compat_v3, "read_cache", _MemReadCache())
    monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **k: None)
    fake = _FakeClient([
        _Hit("p1", 0.5, {"data": "ponteiro", "project": EFFECTIVE}),
        _Hit("o1", 0.5, {"data": "outro", "project": "outro"}),
    ])
    monkeypatch.setattr(compat_v3, "get_memory_client", lambda: fake)
    return fake


async def _post(path, body):
    app = FastAPI()
    app.include_router(compat_v3.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return (await ac.post(path, json=body)).json()


class TestCompatV3Normalizes:
    @pytest.mark.asyncio
    async def test_add_normalizes_project_and_metadata(self, compat_fake):
        data = await _post("/v3/memories/add/", {
            "text": "fato", "user_id": "host", "app_id": RAW,
            "metadata": {"project": RAW, "type": "decision"},
        })
        assert data["status"] == "ok"
        assert data["project"] == EFFECTIVE
        assert EFFECTIVE in data["warning"]
        _, kwargs = compat_fake.add_calls[0]
        assert kwargs["project"] == EFFECTIVE
        assert kwargs["metadata"]["project"] == EFFECTIVE
        assert kwargs["metadata"]["type"] == "decision"

    @pytest.mark.asyncio
    async def test_list_filters_by_effective_project(self, compat_fake):
        data = await _post("/v3/memories/?page=1&page_size=10",
                           {"filters": {"AND": [{"app_id": RAW}]}})
        assert {r["id"] for r in data["results"]} == {"p1"}

    @pytest.mark.asyncio
    async def test_search_hint_uses_effective_project(self, compat_fake, monkeypatch):
        seen = {}

        def _rank(results, preferred_project=None, **k):
            seen["preferred"] = preferred_project

        monkeypatch.setattr(compat_v3, "rank_search_results", _rank)
        await _post("/v3/memories/search/", {"query": "q", "project": RAW})
        assert seen["preferred"] == EFFECTIVE
        await _post("/v3/memories/search/", {"query": "q", "filters": {"app_id": RAW}})
        assert seen["preferred"] == EFFECTIVE


# --------------------------------------------------------------------------- #
# REST /api/v1/memories
# --------------------------------------------------------------------------- #
@pytest.fixture
def rest_client():
    from app.routers.memories import router as memories_router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    db.add(User(user_id="root", name="Root"))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(memories_router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    engine.dispose()


class TestRestNormalizes:
    def test_shared_filter_uses_effective_project(self, rest_client):
        empty = {"items": [], "total": 0, "page": 1, "size": 10, "pages": 0}
        with patch("app.utils.vector_stats.list_shared_memories", return_value=empty) as m, \
                patch("app.utils.read_audit.record_memory_reads"):
            resp = rest_client.post("/api/v1/memories/shared-filter",
                                    json={"user_id": "root", "project": RAW})
        assert resp.status_code == 200
        assert m.call_args.kwargs["project"] == EFFECTIVE

    def test_create_memory_normalizes_metadata_project(self, rest_client):
        mem_client = MagicMock()
        mem_id = str(uuid.uuid4())
        mem_client.add.return_value = {
            "results": [{"id": mem_id, "memory": "fato", "event": "ADD"}]
        }
        with patch("app.routers.memories.get_memory_client", return_value=mem_client):
            resp = rest_client.post("/api/v1/memories/", json={
                "user_id": "root", "text": "fato", "metadata": {"project": RAW},
            })
        assert resp.status_code == 200
        assert resp.json()["metadata_"]["project"] == EFFECTIVE


# --------------------------------------------------------------------------- #
# Admin: listagem por projeto
# --------------------------------------------------------------------------- #
class TestAdminProjectMemories:
    def test_admin_project_memories_uses_effective_project(self):
        from app.routers import admin as admin_mod

        client = MagicMock()
        client.vector_store.list.return_value = (
            [SimpleNamespace(id="1", payload={"data": "x", "project": EFFECTIVE})], None
        )
        with patch("app.utils.memory.get_memory_client_safe", return_value=client), \
                patch("app.utils.partitioning.bind_active_collection"):
            out = admin_mod.project_memories(project=RAW, search=None, limit=10)
        assert client.vector_store.list.call_args.kwargs["filters"] == {"project": EFFECTIVE}
        assert out["project"] == EFFECTIVE
