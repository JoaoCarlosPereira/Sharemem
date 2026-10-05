"""
MCP Server for ShareMem with resilient memory client handling.

This module implements an MCP (Model Context Protocol) server that provides
memory operations for ShareMem. The memory client is initialized lazily
to prevent server crashes when external dependencies (like Ollama) are
unavailable. If the memory client cannot be initialized, the server will
continue running with limited functionality and appropriate error messages.

Key features:
- Lazy memory client initialization
- Graceful error handling for unavailable dependencies
- Fallback to database-only mode when vector store is unavailable
- Proper logging for debugging connection issues
- Environment variable parsing for API keys
"""

import contextvars
import datetime
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

import anyio

from app.database import SessionLocal
from app.models import (
    Memory,
    MemoryAccessLog,
    MemoryState,
    MemoryStatusHistory,
    WriteAuditLog,
)
from app.utils.metrics import (
    EMBED_CACHE_HIT,
    EMBED_CACHE_MISS,
    SEARCH_CACHE_HIT,
    SEARCH_CACHE_MISS,
    SEARCH_LATENCY,
)
from app.utils.env import safe_load_dotenv
from app.utils.db import get_user_and_app
from app.utils.attribution import author_hostname_from_payload
from app.utils.groups import ensure_user_group, ensure_user_registered, requester_group_for_mcp
from app.utils.identity import is_plausible_hostname, resolve_hostname
from app.utils.logging_context import auth_method_var, auth_user_var, machine_var, team_var
from app.utils.memory import get_memory_client_safe
from app.utils.partitioning import bind_active_collection
from app.utils.permissions import check_memory_access_permissions
from app.utils.read_cache import read_cache
from app.utils.project_groups import projects_in_group
from app.utils.project_name import normalize_project, normalize_project_with_notice
from app.utils.recency import rank_search_results
from app.utils.scope_keys import normalize_task, resolve_task
from app.utils.reranking import apply_rerank
from app.utils.token_usage_wrapper import usage_attribution
from app.utils.write_guard import check_write_allowed
from app.utils.write_queue import WriteJob, estimate_write_wait_sec, write_queue
from fastapi import FastAPI, Request
from fastapi.routing import APIRouter
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.responses import Response

# Load environment variables
safe_load_dotenv()

# Initialize MCP
mcp = FastMCP("sharemem")

# get_memory_client_safe is imported from app.utils.memory (canonical location).

# Context variables for user_id and client_name
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id")
client_name_var: contextvars.ContextVar[str] = contextvars.ContextVar("client_name")
registry_auth_headers_var: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "registry_auth_headers",
    default=None,
)

# Read-path defaults (task_03 / ADR-003): keep search top_k bounded for latency;
# list uses a higher default and paginated scroll so project audits are complete.
DEFAULT_SEARCH_TOP_K = 20
DEFAULT_LIST_TOP_K = 200
DEFAULT_LIST_PAGE_SIZE = 256

# Kanban column prompts cache (task_06)
_kanban_prompts_cache: dict[str, dict] = {}
_kanban_prompts_cache_loaded: datetime.datetime | None = None
_KANBAN_PROMPTS_TTL_SECONDS = 600  # 10 minutes

def _kanban_prompts_cache_expired() -> bool:
    """Returns True if the cache is empty or has exceeded its TTL."""
    if _kanban_prompts_cache_loaded is None:
        return True
    return (datetime.datetime.now(datetime.UTC) - _kanban_prompts_cache_loaded).total_seconds() > _KANBAN_PROMPTS_TTL_SECONDS

def _load_kanban_prompts_cache(db: Any) -> None:
    """Loads all prompts from the database into the in-memory cache."""
    try:
        from app.models import KanbanColumnPrompt
        rows = db.query(KanbanColumnPrompt).all()
        _kanban_prompts_cache.clear()
        for r in rows:
            _kanban_prompts_cache[r.column_status] = {
                "prompt": r.prompt,
                "is_enabled": r.is_enabled,
            }
        global _kanban_prompts_cache_loaded
        _kanban_prompts_cache_loaded = datetime.datetime.now(datetime.UTC)
        logging.info("Kanban column prompts cache loaded: %d entries", len(_kanban_prompts_cache))
    except Exception as e:
        logging.warning("Failed to load kanban column prompts cache: %s", e)

def _invalidate_kanban_prompts_cache() -> None:
    """Clears the cache and resets the load timestamp."""
    global _kanban_prompts_cache_loaded
    _kanban_prompts_cache.clear()
    _kanban_prompts_cache_loaded = None
    logging.info("Kanban column prompts cache invalidated")


# Over-fetch factor for the semantic read path. ``rank_search_results`` blends the
# raw score with recency, project and group boosts, so it can only promote what the
# vector store already returned: fetching exactly DEFAULT_SEARCH_TOP_K candidates
# meant re-ranking AFTER truncation, and a recent, same-group memory that missed the
# raw top-20 could never be recovered by any boost. We therefore retrieve a wider
# candidate pool, rank it, and only then cut to DEFAULT_SEARCH_TOP_K.
# Tunable via MEM0_SEARCH_CANDIDATE_K (clamped to at least DEFAULT_SEARCH_TOP_K).
#
# 150 is a MEASURED value, not a guess. Timed inside the API container against the
# production Qdrant (host s0215, 2026-08-03), 5 distinct queries per row:
#
#     top_k   qdrant p50   payload
#        20       7.6 ms     25 KB
#        60      11.0 ms     87 KB
#       150      15.1 ms    298 KB   <- current
#       300      67.2 ms    589 KB
#       500      48.3 ms    859 KB
#
# Put that against the rest of a search: embedding the query costs ~88 ms p50 and
# ranking the pool ~6 ms, so widening 20 -> 150 adds ~7 ms to a ~110 ms call —
# under 7%, for the recall it buys. The cliff is past 150: 300 already quadruples
# the retrieval time. Raise this only with a fresh measurement; the number depends
# on payload size, and this store holds memories of several KB.
DEFAULT_SEARCH_CANDIDATE_K = max(
    DEFAULT_SEARCH_TOP_K,
    int(os.getenv("MEM0_SEARCH_CANDIDATE_K", "150")),
)

# Bound external/vector operations so a slow Ollama or Qdrant cannot consume the
# MCP request until the client gives up. The list fallback keeps reads useful when
# semantic search exceeds its budget.
SEARCH_EMBED_TIMEOUT_SECONDS = float(os.getenv("MEM0_SEARCH_EMBED_TIMEOUT_SEC", "12"))
SEARCH_VECTOR_TIMEOUT_SECONDS = float(os.getenv("MEM0_SEARCH_VECTOR_TIMEOUT_SEC", "8"))
SEARCH_SCROLL_TIMEOUT_SECONDS = float(os.getenv("MEM0_SEARCH_SCROLL_TIMEOUT_SEC", "8"))

async def _run_search_operation(operation, *, timeout: float):
    """Run a blocking memory operation with a bounded MCP-facing wait."""
    with anyio.fail_after(timeout):
        return await anyio.to_thread.run_sync(operation, abandon_on_cancel=True)


MCP_BLOCKING_TIMEOUT_SEC = float(os.getenv("MEM0_MCP_BLOCKING_TIMEOUT_SEC", "15"))
MCP_CLIENT_BUILD_TIMEOUT_SEC = float(os.getenv("MEM0_MCP_CLIENT_BUILD_TIMEOUT_SEC", "60"))

async def _run_blocking(operation, *, timeout: float = MCP_BLOCKING_TIMEOUT_SEC):
    """I/O bloqueante (DB/Redis) fora do event loop, com teto de espera."""
    with anyio.fail_after(timeout):
        return await anyio.to_thread.run_sync(operation, abandon_on_cancel=True)


# Write-path default (task_07): the MCP route always provides a client_name, but
# a direct tool call may not — fall back to an explicit sentinel for attribution.
DEFAULT_CLIENT_NAME = "unknown-client"

# Create a router for MCP endpoints
mcp_router = APIRouter(prefix="/mcp")

# Initialize SSE transport
sse = SseServerTransport("/mcp/messages/")

