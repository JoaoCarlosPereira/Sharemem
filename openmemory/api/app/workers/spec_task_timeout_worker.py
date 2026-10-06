"""Worker de liberação automática de tasks travadas por timeout (Tarefa 5 / ADR-007).

Uma ``TaskCard`` em ``em_andamento`` cujo ``last_activity_at`` ultrapassou o
limite configurável (padrão 24h) é liberada de volta para a coluna ``tasks``
(não atribuída), preservando o histórico de quem tentou. Resolve o caso de
agentes que "somem" sem atualizar o status.

Estrutura de classe (``run()``/``start()``/``stop()``, loop assíncrono) espelha
``GovernanceWorker`` e ``WriteWorker``, MAS a **inicialização** segue o
``write_worker``: é iniciado em ``@app.on_event("startup")`` de ``main.py`` — não
como serviço Docker próprio. A liberação usa ``release_task`` com
``expected_version`` (concorrência otimista): rodando no startup de cada réplica,
apenas uma consegue liberar cada task — sem liberação dupla.

Configuração sem deploy de código via variáveis de ambiente
(``SPEC_TASK_TIMEOUT_HOURS``, ``SPEC_TASK_TIMEOUT_POLL_SECONDS``), no mesmo
padrão de ``worker_from_env`` do ``write_worker``.
"""

import asyncio
import logging
import os
from datetime import timedelta
from typing import Callable, Optional

from app.database import SessionLocal
from app.models import TaskCard, TaskCardStatus, get_current_utc_time
from app.utils.claim_lease import DEFAULT_TIMEOUT_HOURS as _DEFAULT_TIMEOUT_HOURS
from app.utils.claim_lease import TIMEOUT_ACTOR as _TIMEOUT_ACTOR
from app.utils.claim_lease import claim_timeout_hours
from app.utils.task_lock import release_task as _default_release_task

logger = logging.getLogger(__name__)

# Limite padrão de inatividade antes da liberação automática (ADR-007) e cadência
# de varredura. Ambos ajustáveis por env var, sem novo deploy de código.
# O limite vem de ``app.utils.claim_lease``, que é a mesma fonte usada para
# responder ``claim_expires_at`` ao cliente — duas cópias divergiriam no primeiro
# ajuste de env, e o prazo informado deixaria de bater com o prazo aplicado.
DEFAULT_TIMEOUT_HOURS = _DEFAULT_TIMEOUT_HOURS
DEFAULT_POLL_SECONDS = 3600.0

# Ator gravado em TaskStatusHistory.changed_by — distingue automação de release
# manual (que usa o hostname/e-mail do ator humano). Definido em claim_lease para
# que o router reconheça o sentinela sem importar a camada de workers; reexportado
# aqui porque testes e chamadores existentes o referenciam por este módulo.
TIMEOUT_ACTOR = _TIMEOUT_ACTOR


class SpecTaskTimeoutWorker:
    """Libera periodicamente tasks ``em_andamento`` inativas além do limite.

    Args:
        timeout_hours: Janela de inatividade (em horas) antes de liberar.
        poll_seconds: Intervalo entre varreduras no loop de longa duração.
        session_factory: Fábrica de ``Session`` (injetável para testes).
        release: Callable de liberação (injetável); por padrão ``release_task``.
    """

    def __init__(
        self,
        timeout_hours: float = DEFAULT_TIMEOUT_HOURS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        session_factory: Optional[Callable] = None,
        release: Optional[Callable] = None,
    ):
        self._timeout = timedelta(hours=max(0.0, float(timeout_hours)))
        self._poll = max(1.0, float(poll_seconds))
        self._session_factory = session_factory or SessionLocal
        self._release = release or _default_release_task

        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    # --------------------------------------------------------------------- #
    # Seleção de elegíveis + passe único (seam testável)
    # --------------------------------------------------------------------- #
    def eligible_tasks(self, db, now=None):
        """Tasks ``em_andamento`` com ``last_activity_at`` além do limite."""
        now = now or get_current_utc_time()
        cutoff = now - self._timeout
        return (
            db.query(TaskCard)
            .filter(
                TaskCard.status == TaskCardStatus.em_andamento,
                # Arquivado está fora do fluxo: não há lease a expirar.
                TaskCard.archived_at.is_(None),
                TaskCard.last_activity_at.isnot(None),
                TaskCard.last_activity_at < cutoff,
            )
            .all()
        )

    def process_once(self) -> int:
        """Uma varredura: libera as tasks elegíveis. Retorna quantas liberou."""
        db = self._session_factory()
        try:
            released = 0
            for task in self.eligible_tasks(db):
                try:
                    result = self._release(
                        db,
                        task.id,
                        TIMEOUT_ACTOR,
                        reason="liberação automática por timeout de inatividade",
                        expected_version=task.version,
                    )
                    if result.claimed:
                        released += 1
                except Exception:  # noqa: BLE001 - uma falha não aborta o lote
                    logger.exception(
                        "spec-task-timeout: falha ao liberar task %s; continuando",
                        task.id,
                    )
            if released:
                logger.info(
                    "spec-task-timeout: %s task(s) liberada(s) por inatividade", released
                )
            return released
        except Exception:  # noqa: BLE001 - isolamento do worker de background
            logger.exception("spec-task-timeout: passe falhou; continuando")
            return 0
        finally:
            db.close()

    # --------------------------------------------------------------------- #
    # Loop de longa duração + ciclo de vida (espelha write_worker)
    # --------------------------------------------------------------------- #
    async def run(self) -> None:
        logger.info(
            "spec task timeout worker started (timeout=%.1fh, poll=%.0fs)",
            self._timeout.total_seconds() / 3600,
            self._poll,
        )
        while not self._stopped.is_set():
            self.process_once()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
        logger.info("spec task timeout worker stopped")

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
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
            finally:
                self._task = None


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def worker_from_env() -> SpecTaskTimeoutWorker:
    """Constrói o worker a partir de variáveis de ambiente (ajuste sem deploy)."""
    return SpecTaskTimeoutWorker(
        timeout_hours=claim_timeout_hours(),
        poll_seconds=_env_float("SPEC_TASK_TIMEOUT_POLL_SECONDS", DEFAULT_POLL_SECONDS),
    )


# Instância compartilhada usada pelo hook de startup da aplicação.
spec_task_timeout_worker = worker_from_env()
