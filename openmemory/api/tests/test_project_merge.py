"""Tests for LLM-assisted duplicate project merge governance."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.governance import merge_proposals
from app.governance.project_merge import (
    MergeGroup,
    MergeRuleViolation,
    ProjectMergeError,
    ProjectProfile,
    _parse_merge_groups,
    apply_project_merge,
    detect_duplicate_groups_with_llm,
    detect_merge_inconsistencies,
    filter_group_by_rules,
    merge_block_reason,
    relocate_project_memories,
    run_merge_projects_job,
)
from app.models import (
    Base,
    GovernanceJobType,
    GovernancePolicy,
    GovernanceSchedule,
    Project,
    SpecWorkspace,
    TokenUsageLog,
    WriteAuditLog,
    WriteQueueJob,
    WriteQueueStatus,
)
from app.read_audit_log_model import ReadAuditLog
from app.utils.vector_stats import VectorStoreUnavailable

_MERGE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "routers" / "governance_project_merge.py"
)
_spec = importlib.util.spec_from_file_location("governance_project_merge_under_test", _MERGE_PATH)
_governance_merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_governance_merge)


@pytest.fixture(autouse=True)
def _no_real_qdrant(monkeypatch):
    """Never reach a real Qdrant: the global vector store is unavailable here."""
    monkeypatch.setattr("app.utils.vector_stats._vector_store", lambda: (None, None))


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


class FakeLLM:
    def __init__(self, groups):
        self._groups = groups

    def generate_response(self, *, messages, response_format=None):
        return json.dumps({"groups": self._groups})


def test_parse_merge_groups_filters_unknown_projects():
    profiles = [
        ProjectProfile("sysmovs", 10, "host", []),
        ProjectProfile("sysmovs-delphi", 5, "host", []),
    ]
    raw = [
        {
            "canonical": "sysmovs",
            "aliases": ["sysmovs-delphi", "missing"],
            "confidence": 0.9,
            "reason": "same product",
        }
    ]
    groups = _parse_merge_groups(raw, profiles=profiles)
    assert len(groups) == 1
    assert groups[0].aliases == ["sysmovs-delphi"]


def test_detect_duplicate_groups_with_llm_threshold():
    profiles = [
        ProjectProfile("sysmovs", 288, "h1", ["Ark game"]),
        ProjectProfile("dsv-delphi-sysmovs", 43, "h2", ["Delphi module"]),
        ProjectProfile("default", 10, "h3", ["other"]),
    ]
    llm = FakeLLM(
        [
            {
                "canonical": "sysmovs",
                "aliases": ["dsv-delphi-sysmovs"],
                "confidence": 0.93,
                "reason": "same workspace",
            }
        ]
    )
    groups = detect_duplicate_groups_with_llm(profiles, llm, confidence_threshold=0.85)
    assert len(groups) == 1
    assert groups[0].canonical == "sysmovs"


def test_apply_project_merge_updates_sql_and_qdrant(factory):
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="dsv-delphi-sysmovs", first_seen_hostname="dev"))
    db.add(
        WriteQueueJob(
            project="dsv-delphi-sysmovs",
            hostname="dev",
            client_name="cli",
            text="x",
            status=WriteQueueStatus.done,
        )
    )
    db.add(
        WriteAuditLog(
            project="dsv-delphi-sysmovs",
            hostname="dev",
            client_name="cli",
            action="enqueue",
        )
    )
    db.commit()

    vs = MagicMock()
    point = MagicMock()
    point.id = "mem-1"
    point.payload = {"project": "dsv-delphi-sysmovs", "data": "hello"}
    vs._create_filter.return_value = None
    vs.client.scroll.side_effect = [([point], None)]

    moved = apply_project_merge(
        db,
        vs,
        canonical="sysmovs",
        aliases=["dsv-delphi-sysmovs"],
        job_id="job-1",
    )
    assert moved == 1
    vs.update.assert_called_once()
    assert vs.update.call_args[0][0] == "mem-1"
    assert vs.update.call_args[1]["payload"] == {"project": "sysmovs"}

    db = factory()
    assert db.query(Project).filter(Project.name == "dsv-delphi-sysmovs").first() is None
    assert (
        db.query(WriteQueueJob).filter(WriteQueueJob.project == "sysmovs").count() == 1
    )
    assert (
        db.query(WriteAuditLog).filter(WriteAuditLog.project == "sysmovs").count() == 1
    )
    db.close()


def test_apply_project_merge_merges_conflicting_governance_schedules(factory):
    """When both canonical and alias have schedules for the same job_type, merge safely."""
    import datetime

    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="dsv-delphi-sysmovs"))
    canonical_ts = datetime.datetime(2026, 6, 28, 12, 0, 0)
    alias_ts = datetime.datetime(2026, 6, 29, 0, 0, 0)
    db.add(
        GovernanceSchedule(
            job_type=GovernanceJobType.dedup,
            scope="sysmovs",
            last_run_at=canonical_ts,
        )
    )
    db.add(
        GovernanceSchedule(
            job_type=GovernanceJobType.dedup,
            scope="dsv-delphi-sysmovs",
            last_run_at=alias_ts,
        )
    )
    db.commit()

    vs = MagicMock()
    vs._create_filter.return_value = None
    vs.client.scroll.return_value = ([], None)

    apply_project_merge(
        db,
        vs,
        canonical="sysmovs",
        aliases=["dsv-delphi-sysmovs"],
        job_id="job-sched",
    )

    rows = db.query(GovernanceSchedule).filter(
        GovernanceSchedule.job_type == GovernanceJobType.dedup,
        GovernanceSchedule.scope.in_(["sysmovs", "dsv-delphi-sysmovs"]),
    ).all()
    assert len(rows) == 1
    assert rows[0].scope == "sysmovs"
    assert rows[0].last_run_at == alias_ts
    db.close()


def test_run_merge_projects_job_dry_run(factory, monkeypatch):
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()

    monkeypatch.setattr(
        "app.governance.project_merge.collect_project_profiles",
        lambda _db: [
            ProjectProfile("sysmovs", 10, None, []),
            ProjectProfile("sysmovs-delphi", 5, None, []),
        ],
    )
    monkeypatch.setattr(
        "app.governance.project_merge.detect_duplicate_groups_with_llm",
        lambda profiles, llm, **kwargs: [
            MergeGroup("sysmovs", ["sysmovs-delphi"], 0.95, "same")
        ],
    )
    monkeypatch.setattr(
        "app.governance.project_merge.get_memory_client_safe",
        lambda: MagicMock(llm=FakeLLM([]), vector_store=MagicMock()),
        raising=False,
    )

    client = MagicMock()
    client.llm = FakeLLM([])
    client.vector_store = MagicMock()

    count = run_merge_projects_job(
        project=None,
        job_id="dry",
        session_factory=factory,
        memory_client_provider=lambda: client,
        payload={"dry_run": True},
    )
    assert count == 1
    client.vector_store.update.assert_not_called()


def test_merge_preview_endpoint(factory, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.utils.governance_policy import (
        DEFAULT_POLICY,
        default_processes_enabled,
        save_global_policy,
    )

    db = factory()
    doc = {**DEFAULT_POLICY}
    doc["processes_enabled"] = default_processes_enabled()
    save_global_policy(db, doc)
    db.close()

    app = FastAPI()
    app.include_router(_governance_merge.router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override

    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()

    monkeypatch.setattr(
        _governance_merge,
        "preview_project_merges",
        lambda **kwargs: [
            {
                "canonical": "sysmovs",
                "aliases": ["sysmovs-delphi"],
                "confidence": 0.91,
                "reason": "same",
                "memory_counts": {"sysmovs": 10, "sysmovs-delphi": 5},
            }
        ],
    )
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    client = TestClient(app)
    resp = client.get(
        "/admin/governance/projects/merge-preview",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["groups"][0]["canonical"] == "sysmovs"



def test_merge_enqueue_blocked_when_process_disabled(factory):
    from fastapi import HTTPException
    from app.utils.governance_policy import (
        DEFAULT_POLICY,
        default_processes_enabled,
        save_global_policy,
    )

    db = factory()
    doc = {**DEFAULT_POLICY}
    disabled = default_processes_enabled()
    disabled["merge_projects"] = False
    doc["processes_enabled"] = disabled
    save_global_policy(db, doc)

    with pytest.raises(HTTPException) as exc_info:
        _governance_merge._assert_merge_enabled(db)
    db.close()
    assert exc_info.value.status_code == 409
    assert "desabilitado" in exc_info.value.detail


class FakeVectorStore:
    """In-memory stand-in for the Qdrant wrapper used by project_merge."""

    collection_name = "openmemory"

    def __init__(self, points):
        self.points = {pid: dict(payload) for pid, payload in points.items()}
        self.fail_update_ids = set()
        self.client = MagicMock()
        self.client.scroll.side_effect = self._scroll
        self.client.count.side_effect = self._count

    def _create_filter(self, filters):
        return dict(filters)

    def _scroll(self, *, collection_name, scroll_filter, offset, limit, with_payload, with_vectors):
        project = scroll_filter["project"]
        recs = []
        for pid, payload in self.points.items():
            if payload.get("project") == project:
                rec = MagicMock()
                rec.id = pid
                rec.payload = dict(payload)
                recs.append(rec)
        return recs, None

    def _count(self, *, collection_name, count_filter, exact):
        result = MagicMock()
        result.count = self.count(count_filter["project"])
        return result

    def count(self, project):
        return sum(1 for p in self.points.values() if p.get("project") == project)

    def update(self, vector_id, vector=None, payload=None):
        if vector_id in self.fail_update_ids:
            raise RuntimeError(f"qdrant down for {vector_id}")
        # set_payload semantics: merge keys.
        self.points[vector_id].update(payload or {})


@pytest.fixture
def fk_factory():
    """SQLite with PRAGMA foreign_keys=ON (mirrors Postgres FK enforcement)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setattr(
        "app.governance.project_merge.read_cache.invalidate_search", lambda *_a, **_k: None
    )


