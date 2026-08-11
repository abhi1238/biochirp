"""Shared FastAPI factory for the per-DB tool-backend services.

Extracted from the ~70-line `main.py` clones at `app/tools/<db>/app/main.py`
(active services now under `app/tools/<db>/`). The only true per-service
variance is:

    * db_short            — service name & POST route (e.g. "ttd")
    * return_result_fn    — the per-DB worker function
    * get_db_fn           — the preload function called on startup
    * display_name        — human-readable name for FastAPI title and
                            log messages (defaults to db_short.upper())
    * extra_startup       — optional async callable invoked after the
                            preload; used by some services for warm-canary tasks.

Everything else (CORS, logging config, /health, /, the POST endpoint with
provenance stamping, the error-shape DatabaseTable response) is identical
across every service and lives in this factory.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from config.provenance import get_db_provenance
from utils.service_setup import add_download_endpoint

ReturnResultFn = Callable[..., Awaitable[DatabaseTable]]
GetDbFn = Callable[[], object]
ExtraStartupFn = Callable[[], Awaitable[None]]

# ── Feedback / few-shot staging ───────────────────────────────────────────────
_FEWSHOT_RL_MAX = int(os.getenv("FEWSHOT_RL_MAX", "20"))


class _FeedbackPayload(BaseModel):
    session_id:      str
    db:              str
    query:           str
    rephrased_query: str = ""
    parsed_value:    dict = {}
    verdict:         str  # "up" | "down"


async def _sweep_old_results(display_name: str, logger: logging.Logger) -> None:
    """Delete result-CSVs older than RESULTS_TTL_DAYS (default 14) from
    RESULTS_ROOT. Runs once on service startup as a fire-and-forget task.

    CSVs are written unconditionally by _orchestrator._csv_path() even when
    no connection_id is supplied (deliberate — agentic-surface LLMs sometimes
    drop the connection_id, and the frontend still needs a stable
    /download path). Without this sweep the directory grows monotonically.

    Idempotent: multiple services may all start at once, multiple `os.unlink`
    calls on the same stale file all succeed-or-ENOENT. Disabled by setting
    RESULTS_TTL_DAYS=0.
    """
    try:
        ttl_days = int(os.environ.get("RESULTS_TTL_DAYS", "14"))
    except ValueError:
        ttl_days = 14
    if ttl_days <= 0:
        return
    root = Path(os.environ.get("RESULTS_ROOT", "/app/results"))
    if not root.exists():
        return
    cutoff = time.time() - (ttl_days * 86400)
    removed = 0
    errors = 0
    # Only sweep CSVs we authored — never recurse into unknown subdirs.
    try:
        for entry in root.iterdir():
            if not entry.is_file() or entry.suffix.lower() != ".csv":
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except FileNotFoundError:
                pass  # Another service got there first; harmless.
            except Exception:
                errors += 1
    except Exception as e:
        logger.warning("[startup-sweep] %s: iter failed: %s", display_name, e)
        return
    if removed or errors:
        logger.info(
            "[startup-sweep] %s: removed %d CSV(s) older than %dd (errors=%d, root=%s)",
            display_name, removed, ttl_days, errors, root,
        )


def build_app(
    *,
    db_short: str,
    return_result_fn: ReturnResultFn,
    get_db_fn: GetDbFn,
    display_name: Optional[str] = None,
    title: Optional[str] = None,
    extra_startup: Optional[ExtraStartupFn] = None,
) -> FastAPI:
    """Construct a fully-wired FastAPI app for one per-DB tool backend.

    Args:
        db_short: Service name (e.g. "ttd"). Used as `SERVICE_NAME`,
                  the POST route (`/ttd`), and the log prefix.
        return_result_fn: Async callable `(input, connection_id) -> DatabaseTable`.
        get_db_fn: Sync callable that preloads the DB on startup.
        display_name: Human-readable name for FastAPI title and log
                      messages. Defaults to `db_short.upper()`.
        title: Override the FastAPI `title=`. Defaults to
               f"BioChirp {display_name} Service".
        extra_startup: Optional async callable scheduled as a fire-and-
                       forget background task at startup. Used by some
                       services for warm-canary tasks.
    """
    SERVICE_NAME = os.getenv("SERVICE_NAME", db_short)
    _DB_VERSION, _DB_SNAPSHOT_DATE = get_db_provenance(SERVICE_NAME)

    display_name = display_name or db_short.upper()
    title = title or f"BioChirp {display_name} Service"

    # Logging is global; every original main.py invoked basicConfig with
    # this exact format, so do the same here.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logger = logging.getLogger("uvicorn.error")

    app = FastAPI(title=title, version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _preload():
        loaded_ok = False
        try:
            get_db_fn()
            loaded_ok = True
            logger.info("[startup] %s database preloaded successfully", display_name)
        except Exception as e:
            logger.error(
                "[startup] Failed to preload %s database: %s",
                display_name, e, exc_info=True,
            )
        # Schema/parquet integrity gate. Only when the DB actually loaded (so a
        # loader failure keeps its existing logged-but-non-fatal behaviour).
        # Default mode is "warn" (log only) so a restart can't fail a service on
        # a pre-existing benign drift; set SCHEMA_VALIDATION=block to raise here
        # (fails startup → container exits → blocked) once the DB is reconciled.
        # The run-on-purpose blocking gate is scripts/preflight_schema_check.py.
        if loaded_ok:
            from ._schema_guard import assert_db_schema
            assert_db_schema(db_short, get_db_fn)
        # Best-effort: sweep orphan CSVs older than RESULTS_TTL_DAYS (default
        # 14d). Fire-and-forget; never blocks startup or fails the service.
        asyncio.create_task(_sweep_old_results(display_name, logger))
        if extra_startup is not None:
            asyncio.create_task(extra_startup())

    @app.on_event("shutdown")
    async def _shutdown():
        # Drain the keep-alive pool on graceful stop so we don't leak sockets.
        from ._httpx_client import aclose_httpx_client
        await aclose_httpx_client()

    add_download_endpoint(app)

    @app.get("/")
    def root():
        return {"message": f"{display_name} service is up"}

    @app.get("/health")
    async def health():
        return {"status": "OK"}

    @app.post(f"/{SERVICE_NAME}", response_model=DatabaseTable)
    async def endpoint(
        payload: QueryInterpreterOutputGuardrail,
        connection_id: str | None = None,
    ):
        request_id = str(uuid.uuid4())
        log_prefix = f"[{SERVICE_NAME} API][{request_id}]"
        logger.info("%s START | connection_id=%s", log_prefix, connection_id)
        try:
            result = await return_result_fn(input=payload, connection_id=connection_id)
            result.db_version = _DB_VERSION
            result.db_snapshot_date = _DB_SNAPSHOT_DATE
            logger.info("%s SUCCESS | rows=%s", log_prefix, result.row_count)
            return result
        except Exception as exc:
            error_msg = f"{display_name} API error: {str(exc)}"
            logger.error("%s EXCEPTION: %s", log_prefix, error_msg, exc_info=True)
            return DatabaseTable(
                database=SERVICE_NAME,
                table=None,
                csv_path=None,
                row_count=None,
                tool=SERVICE_NAME,
                message=error_msg,
                # Stamp provenance on the error path too, so users debugging
                # "which TTD snapshot produced this error" can see the
                # version + snapshot date in the response. The success
                # branch above sets these on the result; without this the
                # error response carries None for both fields.
                db_version=_DB_VERSION,
                db_snapshot_date=_DB_SNAPSHOT_DATE,
            )

    # Expose so tests / introspection can poke at the wired values.
    app.state.service_name = SERVICE_NAME
    app.state.display_name = display_name
    app.state.db_version = _DB_VERSION
    app.state.db_snapshot_date = _DB_SNAPSHOT_DATE

    @app.post("/feedback")
    async def submit_feedback(req: _FeedbackPayload):
        """
        Accept a user thumbs-up/down for a completed query turn.

        Stores to Redis staging queue ``fewshot_staging:{db}`` for nightly
        promotion by scripts/promote_fewshots.py.  Never blocks the caller:
        Redis errors are swallowed and reported as ``status: error``.

        Rate limit: FEWSHOT_RL_MAX (default 20) votes per session per hour.
        Quarantine: first-vote timestamp is stamped with SETNX so the
        promotion script can enforce a 48-h hold before bank insertion.
        """
        if req.verdict not in ("up", "down"):
            return {"status": "invalid_verdict"}
        if not req.query.strip() or not req.db.strip():
            return {"status": "invalid_payload"}

        from ._worker_helpers import get_redis
        r = await get_redis(logger=logger)
        if r is None:
            return {"status": "error", "reason": "redis_unavailable"}

        # ── Rate limiting (per session per hour) ──────────────────────────
        epoch_hour = int(time.time()) // 3600
        rl_key = f"fewshot_rl:{req.session_id}:{epoch_hour}"
        count = await r.incr(rl_key)
        if count == 1:
            await r.expire(rl_key, 3600)
        if count > _FEWSHOT_RL_MAX:
            return {"status": "rate_limited"}

        # ── Push to staging queue ─────────────────────────────────────────
        entry = {
            "session_id":      req.session_id,
            "db":              req.db.lower(),
            "query":           req.query,
            "rephrased_query": req.rephrased_query or req.query,
            "parsed_value":    req.parsed_value,
            "verdict":         req.verdict,
            "ts":              time.time(),
        }
        staging_key = f"fewshot_staging:{req.db.lower()}"
        await r.lpush(staging_key, json.dumps(entry, ensure_ascii=False))

        # ── Stamp first-vote timestamp for quarantine enforcement ─────────
        qhash = hashlib.sha1(
            f"{req.db}|{req.query}".lower().encode()
        ).hexdigest()[:16]
        ts_key = f"fewshot_first_ts:{req.db.lower()}:{qhash}"
        await r.setnx(ts_key, str(entry["ts"]))
        await r.expire(ts_key, 8 * 24 * 3600)

        logger.info(
            "[feedback] queued verdict=%s db=%s session=%s",
            req.verdict, req.db, req.session_id[:8],
        )
        return {"status": "queued"}

    return app
