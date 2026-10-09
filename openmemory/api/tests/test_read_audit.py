"""Tests for Qdrant/MCP read access audit."""

import os
import uuid
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.read_audit_log_model import ReadAuditLog
from app.routers.apps import router as apps_router
from app.utils.read_audit import (
    count_distinct_memories_accessed,
    record_memory_reads,
)


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield factory
    engine.dispose()


def test_record_and_count_distinct_accesses(db_factory):
    project = "sysmovs"
    mem_a = str(uuid.uuid4())
    mem_b = str(uuid.uuid4())

    with patch("app.utils.read_audit.SessionLocal", db_factory):
        record_memory_reads(
            project=project,
            memory_ids=[mem_a, mem_b],
            access_type="search",
            source="mcp",
            hostname="host-a",
            client_name="claude-code",
            query="Fhelipe gosta de que",
        )
        record_memory_reads(
            project=project,
            memory_ids=[mem_a],
            access_type="search",
            source="mcp",
            hostname="host-a",
            client_name="claude-code",
            query="Fhelipe CS",
        )

    db = db_factory()
    try:
        assert count_distinct_memories_accessed(db, project) == 2
        rows = db.query(ReadAuditLog).filter(ReadAuditLog.project == project).all()
        assert len(rows) == 3
    finally:
        db.close()


def test_list_memory_read_audit_for_detail_page(db_factory):
    mem_id = str(uuid.uuid4())
    with patch("app.utils.read_audit.SessionLocal", db_factory):
        record_memory_reads(
            project="default",
            memory_ids=[mem_id],
            access_type="search",
            source="mcp",
            hostname="S0293",
            client_name="claude",
            query="Fhelipe",
        )

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
    client = TestClient(app)

    resp = client.get(f"/api/v1/memories/{mem_id}/access-log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["logs"][0]["app_name"] == "claude"
    assert body["logs"][0]["client_name"] == "claude"
    assert body["logs"][0]["hostname"] == "S0293"
    assert body["logs"][0]["access_type"] == "search"
    # The column is UTC without tzinfo; a naive string would be read as local time.
    assert body["logs"][0]["accessed_at"].endswith("+00:00")


def test_project_access_stats_returns_utc_aware_timestamps(db_factory):
    from app.utils.read_audit import project_access_stats

    with patch("app.utils.read_audit.SessionLocal", db_factory):
        record_memory_reads(
            project="mem0-shared",
            memory_ids=[str(uuid.uuid4())],
            access_type="search",
            source="mcp",
            hostname="S0258",
        )

    db = db_factory()
    try:
        _, first_accessed, last_accessed = project_access_stats(db, "mem0-shared")
        assert first_accessed is not None and first_accessed.tzinfo is not None
        assert last_accessed is not None and last_accessed.tzinfo is not None
    finally:
        db.close()


def test_apps_list_shows_access_count_for_projects(db_factory):
    from app.models import Project

    db = db_factory()
    db.add(Project(name="sysmovs"))
    db.commit()
    db.close()

    mem_id = str(uuid.uuid4())
    with patch("app.utils.read_audit.SessionLocal", db_factory):
        record_memory_reads(
            project="sysmovs",
            memory_ids=[mem_id],
            access_type="list",
            source="mcp",
            hostname="host-a",
        )

    app = FastAPI()
    app.include_router(apps_router)

    def _override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)

    with patch("app.routers.apps.count_project_memories", return_value=10):
        resp = client.get("/api/v1/apps/")

    assert resp.status_code == 200
    apps = resp.json()["apps"]
    sysmovs = next(a for a in apps if a["name"] == "sysmovs")
    assert sysmovs["total_memories_accessed"] == 1


# --------------------------------------------------------------------------- #
# MCP dispatch path
#
# FastMCP's ``@mcp.tool`` captures the undecorated function in ``Tool.fn`` at
# import time. The audit wrappers used to rebind only the ``app.mcp_server``
# module attribute, which dispatch never reads — so every MCP read went
# unrecorded while the installer logged success.
# --------------------------------------------------------------------------- #
def _registered(name):
    from app import mcp_server

    import app.routers.memories  # noqa: F401 — importing installs the wrappers

    return mcp_server.mcp._tool_manager.get_tool(name)


@pytest.mark.parametrize("tool_name", ["search_memory", "list_memories"])
def test_read_audit_wraps_the_function_the_registry_dispatches_to(tool_name):
    tool = _registered(tool_name)
    assert getattr(tool.fn, "__wrapped__", None) is not None, (
        f"{tool_name} is dispatched through Tool.fn; patching only the module "
        "attribute leaves MCP reads unaudited"
    )


@pytest.mark.asyncio
async def test_mcp_search_records_a_read_audit_row(db_factory):
    from unittest.mock import MagicMock

    from app import mcp_server

    tool = _registered("search_memory")
    mem_id = str(uuid.uuid4())
    # Priming the read cache keeps the vector store and embedder out of the test:
    # search_memory serves the candidate pool straight from it.
    cached = [
        {
            "id": mem_id,
            "memory": "Fhelipe gosta de Counter Strike",
            "hash": "h1",
            "created_at": None,
            "updated_at": None,
            "project": "mem0-shared",
            "owner": "S0258",
            "score": 0.9,
            "state": "active",
        }
    ]

    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=MagicMock()),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server, "requester_group_for_mcp", return_value=None),
        patch.object(mcp_server.read_cache, "get_search", return_value=cached),
        patch("app.utils.read_audit.SessionLocal", db_factory),
    ):
        mcp_server.user_id_var.set("S0258")
        mcp_server.client_name_var.set("claude-code")
        await tool.fn("counter strike", project="mem0-shared")
        # The audit write runs off the event loop (never delays the MCP reply).
        from app.utils.mcp_read_wrappers import drain_pending_read_audits

        await drain_pending_read_audits()

    db = db_factory()
    try:
        rows = db.query(ReadAuditLog).all()
        assert len(rows) == 1
        assert rows[0].memory_id == mem_id
        assert rows[0].source == "mcp"
        assert rows[0].access_type == "search"
        assert rows[0].hostname == "S0258"
    finally:
        db.close()