def _seed_alias_with_spec(db):
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.flush()
    db.add(SpecWorkspace(project_id="sysmovs-delphi", slug="ws-a", name="WS A"))
    db.add(
        WriteQueueJob(
            project="sysmovs-delphi",
            hostname="h",
            client_name="c",
            text="t",
            status=WriteQueueStatus.done,
        )
    )
    db.commit()


def test_commit_failure_after_relocate_reverts_qdrant_payload(fk_factory, no_cache, monkeypatch):
    db = fk_factory()
    _seed_alias_with_spec(db)
    vs = FakeVectorStore(
        {
            "m1": {"project": "sysmovs-delphi", "data": "a"},
            "m2": {"project": "sysmovs-delphi", "data": "b"},
            "m3": {"project": "sysmovs", "data": "c"},
        }
    )

    def _boom():
        raise RuntimeError("commit exploded")

    monkeypatch.setattr(db, "commit", _boom)
    with pytest.raises(ProjectMergeError):
        apply_project_merge(
            db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j-fail"
        )

    assert vs.points["m1"]["project"] == "sysmovs-delphi"
    assert vs.points["m2"]["project"] == "sysmovs-delphi"
    assert vs.points["m3"]["project"] == "sysmovs"
    db.close()

    check = fk_factory()
    assert check.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    assert (
        check.query(SpecWorkspace).filter(SpecWorkspace.project_id == "sysmovs-delphi").count()
        == 1
    )
    assert check.query(WriteQueueJob).filter(WriteQueueJob.project == "sysmovs-delphi").count() == 1
    check.close()


