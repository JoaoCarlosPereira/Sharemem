"""Asynchronous write worker (task_06).

Consumes the persistent write queue (``app.utils.write_queue.write_queue``) and
runs the slow LLM extraction/persistence out of band so that the MCP
``add_memories`` tool can return an immediate ack (ADR-004). For each job the
worker:

1. dequeues ``queued`` jobs (the queue marks them ``processing``);
2. calls the mem0 client ``add(text, project=…, user_id=hostname, metadata=…)``
   — ``user_id`` carries the hostname purely for attribution (ADR-003);
3. upserts the project catalog on the first write of each project (ADR-002),
   idempotently;
4. marks the job ``done`` on success; on failure it retries (re-queues the job
   with an incremented ``attempts`` count) until ``max_attempts`` is reached,
   then marks it terminally ``failed`` recording the error — failed jobs are
   never lost, they stay in the table.

Concurrency against the local LLM is bounded by an ``asyncio.Semaphore`` so a
burst of writes never fires more than ``max_concurrency`` simultaneous
inferences, protecting the Ollama instance from overload.

Testability: the loop is split into :meth:`WriteWorker.process_once` (one
dequeue+process pass) so tests can drive a single iteration without an infinite
loop. :meth:`WriteWorker.run` is the long-running loop used at app startup.
"""

import asyncio
import inspect
import logging
import os
from typing import Callable, Optional

from app.utils.logging_context import job_id_var
from app.utils.partitioning import bind_active_collection, partition_resolver
from app.utils.metrics import (
    DUAL_WRITE_ERRORS,
    WRITE_QUEUE_DEPTH,
    WRITE_WORKER_ERRORS,
    WRITE_WORKER_SUCCESS,
)
from app.utils.project_name import normalize_project
from app.utils.projects import upsert_project as _default_upsert_project
from app.utils.read_cache import read_cache
from app.utils.scope_keys import resolve_task
from app.utils.token_usage_wrapper import usage_attribution
from app.utils.write_queue import WriteJob
from app.utils.write_queue import write_queue as _default_write_queue
from app.utils.write_worker_heartbeat import beat as _heartbeat_beat

logger = logging.getLogger(__name__)


SKIPPED_NO_MEMORIES_REASON = (
    "Nenhuma memória nova (já existe ou conteúdo sem fatos extraíveis)"
)

EXTRACTION_FAILED_MSG = (
    "LLM não extraiu fatos e gravação bruta também falhou (verifique o backend LLM)"
)

LLM_INFRA_ERROR_MSG = (
    "LLM indisponível ou modelo inválido — job será reprocessado quando o backend estiver OK"
)

JOB_TIMEOUT_ERROR_FMT = (
    "Timeout do job após {timeout:.0f}s (LLM/embed lento ou travado) — "
    "use Reprocessar Falhas na UI"
)

# Default bound on concurrent LLM inferences. Kept small so the local LLM is not
# saturated by a burst of queued writes; overridable via the constructor.
# NOTE: local llama.cpp often runs with ``--parallel 1``; raising this without
# raising LLM parallel slots does not improve drain rate (requests still serialize).
# Also: the shared mem0 Memory singleton is not safe for overlapping ``add()``;
# only raise concurrency after confirming LLM parallel slots AND client isolation.
DEFAULT_MAX_CONCURRENCY = 1

# How many times a job is attempted before it is marked terminally ``failed``.
# On a failed attempt the job is re-queued (retentativa) until this ceiling is
# reached; jobs are never lost regardless (ADR-004).
DEFAULT_MAX_ATTEMPTS = 3

# How many jobs to pull from the queue per pass and how long to idle (seconds)
# when the queue is empty before polling again.
# Must be >= max_concurrency or the semaphore never sees overlapping jobs
# (historical bug: concurrency=2 + batch_size=1 → strictly serial drain).
DEFAULT_BATCH_SIZE = 1
DEFAULT_IDLE_SLEEP = 1.0

# Re-queue terminal ``failed`` jobs after this many minutes (0 = disabled).
DEFAULT_FAILED_RETRY_AFTER_MINUTES = 15