@mcp.tool(description="Save content for asynchronous memory extraction in a project. Call this whenever the user shares durable facts or preferences, or asks you to remember something. `project` is REQUIRED and scopes the memory (memories are shared across all machines on the local network). Optional `task` is the Redmine task code (6 digits) this memory belongs to — it is a separate, exactly filterable key, so use it instead of putting the task number in `project` (`project` is the repository/product). Optional `supersedes` is a list of memory IDs this write replaces — those are marked obsolete and hidden from search by default; the reply also lists other active memories that say the same thing as what you superseded, so a duplicate does not survive the correction. Returns immediately with status accepted after enqueue (includes job_id, queue_depth, estimated_wait_sec); extraction runs in the background — the memory is not searchable until the worker finishes. Do NOT poll for job status. If the HTTP call returns 429 / rate_limit_exceeded, wait retry_after_sec (or Retry-After) and retry the same text 1–3 times with backoff; do not discard the content. To find conflicting memories, use search_memory / mark_obsolete — never on this write path.")
async def add_memories(
    text: str,
    project: str,
    task: str | None = None,
    supersedes: list[str] | None = None,
) -> str:
    # task_07 / ADR-004: fire-and-forget enqueue only. No embed/search/LLM on
    # the request path — MCP clients must always get a fast accepted ack when
    # connected (conflict detection is a separate read tool).
    client_name = client_name_var.get(None) or DEFAULT_CLIENT_NAME

    if not text or not text.strip():
        return "Error: text not provided"
    if not project or not project.strip():
        return "Error: project not provided"

    def _sync_checks():
        hostname_val = _mcp_attribution_hostname()
        blocked_val = check_write_allowed(
            hostname_val,
            auth_method=auth_method_var.get(),
            auth_user=auth_user_var.get(),
        )
        if blocked_val:
            return hostname_val, blocked_val
        ensure_user_registered(hostname_val)
        return hostname_val, None

    hostname, blocked = await _run_blocking(_sync_checks)

    if blocked:
        logging.warning(
            "write rejected hostname=%s client=%s auth_method=%s auth_user=%s",
            hostname,
            client_name,
            auth_method_var.get() or "legacy",
            auth_user_var.get() or "-",
        )
        return blocked

    # Chave efetiva: o SDK rejeita whitespace interno no worker (job falhava em
    # silencio depois do ack). Normaliza aqui e informa o projeto efetivo.
    original_project = project.strip()
    project, project_notice = normalize_project_with_notice(project)
    supersede_ids: list[str] = []
    if supersedes:
        for mid in supersedes:
            if mid is None:
                continue
            s = str(mid).strip()
            if s:
                supersede_ids.append(s)

    # Chave de tarefa (opcional): a informada pelo chamador vence; senao tenta a
    # forma explicita `TAREFA #NNNNNN` no proprio texto. Ver app.utils.scope_keys.
    task_key = resolve_task(task, text)

    extras: dict | None = {}
    if supersede_ids:
        extras["supersedes"] = supersede_ids
    if task_key:
        extras["task"] = task_key
    extras = extras or None

    def _sync_enqueue():
        job_id_val = write_queue.enqueue(
            WriteJob(
                id="",
                project=project,
                hostname=hostname,
                client_name=client_name,
                text=text,
                created_at="",
                extras=extras,
            )
        )
        _record_write_audit(job_id=job_id_val, project=project, hostname=hostname,
                             client_name=client_name)
        q_depth = 0
        try:
            q_depth = int(write_queue.depth())
        except Exception as exc:  # noqa: BLE001
            logging.warning("write_queue.depth() failed after enqueue: %s", exc)
        return job_id_val, q_depth

    try:
        job_id, queue_depth = await _run_blocking(_sync_enqueue)
    except Exception as e:
        logging.exception(f"Error enqueuing memory write: {e}")
        return f"Error enqueuing memory write: {e}"

    logging.info(
        "write enqueued job_id=%s project=%s hostname=%s client=%s auth_method=%s auth_user=%s supersedes=%s",
        job_id,
        project,
        hostname,
        client_name,
        auth_method_var.get() or "legacy",
        auth_user_var.get() or "-",
        supersede_ids or None,
    )

    estimated_wait_sec = estimate_write_wait_sec(queue_depth)

    payload = {
        "status": "accepted",
        "job_id": job_id,
        "queue_depth": queue_depth,
        "estimated_wait_sec": estimated_wait_sec,
        "message": (
            "Memory queued for background extraction; it is not searchable yet. "
            "An immediate search may miss it — that is expected, not an error. "
            "Do NOT poll for job status. Operators can inspect GET /admin/write-queue "
            "or /health (not on the hot agent path)."
        ),
        "project": project,
    }
    if project_notice:
        payload["project_requested"] = original_project
        payload["warning"] = project_notice
    if supersede_ids:
        payload["supersedes"] = supersede_ids
    if task_key:
        payload["task"] = task_key
    return json.dumps(payload, indent=2)


def _usage_user_id() -> str:
    """Dimensão ``user`` da atribuição de consumo (feature auth Google).

    Pessoa autenticada por token de agente (``auth_user_var``, ADR-006) quando
    presente; senão o hostname legado — comportamento byte-idêntico ao anterior
    para agentes sem token.
    """
    person = auth_user_var.get() or ""
    if auth_method_var.get() == "agent_token" and person:
        return person
    return resolve_hostname(user_id_var.get(None))


def _mcp_attribution_hostname() -> str:
    """Hostname for write attribution: bound machine > path user_id."""
    bound = (machine_var.get() or "").strip()
    if auth_method_var.get() == "agent_token" and bound:
        return resolve_hostname(bound)
    return resolve_hostname(user_id_var.get(None))


def _effective_mcp_uid(path_uid: str | None) -> str:
    """Prefer AuthMiddleware-bound machine over URL path for agent tokens."""
    bound = (machine_var.get() or "").strip()
    if auth_method_var.get() == "agent_token" and bound:
        return bound
    return path_uid or ""


def _log_machine_divergence_if_any(hostname) -> None:
    """Loga (sem bloquear) token de agente usado em máquina não vinculada.

    Fase 1 não bloqueia divergência máquina-do-token × hostname-da-URL; o log
    estruturado é o insumo da tela de conflitos (Fase 2). Best-effort: nunca
    levanta no caminho de conexão MCP.
    """
    if auth_method_var.get() != "agent_token":
        return
    person = auth_user_var.get() or ""
    if not person or not hostname:
        return
    try:
        # Import tardio: resolve SessionLocal no momento da chamada (testável
        # via monkeypatch de app.database, como em resolve_agent_token).
        from app.database import SessionLocal as _session_factory
        from app.models import Machine, MachineStatus

        db = _session_factory()
        try:
            linked = [
                m.hostname
                for m in db.query(Machine)
                .filter(
                    Machine.linked_user_id == uuid.UUID(person),
                    Machine.status == MachineStatus.linked,
                )
                .all()
            ]
        finally:
            db.close()
        key = resolve_hostname(hostname)
        if linked and key not in linked:
            logging.warning(
                "maquina divergente: token do usuario %s usado no hostname %s "
                "(vinculadas: %s)",
                person,
                key,
                ",".join(sorted(linked)),
            )
    except Exception:  # noqa: BLE001 - verificação é best-effort
        logging.debug("verificação de divergência de máquina falhou", exc_info=True)


