"""Tests for governance queue + worker (tasks 05–06)."""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import uuid

from app.models import Base, GovernanceJobStatus
from app.utils.governance_queue import GovernanceQueue


@pytest.fixture
def queue(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'gq.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    return GovernanceQueue(session_factory=factory)


def test_enqueue_dequeue_fifo(queue):
    id1 = queue.enqueue("dedup", project="p1")
    id2 = queue.enqueue("dedup", project="p2")
    jobs = queue.dequeue(limit=2)
    assert len(jobs) == 2
    assert jobs[0].id == id1
    assert jobs[1].id == id2


@pytest.mark.asyncio
async def test_process_once_marks_done(queue):
    from app.workers import governance_worker as gw

    job_id = queue.enqueue("dedup", project="p1")
    handler = MagicMock(return_value=1)
    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={"dedup": handler},
        enable_scheduler=False,
        session_factory=queue._session_factory,
    )
    processed = await worker.process_once()
    assert processed == 1
    handler.assert_called_once()
    db = queue._session_factory()
    from app.models import GovernanceJob

    row = db.query(GovernanceJob).filter_by(id=uuid.UUID(job_id)).one()
    assert row.status == GovernanceJobStatus.done
    db.close()


@pytest.mark.asyncio
async def test_failed_job_requeues_until_max_attempts(queue):
    from app.workers import governance_worker as gw

    queue.enqueue("dedup")
    calls = {"n": 0}

    def boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("fail")

    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={"dedup": boom},
        max_attempts=2,
        enable_scheduler=False,
        session_factory=queue._session_factory,
    )
    await worker.process_once()
    db = queue._session_factory()
    from app.models import GovernanceJob

    row = db.query(GovernanceJob).one()
    assert row.status == GovernanceJobStatus.queued
    assert row.attempts == 1
    db.close()

    await worker.process_once()
    db = queue._session_factory()
    row = db.query(GovernanceJob).one()
    assert row.status == GovernanceJobStatus.failed
    db.close()


@pytest.mark.asyncio
async def test_off_peak_curfew_defers_scheduled_but_runs_manual(queue):
    """Off-peak curfew: outside the window scheduled jobs wait, but a manually
    forced job runs immediately; when the window opens the scheduled job drains
    (tasks 05–06 / off-peak stop+wake, manual bypass)."""
    import asyncio

    from app.models import GovernanceJob
    from app.workers import governance_worker as gw

    sched_id = queue.enqueue("dedup", project="p1", payload={"scheduled": True})
    man_id = queue.enqueue("dedup", project="p2", payload={"manual": True})
    handler = MagicMock(return_value=1)
    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={"dedup": handler},
        enable_scheduler=False,
        enforce_off_peak=True,
        window_cache_ttl=0.0,  # re-check the window every loop (deterministic test)
        idle_sleep=0.01,
        scheduler_sleep=0.01,
        session_factory=queue._session_factory,
    )

    def _statuses():
        db = queue._session_factory()
        try:
            return {str(r.id): r.status for r in db.query(GovernanceJob).all()}
        finally:
            db.close()

    loop = asyncio.get_running_loop()

    async def _wait_for(predicate, *, what, timeout=15.0):
        # Poll instead of a fixed sleep: the worker hop (DB dequeue → to_thread
        # handler → DB mark_done) outlives 50ms on loaded runners, which flaked
        # the old sleep. Asserts on the event, not the wall clock.
        deadline = loop.time() + timeout
        last = _statuses()
        while not predicate(last):
            if loop.time() >= deadline:
                raise AssertionError(f"timed out waiting for {what}: {last}")
            await asyncio.sleep(0.01)
            last = _statuses()
        return last

    # Outside the window: manual job runs, scheduled job is held back.
    worker._in_off_peak_window = lambda: False
    worker.start()
    st = await _wait_for(
        lambda s: s[man_id] == GovernanceJobStatus.done,
        what=f"manual job {man_id} done",
    )
    assert st[sched_id] == GovernanceJobStatus.queued

    # Window opens: the scheduled job drains too.
    worker._in_off_peak_window = lambda: True
    await _wait_for(
        lambda s: s[sched_id] == GovernanceJobStatus.done,
        what=f"scheduled job {sched_id} done",
    )
    await worker.stop()


