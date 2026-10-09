"""Tests for the opt-in cross-encoder reranking on the MCP search path.

``rerank`` used to be a declared-but-unread parameter: callers asking for it got
plain vector ordering and no indication that nothing had happened. These tests
pin the two properties that fix demands:

- when a reranker IS configured, it reorders the candidate pool;
- when it is NOT (or it blows up), search still works AND the response says so.

The memory client and the reranker are mocked, so these run without Qdrant,
Ollama or any cross-encoder model.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest

from app import mcp_server
from app.mcp_server import DEFAULT_SEARCH_TOP_K, search_memory
from app.utils import reranking


def _hit(mem_id, data, project="A", score=0.9, **payload):
    base = {"data": data, "project": project, "hash": f"h-{mem_id}"}
    base.update(payload)
    return SimpleNamespace(id=mem_id, score=score, payload=base)


@pytest.fixture(autouse=True)
def _clear_reranker_cache():
    reranking.reset_reranker_cache()
    yield
    reranking.reset_reranker_cache()


@pytest.fixture
def patched_client():
    client = MagicMock()
    client.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    client.embedding_model.model = "test-embed-model"
    client.vector_store.search.return_value = []
    client.vector_store.collection_name = "openmemory"
    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=client),
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server.read_cache, "get_search", return_value=None),
        patch.object(mcp_server.read_cache, "set_search"),
        patch.object(mcp_server.read_cache, "get_embedding", return_value=None),
        patch.object(mcp_server.read_cache, "set_embedding"),
    ):
        yield client


class _StubReranker:
    """Cross-encoder stand-in: scores by position in ``preferred``."""

    def __init__(self, preferred):
        self.preferred = preferred
        self.calls = []

    def rerank(self, query, documents, top_k=None):
        self.calls.append((query, len(documents)))
        out = []
        for doc in documents:
            d = dict(doc)
            # Unbounded logits, negatives included — mirrors a real cross-encoder
            # and exercises the normalization before the multiplicative boosts.
            d["rerank_score"] = 8.0 if doc["id"] in self.preferred else -3.5
            out.append(d)
        out.sort(key=lambda d: d["rerank_score"], reverse=True)
        return out


class TestRerankReporting:
    @pytest.mark.asyncio
    async def test_absent_flag_keeps_response_clean(self, patched_client):
        """No rerank asked → no rerank key; existing consumers are unaffected."""
        patched_client.vector_store.search.return_value = [_hit("1", "m1")]
        out = await search_memory("q", project="A")
        assert "rerank" not in json.loads(out)

    @pytest.mark.asyncio
    async def test_unconfigured_reports_not_applied(self, patched_client, monkeypatch):
        """The regression this fixes: asking for rerank without a provider.

        The old code accepted the flag and silently ignored it. Now the caller is
        told, and still gets results.
        """
        monkeypatch.delenv("MEM0_RERANKER_PROVIDER", raising=False)
        patched_client.vector_store.search.return_value = [_hit("1", "m1")]

        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert data["rerank"]["applied"] is False
        assert data["rerank"]["reason"] == "not_configured"
        assert data["rerank"]["provider"] is None
        assert len(data["results"]) == 1

    @pytest.mark.asyncio
    async def test_rerank_config_status_not_configured(self, monkeypatch):
        monkeypatch.delenv("MEM0_RERANKER_PROVIDER", raising=False)
        reranking.reset_reranker_cache()
        status = reranking.rerank_config_status()
        assert status == {
            "configured": False,
            "provider": None,
            "reason": "not_configured",
        }

    @pytest.mark.asyncio
    async def test_provider_that_fails_to_load_reports_reason(
        self, patched_client, monkeypatch
    ):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "nao-existe")
        patched_client.vector_store.search.return_value = [_hit("1", "m1")]

        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert data["rerank"]["applied"] is False
        assert "unavailable" in data["rerank"]["reason"]
        assert len(data["results"]) == 1, "search must survive a broken reranker"

    @pytest.mark.asyncio
    async def test_reranker_exception_degrades_gracefully(
        self, patched_client, monkeypatch
    ):
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "stub")
        boom = MagicMock()
        boom.rerank.side_effect = RuntimeError("modelo fora do ar")
        monkeypatch.setattr(reranking, "_reranker", boom)
        monkeypatch.setattr(reranking, "_loaded", True)
        monkeypatch.setattr(reranking, "_load_error", None)

        patched_client.vector_store.search.return_value = [_hit("1", "m1")]
        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert data["rerank"]["applied"] is False
        assert "modelo fora do ar" in data["rerank"]["reason"]
        assert len(data["results"]) == 1


class TestRerankOrdering:
    @pytest.mark.asyncio
    async def test_configured_reranker_reorders_results(
        self, patched_client, monkeypatch
    ):
        """A low-vector-score memory the cross-encoder likes must come first."""
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "stub")
        stub = _StubReranker(preferred={"underdog"})
        monkeypatch.setattr(reranking, "_reranker", stub)
        monkeypatch.setattr(reranking, "_loaded", True)
        monkeypatch.setattr(reranking, "_load_error", None)

        patched_client.vector_store.search.return_value = [
            _hit("top", "top", score=0.95),
            _hit("mid", "mid", score=0.80),
            _hit("underdog", "underdog", score=0.20),
        ]

        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert data["rerank"]["applied"] is True
        assert data["rerank"]["provider"] == "stub"
        assert [r["id"] for r in data["results"]][0] == "underdog"

    @pytest.mark.asyncio
    async def test_rerank_sees_more_than_the_page(self, patched_client, monkeypatch):
        """Reranking runs before the page cut, not on the already-trimmed page.

        Since card 80110071 only the best MEM0_RERANKER_TOP_N of the blended
        ranking are rescored (bounded CPU cost); with TOP_N above the pool size the
        whole pool still reaches the cross-encoder.
        """
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "stub")
        monkeypatch.setenv("MEM0_RERANKER_TOP_N", "1000")
        stub = _StubReranker(preferred=set())
        monkeypatch.setattr(reranking, "_reranker", stub)
        monkeypatch.setattr(reranking, "_loaded", True)
        monkeypatch.setattr(reranking, "_load_error", None)

        pool = DEFAULT_SEARCH_TOP_K + 12
        patched_client.vector_store.search.return_value = [
            _hit(str(i), f"m{i}", score=0.9 - i / 1000) for i in range(pool)
        ]

        data = json.loads(await search_memory("q", project="A", rerank=True))

        assert stub.calls[0][1] == pool
        assert len(data["results"]) == DEFAULT_SEARCH_TOP_K

    @pytest.mark.asyncio
    async def test_raw_scores_are_preserved_and_normalized(
        self, patched_client, monkeypatch
    ):
        """Negative logits must not reach the multiplicative boosts."""
        monkeypatch.setenv("MEM0_RERANKER_PROVIDER", "stub")
        stub = _StubReranker(preferred={"a"})
        monkeypatch.setattr(reranking, "_reranker", stub)
        monkeypatch.setattr(reranking, "_loaded", True)
        monkeypatch.setattr(reranking, "_load_error", None)

        patched_client.vector_store.search.return_value = [
            _hit("a", "a", score=0.30),
            _hit("b", "b", score=0.70),
        ]

        data = json.loads(await search_memory("q", project="A", rerank=True))
        by_id = {r["id"]: r for r in data["results"]}

        assert by_id["a"]["rerank_score"] == 8.0
        assert by_id["b"]["rerank_score"] == -3.5
        assert by_id["a"]["semantic_score"] == 0.30
        assert by_id["b"]["semantic_score"] == 0.70
        assert all(0.0 <= by_id[k]["score"] <= 1.0 for k in ("a", "b"))