def _record_write_audit(*, job_id, project, hostname, client_name):
    """Persist a write-attribution audit row; never raise to the caller."""
    db = SessionLocal()
    try:
        db.add(
            WriteAuditLog(
                job_id=uuid.UUID(str(job_id)) if job_id else None,
                project=project,
                hostname=hostname,
                client_name=client_name,
                action="enqueue",
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - audit failure must not break the write
        logging.exception("could not record write audit for job_id=%s", job_id)
        db.rollback()
    finally:
        db.close()



def _unwrap_vector_list(raw) -> list:
    """Normalize vector_store.list / Qdrant scroll return to a flat points list."""
    if raw is None:
        return []
    if isinstance(raw, (tuple, list)) and len(raw) > 0 and isinstance(raw[0], (list, tuple)):
        return list(raw[0] or [])
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _point_to_memory_result(point) -> dict:
    payload = getattr(point, "payload", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": str(getattr(point, "id", None) or ""),
        "memory": payload.get("data"),
        "hash": payload.get("hash"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "project": payload.get("project"),
        "owner": author_hostname_from_payload(payload),
    }


def _scroll_project_points(
    memory_client,
    project: str | None,
    *,
    limit: int,
    include_obsolete: bool = False,
) -> list:
    """Paginated scroll of the active collection, optionally filtered by project.

    Uses the Qdrant client directly so list is not truncated to a single page
    (vector_store.list only returns the first scroll page). Applies the same
    governance state filter as search (hides quarantined / obsolete by default).
    """
    bind_active_collection(memory_client)
    vs = memory_client.vector_store
    filters = {"project": project} if project else None
    merged = vs._merge_governance_filters(filters, include_obsolete=include_obsolete)
    scroll_filter = vs._create_filter(merged)
    points: list = []
    offset = None
    page = max(1, min(DEFAULT_LIST_PAGE_SIZE, limit))
    while len(points) < limit:
        batch_limit = min(page, limit - len(points))
        records, offset = vs.client.scroll(
            collection_name=vs.collection_name,
            scroll_filter=scroll_filter,
            limit=batch_limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            break
        points.extend(records)
        if offset is None:
            break
    return points


async def _fetch_all_memories(memory_client, top_k: int = DEFAULT_LIST_TOP_K) -> list:
    """Lista memórias de toda a coleção (sem embedding) — fallback quando o embedder falha."""
    points = await anyio.to_thread.run_sync(
        lambda: _scroll_project_points(memory_client, None, limit=top_k)
    )
    return [_point_to_memory_result(p) for p in points]


@mcp.tool(description="Search stored memories across all projects, ranked by relevance and recency. By default `project` is a soft hint (slight boost for matching names) — wrong or slightly different project names do not exclude results. Pass `strict_project=true` to hard-filter to that exact project name only. Pass `task` (6-digit Redmine code) to hard-filter to one task — that is the exact, reliable way to ask for everything about a task, and it works across projects. Even without it, a task code written in `query` gets an exact-match boost. Obsolete (superseded) memories are hidden by default; pass `include_obsolete=true` for audit. Pass `rerank=true` only if you want cross-encoder reordering: it applies solely when MEM0_RERANKER_PROVIDER is set on the server; otherwise the response includes `rerank.applied=false` with reason `not_configured` (expected, not a bug). Memories are shared across all machines on the local network.")
async def search_memory(
    query: str,
    project: str,
    rerank: bool = False,
    strict_project: bool = False,
    include_obsolete: bool = False,
    task: str | None = None,
) -> str:
    # NOTE (task_03 / ADR-003): semantic reads are GLOBAL across projects and SHARED
    # across all machines by default. ``project`` is a ranking hint only (small boost
    # for name match) unless ``strict_project`` is true. We intentionally do NOT filter
    # by ``user_id`` (hostname is write-path attribution only).
    if not project:
        return "Error: project not provided"
    # Mesma chave da escrita: "PONTEIRO DE SPEC" acha o gravado como
    # "PONTEIRO-DE-SPEC" (filtro estrito, hint, cache e auditoria).
    project = normalize_project(project)

    started = time.perf_counter()
    try:
        def _sync_search_setup():
            # Grupo do solicitante (ADR-003): hostname da conexão → users.group_id (Admin).
            req_group = requester_group_for_mcp(user_id_var.get(None))
            mem_client = get_memory_client_safe()
            if not mem_client:
                return req_group, mem_client, None
            bind_active_collection(mem_client)
            # strict_project narrows to the project's configured family when there is
            # one: asking to stay "in this project" means the subject, not the single
            # repository the session happens to be rooted at.
            scope = (
                [normalize_project(p) for p in projects_in_group(project)]
                if strict_project
                else []
            )
            return req_group, mem_client, scope

        requester_group, memory_client, strict_scope = await _run_blocking(
            _sync_search_setup, timeout=MCP_CLIENT_BUILD_TIMEOUT_SEC
        )
        if not memory_client:
            return "Error: Memory system is currently unavailable. Please try again later."

        if strict_project:
            search_filters = (
                {"project": {"in": strict_scope}}
                if len(strict_scope) > 1
                else {"project": project}
            )
        else:
            search_filters = None

        # ``task`` e filtro duro e ORTOGONAL ao projeto: a mesma tarefa vive em
        # mais de um repositorio (a 371145 em sysmo-api-tributacao e sysmo-s1),
        # entao restringir a tarefa nao pode implicar restringir o projeto.
        task_key = normalize_task(task)
        if task_key:
            search_filters = dict(search_filters or {})
            search_filters["task"] = task_key
        filter_hash = hashlib.sha256(
            json.dumps(
                {
                    "mode": "strict" if strict_project else "global",
                    "preferred_project": project,
                    # Parte da chave: o pool em cache e o pool FILTRADO, entao
                    # uma busca com task nao pode ser servida da entrada sem ela.
                    "task": task_key,
                    # Part of the key: the family is config-driven, so a change to
                    # MEM0_PROJECT_GROUPS must not be served from a stale entry.
                    "scope": strict_scope,
                    "include_obsolete": bool(include_obsolete),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]

        # The cache holds the unranked CANDIDATE pool (not the final page), so the
        # ranking below stays group-specific while the expensive retrieval is shared.
        cached_hits = await _run_blocking(
            lambda: read_cache.get_search(
                project, query, DEFAULT_SEARCH_CANDIDATE_K, filter_hash
            )
        )
        if cached_hits is not None:
            SEARCH_CACHE_HIT.inc()
            # Copy before ranking: the Redis backend hands back freshly deserialized
            # objects, but ranking/annotation mutate in place and must not depend on
            # that — a non-serializing cache backend would otherwise leak one
            # requester's group-specific ordering into another's.
            results = [dict(r) for r in cached_hits]
        else:
            SEARCH_CACHE_MISS.inc()
            embed_model = getattr(memory_client.embedding_model, "model", "default")
            embeddings = await _run_blocking(
                lambda: read_cache.get_embedding(embed_model, query)
            )
            if embeddings is not None:
                EMBED_CACHE_HIT.inc()
            else:
                EMBED_CACHE_MISS.inc()
                try:
                    # usage_attribution can be sync, but _usage_user_id calls resolve_hostname which is safe or we can wrap.
                    # Since we are already inside a try block, let's keep the usage_attribution
                    # But _usage_user_id might do sync calls, so we wrap it.
                    u_id = await _run_blocking(_usage_user_id)
                    with usage_attribution(
                        project=project,
                        agent=client_name_var.get(None) or DEFAULT_CLIENT_NAME,
                        user_id=u_id,
                        operation_type="search",
                    ):
                        embeddings = await _run_search_operation(
                            lambda: memory_client.embedding_model.embed(query, "search"),
                            timeout=SEARCH_EMBED_TIMEOUT_SECONDS,
                        )
                    await _run_blocking(
                        lambda: read_cache.set_embedding(embed_model, query, embeddings)
                    )
                except Exception as embed_err:  # noqa: BLE001
                    logging.warning(
                        "Semantic search unavailable (%s); falling back to %s list",
                        embed_err,
                        "project" if strict_project else "global",
                    )
                    scope = project if strict_project else None
                    points = await _run_search_operation(
                        lambda: _scroll_project_points(
                            memory_client,
                            scope,
                            limit=DEFAULT_SEARCH_CANDIDATE_K,
                            include_obsolete=include_obsolete,
                        ),
                        timeout=SEARCH_SCROLL_TIMEOUT_SECONDS,
                    )
                    results = [_point_to_memory_result(p) for p in points]
                    await _run_blocking(
                        lambda: rank_search_results(
                            results,
                            preferred_project=project,
                            requester_group=requester_group,
                            annotate=True,
                            query=query,
                        )
                    )
                    return json.dumps(
                        {
                            "results": results[:DEFAULT_SEARCH_TOP_K],
                            "degraded": "list_fallback",
                        },
                        indent=2,
                    )

            hits = await _run_search_operation(
                lambda: memory_client.vector_store.search(
                    query=query,
                    vectors=embeddings,
                    top_k=DEFAULT_SEARCH_CANDIDATE_K,
                    filters=search_filters,
                    shard_key_selector=None,
                    include_obsolete=include_obsolete,
                ),
                timeout=SEARCH_VECTOR_TIMEOUT_SECONDS,
            )

            results = []
            for h in hits:
                id, score, payload = h.id, h.score, h.payload
                results.append({
                    "id": id,
                    "memory": payload.get("data"),
                    "hash": payload.get("hash"),
                    "created_at": payload.get("created_at"),
                    "updated_at": payload.get("updated_at"),
                    "project": payload.get("project"),
                    "owner": author_hostname_from_payload(payload),
                    "score": score,
                    "state": payload.get("state"),
                })
            await _run_blocking(
                lambda: read_cache.set_search(
                    project, query, DEFAULT_SEARCH_CANDIDATE_K, filter_hash, results
                )
            )

        def _rank(items):
            rank_search_results(
                items,
                preferred_project=project,
                requester_group=requester_group,
                annotate=True,
                query=query,
            )

        def _sync_rerank_and_rank():
            # Rank the whole candidate pool, THEN cut the page: recency/project/group
            # boosts must be able to promote a candidate that missed the raw top-K.
            _rank(results)
            if not rerank:
                return None
            # Reranking (opt-in) rescores only the best MEM0_RERANKER_TOP_N of that
            # blend (bounded CPU cost), then the head is blended again with the
            # normalized rerank score so recency/project/group keep the final say.
            # On any failure/timeout ``results`` is untouched (original order).
            r_status = apply_rerank(query, results, page_size=DEFAULT_SEARCH_TOP_K)
            if r_status.get("applied"):
                head_size = r_status["reranked"]
                head = results[:head_size]
                _rank(head)
                results[:head_size] = head
            return r_status

        rerank_status = await _run_blocking(_sync_rerank_and_rank)

        payload = {"results": results[:DEFAULT_SEARCH_TOP_K]}
        if rerank_status is not None:
            # Always report the outcome: asking for rerank and getting it are
            # different things, and silently ignoring the flag hides misconfiguration.
            payload["rerank"] = rerank_status
        return json.dumps(payload, indent=2)
    except Exception as e:
        logging.exception(e)
        return f"Error searching memory: {e}"
    finally:
        SEARCH_LATENCY.observe(time.perf_counter() - started)


@mcp.tool(description="List stored memories scoped by project (hard filter; shared across all machines). Returns matching points up to `limit` (default 200) via paginated scroll. Obsolete memories are hidden by default; pass `include_obsolete=true` for audit.")
async def list_memories(
    project: str,
    limit: int = DEFAULT_LIST_TOP_K,
    include_obsolete: bool = False,
) -> str:
    if not project:
        return "Error: project not provided"

    project = normalize_project(project.strip())
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIST_TOP_K
    limit = max(1, min(limit, 1000))

    def _sync_list_setup():
        req_group = requester_group_for_mcp(user_id_var.get(None))
        mem_client = get_memory_client_safe()
        return req_group, mem_client

    requester_group, memory_client = await _run_blocking(
        _sync_list_setup, timeout=MCP_CLIENT_BUILD_TIMEOUT_SEC
    )
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    try:
        points = await _run_search_operation(
            lambda: _scroll_project_points(
                memory_client,
                project,
                limit=limit,
                include_obsolete=include_obsolete,
            ),
            timeout=SEARCH_SCROLL_TIMEOUT_SECONDS,
        )
        results = [_point_to_memory_result(p) for p in points]

        await _run_blocking(
            lambda: rank_search_results(
                results, preferred_project=project, requester_group=requester_group
            )
        )

        return json.dumps(
            {"results": results, "total": len(results), "project": project},
            indent=2,
        )
    except Exception as e:
        logging.exception(f"Error getting memories: {e}")
        return f"Error getting memories: {e}"


@mcp.tool(description="Mark specific memories as obsolete (superseded) without deleting them. They disappear from search/list by default and remain recoverable via include_obsolete=true. Pass optional superseded_by with the correcting memory ID. The reply carries `sibling_candidates`: OTHER memories still active that say the same thing as what you just obsoleted, plus `ingest_siblings`, the other facts extracted from the same original write. Neither is marked automatically — review them and call mark_obsolete again for the ones that must also go, or the wrong fact survives the correction in a second copy.")
async def mark_obsolete(
    memory_ids: list[str],
    superseded_by: str | None = None,
    suggest_siblings: bool = True,
) -> str:
    from app.utils.supersede_fanout import find_sibling_candidates, ingest_siblings
    from app.utils.supersedes import mark_points_obsolete

    if not memory_ids:
        return "Error: memory_ids not provided"

    memory_client = await _run_blocking(
        get_memory_client_safe, timeout=MCP_CLIENT_BUILD_TIMEOUT_SEC
    )
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    ids = [str(m).strip() for m in memory_ids if m and str(m).strip()]
    if not ids:
        return "Error: memory_ids not provided"

    try:
        # Os irmaos sao lidos ANTES de marcar: depois de obsoleto o ponto ainda
        # existe, mas o filtro de estado passaria a exclui-lo dos vizinhos e a
        # busca partiria de um texto que ja nao esta em circulacao.
        candidates: list = []
        siblings: list = []
        if suggest_siblings:
            try:
                candidates = await anyio.to_thread.run_sync(
                    lambda: find_sibling_candidates(
                        memory_client,
                        ids,
                        exclude_ids=[superseded_by] if superseded_by else [],
                    )
                )
                siblings = await anyio.to_thread.run_sync(
                    lambda: ingest_siblings(memory_client, ids)
                )
            except Exception:  # noqa: BLE001 - sugerir nao pode impedir corrigir
                logging.exception("sibling lookup failed; proceeding with mark_obsolete")

        out = await anyio.to_thread.run_sync(
            lambda: mark_points_obsolete(
                memory_client,
                ids,
                superseded_by=(superseded_by.strip() if superseded_by else None),
            )
        )
        # Best-effort cache bust for common projects of updated points is hard
        # without payloads; callers should re-search.
        payload = {
            "status": "ok",
            "updated": out.get("updated") or [],
            "missing": out.get("missing") or [],
        }
        if suggest_siblings:
            payload["sibling_candidates"] = candidates
            payload["ingest_siblings"] = siblings
            if candidates or siblings:
                payload["note"] = (
                    "Estas memorias continuam ATIVAS e dizem o mesmo que a que "
                    "voce acabou de superar. Nenhuma foi marcada. Revise e chame "
                    "mark_obsolete de novo para as que tambem devem sair."
                )
        return json.dumps(payload, indent=2)
    except Exception as e:
        logging.exception("mark_obsolete failed")
        return f"Error marking obsolete: {e}"


@mcp.tool(description="Delete specific memories by their IDs from the shared vector store (Qdrant). IDs may come from search_memory or list_memories — no project parameter needed. Respects MEM0_ALLOW_MEMORY_DELETE / MEM0_ALLOW_BULK_DELETE.")
async def delete_memories(memory_ids: list[str]) -> str:
    from app.utils.deletion_guard import check_bulk_delete_allowed, check_memory_delete_allowed
    from app.utils.partitioning import bind_active_collection as _bind

    if not memory_ids:
        return "Error: memory_ids not provided"

    def _sync_del_setup():
        if len(memory_ids) > 1:
            blocked_val = check_bulk_delete_allowed("bulk_delete")
        else:
            blocked_val = check_memory_delete_allowed("delete")
        if blocked_val:
            return blocked_val, None, None

        u_id = resolve_hostname(user_id_var.get(None))
        mem_client = get_memory_client_safe()
        return None, u_id, mem_client

    blocked, uid, memory_client = await _run_blocking(
        _sync_del_setup, timeout=MCP_CLIENT_BUILD_TIMEOUT_SEC
    )

    if blocked:
        return f"Error: {blocked}"
    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    client_name = client_name_var.get(None) or DEFAULT_CLIENT_NAME

    # Normalize IDs; reject obviously invalid values early.
    requested: list[str] = []
    for mid in memory_ids:
        if mid is None:
            continue
        text = str(mid).strip()
        if text:
            requested.append(text)
    if not requested:
        return "Error: memory_ids not provided"

    try:
        await _run_blocking(lambda: _bind(memory_client))
        deleted_ids: list[str] = []
        missing_ids: list[str] = []
        vs = memory_client.vector_store

        for memory_id in requested:
            # Confirm the point exists in Qdrant (MCP writes never hit SQL memories).
            try:
                found = await anyio.to_thread.run_sync(
                    lambda mid=memory_id: vs.client.retrieve(
                        collection_name=vs.collection_name,
                        ids=[mid],
                        with_payload=False,
                        with_vectors=False,
                    )
                )
            except Exception as retrieve_error:  # noqa: BLE001
                logging.warning(
                    "Failed to retrieve memory %s before delete: %s",
                    memory_id,
                    retrieve_error,
                )
                found = []

            if not found:
                missing_ids.append(memory_id)
                continue

            try:
                await anyio.to_thread.run_sync(
                    lambda mid=memory_id: memory_client.delete(mid)
                )
                deleted_ids.append(memory_id)
            except Exception as delete_error:
                logging.warning(
                    "Failed to delete memory %s from vector store: %s",
                    memory_id,
                    delete_error,
                )
                missing_ids.append(memory_id)

        # Best-effort SQL catalog sync when a UI/API row exists for the same ID.
        if deleted_ids:
            def _sync_sql_sync():
                db = SessionLocal()
                try:
                    user, app = get_user_and_app(db, user_id=uid, app_id=client_name)
                    now = datetime.datetime.now(datetime.UTC)
                    for memory_id in deleted_ids:
                        try:
                            mem_uuid = uuid.UUID(memory_id)
                        except ValueError:
                            continue
                        memory = db.query(Memory).filter(Memory.id == mem_uuid).first()
                        if not memory:
                            continue
                        memory.state = MemoryState.deleted
                        memory.deleted_at = now
                        db.add(
                            MemoryStatusHistory(
                                memory_id=mem_uuid,
                                changed_by=user.id,
                                old_state=MemoryState.active,
                                new_state=MemoryState.deleted,
                            )
                        )
                        db.add(
                            MemoryAccessLog(
                                memory_id=mem_uuid,
                                app_id=app.id,
                                access_type="delete",
                                metadata_={"operation": "delete_by_id", "source": "mcp"},
                            )
                        )
                    db.commit()
                except Exception:  # noqa: BLE001
                    logging.exception("SQL catalog sync after MCP delete failed")
                    db.rollback()
                finally:
                    db.close()
            await _run_blocking(_sync_sql_sync)

        if not deleted_ids:
            return (
                "Error: No accessible memories found with provided IDs"
                + (f" (missing={missing_ids})" if missing_ids else "")
            )

        msg = f"Successfully deleted {len(deleted_ids)} memories"
        if missing_ids:
            msg += f" ({len(missing_ids)} not found: {missing_ids})"
        return msg
    except Exception as e:
        logging.exception(f"Error deleting memories: {e}")
        return f"Error deleting memories: {e}"


@mcp.tool(description="Delete all memories in the user's memory")
async def delete_all_memories() -> str:
    from app.utils.deletion_guard import check_bulk_delete_allowed
    def _sync_del_all_setup():
        blocked_val = check_bulk_delete_allowed("delete_all")
        if blocked_val:
            return blocked_val, None
        mem_client = get_memory_client_safe()
        return None, mem_client

    blocked, memory_client = await _run_blocking(
        _sync_del_all_setup, timeout=MCP_CLIENT_BUILD_TIMEOUT_SEC
    )

    if blocked:
        return f"Error: {blocked}"

    uid = user_id_var.get(None)
    client_name = client_name_var.get(None)
    if not uid:
        return "Error: user_id not provided"
    if not client_name:
        return "Error: client_name not provided"

    if not memory_client:
        return "Error: Memory system is currently unavailable. Please try again later."

    def _sync_delete_all():
        db = SessionLocal()
        try:
            # Get or create user and app
            user, app = get_user_and_app(db, user_id=uid, app_id=client_name)

            user_memories = db.query(Memory).filter(Memory.user_id == user.id).all()
            accessible_memory_ids = [memory.id for memory in user_memories if check_memory_access_permissions(db, memory, app.id)]

            # delete the accessible memories only
            for memory_id in accessible_memory_ids:
                try:
                    memory_client.delete(str(memory_id))
                except Exception as delete_error:
                    logging.warning(f"Failed to delete memory {memory_id} from vector store: {delete_error}")

            # Update each memory's state and create history entries
            now = datetime.datetime.now(datetime.UTC)
            for memory_id in accessible_memory_ids:
                memory = db.query(Memory).filter(Memory.id == memory_id).first()
                # Update memory state
                memory.state = MemoryState.deleted
                memory.deleted_at = now

                # Create history entry
                history = MemoryStatusHistory(
                    memory_id=memory_id,
                    changed_by=user.id,
                    old_state=MemoryState.active,
                    new_state=MemoryState.deleted
                )
                db.add(history)

                # Create access log entry
                access_log = MemoryAccessLog(
                    memory_id=memory_id,
                    app_id=app.id,
                    access_type="delete_all",
                    metadata_={"operation": "bulk_delete"}
                )
                db.add(access_log)

            db.commit()
            return "Successfully deleted all memories"
        finally:
            db.close()

    try:
        return await _run_blocking(_sync_delete_all)
    except Exception as e:
        logging.exception(f"Error deleting memories: {e}")
        return f"Error deleting memories: {e}"

# --------------------------------------------------------------------------- #
# Tools de specs (Tarefa 7) — wrappers finos sobre os utilitários/router das
# Tarefas 2/3/6. Cada tool segue o molde de add_memories: resolve hostname via
# ContextVar, delega a lógica de domínio e NUNCA propaga exceção crua.
# --------------------------------------------------------------------------- #
@mcp.tool(description="Create (idempotently) or return a shared spec workspace for a project's task. Call this before writing PRD/TechSpec/Tasks documents. Idempotent by (project_id, slug) — calling twice with the same slug returns the existing workspace. Returns JSON with the workspace id, slug and status.")
async def create_spec_workspace(project_id: str, slug: str, name: str) -> str:
    try:
        def _sync_op():
            from app.routers.specs import WorkspaceResponse, get_or_create_workspace

            hostname = resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                from app.utils.machine_resolver import find_legacy_host_user
                u = find_legacy_host_user(db, hostname) if hostname else None
                group_id = u.group_id if u else None
                ws, created = get_or_create_workspace(
                    db, project_id=project_id, slug=slug, name=name,
                    created_by=hostname, group_id=group_id
                )
                out = WorkspaceResponse.model_validate(ws).model_dump(mode="json")
                out["created"] = created
                return json.dumps(out, default=str)
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="List spec workspaces, each with a task-count summary per Kanban column. Use to discover existing workspaces before creating a new one. `project_id` scopes to one project; OMIT it to list every workspace you can access, across all projects. `slug` finds a workspace by slug in ANY project — use it when a feature spans several repositories, since project_id follows the working directory and would otherwise return empty in the other repos. An empty list means there really is none; do NOT create a second workspace for a spec that already exists elsewhere.")
async def list_spec_workspaces(
    project_id: str | None = None, slug: str | None = None
) -> str:
    try:
        def _sync_op():
            from app.routers.specs import list_all_workspaces, list_project_workspaces
        
            db = SessionLocal()
            try:
                if project_id:
                    items = list_project_workspaces(project_id, db=db)
                    if slug:
                        items = [i for i in items if i.slug == slug]
                else:
                    # Sem project_id: índice global (opcionalmente por slug). É o
                    # caminho de descoberta de quem está noutro repositório da mesma
                    # feature e não sabe sob qual project_id a spec foi criada.
                    items = list_all_workspaces(slug=slug, db=db)
                return json.dumps([i.model_dump(mode="json") for i in items], default=str)
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Update the lifecycle status of a spec workspace (planejamento/ativo/concluido/arquivado). Transitioning to concluido indexes PRD/TechSpec/Tasks for semantic search. Returns JSON with the workspace.")
async def update_spec_workspace_status(workspace_id: str, status: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import SpecWorkspaceStatus
            from app.routers.specs import WorkspaceResponse, WorkspaceStatusUpdate
            from app.routers.specs import update_workspace_status as _update_ws
        
            db = SessionLocal()
            try:
                try:
                    status_enum = SpecWorkspaceStatus(status)
                except ValueError:
                    return json.dumps(
                        {
                            "error": f"status inválido: {status}",
                            "valid": [s.value for s in SpecWorkspaceStatus],
                        }
                    )
                try:
                    ws = _update_ws(
                        uuid.UUID(workspace_id),
                        WorkspaceStatusUpdate(status=status_enum),
                        db=db,
                    )
                except HTTPException as he:
                    return f"Error: {he.detail}"
                return json.dumps(
                    WorkspaceResponse.model_validate(ws).model_dump(mode="json"), default=str
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Write a new version of a spec document (document_type = prd/techspec/tasks/adrs; alias adr→adrs) in a workspace, using optimistic concurrency. ADRs are NOT Kanban cards — store the full ADR bodies in document_type=adrs and link them from PRD/TechSpec. Pass expected_version = the version you last read (omit/null only for the very first write). On a version conflict this returns JSON {conflict: true, expected_version, current_version, current_content} so you can re-read and retry — it NEVER overwrites silently.")
async def write_spec_document(
    workspace_id: str, document_type: str, content: str, expected_version: int | None = None
) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import DocumentOrigin, SpecWorkspace, parse_document_type
            from app.routers.specs import _assert_access, get_or_create_document
            from app.utils.spec_versioning import write_document_version
        
            hostname = resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                ws_uuid = uuid.UUID(workspace_id)
                dtype = parse_document_type(document_type)
                if db.query(SpecWorkspace).filter(SpecWorkspace.id == ws_uuid).first() is None:
                    return f"Error: workspace {workspace_id} não encontrado"
                try:
                    _assert_access(db, ws_uuid)
                except HTTPException as he:
                    return f"Error: {he.detail}"
        
                doc = get_or_create_document(db, ws_uuid, dtype)
                result = write_document_version(
                    db, doc.id, content, expected_version, hostname, DocumentOrigin.mcp
                )
                if not result.conflict:
                    # Index + PLANKA mirror off the critical path: Ollama embed of a
                    # techspec routinely exceeds the MCP client ~30s timeout and would
                    # block the SSE loop even though Postgres already has the version.
                    from app.utils.spec_side_effects import schedule_document_post_write
        
                    schedule_document_post_write(
                        ws_uuid, dtype.value, mirror=True
                    )
                if result.conflict:
                    return json.dumps(
                        {
                            "conflict": True,
                            "expected_version": expected_version,
                            "current_version": result.version,
                            "current_content": result.current_content,
                        },
                        default=str,
                    )
                return json.dumps(
                    {
                        "conflict": False,
                        "document_id": str(result.document_id),
                        "version": result.version,
                    }
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Read the current version and content of a spec document (document_type = prd/techspec/tasks/adrs; alias adr→adrs) in a workspace. Call this to load the latest content and version BEFORE writing an update (pass that version as write_spec_document's expected_version). For Architecture Decision Records, use document_type=adrs.")
async def read_spec_document(workspace_id: str, document_type: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import SpecDocument, parse_document_type
            from app.routers.specs import _assert_access
        
            db = SessionLocal()
            try:
                ws_uuid = uuid.UUID(workspace_id)
                try:
                    _assert_access(db, ws_uuid)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                dtype = parse_document_type(document_type)
                doc = (
                    db.query(SpecDocument)
                    .filter(
                        SpecDocument.workspace_id == ws_uuid,
                        SpecDocument.document_type == dtype,
                    )
                    .first()
                )
                if doc is None:
                    return json.dumps(
                        {"found": False, "workspace_id": workspace_id, "document_type": dtype.value}
                    )
                return json.dumps(
                    {
                        "found": True,
                        "document_id": str(doc.id),
                        "document_type": dtype.value,
                        "current_version": doc.current_version,
                        "current_content": doc.current_content,
                    },
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Semantic search over specs (PRD/TechSpec/Tasks/ADRs) across projects, to find existing work or reuse prior knowledge. By default only COMPLETED specs are searched. Pass statuses to include work in progress — e.g. statuses=['ativo','planejamento'] or statuses=['*'] for any state — which is how you find a spec that is still being written. `project` is an optional filter. Returns a JSON list ranked by relevance; an empty list when nothing matches (never an error).")
async def search_specs(
    query: str, project: str | None = None, statuses: list[str] | None = None
) -> str:
    try:
        def _sync_op():
            from app.utils.permissions import get_accessible_spec_workspace_ids
            from app.utils.spec_auth import is_legacy_spec_access_open, resolve_spec_subject
            from app.utils.spec_search import search_specs as _search_specs

            requester_group = requester_group_for_mcp(user_id_var.get(None))
            subject_type, subject_id = resolve_spec_subject()
            db = SessionLocal()
            try:
                accessible = (
                    None
                    if is_legacy_spec_access_open()
                    else get_accessible_spec_workspace_ids(db, subject_type, subject_id)
                )
            finally:
                db.close()
            results = _search_specs(
                query,
                project_id=project,
                requester_group=requester_group,
                accessible_workspace_ids=accessible,
                statuses=statuses,
            )
            return json.dumps({"results": results}, default=str)
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tools da loja interna (AgentRegistry + InstallRecipeService).
# Publish/install exigem confirmação explícita no schema da tool e na descrição.
# --------------------------------------------------------------------------- #
def get_agent_registry_client():
    from app.utils.agentregistry import AgentRegistryHttpClient

    return AgentRegistryHttpClient()


def get_catalog_install_recipe_service():
    from app.services.store_recipes import InstallRecipeService

    return InstallRecipeService()


def _registry_auth_headers_from_request(request: Request) -> dict[str, str] | None:
    from app.utils.agentregistry import auth_headers_from_http_request

    return auth_headers_from_http_request(request)


def _registry_auth_headers_for_mcp() -> dict[str, str] | None:
    from app.utils.agentregistry import resolve_registry_auth_headers

    return resolve_registry_auth_headers(registry_auth_headers_var.get(None))


def _current_mcp_actor_id() -> str:
    if auth_user_var.get():
        return auth_user_var.get()
    if team_var.get():
        return f"team:{team_var.get()}"
    if auth_method_var.get() == "legacy":
        return "legacy"
    hostname = resolve_hostname(user_id_var.get(None))
    return hostname or "mcp"


@mcp.tool(description="Search the internal ShareMem catalog across skills, hooks, MCP servers, prompts, agents and plugins via AgentRegistry. Safe read-only operation. `query` may be empty to list recent resources; `kind` optionally narrows to skill|hook|mcpserver|prompt|agent|plugin. Returns JSON results with summaries and raw resources.")
async def search_catalog(
    query: str = "",
    kind: str | None = None,
    limit: int = 20,
    namespace: str = "all",
) -> str:
    try:
        from app.utils.agentregistry import (
            KIND_TO_REGISTRY_COLLECTION,
            AgentRegistryError,
            clamp_limit,
            resource_matches_query,
            summarize_resource,
            validate_catalog_kind,
        )

        client = get_agent_registry_client()
        safe_limit = clamp_limit(limit)
        kinds = [validate_catalog_kind(kind)] if kind else list(KIND_TO_REGISTRY_COLLECTION)
        results: list[dict[str, Any]] = []
        for catalog_kind in kinds:
            payload = await client.list_resources(
                kind=catalog_kind,
                namespace=namespace,
                limit=safe_limit,
                auth_headers=_registry_auth_headers_for_mcp(),
            )
            items = payload.get("items")
            if not isinstance(items, list):
                items = []
            for resource in items:
                if not isinstance(resource, dict):
                    continue
                if not resource_matches_query(resource, query):
                    continue
                summary = summarize_resource(resource)
                summary["resource"] = resource
                results.append(summary)
                if len(results) >= safe_limit:
                    return json.dumps({"results": results, "limit": safe_limit}, default=str)
        return json.dumps({"results": results, "limit": safe_limit}, default=str)
    except AgentRegistryError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Get one internal catalog resource from AgentRegistry by kind, name and optional tag. Safe read-only operation. Use this before asking for an install recipe so the developer can inspect the resource metadata.")
async def get_catalog_resource(
    kind: str,
    name: str,
    tag: str | None = None,
    namespace: str = "default",
) -> str:
    try:
        from app.utils.agentregistry import AgentRegistryError

        resource = await get_agent_registry_client().get_resource(
            kind=kind,
            name=name,
            tag=tag,
            namespace=namespace,
            auth_headers=_registry_auth_headers_for_mcp(),
        )
        return json.dumps({"resource": resource}, default=str)
    except AgentRegistryError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Publish or update a catalog resource through AgentRegistry POST /v0/apply. Only call after an explicit developer request to publish/update; pass confirm_user_requested=true to acknowledge that request. `resource` must be a v1alpha1 resource object.")
async def publish_catalog_resource(
    resource: dict[str, Any],
    dry_run: bool = False,
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.utils.agentregistry import AgentRegistryError

        if not confirm_user_requested:
            return "Error: publish_catalog_resource exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        result = await get_agent_registry_client().apply_resource(
            resource=resource,
            dry_run=dry_run,
            auth_headers=_registry_auth_headers_for_mcp(),
        )
        return json.dumps({"result": result, "dry_run": bool(dry_run)}, default=str)
    except AgentRegistryError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Publica uma Skill completa na Store. Informe nome, descrição em PT-BR e todos os arquivos da pasta, incluindo SKILL.md. Arquivos binários devem usar encoding=base64. Exige pedido explícito e confirm_user_requested=true.")
async def publish_skill_package(
    name: str,
    description: str,
    files: list[dict[str, Any]],
    tag: str = "latest",
    title: str | None = None,
    language: str = "pt-BR",
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.services.skill_packages import SkillPackageInput, build_skill_archive
        from app.utils.agentregistry import AgentRegistryError

        if not confirm_user_requested:
            return "Error: publish_skill_package exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        payload = SkillPackageInput(
            name=name,
            tag=tag,
            title=title,
            description=description,
            language=language,
            files=files,
        )
        archive, inventory = build_skill_archive(payload)
        client = get_agent_registry_client()
        resource = {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Skill",
            "metadata": {"name": name, "tag": tag},
            "spec": {
                "title": title or name,
                "description": description,
                "language": language,
            },
        }
        auth_headers = _registry_auth_headers_for_mcp()
        apply_result = await client.apply_resource(resource=resource, auth_headers=auth_headers)
        artifact_result = await client.put_skill_artifact(
            name=name, tag=tag, archive=archive, auth_headers=auth_headers
        )
        return json.dumps(
            {
                "resource": resource,
                "apply": apply_result,
                "artifact": {
                    "sha256": hashlib.sha256(archive).hexdigest(),
                    "size": len(archive),
                    "files": inventory,
                    "transport": artifact_result,
                },
            },
            default=str,
        )
    except (ValueError, AgentRegistryError) as e:
        return f"Error: {getattr(e, 'detail', str(e))}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Publica um Hook completo na Store. Informe nome, descrição em PT-BR, o mapa de eventos (ex.: {\"PreToolUse\": [{\"matcher\": \"Bash\", \"hooks\": [{\"type\": \"command\", \"command\": \"python ${HOOK_DIR}/guard.py\"}]}]}) e todos os arquivos da pasta, incluindo HOOK.md. Use ${HOOK_DIR} nos comandos para apontar para arquivos do próprio pacote. Exige pedido explícito e confirm_user_requested=true.")
async def publish_hook_package(
    name: str,
    description: str,
    events: dict[str, Any],
    files: list[dict[str, Any]],
    tag: str = "latest",
    title: str | None = None,
    language: str = "pt-BR",
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.services.hook_packages import (
            HookPackageInput,
            build_hook_archive,
            validate_command_references,
        )
        from app.utils.agentregistry import AgentRegistryError

        if not confirm_user_requested:
            return "Error: publish_hook_package exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        payload = HookPackageInput(
            name=name,
            tag=tag,
            title=title,
            description=description,
            language=language,
            events=events,
            files=files,
        )
        validate_command_references(payload)
        archive, inventory = build_hook_archive(payload)
        client = get_agent_registry_client()
        resource = {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Hook",
            "metadata": {"name": name, "tag": tag},
            "spec": {
                "title": title or name,
                "description": description,
                "language": language,
                "events": payload.to_registry_events(),
            },
        }
        auth_headers = _registry_auth_headers_for_mcp()
        apply_result = await client.apply_resource(resource=resource, auth_headers=auth_headers)
        artifact_result = await client.put_package_artifact(
            kind="hook", name=name, tag=tag, archive=archive, auth_headers=auth_headers
        )
        return json.dumps(
            {
                "resource": resource,
                "apply": apply_result,
                "artifact": {
                    "sha256": hashlib.sha256(archive).hexdigest(),
                    "size": len(archive),
                    "files": inventory,
                    "transport": artifact_result,
                },
            },
            default=str,
        )
    except (ValueError, AgentRegistryError) as e:
        return f"Error: {getattr(e, 'detail', str(e))}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Exclui um Hook completo da Store por nome e tag. Operação irreversível no catálogo e exige pedido explícito com confirm_user_requested=true.")
async def delete_hook_package(
    name: str,
    tag: str = "latest",
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.utils.agentregistry import AgentRegistryError

        if not confirm_user_requested:
            return "Error: delete_hook_package exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        result = await get_agent_registry_client().delete_resource(
            kind="hook",
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers_for_mcp(),
        )
        return json.dumps({"deleted": True, "name": name, "tag": tag, "result": result})
    except AgentRegistryError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Exclui uma Skill completa da Store por nome e tag. Operação irreversível no catálogo e exige pedido explícito com confirm_user_requested=true.")
async def delete_skill_package(
    name: str,
    tag: str = "latest",
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.utils.agentregistry import AgentRegistryError

        if not confirm_user_requested:
            return "Error: delete_skill_package exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        result = await get_agent_registry_client().delete_resource(
            kind="skill",
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers_for_mcp(),
        )
        return json.dumps({"deleted": True, "name": name, "tag": tag, "result": result})
    except AgentRegistryError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Build a host-applied install recipe for a catalog resource using ShareMem InstallRecipeService. This does not write files itself, but it starts an install workflow; only call after an explicit developer request to install and pass confirm_user_requested=true.")
async def get_install_recipe(
    kind: str,
    name: str,
    tag: str,
    target: str,
    confirm_user_requested: bool = False,
) -> str:
    try:
        from app.services.store_recipes import StoreRecipeError

        if not confirm_user_requested:
            return "Error: get_install_recipe exige pedido explícito do desenvolvedor (confirm_user_requested=true)"
        recipe = await get_catalog_install_recipe_service().build(
            kind=kind,
            name=name,
            tag=tag,
            target=target,
            user_id=_current_mcp_actor_id(),
            auth_headers=_registry_auth_headers_for_mcp(),
        )
        return json.dumps({"recipe": recipe}, default=str)
    except StoreRecipeError as e:
        return f"Error: {e.detail}"
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Tools de tasks e comentários (Tarefa 8) — completam a superfície MCP do quadro
# Kanban. Mesmo molde da Tarefa 7; delegam a task_lock (Tarefa 2) e ao router
# (Tarefa 4), sem duplicar lógica de negócio.
# --------------------------------------------------------------------------- #
@mcp.tool(description="Create a task card in a spec workspace. The card starts in the 'tasks' (backlog) column. Returns JSON with the task id, status, version and kanban guidance (do_now = claim_task before coding).")
async def create_task(
    workspace_id: str, title: str, description: str | None = None, branch_ref: str | None = None
) -> str:
    try:
        def _sync_op():
            from app.routers.specs import TaskCreate, TaskResponse
            from app.routers.specs import create_task as _create_task_endpoint
            from app.utils.kanban_pipeline import enrich_status_payload
        
            db = SessionLocal()
            try:
                payload = TaskCreate(
                    workspace_id=uuid.UUID(workspace_id),
                    title=title,
                    description=description,
                    branch_ref=branch_ref,
                )
                task = _create_task_endpoint(payload, db=db)
                data = TaskResponse.model_validate(task).model_dump(mode="json")
                return json.dumps(
                    enrich_status_payload(data, data.get("status") or "tasks", db=db),
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Claim a task so you become its assignee and it moves to 'em_andamento'. On success the JSON includes kanban={column,label,means,do_now,next_column,next_action,pipeline,pipeline_rule} — you MUST follow do_now before advancing. IDEMPOTENT FOR YOU: if you are already the assignee, calling this again re-claims the card from ANY column and renews the lease — that is how you send a card back from revisao_codigo/fase_teste to em_andamento when a check failed, and how you renew a claim before it expires. UNASSIGNED CARD OUTSIDE BACKLOG (e.g. created in PLANKA directly in revisao_codigo): claim assigns you and KEEPS the current column (status in the response). It only fails by exclusivity when the card is active with a DIFFERENT assignee (claimed=false) — do NOT retry blindly. LEASE: a claim expires after a window of inactivity (SPEC_TASK_TIMEOUT_HOURS, default 24h) and the card returns to the backlog; the response carries claim_expires_at, and any action on the card (status change, edit, re-claim) renews it. Pipeline: em_andamento → revisao_codigo → fase_teste → concluido (never skip).")
async def claim_task(task_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import TaskCard
            from app.routers.specs import _assert_access
            from app.utils.kanban_pipeline import enrich_status_payload
            from app.utils.task_lock import claim_task as _claim_task
        
            claimant = resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                tid = uuid.UUID(task_id)
                task = db.query(TaskCard).filter(TaskCard.id == tid).first()
                if task is None:
                    return f"Error: task {task_id} não encontrada"
                try:
                    _assert_access(db, task.workspace_id)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                result = _claim_task(db, tid, claimant)
                if result.claimed:
                    # Adoção de card sem dono fora do backlog mantém a coluna.
                    status_after = result.status or "em_andamento"
                    return json.dumps(
                        enrich_status_payload(
                            {
                                "claimed": True,
                                "assignee": claimant,
                                "version": result.version,
                                "status": status_after,
                                # Prazo do lease: passado este ponto sem atividade, o
                                # card volta ao backlog sozinho.
                                "claim_expires_at": result.expires_at,
                            },
                            status_after,
                            db=db,
                        ),
                        default=str,
                    )
                if result.archived:
                    return json.dumps(
                        {
                            "claimed": False,
                            "archived": True,
                            "current_assignee": result.current_assignee,
                            "version": result.version,
                            "message": (
                                "Card arquivado — está fora do fluxo. Para retomá-lo, "
                                "chame unarchive_task(task_id, expected_version) antes."
                            ),
                        }
                    )
                return json.dumps(
                    {
                        "claimed": False,
                        "current_assignee": result.current_assignee,
                        "version": result.version,
                        "message": (
                            "Task já está ativa com OUTRO responsável — escolha outra. "
                            "Consulte o quadro (list_tasks) antes de tentar de novo."
                        ),
                    }
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Release a task you no longer work on: it returns to the 'tasks' column, unassigned, and its block marker is cleared. JSON includes kanban guidance for the backlog column. Returns JSON with the new version.")
async def release_task(task_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import TaskCard
            from app.routers.specs import _assert_access
            from app.utils.kanban_pipeline import enrich_status_payload
            from app.utils.task_lock import TaskStatusPolicyError
            from app.utils.task_lock import release_task as _release_task
        
            actor = resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                tid = uuid.UUID(task_id)
                task = db.query(TaskCard).filter(TaskCard.id == tid).first()
                if task is None:
                    return f"Error: task {task_id} não encontrada"
                try:
                    _assert_access(db, task.workspace_id)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                try:
                    result = _release_task(db, tid, actor, reason="release via MCP")
                except TaskStatusPolicyError as exc:
                    return json.dumps(
                        {"policy": True, "code": exc.code, "message": exc.message}
                    )
                return json.dumps(
                    enrich_status_payload(
                        {"released": True, "version": result.version, "status": "tasks"},
                        "tasks",
                        db=db,
                    )
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Move a task to a new Kanban column and/or set its block marker, using optimistic concurrency. new_status: tasks|em_andamento|revisao_codigo|fase_teste|concluido. MANDATORY: advance ONE column at a time (em_andamento→revisao_codigo→fase_teste→concluido); skipping is rejected with policy skip_pipeline. On success JSON includes kanban={means,do_now,next_column,...} — ALWAYS execute do_now before the next move. Entering em_andamento MUST use claim_task; returning to tasks MUST use release_task. Pass expected_version. Blocker: same status + is_blocked=true + block_reason.")
async def update_task_status(
    task_id: str,
    new_status: str,
    expected_version: int,
    is_blocked: bool | None = None,
    block_reason: str | None = None,
) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import TaskCard, TaskCardStatus
            from app.routers.specs import _assert_access
            from app.utils.kanban_pipeline import enrich_status_payload
            from app.utils.task_lock import TaskStatusPolicyError
            from app.utils.task_lock import update_task_status as _update_task_status
        
            actor = resolve_hostname(user_id_var.get(None))
            try:
                status_enum = TaskCardStatus(new_status)
            except ValueError:
                return json.dumps(
                    {
                        "error": f"status inválido: {new_status}",
                        "valid": [s.value for s in TaskCardStatus],
                    }
                )
            db = SessionLocal()
            try:
                tid = uuid.UUID(task_id)
                task = db.query(TaskCard).filter(TaskCard.id == tid).first()
                if task is None:
                    return f"Error: task {task_id} não encontrada"
                try:
                    _assert_access(db, task.workspace_id)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                try:
                    result = _update_task_status(
                        db,
                        tid,
                        status_enum,
                        expected_version,
                        actor,
                        is_blocked=is_blocked,
                        block_reason=block_reason,
                    )
                except TaskStatusPolicyError as exc:
                    return json.dumps(
                        {"policy": True, "code": exc.code, "message": exc.message}
                    )
                if result.conflict:
                    return json.dumps(
                        {
                            "conflict": True,
                            "current_version": result.version,
                            "current_status": result.status,
                        }
                    )
                return json.dumps(
                    enrich_status_payload(
                        {
                            "updated": True,
                            "status": result.status,
                            "version": result.version,
                        },
                        result.status,
                        db=db,
                    )
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Add a comment to a workspace, document or task. target_type must be one of: workspace, document, task; target_id is that object's id. Returns JSON with the comment id.")
async def add_spec_comment(target_type: str, target_id: str, body: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import CommentTargetType
            from app.routers.specs import CommentCreate, CommentResponse
            from app.routers.specs import create_comment as _create_comment_endpoint
        
            author = (
                (auth_user_var.get() or "").strip()
                if auth_method_var.get() == "agent_token"
                else ""
            ) or resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                payload = CommentCreate(
                    target_type=CommentTargetType(target_type),
                    target_id=uuid.UUID(target_id),
                    body=body,
                    author=author,
                )
                try:
                    comment = _create_comment_endpoint(payload, db=db)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                return json.dumps(
                    CommentResponse.model_validate(comment).model_dump(mode="json"), default=str
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="List the comments of a workspace, document or task, oldest first. target_type must be one of: workspace, document, task; target_id is that object's id. Use it to read back code-review notes and test evidence recorded on a card — including ones written in an earlier session. Returns a JSON list; empty when there are none (never an error).")
async def list_spec_comments(target_type: str, target_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import CommentTargetType
            from app.routers.specs import CommentResponse
            from app.routers.specs import list_comments as _list_comments_endpoint
        
            db = SessionLocal()
            try:
                try:
                    ttype = CommentTargetType(target_type)
                except ValueError:
                    return json.dumps(
                        {
                            "error": f"target_type inválido: {target_type}",
                            "valid": [t.value for t in CommentTargetType],
                        }
                    )
                try:
                    comments = _list_comments_endpoint(ttype, uuid.UUID(target_id), db=db)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                return json.dumps(
                    {
                        "results": [
                            CommentResponse.model_validate(c).model_dump(mode="json")
                            for c in comments
                        ]
                    },
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Read a task card in full, including its `description` (requirements, subtasks, relevant files, deliverables, test cases, acceptance criteria) and `version`. Call this before claim_task to decide whether to take the card, and to build the execution checklist. Returns JSON with the card plus kanban guidance.")
async def get_task(task_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.routers.specs import TaskResponse
            from app.routers.specs import get_task as _get_task_endpoint
            from app.utils.kanban_pipeline import enrich_status_payload
        
            db = SessionLocal()
            try:
                try:
                    task = _get_task_endpoint(uuid.UUID(task_id), db=db)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                data = TaskResponse.model_validate(task).model_dump(mode="json")
                return json.dumps(
                    enrich_status_payload(data, data.get("status") or "tasks", db=db),
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="List the task cards of a spec workspace, optionally filtered by Kanban column (tasks/em_andamento/revisao_codigo/fase_teste/concluido). This is how you go from a workspace to a claimable task_id without copying it from the web UI. Each item carries `version`, which update_task_status requires as expected_version — no extra read needed. Pass include_description=true to also get each card's body. Archived cards (see archive_task) are hidden by default; pass include_archived=true to list them too (they carry archived_at). Returns a JSON list; empty when there are none (never an error).")
async def list_tasks(
    workspace_id: str,
    status: str | None = None,
    include_description: bool = False,
    include_archived: bool = False,
) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import TaskCardStatus
            from app.routers.specs import TaskResponse
            from app.routers.specs import list_workspace_tasks as _list_tasks_endpoint
        
            db = SessionLocal()
            try:
                status_enum = None
                if status:
                    try:
                        status_enum = TaskCardStatus(status)
                    except ValueError:
                        return json.dumps(
                            {
                                "error": f"status inválido: {status}",
                                "valid": [s.value for s in TaskCardStatus],
                            }
                        )
                try:
                    tasks = _list_tasks_endpoint(
                        uuid.UUID(workspace_id),
                        status=status_enum,
                        include_archived=include_archived,
                        db=db,
                    )
                except HTTPException as he:
                    return f"Error: {he.detail}"
        
                results = []
                for t in tasks:
                    data = TaskResponse.model_validate(t).model_dump(mode="json")
                    if not include_description:
                        # Omitido por padrão: o corpo enriquecido de um card é longo, e
                        # listar um workspace inteiro com todos eles estoura o contexto
                        # de quem só quer escolher o que puxar.
                        data.pop("description", None)
                    results.append(data)
                return json.dumps({"results": results}, default=str)
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Edit a task card's title, description and/or branch_ref. Use it when a decision changes after the cards were created — in a Spec-Driven flow it is normal and desirable for the spec to change when a measurement contradicts the hypothesis, and a card whose title no longer matches what has to be done misleads whoever picks it up. Omitted fields are left untouched. Pass expected_version = the version you last read; on a version conflict this returns {conflict: true, current_version, ...} and changes NOTHING — re-read with get_task and retry. Editing also renews the claim lease.")
async def update_task(
    task_id: str,
    expected_version: int,
    title: str | None = None,
    description: str | None = None,
    branch_ref: str | None = None,
) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.models import TaskCard
            from app.routers.specs import _assert_access
            from app.utils.task_lock import update_task_metadata
        
            db = SessionLocal()
            try:
                tid = uuid.UUID(task_id)
                task = db.query(TaskCard).filter(TaskCard.id == tid).first()
                if task is None:
                    return f"Error: task {task_id} não encontrada"
                try:
                    _assert_access(db, task.workspace_id)
                except HTTPException as he:
                    return f"Error: {he.detail}"
        
                if title is None and description is None and branch_ref is None:
                    return json.dumps(
                        {
                            "error": "nada a atualizar",
                            "hint": "informe title, description e/ou branch_ref",
                        }
                    )
        
                result = update_task_metadata(
                    db,
                    tid,
                    expected_version,
                    title=title,
                    description=description,
                    branch_ref=branch_ref,
                )
                if result.conflict:
                    return json.dumps(
                        {
                            "conflict": True,
                            "expected_version": expected_version,
                            "current_version": result.version,
                            "current_title": result.title,
                            "current_description": result.description,
                            "current_branch_ref": result.branch_ref,
                        },
                        default=str,
                    )
                return json.dumps(
                    {
                        "updated": True,
                        "version": result.version,
                        "title": result.title,
                        "description": result.description,
                        "branch_ref": result.branch_ref,
                    },
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="List a task card's column-change history, oldest first. Each entry has old_status, new_status, changed_by, changed_at and by_timeout. Use it to tell an automatic lease expiry (by_timeout=true, changed_by='system:timeout') apart from someone releasing the card on purpose — a card that went back to the backlog on its own is otherwise indistinguishable from one that was released. Returns a JSON list; empty when the card never changed column (never an error).")
async def list_task_history(task_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.routers.specs import list_task_history as _list_history_endpoint
        
            db = SessionLocal()
            try:
                try:
                    rows = _list_history_endpoint(uuid.UUID(task_id), db=db)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                return json.dumps(
                    {"results": [r.model_dump(mode="json") for r in rows]}, default=str
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Delete a task card and its status history and comments. Use it to remove a card created by mistake or duplicated — without this, a test card stays on the board forever, which discourages validating against the real server. Irreversible; it does NOT touch memories. Returns JSON {deleted: true, task_id}.")
async def delete_task(task_id: str) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException
        
            from app.routers.specs import delete_task as _delete_task_endpoint
        
            db = SessionLocal()
            try:
                try:
                    _delete_task_endpoint(uuid.UUID(task_id), db=db)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                return json.dumps({"deleted": True, "task_id": task_id})
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


async def _set_task_archived(
    task_id: str, expected_version: int, *, archive: bool, reason: str | None = None
) -> str:
    try:
        def _sync_op():
            from fastapi import HTTPException

            from app.models import TaskCard
            from app.routers.specs import _assert_access
            from app.utils.task_lock import TaskStatusPolicyError
            from app.utils.task_lock import archive_task as _archive
            from app.utils.task_lock import unarchive_task as _unarchive

            actor = resolve_hostname(user_id_var.get(None))
            db = SessionLocal()
            try:
                tid = uuid.UUID(task_id)
                task = db.query(TaskCard).filter(TaskCard.id == tid).first()
                if task is None:
                    return f"Error: task {task_id} não encontrada"
                try:
                    _assert_access(db, task.workspace_id)
                except HTTPException as he:
                    return f"Error: {he.detail}"
                try:
                    if archive:
                        result = _archive(db, tid, expected_version, actor, reason=reason)
                    else:
                        result = _unarchive(db, tid, expected_version, actor)
                except TaskStatusPolicyError as exc:
                    return json.dumps(
                        {"policy": True, "code": exc.code, "message": exc.message}
                    )
                if result.conflict:
                    return json.dumps(
                        {
                            "conflict": True,
                            "expected_version": expected_version,
                            "current_version": result.version,
                            "archived_at": result.archived_at,
                        },
                        default=str,
                    )
                return json.dumps(
                    {
                        "archived": archive,
                        "task_id": task_id,
                        "version": result.version,
                        "status": task.status.value,
                        "archived_at": result.archived_at,
                        "archived_by": result.archived_by,
                    },
                    default=str,
                )
            finally:
                db.close()
        return await _run_blocking(_sync_op)
    except Exception as e:  # noqa: BLE001
        logging.exception(e)
        return f"Error: {e}"


@mcp.tool(description="Archive a task card WITHOUT deleting anything — the non-destructive alternative to delete_task. Use it for a card that was worked on and then cancelled or became obsolete: it disappears from list_tasks and the board by default, but keeps its column, assignee, status history and comments (list_tasks(include_archived=true) shows it again, with archived_at). Prefer delete_task only for a card created by mistake. An archived card cannot be claimed or moved until unarchive_task. Pass expected_version; on a version conflict this returns {conflict: true, current_version} and changes NOTHING. A card active with ANOTHER assignee is refused (policy not_assignee). Optional reason is recorded in the audit log.")
async def archive_task(task_id: str, expected_version: int, reason: str | None = None) -> str:
    return await _set_task_archived(task_id, expected_version, archive=True, reason=reason)


@mcp.tool(description="Undo archive_task: the card reappears in list_tasks and on the board in the same column it was in, with history and comments intact. Pass expected_version; on a version conflict this returns {conflict: true, current_version} and changes NOTHING.")
async def unarchive_task(task_id: str, expected_version: int) -> str:
    return await _set_task_archived(task_id, expected_version, archive=False)


def _warn_invalid_mcp_hostname(raw_uid: str | None) -> None:
    if raw_uid and not is_plausible_hostname(str(raw_uid).strip()):
        logging.warning(
            "hostname MCP inválido — use ${env:COMPUTERNAME} nos comandos PowerShell "
            "(não $env:COMPUTERNAME antes de ?token=): %s",
            str(raw_uid)[:160],
        )


@mcp_router.get("/{client_name}/sse/{user_id}")
async def handle_sse(request: Request):
    """Handle SSE connections for a specific user and client"""
    # Extract user_id and client_name from path parameters
    path_uid = request.path_params.get("user_id")
    _warn_invalid_mcp_hostname(path_uid)
    uid = _effective_mcp_uid(path_uid)
    user_token = user_id_var.set(uid or "")
    client_name = request.path_params.get("client_name")
    client_token = client_name_var.set(client_name or "")
    registry_token = registry_auth_headers_var.set(_registry_auth_headers_from_request(request))
    # ?group= na URL de instalação: vincula equipe na primeira conexão (ADR-004).
    
    def _sync_sse_setup():
        ensure_user_group(uid, request.query_params.get("group"))
        # Token de agente em máquina não vinculada: log estruturado (Fase 2 trata).
        _log_machine_divergence_if_any(path_uid)
        
    await _run_blocking(_sync_sse_setup)

    try:
        # NOTE: request._send is the raw ASGI `send` callable. Starlette does not
        # expose it publicly, but the MCP SDK transports require the raw ASGI
        # interface (scope, receive, send). This is the standard pattern from the
        # MCP Python SDK examples.
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )
    finally:
        # Clean up context variables
        registry_auth_headers_var.reset(registry_token)
        user_id_var.reset(user_token)
        client_name_var.reset(client_token)

    # Body already written via request._send — returning JSON null caused a second
    # http.response.start (content-length 4) and killed the SSE session.
    return _McpStreamAlreadySentResponse()


class _McpStreamAlreadySentResponse(Response):
    """No-op ASGI response when the MCP transport already wrote via ``request._send``."""

    def __init__(self) -> None:
        super().__init__(content=b"")

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        return


@mcp_router.post("/messages/")
async def handle_get_message(request: Request):
    return await _handle_post_message_impl(request)


@mcp_router.post("/{client_name}/sse/{user_id}/messages/")
async def handle_post_message(request: Request):
    return await _handle_post_message_impl(request)

async def _handle_post_message_impl(request: Request):
    """Handle POST messages for SSE"""
    response_status = 202

    try:
        body = await request.body()

        # Create a simple receive function that returns the body
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]

        # The SSE transport owns the response status. Preserve errors such as
        # 404 for an expired/missing session so clients reconnect immediately
        # instead of treating the request as accepted and retrying for minutes.
        # Call handle_post_message with the correct arguments
        await sse.handle_post_message(request.scope, receive, send)

        return Response(status_code=response_status)
    finally:
        pass


@mcp_router.api_route("/{client_name}/http/{user_id}", methods=["POST", "GET", "DELETE"])
async def handle_streamable_http(request: Request):
    """Handle Streamable HTTP connections for a specific user and client.

    Uses the Streamable HTTP transport (MCP spec 2025-03-26+) which replaces
    the deprecated SSE transport. Runs in stateless mode — each request is
    handled independently with no persistent session.

    The transport writes its response directly to the ASGI ``send`` callable.
    We intercept it via ``capture_send`` so we can return a proper ``Response``
    to FastAPI — otherwise FastAPI would also try to send its own response,
    causing a "double-response" bug.
    """
    path_uid = request.path_params.get("user_id")
    _warn_invalid_mcp_hostname(path_uid)
    uid = _effective_mcp_uid(path_uid)
    user_token = user_id_var.set(uid or "")
    client_name = request.path_params.get("client_name")
    client_token = client_name_var.set(client_name or "")
    registry_token = registry_auth_headers_var.set(_registry_auth_headers_from_request(request))
    
    def _sync_http_setup():
        ensure_user_group(uid, request.query_params.get("group"))
        # Token de agente em máquina não vinculada: log estruturado (Fase 2 trata).
        _log_machine_divergence_if_any(path_uid)
        
    await _run_blocking(_sync_http_setup)

    # Intercept the ASGI messages the transport sends so we can return them
    # as a single Response to FastAPI.  Without this, FastAPI would attempt to
    # write its own response after the transport already wrote one.
    response_started = False
    response_status = 200
    response_headers: list[tuple[bytes, bytes]] = []
    response_body = bytearray()

    async def capture_send(message):
        nonlocal response_started, response_status
        if message["type"] == "http.response.start":
            response_started = True
            response_status = message["status"]
            response_headers.extend(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    try:
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
        )

        async with anyio.create_task_group() as tg:

            async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
                async with transport.connect() as (read_stream, write_stream):
                    task_status.started()
                    await mcp._mcp_server.run(
                        read_stream,
                        write_stream,
                        mcp._mcp_server.create_initialization_options(),
                        stateless=True,
                    )

            await tg.start(run_server)
            await transport.handle_request(request.scope, request.receive, capture_send)
            await transport.terminate()
            tg.cancel_scope.cancel()
    finally:
        registry_auth_headers_var.reset(registry_token)
        user_id_var.reset(user_token)
        client_name_var.reset(client_token)

    if not response_started:
        return Response(status_code=500, content=b"Transport did not produce a response")

    # Header dict conversion is safe here: the MCP transport in stateless JSON
    # mode only emits single-valued headers (Content-Type, Content-Length).
    return Response(
        content=bytes(response_body),
        status_code=response_status,
        headers={k.decode(): v.decode() for k, v in response_headers},
    )


def setup_mcp_server(app: FastAPI):
    """Setup MCP server with the FastAPI application"""
    mcp._mcp_server.name = "mem0-mcp-server"

    # Include MCP router in the FastAPI app
    app.include_router(mcp_router)
