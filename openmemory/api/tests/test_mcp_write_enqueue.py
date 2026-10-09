"""Tests for the non-blocking MCP write tool ``add_memories`` (task_07 / ADR-004).

``add_memories`` enqueues a :class:`WriteJob` and returns an immediate
fire-and-forget ack ``{"status": "accepted", "job_id", "queue_depth",
"estimated_wait_sec", ...}``. Agents must not poll; depth/ETA are informational.
No embed/search/LLM on the request path; extraction runs in the write worker.

Covered:
- a valid call enqueues exactly one job and returns the accepted ack;
- the enqueued job carries the resolved hostname (attribution, task_04) and the
  originating client_name;
- a missing/blank ``project`` returns a descriptive error and enqueues nothing;
- a blank ``text`` returns a descriptive error and enqueues nothing;
- ``client.add`` / embed / search are never invoked on the request path;
- integration: an ``add_memories`` call lands a row the worker can consume.
"""

import json
import os
import uuid
from unittest.mock import MagicMock, patch

# Dummy key before importing modules that may build a client lazily.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
import app.database as database_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import mcp_server
from app.mcp_server import add_memories
from app.database import Base
from app.models import Machine, MachineStatus, User, USER_TYPE_LEGACY_HOST, USER_TYPE_PERSON, WriteAuditLog
from app.models import WriteQueueJob as WriteQueueModel
from app.models import WriteQueueStatus
from app.mcp_server import DEFAULT_CLIENT_NAME
from app.utils.write_queue import WriteQueue


def _audit_factory():
    """In-memory sqlite sessionmaker with just the write_audit_logs table."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    WriteAuditLog.__table__.create(bind=engine, checkfirst=True)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _linked_session_factory(hostname: str = "maqA"):
    """SQLite with linked machine so write guard allows enqueue in unit tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    person = User(
        user_id="google-sub-test",
        google_sub="google-sub-test",
        display_name="Test User",
        user_type=USER_TYPE_PERSON,
    )
    legacy = User(user_id=hostname, user_type=USER_TYPE_LEGACY_HOST)
    session.add_all([person, legacy])
    session.flush()
    session.add(
        Machine(
            hostname=hostname,
            linked_user_id=person.id,
            legacy_user_id=legacy.id,
            status=MachineStatus.linked,
        )
    )
    session.commit()
    session.close()
    return Session


# A fixed but realistic UUID (contains hex letters so SQLite keeps TEXT affinity;
# an all-numeric value would be coerced to a float on read-back).
FAKE_JOB_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


class _FakeQueue:
    """Records enqueued jobs; returns a deterministic (valid UUID) job id."""

    def __init__(self, depth: int = 3):
        self.jobs = []
        self._depth = depth

    def enqueue(self, job):
        self.jobs.append(job)
        return FAKE_JOB_ID

    def depth(self):
        return self._depth


@pytest.fixture
def fake_queue(monkeypatch):
    q = _FakeQueue()
    linked_factory = _linked_session_factory("maqA")
    monkeypatch.setattr(database_module, "SessionLocal", linked_factory)
    # Also redirect the audit write to an isolated in-memory DB so unit tests do
    # not touch the real application database.
    with patch.object(mcp_server, "write_queue", q), \
            patch.object(mcp_server, "SessionLocal", _audit_factory()):
        yield q


def _set_ctx(uid="maqA", client="cursor"):
    mcp_server.user_id_var.set(uid)
    mcp_server.client_name_var.set(client)


