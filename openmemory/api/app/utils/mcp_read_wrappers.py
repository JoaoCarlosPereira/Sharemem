"""Monkey-patch MCP read tools to record access audit (avoids editing root-owned mcp_server.py).

The patch target is the ``Tool`` object inside FastMCP's registry, not the
``app.mcp_server`` module attribute: ``@mcp.tool`` stores the undecorated
function in ``Tool.fn`` at import time, so reassigning the module attribute
patches something nothing calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial, wraps
from typing import Callable

logger = logging.getLogger(__name__)
_installed = False

# Background audit writes in flight. Strong references keep the tasks alive
# until done (asyncio only holds weak refs); tests await them via
# :func:`drain_pending_read_audits`.
_pending: set[asyncio.Future] = set()


def _audit_results(
    *,
    project: str | None,
    results: list[dict],
    access_type: str,
    query: str | None = None,
    hostname: str | None = None,
    client_name: str | None = None,
) -> None:
    from app.utils.project_name import normalize_project
    from app.utils.read_audit import record_memory_reads

    record_memory_reads(
        # Mesma chave efetiva usada pela tool (ver app.utils.project_name).
        project=normalize_project(project),
        memory_ids=[r.get("id") for r in results],
        access_type=access_type,
        source="mcp",
        hostname=hostname,
        client_name=client_name,
        query=query,
        items=results,
    )


def _schedule_audit(**kwargs) -> None:
    """Record the read in a worker thread without delaying the MCP response.

    ``record_memory_reads`` is one batched INSERT + commit for all result rows,
    but it is synchronous DB I/O; running it inline on the event loop stalled
    every MCP session on the same worker. Identity (hostname/client) must be
    captured by the caller *before* scheduling — contextvars are read here.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _audit_results(**kwargs)
        return
    future = loop.run_in_executor(None, partial(_audit_results, **kwargs))
    _pending.add(future)

    def _done(fut: asyncio.Future) -> None:
        _pending.discard(fut)
        exc = fut.exception() if not fut.cancelled() else None
        if exc is not None:
            logger.warning("mcp read-audit failed: %s", exc)

    future.add_done_callback(_done)


async def drain_pending_read_audits(timeout: float | None = None) -> bool:
    """Await in-flight background audit writes (tests / graceful shutdown).

    With ``timeout`` (seconds) gives up after that long so a stuck DB never
    blocks shutdown; returns ``True`` when everything was flushed.
    """

    async def _drain() -> None:
        while _pending:
            batch = list(_pending)
            await asyncio.gather(*batch, return_exceptions=True)
            # Done-callbacks also discard, but do it here so draining never spins.
            _pending.difference_update(batch)

    if timeout is None:
        await _drain()
        return True
    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("read-audit: %d pending write(s) not flushed before shutdown", len(_pending))
        return False
    return True


def _wrap_search(fn: Callable) -> Callable:
    @wraps(fn)
    async def wrapper(
        query: str,
        project: str,
        rerank: bool = False,
        strict_project: bool = False,
        include_obsolete: bool = False,
        task: str | None = None,
    ) -> str:
        # A assinatura tem de acompanhar a da tool: o FastMCP valida contra o
        # schema da funcao ORIGINAL e chama esta com os mesmos kwargs, entao um
        # parametro novo que nao chegue aqui vira TypeError em tempo de dispatch.
        out = await fn(
            query,
            project,
            rerank=rerank,
            strict_project=strict_project,
            include_obsolete=include_obsolete,
            task=task,
        )
        try:
            from app.mcp_server import (
                DEFAULT_CLIENT_NAME,
                client_name_var,
                user_id_var,
            )
            from app.utils.identity import resolve_hostname

            payload = json.loads(out)
            results = payload.get("results") or []
            if isinstance(results, list) and results:
                _schedule_audit(
                    project=project,
                    results=results,
                    access_type="search",
                    query=query,
                    hostname=resolve_hostname(user_id_var.get(None)),
                    client_name=client_name_var.get(None) or DEFAULT_CLIENT_NAME,
                )
        except Exception:  # noqa: BLE001
            logger.debug("mcp search read-audit skipped", exc_info=True)
        return out

    return wrapper