# Per-job wall-clock limit (slow HW: ~5–6 min/job is normal; 30 min is generous).
# Prevents a hung ``add()`` from locking the consumer forever without a UI failure.
DEFAULT_JOB_TIMEOUT_SEC = 1800.0

# Redis heartbeat so the API can detect a frozen/dead worker.
DEFAULT_HEARTBEAT_INTERVAL_SEC = 15.0

# How often the side recovery/heartbeat loop runs (independent of process_once).
DEFAULT_SIDE_LOOP_INTERVAL_SEC = 15.0

STARTUP_RETRY_INITIAL_SEC = 1.0
STARTUP_RETRY_MAX_SEC = 30.0




def _persisted_result_count(result) -> int:
    """Count non-DELETE memories persisted by a successful ``add()`` call."""
    if not isinstance(result, dict):
        return 0
    count = 0
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("event") or "ADD").upper() == "DELETE":
            continue
        if item.get("id"):
            count += 1
    return count


def _is_llm_infrastructure_error(message: str | None) -> bool:
    """True when empty extraction is likely caused by LLM/config outage, not content."""
    if not message:
        return False
    lower = message.lower()
    markers = (
        "404",
        "401",
        "403",
        "500",
        "502",
        "503",
        "nao esta carregado",
        "não está carregado",
        "not loaded",
        "not found",
        "authentication",
        "api key",
        "invalid api key",
        "connection refused",
        "connection error",
        "timed out",
        "timeout",
        "modelo ",
        "model ",
        "unavailable",
        "indispon",
    )
    return any(marker in lower for marker in markers)


def _last_llm_extraction_error() -> str | None:
    from mem0.memory.main import get_last_llm_extraction_error

    return get_last_llm_extraction_error()


def _clear_llm_extraction_error() -> None:
    from mem0.memory.main import clear_last_llm_extraction_error

    clear_last_llm_extraction_error()


def _default_client_provider():
    """Lazily import the memory client accessor.

    Imported lazily from ``app.utils.memory`` (the infrastructure layer) so that
    importing this module never triggers client/Ollama initialization.
    """
    from app.utils.memory import get_memory_client_safe

    return get_memory_client_safe()


