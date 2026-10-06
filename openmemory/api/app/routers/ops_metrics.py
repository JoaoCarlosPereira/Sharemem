"""Prometheus metrics endpoint (scale architecture observability)."""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST

# Import registers custom collectors with the default registry before scrape.
from app.utils import metrics as _metrics  # noqa: F401
from app.utils.prometheus_multiproc import generate_metrics_payload

router = APIRouter(tags=["operations"])


@router.get("/metrics")
async def metrics():
    """Expose Prometheus metrics.

    Com ``PROMETHEUS_MULTIPROC_DIR`` definido, agrega os valores de todos os
    processos que gravam no diretório (``MultiProcessCollector``); sem a
    variável, devolve o registry padrão deste processo (comportamento atual).
    """
    return Response(content=generate_metrics_payload(), media_type=CONTENT_TYPE_LATEST)
