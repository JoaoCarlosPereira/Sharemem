"""Tests for the cloud-compatible /v3/memories shim used by local-only hooks.

Asserts the contract the plugin hooks rely on, backed by a fake memory client:
  - search is global (shared; user_id ignored); app_id is a soft ranking hint;
    metadata type/threshold post-filter still apply;
  - add concatenates messages, scopes by app_id=project, and preserves the
    hook-supplied metadata (type/file) by calling client.add directly;
  - list returns a count + results and remains project-scoped.
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

# Path-load the router WITHOUT importing app.routers.__init__ (heavy deps /
# import-time OpenAI client). Stub only the heavy utils the router imports;
# keep real ``app`` / ``app.utils`` packages so lightweight deps
# (identity, groups, write_guard, read_audit, recency) resolve normally.
#
# Save originals so stubs are torn down after load — later test files that
# import app.utils.memory must get the real module, not the bare stub.
import app.database  # noqa: E402, F401 — prime DB before models↔read_audit cycle

_stub_names = (
    "app.utils.memory",
    "app.utils.partitioning",
)
_saved_modules = {n: sys.modules.get(n) for n in _stub_names}
for _name in _stub_names:
    sys.modules[_name] = types.ModuleType(_name)
sys.modules["app.utils.memory"].get_memory_client = lambda: None
# Partition routing is exercised in dedicated tests; here it is a no-op so the
# router's contract (search/add/list shapes) can be asserted with a fake client.
sys.modules["app.utils.partitioning"].bind_active_collection = lambda *a, **k: "openmemory"
sys.modules["app.utils.partitioning"].resolve_and_bind = (
    lambda *a, **k: types.SimpleNamespace(collection="openmemory", shard_key=None)
)

_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "compat_v3.py"
_spec = importlib.util.spec_from_file_location("compat_v3_under_test", _PATH)
compat_v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat_v3)

for _name in _stub_names:
    _prior = _saved_modules[_name]
    if _prior is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _prior
del _stub_names, _saved_modules, _name, _prior

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _allow_writes(monkeypatch):
    """Bypass fail-closed write guard — this suite tests the REST contract, not registration."""
    monkeypatch.setattr(compat_v3, "check_write_allowed", lambda *a, **k: None)
    monkeypatch.setattr(compat_v3, "ensure_user_registered", lambda *a, **k: None)


async def _search(body):
    """Drive a single /search/ request against a freshly-mounted router.

    The memory client must be installed via ``monkeypatch.setattr(compat_v3,
    "get_memory_client", ...)`` before calling.
    """
    app = FastAPI()
    app.include_router(compat_v3.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return (await ac.post("/v3/memories/search/", json=body)).json()


class _Hit:
    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


class _Embed:
    model = "test-embed"

    def embed(self, query, mode):  # noqa: D401
        return [0.1, 0.2, 0.3]


class _CountingEmbed(_Embed):
    def __init__(self):
        self.calls = 0

    def embed(self, query, mode):
        self.calls += 1
        return super().embed(query, mode)


class _MemReadCache:
    """In-memory stand-in for the Redis read cache (embed + search pools)."""

    def __init__(self):
        self.embeds = {}
        self.pools = {}

    def get_embedding(self, model, query):
        return self.embeds.get((model, query))

    def set_embedding(self, model, query, vector):
        self.embeds[(model, query)] = vector

    def get_search(self, project, query, top_k, filter_hash):
        return self.pools.get((project, query, top_k, filter_hash))

    def set_search(self, project, query, top_k, filter_hash, hits):
        self.pools[(project, query, top_k, filter_hash)] = hits


class _VectorStore:
    def __init__(self, hits):
        self._hits = hits

    def _scoped(self, filters):
        hits = self._hits
        if filters and "project" in filters:
            hits = [h for h in hits if h.payload.get("project") == filters["project"]]
        return hits

    def search(self, query, vectors, top_k, filters, shard_key_selector=None):
        return self._scoped(filters)[:top_k]

    def list(self, filters, top_k, shard_key_selector=None):
        return self._scoped(filters)[:top_k]


class _FakeClient:
    def __init__(self, hits):
        self.embedding_model = _Embed()
        self.vector_store = _VectorStore(hits)
        self.add_calls = []

    def add(self, text, **kwargs):
        self.add_calls.append((text, kwargs))
        return {"results": [{"id": "new-1", "memory": text, "event": "ADD"}]}


def _hits():
    return [
        _Hit("a1", 0.9, {"data": "alpha state", "project": "A", "type": "session_state"}),
        _Hit("a2", 0.4, {"data": "alpha decision", "project": "A", "type": "decision"}),
        _Hit("b1", 0.95, {"data": "beta secret", "project": "B", "type": "session_state"}),
    ]


@pytest_asyncio.fixture
async def client(monkeypatch):
    _allow_writes(monkeypatch)
    fake = _FakeClient(_hits())
    monkeypatch.setattr(compat_v3, "get_memory_client", lambda: fake)
    app = FastAPI()
    app.include_router(compat_v3.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac._fake = fake  # expose for assertions
        yield ac


def _and(*clauses):
    return {"AND": list(clauses)}


class TestSearch:
    @pytest.fixture(autouse=True)
    def _mem_cache(self, monkeypatch):
        """Keep the search hermetic: swap the Redis read cache for an in-memory one."""
        cache = _MemReadCache()
        monkeypatch.setattr(compat_v3, "read_cache", cache)
        self.read_cache = cache
        yield

    @pytest.mark.asyncio
    async def test_repeated_search_uses_cached_pool_and_embedding(self, client, monkeypatch):
        """Hooks re-send the same query; the 2nd call must skip embed + vector search."""
        fake = _FakeClient(_hits())
        fake.embedding_model = _CountingEmbed()
        search_calls = []
        original_search = fake.vector_store.search

        def _counting_search(*args, **kwargs):
            search_calls.append(kwargs)
            return original_search(*args, **kwargs)

        fake.vector_store.search = _counting_search
        monkeypatch.setattr(compat_v3, "get_memory_client", lambda: fake)
        client._fake = fake
        body = {"query": "state", "filters": _and({"user_id": "host"}, {"app_id": "A"})}

        first = (await client.post("/v3/memories/search/", json=body)).json()
        second = (await client.post("/v3/memories/search/", json=body)).json()

        assert fake.embedding_model.calls == 1  # embed computed once
        assert len(search_calls) == 1  # pool served from cache on the 2nd call
        assert {r["id"] for r in first["results"]} == {r["id"] for r in second["results"]}

    @pytest.mark.asyncio
    async def test_global_search_includes_all_projects(self, client):
        body = {"query": "state", "filters": _and({"user_id": "host"}, {"app_id": "A"})}
        data = (await client.post("/v3/memories/search/", json=body)).json()
        ids = {r["id"] for r in data["results"]}
        assert ids == {"a1", "a2", "b1"}  # global search; app_id is hint only

    @pytest.mark.asyncio
    async def test_project_hint_boosts_matching_project(self, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        hits = [
            _Hit("other", 0.90, {"data": "other", "project": "other-repo", "updated_at": now}),
            _Hit("mine", 0.88, {"data": "mine", "project": "mem0-shared", "updated_at": now}),
        ]
        monkeypatch.setattr(compat_v3, "get_memory_client", lambda: _FakeClient(hits))
        _rec = importlib.import_module(compat_v3.rank_search_results.__module__)
        monkeypatch.setattr(_rec, "SEARCH_RECENCY_WEIGHT", 0.0)
        data = await _search({"query": "x", "filters": _and({"app_id": "mem0-shared"})})
        assert [r["id"] for r in data["results"]] == ["mine", "other"]

    @pytest.mark.asyncio
    async def test_metadata_type_post_filter(self, client):
        body = {
            "query": "state",
            "filters": _and({"app_id": "A"}, {"metadata": {"type": "session_state"}}),
        }
        data = (await client.post("/v3/memories/search/", json=body)).json()
        # Global search + metadata filter; app_id A boosts a1 above b1.
        assert [r["id"] for r in data["results"]] == ["a1", "b1"]

    @pytest.mark.asyncio
    async def test_threshold_filters_low_scores(self, client):
        body = {"query": "state", "filters": _and({"app_id": "A"}), "threshold": 0.5}
        data = (await client.post("/v3/memories/search/", json=body)).json()
        assert [r["id"] for r in data["results"]] == ["a1", "b1"]  # a2 score 0.4 dropped

    @pytest.mark.asyncio
    async def test_result_shape(self, client):
        body = {"query": "state", "filters": _and({"app_id": "A"})}
        r = (await client.post("/v3/memories/search/", json=body)).json()["results"][0]
        for key in ("id", "memory", "score", "metadata"):
            assert key in r

    @pytest.mark.asyncio
    async def test_recency_outranks_older_more_similar(self, monkeypatch):
        # Parity with app.mcp_server.search_memory: the older fact is a closer
        # match (higher score) but was last changed years ago; the newer fact is
        # less similar but UPDATED today (despite an old created_at) — recency,
        # keyed off updated_at, must surface it first (ADR-003 at read time).
        now = datetime.now(timezone.utc).isoformat()
        hits = [
            _Hit("old", 0.95, {"data": "old", "project": "A",
                               "created_at": "2020-01-01T00:00:00+00:00",
                               "updated_at": "2020-01-01T00:00:00+00:00"}),
            _Hit("new", 0.80, {"data": "new", "project": "A",
                               "created_at": "2019-01-01T00:00:00+00:00",
                               "updated_at": now}),
        ]
        monkeypatch.setattr(compat_v3, "get_memory_client", lambda: _FakeClient(hits))
        data = await _search({"query": "x", "filters": _and({"app_id": "A"})})
        assert [r["id"] for r in data["results"]] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_recency_ordering_runs_before_topk_truncation(self, monkeypatch):
        # With a metadata filter the router over-fetches (fetch_k = top_k*4), so a
        # recent fact ranked below top_k by raw score is still rescued: recency
        # ordering happens BEFORE the cut to top_k.
        now = datetime.now(timezone.utc).isoformat()
        hits = [
            _Hit("old", 0.95, {"data": "old", "project": "A", "type": "decision",
                               "updated_at": "2020-01-01T00:00:00+00:00"}),
            _Hit("new", 0.80, {"data": "new", "project": "A", "type": "decision",
                               "updated_at": now}),
        ]
        monkeypatch.setattr(compat_v3, "get_memory_client", lambda: _FakeClient(hits))
        body = {
            "query": "x",
            "top_k": 1,
            "filters": _and({"app_id": "A"}, {"metadata": {"type": "decision"}}),
        }
        data = await _search(body)
        assert [r["id"] for r in data["results"]] == ["new"]


class TestAdd:
    @pytest.mark.asyncio
    async def test_add_concatenates_messages_and_preserves_metadata(self, client):
        body = {
            "messages": [{"role": "user", "content": "we use pytest"}],
            "user_id": "host",
            "app_id": "A",
            "metadata": {"type": "decision", "file": "x.py"},
            "infer": False,
        }
        data = (await client.post("/v3/memories/add/", json=body)).json()
        assert data["status"] == "ok"
        assert data["event_id"] == "new-1"

        text, kwargs = client._fake.add_calls[0]
        assert "we use pytest" in text
        assert kwargs["project"] == "A"
        assert kwargs["infer"] is False
        assert kwargs["metadata"]["type"] == "decision"
        assert kwargs["metadata"]["file"] == "x.py"
        assert kwargs["metadata"]["project"] == "A"  # scoping injected

    @pytest.mark.asyncio
    async def test_empty_payload_is_noop(self, client):
        data = (await client.post("/v3/memories/add/", json={"app_id": "A"})).json()
        assert data["status"] == "empty"
        assert client._fake.add_calls == []


class TestList:
    @pytest.mark.asyncio
    async def test_count_and_results_project_scoped(self, client):
        body = {"filters": _and({"app_id": "A"})}
        data = (await client.post("/v3/memories/?page=1&page_size=10", json=body)).json()
        assert data["count"] == 2
        assert {r["id"] for r in data["results"]} == {"a1", "a2"}


class TestFlatHookContract:
    """The lifecycle hooks post ``{query, user_id, project, limit}`` — not a filter tree.

    Those fields used to be dropped by the request model, so every hook read was
    audited as ``unknown-host`` with no project ranking hint.
    """

    @pytest.mark.asyncio
    async def test_flat_user_id_is_audited_as_the_reader_hostname(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **kw: calls.append(kw))

        await client.post(
            "/v3/memories/search/",
            json={"query": "alpha", "user_id": "S0258", "project": "A", "limit": 5},
        )

        assert calls, "search must record a read-audit row"
        assert calls[0]["hostname"] == "S0258"
        assert calls[0]["project"] == "A"

    @pytest.mark.asyncio
    async def test_flat_star_user_id_still_flags_a_global_read(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **kw: calls.append(kw))

        data = (
            await client.post(
                "/v3/memories/search/", json={"query": "alpha", "user_id": "*"}
            )
        ).json()

        # "*" is the global marker, never a hostname.
        assert calls[0]["hostname"] == "unknown-host"
        assert {r["id"] for r in data["results"]} == {"a1", "a2", "b1"}

    @pytest.mark.asyncio
    async def test_flat_limit_caps_the_page(self, client):
        data = (
            await client.post(
                "/v3/memories/search/", json={"query": "alpha", "limit": 1}
            )
        ).json()
        assert len(data["results"]) == 1

    @pytest.mark.asyncio
    async def test_filter_tree_still_wins_over_flat_fields(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **kw: calls.append(kw))

        await client.post(
            "/v3/memories/search/",
            json={
                "query": "alpha",
                "filters": _and({"app_id": "A"}, {"user_id": "S0293"}),
                "user_id": "S0258",
                "project": "B",
            },
        )

        assert calls[0]["hostname"] == "S0293"
        assert calls[0]["project"] == "A"


class TestAgentTokenAttribution:
    """Token de agente: a máquina vinculada à pessoa vence o ``user_id`` do corpo.

    O hook ``UserPromptSubmit`` manda ``user_id: hostname()``; numa máquina nova,
    ainda não vinculada, isso atribuía as leituras à máquina e não à pessoa.
    """

    @pytest.fixture
    def agent_token(self):
        from app.utils.logging_context import auth_method_var, machine_var

        method = auth_method_var.set("agent_token")
        machine = machine_var.set("S0258")
        yield
        machine_var.reset(machine)
        auth_method_var.reset(method)

    @pytest.mark.asyncio
    async def test_search_audits_the_token_machine(self, client, monkeypatch, agent_token):
        calls = []
        monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **kw: calls.append(kw))

        await client.post(
            "/v3/memories/search/",
            json={"query": "alpha", "user_id": "S0360", "project": "A", "limit": 5},
        )

        assert calls[0]["hostname"] == "S0258"

    @pytest.mark.asyncio
    async def test_token_also_wins_over_the_filter_tree(self, client, monkeypatch, agent_token):
        calls = []
        monkeypatch.setattr(compat_v3, "record_memory_reads", lambda **kw: calls.append(kw))

        await client.post(
            "/v3/memories/?page=1&page_size=10",
            json={"filters": _and({"app_id": "A"}, {"user_id": "S0360"})},
        )

        assert calls[0]["hostname"] == "S0258"

    @pytest.mark.asyncio
    async def test_add_attributes_to_the_token_machine(self, client, agent_token):
        body = {"text": "we use pytest", "user_id": "S0360", "app_id": "A", "infer": False}
        data = (await client.post("/v3/memories/add/", json=body)).json()
        assert data["status"] == "ok"

        _, kwargs = client._fake.add_calls[0]
        assert kwargs["user_id"] == "S0258"
        assert kwargs["metadata"]["hostname"] == "S0258"

    @pytest.mark.asyncio
    async def test_without_token_body_user_id_is_kept(self, client, monkeypatch):
        from app.utils.logging_context import auth_method_var, machine_var

        method = auth_method_var.set("legacy")
        machine = machine_var.set("S0258")
        try:
            calls = []
            monkeypatch.setattr(
                compat_v3, "record_memory_reads", lambda **kw: calls.append(kw)
            )
            await client.post(
                "/v3/memories/search/", json={"query": "alpha", "user_id": "S0360"}
            )
        finally:
            machine_var.reset(machine)
            auth_method_var.reset(method)

        assert calls[0]["hostname"] == "S0360"
