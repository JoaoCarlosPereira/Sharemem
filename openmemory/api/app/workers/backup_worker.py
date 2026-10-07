"""Worker de backup dedicado — agendamento autônomo (task_05 / ADR-002).

Loop asyncio independente da governança: a cada intervalo lê a ``BackupPolicy``,
decide se há backup devido (cadência × último backup × ``run_at``/timezone) e
dispara ``BackupArchive.create()``. Mantido propositalmente simples (sem fila
multi-job). Executável como módulo::

    python -m app.workers.backup_worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from app.database import SessionLocal
from app.utils.backup import BackupService
from app.utils.backup_archive import _REGULAR_RE, BackupArchive
from app.utils.backup_policy import get_backup_policy_runtime
from app.utils.logging_context import configure_process_logging

logger = logging.getLogger(__name__)

DEFAULT_SLEEP = 300.0  # segundos entre verificações de agenda
_INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


class BackupWorker:
    def __init__(
        self,
        build: Callable[[], BackupArchive],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_sleep: float = DEFAULT_SLEEP,
    ):
        self._build = build
        self._clock = clock
        self._sleep = interval_sleep
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- decisão de agenda -------------------------------------------------
    def due(self, archive: BackupArchive) -> bool:
        """Whether a scheduled backup is due now, given policy and last backup."""
        policy = archive.policy
        if not policy.enabled:
            return False
        now = self._clock()
        now_local = now.astimezone(ZoneInfo(policy.timezone))
        hh, mm = (int(x) for x in policy.run_at.split(":"))
        run_today = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_local < run_today:
            return False  # ainda não chegou o horário de hoje
        last = self._last_success(archive)
        if last is None:
            return True
        interval = _INTERVALS.get(policy.frequency, _INTERVALS["daily"])
        return (now - last) >= interval

    def _last_success(self, archive: BackupArchive) -> Optional[datetime]:
        """created_at do backup regular mais recente (ignora ``pre-restore`` etc.)."""
        stamps = [
            i.created_at
            for i in archive.list()
            if i.location == "local" and _REGULAR_RE.match(i.name) and i.created_at
        ]
        return max(stamps) if stamps else None

    # -- ciclo -------------------------------------------------------------
    async def tick(self) -> bool:
        """Uma passada do agendador; retorna True se um backup foi executado."""
        try:
            archive = self._build()
            if not self.due(archive):
                return False
            result = archive.create()
            logger.info("scheduled backup created: %s", getattr(result, "name", "?"))
            return True
        except Exception:  # noqa: BLE001 — métrica BACKUP_ERRORS_TOTAL é incrementada em create()
            logger.exception("scheduled backup tick failed")
            return False

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.tick()
            await self._wait(self._sleep)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def start(self) -> asyncio.Task:
        self._stopped.clear()
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def _build_archive() -> BackupArchive:
    db = SessionLocal()
    try:
        policy = get_backup_policy_runtime(db)
    finally:
        db.close()
    return BackupArchive(BackupService(), policy)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def worker_from_env() -> BackupWorker:
    return BackupWorker(
        _build_archive,
        interval_sleep=_env_float("BACKUP_WORKER_SLEEP", DEFAULT_SLEEP),
    )


async def _main() -> None:
    worker = worker_from_env()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    worker.start()
    await stop_event.wait()
    await worker.stop()


def main() -> None:
    configure_process_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
