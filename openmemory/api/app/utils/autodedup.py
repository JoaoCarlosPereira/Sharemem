"""Automatic near-duplicate detection for freshly written memories.

The extraction prompt is ADD-only ("Your sole operation is ADD") and the dedup
context handed to the LLM is the top-10 neighbours of the WHOLE submitted text —
never of each extracted fact. A long submission therefore retrieves neighbours of
the document as a whole, individual facts are never compared against their own
neighbours, and paraphrases of an existing memory get stored again. The store
ends up with clusters saying the same thing, and every one of them competes for a
slot in the search page.

``supersedes`` already exists but is manual: the caller has to know the IDs. This
closes the loop by looking, per newly written memory, for an existing one that is
semantically the same and marking it obsolete.

Because this changes stored data, it is OFF by default and has a report mode:

  MEM0_AUTODEDUP_MODE      — off (default) | report | apply
  MEM0_AUTODEDUP_THRESHOLD — cosine similarity to treat as duplicate (default 0.95)
  MEM0_AUTODEDUP_TOP_K     — neighbours inspected per new memory (default 10)

``report`` logs what it would supersede and touches nothing — run it for a while
and read the logs before switching to ``apply``. Report mode also persists every
candidate in ``autodedup_reports`` (see app.utils.autodedup_report), queryable via
``GET /admin/autodedup/report``. To calibrate *below* the current threshold it
records "near misses" too:

  MEM0_AUTODEDUP_REPORT_FLOOR — lowest score recorded in report mode (default 0.85,
                                capped at the threshold). Ignored by ``apply``.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_REPORT = "report"
MODE_APPLY = "apply"


def autodedup_mode() -> str:
    mode = (os.getenv("MEM0_AUTODEDUP_MODE") or MODE_OFF).strip().lower()
    return mode if mode in (MODE_OFF, MODE_REPORT, MODE_APPLY) else MODE_OFF


DEFAULT_THRESHOLD = 0.95
DEFAULT_REPORT_FLOOR = 0.85


def _env_unit_float(name: str, default: float) -> float:
    """Float env var restricted to [0, 1]; anything else (nan, inf, out of range,
    garbage) falls back to ``default`` with a warning.

    ``nan`` matters: every ``score >= nan`` is False, so a typo would silently
    turn ``apply`` into a no-op (or, for the floor, record nothing).
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        logger.warning("%s=%r is not a number in [0, 1]; using %s", name, raw, default)
        return default
    return value


def autodedup_threshold() -> float:
    return _env_unit_float("MEM0_AUTODEDUP_THRESHOLD", DEFAULT_THRESHOLD)


def autodedup_report_floor(threshold: Optional[float] = None) -> float:
    """Lowest score recorded by report mode, clamped to ``[0, threshold]``."""
    threshold = autodedup_threshold() if threshold is None else threshold
    floor = _env_unit_float("MEM0_AUTODEDUP_REPORT_FLOOR", DEFAULT_REPORT_FLOOR)
    return max(0.0, min(floor, threshold))


def autodedup_top_k() -> int:
    try:
        return max(1, int(os.getenv("MEM0_AUTODEDUP_TOP_K", "10")))
    except ValueError:
        return 10


def _added_memories(result: Any) -> list[dict]:
    """New (non-DELETE) memories from a mem0 ``add`` result."""
    out = []
    for r in (result or {}).get("results", []) or []:
        if not isinstance(r, dict):
            continue
        if (r.get("event") or "ADD").upper() == "DELETE":
            continue
        if r.get("id") and (r.get("memory") or "").strip():
            out.append({"id": str(r["id"]), "memory": r["memory"]})
    return out


