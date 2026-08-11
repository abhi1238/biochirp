"""Shared helpers extracted from the top ~120 lines of every DB worker.

Each `app/tools/<db>/app/<db>.py` previously redefined the same six helpers
(`_get_redis`, `_post`, `_valid_columns`, `_publish_ws`, plus inline Redis
setup and prompt loading). 23 of 24 had byte-for-byte identical bodies
after DB-name substitution; the 24th (`ttd`) uses `httpx.AsyncClient` and
keeps its own.

Worker files now import what they need:

    from app.per_db_tool._worker_helpers import (
        get_redis, post_with_retry, valid_columns, publish_ws,
    )

The `_HTTP_SESSION` re-export lives at the package root
(`from app.per_db_tool import HTTP_SESSION`) and is shared with the
post-helper here.

Log prefix:
    Each worker passes its own logger to the helpers. Where the original
    code formatted log lines as `"[<db>] POST %s failed: %s"`, the
    helpers now take a logger and emit through it, preserving the
    "[<db>] " prefix via the caller's logger name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Iterable, Optional

import redis.asyncio as redis

from ._http_session import HTTP_SESSION

# ----- Logging (one-time setup, applied on first import) --------------------
# Every per-DB worker used to repeat this block byte-for-byte. basicConfig is
# a no-op once handlers exist, so re-imports / parallel workers are safe.
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ----- Redis ----------------------------------------------------------------
_REDIS_SINGLETON: dict[str, redis.Redis | None] = {"client": None}
# Lazily-created in get_redis() — asyncio.Lock must be constructed inside an
# event loop, so we can't create it at module-import time (no loop yet).
_REDIS_INIT_LOCK: asyncio.Lock | None = None


async def get_redis(
    host: str | None = None,
    port: int | None = None,
    logger: logging.Logger | None = None,
) -> redis.Redis | None:
    """Lazy async Redis client.

    Falls back to env vars (REDIS_HOST/REDIS_PORT) if args not given.
    Returns None on failure (callers tolerate this). The init path is
    guarded by an asyncio.Lock so concurrent first-callers don't each
    build a client + leak the loser.
    """
    global _REDIS_INIT_LOCK
    if _REDIS_SINGLETON["client"] is not None:
        return _REDIS_SINGLETON["client"]
    host = host or os.getenv("REDIS_HOST", "biochirp_redis_tool")
    port = port or int(os.getenv("REDIS_PORT", "6379"))
    log = logger or logging.getLogger("uvicorn.error")
    if _REDIS_INIT_LOCK is None:
        _REDIS_INIT_LOCK = asyncio.Lock()
    async with _REDIS_INIT_LOCK:
        # Double-check inside the lock — another coroutine may have just
        # finished initialising.
        if _REDIS_SINGLETON["client"] is not None:
            return _REDIS_SINGLETON["client"]
        try:
            client = redis.Redis(host=host, port=port, decode_responses=True)
            await client.ping()
            _REDIS_SINGLETON["client"] = client
        except Exception as e:
            log.error("Redis init failed: %s", e)
            _REDIS_SINGLETON["client"] = None
    return _REDIS_SINGLETON["client"]


# ----- HTTP POST with retry -------------------------------------------------
# 60s was tight for the expand_and_match_db / planner POSTs under load — a
# moderately-slow call (70-90s) would fail here, burn a retry, and often fail
# again, costing MORE total latency than one longer attempt. 120s matches
# _remote_schema_map's existing timeout and stays well inside the outer MCP
# layer's 180-300s per-DB budget (mcp_server/server.py _DB_TIMEOUT*), so a
# slow-but-completing call now finishes instead of round-tripping through an
# extra failed attempt first.
_POST_TIMEOUT = float(os.getenv("POST_TIMEOUT_SECONDS", "120"))


def post_with_retry(
    url: str,
    *,
    timeout: float | None = None,
    logger: logging.Logger | None = None,
    **kw: Any,
):
    """Synchronous keep-alive POST with one retry on timeout.

    Replaces the per-worker `_post(url, **kw)` that did the same thing
    against its own `_HTTP_SESSION`. Returns the `Response` on success or
    `None` on failure (callers tolerate this).
    """
    log = logger or logging.getLogger("uvicorn.error")
    timeout = timeout or _POST_TIMEOUT
    try:
        return HTTP_SESSION.post(url, timeout=timeout, **kw)
    except Exception as e:
        # The original code retries once specifically on timeout. We treat
        # any exception the same way the original did: retry once, then
        # give up. (Original: bare except, log, retry; we preserve that.)
        e_name = type(e).__name__
        is_timeout = "Timeout" in e_name or "ReadTimeout" in e_name
        if is_timeout:
            log.warning("POST %s timed out, retrying once", url)
        else:
            log.warning("POST %s failed (%s), retrying once: %s", url, e_name, e)
        try:
            return HTTP_SESSION.post(url, timeout=timeout, **kw)
        except Exception as e2:
            log.error("POST %s failed after retry: %s", url, e2)
            return None


# ----- valid_columns --------------------------------------------------------
def valid_columns(req: dict, db: str) -> list[str]:
    """Filter `req` to only columns that exist in `database_schemas[db]`.

    Identical body across all 24 workers. Imported here to avoid the 24×
    redefinition; behaviour is unchanged.
    """
    from config.schema import database_schemas
    if not isinstance(req, dict):
        return []
    valid_cols: set[str] = set()
    for tbl_cols in database_schemas.get(db, {}).values():
        valid_cols.update(tbl_cols)
    return [c for c in req.keys() if c in valid_cols]


# ----- WS publish (table-event payload to redis pubsub) ---------------------
async def publish_ws(
    conn_id: Optional[str],
    csv_path: str,
    rows: int,
    *,
    service_name: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Publish a `<service>_table` event onto the redis pubsub channel
    keyed by `conn_id`. No-op if `conn_id` is None or redis is unreachable.

    The original per-worker `_publish_ws` constructed the event_type as
    f"{SERVICE_NAME}_table"; we preserve that exact key so the chat-side
    frontend listeners continue to match. The `service_name` arg defaults
    to the `SERVICE_NAME` env var.
    """
    if not conn_id:
        return
    service_name = service_name or os.getenv("SERVICE_NAME", "")
    log = logger or logging.getLogger("uvicorn.error")
    try:
        r = await get_redis(logger=log)
        if r is None:
            return
        await r.publish(
            conn_id,
            json.dumps({
                "type": f"{service_name}_table",
                "csv_path": csv_path,
                "row_count": rows,
            }),
        )
    except Exception as e:
        log.error("[%s][ws] Publish failed: %s", service_name, e)


