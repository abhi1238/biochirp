"""Process-wide async HTTP client shared by per-DB tool workers.

The async-style workers (ttd, ctd, hcdt) historically used a
private `_HTTPX_CLIENT` singleton built at module load time. This module
extracts that singleton into one shared instance so all four can import
it identically.

Pool sizes match the original `_get_httpx_client()` body that appeared
verbatim in those four workers:

    max_keepalive_connections=64
    max_connections=128
    keepalive_expiry=300.0
    timeout=(connect=5.0, total=120.0)

Comment from the original TTD worker:
    "Replaces the blocking `requests` Session we used previously — sync
     POSTs from an async FastAPI route blocked the uvicorn worker event
     loop, capping per-worker concurrency at 1."

This shared client is paired with `post_async()` in `_worker_helpers.py`.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

_HTTPX_CLIENT: Optional[httpx.AsyncClient] = None


def get_httpx_client() -> httpx.AsyncClient:
    """Return the process-wide async HTTP client, creating it lazily.

    Reuses across the worker's lifetime; auto-recreates if a previous
    client was closed (rare — graceful-shutdown edge case).

    Concurrency note: the constructor is sync, so we can't use asyncio.Lock
    here without making this function async (which would touch every caller).
    Cold-start races are rare and benign — at worst one client is created and
    immediately replaced; the orphan has no in-flight requests so its pool
    is empty. Real shutdown leaks are prevented by `aclose_httpx_client()`
    below, wired into the per-DB FastAPI lifespan.
    """
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None or _HTTPX_CLIENT.is_closed:
        _HTTPX_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=5.0),
            limits=httpx.Limits(
                max_keepalive_connections=64,
                max_connections=128,
                keepalive_expiry=300.0,
            ),
        )
    return _HTTPX_CLIENT


async def aclose_httpx_client() -> None:
    """Close the process-wide httpx client. Idempotent; safe to call on
    FastAPI lifespan shutdown. Drains the keep-alive pool so we don't leak
    sockets on graceful container stop.
    """
    global _HTTPX_CLIENT
    log = logging.getLogger("uvicorn.error")
    client = _HTTPX_CLIENT
    _HTTPX_CLIENT = None
    if client is None or client.is_closed:
        return
    try:
        await client.aclose()
    except Exception as e:
        log.warning("httpx aclose failed: %s", e)


__all__ = ["get_httpx_client", "aclose_httpx_client"]