# --------------------------------------------------------------------------- #
# Enqueue + ack
# --------------------------------------------------------------------------- #
class TestEnqueueAck:
    @pytest.mark.asyncio
    async def test_valid_call_enqueues_and_acks(self, fake_queue, monkeypatch):
        monkeypatch.setenv("WRITE_WORKER_EMA_JOB_SEC", "45")
        monkeypatch.setenv("WRITE_WORKER_ETA_CONCURRENCY", "1")
        monkeypatch.delenv("WRITE_WORKER_MAX_CONCURRENCY", raising=False)
        _set_ctx()
        out = await add_memories("remember X", project="alpha")
        data = json.loads(out)

        assert data["status"] == "accepted"
        assert data["job_id"] == FAKE_JOB_ID
        assert data["queue_depth"] == 3
        assert data["estimated_wait_sec"] == 135  # ceil(3 * 45 / 1)
        assert "not searchable" in data["message"].lower() or "not searchable yet" in data["message"]
        assert data["project"] == "alpha"
        assert len(fake_queue.jobs) == 1

    @pytest.mark.asyncio
    async def test_eta_ignores_worker_concurrency_by_default(
        self, fake_queue, monkeypatch
    ):
        """Worker MAX_CONCURRENCY must not optimistic-divide ETA while LLM is serial."""
        monkeypatch.setenv("WRITE_WORKER_EMA_JOB_SEC", "45")
        monkeypatch.setenv("WRITE_WORKER_MAX_CONCURRENCY", "2")
        monkeypatch.delenv("WRITE_WORKER_ETA_CONCURRENCY", raising=False)
        _set_ctx()
        out = await add_memories("remember X", project="alpha")
        data = json.loads(out)
        # depth=3 from fake queue; must NOT be ceil(3*45/2)=68
        assert data["estimated_wait_sec"] == 135

    @pytest.mark.asyncio
    async def test_eta_respects_explicit_eta_concurrency(
        self, fake_queue, monkeypatch
    ):
        monkeypatch.setenv("WRITE_WORKER_EMA_JOB_SEC", "50")
        monkeypatch.setenv("WRITE_WORKER_ETA_CONCURRENCY", "2")
        _set_ctx()
        out = await add_memories("remember X", project="alpha")
        data = json.loads(out)
        assert data["estimated_wait_sec"] == 75  # ceil(3 * 50 / 2)

    @pytest.mark.asyncio
    async def test_job_carries_hostname_and_client(self, fake_queue):
        _set_ctx(uid="maqA", client="cursor")
        await add_memories("remember X", project="alpha")
        job = fake_queue.jobs[0]

        assert job.text == "remember X"
        assert job.project == "alpha"
        assert job.hostname == "maqA"       # attribution (task_04)
        assert job.client_name == "cursor"  # originating client

    @pytest.mark.asyncio
    async def test_project_is_trimmed(self, fake_queue):
        _set_ctx()
        await add_memories("x", project="  alpha  ")
        assert fake_queue.jobs[0].project == "alpha"

    @pytest.mark.asyncio
    async def test_missing_hostname_rejected(self, fake_queue):
        # No user_id in context -> attribution falls back to the sentinel; guard blocks.
        mcp_server.user_id_var.set("")
        mcp_server.client_name_var.set("cursor")
        out = await add_memories("x", project="alpha")
        assert "memory write blocked" in out
        assert fake_queue.jobs == []

    @pytest.mark.asyncio
    async def test_missing_client_name_uses_default(self, fake_queue):
        # No client_name in context -> falls back to the sentinel.
        mcp_server.user_id_var.set("maqA")
        mcp_server.client_name_var.set("")
        await add_memories("x", project="alpha")
        assert fake_queue.jobs[0].client_name == DEFAULT_CLIENT_NAME


# --------------------------------------------------------------------------- #
# Validation (no enqueue on bad input)
# --------------------------------------------------------------------------- #
class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_project_errors_without_enqueue(self, fake_queue):
        _set_ctx()
        out = await add_memories("remember X", project="")
        assert "project not provided" in out
        assert fake_queue.jobs == []

    @pytest.mark.asyncio
    async def test_blank_project_errors_without_enqueue(self, fake_queue):
        _set_ctx()
        out = await add_memories("remember X", project="   ")
        assert "project not provided" in out
        assert fake_queue.jobs == []

    @pytest.mark.asyncio
    async def test_blank_text_errors_without_enqueue(self, fake_queue):
        _set_ctx()
        out = await add_memories("   ", project="alpha")
        assert "text not provided" in out
        assert fake_queue.jobs == []


# --------------------------------------------------------------------------- #
# No embed / search / LLM on the request path
# --------------------------------------------------------------------------- #
class TestNoLlmOnRequestPath:
    @pytest.mark.asyncio
    async def test_memory_client_not_touched_on_write(self, fake_queue):
        _set_ctx()
        client = MagicMock()
        with patch.object(
            mcp_server, "get_memory_client_safe", return_value=client
        ) as get_client, patch.object(
            mcp_server, "bind_active_collection"
        ) as bind:
            out = await add_memories("remember X", project="alpha")
        data = json.loads(out)
        assert data["status"] == "accepted"
        assert "similar" not in data
        get_client.assert_not_called()
        bind.assert_not_called()
        client.add.assert_not_called()
        client.embedding_model.embed.assert_not_called()
        client.vector_store.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_supersedes_stored_on_job(self, fake_queue):
        _set_ctx()
        out = await add_memories(
            "corrected fact",
            project="alpha",
            supersedes=["725104c0-4cf8-4af3-b21a-2d979b0caca5"],
        )
        data = json.loads(out)
        assert data["supersedes"] == ["725104c0-4cf8-4af3-b21a-2d979b0caca5"]
        assert fake_queue.jobs[0].extras == {
            "supersedes": ["725104c0-4cf8-4af3-b21a-2d979b0caca5"]
        }

    @pytest.mark.asyncio
    async def test_enqueue_failure_is_reported(self, fake_queue):
        _set_ctx()
        fake_queue.enqueue = MagicMock(side_effect=RuntimeError("db gone"))
        out = await add_memories("remember X", project="alpha")
        assert "Error enqueuing memory write" in out