class WriteWorker:
    """Background consumer of the write queue.

    Args:
        queue: The write queue to consume (defaults to the shared singleton).
        client_provider: Zero-arg callable returning a mem0 client (or ``None``
            when the LLM/memory backend is unavailable). Injected for testing.
        upsert_project: Callable ``(name, hostname) -> None`` registering a
            project in the catalog (idempotent).
        max_concurrency: Maximum number of concurrent ``add`` calls.
        batch_size: Maximum number of jobs dequeued per pass.
        idle_sleep: Seconds to sleep when the queue is empty.
    """

    def __init__(
        self,
        queue=None,
        client_provider: Optional[Callable] = None,
        upsert_project: Optional[Callable] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_sleep: float = DEFAULT_IDLE_SLEEP,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        failed_retry_after_minutes: int = DEFAULT_FAILED_RETRY_AFTER_MINUTES,
        job_timeout_sec: float = DEFAULT_JOB_TIMEOUT_SEC,
        heartbeat_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
    ):
        self._queue = queue if queue is not None else _default_write_queue
        self._client_provider = client_provider or _default_client_provider
        self._upsert_project = upsert_project or _default_upsert_project
        self._max_concurrency = max(1, int(max_concurrency))
        # Dequeue at least max_concurrency jobs per pass so the semaphore can
        # actually overlap work. A smaller batch_size silently forced serial FIFO.
        configured_batch = max(1, int(batch_size))
        self._batch_size = max(configured_batch, self._max_concurrency)
        self._idle_sleep = idle_sleep
        self._max_attempts = max(1, int(max_attempts))
        self._failed_retry_after_minutes = max(0, int(failed_retry_after_minutes))
        self._job_timeout_sec = max(0.0, float(job_timeout_sec))
        self._heartbeat_interval_sec = max(1.0, float(heartbeat_interval_sec))

        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        # Projects already cataloged in this process; avoids a redundant DB
        # round-trip after the first write of a project (the catalog upsert is
        # idempotent regardless, this is just a cheap short-circuit).
        self._cataloged_projects: set = set()

        self._task: Optional[asyncio.Task] = None
        self._side_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._current_job_id: Optional[str] = None

    # --------------------------------------------------------------------- #
    # Single-pass processing (testable seam)
    # --------------------------------------------------------------------- #
    async def process_once(self) -> int:
        """Dequeue and process one batch of jobs.

        Returns the number of jobs that were dequeued in this pass. Returns 0
        when the queue is empty. Jobs in the batch are processed concurrently,
        bounded by the configured semaphore.
        """
        jobs = self._queue.dequeue(limit=self._batch_size)
        if not jobs:
            return 0

        await asyncio.gather(*(self._process_job(job) for job in jobs))
        return len(jobs)

    async def _process_job(self, job: WriteJob) -> None:
        """Process a single job: extract/persist, catalog, mark done/failed.

        On failure (including the LLM/memory backend being unavailable) the job
        is retried — re-queued with an incremented ``attempts`` count — until the
        configured ceiling is reached, at which point it is marked terminally
        ``failed``. Either way the job is never lost: it stays in the table.

        A per-job wall-clock timeout marks the job ``failed`` so a hung LLM call
        cannot leave the queue silently locked; late thread completion cannot
        overwrite that failure (``mark_done_if_processing``).
        """
        job_token = job_id_var.set(job.id)
        self._current_job_id = job.id
        try:
            async with self._semaphore:
                try:
                    if self._job_timeout_sec > 0:
                        await asyncio.wait_for(
                            self._process_job_body(job),
                            timeout=self._job_timeout_sec,
                        )
                    else:
                        await self._process_job_body(job)
                except asyncio.TimeoutError:
                    WRITE_WORKER_ERRORS.inc()
                    msg = JOB_TIMEOUT_ERROR_FMT.format(
                        timeout=self._job_timeout_sec
                    )
                    logger.error(
                        "write job timed out job_id=%s project=%s after %.0fs",
                        job.id,
                        job.project,
                        self._job_timeout_sec,
                    )
                    try:
                        self._queue.mark_failed(
                            job.id, msg, attempts=job.attempts + 1
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "could not mark timed-out job %s as failed", job.id
                        )
                except Exception as e:  # noqa: BLE001 - background isolation
                    WRITE_WORKER_ERRORS.inc()
                    self._handle_failure(job, e)
        finally:
            if self._current_job_id == job.id:
                self._current_job_id = None
            job_id_var.reset(job_token)

    async def _process_job_body(self, job: WriteJob) -> None:
        """Inner job work (timeout-wrapped by :meth:`_process_job`)."""
        # Defensivo: jobs enfileirados antes da normalizacao na entrada podem
        # trazer ``project`` com espaco interno, que o SDK rejeita (ValueError)
        # e faria o job falhar apos todas as tentativas. Normaliza a chave em
        # memoria (a linha da fila nao e reescrita) para que add, catalogo,
        # invalidacao de cache e atribuicao usem a mesma chave efetiva.
        effective_project = normalize_project(job.project)
        if effective_project and effective_project != job.project:
            logger.info(
                "write job project normalized job_id=%s project=%r -> %r",
                job.id,
                job.project,
                effective_project,
            )
            job.project = effective_project
        client = self._client_provider()
        if client is None:
            raise RuntimeError("memory client unavailable (LLM/backend down)")

        result = await self._run_add_with_extraction_policy(client, job)
        if _persisted_result_count(result) == 0:
            self._queue.mark_skipped(job.id, SKIPPED_NO_MEMORIES_REASON)
            WRITE_WORKER_SUCCESS.inc()
            return
        self._maybe_dual_write(client, result)
        self._apply_supersedes(client, job, result)
        await self._auto_supersede_duplicates(client, job, result)
        self._catalog_project(job)
        read_cache.invalidate_search(job.project)
        # Prefer conditional done: a concurrent timeout/stall watchdog may have
        # already marked the job failed while the thread was still finishing.
        if hasattr(self._queue, "mark_done_if_processing"):
            if not self._queue.mark_done_if_processing(job.id):
                logger.warning(
                    "write job finished after terminal status; not marking done "
                    "job_id=%s",
                    job.id,
                )
                return
        else:
            self._queue.mark_done(job.id)
        WRITE_WORKER_SUCCESS.inc()

    def _handle_failure(self, job: WriteJob, error: Exception) -> None:
        """Re-queue the job for another attempt, or fail it terminally.

        Bounded retentativa (ADR-004): a transient failure (e.g. the LLM being
        momentarily down) re-queues the job so a later pass reprocesses it; once
        ``max_attempts`` is reached the job is marked ``failed`` and stops being
        retried. A failure to record the transition is swallowed so the worker
        loop never dies.
        """
        attempts = job.attempts + 1
        will_retry = attempts < self._max_attempts
        logger.warning(
            "write job attempt %s/%s failed job_id=%s project=%s hostname=%s "
            "(%s): %s",
            attempts,
            self._max_attempts,
            job.id,
            job.project,
            job.hostname,
            "will retry" if will_retry else "giving up",
            error,
        )
        try:
            if will_retry:
                self._queue.requeue(job.id, str(error), attempts)
            else:
                self._queue.mark_failed(job.id, str(error), attempts)
        except Exception:  # noqa: BLE001
            logger.exception(
                "could not record failure transition for job %s", job.id
            )

    async def _run_add(self, client, job: WriteJob, *, infer: bool = True):
        """Invoke the mem0 client ``add``.

        ``user_id`` carries the hostname for attribution only (ADR-003); the
        project scope travels in ``project`` + ``metadata`` so reads filter by
        project. Supports both async and sync clients (the OpenMemory default
        client is sync, so it is run in a thread to avoid blocking the loop).
        """
        metadata = {
            "project": job.project,
            "hostname": job.hostname,
            "source_app": "openmemory",
            "mcp_client": job.client_name,
            "state": "active",
        }
        extras = job.extras if isinstance(job.extras, dict) else {}
        supersedes = extras.get("supersedes") or []
        if isinstance(supersedes, list) and supersedes:
            metadata["supersedes"] = [str(x) for x in supersedes if x]

        # Procedencia (app.utils.scope_keys): a extracao e fan-out, um texto vira
        # N fatos atomicos com ids independentes. O id do job ja e unico por
        # submissao, entao serve de agrupador - superar a decisao passa a poder
        # alcancar todos os fatos que nasceram dela.
        if job.id:
            metadata["ingest_id"] = str(job.id)
        task_key = resolve_task(extras.get("task"), job.text)
        if task_key:
            metadata["task"] = task_key
        kwargs = dict(
            user_id=job.hostname,
            project=job.project,
            infer=infer,
            metadata=metadata,
        )
        # Writes target the active collection (blue-green, ADR-003).
        bind_active_collection(client)
        add = client.add
        # Atribuição de consumo de tokens (task_06): as chamadas LLM/embedding
        # internas ao add() são registradas contra este job. Contextvars
        # propagam por asyncio.to_thread (o contexto é copiado para a thread).
        attribution = usage_attribution(
            project=job.project,
            agent=job.client_name or "write-worker",
            user_id=job.hostname,
            operation_type="add",
        )
        with attribution:
            if inspect.iscoroutinefunction(add):
                return await add(job.text, **kwargs)

            # Call it; if the client returned a coroutine/awaitable (e.g. an
            # async client wrapped so ``iscoroutinefunction`` does not detect
            # it), await the result. Otherwise it is a sync client (the
            # OpenMemory default) — offload to a thread so the LLM call does
            # not block the event loop. We probe sync-ness with
            # iscoroutinefunction first to avoid running a blocking sync
            # ``add`` on the event loop thread.
            result = await asyncio.to_thread(add, job.text, **kwargs)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                return await result
            return result

    def _maybe_dual_write(self, client, result) -> None:
        """Mirror the just-written delta to the migration target (task_05 / ADR-003).

        During the blue-green window (``dual_write_enabled``), the points produced
        by ``add`` are replicated by id to the target collection so it stays fresh.
        Replication is best-effort: any failure is counted and logged but never
        fails the job — the active-collection write already succeeded.
        """
        target = partition_resolver.dual_write_target()
        if not target:
            return

        vs = getattr(client, "vector_store", None)
        if vs is None or not hasattr(vs, "replicate_to"):
            return

        add_ids, del_ids = [], []
        for r in (result or {}).get("results", []) or []:
            mid = r.get("id")
            if not mid:
                continue
            if (r.get("event") or "").upper() == "DELETE":
                del_ids.append(mid)
            else:
                add_ids.append(mid)

        try:
            vs.replicate_to(add_ids, target)
            vs.delete_from(del_ids, target)
        except Exception as e:  # noqa: BLE001 - never fail the job on mirror error
            DUAL_WRITE_ERRORS.inc()
            logger.warning("dual-write to %s failed: %s", target, e)

    def _apply_supersedes(self, client, job: WriteJob, result) -> None:
        """Mark superseded IDs obsolete after a successful write (best-effort)."""
        extras = job.extras if isinstance(job.extras, dict) else {}
        supersedes = extras.get("supersedes") or []
        if not isinstance(supersedes, list) or not supersedes:
            return
        new_ids = [
            r.get("id")
            for r in (result or {}).get("results", []) or []
            if isinstance(r, dict)
            and r.get("id")
            and (r.get("event") or "ADD").upper() != "DELETE"
        ]
        superseded_by = new_ids[0] if new_ids else None
        try:
            from app.utils.supersedes import mark_points_obsolete

            # Vizinhos ativos ANTES de marcar: depois o filtro de estado os
            # esconderia. Sao so registrados - marcar em silencio uma memoria
            # que o chamador nao citou seria decidir por ele.
            candidates = []
            try:
                from app.utils.supersede_fanout import find_sibling_candidates

                candidates = find_sibling_candidates(
                    client, supersedes, exclude_ids=new_ids
                )
            except Exception:  # noqa: BLE001
                logger.exception("sibling lookup failed job_id=%s", job.id)

            out = mark_points_obsolete(
                client, supersedes, superseded_by=superseded_by
            )
            logger.info(
                "supersedes applied job_id=%s project=%s updated=%s missing=%s",
                job.id,
                job.project,
                out.get("updated"),
                out.get("missing"),
            )
            for c in candidates:
                logger.warning(
                    "supersede sibling STILL ACTIVE job_id=%s superseded=%s "
                    "candidate=%s score=%.4f project=%s text=%r",
                    job.id,
                    c["superseded_id"],
                    c["candidate_id"],
                    c["score"],
                    c.get("project"),
                    (c.get("candidate_text") or "")[:160],
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "supersedes apply failed job_id=%s project=%s", job.id, job.project
            )

    async def _auto_supersede_duplicates(self, client, job: WriteJob, result) -> None:
        """Supersede existing memories that duplicate what was just written.

        Complements ``_apply_supersedes``, which only handles IDs the caller knew
        about. Off unless MEM0_AUTODEDUP_MODE is set; see app.utils.autodedup.
        The lookup is synchronous and may perform one slow embedding request per
        extracted memory, so keep it off the event loop. Otherwise the heartbeat
        side task is starved and the API incorrectly reports a dead worker while
        the job is still making progress.
        """
        try:
            from app.utils.autodedup import autodedup_after_write

            await asyncio.to_thread(
                autodedup_after_write,
                client,
                result,
                project=job.project,
                job_id=job.id,
            )
        except Exception:  # noqa: BLE001 - dedup must never fail a write
            logger.exception(
                "autodedup failed job_id=%s project=%s", job.id, job.project
            )

    def _catalog_project(self, job: WriteJob) -> None:
        """Upsert the project catalog on the first write of each project."""
        if job.project in self._cataloged_projects:
            return
        self._upsert_project(job.project, job.hostname)
        self._cataloged_projects.add(job.project)

    # --------------------------------------------------------------------- #
    # Long-running loop + lifecycle
    # --------------------------------------------------------------------- #

    def _recover_failed_jobs(self) -> None:
        """Re-queue ``failed`` jobs that exceeded the cooldown."""
        minutes = self._failed_retry_after_minutes
        if minutes <= 0:
            return
        try:
            recovered = self._queue.recover_failed_jobs(minutes)
        except Exception:  # noqa: BLE001
            logger.exception("failed-job recovery pass errored; continuing")
            return
        if recovered:
            logger.info(
                "recovered %s failed jobs -> queued (cooldown=%sm)",
                recovered,
                minutes,
            )

    async def run(self) -> None:
        """Run the consume loop until :meth:`stop` is requested."""
        await self._startup_recovery()
        self._recover_failed_jobs()
        self._log_llm_startup_status()
        logger.info(
            "write worker started (max_concurrency=%s, batch_size=%s, "
            "job_timeout_sec=%s). Overlapping Phase-2 still requires the LLM "
            "server to accept parallel slots (e.g. llama.cpp --parallel N); "
            "otherwise drain stays ~serial despite concurrency>1.",
            self._max_concurrency,
            self._batch_size,
            self._job_timeout_sec,
        )
        # Heartbeat + recovery run on a side task so a hung process_once
        # (blocked on a job) cannot silence health signals forever.
        _heartbeat_beat({"phase": "started", "job_id": None})
        # NOTE: the stop event is (re)cleared in start() *before* the task is
        # scheduled — never here. Clearing it inside run() races with stop():
        # if stop() runs before this coroutine body executes, it would wipe the
        # stop signal and the loop would never terminate (the task hangs).
        while not self._stopped.is_set():
            try:
                WRITE_QUEUE_DEPTH.set(self._queue.depth())
                processed = await self.process_once()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("write worker pass failed; continuing")
                processed = 0
            if processed == 0:
                self._recover_failed_jobs()
                self._recover_stale_processing()
                # Idle: wait for the poll interval or an early stop signal.
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=self._idle_sleep
                    )
                except asyncio.TimeoutError:
                    pass
        logger.info("write worker stopped")

    async def _startup_recovery(self) -> None:
        delay = STARTUP_RETRY_INITIAL_SEC
        while not self._stopped.is_set():
            try:
                recovered = self._queue.recover_stale_processing()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "startup recovery failed (database unreachable?); "
                    "retrying in %.0fs",
                    delay,
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, STARTUP_RETRY_MAX_SEC)
                continue
            if recovered:
                logger.info("recovered %s stale processing jobs -> queued", recovered)
            return

    async def _side_loop(self) -> None:
        """Heartbeat + fail stuck processing even while a job is in flight."""
        interval = min(self._heartbeat_interval_sec, DEFAULT_SIDE_LOOP_INTERVAL_SEC)
        while not self._stopped.is_set():
            try:
                _heartbeat_beat(
                    {
                        "phase": "running",
                        "job_id": self._current_job_id,
                        "depth": self._queue.depth(),
                    }
                )
                self._fail_stale_processing_jobs()
            except Exception:  # noqa: BLE001
                logger.exception("write worker side loop errored; continuing")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _fail_stale_processing_jobs(self) -> None:
        """Mark processing jobs that exceeded the job timeout as failed.

        Complements asyncio.wait_for: if the event loop itself is wedged on a
        single job, this still runs on the side task. Minutes are derived from
        job_timeout so slow HW (30 min timeout) is not failed prematurely.
        """
        if self._job_timeout_sec <= 0:
            return
        # Slightly above the asyncio timeout so the wait_for path usually wins.
        minutes = max(1, int(self._job_timeout_sec / 60) + 5)
        error = JOB_TIMEOUT_ERROR_FMT.format(timeout=self._job_timeout_sec)
        try:
            failed = self._queue.fail_stale_processing(minutes, error)
        except Exception:  # noqa: BLE001
            logger.exception("fail-stale-processing pass errored; continuing")
            return
        if failed:
            logger.warning(
                "marked %s stale processing jobs as failed (age>=%sm)",
                failed,
                minutes,
            )

    def _recover_stale_processing(self) -> None:
        # With per-job timeout + side-loop fail_stale, idle requeue races a
        # still-running ``add()`` thread. Startup still recovers all processing.
        if self._job_timeout_sec > 0:
            return
        minutes = _env_int("WRITE_WORKER_STALE_PROCESSING_MINUTES", 15)
        if minutes <= 0:
            return
        try:
            recovered = self._queue.recover_stale_processing(
                older_than_minutes=minutes
            )
        except Exception:  # noqa: BLE001
            logger.exception("stale-processing recovery pass errored; continuing")
            return
        if recovered:
            logger.info(
                "recovered %s stale processing jobs -> queued (age>=%sm)",
                recovered,
                minutes,
            )

    async def _run_add_with_extraction_policy(self, client, job: WriteJob):
        """Run infer=True; retry once after client reset on infra errors.

        Falls back to infer=False only when extraction returned no facts without
        an LLM infrastructure failure (model down, 404, auth, etc.).
        """
        from app.utils.memory import reset_memory_client

        _clear_llm_extraction_error()
        result = await self._run_add(client, job, infer=True)
        if _persisted_result_count(result) > 0:
            return result

        llm_err = _last_llm_extraction_error()
        if llm_err and _is_llm_infrastructure_error(llm_err):
            logger.warning(
                "write job LLM infra error; resetting client and retrying infer=True "
                "job_id=%s project=%s error=%s",
                job.id,
                job.project,
                llm_err,
            )
            reset_memory_client()
            _clear_llm_extraction_error()
            client = self._client_provider()
            if client is None:
                raise RuntimeError(
                    f"{LLM_INFRA_ERROR_MSG}: memory client unavailable after reset"
                )
            result = await self._run_add(client, job, infer=True)
            if _persisted_result_count(result) > 0:
                return result
            llm_err = _last_llm_extraction_error() or llm_err
            raise RuntimeError(f"{LLM_INFRA_ERROR_MSG}: {llm_err}")

        logger.warning(
            "write job LLM extraction empty; falling back to infer=False "
            "job_id=%s project=%s",
            job.id,
            job.project,
        )
        return await self._run_add(client, job, infer=False)

    def _log_llm_startup_status(self) -> None:
        """Log configured model and probe LLM so misconfig is visible early."""
        try:
            from app.utils.memory import get_configured_llm_model, probe_llm_model

            model = get_configured_llm_model() or "(unset)"
            ok, detail = probe_llm_model()
            if ok:
                logger.info("write worker LLM ready | model=%s", detail or model)
            else:
                logger.error(
                    "write worker LLM probe FAILED | configured_model=%s error=%s "
                    "(jobs will retry; fix model/token in UI or load model on server)",
                    model,
                    detail,
                )
        except Exception:
            logger.exception("write worker LLM startup check failed")

    def start(self) -> asyncio.Task:
        """Schedule :meth:`run` as a background task on the running loop."""
        if self._task is None or self._task.done():
            # Reset the stop signal here, before scheduling the task, so a
            # stop() issued right after start() cannot be erased by run().
            self._stopped.clear()
            self._task = asyncio.create_task(self.run())
            self._side_task = asyncio.create_task(self._side_loop())
        return self._task

    async def stop(self) -> None:
        """Signal the loop to stop and await the background task."""
        self._stopped.set()
        for task in (self._task, self._side_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._side_task = None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def worker_from_env() -> WriteWorker:
    """Build a :class:`WriteWorker` from environment variables."""
    return WriteWorker(
        max_concurrency=_env_int("WRITE_WORKER_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY),
        batch_size=_env_int("WRITE_WORKER_BATCH_SIZE", DEFAULT_BATCH_SIZE),
        idle_sleep=_env_float("WRITE_WORKER_IDLE_SLEEP", DEFAULT_IDLE_SLEEP),
        max_attempts=_env_int("WRITE_WORKER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        job_timeout_sec=_env_float(
            "WRITE_WORKER_JOB_TIMEOUT_SEC", DEFAULT_JOB_TIMEOUT_SEC
        ),
        heartbeat_interval_sec=_env_float(
            "WRITE_WORKER_HEARTBEAT_INTERVAL_SEC", DEFAULT_HEARTBEAT_INTERVAL_SEC
        ),
    )


def embedded_worker_enabled() -> bool:
    """Whether the API process should run the write worker (dev/single-host)."""
    return os.environ.get("RUN_EMBEDDED_WORKER", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Shared worker instance used by the application startup hook.
write_worker = worker_from_env()
