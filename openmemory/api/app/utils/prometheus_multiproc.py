"""Suporte opt-in ao modo multiprocesso do ``prometheus_client``.

Sem ``PROMETHEUS_MULTIPROC_DIR`` o comportamento é o histórico: cada processo
usa o registry padrão em memória e ``/metrics`` devolve ``generate_latest()``.

Com a variável definida (antes de o processo iniciar — o ``prometheus_client``
escolhe o backend de valores no import), cada processo grava seus valores em
arquivos mmap ``<tipo>_<pid>.db`` nesse diretório e ``/metrics`` agrega todos os
arquivos via :class:`prometheus_client.multiprocess.MultiProcessCollector`.
Assim workers do uvicorn (``--workers N``) ou processos auxiliares que
compartilhem o diretório aparecem somados no mesmo scrape.

Particularidades tratadas aqui:

* ``mark_current_process_dead`` remove os arquivos de Gauges ``live*`` do
  processo que encerra: via ``atexit`` (registrado no import de
  ``app.utils.metrics``; cobre ``python -m``, alembic e workers ``spawn`` do
  uvicorn) e, por redundância, no shutdown do FastAPI. Processos mortos por
  SIGKILL/OOM não rodam nenhum dos dois — por isso o diretório também é limpo
  no start do container. Não há gancho ``child_exit`` como no gunicorn: o
  supervisor do uvicorn não expõe callback de morte de worker.
* A limpeza do diretório no start do container fica em
  ``docker-entrypoint.sh`` (antes de qualquer processo Python criar arquivos).
* Métricas de plataforma/processo (``process_*``, ``python_*``) não existem no
  modo multiprocesso — limitação documentada do ``prometheus_client``.
"""

from __future__ import annotations

import atexit
import os

ENV_VAR = "PROMETHEUS_MULTIPROC_DIR"
_LEGACY_ENV_VAR = "prometheus_multiproc_dir"

_exit_hook_installed = False


def multiproc_dir() -> str | None:
    """Diretório multiprocesso configurado, ou ``None`` (modo single-process)."""
    value = os.environ.get(ENV_VAR) or os.environ.get(_LEGACY_ENV_VAR)
    return value or None


def multiprocess_enabled() -> bool:
    return multiproc_dir() is not None


def generate_metrics_payload() -> bytes:
    """Exposição Prometheus: agregada entre processos quando habilitado."""
    from prometheus_client import CollectorRegistry, generate_latest

    path = multiproc_dir()
    if path is None:
        return generate_latest()

    from prometheus_client import multiprocess

    # Registry novo por scrape (padrão recomendado): o MultiProcessCollector
    # lê os arquivos de todos os PIDs; não registrar no REGISTRY global, senão
    # os valores do processo atual seriam expostos em duplicidade.
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=path)
    return generate_latest(registry)


def mark_current_process_dead() -> None:
    """Remove os arquivos de Gauges ``live*`` deste PID (no-op sem multiproc)."""
    path = multiproc_dir()
    if path is None or not os.path.isdir(path):
        return
    from prometheus_client import multiprocess

    try:
        multiprocess.mark_process_dead(os.getpid(), path)
    except OSError:
        # Encerramento não pode falhar por causa de métrica.
        pass


def install_exit_hook() -> None:
    """Registra ``mark_current_process_dead`` no ``atexit`` (uma vez por processo)."""
    global _exit_hook_installed
    if _exit_hook_installed or not multiprocess_enabled():
        return
    atexit.register(mark_current_process_dead)
    _exit_hook_installed = True