# ----- async HTTP POST (for workers that use httpx.AsyncClient) -------------
async def post_async(
    url: str,
    *,
    timeout: float | None = None,
    logger: logging.Logger | None = None,
    **kw: Any,
):
    """Async keep-alive POST with one retry on timeout.

    Replaces the per-worker `async def _post(url, **kw)` that did the same
    thing against its own `_HTTPX_CLIENT`. Returns the httpx `Response`
    on success or `None` on failure.

    Used by the async-style workers (ttd, ctd, hcdt); the
    sync workers use `post_with_retry()` instead.
    """
    from ._httpx_client import get_httpx_client
    import httpx as _httpx

    log = logger or logging.getLogger("uvicorn.error")
    timeout_val = timeout if timeout is not None else _POST_TIMEOUT
    client = get_httpx_client()
    for attempt in (1, 2):
        try:
            r = await client.post(url, timeout=timeout_val, **kw)
            # Return the response regardless of status so callers can inspect
            # non-2xx codes (e.g. expand tool returns 400 for unknown databases).
            return r
        except _httpx.TimeoutException as e:
            if attempt == 1:
                log.warning("POST %s timed out, retrying once", url)
                continue
            log.error("POST %s failed after retry: %s", url, e)
            return None
        except _httpx.HTTPError as e:
            log.error("POST %s failed: %s", url, e)
            return None
        except Exception as e:  # noqa: BLE001 - mirror sync post_with_retry: never raise to caller
            # Non-httpx failures (InvalidURL, transport RuntimeError, OSError, …)
            # must return None like the sync twin so callers' `if not resp:`
            # error-handling holds. CancelledError is BaseException → still propagates.
            log.error("POST %s failed (%s): %s", url, type(e).__name__, e)
            return None
    return None  # defensive: loop always returns above, but never fall through implicitly


# ----- WS publish (planner-step tool card) ---------------------------------
async def publish_planner_step(
    conn_id: Optional[str],
    db: str,
    plan: Any,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Publish a `planner-<db>` tool card event onto the redis pubsub
    channel so the chat UI can render the plan as a chat-visible step.

    Imported and called by `_orchestrator.py` after the planner call returns
    (between expand and join). No-op if `conn_id` is None or redis is
    unreachable. Failures are swallowed by the caller in a try/except —
    a missing planner card never blocks the actual query result.

    The event shape mirrors `publish_ws`: a `tool_called` + `tool_result`
    pair keyed on `tool_id=planner-<db>`, matching the per-DB-card pattern
    the frontend (assets/decomp_tree.min.js) already listens for.
    """
    if not conn_id:
        return
    log = logger or logging.getLogger("uvicorn.error")
    try:
        r = await get_redis(logger=log)
        if r is None:
            return
        tool_id = f"planner-{db}"
        # 2026-05-21: explicit `name` field added. Previously this event
        # carried only `tool_id` — the frontend chip renderer
        # (assets/chat-main.min.js) falls back to literal "Tool" when
        # `name` is missing, producing an unnamed 3 ms chip in the chat
        # UI between the per-DB tool card and the synthesizer card. The
        # decomp_tree.min.js DAG panel keys on `tool_id`, so adding the
        # human-readable `name` here is additive — DAG rendering keeps
        # working AND the chip now displays a meaningful label.
        await r.publish(
            conn_id,
            json.dumps({
                "type": "tool_called",
                "tool_id": tool_id,
                "name": f"planner ({db})",
            }),
        )
        await r.publish(
            conn_id,
            json.dumps({
                "type": "tool_result",
                "tool_id": tool_id,
                "name": f"planner ({db})",
                "ok": True,
                "plan": plan,
            }),
        )
    except Exception as e:
        log.warning("[%s][planner-card] publish failed: %s", db, e)


__all__ = [
    "get_redis", "post_with_retry", "post_async",
    "valid_columns", "publish_ws", "publish_planner_step",
]
