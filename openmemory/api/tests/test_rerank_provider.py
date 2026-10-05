"""Rerank LOCAL via sentence-transformers (card 80110071).

Pins the opt-in contract of :mod:`app.utils.reranking` with fakes only — no torch,
no model download:

- env -> SDK config mapping (device CPU, batch, top_k=None) through the REAL
  ``RerankerFactory`` and ``SentenceTransformerReranker`` with a fake CrossEncoder;
- reordering reported as ``applied=true`` with provider/model;
- only the best ``MEM0_RERANKER_TOP_N`` candidates are rescored;
- fallback to the original order on error, timeout or still-loading model;
- nothing happens by default (no provider -> no load, no thread).
"""

import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest

from app import mcp_server
from app.mcp_server import DEFAULT_SEARCH_TOP_K, search_memory
from app.utils import reranking

ST_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "MEM0_RERANKER_PROVIDER",
        "MEM0_RERANKER_MODEL",
        "MEM0_RERANKER_API_KEY",
        "MEM0_RERANKER_DEVICE",
        "MEM0_RERANKER_BATCH_SIZE",
        "MEM0_RERANKER_TOP_N",
        "MEM0_RERANKER_TIMEOUT_SEC",
        "MEM0_RERANKER_WARMUP",
        "MEM0_RERANKER_MAX_CONCURRENCY",
        "MEM0_RERANKER_THREADS",
        "MEM0_RERANKER_BREAKER_THRESHOLD",
        "MEM0_RERANKER_BREAKER_COOLDOWN_SEC",
        "MEM0_RERANKER_ALLOW_DOWNLOAD",
        "MEM0_LOCAL_ONLY",
        # Written by the loader itself: setenv+delenv so teardown removes them.
        *reranking.HF_OFFLINE_ENV,
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    reranking.reset_reranker_cache()
    yield
    reranking.reset_reranker_cache()


def _hit(mem_id, data, project="A", score=0.9):
    return SimpleNamespace(
        id=mem_id, score=score, payload={"data": data, "project": project, "hash": f"h-{mem_id}"}
    )


@pytest.fixture
def patched_client():
    client = MagicMock()
    client.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    client.embedding_model.model = "test-embed-model"
    client.vector_store.search.return_value = []
    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=client),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server.read_cache, "get_search", return_value=None),
        patch.object(mcp_server.read_cache, "set_search"),
        patch.object(mcp_server.read_cache, "get_embedding", return_value=None),
        patch.object(mcp_server.read_cache, "set_embedding"),
    ):
        yield client


def _install(monkeypatch, instance, provider="stub", model=None):
    monkeypatch.setenv("MEM0_RERANKER_PROVIDER", provider)
    if model:
        monkeypatch.setenv("MEM0_RERANKER_MODEL", model)
    monkeypatch.setattr(reranking, "_reranker", instance)
    monkeypatch.setattr(reranking, "_loaded", True)
    monkeypatch.setattr(reranking, "_load_error", None)


