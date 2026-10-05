"""Persistência e agregação do relatório do autodedup (``MEM0_AUTODEDUP_MODE=report``).

O modo ``report`` só observa: nada aqui altera o Qdrant. Cada par candidato
(nova memória x memória existente parecida) vira uma linha em
``autodedup_reports`` para que o limiar possa ser calibrado com tráfego real via
``GET /admin/autodedup/report`` — antes o relatório só existia no ``logger.info``
do write-worker.

Garantias:

* **Best-effort**: ``record_report_candidates`` nunca levanta; uma falha de banco
  vira ``logger.warning`` e o job de escrita segue intacto.
* **Privacidade/tamanho**: só trechos curtos dos textos são gravados
  (``MEM0_AUTODEDUP_REPORT_TEXT_CHARS``, default 160; ``0`` não grava texto). O
  endpoint é somente leitura e exige admin.
* **Retenção** (só nesta tabela): linhas com mais de
  ``MEM0_AUTODEDUP_REPORT_RETENTION_DAYS`` (default 30) dias e o excedente acima
  de ``MEM0_AUTODEDUP_REPORT_MAX_ROWS`` (default 50000, as mais antigas primeiro)
  são removidos após uma gravação, no máximo uma vez a cada
  ``_PRUNE_INTERVAL_SEC`` por processo. ``0`` desliga o respectivo limite.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TEXT_CHARS = 160
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_ROWS = 50000
_PRUNE_INTERVAL_SEC = 300.0
_PRUNE_BATCH = 1000

# Faixas do histograma e limiares candidatos: 0.85, 0.86, ..., 0.99.
HISTOGRAM_START = 0.85
THRESHOLD_STEPS = tuple(round(HISTOGRAM_START + i / 100, 2) for i in range(15))

_prune_lock = threading.Lock()
_last_prune_monotonic: Optional[float] = None


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def report_text_chars() -> int:
    return _env_int("MEM0_AUTODEDUP_REPORT_TEXT_CHARS", DEFAULT_TEXT_CHARS)


def report_retention_days() -> int:
    return _env_int("MEM0_AUTODEDUP_REPORT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def report_max_rows() -> int:
    return _env_int("MEM0_AUTODEDUP_REPORT_MAX_ROWS", DEFAULT_MAX_ROWS)


def _excerpt(text: Any, limit: int) -> Optional[str]:
    if limit <= 0 or not text:
        return None
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _utcnow() -> datetime.datetime:
    # Naive UTC, como as demais colunas DateTime do schema.
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _default_session_factory():
    """Fábrica de sessão usada quando o chamador não passa uma.

    Resolvida a cada chamada (não no import) e isolada num ponto só para que a
    suíte de testes possa redirecioná-la (ver ``tests/conftest.py``) — sem isso,
    qualquer teste que acione o modo report gravaria no banco real
    (``DATABASE_URL``/``./openmemory.db``) e rodaria a retenção (DELETE) nele.
    """
    from app.database import SessionLocal

    return SessionLocal


def record_report_candidates(
    candidates: Iterable[dict],
    *,
    threshold: float,
    project: str = "",
    job_id: str = "",
    session_factory=None,
) -> int:
    """Grava os pares do modo report. Retorna quantas linhas foram gravadas.

    Nunca levanta: o relatório é diagnóstico e não pode falhar a escrita.
    """
    rows = list(candidates or [])
    if not rows:
        return 0
    try:
        from app.models import AutodedupReport

        if session_factory is None:
            session_factory = _default_session_factory()

        limit = report_text_chars()
        now = _utcnow()
        db = session_factory()
        try:
            for c in rows:
                score = float(c["score"])
                db.add(
                    AutodedupReport(
                        created_at=now,
                        job_id=str(job_id) if job_id else None,
                        project=project or None,
                        new_memory_id=str(c["new_id"]),
                        duplicate_memory_id=str(c["duplicate_id"]),
                        duplicate_project=c.get("duplicate_project") or None,
                        score=score,
                        threshold=float(threshold),
                        above_threshold=score >= threshold,
                        new_text=_excerpt(c.get("new_text"), limit),
                        duplicate_text=_excerpt(c.get("duplicate_text"), limit),
                    )
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - nunca falhar a escrita por diagnóstico
        logger.warning(
            "autodedup report persist failed job_id=%s project=%s: %s", job_id, project, exc
        )
        return 0

    maybe_prune(session_factory=session_factory)
    return len(rows)


def prune_reports(
    db,
    *,
    retention_days: Optional[int] = None,
    max_rows: Optional[int] = None,
    now: Optional[datetime.datetime] = None,
) -> int:
    """Aplica a retenção em ``autodedup_reports`` (e SOMENTE nela). Retorna removidas."""
    from app.models import AutodedupReport

    retention_days = report_retention_days() if retention_days is None else retention_days
    max_rows = report_max_rows() if max_rows is None else max_rows
    now = now or _utcnow()
    removed = 0

    if retention_days > 0:
        cutoff = now - datetime.timedelta(days=retention_days)
        removed += (
            db.query(AutodedupReport)
            .filter(AutodedupReport.created_at < cutoff)
            .delete(synchronize_session=False)
        )

    if max_rows > 0:
        # Excedente acima do teto, mais antigos primeiro; apagado em lotes por id.
        excess_ids = [
            r.id
            for r in db.query(AutodedupReport.id)
            .order_by(AutodedupReport.created_at.desc(), AutodedupReport.id.desc())
            .offset(max_rows)
            .all()
        ]
        for i in range(0, len(excess_ids), _PRUNE_BATCH):
            batch = excess_ids[i : i + _PRUNE_BATCH]
            removed += (
                db.query(AutodedupReport)
                .filter(AutodedupReport.id.in_(batch))
                .delete(synchronize_session=False)
            )

    db.commit()
    return removed


def maybe_prune(*, session_factory=None, force: bool = False) -> int:
    """Retenção com throttle por processo; best-effort."""
    global _last_prune_monotonic
    with _prune_lock:
        nowm = time.monotonic()
        if (
            not force
            and _last_prune_monotonic is not None
            and nowm - _last_prune_monotonic < _PRUNE_INTERVAL_SEC
        ):
            return 0
        _last_prune_monotonic = nowm
    try:
        if session_factory is None:
            session_factory = _default_session_factory()
        db = session_factory()
        try:
            return prune_reports(db)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("autodedup report prune failed: %s", exc)
        return 0


# --------------------------------------------------------------------------- #
# Agregação
#
# Toda contagem é "quantos pares têm ``score >= t``" com comparação EXATA, a
# mesma do ``apply`` (``score >= threshold`` em autodedup.find_near_duplicates).
# Não há tolerância: um score float32 que o Qdrant devolve como 0.94999998 NÃO
# seria supersedido com limiar 0.95, então também não conta no limiar 0.95 nem
# na faixa [0.95, 0.96) do histograma. As faixas do histograma são diferenças
# entre esses mesmos cortes, logo histograma e tabela de limiares nunca divergem.
# --------------------------------------------------------------------------- #
def _cuts(current_threshold: float) -> list[float]:
    """Limiares candidatos (grade + o vigente, sem arredondar) e o topo 1.0."""
    return sorted(set(THRESHOLD_STEPS) | {float(current_threshold), 1.0})


def _build_summary(
    total: int,
    counts: dict[float, tuple[int, int, int]],
    *,
    current_threshold: float,
    report_floor: Optional[float],
) -> dict:
    """Monta o resumo a partir de ``counts[t] = (pares, duplicatas, novas)``."""
    current = float(current_threshold)

    def _below_floor(t: float) -> bool:
        # Abaixo do piso o modo report não grava pares: a contagem é só um
        # mínimo (só aparecem pares gravados quando o piso era mais baixo).
        return report_floor is not None and t < report_floor

    histogram = []
    for i, lo in enumerate(THRESHOLD_STEPS):
        hi = THRESHOLD_STEPS[i + 1] if i + 1 < len(THRESHOLD_STEPS) else 1.0
        # A última faixa [0.99, 1.00] inclui score 1.0.
        upper = counts[hi][0] if hi < 1.0 else 0
        histogram.append(
            {
                "min": lo,
                "max": hi,
                "count": counts[lo][0] - upper,
                "below_report_floor": _below_floor(lo),
            }
        )

    thresholds = []
    for t in _cuts(current):
        if t == 1.0 and t != current:  # 1.0 só entra na grade como topo do histograma
            continue
        pairs, dups, news = counts[t]
        thresholds.append(
            {
                "threshold": t,
                "pairs": pairs,
                # O apply marcaria obsoletas as memórias existentes (duplicatas).
                "would_supersede": dups,
                "new_memories": news,
                "current": t == current,
                "below_report_floor": _below_floor(t),
            }
        )

    return {
        "total_pairs": total,
        "below_histogram": total - counts[HISTOGRAM_START][0],
        "report_floor": report_floor,
        "histogram": histogram,
        "thresholds": thresholds,
    }


def summarize(
    rows: list[tuple[float, str, str]],
    *,
    current_threshold: float,
    report_floor: Optional[float] = None,
) -> dict:
    """Histograma por faixa de 0.01 e efeito de cada limiar candidato (em memória).

    ``rows`` = ``[(score, new_memory_id, duplicate_memory_id), ...]``. O endpoint
    usa :func:`summarize_query` (mesmo resultado, agregado no banco).
    """
    counts = {}
    for t in _cuts(current_threshold):
        hit = [(n, d) for s, n, d in rows if s >= t]
        counts[t] = (len(hit), len({d for _n, d in hit}), len({n for n, _d in hit}))
    return _build_summary(
        len(rows), counts, current_threshold=current_threshold, report_floor=report_floor
    )


def summarize_query(
    query, *, current_threshold: float, report_floor: Optional[float] = None
) -> dict:
    """Mesmo resumo de :func:`summarize`, mas agregado em SQL numa única consulta.

    ``query`` é um ``Query(AutodedupReport)`` já filtrado. Nenhuma linha é
    carregada em memória — o custo não depende de retenção/teto da tabela.
    """
    from sqlalchemy import case, func

    from app.models import AutodedupReport

    score = AutodedupReport.score
    cuts = _cuts(current_threshold)
    cols = [func.count(AutodedupReport.id)]
    for t in cuts:
        hit = score >= t
        cols.append(func.coalesce(func.sum(case((hit, 1), else_=0)), 0))
        cols.append(func.count(func.distinct(case((hit, AutodedupReport.duplicate_memory_id)))))
        cols.append(func.count(func.distinct(case((hit, AutodedupReport.new_memory_id)))))
    row = query.with_entities(*cols).order_by(None).one()
    total = int(row[0] or 0)
    counts = {
        t: (int(row[1 + 3 * i]), int(row[2 + 3 * i]), int(row[3 + 3 * i]))
        for i, t in enumerate(cuts)
    }
    return _build_summary(
        total, counts, current_threshold=current_threshold, report_floor=report_floor
    )
