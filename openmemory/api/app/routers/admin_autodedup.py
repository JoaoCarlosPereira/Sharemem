"""Relatório do autodedup (somente leitura) para calibrar MEM0_AUTODEDUP_THRESHOLD.

``GET /admin/autodedup/report`` lê ``autodedup_reports`` (gravada pelo modo
``report`` do write-worker) e devolve os pares mais recentes + um resumo
agregado: histograma por faixa de 0.01 (0.85–1.00) e, para cada limiar candidato,
quantos pares/memórias existentes seriam supersedidos. Não altera nada.
"""

from __future__ import annotations

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AutodedupReport
from app.utils.admin_auth import require_admin
from app.utils.autodedup import autodedup_mode, autodedup_report_floor, autodedup_threshold
from app.utils.autodedup_report import (
    report_max_rows,
    report_retention_days,
    report_text_chars,
    summarize_query,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _naive_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(datetime.UTC).replace(tzinfo=None)


@router.get("/autodedup/report")
def autodedup_report(
    since: Optional[datetime.datetime] = Query(
        None, description="Só pares gravados a partir deste instante (ISO 8601; sem fuso = UTC)"
    ),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0, description="Score mínimo (>=)"),
    max_score: Optional[float] = Query(
        None, ge=0.0, le=1.0, description="Score máximo (<), ex.: faixa [min_score, max_score)"
    ),
    above_threshold: Optional[bool] = Query(
        None,
        description="true = só pares acima do limiar vigente na gravação; false = só quase-duplicatas",
    ),
    project: Optional[str] = Query(None, description="Projeto da nova memória"),
    limit: int = Query(100, ge=0, le=1000, description="Máximo de pares listados em items"),
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(AutodedupReport)
    since = _naive_utc(since)
    if since is not None:
        q = q.filter(AutodedupReport.created_at >= since)
    if min_score is not None:
        q = q.filter(AutodedupReport.score >= min_score)
    if max_score is not None:
        q = q.filter(AutodedupReport.score < max_score)
    if above_threshold is not None:
        q = q.filter(AutodedupReport.above_threshold.is_(above_threshold))
    if project:
        q = q.filter(AutodedupReport.project == project)

    # Agregação sobre TODAS as linhas filtradas (não só as listadas em items),
    # feita no banco: nenhuma linha é carregada, mesmo com MAX_ROWS/RETENTION=0.
    threshold = autodedup_threshold()
    floor = autodedup_report_floor(threshold)
    summary = summarize_query(q, current_threshold=threshold, report_floor=floor)

    items = []
    if limit:
        for r in (
            q.order_by(AutodedupReport.score.desc(), AutodedupReport.created_at.desc())
            .limit(limit)
            .all()
        ):
            items.append(
                {
                    "id": str(r.id),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "job_id": r.job_id,
                    "project": r.project,
                    "new_memory_id": r.new_memory_id,
                    "duplicate_memory_id": r.duplicate_memory_id,
                    "duplicate_project": r.duplicate_project,
                    "score": r.score,
                    "threshold": r.threshold,
                    "above_threshold": bool(r.above_threshold),
                    "new_text": r.new_text,
                    "duplicate_text": r.duplicate_text,
                }
            )

    return {
        "config": {
            "mode": autodedup_mode(),
            "threshold": threshold,
            "report_floor": floor,
            "retention_days": report_retention_days(),
            "max_rows": report_max_rows(),
            "text_chars": report_text_chars(),
        },
        "filters": {
            "since": since.isoformat() if since else None,
            "min_score": min_score,
            "max_score": max_score,
            "above_threshold": above_threshold,
            "project": project,
            "limit": limit,
        },
        "summary": summary,
        "items": items,
    }
