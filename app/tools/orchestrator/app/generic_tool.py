"""Generic tool client — shared base for every orchestrator *_tool.py wrapper.

Mirrors the reference repo's `orchestrator_service/app/generic_tool.py`: the
orchestrator itself does no heavy work; each capability is a thin client that
POSTs to a backend microservice over HTTP. This base centralises:

  * host/port resolution from env (so wiring lives in docker-compose, not code),
  * the DB-agnostic `?database=<db>` convention,
  * a shared async httpx client with sane timeouts,
  * uniform logging + a structured event hook (tool_called / tool_result) so the
    orchestrator can stream progress to the UI exactly like the github version.

Concrete wrappers (planner_tool, router_tool, …) subclass `GenericTool`, set
`name` / `env_host` / `env_port` / `path`, and call `self.call(payload, db=...)`.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("uvicorn.error")

# Event sink: the orchestrator sets this to push tool_called/tool_result events
# onto the websocket/Redis stream. Default is a no-op so tools work standalone.
EventSink = Callable[[dict], Awaitable[None]]


async def _noop_event(_evt: dict) -> None:
    return None


class GenericTool:
    """Base HTTP client for one backend tool service."""

    #: short tool name, used in events/logs (e.g. "schema_planner")
    name: str = "generic"
    #: env var holding the backend hostname (e.g. "SCHEMA_PLANNER_HOST")
    env_host: str = ""
    #: env var holding the backend port (e.g. "SCHEMA_PLANNER_PORT")
    env_port: str = ""
    #: default host/port used when the env vars are unset (local/dev)
    default_host: str = "localhost"
    default_port: str = "8000"
    #: URL path on the backend (e.g. "/schema_planner")
    path: str = "/"
    #: per-call timeout (seconds) — subclasses override where needed
    timeout: float = 60.0

    def __init__(self, event_sink: Optional[EventSink] = None) -> None:
        self._emit: EventSink = event_sink or _noop_event

    # -- URL ------------------------------------------------------------------
    def base_url(self) -> str:
        host = os.getenv(self.env_host, self.default_host) if self.env_host else self.default_host
        port = os.getenv(self.env_port, self.default_port) if self.env_port else self.default_port
        return f"http://{host}:{port}{self.path}"

    def url(self, db: Optional[str] = None) -> str:
        url = self.base_url()
        if db:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}database={db}"
        return url

    # -- call -----------------------------------------------------------------
    async def call(self, payload: dict, db: Optional[str] = None,
                   request_id: str = "") -> Any:
        """POST `payload` to the backend; emit tool_called/tool_result events.

        Returns the parsed JSON body, or None on error (the orchestrator decides
        how to degrade — it never crashes the whole flow on one tool failing).
        """
        url = self.url(db)
        await self._emit({"type": "tool_called", "tool": self.name,
                          "db": db, "request_id": request_id})
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                body = resp.json()
            elapsed = time.perf_counter() - t0
            logger.info("[orchestrator] %s ok db=%s (%.3fs)", self.name, db, elapsed)
            await self._emit({"type": "tool_result", "tool": self.name, "db": db,
                              "ok": True, "elapsed": elapsed, "request_id": request_id})
            return body
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.warning("[orchestrator] %s FAILED db=%s (%.3fs): %s",
                           self.name, db, elapsed, exc)
            await self._emit({"type": "tool_result", "tool": self.name, "db": db,
                              "ok": False, "error": str(exc), "request_id": request_id})
            return None