# --------------------------------------------------------------------------- #
# Integration: enqueue lands a row the worker can consume
# --------------------------------------------------------------------------- #
class TestIntegrationWithQueue:
    @pytest.mark.asyncio
    async def test_add_memories_lands_consumable_row(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "enqueue_it.db")
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = factory()
        person = User(
            user_id="google-sub-it",
            google_sub="google-sub-it",
            display_name="IT User",
            user_type=USER_TYPE_PERSON,
        )
        legacy = User(user_id="maqA", user_type=USER_TYPE_LEGACY_HOST)
        session.add_all([person, legacy])
        session.flush()
        session.add(
            Machine(
                hostname="maqA",
                linked_user_id=person.id,
                legacy_user_id=legacy.id,
                status=MachineStatus.linked,
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr(database_module, "SessionLocal", factory)
        real_queue = WriteQueue(session_factory=factory)

        with patch.object(mcp_server, "write_queue", real_queue), \
                patch.object(mcp_server, "SessionLocal", factory):
            _set_ctx(uid="maqA", client="cursor")
            out = await add_memories("remember X", project="alpha")

        data = json.loads(out)
        assert data["status"] == "accepted"
        assert data["project"] == "alpha"
        assert data["job_id"]
        assert data["queue_depth"] >= 1
        assert "estimated_wait_sec" in data

        # The job is persisted as queued before the worker dequeues it.
        db = factory()
        try:
            rows = db.query(WriteQueueModel).all()
            assert len(rows) == 1
            row = rows[0]
            job_id = str(row.id)
            assert row.status == WriteQueueStatus.queued
            assert row.project == "alpha"
            assert row.hostname == "maqA"
            assert row.client_name == "cursor"
            assert row.text == "remember X"
        finally:
            db.close()

        dequeued = real_queue.dequeue(limit=1)
        assert len(dequeued) == 1
        assert dequeued[0].id == job_id
        assert dequeued[0].text == "remember X"

        # dequeue marks the row processing so a crash does not re-deliver it.
        db = factory()
        try:
            row = db.query(WriteQueueModel).filter(
                WriteQueueModel.id == uuid.UUID(job_id)
            ).first()
            assert row.status == WriteQueueStatus.processing
        finally:
            db.close()

        # A durable audit row was recorded with the hostname attribution.
        db = factory()
        try:
            audit = db.query(WriteAuditLog).filter(
                WriteAuditLog.job_id == uuid.UUID(job_id)
            ).first()
            assert audit is not None
            assert audit.hostname == "maqA"
            assert audit.project == "alpha"
            assert audit.client_name == "cursor"
            assert audit.action == "enqueue"
        finally:
            db.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# Durable write audit (task_04 / ADR-003)
# --------------------------------------------------------------------------- #
class TestWriteAudit:
    @pytest.mark.asyncio
    async def test_enqueue_records_audit_with_hostname(self, fake_queue):
        _set_ctx(uid="maqA", client="claude")
        out = await add_memories("remember X", project="alpha")
        assert json.loads(out)["status"] == "accepted"
        job_id = FAKE_JOB_ID

        db = mcp_server.SessionLocal()
        try:
            audit = db.query(WriteAuditLog).filter(
                WriteAuditLog.job_id == uuid.UUID(job_id)
            ).first()
            assert audit is not None
            assert audit.hostname == "maqA"        # attribution (task_04)
            assert audit.project == "alpha"
            assert audit.client_name == "claude"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_invalid_input_records_no_audit(self, fake_queue):
        _set_ctx()
        await add_memories("remember X", project="")  # invalid -> no enqueue

        db = mcp_server.SessionLocal()
        try:
            assert db.query(WriteAuditLog).count() == 0
        finally:
            db.close()