def _wrap_list(fn: Callable) -> Callable:
    @wraps(fn)
    async def wrapper(
        project: str, limit: int | None = None, include_obsolete: bool = False
    ) -> str:
        from app.mcp_server import DEFAULT_LIST_TOP_K

        out = await fn(
            project,
            limit=DEFAULT_LIST_TOP_K if limit is None else limit,
            include_obsolete=include_obsolete,
        )
        try:
            from app.mcp_server import (
                DEFAULT_CLIENT_NAME,
                client_name_var,
                user_id_var,
            )
            from app.utils.identity import resolve_hostname

            payload = json.loads(out)
            results = payload.get("results") or []
            if isinstance(results, list) and results:
                _schedule_audit(
                    project=project,
                    results=results,
                    access_type="list",
                    hostname=resolve_hostname(user_id_var.get(None)),
                    client_name=client_name_var.get(None) or DEFAULT_CLIENT_NAME,
                )
        except Exception:  # noqa: BLE001
            logger.debug("mcp list read-audit skipped", exc_info=True)
        return out

    return wrapper


def _registered_tool(mcp_obj, name: str):
    """Return the ``Tool`` object FastMCP dispatches to, or ``None``."""
    manager = getattr(mcp_obj, "_tool_manager", None)
    if manager is None:
        return None
    getter = getattr(manager, "get_tool", None)
    if callable(getter):
        return getter(name)
    return (getattr(manager, "_tools", None) or {}).get(name)


def _patch_registered_tool(mcp_obj, name: str, factory: Callable) -> bool:
    """Wrap the function held by the FastMCP registry. True when it took effect.

    Rebinding the module attribute is NOT enough: ``@mcp.tool`` captures the
    function object at import time into ``Tool.fn``, and dispatch calls that
    reference — so a module-level reassignment leaves every MCP read unaudited.
    """
    tool = _registered_tool(mcp_obj, name)
    original = getattr(tool, "fn", None)
    if tool is None or not callable(original):
        return False
    wrapped = factory(original)
    try:
        tool.fn = wrapped
    except Exception:  # noqa: BLE001 — pydantic model guarding assignment
        object.__setattr__(tool, "fn", wrapped)
    # Verify instead of trusting: the whole point of this module is that a patch
    # can look installed while dispatch still reaches the unwrapped function.
    return getattr(_registered_tool(mcp_obj, name), "fn", None) is wrapped


def install_mcp_read_audit() -> None:
    global _installed
    if _installed:
        return
    try:
        from app import mcp_server as mcp_mod
    except Exception:  # noqa: BLE001
        logger.warning("could not import mcp_server for read-audit", exc_info=True)
        return

    _installed = True  # one attempt only: re-running would double-wrap.
    patched: list[str] = []
    failed: list[str] = []
    for name, factory in (("search_memory", _wrap_search), ("list_memories", _wrap_list)):
        try:
            ok = _patch_registered_tool(mcp_mod.mcp, name, factory)
        except Exception:  # noqa: BLE001
            logger.warning("read-audit wrapper failed for %s", name, exc_info=True)
            ok = False
        if ok:
            patched.append(name)
            # Keep the module attribute in sync so direct callers are audited too.
            setattr(mcp_mod, name, _registered_tool(mcp_mod.mcp, name).fn)
        else:
            failed.append(name)

    if failed:
        # Loud on purpose: silently skipping the audit is what hid this for months.
        logger.error(
            "MCP read-audit NOT installed for %s — those reads will go unrecorded",
            ", ".join(failed),
        )
    if patched:
        logger.info("MCP read-audit wrappers installed for %s", ", ".join(patched))
