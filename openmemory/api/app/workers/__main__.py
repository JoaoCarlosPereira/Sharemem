"""Standalone entrypoint for the write worker process.

Usage::

    python -m app.workers.write_worker

Runs the queue consumer as an independent process (ADR-003), sharing the same
``DATABASE_URL`` as the API. Handles SIGINT/SIGTERM for graceful shutdown.
"""

import asyncio
import logging
import signal
import sys

from app.workers.write_worker import worker_from_env

logger = logging.getLogger(__name__)


async def _run() -> int:
    worker = worker_from_env()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    run_task = worker.start()
    stop_task = asyncio.create_task(stop_event.wait())
    await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if not stop_event.is_set():
        stop_task.cancel()
        error = None if run_task.cancelled() else run_task.exception()
        logger.critical(
            "write worker loop exited without a shutdown signal; exiting so "
            "the container restarts",
            exc_info=error,
        )
        exit_code = 1

    try:
        await worker.stop()
    except Exception:  # noqa: BLE001
        if exit_code == 0:
            logger.exception("write worker stopped with an error")
            exit_code = 1
    return exit_code


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