def test_relocate_failure_midway_reverts_already_moved(fk_factory, no_cache):
    db = fk_factory()
    _seed_alias_with_spec(db)
    vs = FakeVectorStore(
        {
            "m1": {"project": "sysmovs-delphi"},
            "m2": {"project": "sysmovs-delphi"},
        }
    )
    vs.fail_update_ids = {"m2"}
    with pytest.raises(ProjectMergeError) as exc_info:
        apply_project_merge(
            db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j-mid"
        )
    assert exc_info.value.compensation_failed_ids == []
    assert vs.points["m1"]["project"] == "sysmovs-delphi"
    db.close()
    check = fk_factory()
    assert check.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    check.close()


def test_compensation_failure_is_reported(fk_factory, no_cache, monkeypatch):
    db = fk_factory()
    _seed_alias_with_spec(db)
    vs = FakeVectorStore({"m1": {"project": "sysmovs-delphi"}})

    original_update = vs.update
    calls = {"n": 0}

    def _update(vector_id, vector=None, payload=None):
        calls["n"] += 1
        if calls["n"] > 1:  # forward move ok, compensation fails
            raise RuntimeError("qdrant gone")
        original_update(vector_id, vector=vector, payload=payload)

    vs.update = _update
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(ProjectMergeError) as exc_info:
        apply_project_merge(
            db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j-comp"
        )
    assert exc_info.value.compensation_failed_ids == ["m1"]
    assert "compensation failed" in str(exc_info.value)
    db.close()


def test_alias_with_spec_workspace_merges_without_fk_violation(fk_factory, no_cache):
    db = fk_factory()
    _seed_alias_with_spec(db)
    vs = FakeVectorStore({"m1": {"project": "sysmovs-delphi"}})

    moved = apply_project_merge(
        db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j-spec"
    )
    assert moved == 1
    assert vs.points["m1"]["project"] == "sysmovs"
    db.close()

    check = fk_factory()
    assert check.query(Project).filter(Project.name == "sysmovs-delphi").first() is None
    assert check.query(SpecWorkspace).filter(SpecWorkspace.project_id == "sysmovs").count() == 1
    assert check.query(WriteQueueJob).filter(WriteQueueJob.project == "sysmovs").count() == 1
    check.close()


def test_spec_slug_conflict_aborts_before_touching_qdrant(fk_factory, no_cache):
    db = fk_factory()
    _seed_alias_with_spec(db)
    db.add(SpecWorkspace(project_id="sysmovs", slug="ws-a", name="WS A canonical"))
    db.commit()
    vs = FakeVectorStore({"m1": {"project": "sysmovs-delphi"}})
    with pytest.raises(MergeRuleViolation):
        apply_project_merge(
            db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j-slug"
        )
    assert vs.points["m1"]["project"] == "sysmovs-delphi"
    db.close()


@pytest.mark.parametrize(
    "name,reason",
    [
        ("default", "default"),
        ("Default", "default"),
        ("tarefa-123-algo", "tarefa"),
        ("370631", "numeric"),
        ("sysmovs", None),
        ("melhorias-mem0", None),
    ],
)
def test_merge_block_reason(name, reason):
    assert merge_block_reason(name) == reason


@pytest.mark.parametrize(
    "canonical,aliases",
    [
        ("default", ["sysmovs-x"]),
        ("sysmovs", ["tarefa-abc"]),
        ("tarefa-abc", ["sysmovs"]),
        ("sysmovs", ["370631"]),
        ("370631", ["sysmovs"]),
    ],
)
def test_apply_project_merge_rejects_protected_names(fk_factory, no_cache, canonical, aliases):
    db = fk_factory()
    for name in {canonical, *aliases}:
        db.add(Project(name=name))
    db.commit()
    vs = FakeVectorStore({"m1": {"project": aliases[0]}})
    with pytest.raises(MergeRuleViolation):
        apply_project_merge(db, vs, canonical=canonical, aliases=aliases, job_id="j-rule")
    assert vs.points["m1"]["project"] == aliases[0]
    db.close()


def test_filter_group_by_rules_drops_protected_members():
    group = MergeGroup("default", ["tarefa-1", "370631", "sysmovs", "sysmovs-delphi"], 0.9, "r")
    filtered = filter_group_by_rules(group)
    assert filtered is not None
    assert filtered.canonical == "sysmovs"
    assert filtered.aliases == ["sysmovs-delphi"]
    assert filter_group_by_rules(MergeGroup("default", ["tarefa-1", "sysmovs"], 0.9, "r")) is None


def _llm_client(groups, vs=None):
    client = MagicMock()
    client.llm = FakeLLM(groups)
    client.vector_store = vs or MagicMock()
    return client


def test_scheduled_job_only_records_proposals(factory, monkeypatch, no_cache):
    db = factory()
    for name in ("sysmovs", "sysmovs-delphi", "default", "tarefa-x"):
        db.add(Project(name=name))
    db.commit()
    db.close()

    monkeypatch.setattr(
        "app.governance.project_merge.collect_project_profiles",
        lambda _db: [
            ProjectProfile("sysmovs", 10, None, []),
            ProjectProfile("sysmovs-delphi", 5, None, []),
            ProjectProfile("default", 50, None, []),
            ProjectProfile("tarefa-x", 3, None, []),
        ],
    )
    monkeypatch.setattr("app.governance.project_merge.count_project_memories", lambda _n: 0)
    vs = MagicMock()
    client = _llm_client(
        [
            {"canonical": "sysmovs", "aliases": ["sysmovs-delphi"], "confidence": 0.95},
            {"canonical": "default", "aliases": ["tarefa-x"], "confidence": 0.99},
        ],
        vs,
    )
    recorded = run_merge_projects_job(
        project=None,
        job_id="sched-1",
        session_factory=factory,
        memory_client_provider=lambda: client,
        payload={"scheduled": True},
    )
    assert recorded == 1
    vs.update.assert_not_called()

    db = factory()
    pending = merge_proposals.list_proposals(db, status="pending")
    assert len(pending) == 1
    assert pending[0]["canonical"] == "sysmovs"
    assert pending[0]["aliases"] == ["sysmovs-delphi"]
    # Alias is untouched until approval.
    assert db.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    db.close()

    # Same suggestion again is idempotent (no duplicate pending proposal).
    run_merge_projects_job(
        project=None,
        job_id="sched-2",
        session_factory=factory,
        memory_client_provider=lambda: client,
        payload={"scheduled": True},
    )
    db = factory()
    pending = merge_proposals.list_proposals(db, status="pending")
    assert len(pending) == 1
    assert merge_proposals.count_proposals(db) == 1
    db.close()


