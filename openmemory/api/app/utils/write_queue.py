"""Persistent write queue access layer.

Implements the ``WriteQueue`` interface described in the TechSpec
("Design de Implementação → Interfaces Principais"). It decouples the MCP
``add_memories`` tool from the (slow) LLM extraction performed by the background
worker: writes are validated and enqueued (returning an immediate ack/job_id),
and a worker later consumes the queue.

Persistence is SQLite-backed through the existing SQLAlchemy stack
(``app.database``), so enqueued jobs survive process restarts.
"""

import uuid
from datetime import timedelta
from dataclasses import dataclass
from typing import List, Optional

from app.database import SessionLocal, is_postgresql
from app.utils.datetime_format import format_utc_iso
from app.models import WriteQueueJob as WriteQueueModel
from app.models import WriteQueueStatus
from sqlalchemy.orm import Session


@dataclass
class WriteJob:
    """A single write request enqueued for asynchronous LLM extraction."""
    id: str           # tracking id returned in the ack
    project: str      # target space/project (auto-cataloged)
    hostname: str     # identity (attribution/audit)
    client_name: str  # originating MCP client/agent
    text: str         # raw content for LLM extraction
    created_at: str
    attempts: int = 0  # processing attempts already made (retry bookkeeping)
    extras: Optional[dict] = None  # e.g. {"supersedes": ["uuid", ...]}


def _to_job(row: WriteQueueModel) -> WriteJob:
    """Map a persisted row to the in-memory ``WriteJob`` dataclass."""
    extras = getattr(row, "extras", None)
    if extras is not None and not isinstance(extras, dict):
        extras = None
    return WriteJob(
        id=str(row.id),
        project=row.project,
        hostname=row.hostname,
        client_name=row.client_name,
        text=row.text,
        created_at=format_utc_iso(row.created_at),
        attempts=row.attempts or 0,
        extras=extras,
    )