def find_near_duplicates(
    client,
    new_items: Iterable[dict],
    *,
    threshold: Optional[float] = None,
    top_k: Optional[int] = None,
    floor: Optional[float] = None,
) -> list[dict]:
    """Existing memories that duplicate one of ``new_items``.

    Returns ``[{"new_id", "duplicate_id", "score", "duplicate_text", "new_text",
    "duplicate_project", "above_threshold"}]``. Never raises: a failure to inspect
    one item must not fail the write that produced it.

    ``floor`` (report mode only) lowers the cut-off so near misses are returned
    too, flagged ``above_threshold=False``; by default it equals ``threshold``.
    """
    threshold = autodedup_threshold() if threshold is None else threshold
    top_k = autodedup_top_k() if top_k is None else top_k
    cutoff = threshold if floor is None else min(floor, threshold)

    items = list(new_items)
    if not items:
        return []

    # Everything written by this job is excluded: a submission that legitimately
    # states the same fact twice must not supersede itself.
    own_ids = {i["id"] for i in items}
    found: list[dict] = []

    for item in items:
        try:
            vectors = client.embedding_model.embed(item["memory"], "search")
            hits = client.vector_store.search(
                query=item["memory"],
                vectors=vectors,
                top_k=top_k,
                filters=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("autodedup lookup failed for id=%s: %s", item["id"], exc)
            continue

        for h in hits or []:
            hid = str(getattr(h, "id", "") or "")
            score = getattr(h, "score", None)
            if not hid or hid in own_ids:
                continue
            if not isinstance(score, (int, float)) or score < cutoff:
                continue
            payload = getattr(h, "payload", {}) or {}
            if (payload.get("state") or "").lower() == "obsolete":
                continue
            found.append(
                {
                    "new_id": item["id"],
                    "duplicate_id": hid,
                    "score": float(score),
                    "duplicate_text": payload.get("data"),
                    "new_text": item["memory"],
                    "duplicate_project": payload.get("project"),
                    "above_threshold": score >= threshold,
                }
            )

    return found


def autodedup_after_write(client, result, *, project: str = "", job_id: str = "") -> dict:
    """Detect and (in apply mode) supersede duplicates of a just-written memory.

    Returns a summary dict; callers use it for logging only. Best-effort by design.
    """
    mode = autodedup_mode()
    if mode == MODE_OFF:
        return {"mode": MODE_OFF, "candidates": []}

    items = _added_memories(result)
    if not items:
        return {"mode": mode, "candidates": []}

    threshold = autodedup_threshold()
    if mode == MODE_REPORT:
        # Same single search per item; only the recording cut-off is lower, so
        # near misses below the threshold become visible for calibration.
        found = find_near_duplicates(
            client, items, threshold=threshold, floor=autodedup_report_floor(threshold)
        )
    else:
        found = find_near_duplicates(client, items, threshold=threshold)
    candidates = [c for c in found if c["above_threshold"]]
    summary: dict[str, Any] = {"mode": mode, "candidates": candidates}

    if mode == MODE_REPORT:
        summary["near_misses"] = [c for c in found if not c["above_threshold"]]
        if found:
            # record_report_candidates never raises; only the import is guarded.
            try:
                from app.utils.autodedup_report import record_report_candidates
            except Exception as exc:  # noqa: BLE001 - diagnostics never fail a write
                logger.warning("autodedup report unavailable job_id=%s: %s", job_id, exc)
                summary["recorded"] = 0
            else:
                summary["recorded"] = record_report_candidates(
                    found, threshold=threshold, project=project, job_id=job_id
                )

    if not candidates:
        return summary

    for c in candidates:
        logger.info(
            "autodedup %s job_id=%s project=%s new=%s duplicate=%s score=%.4f text=%r",
            mode,
            job_id,
            project,
            c["new_id"],
            c["duplicate_id"],
            c["score"],
            (c.get("duplicate_text") or "")[:160],
        )

    if mode != MODE_APPLY:
        return summary

    # Group by the winner so each obsolete point records what replaced it.
    by_new: dict[str, list[str]] = {}
    for c in candidates:
        by_new.setdefault(c["new_id"], []).append(c["duplicate_id"])

    updated: list[str] = []
    try:
        from app.utils.supersedes import mark_points_obsolete

        for new_id, dup_ids in by_new.items():
            out = mark_points_obsolete(client, dup_ids, superseded_by=new_id)
            updated.extend(out.get("updated") or [])
    except Exception:  # noqa: BLE001 - never fail the write over dedup
        logger.exception("autodedup apply failed job_id=%s project=%s", job_id, project)

    summary["superseded"] = updated
    return summary