class _FakeCrossEncoder:
    """Stand-in for sentence_transformers.CrossEncoder (records init + calls)."""

    instances: list = []

    def __init__(self, model_name, device=None, **kwargs):
        self.model_name = model_name
        self.device = device
        self.predict_calls = []
        _FakeCrossEncoder.instances.append(self)

    def predict(self, pairs, batch_size=32, show_progress_bar=False):
        self.predict_calls.append({"n": len(pairs), "batch_size": batch_size})
        # Score = number of query words found in the document (deterministic).
        return [float(sum(w in doc for w in q.split())) for q, doc in pairs]


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    from mem0.reranker import sentence_transformer_reranker as st_mod

    _FakeCrossEncoder.instances = []
    monkeypatch.setattr(st_mod, "CrossEncoder", _FakeCrossEncoder, raising=False)
    monkeypatch.setattr(st_mod, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    # The fake model is "in the cache" whatever HF stack the venv has.
    monkeypatch.setattr(reranking, "_model_in_local_cache", lambda model: True)
    return _FakeCrossEncoder


class _StubReranker:
    def __init__(self, preferred=(), delay=0.0):
        self.preferred = set(preferred)
        self.delay = delay
        self.calls = []

    def rerank(self, query, documents, top_k=None):
        self.calls.append([d["id"] for d in documents])
        if self.delay:
            time.sleep(self.delay)
        out = []
        for doc in documents:
            d = dict(doc)
            d["rerank_score"] = 5.0 if doc["id"] in self.preferred else -2.0
            out.append(d)
        out.sort(key=lambda d: d["rerank_score"], reverse=True)
        return out


# --------------------------------------------------------------------------- #
# Config mapping
# --------------------------------------------------------------------------- #
class TestConfigMapping:
    def test_sentence_transformer_defaults_to_cpu_and_no_provider_top_k(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_MODEL", ST_MODEL)
        cfg = reranking.build_reranker_config("sentence_transformer")
        assert cfg == {
            "provider": "sentence_transformer",
            "model": ST_MODEL,
            "device": "cpu",
            "batch_size": reranking.DEFAULT_BATCH_SIZE,
            "top_k": None,
            "show_progress_bar": False,
        }

    def test_env_overrides_device_and_batch(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_DEVICE", "cuda:0")
        monkeypatch.setenv("MEM0_RERANKER_BATCH_SIZE", "4")
        cfg = reranking.build_reranker_config("sentence_transformer")
        assert cfg["device"] == "cuda:0"
        assert cfg["batch_size"] == 4

    def test_invalid_numbers_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_BATCH_SIZE", "abc")
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", "-3")
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0")
        assert reranking.build_reranker_config("sentence_transformer")["batch_size"] == 16
        assert reranking.rerank_top_n() == 1  # clamped to >= 1
        assert reranking.rerank_timeout_seconds() == reranking.DEFAULT_TIMEOUT_SEC

    def test_remote_provider_gets_no_local_keys(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_API_KEY", "k")
        cfg = reranking.build_reranker_config("cohere")
        assert cfg == {"provider": "cohere", "api_key": "k"}

    def test_mapping_reaches_sdk_cross_encoder(self, monkeypatch, fake_sentence_transformers):
        """End to end through the real RerankerFactory: model + CPU reach CrossEncoder."""
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setenv("MEM0_RERANKER_MODEL", ST_MODEL)
        monkeypatch.setenv("MEM0_RERANKER_BATCH_SIZE", "8")

        instance, err = reranking.get_reranker()

        assert err is None
        encoder = fake_sentence_transformers.instances[-1]
        assert encoder.model_name == ST_MODEL
        assert encoder.device == "cpu"
        assert instance.config.batch_size == 8
        assert instance.config.top_k is None

    def test_missing_package_reports_unavailable(self, monkeypatch):
        from mem0.reranker import sentence_transformer_reranker as st_mod

        monkeypatch.setattr(st_mod, "SENTENCE_TRANSFORMERS_AVAILABLE", False)
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        instance, err = reranking.get_reranker()
        assert instance is None
        assert err.startswith("unavailable:")
        assert "sentence-transformers" in err

    def test_remote_provider_refused_in_local_only(self, monkeypatch):
        monkeypatch.setenv("MEM0_LOCAL_ONLY", "1")
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "cohere")
        instance, err = reranking.get_reranker()
        assert instance is None
        assert err.startswith("blocked_local_only")


# --------------------------------------------------------------------------- #
# Default off
# --------------------------------------------------------------------------- #
class TestDisabledByDefault:
    def test_no_provider_no_load_no_thread(self, monkeypatch):
        build = MagicMock()
        monkeypatch.setattr(reranking, "_build_reranker", build)
        assert reranking.warmup_on_startup() is False
        assert reranking.get_reranker_nonblocking() == (None, "not_configured")
        status = reranking.apply_rerank("q", [{"id": "1", "memory": "m", "score": 0.5}])
        assert status == {"applied": False, "provider": None, "reason": "not_configured"}
        build.assert_not_called()
        assert reranking._load_thread is None

    @pytest.mark.asyncio
    async def test_search_without_flag_never_touches_reranker(self, patched_client, monkeypatch):
        stub = _StubReranker(preferred={"b"})
        _install(monkeypatch, stub)
        patched_client.vector_store.search.return_value = [_hit("a", "a"), _hit("b", "b", score=0.1)]
        data = json.loads(await search_memory("q", project="A"))
        assert "rerank" not in data
        assert stub.calls == []
        assert [r["id"] for r in data["results"]] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Applied / ordering / top_n
# --------------------------------------------------------------------------- #
class TestApplied:
    @pytest.mark.asyncio
    async def test_sentence_transformer_end_to_end_reorders(
        self, patched_client, monkeypatch, fake_sentence_transformers
    ):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setenv("MEM0_RERANKER_MODEL", ST_MODEL)
        assert reranking.start_background_load(warm=False) is True
        assert reranking.wait_for_reranker(5)

        patched_client.vector_store.search.return_value = [
            _hit("noise", "unrelated text", score=0.95),
            _hit("match", "deploy qdrant volume backup", score=0.40),
        ]
        data = json.loads(
            await search_memory("qdrant volume backup", project="A", rerank=True)
        )

        assert data["rerank"]["applied"] is True
        assert data["rerank"]["provider"] == "sentence_transformer"
        assert data["rerank"]["model"] == ST_MODEL
        assert data["rerank"]["reranked"] == 2
        assert "latency_ms" in data["rerank"]
        assert [r["id"] for r in data["results"]] == ["match", "noise"]
        top = data["results"][0]
        assert top["rerank_score"] == 3.0
        assert top["semantic_score"] == 0.40
        # effective_score = normalized rerank score x boosts
        factors = top["ranking_factors"]
        expected = 1.0 * factors["recency"] * factors["project"] * factors["group"] * factors["lexical"]
        assert top["effective_score"] == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_response_reports_sdk_default_model_when_unset(
        self, patched_client, monkeypatch, fake_sentence_transformers
    ):
        """Sem MEM0_RERANKER_MODEL a resposta traz o modelo efetivo (default do SDK),
        não ``null`` — o mesmo que ``effective_model`` de /admin/rerank."""
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        assert reranking.start_background_load(warm=False) is True
        assert reranking.wait_for_reranker(5)
        default = reranking.SDK_DEFAULT_MODELS["sentence_transformer"]
        assert fake_sentence_transformers.instances[-1].model_name == default

        patched_client.vector_store.search.return_value = [
            _hit("noise", "unrelated text", score=0.95),
            _hit("match", "deploy qdrant volume backup", score=0.40),
        ]
        data = json.loads(await search_memory("qdrant volume backup", project="A", rerank=True))
        assert data["rerank"]["applied"] is True
        assert data["rerank"]["model"] == default
        assert data["rerank"]["model"] == reranking.rerank_config_status()["effective_model"]

    def test_fallback_status_reports_sdk_default_model_when_unset(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.05")
        _install(monkeypatch, _StubReranker(delay=0.5), provider="sentence_transformer")
        status = reranking.apply_rerank("q", [{"id": "a", "memory": "a", "score": 0.5}])
        assert status["applied"] is False
        assert status["reason"].startswith("timeout")
        assert status["model"] == reranking.SDK_DEFAULT_MODELS["sentence_transformer"]

    @pytest.mark.asyncio
    async def test_only_top_n_candidates_are_rescored(self, patched_client, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", str(DEFAULT_SEARCH_TOP_K + 5))
        stub = _StubReranker(preferred={"0"})
        _install(monkeypatch, stub)
        pool = DEFAULT_SEARCH_TOP_K + 30
        patched_client.vector_store.search.return_value = [
            _hit(str(i), f"m{i}", score=0.9 - i / 1000) for i in range(pool)
        ]

        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert len(stub.calls[0]) == DEFAULT_SEARCH_TOP_K + 5
        # The head handed over is the best of the blended ranking, in order.
        assert stub.calls[0][:3] == ["0", "1", "2"]
        assert data["rerank"]["reranked"] == DEFAULT_SEARCH_TOP_K + 5
        assert data["rerank"]["candidates"] == pool
        assert len(data["results"]) == DEFAULT_SEARCH_TOP_K
        assert all("rerank_score" in r for r in data["results"])

    def test_top_n_is_clamped_to_page_size(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", "3")
        assert reranking.rerank_top_n(page_size=DEFAULT_SEARCH_TOP_K) == DEFAULT_SEARCH_TOP_K
        monkeypatch.delenv("MEM0_RERANKER_TOP_N")
        assert reranking.rerank_top_n() == reranking.DEFAULT_TOP_N

    def test_tail_stays_after_head_in_original_order(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", "2")
        stub = _StubReranker(preferred={"b"})
        _install(monkeypatch, stub)
        results = [{"id": k, "memory": k, "score": 0.5} for k in ("a", "b", "c", "d")]
        status = reranking.apply_rerank("q", results)
        assert status["applied"] is True
        assert [r["id"] for r in results] == ["b", "a", "c", "d"]
        assert "rerank_score" not in results[2]


# --------------------------------------------------------------------------- #
# Fallbacks
# --------------------------------------------------------------------------- #
class TestFallback:
    @pytest.mark.asyncio
    async def test_timeout_keeps_original_order(self, patched_client, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.05")
        stub = _StubReranker(preferred={"b"}, delay=0.5)
        _install(monkeypatch, stub, model=ST_MODEL)
        patched_client.vector_store.search.return_value = [
            _hit("a", "a", score=0.9),
            _hit("b", "b", score=0.2),
        ]

        started = time.perf_counter()
        data = json.loads(await search_memory("q", project="A", rerank=True))
        assert time.perf_counter() - started < 0.45, "timeout must not wait for the model"

        assert data["rerank"]["applied"] is False
        assert data["rerank"]["reason"].startswith("timeout")
        assert data["rerank"]["model"] == ST_MODEL
        assert [r["id"] for r in data["results"]] == ["a", "b"]
        assert all("rerank_score" not in r for r in data["results"])

    @pytest.mark.asyncio
    async def test_error_keeps_original_order(self, patched_client, monkeypatch):
        boom = MagicMock()
        boom.rerank.side_effect = RuntimeError("cross-encoder quebrou")
        _install(monkeypatch, boom)
        patched_client.vector_store.search.return_value = [
            _hit("a", "a", score=0.9),
            _hit("b", "b", score=0.2),
        ]
        data = json.loads(await search_memory("q", project="A", rerank=True))
        assert data["rerank"]["applied"] is False
        assert "cross-encoder quebrou" in data["rerank"]["reason"]
        assert [r["id"] for r in data["results"]] == ["a", "b"]

    def test_sdk_swallowed_predict_error_is_not_reported_as_applied(
        self, monkeypatch, fake_sentence_transformers
    ):
        """The SDK provider hides predict() errors behind rerank_score=0.0."""
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        instance, _ = reranking.get_reranker()
        instance.model.predict = MagicMock(side_effect=RuntimeError("OOM"))
        results = [{"id": k, "memory": k, "score": 0.5} for k in ("a", "b")]
        status = reranking.apply_rerank("q", results)
        assert status["applied"] is False
        assert status["reason"].startswith("failed")
        assert all("rerank_score" not in r for r in results)

    @pytest.mark.asyncio
    async def test_model_loading_does_not_block_search(self, patched_client, monkeypatch):
        """First request while the model loads: original order, reason=loading."""
        release = threading.Event()

        def slow_build():
            release.wait(5)
            return _StubReranker(preferred={"b"}), None

        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setattr(reranking, "_build_reranker", slow_build)
        patched_client.vector_store.search.return_value = [
            _hit("a", "a", score=0.9),
            _hit("b", "b", score=0.2),
        ]
        try:
            data = json.loads(await search_memory("q", project="A", rerank=True))
            assert data["rerank"] == {
                "applied": False,
                "provider": "sentence_transformer",
                "reason": "loading",
            }
            assert [r["id"] for r in data["results"]] == ["a", "b"]
            assert reranking.rerank_config_status()["reason"] == "loading"
        finally:
            release.set()
        assert reranking.wait_for_reranker(5)
        data = json.loads(await search_memory("q", project="A", rerank=True))
        assert data["rerank"]["applied"] is True
        assert [r["id"] for r in data["results"]][0] == "b"

    def test_stale_background_load_is_discarded_after_reset(self, monkeypatch):
        release = threading.Event()

        def slow_build():
            release.wait(5)
            return _StubReranker(), None

        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setattr(reranking, "_build_reranker", slow_build)
        assert reranking.start_background_load(warm=False)
        thread = reranking._load_thread
        reranking.reset_reranker_cache()
        release.set()
        thread.join(5)
        assert reranking._loaded is False
        assert reranking._reranker is None


# --------------------------------------------------------------------------- #
# Warmup / status
# --------------------------------------------------------------------------- #
class TestWarmupAndStatus:
    def test_warmup_on_startup_loads_and_scores_once(self, monkeypatch, fake_sentence_transformers):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        assert reranking.warmup_on_startup() is True
        assert reranking.wait_for_reranker(5)
        encoder = fake_sentence_transformers.instances[-1]
        assert encoder.predict_calls, "warmup must run one scoring pass"
        assert reranking.rerank_config_status()["reason"] is None

    def test_warmup_disabled_defers_load(self, monkeypatch):
        build = MagicMock()
        monkeypatch.setattr(reranking, "_build_reranker", build)
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setenv("MEM0_RERANKER_WARMUP", "0")
        assert reranking.warmup_on_startup() is False
        build.assert_not_called()
        assert reranking.rerank_config_status()["reason"] == "not_loaded_yet"

    def test_status_exposes_effective_settings(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setenv("MEM0_RERANKER_MODEL", ST_MODEL)
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", "40")
        status = reranking.rerank_config_status()
        assert status["configured"] is True
        assert status["model"] == ST_MODEL
        assert status["device"] == "cpu"
        assert status["top_n"] == 40
        assert status["timeout_sec"] == reranking.DEFAULT_TIMEOUT_SEC


# --------------------------------------------------------------------------- #
# Packaging: optional dependency, default-off build arg
# --------------------------------------------------------------------------- #
class TestPackaging:
    def test_torch_not_in_main_requirements(self):
        from tests.paths import openmemory_root

        api = openmemory_root() / "api"
        main_req = (api / "requirements.txt").read_text(encoding="utf-8")
        assert "sentence-transformers" not in main_req
        assert "torch" not in main_req
        extra = (api / "requirements-rerank.txt").read_text(encoding="utf-8")
        assert "sentence-transformers" in extra

    def test_torch_comes_only_from_the_cpu_index(self):
        """--extra-index-url let pip pick the CUDA wheel from PyPI; --index-url cannot."""
        from tests.paths import openmemory_root

        api = openmemory_root() / "api"
        torch_req = (api / "requirements-rerank-torch.txt").read_text(encoding="utf-8")
        lines = [ln.strip() for ln in torch_req.splitlines() if ln.strip() and not ln.startswith("#")]
        assert "--index-url https://download.pytorch.org/whl/cpu" in lines
        assert not any(ln.startswith("--extra-index-url") for ln in lines)
        assert "torch>=2.4.0,<3" in lines
        extra = (api / "requirements-rerank.txt").read_text(encoding="utf-8")
        extra_lines = [ln.strip() for ln in extra.splitlines() if ln.strip() and not ln.startswith("#")]
        assert not any("index-url" in ln for ln in extra_lines)
        assert not any(ln.startswith("torch") for ln in extra_lines)

    def test_dockerfile_install_rerank_defaults_off(self):
        from tests.paths import openmemory_root

        dockerfile = (openmemory_root() / "api" / "Dockerfile").read_text(encoding="utf-8")
        assert "ARG INSTALL_RERANK=0" in dockerfile
        assert "requirements-rerank.txt" in dockerfile
        assert "HF_HOME=" in dockerfile
        # torch is installed in its own step, BEFORE sentence-transformers.
        torch_step = dockerfile.index("pip install --no-cache-dir -r requirements-rerank-torch.txt")
        st_step = dockerfile.index("pip install --no-cache-dir -r requirements-rerank.txt")
        assert torch_step < st_step
        assert "--extra-index-url" not in "".join(
            ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_compose_passes_build_arg_default_off_without_new_volume(self):
        yaml = pytest.importorskip("yaml")
        from tests.paths import openmemory_root

        compose = yaml.safe_load(
            (openmemory_root() / "docker-compose.scale.yml").read_text(encoding="utf-8")
        )
        args = compose["x-api-common"]["build"]["args"]
        assert args["INSTALL_RERANK"] == "${INSTALL_RERANK:-0}"
        assert not any("hf" in str(v).lower() for v in compose.get("volumes", {}) or {})


# --------------------------------------------------------------------------- #
# Hugging Face network policy (IMPORTANTE 1)
# --------------------------------------------------------------------------- #
class TestOfflinePolicy:
    def _load(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setenv("MEM0_RERANKER_MODEL", ST_MODEL)
        return reranking.get_reranker()

    def test_runtime_is_offline_by_default(self, monkeypatch, fake_sentence_transformers):
        instance, err = self._load(monkeypatch)
        assert err is None and instance is not None
        for name in reranking.HF_OFFLINE_ENV:
            assert os.environ[name] == "1", name
        assert reranking.downloads_allowed() is False
        assert reranking.rerank_config_status()["offline"] is True

    def test_local_only_overrides_inherited_online_env(self, monkeypatch, fake_sentence_transformers):
        monkeypatch.setenv("MEM0_LOCAL_ONLY", "1")
        monkeypatch.setenv("MEM0_RERANKER_ALLOW_DOWNLOAD", "1")  # ignored in local-only
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
        _, err = self._load(monkeypatch)
        assert err is None
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"

    def test_explicit_opt_out_allows_download_outside_local_only(
        self, monkeypatch, fake_sentence_transformers
    ):
        monkeypatch.setenv("MEM0_RERANKER_ALLOW_DOWNLOAD", "1")
        _, err = self._load(monkeypatch)
        assert err is None
        assert os.environ["HF_HUB_OFFLINE"] == "0"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "0"
        assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"

    def test_missing_model_is_unavailable_without_touching_the_hub(
        self, monkeypatch, fake_sentence_transformers
    ):
        monkeypatch.setattr(reranking, "_model_in_local_cache", lambda model: False)
        monkeypatch.setenv("MEM0_LOCAL_ONLY", "1")
        instance, err = self._load(monkeypatch)
        assert instance is None
        assert err.startswith("unavailable:")
        assert f"'{ST_MODEL}'" in err and "cache local" in err
        assert f"RERANK_PRELOAD_MODEL={ST_MODEL}" in err
        assert fake_sentence_transformers.instances == [], "CrossEncoder must not be built"

    def test_offline_load_error_maps_to_missing_model(self, monkeypatch, fake_sentence_transformers):
        class LocalEntryNotFoundError(Exception):
            pass

        def boom(*a, **k):
            raise OSError("We couldn't connect to 'https://huggingface.co'") from LocalEntryNotFoundError("x")

        monkeypatch.setattr(reranking, "_model_in_local_cache", lambda model: None)
        from mem0.reranker import sentence_transformer_reranker as st_mod

        monkeypatch.setattr(st_mod, "CrossEncoder", boom)
        _, err = self._load(monkeypatch)
        assert f"RERANK_PRELOAD_MODEL={ST_MODEL}" in err

    def test_sdk_default_model_is_named_when_unset(self, monkeypatch, fake_sentence_transformers):
        monkeypatch.setattr(reranking, "_model_in_local_cache", lambda model: False)
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        _, err = reranking.get_reranker()
        assert "RERANK_PRELOAD_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2" in err

    def test_main_sets_hf_offline_before_app_imports(self):
        from tests.paths import openmemory_root

        src = (openmemory_root() / "api" / "main.py").read_text(encoding="utf-8")
        first_app_import = src.index("from app.")
        block = src[:first_app_import]
        assert "MEM0_LOCAL_ONLY" in block
        for name in reranking.HF_OFFLINE_ENV:
            assert name in block, name

    def test_dockerfile_enables_hub_only_for_the_preload_run(self):
        from tests.paths import openmemory_root

        dockerfile = (openmemory_root() / "api" / "Dockerfile").read_text(encoding="utf-8")
        assert "HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0" in dockerfile
        # never as a persistent ENV of the runtime image
        assert not any(
            ln.strip().startswith("ENV") and "OFFLINE" in ln for ln in dockerfile.splitlines()
        )
        assert "torch>=2.4.0,<3" in (
            openmemory_root() / "api" / "requirements-rerank-torch.txt"
        ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# CPU threads (IMPORTANTE 2a)
# --------------------------------------------------------------------------- #
class TestThreads:
    def test_default_and_validation(self, monkeypatch):
        monkeypatch.setattr(reranking.os, "cpu_count", lambda: 8)
        assert reranking.reranker_threads() == 2
        monkeypatch.setenv("MEM0_RERANKER_THREADS", "abc")
        assert reranking.reranker_threads() == 2
        monkeypatch.setenv("MEM0_RERANKER_THREADS", "0")
        assert reranking.reranker_threads() == 1
        monkeypatch.setenv("MEM0_RERANKER_THREADS", "64")
        assert reranking.reranker_threads() == 8

    def test_loader_applies_torch_set_num_threads(self, monkeypatch, fake_sentence_transformers):
        fake_torch = SimpleNamespace(set_num_threads=MagicMock())
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setenv("MEM0_RERANKER_THREADS", "1")
        monkeypatch.setenv("MKL_NUM_THREADS", "3")  # operator value wins
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        _, err = reranking.get_reranker()
        assert err is None
        fake_torch.set_num_threads.assert_called_once_with(1)
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["MKL_NUM_THREADS"] == "3"
        assert reranking.rerank_config_status()["threads"] == 1


# --------------------------------------------------------------------------- #
# Admission control: busy fail-fast + circuit breaker (IMPORTANTE 2b/2c)
# --------------------------------------------------------------------------- #
class _GatedReranker:
    """Blocks inside ``rerank`` until released — a deterministic slow predict()."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def rerank(self, query, documents, top_k=None):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return [dict(d, rerank_score=float(i)) for i, d in enumerate(documents)]


def _docs():
    return [{"id": k, "memory": k, "score": 0.5} for k in ("a", "b", "c")]


def _wait_idle(timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reranking._get_gate().snapshot()["inflight"] == 0:
            return True
        time.sleep(0.005)
    return False


def _outcome(name):
    from app.utils.metrics import RERANK_OUTCOME

    return RERANK_OUTCOME.labels(outcome=name)._value.get()


class TestAdmission:
    def test_busy_answers_immediately_with_original_order(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "5")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        first: dict = {}
        worker = threading.Thread(target=lambda: first.update(reranking.apply_rerank("q", _docs())))
        worker.start()
        try:
            assert gated.started.wait(2)
            busy_before = _outcome("busy")
            docs = _docs()
            t0 = time.perf_counter()
            status = reranking.apply_rerank("q", docs)
            assert time.perf_counter() - t0 < 0.5, "busy must not wait for the running pass"
            assert status["applied"] is False and status["reason"] == "busy"
            assert [d["id"] for d in docs] == ["a", "b", "c"]
            assert gated.calls == 1, "busy call must not reach the model"
            assert _outcome("busy") == busy_before + 1
        finally:
            gated.release.set()
            worker.join(5)
        assert first["applied"] is True

    def test_abandoned_pass_keeps_its_slot_until_it_really_ends(self, monkeypatch):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.05")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            assert reranking.apply_rerank("q", _docs())["reason"].startswith("timeout")
            snap = reranking._get_gate().snapshot()
            assert snap["inflight"] == 1 and snap["abandoned_inflight"] == 1
            t0 = time.perf_counter()
            status = reranking.apply_rerank("q", _docs())
            # Before: this call queued behind the abandoned predict and timed out too.
            assert status["reason"] == "busy"
            assert time.perf_counter() - t0 < 0.04, "must not even wait the timeout"
        finally:
            gated.release.set()
        assert _wait_idle()
        snap = reranking._get_gate().snapshot()
        assert snap["abandoned_inflight"] == 0
        assert reranking.apply_rerank("q", _docs())["applied"] is True

    def test_timeout_budget_includes_the_whole_wait(self, monkeypatch):
        """The budget is wall clock from submit — no hidden queue time on top."""
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.1")
        monkeypatch.setenv("MEM0_RERANKER_MAX_CONCURRENCY", "2")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        out: list = []

        def call():
            t0 = time.perf_counter()
            status = reranking.apply_rerank("q", _docs())
            out.append((status, time.perf_counter() - t0))

        threads = [threading.Thread(target=call) for _ in range(2)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(2)
            # Admitted work never waits in an executor queue (workers == slots), so
            # each caller is answered within ITS budget, measured from its own call.
            assert reranking._get_executor()._max_workers == reranking._get_gate().capacity == 2
            assert [s["reason"] for s, _ in out] == ["timeout: exceeded 0.1s"] * 2
            assert all(elapsed < 0.35 for _, elapsed in out)
            # Both slots held by abandoned passes -> the third is refused, not queued.
            assert reranking.apply_rerank("q", _docs())["reason"] == "busy"
        finally:
            gated.release.set()
        assert _wait_idle()


class TestCircuitBreaker:
    @pytest.fixture
    def clock(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(reranking, "_clock", lambda: now[0])
        return now

    def _timeout_once(self, gated):
        status = reranking.apply_rerank("q", _docs())
        gated.release.set()  # let the abandoned pass end so the slot frees
        assert _wait_idle()
        gated.release.clear()
        return status

    def test_opens_after_threshold_then_half_open_probe_closes(self, monkeypatch, clock):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "2")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_COOLDOWN_SEC", "60")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            assert self._timeout_once(gated)["reason"].startswith("timeout")
            assert reranking._get_gate().snapshot()["state"] == "closed"
            assert self._timeout_once(gated)["reason"].startswith("timeout")
            snap = reranking._get_gate().snapshot()
            assert snap["state"] == "open" and snap["opened_total"] == 1

            calls, opened_before = gated.calls, _outcome("circuit_open")
            t0 = time.perf_counter()
            status = reranking.apply_rerank("q", _docs())
            assert time.perf_counter() - t0 < 0.02
            assert status["reason"] == "circuit_open"
            assert gated.calls == calls, "open breaker must not call the model"
            assert _outcome("circuit_open") == opened_before + 1

            clock[0] += 59
            assert reranking.apply_rerank("q", _docs())["reason"] == "circuit_open"
            clock[0] += 2  # cooldown over -> half-open, exactly one probe
            assert reranking._get_gate().snapshot()["state"] == "half_open"

            monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "5")
            probe: dict = {}
            gated.started.clear()  # set by the earlier passes; wait for THE probe
            worker = threading.Thread(target=lambda: probe.update(reranking.apply_rerank("q", _docs())))
            worker.start()
            assert gated.started.wait(2)
            # While the probe runs, everyone else still skips.
            assert reranking.apply_rerank("q", _docs())["reason"] == "circuit_open"
            gated.release.set()
            worker.join(5)
            assert probe["applied"] is True
            snap = reranking._get_gate().snapshot()
            assert snap["state"] == "closed" and snap["consecutive_timeouts"] == 0
        finally:
            gated.release.set()

    def test_probe_timeout_reopens(self, monkeypatch, clock):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "1")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            self._timeout_once(gated)
            assert reranking._get_gate().snapshot()["state"] == "open"
            clock[0] += reranking.DEFAULT_BREAKER_COOLDOWN_SEC + 1
            assert self._timeout_once(gated)["reason"].startswith("timeout")  # the probe
            snap = reranking._get_gate().snapshot()
            assert snap["state"] == "open" and snap["opened_total"] == 2
            assert reranking.apply_rerank("q", _docs())["reason"] == "circuit_open"
        finally:
            gated.release.set()

    def test_success_resets_consecutive_count(self, monkeypatch, clock):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "2")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            self._timeout_once(gated)
            gated.release.set()
            assert reranking.apply_rerank("q", _docs())["applied"] is True
            gated.release.clear()
            self._timeout_once(gated)
            assert reranking._get_gate().snapshot()["state"] == "closed"
        finally:
            gated.release.set()

    def test_admin_endpoint_exposes_breaker(self, monkeypatch, clock):
        from app.routers.admin import rerank_admin

        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "1")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            body = rerank_admin()
            assert body["breaker"]["state"] == "closed"
            assert body["breaker"]["max_concurrency"] == 1
            self._timeout_once(gated)
            body = rerank_admin()
            assert body["breaker"]["state"] == "open"
            assert body["breaker"]["reopens_in_sec"] == reranking.DEFAULT_BREAKER_COOLDOWN_SEC
            assert "circuit breaker OPEN" in body["message"]
        finally:
            gated.release.set()

    def test_probe_timeout_log_does_not_quote_growing_count(self, monkeypatch, clock):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "1")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            self._timeout_once(gated)
            clock[0] += reranking.DEFAULT_BREAKER_COOLDOWN_SEC + 1
            # Spy instead of caplog: other suites reconfigure logging propagation.
            spy = MagicMock()
            monkeypatch.setattr(reranking, "logger", spy)
            self._timeout_once(gated)  # the probe
            msgs = [c.args[0] % c.args[1:] for c in spy.warning.call_args_list]
            breaker_msgs = [m for m in msgs if "circuit breaker" in m]
            assert breaker_msgs == ["rerank circuit breaker: half-open probe timed out; reopening for 60s"]
            assert not any("consecutive" in m for m in msgs)
        finally:
            gated.release.set()

    # Regression: an exception between admit() and submit() leaked the slot and,
    # for the half-open probe, pinned probe_in_flight (circuit_open until restart).
    def test_non_dict_candidate_never_takes_a_slot(self, monkeypatch, clock):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "1")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            self._timeout_once(gated)
            clock[0] += reranking.DEFAULT_BREAKER_COOLDOWN_SEC + 1  # next call = probe
            bad = [{"id": "a", "memory": "a"}, object(), {"id": "c", "memory": "c"}]
            snapshot = list(bad)
            status = reranking.apply_rerank("q", bad)
            assert status["applied"] is False and status["reason"].startswith("failed:")
            assert bad == snapshot, "original order is kept"
            gate = reranking._get_gate()
            assert gate.snapshot()["inflight"] == 0
            assert gate.probe_in_flight is False
            assert gate.snapshot()["state"] == "half_open", "bad input must not burn the probe"
            gated.release.set()
            assert reranking.apply_rerank("q", _docs())["applied"] is True  # probe still usable
            assert gate.snapshot()["state"] == "closed"
        finally:
            gated.release.set()

    @pytest.mark.parametrize("where", ["submit", "add_done_callback"])
    def test_submit_error_frees_slot_and_probe(self, monkeypatch, clock, where):
        monkeypatch.setenv("MEM0_RERANKER_TIMEOUT_SEC", "0.02")
        monkeypatch.setenv("MEM0_RERANKER_BREAKER_THRESHOLD", "1")
        gated = _GatedReranker()
        _install(monkeypatch, gated)
        try:
            self._timeout_once(gated)
            clock[0] += reranking.DEFAULT_BREAKER_COOLDOWN_SEC + 1  # next call = probe

            class _BrokenFuture:
                def add_done_callback(self, fn):
                    raise RuntimeError("callback boom")

            class _BrokenExecutor:
                def submit(self, *a, **k):
                    if where == "submit":
                        raise RuntimeError("cannot schedule new futures after shutdown")
                    return _BrokenFuture()

            with patch.object(reranking, "_get_executor", return_value=_BrokenExecutor()):
                docs = _docs()
                status = reranking.apply_rerank("q", docs)
            assert status["applied"] is False and status["reason"].startswith("failed:")
            assert [d["id"] for d in docs] == ["a", "b", "c"]
            gate = reranking._get_gate()
            assert gate.snapshot()["inflight"] == 0
            assert gate.probe_in_flight is False
            # The failed probe reopened the breaker; after the cooldown a new probe passes.
            assert gate.snapshot()["state"] == "open"
            clock[0] += reranking.DEFAULT_BREAKER_COOLDOWN_SEC + 1
            gated.release.set()
            assert reranking.apply_rerank("q", _docs())["applied"] is True
            assert gate.snapshot()["state"] == "closed"
        finally:
            gated.release.set()


# --------------------------------------------------------------------------- #
# Minor fixes
# --------------------------------------------------------------------------- #
class TestMinor:
    def test_normalization_floor_keeps_last_item_liftable(self):
        docs = [
            {"id": "a", "rerank_score": 5.0, "score": 0.1},
            {"id": "b", "rerank_score": -2.0, "score": 0.9},
            {"id": "c", "rerank_score": 1.5, "score": 0.5},
        ]
        reranking._normalize_into_score(docs)
        by_id = {d["id"]: d for d in docs}
        assert by_id["a"]["score"] == pytest.approx(1.0)
        assert by_id["b"]["score"] == pytest.approx(reranking.RERANK_SCORE_FLOOR)
        assert by_id["c"]["score"] == pytest.approx(0.05 + 0.95 * 0.5)
        assert by_id["b"]["semantic_score"] == 0.9

    def test_background_load_crash_never_sticks_in_loading(self, monkeypatch):
        def crash():
            raise RuntimeError("segfault-ish")

        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "sentence_transformer")
        monkeypatch.setattr(reranking, "_build_reranker", crash)
        assert reranking.start_background_load(warm=False)
        assert reranking.wait_for_reranker(5)
        status = reranking.rerank_config_status()
        assert status["reason"].startswith("unavailable: loader crashed")
        assert reranking.apply_rerank("q", _docs())["reason"].startswith("unavailable")