class WriteQueue:
    """SQLite-backed implementation of the ``WriteQueue`` protocol.

    Each public method opens a short-lived session (via ``SessionLocal`` by
    default) and commits the state transition so that progress is durable. A
    custom ``session_factory`` can be injected for testing against a temporary
    database.
    """

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def enqueue(self, job: WriteJob) -> str:
        """Persist a new job with status ``queued`` and return its ``job_id``."""
        db = self._session()
        try:
            row = WriteQueueModel(
                id=uuid.UUID(job.id) if job.id else uuid.uuid4(),
                project=job.project,
                hostname=job.hostname,
                client_name=job.client_name,
                text=job.text,
                extras=job.extras if isinstance(job.extras, dict) else None,
                status=WriteQueueStatus.queued,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return str(row.id)
        finally:
            db.close()

    def dequeue(self, limit: int = 1) -> List[WriteJob]:
        """Return up to ``limit`` ``queued`` jobs, marking them ``processing``.

        Jobs are returned oldest-first (FIFO) and the transition to
        ``processing`` is committed before returning, so a crash after dequeue
        does not silently re-deliver the same job as ``queued``.

        On PostgreSQL, ``FOR UPDATE SKIP LOCKED`` guarantees each job is
        delivered to at most one concurrent worker (ADR-003). On SQLite the
        lock clause is omitted (no-op) so dev mode keeps working.

        ``updated_at`` is bumped on claim so stale-processing age is measured
        from when the worker took the job (not from enqueue).
        """
        from app.utils.datetime_utc import utc_now_naive

        db = self._session()
        try:
            query = (
                db.query(WriteQueueModel)
                .filter(WriteQueueModel.status == WriteQueueStatus.queued)
                .order_by(WriteQueueModel.created_at.asc())
            )
            if is_postgresql(str(db.get_bind().url)):
                query = query.with_for_update(skip_locked=True)
            rows = query.limit(limit).all()
            jobs = []
            now = utc_now_naive()
            for row in rows:
                row.status = WriteQueueStatus.processing
                row.updated_at = now
                jobs.append(_to_job(row))
            db.commit()
            return jobs
        finally:
            db.close()

    def mark_done(self, job_id: str) -> None:
        """Transition a job to ``done``."""
        self._set_status(job_id, WriteQueueStatus.done)

    def mark_done_if_processing(self, job_id: str) -> bool:
        """Mark ``done`` only if the job is still ``processing``.

        Used after a job-level timeout: a hung ``asyncio.to_thread(add)`` may
        still finish later; we must not overwrite a terminal ``failed`` status.
        """
        db = self._session()
        try:
            row = (
                db.query(WriteQueueModel)
                .filter(WriteQueueModel.id == uuid.UUID(str(job_id)))
                .first()
            )
            if row is None or row.status != WriteQueueStatus.processing:
                return False
            row.status = WriteQueueStatus.done
            row.error = None
            db.commit()
            return True
        finally:
            db.close()

    def mark_skipped(self, job_id: str, reason: str) -> None:
        """Transition a job to ``skipped`` (processed, but nothing new to persist)."""
        self._set_status(job_id, WriteQueueStatus.skipped, error=reason)

    def mark_failed(self, job_id: str, error: str, attempts: Optional[int] = None) -> None:
        """Transition a job to ``failed`` (terminal) and record the ``error``.

        ``attempts`` (when provided) records how many processing attempts were
        made before giving up, for diagnostics.
        """
        self._set_status(
            job_id, WriteQueueStatus.failed, error=error, attempts=attempts
        )

    def requeue(self, job_id: str, error: str, attempts: int) -> None:
        """Put a job back in ``queued`` for another attempt (retry).

        Records the last ``error`` and the incremented ``attempts`` count so the
        worker can stop retrying once the configured ceiling is reached. The job
        is never lost: it stays in the table, becoming eligible for ``dequeue``
        again on the next pass.
        """
        self._set_status(
            job_id, WriteQueueStatus.queued, error=error, attempts=attempts
        )

    def get_job(self, job_id: str) -> Optional[dict]:
        """Return status info for a job by id, or None if not found."""
        db = self._session()
        try:
            row = (
                db.query(WriteQueueModel)
                .filter(WriteQueueModel.id == uuid.UUID(str(job_id)))
                .first()
            )
            if row is None:
                return None
            return {
                "job_id": str(row.id),
                "status": row.status.value,
                "project": row.project,
                "hostname": row.hostname,
                "attempts": row.attempts or 0,
                "error": row.error,
                "created_at": format_utc_iso(row.created_at) or None,
            }
        finally:
            db.close()

    def depth(self) -> int:
        """Return the number of pending (``queued`` or ``processing``) jobs."""
        db = self._session()
        try:
            return (
                db.query(WriteQueueModel)
                .filter(
                    WriteQueueModel.status.in_(
                        [WriteQueueStatus.queued, WriteQueueStatus.processing]
                    )
                )
                .count()
            )
        finally:
            db.close()

    def recover_stale_processing(self, older_than_minutes: int | None = None) -> int:
        """Return orphaned ``processing`` jobs to ``queued``.

        When ``older_than_minutes`` is set, only jobs whose ``updated_at`` is at
        least that old are requeued (periodic recovery). When ``None``, all
        ``processing`` rows are requeued (startup recovery after a crash).
        """
        from app.utils.datetime_utc import utc_now_naive

        db = self._session()
        try:
            query = db.query(WriteQueueModel).filter(
                WriteQueueModel.status == WriteQueueStatus.processing
            )
            if older_than_minutes is not None and older_than_minutes > 0:
                cutoff = utc_now_naive() - timedelta(minutes=older_than_minutes)
                query = query.filter(WriteQueueModel.updated_at <= cutoff)
            rows = query.all()
            for row in rows:
                row.status = WriteQueueStatus.queued
            if rows:
                db.commit()
            return len(rows)
        finally:
            db.close()

    def recover_failed_jobs(self, older_than_minutes: int) -> int:
        """Re-queue terminal ``failed`` jobs after a cooldown (infra recovery).

        Jobs that have been ``failed`` for at least ``older_than_minutes`` are
        moved back to ``queued`` with ``attempts`` reset so the worker can try
        again (e.g. after LLM/DB came back). Returns the number recovered.
        """
        from app.utils.datetime_utc import utc_now_naive

        if older_than_minutes <= 0:
            return 0
        db = self._session()
        try:
            cutoff = utc_now_naive() - timedelta(minutes=older_than_minutes)
            rows = (
                db.query(WriteQueueModel)
                .filter(
                    WriteQueueModel.status == WriteQueueStatus.failed,
                    WriteQueueModel.updated_at <= cutoff,
                )
                .all()
            )
            for row in rows:
                row.status = WriteQueueStatus.queued
                row.attempts = 0
                row.error = (
                    f"auto-recovered after {older_than_minutes}m cooldown"
                )
            if rows:
                db.commit()
            return len(rows)
        finally:
            db.close()

    def fail_stale_processing(
        self,
        older_than_minutes: int,
        error: str,
    ) -> int:
        """Mark orphaned ``processing`` jobs as terminal ``failed``.

        Used when a job exceeded the per-job timeout (or the worker died while
        holding the claim). Materializes a failure the admin UI can show and
        reprocess via retry-failed — instead of leaving jobs stuck forever.
        """
        from app.utils.datetime_utc import utc_now_naive

        if older_than_minutes <= 0:
            return 0
        db = self._session()
        try:
            cutoff = utc_now_naive() - timedelta(minutes=older_than_minutes)
            rows = (
                db.query(WriteQueueModel)
                .filter(
                    WriteQueueModel.status == WriteQueueStatus.processing,
                    WriteQueueModel.updated_at <= cutoff,
                )
                .all()
            )
            for row in rows:
                row.status = WriteQueueStatus.failed
                row.error = error
                row.attempts = max(row.attempts or 0, 1)
            if rows:
                db.commit()
            return len(rows)
        finally:
            db.close()

    def fail_stalled_queued(
        self,
        older_than_minutes: int,
        error: str,
    ) -> int:
        """Mark old ``queued`` jobs as ``failed`` (worker-dead stall only).

        Must not be called while the worker is healthy: a slow FIFO backlog is
        legitimate on limited hardware. Call only when the heartbeat says the
        consumer is stalled/dead.
        """
        from app.utils.datetime_utc import utc_now_naive

        if older_than_minutes <= 0:
            return 0
        db = self._session()
        try:
            cutoff = utc_now_naive() - timedelta(minutes=older_than_minutes)
            rows = (
                db.query(WriteQueueModel)
                .filter(
                    WriteQueueModel.status == WriteQueueStatus.queued,
                    WriteQueueModel.created_at <= cutoff,
                )
                .all()
            )
            for row in rows:
                row.status = WriteQueueStatus.failed
                row.error = error
                row.attempts = max(row.attempts or 0, 1)
            if rows:
                db.commit()
            return len(rows)
        finally:
            db.close()

    def _set_status(
        self,
        job_id: str,
        status: WriteQueueStatus,
        error: Optional[str] = None,
        attempts: Optional[int] = None,
    ) -> None:
        db = self._session()
        try:
            row = (
                db.query(WriteQueueModel)
                .filter(WriteQueueModel.id == uuid.UUID(str(job_id)))
                .first()
            )
            if row is None:
                return
            row.status = status
            if error is not None:
                row.error = error
            if attempts is not None:
                row.attempts = attempts
            db.commit()
        finally:
            db.close()


# Default instance backed by the application's SessionLocal. The worker
# (task_06) and add_memories (task_07) import this.
write_queue = WriteQueue()


def estimate_write_wait_sec(
    queue_depth: int,
    *,
    ema_job_sec: float | None = None,
    concurrency: int | None = None,
) -> int:
    """Approximate seconds until a just-enqueued write becomes searchable.

    Uses ``depth * ema_job_sec / eta_concurrency``.

    Defaults reflect measured local drain (~43–50s/job wall-clock) with a
    **serial** LLM (llama.cpp ``--parallel 1`` / Ollama embed ``-np 1``):

    - ``WRITE_WORKER_EMA_JOB_SEC`` default **45** (Phase-2 dominated job time)
    - ``WRITE_WORKER_ETA_CONCURRENCY`` default **1** (do not assume worker
      ``MAX_CONCURRENCY`` speeds the queue when the LLM only accepts one slot)

    Set ``WRITE_WORKER_ETA_CONCURRENCY`` to match real parallel LLM slots when
    those are raised; until then, dividing by ``WRITE_WORKER_MAX_CONCURRENCY``
    underestimates wait by ~40%+ (e.g. depth 40 → ~1000s optimistic vs ~1800s).
    """
    import math
    import os

    if ema_job_sec is None:
        try:
            ema_job_sec = float(os.getenv("WRITE_WORKER_EMA_JOB_SEC", "45"))
        except ValueError:
            ema_job_sec = 45.0
    if concurrency is None:
        # Prefer explicit ETA concurrency; do NOT default to MAX_CONCURRENCY
        # (worker semaphore can be >1 while the LLM still serializes).
        raw = os.getenv("WRITE_WORKER_ETA_CONCURRENCY")
        if raw is None or raw.strip() == "":
            concurrency = 1
        else:
            try:
                concurrency = int(raw)
            except ValueError:
                concurrency = 1

    depth = max(0, int(queue_depth))
    ema = max(0.1, float(ema_job_sec))
    conc = max(1, int(concurrency))
    if depth <= 0:
        return 0
    return int(math.ceil(depth * ema / conc))
