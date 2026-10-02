"""Optional cross-encoder reranking for the MCP semantic read path.

``search_memory`` has always accepted a ``rerank`` flag, but nothing was wired to
it: the parameter was declared and never read, so callers asking for reranking
silently got plain vector order. This module supplies the wiring and — just as
importantly — makes the outcome observable, so a caller can tell whether
reranking actually happened.

Everything here is OPT-IN: with ``MEM0_RERANKER_PROVIDER`` unset nothing is
imported, loaded or changed, and ``search_memory(rerank=true)`` answers
``rerank.applied=false reason=not_configured``.

Configuration (all optional):
  MEM0_RERANKER_PROVIDER     — sentence_transformer | huggingface (local, CPU) |
                               cohere | zero_entropy | llm_reranker (remote; refused
                               when MEM0_LOCAL_ONLY=1, see ``REMOTE_PROVIDERS``)
  MEM0_RERANKER_MODEL        — provider-specific model id (HF id for local ones).
                               The store is PT-BR: prefer a multilingual
                               cross-encoder (cross-encoder/mmarco-mMiniLMv2-L12-H384-v1);
                               ms-marco-MiniLM-L-6-v2 is English-only.
  MEM0_RERANKER_API_KEY      — for providers that need one
  MEM0_RERANKER_DEVICE       — torch device for local providers (default ``cpu``)
  MEM0_RERANKER_BATCH_SIZE   — cross-encoder batch size (default 16)
  MEM0_RERANKER_MAX_LENGTH   — token limit per pair, huggingface provider only (512)
  MEM0_RERANKER_TOP_N        — how many of the best candidates are rescored
                               (default 30, clamped to >= the page size). CPU cost
                               is linear in it: measured ~10 ms/candidate for a
                               ~200-token memory with a MiniLM cross-encoder
  MEM0_RERANKER_TIMEOUT_SEC  — budget for one rerank call (default 3.0); on timeout
                               the original order is kept
  MEM0_RERANKER_MAX_CONCURRENCY — rerank passes allowed to run at once (default 1).
                               Calls beyond it are NOT queued: they answer
                               ``reason=busy`` immediately with the original order.
                               A pass abandoned by a timeout keeps its slot until
                               the model really returns (torch cannot be cancelled).
  MEM0_RERANKER_THREADS      — torch intra-op threads for local providers (default 2,
                               clamped to [1, cpu_count]); also the default for
                               OMP_NUM_THREADS / MKL_NUM_THREADS when unset, so the
                               cross-encoder does not starve llama.cpp / Qdrant.
  MEM0_RERANKER_BREAKER_THRESHOLD — consecutive timeouts that open the circuit
                               breaker (default 3)
  MEM0_RERANKER_BREAKER_COOLDOWN_SEC — how long an open breaker skips rerank
                               (``reason=circuit_open``, default 60); afterwards ONE
                               probe call is let through (half-open): success closes
                               the breaker, a timeout reopens it.
  MEM0_RERANKER_ALLOW_DOWNLOAD — 0 (default): the loader runs the Hugging Face stack
                               OFFLINE (HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE /
                               HF_HUB_DISABLE_TELEMETRY = 1) and a model missing
                               from the local cache answers ``unavailable``. 1 lets
                               it reach huggingface.co (dev only). Ignored when
                               MEM0_LOCAL_ONLY=1: local-only is always offline.
  MEM0_RERANKER_WARMUP       — 1 (default): load the model in the background at
                               API startup; 0: load on the first rerank request.
                               Either way the model is NEVER loaded on the request
                               path: until it is ready, rerank answers
                               ``applied=false reason=loading``.

Pipeline (see ``app.mcp_server.search_memory``):
  1. The candidate pool (``MEM0_SEARCH_CANDIDATE_K``, 150) is ranked with the usual
     blend ``score × recency × project × group × lexical``.
  2. The best ``MEM0_RERANKER_TOP_N`` of that ranking are rescored by the
     cross-encoder. Selecting AFTER the blend keeps the recency/group rescue that
     motivated the wide pool: a recent same-group memory that missed the raw vector
     top-20 can still reach the cross-encoder.
  3. The rescored head is ranked again with the same blend, now with ``score`` =
     the normalized rerank score; the head stays above the tail (the tail was never
     judged by the cross-encoder, so its cosine score is not comparable). Since
     ``TOP_N >= page size``, the page returned to the client is entirely reranked.

Scores: cross-encoders emit unbounded logits (or sigmoid probabilities, depending
on the model), which cannot be fed to the multiplicative boosts in
:mod:`app.utils.recency` — a negative logit multiplied by a boost moves the wrong
way. We therefore min-max normalize the rerank scores over the rescored head into
``score`` (the field the ranker consumes), while preserving the untouched values in
``semantic_score`` and ``rerank_score`` for inspection. The normalized value is
floored at ``RERANK_SCORE_FLOOR`` (``floor + (1 - floor) × minmax``): a plain
min-max would give the last item of the head exactly 0, and ``0 × boosts`` would
make it impossible for recency/group to lift it at all. ``effective_score`` of a
reranked item is therefore ``rerank_norm × recency × project × group × lexical``.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Providers that score documents on a third-party service: memory content would
# leave the LAN. ``llm_reranker`` is included because, configured from env only,
# it defaults to the OpenAI provider.
REMOTE_PROVIDERS = frozenset({"cohere", "zero_entropy", "llm_reranker"})
# Providers that run a local torch model and accept ``device`` / ``batch_size``.
LOCAL_MODEL_PROVIDERS = frozenset({"sentence_transformer", "huggingface"})
# Model the SDK picks when MEM0_RERANKER_MODEL is unset (mem0/configs/rerankers/*).
SDK_DEFAULT_MODELS = {
    "sentence_transformer": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "huggingface": "BAAI/bge-reranker-base",
}

DEFAULT_TOP_N = 30
DEFAULT_TIMEOUT_SEC = 3.0
DEFAULT_BATCH_SIZE = 16
DEFAULT_THREADS = 2
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_SEC = 60.0
# Lowest normalized score inside the rescored head (see module docstring).
RERANK_SCORE_FLOOR = 0.05
# Env vars that keep the Hugging Face stack off the network at runtime.
HF_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")

# Monotonic clock used by the circuit breaker (patched by tests).
_clock: Callable[[], float] = time.monotonic

_lock = threading.Lock()
_reranker = None
_load_error: Optional[str] = None
_loaded = False
# Background loading state. ``_generation`` invalidates a load that finishes after
# ``reset_reranker_cache`` (tests / config reload) so it cannot resurrect old state.
_loading = False
_load_thread: Optional[threading.Thread] = None
_generation = 0
_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


class _Gate:
    """Admission control for rerank passes: slots + circuit breaker (thread-safe).

    Slots: at most ``capacity`` passes run at once. A slot is released when the
    provider call REALLY finishes (future done-callback), not when the caller gives
    up on a timeout — torch cannot be interrupted, so an abandoned pass still holds
    the CPU and the executor worker. A caller that finds no free slot is answered
    ``busy`` right away instead of queueing behind it (a queue would turn one slow
    pass into "+timeout on every search").

    Breaker: ``threshold`` consecutive timeouts open it for ``cooldown`` seconds
    (``circuit_open``); then a single probe is admitted (half-open). Probe success
    closes it; a probe timeout/failure reopens it for another cooldown.
    """

    def __init__(self, capacity: int, threshold: int, cooldown: float) -> None:
        self.capacity = capacity
        self.threshold = threshold
        self.cooldown = cooldown
        self.lock = threading.Lock()
        self.inflight = 0
        self.abandoned = 0  # in-flight passes whose caller already timed out
        self.state = "closed"  # closed | open | half_open
        self.consecutive_timeouts = 0
        self.opened_at: Optional[float] = None
        self.probe_in_flight = False
        self.opened_total = 0

    def admit(self) -> tuple[Optional[str], bool]:
        """Try to take a slot. Returns ``(refusal_reason, is_probe)``."""
        with self.lock:
            is_probe = False
            if self.state == "open":
                if _clock() - (self.opened_at or 0.0) < self.cooldown:
                    return "circuit_open", False
                self.state = "half_open"
            if self.state == "half_open":
                if self.probe_in_flight:
                    return "circuit_open", False
                is_probe = True
            if self.inflight >= self.capacity:
                return "busy", False
            self.inflight += 1
            if is_probe:
                self.probe_in_flight = True
            return None, is_probe

    def release(self, ticket: dict) -> None:
        """Free the slot of a pass that really finished (done-callback / submit error)."""
        with self.lock:
            if ticket.get("done"):
                return
            ticket["done"] = True
            self.inflight = max(0, self.inflight - 1)
            if ticket.get("abandoned"):
                self.abandoned = max(0, self.abandoned - 1)

    def abandon(self, ticket: dict) -> None:
        """The caller gave up (timeout) but the pass may still be running."""
        with self.lock:
            if ticket.get("done") or ticket.get("abandoned"):
                return
            ticket["abandoned"] = True
            self.abandoned += 1

    def _open(self, probe_outcome: Optional[str] = None) -> None:
        self.state = "open"
        self.opened_at = _clock()
        self.opened_total += 1
        if probe_outcome is not None:
            # The counter keeps growing across reopen cycles; quoting it here would
            # read as "N timeouts in a row" when only the single probe just failed.
            logger.warning(
                "rerank circuit breaker: half-open probe %s; reopening for %.0fs",
                "timed out" if probe_outcome == "timeout" else "failed",
                self.cooldown,
            )
            return
        logger.warning(
            "rerank circuit breaker OPEN for %.0fs after %d consecutive timeout(s)",
            self.cooldown,
            self.consecutive_timeouts,
        )

    def record(self, outcome: str, is_probe: bool) -> None:
        """Feed the breaker with the outcome of an admitted pass."""
        with self.lock:
            if is_probe:
                self.probe_in_flight = False
            elif self.state != "closed":
                # A pass admitted before the breaker opened finished late: only the
                # half-open probe decides whether the breaker closes again.
                return
            if outcome == "applied":
                if self.state != "closed":
                    logger.info("rerank circuit breaker CLOSED (probe succeeded)")
                self.state = "closed"
                self.consecutive_timeouts = 0
                self.opened_at = None
                return
            if outcome == "timeout":
                self.consecutive_timeouts += 1
            if is_probe and outcome in ("timeout", "failed"):
                self._open(probe_outcome=outcome)
            elif outcome == "timeout" and self.state == "closed" and self.consecutive_timeouts >= self.threshold:
                self._open()

    def snapshot(self) -> dict:
        with self.lock:
            state = self.state
            remaining = None
            if state == "open":
                remaining = max(0.0, self.cooldown - (_clock() - (self.opened_at or 0.0)))
                if remaining == 0.0:
                    state = "half_open"  # next call will be the probe
            return {
                "state": state,
                "consecutive_timeouts": self.consecutive_timeouts,
                "threshold": self.threshold,
                "cooldown_sec": self.cooldown,
                "reopens_in_sec": round(remaining, 1) if remaining else None,
                "opened_total": self.opened_total,
                "inflight": self.inflight,
                "abandoned_inflight": self.abandoned,
                "max_concurrency": self.capacity,
            }


_gate: Optional[_Gate] = None


def _get_gate() -> _Gate:
    global _gate
    with _lock:
        if _gate is None:
            _gate = _Gate(max_concurrency(), breaker_threshold(), breaker_cooldown_seconds())
        return _gate


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        value = default
    return value if value > 0 else default


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def reranker_provider() -> str:
    return (os.getenv("MEM0_RERANKER_PROVIDER") or "").strip()


def reranker_model() -> Optional[str]:
    return (os.getenv("MEM0_RERANKER_MODEL") or "").strip() or None


def rerank_top_n(page_size: int = 0) -> int:
    """Number of best-ranked candidates handed to the cross-encoder.

    Clamped to at least ``page_size`` so the page returned to the client never
    mixes reranked and non-reranked items.
    """
    return max(page_size, _env_int("MEM0_RERANKER_TOP_N", DEFAULT_TOP_N))


def rerank_timeout_seconds() -> float:
    return _env_float("MEM0_RERANKER_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)


def warmup_enabled() -> bool:
    return _env_flag("MEM0_RERANKER_WARMUP", True)


def max_concurrency() -> int:
    return _env_int("MEM0_RERANKER_MAX_CONCURRENCY", 1)


def reranker_threads() -> int:
    """torch intra-op threads for the cross-encoder, clamped to [1, cpu_count]."""
    return min(_env_int("MEM0_RERANKER_THREADS", DEFAULT_THREADS), os.cpu_count() or 1)


def breaker_threshold() -> int:
    return _env_int("MEM0_RERANKER_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD)


def breaker_cooldown_seconds() -> float:
    return _env_float("MEM0_RERANKER_BREAKER_COOLDOWN_SEC", DEFAULT_BREAKER_COOLDOWN_SEC)


def _is_local_only() -> bool:
    from app.utils.env import is_local_only

    return is_local_only()


def downloads_allowed() -> bool:
    """Whether the loader may reach huggingface.co.

    Offline is the runtime default everywhere: the model is baked into the image at
    build time (``RERANK_PRELOAD_MODEL``), and a startup that silently pings — or
    downloads from — the hub is both a LAN leak and a latency/availability hazard.
    ``MEM0_RERANKER_ALLOW_DOWNLOAD=1`` is the explicit dev opt-out; it is ignored
    under ``MEM0_LOCAL_ONLY=1``.
    """
    if _is_local_only():
        return False
    return _env_flag("MEM0_RERANKER_ALLOW_DOWNLOAD", False)


def effective_model(provider: str) -> Optional[str]:
    return reranker_model() or SDK_DEFAULT_MODELS.get(provider)


def _apply_hub_policy() -> bool:
    """Pin the HF network policy before the stack is imported. Returns offline?.

    Offline OVERWRITES (fail-closed: an inherited ``HF_HUB_OFFLINE=0`` must not
    reopen the network — the only opt-out is ``MEM0_RERANKER_ALLOW_DOWNLOAD=1``).

    Scope: ``os.environ`` is PROCESS-WIDE. With a local provider configured, the
    whole API process goes HF-offline — including a ``huggingface`` embedder or
    the fastembed BM25 model, which then must also be present in the local cache.
    """
    offline = not downloads_allowed()
    if offline:
        for name in HF_OFFLINE_ENV:
            os.environ[name] = "1"
        # huggingface_hub reads HF_HUB_OFFLINE once, at import. If something already
        # imported it, flip the cached constant too (best effort).
        # Import order matters: ``transformers`` caches its own offline flag
        # (``transformers.utils.hub``/``TRANSFORMERS_OFFLINE``) at import time and is
        # NOT patched here — if it was imported before this runs, it keeps the old
        # value. ``main.py`` sets the env first under MEM0_LOCAL_ONLY=1 for that reason.
        hub_constants = sys.modules.get("huggingface_hub.constants")
        if hub_constants is not None and hasattr(hub_constants, "HF_HUB_OFFLINE"):
            try:
                hub_constants.HF_HUB_OFFLINE = True
            except Exception:  # noqa: BLE001
                pass
    else:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return offline


def _apply_thread_limit() -> int:
    """Cap torch CPU threads so the cross-encoder does not starve llama.cpp/Qdrant."""
    threads = reranker_threads()
    # Only honoured by OpenMP/MKL if set before torch is first imported (the loader
    # thread is usually that first import); explicit operator values win.
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))
    try:
        import torch  # noqa: PLC0415 - optional, heavy
    except ImportError:
        return threads
    try:
        torch.set_num_threads(threads)
    except Exception as exc:  # noqa: BLE001
        logger.warning("torch.set_num_threads(%s) failed: %s", threads, exc)
    return threads


def _model_in_local_cache(model: Optional[str]) -> Optional[bool]:
    """True/False when the HF cache can be inspected, None when unknown."""
    if not model:
        return None
    if os.path.isdir(model):
        return True
    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - stack absent: let the factory report it
        return None
    try:
        found = try_to_load_from_cache(model, "config.json")
    except Exception:  # noqa: BLE001
        return None
    return isinstance(found, str)


def _missing_model_reason(model: Optional[str]) -> str:
    return (
        f"unavailable: modelo '{model}' não está no cache local "
        f"(HF_HOME={os.getenv('HF_HOME') or '~/.cache/huggingface'}; runtime offline); "
        f"rebuild com RERANK_PRELOAD_MODEL={model}"
    )


def _looks_like_missing_model(exc: BaseException) -> bool:
    names = {type(e).__name__ for e in _exc_chain(exc)}
    if names & {"LocalEntryNotFoundError", "OfflineModeIsEnabled", "EntryNotFoundError"}:
        return True
    text = " ".join(str(e) for e in _exc_chain(exc)).lower()
    return any(
        marker in text
        for marker in ("offline", "local_files_only", "couldn't connect", "cannot find the requested files")
    )


def _exc_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def build_reranker_config(provider: str) -> dict[str, Any]:
    """Translate MEM0_RERANKER_* env vars into the SDK's provider config dict.

    Only keys the provider's config class declares are emitted (the SDK config
    classes are strict pydantic models fed via ``config_class(**config)``).
    """
    config: dict[str, Any] = {"provider": provider}
    model = reranker_model()
    if model:
        config["model"] = model
    api_key = (os.getenv("MEM0_RERANKER_API_KEY") or "").strip()
    if api_key:
        config["api_key"] = api_key
    if provider in LOCAL_MODEL_PROVIDERS:
        # CPU by default: the shared deploy has no GPU and "auto" would silently
        # pick CUDA on a dev box, making latency numbers incomparable.
        config["device"] = (os.getenv("MEM0_RERANKER_DEVICE") or "").strip() or "cpu"
        config["batch_size"] = _env_int("MEM0_RERANKER_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        # ``top_k`` stays None: the head size is decided here (TOP_N), not by the
        # provider, and the provider must return every document it was given.
        config["top_k"] = None
    if provider == "sentence_transformer":
        config["show_progress_bar"] = False
    if provider == "huggingface":
        config["max_length"] = _env_int("MEM0_RERANKER_MAX_LENGTH", 512)
    return config


def _precheck(provider: str) -> Optional[str]:
    """Cheap, synchronous reasons to refuse a provider (no model import)."""
    from app.utils.env import is_local_only

    if provider in REMOTE_PROVIDERS and is_local_only():
        return (
            f"blocked_local_only: provider '{provider}' sends memory content off the "
            "LAN; use sentence_transformer or huggingface (MEM0_LOCAL_ONLY=1)"
        )
    try:
        from mem0.utils.factory import RerankerFactory
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"
    if provider not in RerankerFactory.provider_to_class:
        return f"unavailable: Unsupported reranker provider: {provider}"
    return None


def _build_reranker():
    """Instantiate the configured reranker, or return None when unavailable."""
    provider = reranker_provider()
    if not provider:
        return None, "not_configured"

    refused = _precheck(provider)
    if refused:
        logger.warning("reranker '%s' refused: %s", provider, refused)
        return None, refused

    config = build_reranker_config(provider)
    model = effective_model(provider)
    offline = False
    threads = None
    if provider in LOCAL_MODEL_PROVIDERS:
        # Network policy and CPU cap BEFORE the HF/torch stack is imported: both are
        # read at import time. Done here (not only in main.py) so scripts such as
        # bench-rerank-latency.py get the same guarantees.
        offline = _apply_hub_policy()
        threads = _apply_thread_limit()
        if offline and _model_in_local_cache(model) is False:
            reason = _missing_model_reason(model)
            logger.warning("reranker '%s' %s", provider, reason)
            return None, reason
    started = time.perf_counter()
    try:
        from mem0.utils.factory import RerankerFactory

        instance = RerankerFactory.create(provider, config)
    except Exception as exc:  # noqa: BLE001 - never break search over reranking
        if offline and _looks_like_missing_model(exc):
            reason = _missing_model_reason(model)
        else:
            reason = f"unavailable: {exc}"
        logger.warning("reranker '%s' %s", provider, reason)
        return None, reason
    logger.info(
        "reranker '%s' loaded (model=%s device=%s threads=%s offline=%s) in %.1fs",
        provider,
        model,
        config.get("device"),
        threads,
        offline,
        time.perf_counter() - started,
    )
    return instance, None


def _warm(instance) -> None:
    """One tiny scoring pass so the first real request does not pay lazy init."""
    try:
        instance.rerank("warmup", [{"id": "warmup", "memory": "warmup"}], top_k=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reranker warmup pass failed (ignored): %s", exc)


def get_reranker():
    """Build the reranker synchronously once; cache the instance and any failure.

    Blocking — for scripts and the background loader. The request path uses
    :func:`get_reranker_nonblocking`.
    """
    global _reranker, _load_error, _loaded
    if _loaded:
        return _reranker, _load_error
    with _lock:
        if not _loaded:
            _reranker, _load_error = _build_reranker()
            _loaded = True
    return _reranker, _load_error


def _background_load(generation: int, warm: bool) -> None:
    global _reranker, _load_error, _loaded, _loading
    instance, error = None, "unavailable: loader crashed"
    try:
        instance, error = _build_reranker()
        if instance is not None and warm:
            _warm(instance)
    except BaseException as exc:  # noqa: BLE001 - never leave the state at "loading"
        logger.exception("reranker background load crashed")
        instance, error = None, f"unavailable: loader crashed: {exc}"
        if not isinstance(exc, Exception):
            raise
    finally:
        with _lock:
            if generation == _generation:  # else: cache was reset; discard stale result
                _reranker, _load_error = instance, error
                _loaded = True
                _loading = False


def start_background_load(warm: bool = True) -> bool:
    """Start loading the configured model in a daemon thread (idempotent).

    Returns True when a load was started now. No-op when rerank is not configured,
    already loaded, or already loading.
    """
    global _loading, _load_thread, _reranker, _load_error, _loaded
    provider = reranker_provider()
    if not provider:
        return False
    with _lock:
        if _loaded or _loading:
            return False
        refused = _precheck(provider)
        if refused:
            # Fail fast and visibly: no thread for a config that can never load.
            _reranker, _load_error, _loaded = None, refused, True
            return False
        _loading = True
        _load_thread = threading.Thread(
            target=_background_load,
            args=(_generation, warm),
            name="reranker-loader",
            daemon=True,
        )
        _load_thread.start()
    return True


def get_reranker_nonblocking():
    """Return ``(instance, reason)`` without ever loading a model on this thread.

    While the model is loading the reason is ``"loading"`` and the caller keeps the
    original order.
    """
    if _loaded:
        return _reranker, _load_error
    if not reranker_provider():
        return None, "not_configured"
    start_background_load(warm=False)
    if _loaded:  # precheck failed synchronously
        return _reranker, _load_error
    return None, "loading"


def wait_for_reranker(timeout: Optional[float] = None) -> bool:
    """Block until a background load finishes (tests / scripts). True if loaded."""
    thread = _load_thread
    if thread is not None:
        thread.join(timeout)
    return _loaded


def warmup_on_startup() -> bool:
    """API startup hook: preload the model off the request path when opted in."""
    if not reranker_provider() or not warmup_enabled():
        return False
    return start_background_load(warm=True)


def reset_reranker_cache() -> None:
    """Drop the cached instance (tests / config reload)."""
    global _reranker, _load_error, _loaded, _loading, _load_thread, _generation, _executor, _gate
    with _lock:
        _reranker = None
        _load_error = None
        _loaded = False
        _loading = False
        _load_thread = None
        _generation += 1
        executor, _executor = _executor, None
        _gate = None  # passes still running release into the old (dropped) gate
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Executor sized to the gate capacity: admitted work never waits in its queue."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_concurrency(),
                thread_name_prefix="reranker",
            )
        return _executor


def rerank_config_status() -> dict:
    """Operator-facing status: whether rerank is configured and usable.

    Does not load the model — reports env + cache + admission state.
    """
    provider = reranker_provider() or None
    if not provider:
        return {
            "configured": False,
            "provider": None,
            "reason": "not_configured",
        }
    settings = {
        "model": reranker_model(),
        "effective_model": effective_model(provider),
        "device": build_reranker_config(provider).get("device"),
        "top_n": rerank_top_n(),
        "timeout_sec": rerank_timeout_seconds(),
        "warmup": warmup_enabled(),
        "max_concurrency": max_concurrency(),
    }
    if provider in LOCAL_MODEL_PROVIDERS:
        settings["threads"] = reranker_threads()
        settings["offline"] = not downloads_allowed()
    if _loaded:
        reason = (_load_error or "unavailable") if _reranker is None else None
    elif _loading:
        reason = "loading"
    else:
        reason = "not_loaded_yet"
    return {
        "configured": True,
        "provider": provider,
        "reason": reason,
        **settings,
        "breaker": _get_gate().snapshot(),
    }


def _normalize_into_score(results: list[dict]) -> None:
    """Map ``rerank_score`` onto ``score`` in [RERANK_SCORE_FLOOR, 1].

    ``floor + (1 - floor) × minmax``: the best item gets 1.0 and the worst gets the
    floor (not 0), so the multiplicative boosts can still lift it. Originals are
    preserved in ``rerank_score`` / ``semantic_score``. A flat pool (every document
    scored alike) carries no ordering information, so every entry gets 1.0 and the
    recency/project/group boosts decide alone.
    """
    scores = [
        r["rerank_score"]
        for r in results
        if isinstance(r.get("rerank_score"), (int, float))
    ]
    if not scores:
        return
    lo, hi = min(scores), max(scores)
    span = hi - lo
    for r in results:
        raw = r.get("rerank_score")
        if not isinstance(raw, (int, float)):
            continue
        r.setdefault("semantic_score", r.get("score"))
        if span <= 0:
            r["score"] = 1.0
        else:
            r["score"] = RERANK_SCORE_FLOOR + (1.0 - RERANK_SCORE_FLOOR) * (raw - lo) / span


def _looks_like_swallowed_failure(head: list[dict], reranked: list[dict]) -> bool:
    """The SDK sentence_transformer provider hides predict() errors.

    On exception it returns the input order with ``rerank_score=0.0`` on every
    document. A real cross-encoder never scores a multi-document pool exactly 0.0
    across the board, so treat that signature as a failure instead of reporting
    ``applied=true`` over an unchanged order.
    """
    if len(reranked) < 2:
        return False
    if any(r.get("rerank_score") != 0.0 for r in reranked):
        return False
    return [r.get("id") for r in reranked] == [r.get("id") for r in head]


def _observe(outcome: str, seconds: Optional[float] = None) -> None:
    try:
        from app.utils.metrics import RERANK_LATENCY, RERANK_OUTCOME

        RERANK_OUTCOME.labels(outcome=outcome).inc()
        if seconds is not None:
            RERANK_LATENCY.observe(seconds)
    except Exception:  # noqa: BLE001 - metrics must never break search
        pass


def apply_rerank(query: str, results: list[dict], *, page_size: int = 0) -> dict:
    """Rerank the head of ``results`` in place when a reranker is configured.

    ``results`` must already be in final-ranking order: the best
    :func:`rerank_top_n` entries are rescored, the rest (the tail) keeps its
    position after them. Returns a status dict that the caller must surface to the
    client, so that "I asked for rerank" and "rerank happened" are never conflated.
    On ``applied=True`` the status carries ``reranked`` = size of the rescored head,
    which the caller re-ranks with the boosts. Any failure, timeout, busy slot or
    open breaker degrades to the original ordering — reranking must not break
    search, and must never make a search wait for somebody else's pass.
    """
    provider = reranker_provider()
    if not results:
        return {"applied": False, "provider": provider or None, "reason": "no_results"}

    reranker, load_error = get_reranker_nonblocking()
    if reranker is None:
        reason = load_error or "not_configured"
        if reason != "not_configured":
            _observe("loading" if reason == "loading" else "unavailable")
        return {"applied": False, "provider": provider or None, "reason": reason}

    # The model actually loaded: without MEM0_RERANKER_MODEL the SDK default is in
    # use, and the client must see its name (not ``null``) — same as /admin/rerank's
    # ``effective_model``.
    model = effective_model(provider)
    # Everything that can raise is prepared BEFORE taking a slot: an exception
    # between admit() and submit() would leak the slot (and, for the half-open
    # probe, pin ``probe_in_flight``), leaving rerank busy/circuit_open until restart.
    try:
        top_n = rerank_top_n(page_size)
        # Defensive copy: the caller's list may hold dicts shared with the read cache,
        # and not every provider copies before writing ``rerank_score``.
        head = [dict(r) for r in results[:top_n]]
        tail = results[top_n:]
        timeout = rerank_timeout_seconds()
    except Exception as exc:  # noqa: BLE001 - malformed candidates: keep original order
        logger.warning("rerank skipped, bad candidates (provider=%s): %s", provider, exc)
        _observe("failed")
        return {"applied": False, "provider": provider, "model": model, "reason": f"failed: {exc}"}

    gate = _get_gate()
    refused, is_probe = gate.admit()
    if refused:
        # No queueing: answer at once with the original order.
        _observe(refused)
        return {"applied": False, "provider": provider, "model": model, "reason": refused}

    ticket: dict = {}
    started = time.perf_counter()
    try:
        # top_k=None: the provider must return the whole head; the caller applies
        # the recency/project/group boosts and cuts the page afterwards.
        future = _get_executor().submit(reranker.rerank, query, head, None)
        # The slot is freed when the provider REALLY returns, even after a timeout.
        future.add_done_callback(lambda _f: gate.release(ticket))
    except Exception as exc:  # noqa: BLE001 - e.g. executor shut down by a reset
        # release() is idempotent per ticket (a done-callback that already ran wins).
        gate.release(ticket)
        gate.record("failed", is_probe)
        _observe("failed")
        return {"applied": False, "provider": provider, "model": model, "reason": f"failed: {exc}"}
    try:
        reranked = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        gate.abandon(ticket)
        gate.record("timeout", is_probe)
        logger.warning("rerank timed out after %.2fs (provider=%s)", timeout, provider)
        _observe("timeout", time.perf_counter() - started)
        return {
            "applied": False,
            "provider": provider,
            "model": model,
            "reason": f"timeout: exceeded {timeout:g}s",
        }
    except Exception as exc:  # noqa: BLE001
        gate.record("failed", is_probe)
        logger.warning("rerank failed (provider=%s): %s", provider, exc)
        _observe("failed", time.perf_counter() - started)
        return {"applied": False, "provider": provider, "model": model, "reason": f"failed: {exc}"}
    elapsed = time.perf_counter() - started

    if not reranked:
        gate.record("failed", is_probe)
        _observe("failed", elapsed)
        return {"applied": False, "provider": provider, "model": model, "reason": "empty_result"}
    if _looks_like_swallowed_failure(head, reranked):
        gate.record("failed", is_probe)
        _observe("failed", elapsed)
        return {
            "applied": False,
            "provider": provider,
            "model": model,
            "reason": "failed: provider returned no scores",
        }

    gate.record("applied", is_probe)
    _normalize_into_score(reranked)
    results[:] = list(reranked) + list(tail)
    _observe("applied", elapsed)
    return {
        "applied": True,
        "provider": provider,
        "model": model,
        "reranked": len(reranked),
        "candidates": len(reranked) + len(tail),
        "latency_ms": round(elapsed * 1000, 1),
    }