def _governance_app(factory, monkeypatch, *, merge_enabled=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.utils.governance_policy import (
        DEFAULT_POLICY,
        default_processes_enabled,
        save_global_policy,
    )

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    db = factory()
    doc = {**DEFAULT_POLICY}
    processes = default_processes_enabled()
    processes["merge_projects"] = merge_enabled
    doc["processes_enabled"] = processes
    save_global_policy(db, doc)
    db.close()

    from app.routers.apps import router as apps_router

    app = FastAPI()
    app.include_router(_governance_merge.router)
    app.include_router(apps_router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _enqueue_capture(monkeypatch):
    captured = []

    def _enqueue(job_type, *, project=None, payload=None, job_id=None):
        captured.append({"job_type": job_type, "payload": payload})
        return f"job-{len(captured)}"

    monkeypatch.setattr(_governance_merge.governance_queue, "enqueue", _enqueue)
    return captured


ADMIN = {"X-Admin-Token": "test-admin-token"}


def test_proposal_approval_flow_and_apps_listing(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)

    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = merge_proposals.record_proposals(
        db,
        [{"canonical": "sysmovs", "aliases": ["sysmovs-delphi"], "confidence": 0.9}],
        source_job_id="sched",
    )[0]
    db.close()

    assert client.get("/admin/governance/projects/merge-proposals").status_code == 401
    resp = client.get("/admin/governance/projects/merge-proposals?status=pending", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    # Approval requires admin credentials.
    assert (
        client.post(f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve")
        .status_code
        == 401
    )
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["proposal"]["status"] == "approved"
    assert captured[0]["payload"]["proposal_id"] == proposal["id"]
    assert captured[0]["payload"]["manual"] is True

    # Second approval → 409.
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    assert resp.status_code == 409

    # Worker applies the approved proposal.
    vs = FakeVectorStore(
        {
            "a1": {"project": "sysmovs"},
            "a2": {"project": "sysmovs"},
            "b1": {"project": "sysmovs-delphi"},
        }
    )
    applied = run_merge_projects_job(
        project=None,
        job_id="apply-1",
        session_factory=factory,
        memory_client_provider=lambda: _llm_client([], vs),
        payload=captured[0]["payload"],
    )
    assert applied == 1
    db = factory()
    stored = merge_proposals.get_proposal(db, proposal["id"])
    assert stored["status"] == "applied"
    assert stored["moved_memories"] == 1
    db.close()

    # Re-running the same job (retry) is a no-op.
    run_merge_projects_job(
        project=None,
        job_id="apply-1",
        session_factory=factory,
        memory_client_provider=lambda: _llm_client([], vs),
        payload=captured[0]["payload"],
    )
    assert vs.count("sysmovs") == 3

    monkeypatch.setattr("app.routers.apps.count_project_memories", vs.count)
    monkeypatch.setattr("app.routers.apps.count_distinct_memories_accessed", lambda *_: 0)
    resp = client.get("/api/v1/apps/")
    assert resp.status_code == 200
    apps = {a["name"]: a for a in resp.json()["apps"]}
    assert "sysmovs-delphi" not in apps
    assert apps["sysmovs"]["total_memories_created"] == 3


def test_approve_blocked_when_merge_paused(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch, merge_enabled=False)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = merge_proposals.record_proposals(
        db, [{"canonical": "sysmovs", "aliases": ["sysmovs-delphi"]}], source_job_id="s"
    )[0]
    db.close()
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    assert resp.status_code == 409
    assert captured == []
    db = factory()
    assert merge_proposals.get_proposal(db, proposal["id"])["status"] == "pending"
    db.close()


def test_reject_proposal_and_rejected_apply_is_noop(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = merge_proposals.record_proposals(
        db, [{"canonical": "sysmovs", "aliases": ["sysmovs-delphi"]}], source_job_id="s"
    )[0]
    db.close()
    client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/reject", headers=ADMIN
    )
    assert resp.status_code == 200
    assert resp.json()["proposal"]["status"] == "rejected"

    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    run_merge_projects_job(
        project=None,
        job_id="apply-x",
        session_factory=factory,
        memory_client_provider=lambda: _llm_client([], vs),
        payload=captured[0]["payload"],
    )
    assert vs.points["b1"]["project"] == "sysmovs-delphi"


def test_approve_rejects_protected_proposal(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    _enqueue_capture(monkeypatch)
    db = factory()
    # Bypass rules to simulate a legacy/injected proposal.
    proposal = merge_proposals.record_proposals(
        db, [{"canonical": "default", "aliases": ["sysmovs"]}], source_job_id="legacy"
    )[0]
    db.close()
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    assert resp.status_code == 422


def test_reconciliation_detects_half_applied_merge(factory):
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="melhorias-mem0"))
    db.add(Project(name="empty-project"))
    db.add(
        WriteQueueJob(
            project="melhorias-mem0",
            hostname="h",
            client_name="c",
            text="t",
            status=WriteQueueStatus.done,
        )
    )
    db.add(
        ReadAuditLog(
            project="melhorias-mem0",
            memory_id="m",
            access_type="search",
            source="mcp",
            hostname="h",
        )
    )
    db.commit()
    merge_proposals.record_proposals(
        db, [{"canonical": "sysmovs", "aliases": ["melhorias-mem0"]}], source_job_id="s"
    )

    counts = {"sysmovs": 40, "melhorias-mem0": 0, "empty-project": 0}
    report = detect_merge_inconsistencies(db, count_fn=lambda n: counts.get(n, 0))
    assert [r["project"] for r in report["items"]] == ["melhorias-mem0"]
    item = report["items"][0]
    assert item["sql_references"]["write_queue_done"] == 1
    assert item["suspected_half_merge"] is True
    assert item["related_proposals"][0]["canonical"] == "sysmovs"
    # Detection never mutates the catalog.
    assert db.query(Project).filter(Project.name == "melhorias-mem0").first() is not None
    db.close()


def test_reconciliation_endpoint_read_only(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="orphan"))
    db.add(
        WriteAuditLog(project="orphan", hostname="h", client_name="c", action="enqueue")
    )
    db.commit()
    db.close()
    monkeypatch.setattr(
        "app.governance.project_merge.facet_project_counts", lambda: {"ghost": 7}
    )
    assert client.get("/admin/governance/projects/merge-inconsistencies").status_code == 401
    resp = client.get("/admin/governance/projects/merge-inconsistencies", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["orphan_qdrant_projects"] == [{"project": "ghost", "qdrant_points": 7}]
    assert body["orphan_scan"] == "ok"
    db = factory()
    assert db.query(Project).filter(Project.name == "orphan").first() is not None
    db.close()


def test_governance_queue_preserves_error_history(factory):
    import uuid as _uuid

    from app.models import GovernanceJob, GovernanceJobStatus
    from app.utils.governance_queue import GovernanceQueue

    queue = GovernanceQueue(session_factory=factory)
    job_id = queue.enqueue("merge_projects", payload={"manual": True})
    queue.requeue(job_id, "ForeignKeyViolation spec_workspaces_project_id_fkey", 1)
    queue.mark_done(job_id)
    db = factory()
    row = db.query(GovernanceJob).filter(GovernanceJob.id == _uuid.UUID(job_id)).one()
    assert row.status == GovernanceJobStatus.done
    assert row.error is None
    assert "ForeignKeyViolation" in row.payload["error_history"][0]["error"]
    assert row.payload["error_history"][0]["attempt"] == 1
    assert row.payload["manual"] is True
    db.close()

    clean_id = queue.enqueue("dedup")
    queue.mark_done(clean_id)
    db = factory()
    row = db.query(GovernanceJob).filter(GovernanceJob.id == _uuid.UUID(clean_id)).one()
    assert row.error is None
    db.close()


def _rename(client, name, new_name, headers=None):
    from app.utils.project_apps import project_to_app_id

    return client.post(
        f"/api/v1/apps/{project_to_app_id(name)}/rename",
        json={"new_name": new_name},
        headers=headers or {},
    )


def test_rename_into_protected_project_rejected(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="default"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: MagicMock())
    assert _rename(client, "sysmovs", "default", ADMIN).status_code == 422


def test_rename_merge_requires_admin(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))
    resp = _rename(client, "sysmovs-delphi", "sysmovs")
    assert resp.status_code in (401, 403)
    assert vs.points["b1"]["project"] == "sysmovs-delphi"
    db = factory()
    assert merge_proposals.count_proposals(db) == 0
    db.close()


def test_rename_onto_existing_catalog_project_creates_proposal(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))

    resp = _rename(client, "sysmovs-delphi", "sysmovs", ADMIN)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "proposal_pending"
    assert body["proposal"]["canonical"] == "sysmovs"
    assert body["proposal"]["aliases"] == ["sysmovs-delphi"]
    assert body["proposal"]["origin"] == "rename"
    # Nothing was applied.
    assert vs.points["b1"]["project"] == "sysmovs-delphi"
    db = factory()
    assert db.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    db.close()

    # Repeating the rename is idempotent and returns the same open proposal.
    again = _rename(client, "sysmovs-delphi", "sysmovs", ADMIN)
    assert again.status_code == 202
    assert again.json()["proposal"]["id"] == body["proposal"]["id"]


def test_rename_onto_project_only_in_qdrant_creates_proposal(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    vs = FakeVectorStore({"a1": {"project": "sysmovs"}, "b1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))
    monkeypatch.setattr("app.routers.apps.count_project_memories_strict", vs.count)

    resp = _rename(client, "sysmovs-delphi", "sysmovs", ADMIN)
    assert resp.status_code == 202, resp.text
    assert vs.points["b1"]["project"] == "sysmovs-delphi"


def test_rename_merge_blocked_when_paused(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch, merge_enabled=False)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([]))
    assert _rename(client, "sysmovs-delphi", "sysmovs", ADMIN).status_code == 409
    db = factory()
    assert merge_proposals.count_proposals(db) == 0
    db.close()


def test_rename_qdrant_error_is_not_read_as_new_name(factory, monkeypatch):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))

    def _down(_name):
        raise VectorStoreUnavailable("qdrant down")

    monkeypatch.setattr("app.routers.apps.count_project_memories_strict", _down)
    assert _rename(client, "sysmovs-delphi", "sysmovs", ADMIN).status_code == 503
    assert vs.points["b1"]["project"] == "sysmovs-delphi"


def test_plain_rename_to_new_name_applies(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi", "data": "x"}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))
    monkeypatch.setattr("app.routers.apps.count_project_memories_strict", vs.count)

    resp = _rename(client, "sysmovs-delphi", "sysmovs-novo", ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved_memories"] == 1
    assert vs.points["b1"] == {"project": "sysmovs-novo", "data": "x"}


@pytest.mark.parametrize(
    "old,new",
    [
        ("default", "algo-novo"),
        ("tarefa-123", "algo-novo"),
        ("370631", "algo-novo"),
        ("sysmovs", "tarefa-novo"),
        ("sysmovs", "999"),
        ("sysmovs", "default"),
    ],
)
def test_plain_rename_with_protected_name_rejected(factory, monkeypatch, old, new):
    client = _governance_app(factory, monkeypatch)
    db = factory()
    db.add(Project(name=old))
    db.commit()
    db.close()
    vs = FakeVectorStore({"p1": {"project": old}})
    monkeypatch.setattr("app.routers.apps.get_memory_client_safe", lambda: _llm_client([], vs))
    monkeypatch.setattr("app.routers.apps.count_project_memories_strict", lambda _n: 0)
    resp = _rename(client, old, new, ADMIN)
    assert resp.status_code == 422, resp.text
    assert vs.points["p1"]["project"] == old


def _pending(db, canonical="sysmovs", aliases=("sysmovs-delphi",)):
    return merge_proposals.record_proposals(
        db, [{"canonical": canonical, "aliases": list(aliases)}], source_job_id="s"
    )[0]


def test_concurrent_approvals_only_one_wins(tmp_path):
    """Two sessions that both saw ``pending``: the atomic UPDATE lets one win."""
    import threading

    db_file = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    proposal = _pending(db)
    db.close()

    barrier = threading.Barrier(4)
    results = []

    def _approve(i):
        s = Session()
        try:
            assert merge_proposals.get_proposal(s, proposal["id"])["status"] == "pending"
            barrier.wait()
            merge_proposals.transition_proposal(
                s,
                proposal["id"],
                expected=merge_proposals.APPROVABLE_STATUSES,
                new_status="approved",
                decided_by=f"admin-{i}",
                apply_job_id=f"job-{i}",
            )
            results.append(("ok", i))
        except merge_proposals.ProposalError as exc:
            results.append(("conflict", exc.status_code))
        finally:
            s.close()

    threads = [threading.Thread(target=_approve, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()

    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1
    assert sorted(r for r in results if r[0] == "conflict") == [("conflict", 409)] * 3
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    s = sessionmaker(bind=engine)()
    stored = merge_proposals.get_proposal(s, proposal["id"])
    assert stored["status"] == "approved"
    assert stored["apply_job_id"] == f"job-{winners[0][1]}"
    s.close()
    engine.dispose()


def test_approve_vs_reject_race_stale_reader_loses(factory):
    db_a, db_b = factory(), factory()
    proposal = _pending(db_a)
    # Both readers see pending; A approves first, B's reject must 409.
    assert merge_proposals.get_proposal(db_b, proposal["id"])["status"] == "pending"
    merge_proposals.transition_proposal(
        db_a, proposal["id"], expected="pending", new_status="approved"
    )
    with pytest.raises(merge_proposals.ProposalError) as exc_info:
        merge_proposals.transition_proposal(
            db_b, proposal["id"], expected="pending", new_status="rejected"
        )
    assert exc_info.value.status_code == 409
    assert merge_proposals.get_proposal(db_b, proposal["id"])["status"] == "approved"
    db_a.close()
    db_b.close()


def _approve_and_apply(client, captured, factory, vs, proposal_id):
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal_id}/approve", headers=ADMIN
    )
    assert resp.status_code == 202, resp.text
    job = captured[-1]
    return run_merge_projects_job(
        project=None,
        job_id=resp.json()["job_id"],
        session_factory=factory,
        memory_client_provider=lambda: _llm_client([], vs),
        payload=job["payload"],
    )


def test_proposal_with_missing_canonical_fails_without_changes(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = _pending(db, canonical="sysmovs-fantasma")
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})

    applied = _approve_and_apply(client, captured, factory, vs, proposal["id"])
    assert applied == 0
    assert vs.points["b1"]["project"] == "sysmovs-delphi"
    db = factory()
    stored = merge_proposals.get_proposal(db, proposal["id"])
    assert stored["status"] == "failed"
    assert "não existe" in stored["last_error"]
    assert db.query(Project).filter(Project.name == "sysmovs-fantasma").first() is None
    assert db.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    db.close()


def test_proposal_with_no_existing_alias_fails(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.commit()
    proposal = _pending(db, aliases=("sumiu",))
    db.close()
    vs = FakeVectorStore({"a1": {"project": "sysmovs"}})
    assert _approve_and_apply(client, captured, factory, vs, proposal["id"]) == 0
    db = factory()
    assert merge_proposals.get_proposal(db, proposal["id"])["status"] == "failed"
    db.close()


def test_deterministic_violation_in_job_marks_failed_without_retry(
    fk_factory, monkeypatch, no_cache
):
    client = _governance_app(fk_factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = fk_factory()
    _seed_alias_with_spec(db)
    db.add(SpecWorkspace(project_id="sysmovs", slug="ws-a", name="clash"))
    db.commit()
    proposal = _pending(db)
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    # Does not raise: the worker marks the job done instead of retrying.
    assert _approve_and_apply(client, captured, fk_factory, vs, proposal["id"]) == 0
    db = fk_factory()
    stored = merge_proposals.get_proposal(db, proposal["id"])
    assert stored["status"] == "failed"
    assert "slug" in stored["last_error"]
    db.close()


def test_failed_proposal_can_be_reapproved(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = _pending(db)
    db.close()

    vs = FakeVectorStore({"a1": {"project": "sysmovs"}, "b1": {"project": "sysmovs-delphi"}})
    vs.fail_update_ids = {"b1"}
    with pytest.raises(ProjectMergeError):
        _approve_and_apply(client, captured, factory, vs, proposal["id"])
    db = factory()
    assert merge_proposals.get_proposal(db, proposal["id"])["status"] == "failed"
    db.close()

    # An older job retrying after re-approval is superseded (no-op).
    old_payload = captured[-1]["payload"]
    vs.fail_update_ids = set()
    assert _approve_and_apply(client, captured, factory, vs, proposal["id"]) == 1
    assert vs.points["b1"]["project"] == "sysmovs"
    assert (
        run_merge_projects_job(
            project=None,
            job_id="stale-job",
            session_factory=factory,
            memory_client_provider=lambda: _llm_client([], vs),
            payload=old_payload,
        )
        == 1
    )
    db = factory()
    assert merge_proposals.get_proposal(db, proposal["id"])["status"] == "applied"
    db.close()


def test_transient_job_failure_retry_resumes_same_job(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = _pending(db)
    db.close()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    vs.fail_update_ids = {"b1"}
    resp = client.post(
        f"/admin/governance/projects/merge-proposals/{proposal['id']}/approve", headers=ADMIN
    )
    job_id = resp.json()["job_id"]
    kwargs = dict(
        project=None,
        job_id=job_id,
        session_factory=factory,
        memory_client_provider=lambda: _llm_client([], vs),
        payload=captured[-1]["payload"],
    )
    with pytest.raises(ProjectMergeError):
        run_merge_projects_job(**kwargs)
    vs.fail_update_ids = set()
    assert run_merge_projects_job(**kwargs) == 1
    assert vs.points["b1"]["project"] == "sysmovs"


def test_applied_proposal_records_undo_info(factory, monkeypatch, no_cache):
    client = _governance_app(factory, monkeypatch)
    captured = _enqueue_capture(monkeypatch)
    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi", first_seen_hostname="dev"))
    wq = WriteQueueJob(
        project="sysmovs-delphi", hostname="h", client_name="c", text="t",
        status=WriteQueueStatus.done,
    )
    audit = WriteAuditLog(project="sysmovs-delphi", hostname="h", client_name="c", action="enqueue")
    db.add_all([wq, audit])
    db.commit()
    wq_id, audit_id = str(wq.id), str(audit.id)
    proposal = _pending(db)
    db.close()
    vs = FakeVectorStore(
        {"b1": {"project": "sysmovs-delphi"}, "b2": {"project": "sysmovs-delphi"}}
    )
    assert _approve_and_apply(client, captured, factory, vs, proposal["id"]) == 1

    db = factory()
    stored = merge_proposals.get_proposal(db, proposal["id"])
    db.close()
    assert stored["moved_memories"] == 2
    undo = stored["undo_info"]
    assert undo["canonical"] == "sysmovs"
    alias = undo["aliases"][0]
    assert alias["alias"] == "sysmovs-delphi"
    assert alias["memory_count"] == 2
    assert sorted(alias["qdrant_point_ids"]) == ["b1", "b2"]
    assert alias["write_queue_ids"] == [wq_id]
    assert alias["write_audit_log_ids"] == [audit_id]
    assert alias["project_row"]["first_seen_hostname"] == "dev"


def test_merge_with_governance_policy_and_token_usage_fk_on(fk_factory, no_cache):
    db = fk_factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.add(Project(name="outro"))
    db.add(Project(name="outro-alias"))
    db.flush()
    db.add(GovernancePolicy(project_name="sysmovs", overrides={"ttl_days": 30}))
    db.add(GovernancePolicy(project_name="sysmovs-delphi", overrides={"dedup": False}))
    db.add(GovernancePolicy(project_name="outro-alias", overrides={"ttl_days": 7}))
    for name in ("sysmovs-delphi", "outro-alias"):
        db.add(
            TokenUsageLog(
                project=name, agent="a", user_id="u", operation_type="add", model="m"
            )
        )
    db.commit()
    vs = FakeVectorStore(
        {"b1": {"project": "sysmovs-delphi"}, "c1": {"project": "outro-alias"}}
    )

    apply_project_merge(db, vs, canonical="sysmovs", aliases=["sysmovs-delphi"], job_id="j1")
    apply_project_merge(db, vs, canonical="outro", aliases=["outro-alias"], job_id="j2")
    db.close()

    check = fk_factory()
    policies = {p.project_name: p.overrides for p in check.query(GovernancePolicy).all()}
    assert policies == {
        "sysmovs": {"ttl_days": 30, "dedup": False},
        "outro": {"ttl_days": 7},
    }
    assert check.query(TokenUsageLog).filter(TokenUsageLog.project == "sysmovs").count() == 1
    assert check.query(TokenUsageLog).filter(TokenUsageLog.project == "outro").count() == 1
    assert check.query(Project).filter(Project.name.in_(["sysmovs-delphi", "outro-alias"])).count() == 0
    check.close()


def test_relocation_sends_only_project_field():
    vs = MagicMock()
    vs._create_filter.return_value = "filter"
    rec = MagicMock()
    rec.id = "p1"
    rec.payload = {"project": "a", "data": "keep", "hash": "h"}
    vs.client.scroll.return_value = ([rec], None)
    moved_log = []
    assert relocate_project_memories(vs, source="a", target="b", moved_log=moved_log) == 1
    vs.update.assert_called_once_with("p1", payload={"project": "b"})
    assert vs.client.scroll.call_args.kwargs["with_payload"] is False
    assert moved_log == [("p1", "a")]


def test_reconciliation_distinguishes_qdrant_error_from_zero(factory):
    db = factory()
    db.add(Project(name="melhorias-mem0"))
    db.add(
        WriteQueueJob(
            project="melhorias-mem0", hostname="h", client_name="c", text="t",
            status=WriteQueueStatus.done,
        )
    )
    db.commit()

    def _down(_name):
        raise VectorStoreUnavailable("qdrant down")

    report = detect_merge_inconsistencies(db, count_fn=_down)
    assert report["items"] == []
    assert report["qdrant_errors"][0]["project"] == "melhorias-mem0"
    db.close()


def test_reconciliation_reports_qdrant_projects_missing_from_catalog(factory):
    db = factory()
    db.add(Project(name="sysmovs"))
    db.commit()
    report = detect_merge_inconsistencies(
        db, facet_fn=lambda: {"sysmovs": 3, "fantasma": 5, "vazio": 0}
    )
    assert report["orphan_scan"] == "ok"
    assert report["orphan_qdrant_projects"] == [{"project": "fantasma", "qdrant_points": 5}]
    assert report["items"] == []
    db.close()


def test_merge_proposals_migration_round_trip_sqlite(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    api_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_root / "alembic"))

    command.upgrade(cfg, "r0s1t2u3v4w5")
    eng = create_engine(url)
    assert "project_merge_proposals" not in sa.inspect(eng).get_table_names()
    eng.dispose()

    command.upgrade(cfg, "m1p2r3o4p5s6")
    eng = create_engine(url)
    insp = sa.inspect(eng)
    assert "project_merge_proposals" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("project_merge_proposals")}
    assert {"status", "canonical", "aliases", "undo_info", "apply_job_id"} <= cols
    assert "ix_project_merge_proposals_status" in {
        i["name"] for i in insp.get_indexes("project_merge_proposals")
    }
    eng.dispose()

    command.downgrade(cfg, "r0s1t2u3v4w5")
    eng = create_engine(url)
    assert "project_merge_proposals" not in sa.inspect(eng).get_table_names()
    eng.dispose()


# --- Rodada 2: alias inelegível, commit ambíguo, payload da listagem, auth -------


def test_merge_with_no_eligible_alias_rolls_back(factory, no_cache):
    """Todos os aliases dedicados → MergeRuleViolation, sem criar o canônico."""
    from app.models import PartitionTier

    db = factory()
    db.add(Project(name="sysmovs-delphi", partition_tier=PartitionTier.dedicated))
    db.commit()
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    with pytest.raises(MergeRuleViolation, match="nenhum alias elegível"):
        apply_project_merge(
            db, vs, canonical="sysmovs-novo", aliases=["sysmovs-delphi"], job_id="j"
        )
    db.close()
    check = factory()
    assert check.query(Project).filter(Project.name == "sysmovs-novo").first() is None
    assert check.query(Project).filter(Project.name == "sysmovs-delphi").first() is not None
    check.close()
    assert vs.points["b1"]["project"] == "sysmovs-delphi"


def _ambiguous_commit(db, *, really_commit):
    from sqlalchemy.exc import OperationalError

    real_commit = db.commit

    def _commit():
        if really_commit:
            real_commit()
        raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    return _commit


@pytest.mark.parametrize("really_commit", [True, False])
def test_ambiguous_commit_on_rename_checks_alias_row(
    fk_factory, no_cache, monkeypatch, really_commit
):
    db = fk_factory()
    _seed_alias_with_spec(db)
    vs = FakeVectorStore({"m1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr(db, "commit", _ambiguous_commit(db, really_commit=really_commit))

    if really_commit:
        moved = apply_project_merge(
            db,
            vs,
            canonical="sysmovs-novo",
            aliases=["sysmovs-delphi"],
            job_id="j",
            require_new_canonical=True,
        )
        assert moved == 1
        assert vs.points["m1"]["project"] == "sysmovs-novo"  # não compensou
    else:
        with pytest.raises(ProjectMergeError):
            apply_project_merge(
                db,
                vs,
                canonical="sysmovs-novo",
                aliases=["sysmovs-delphi"],
                job_id="j",
                require_new_canonical=True,
            )
        assert vs.points["m1"]["project"] == "sysmovs-delphi"  # compensou
    db.close()


def test_ambiguous_commit_on_proposal_already_applied_does_not_compensate(
    factory, monkeypatch, no_cache
):
    from app.governance.project_merge import apply_approved_proposal

    db = factory()
    db.add(Project(name="sysmovs"))
    db.add(Project(name="sysmovs-delphi"))
    db.commit()
    proposal = _pending(db)
    merge_proposals.transition_proposal(
        db, proposal["id"], expected="pending", new_status="approved", apply_job_id="job-x"
    )
    vs = FakeVectorStore({"b1": {"project": "sysmovs-delphi"}})
    monkeypatch.setattr(db, "commit", _ambiguous_commit(db, really_commit=True))

    moved = apply_approved_proposal(
        db, vs, proposal_id=proposal["id"], job_id="job-x", count_fn=vs.count
    )
    db.close()
    assert moved == 1
    assert vs.points["b1"]["project"] == "sysmovs"
    check = factory()
    stored = merge_proposals.get_proposal(check, proposal["id"])
    check.close()
    assert stored["status"] == "applied"
    assert stored["apply_job_id"] == "job-x"


def test_proposal_listing_omits_undo_info(factory):
    db = factory()
    proposal = _pending(db)
    merge_proposals.transition_proposal(
        db,
        proposal["id"],
        expected="pending",
        new_status="applied",
        undo_info={"moved_memories": 3},
    )
    listed = merge_proposals.list_proposals(db)[0]
    detail = merge_proposals.get_proposal(db, proposal["id"])
    db.close()
    assert "undo_info" not in listed
    assert listed["moved_memories"] == 3
    assert detail["undo_info"] == {"moved_memories": 3}


def test_proposals_mentioning_only_open(factory):
    db = factory()
    open_p = _pending(db, canonical="sysmovs", aliases=("sysmovs-delphi",))
    closed = _pending(db, canonical="sysmovs", aliases=("sysmovs-x",))
    merge_proposals.transition_proposal(
        db, closed["id"], expected="pending", new_status="rejected"
    )
    ids = {p["id"] for p in merge_proposals.proposals_mentioning(db, "sysmovs")}
    db.close()
    assert ids == {open_p["id"]}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/governance/projects/merge-preview"),
        ("post", "/admin/governance/projects/merge"),
        ("post", "/admin/governance/projects/merge-now"),
    ],
)
def test_merge_endpoints_require_admin(factory, monkeypatch, method, path):
    client = _governance_app(factory, monkeypatch)
    kwargs = {"json": {"dry_run": True}} if method == "post" else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401


def test_lock_proposal_sets_lock_timeout_before_for_update():
    """PostgreSQL: the row wait in ``FOR UPDATE`` must be bounded by lock_timeout."""
    calls = []
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.side_effect = lambda stmt, *a, **k: calls.append(("execute", str(stmt)))
    query = db.query.return_value.filter.return_value
    query.with_for_update.side_effect = lambda: calls.append(("for_update", "")) or query

    merge_proposals.lock_proposal(db, "00000000-0000-0000-0000-000000000001")

    assert calls[0] == (
        "execute",
        f"SET LOCAL lock_timeout = '{merge_proposals.MERGE_LOCK_TIMEOUT}'",
    )
    assert calls[1][0] == "for_update"