# ---------------------------------------------------------------------------
# Process-level enable/disable (controles individuais de governança)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_process_stays_queued_until_reenabled(queue):
    """Desabilitar um processo pausa os jobs já enfileirados (sem mudar o
    status) e a execução retoma quando o processo é reativado."""
    from app.models import GovernanceJob
    from app.utils.governance_policy import (
        DEFAULT_POLICY,
        default_processes_enabled,
        save_global_policy,
    )
    from app.workers import governance_worker as gw

    db = queue._session_factory()
    doc = {**DEFAULT_POLICY}
    disabled = default_processes_enabled()
    disabled["dedup"] = False
    doc["processes_enabled"] = disabled
    save_global_policy(db, doc)
    db.close()

    job_id = queue.enqueue("dedup", project="p1")
    handler = MagicMock(return_value=1)
    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={"dedup": handler},
        enable_scheduler=False,
        session_factory=queue._session_factory,
    )

    # Processo desabilitado: nenhum job é consumido, nem manual.
    assert await worker.process_once() == 0
    assert await worker.process_once(manual_only=True) == 0
    handler.assert_not_called()
    db = queue._session_factory()
    row = db.query(GovernanceJob).filter_by(id=uuid.UUID(job_id)).one()
    assert row.status == GovernanceJobStatus.queued
    db.close()

    # Reativado: o job pendente é executado.
    db = queue._session_factory()
    doc2 = {**DEFAULT_POLICY}
    doc2["processes_enabled"] = default_processes_enabled()
    save_global_policy(db, doc2)
    db.close()

    assert await worker.process_once() == 1
    handler.assert_called_once()
    db = queue._session_factory()
    row = db.query(GovernanceJob).filter_by(id=uuid.UUID(job_id)).one()
    assert row.status == GovernanceJobStatus.done
    db.close()


@pytest.mark.asyncio
async def test_run_retries_startup_recovery_until_db_answers(queue, monkeypatch):
    import asyncio

    from app.workers import governance_worker as gw

    monkeypatch.setattr(gw, "STARTUP_RETRY_INITIAL_SEC", 0.01)
    real_recover = queue.recover_stale_processing
    calls = {"n": 0}

    def recover_failing_twice(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("failed to resolve host 'pgbouncer'")
        return real_recover(*args, **kwargs)

    queue.recover_stale_processing = recover_failing_twice
    job_id = queue.enqueue("dedup", project="p1", payload={"manual": True})
    handler = MagicMock(return_value=1)
    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={"dedup": handler},
        enable_scheduler=False,
        enforce_off_peak=False,
        idle_sleep=0.01,
        session_factory=queue._session_factory,
    )

    task = worker.start()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if handler.called:
            break
    assert not task.done()
    await worker.stop()

    assert calls["n"] >= 3
    handler.assert_called_once()
    assert job_id


@pytest.mark.asyncio
async def test_stop_interrupts_governance_startup_retries(queue):
    import asyncio

    from app.workers import governance_worker as gw

    queue.recover_stale_processing = MagicMock(side_effect=RuntimeError("db down"))
    worker = gw.GovernanceWorker(
        queue=queue,
        handlers={},
        enable_scheduler=False,
        session_factory=queue._session_factory,
    )

    task = worker.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(worker.stop(), timeout=1)

    assert task.done()


@pytest.mark.asyncio
async def test_governance_main_exits_nonzero_when_run_loop_dies():
    import asyncio
    from unittest.mock import patch

    from app.workers import governance_worker as gw

    class DyingWorker:
        def __init__(self):
            self.stopped = False

        def start(self):
            async def _die():
                raise RuntimeError("failed to resolve host 'pgbouncer'")

            self._task = asyncio.create_task(_die())
            return self._task

        async def stop(self):
            self.stopped = True
            await self._task

    worker = DyingWorker()
    with patch.object(gw, "worker_from_env", return_value=worker):
        exit_code = await asyncio.wait_for(gw._main(), timeout=1)

    assert exit_code == 1
    assert worker.stopped
