"""Tests for the global, user_id-agnostic MCP read tools (task_03).

These cover `search_memory` and `list_memories` from ``app.mcp_server`` and assert
the shared-read behavior mandated by ADR-003:

- semantic search is GLOBAL (no hard project filter); ``project`` is a soft ranking
  hint only — relevance + recency dominate ordering;
- reads NEVER filter by ``user_id`` (shared across all machines on the local
  network — the hostname only feeds attribution on writes);
- a bounded default ``top_k`` is applied; results are ordered by semantic score
  blended with recency (``updated_at`` → ``created_at``);
- ``list_memories`` remains project-scoped for enumeration;
- the memory client is reused via ``get_memory_client_safe`` (no per-call reconnect).

The memory client is fully mocked, so these run without Qdrant/Ollama/LLM access.
"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Set a dummy key before importing modules that may build a client lazily.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest

from app import mcp_server
from app.utils import recency
from app.mcp_server import (
    DEFAULT_LIST_TOP_K,
    DEFAULT_SEARCH_CANDIDATE_K,
    DEFAULT_SEARCH_TOP_K,
    list_memories,
    search_memory,
)


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
def _hit(mem_id, data, project, score=0.9, **payload):
    """Build a fake vector-store search hit (OutputData-like)."""
    base = {"data": data, "project": project, "hash": f"h-{mem_id}"}
    base.update(payload)
    return SimpleNamespace(id=mem_id, score=score, payload=base)


def _point(mem_id, data, project, **payload):
    """Build a fake vector-store list point (scroll-like)."""
    base = {"data": data, "project": project, "hash": f"h-{mem_id}"}
    base.update(payload)
    return SimpleNamespace(id=mem_id, payload=base)


def _make_client(search_return=None, list_return=None):
    """Create a mocked memory client with embedding + vector store."""
    client = MagicMock()
    client.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    client.vector_store.search.return_value = search_return or []
    client.vector_store.list.return_value = list_return or []
    client.vector_store.collection_name = "openmemory"
    # Mirror real Qdrant merge so scroll gets a filter dict.
    from mem0.vector_stores.qdrant import Qdrant

    client.vector_store._merge_governance_filters = (
        lambda filters, include_obsolete=False: Qdrant._merge_governance_filters(
            client.vector_store, filters, include_obsolete=include_obsolete
        )
    )
    client.vector_store._create_filter = (
        lambda filters: filters  # pass-through for assertions
    )
    points = list_return
    if isinstance(list_return, tuple):
        points = list_return[0]
    client.vector_store.client.scroll.return_value = (points or [], None)
    return client


@pytest.fixture
def patched_client():
    """Patch get_memory_client_safe to return a fresh mocked client."""
    client = _make_client()
    client.embedding_model.model = "test-embed-model"
    with (
        patch.object(mcp_server, "get_memory_client_safe", return_value=client) as p,
        patch.object(mcp_server, "bind_active_collection"),
        patch.object(mcp_server.read_cache, "get_search", return_value=None),
        patch.object(mcp_server.read_cache, "set_search"),
        patch.object(mcp_server.read_cache, "get_embedding", return_value=None),
        patch.object(mcp_server.read_cache, "set_embedding"),
    ):
        yield client, p


# --------------------------------------------------------------------------- #
# search_memory — global search, no user_id, project as ranking hint
# --------------------------------------------------------------------------- #
class TestSearchMemoryProjectScope:
    @pytest.mark.asyncio
    async def test_search_does_not_hard_filter_by_project(self, patched_client):
        client, _ = patched_client
        client.vector_store.search.return_value = [_hit("1", "coffee", "A")]

        mcp_server.user_id_var.set("maqA")
        mcp_server.client_name_var.set("cursor")

        out = await search_memory("coffee", project="A")
        data = json.loads(out)

        filters = client.vector_store.search.call_args.kwargs["filters"]
        assert filters is None
        assert client.vector_store.search.call_args.kwargs["shard_key_selector"] is None
        assert data["results"][0]["memory"] == "coffee"
        assert data["results"][0]["project"] == "A"

    @pytest.mark.asyncio
    async def test_search_does_not_filter_by_user_id(self, patched_client):
        client, _ = patched_client
        client.vector_store.search.return_value = []

        mcp_server.user_id_var.set("maqA")
        mcp_server.client_name_var.set("cursor")

        await search_memory("anything", project="A")

        filters = client.vector_store.search.call_args.kwargs["filters"]
        assert "user_id" not in (filters or {})
        assert filters is None

    @pytest.mark.asyncio
    async def test_search_overfetches_candidates_before_ranking(self, patched_client):
        """The vector store is queried for the wider candidate pool, not the page.

        Ranking blends score with recency/project/group and can only promote what
        was retrieved, so retrieval must be wider than the returned page.
        """
        client, _ = patched_client
        await search_memory("q", project="A")
        assert (
            client.vector_store.search.call_args.kwargs["top_k"]
            == DEFAULT_SEARCH_CANDIDATE_K
        )
        assert DEFAULT_SEARCH_TOP_K == 20
        assert DEFAULT_SEARCH_CANDIDATE_K > DEFAULT_SEARCH_TOP_K

    @pytest.mark.asyncio
    async def test_search_truncates_page_to_top_k(self, patched_client):
        """More candidates than a page → response is cut to DEFAULT_SEARCH_TOP_K."""
        client, _ = patched_client
        client.vector_store.search.return_value = [
            _hit(str(i), f"m{i}", "A", score=0.9 - i / 1000)
            for i in range(DEFAULT_SEARCH_TOP_K + 15)
        ]

        out = await search_memory("q", project="A")
        data = json.loads(out)

        assert len(data["results"]) == DEFAULT_SEARCH_TOP_K

    @pytest.mark.asyncio
    async def test_recent_candidate_outside_raw_top_k_reaches_the_page(
        self, patched_client
    ):
        """Regression: re-ranking used to happen AFTER truncation.

        A fresh memory whose raw score puts it beyond the page must still surface,
        because the recency boost is applied to the whole candidate pool first.
        """
        client, _ = patched_client
        now = datetime.now(timezone.utc).isoformat()
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()

        stale = [
            _hit(f"stale{i}", f"stale{i}", "A", score=0.90 - i / 1000, updated_at=old)
            for i in range(DEFAULT_SEARCH_TOP_K + 10)
        ]
        fresh = _hit("fresh", "fresh", "A", score=0.60, updated_at=now)
        corpus = stale + [fresh]

        # Faithful vector-store behavior: honor top_k, best raw score first. Without
        # this the mock would hand back the whole corpus and hide the truncation bug.
        def _search(*_args, top_k, **_kwargs):
            return sorted(corpus, key=lambda h: h.score, reverse=True)[:top_k]

        client.vector_store.search.side_effect = _search

        out = await search_memory("q", project="A")
        data = json.loads(out)

        returned = [r["id"] for r in data["results"]]
        assert "fresh" in returned, "recency boost must survive truncation"
        assert returned[0] == "fresh"

    @pytest.mark.asyncio
    async def test_results_expose_the_score_that_decided_the_order(
        self, patched_client
    ):
        """Raw ``score`` alone cannot explain the order; the blend must be visible.

        Without this, a result ranked by recency/project/group looks out of order
        to anyone reading the response, and the ranking cannot be calibrated.
        """
        client, _ = patched_client
        now = datetime.now(timezone.utc).isoformat()
        client.vector_store.search.return_value = [
            _hit("old", "old", "A", score=0.95, updated_at="2020-01-01T00:00:00+00:00"),
            _hit("new", "new", "A", score=0.80, updated_at=now),
        ]

        data = json.loads(await search_memory("q", project="A"))
        by_id = {r["id"]: r for r in data["results"]}

        # Raw score is untouched; the effective one explains the inversion.
        assert by_id["old"]["score"] == 0.95
        assert by_id["new"]["score"] == 0.80
        assert by_id["new"]["effective_score"] > by_id["old"]["effective_score"]

        factors = by_id["new"]["ranking_factors"]
        assert set(factors) == {"recency", "project", "group", "lexical"}
        # project="A" matches exactly → the documented exact-match boost.
        assert factors["project"] == pytest.approx(1.0 + recency.SEARCH_PROJECT_BOOST_EXACT)
        assert by_id["new"]["effective_score"] == pytest.approx(
            0.80 * factors["recency"] * factors["project"] * factors["group"]
        )
        # A fact updated years ago must be discounted against one updated today.
        assert factors["recency"] > by_id["old"]["ranking_factors"]["recency"]

    @pytest.mark.asyncio
    async def test_search_orders_by_score_without_timestamps(self, patched_client):
        client, _ = patched_client
        # With no timestamps, recency is neutral and order falls back to score.
        client.vector_store.search.return_value = [
            _hit("low", "low", "A", score=0.1),
            _hit("high", "high", "A", score=0.9),
        ]
        out = await search_memory("q", project="A")
        ids = [r["id"] for r in json.loads(out)["results"]]
        assert ids == ["high", "low"]

    @pytest.mark.asyncio
    async def test_search_recency_outranks_older_more_similar(self, patched_client):
        client, _ = patched_client
        # The "old" fact is a closer semantic match (higher score) but was last
        # changed years ago. The "new" fact is less similar but was UPDATED today
        # (despite an old created_at) — recency must surface it first. This is the
        # ADR-003 "recent wins" rule applied at read time, keyed off updated_at.
        now = datetime.now(timezone.utc)
        client.vector_store.search.return_value = [
            _hit(
                "old", "old fact", "A", score=0.95,
                created_at="2020-01-01T00:00:00+00:00",
                updated_at="2020-01-01T00:00:00+00:00",
            ),
            _hit(
                "new", "new fact", "A", score=0.80,
                created_at="2019-01-01T00:00:00+00:00",
                updated_at=now.isoformat(),
            ),
        ]
        out = await search_memory("q", project="A")
        ids = [r["id"] for r in json.loads(out)["results"]]
        assert ids == ["new", "old"]

    @pytest.mark.asyncio
    async def test_search_recency_weight_zero_is_pure_score(self, patched_client, monkeypatch):
        client, _ = patched_client
        # Disabling recency (weight 0) restores pure semantic ordering even when a
        # less-similar fact is far more recent.
        monkeypatch.setattr(recency, "SEARCH_RECENCY_WEIGHT", 0.0)
        now = datetime.now(timezone.utc)
        client.vector_store.search.return_value = [
            _hit("old", "old", "A", score=0.95, updated_at="2020-01-01T00:00:00+00:00"),
            _hit("new", "new", "A", score=0.80, updated_at=now.isoformat()),
        ]
        out = await search_memory("q", project="A")
        ids = [r["id"] for r in json.loads(out)["results"]]
        assert ids == ["old", "new"]

    @pytest.mark.asyncio
    async def test_search_requires_project(self, patched_client):
        out = await search_memory("q", project="")
        assert "project not provided" in out

    @pytest.mark.asyncio
    async def test_search_client_unavailable(self):
        with patch.object(mcp_server, "get_memory_client_safe", return_value=None):
            out = await search_memory("q", project="A")
        assert "unavailable" in out

    @pytest.mark.asyncio
    async def test_search_handles_backend_error(self, patched_client):
        client, _ = patched_client
        client.vector_store.search.side_effect = RuntimeError("qdrant down")
        out = await search_memory("q", project="A")
        assert "Error searching memory" in out

    @pytest.mark.asyncio
    async def test_search_strict_project_hard_filters(self, patched_client):
        client, _ = patched_client
        client.vector_store.search.return_value = [_hit("1", "coffee", "A")]
        await search_memory("coffee", project="A", strict_project=True)
        filters = client.vector_store.search.call_args.kwargs["filters"]
        assert filters == {"project": "A"}

    @pytest.mark.asyncio
    async def test_search_reuses_client_no_reconnect(self, patched_client):
        client, getter = patched_client
        await search_memory("q1", project="A")
        await search_memory("q2", project="A")
        # One get per call, but each returns the SAME reused singleton client.
        assert getter.call_count == 2
        assert all(c.args == () for c in getter.call_args_list)


# --------------------------------------------------------------------------- #
# list_memories — project filter, no user_id
# --------------------------------------------------------------------------- #
class TestListMemoriesProjectScope:
    @pytest.mark.asyncio
    async def test_list_scoped_to_project(self, patched_client):
        client, _ = patched_client
        client.vector_store.client.scroll.return_value = (
            [_point("1", "m1", "A"), _point("2", "m2", "A")],
            None,
        )
        mcp_server.user_id_var.set("maqA")
        out = await list_memories(project="A")
        data = json.loads(out)

        scroll_kwargs = client.vector_store.client.scroll.call_args.kwargs
        scroll_filter = scroll_kwargs["scroll_filter"]
        # Governance merge wraps project filter; project must still be present.
        assert "project" in str(scroll_filter) or (
            isinstance(scroll_filter, dict)
            and (
                scroll_filter.get("project") == "A"
                or "A" in json.dumps(scroll_filter)
            )
        )
        assert {r["id"] for r in data["results"]} == {"1", "2"}
        assert data["total"] == 2
        assert data["project"] == "A"

    @pytest.mark.asyncio
    async def test_list_does_not_filter_by_user_id(self, patched_client):
        client, _ = patched_client
        mcp_server.user_id_var.set("maqA")
        await list_memories(project="A")
        scroll_filter = client.vector_store.client.scroll.call_args.kwargs["scroll_filter"]
        assert "user_id" not in (scroll_filter or {})

    @pytest.mark.asyncio
    async def test_list_applies_default_top_k(self, patched_client):
        client, _ = patched_client
        await list_memories(project="A")
        assert client.vector_store.client.scroll.call_args.kwargs["limit"] <= DEFAULT_LIST_TOP_K
        assert DEFAULT_LIST_TOP_K == 200

    @pytest.mark.asyncio
    async def test_list_unwraps_tuple_return(self, patched_client):
        client, _ = patched_client
        # Qdrant scroll returns (points, next_page_offset).
        client.vector_store.client.scroll.return_value = (
            [_point("1", "m1", "A")],
            None,
        )
        out = await list_memories(project="A")
        data = json.loads(out)
        assert data["results"][0]["id"] == "1"

    @pytest.mark.asyncio
    async def test_list_requires_project(self, patched_client):
        out = await list_memories(project="")
        assert "project not provided" in out

    @pytest.mark.asyncio
    async def test_list_client_unavailable(self):
        with patch.object(mcp_server, "get_memory_client_safe", return_value=None):
            out = await list_memories(project="A")
        assert "unavailable" in out

    @pytest.mark.asyncio
    async def test_list_handles_backend_error(self, patched_client):
        client, _ = patched_client
        client.vector_store.client.scroll.side_effect = RuntimeError("scroll failed")
        out = await list_memories(project="A")
        assert "Error getting memories" in out


# --------------------------------------------------------------------------- #
# Cross-host shared read (integration at the mock level) — ADR-003
# --------------------------------------------------------------------------- #
class TestSharedReadAcrossHosts:
    @pytest.mark.asyncio
    async def test_memory_written_by_maqA_readable_as_maqB(self, patched_client):
        """A memory in project A (written by maqA) is returned for a search run
        as maqB in project A — because the read filter carries no user_id."""
        client, _ = patched_client
        # Memory carries user_id=maqA in its payload (write-time attribution).
        client.vector_store.search.return_value = [
            _hit("m-A", "shared fact", "A", user_id="maqA")
        ]

        # Search executed as host maqB.
        mcp_server.user_id_var.set("maqB")
        mcp_server.client_name_var.set("windsurf")

        out = await search_memory("shared", project="A")
        data = json.loads(out)

        filters = client.vector_store.search.call_args.kwargs["filters"]
        assert "user_id" not in (filters or {})  # cross-host read is not blocked
        assert data["results"][0]["id"] == "m-A"
        assert data["results"][0]["memory"] == "shared fact"

    @pytest.mark.asyncio
    async def test_search_includes_cross_project_results(self, patched_client):
        """Wrong project hint must not exclude relevant memories from other projects."""
        client, _ = patched_client
        client.vector_store.search.return_value = [
            _hit("m-A", "shared fact", "A", score=0.95),
        ]

        mcp_server.user_id_var.set("maqB")
        out = await search_memory("shared", project="B")
        data = json.loads(out)

        assert data["results"][0]["id"] == "m-A"
        assert data["results"][0]["project"] == "A"

    @pytest.mark.asyncio
    async def test_search_project_hint_boosts_matching_project(self, patched_client, monkeypatch):
        """When scores are close, matching project name gets a small ranking boost."""
        monkeypatch.setattr(recency, "SEARCH_RECENCY_WEIGHT", 0.0)
        now = datetime.now(timezone.utc).isoformat()
        client, _ = patched_client
        client.vector_store.search.return_value = [
            _hit("other", "other fact", "other-repo", score=0.90, updated_at=now),
            _hit("mine", "mine fact", "mem0-shared", score=0.88, updated_at=now),
        ]

        out = await search_memory("fact", project="mem0-shared")
        ids = [r["id"] for r in json.loads(out)["results"]]
        assert ids == ["mine", "other"]
