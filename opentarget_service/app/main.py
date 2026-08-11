"""
opentarget_service — live GraphQL client for the Open Targets Platform.

Architectural note (federation exemption)
-----------------------------------------
Unlike the 25 parquet-backed tools under app/tools/, this service queries Open
Targets over GraphQL at request time. As a result it is intentionally NOT
registered in config/schema.py (no parquet to plan over) and does NOT carry
the shared resources/prompts/ fragment set (interpreter / orchestrator /
summarizer). Its prompts live inline in resources/prompts/opentarget_*.md and
are wired directly to the service's own resolvers/interpreter, not to the
federated prompt builder. It still appears in config/attributions.py because
BioChirp serves Open Targets data and must honour its CC0 attribution.

If/when a parquet-backed loader replaces the GraphQL path, this exemption
should be retired and the service brought under the standard prompt-builder.
"""
from agents import set_default_openai_api, set_default_openai_client
set_default_openai_api("chat_completions")

import os as _os
from openai import AsyncOpenAI as _AsyncOpenAI
from agents import OpenAIChatCompletionsModel as _OpenAIChatCompletionsModel

from config import settings as _settings
_OT_ORCH_MODEL = _os.getenv("OT_ORCHESTRATOR_MODEL", "")
if not _OT_ORCH_MODEL:
    import logging as _logging
    _logging.warning("OT_ORCHESTRATOR_MODEL is not set; orchestrator calls will fail at runtime.")
_GROQ_API_KEY  = _settings.get_groq_key("opentargets")
_groq_client   = _AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=_GROQ_API_KEY,
)
# Override Agents SDK default client so no stray calls hit LiteLLM
set_default_openai_client(_groq_client)

_OT_ORCHESTRATOR = _OpenAIChatCompletionsModel(
    model=_OT_ORCH_MODEL,
    openai_client=_groq_client,
)


def _ot_reasoning_extra(model: str) -> dict:
    """Reasoning-effort extra_body, per the model's accepted values.

    gpt-oss: low|medium|high (env OT_REASONING_EFFORT, default low).
    qwen3:   none|default     (env OT_REASONING_EFFORT, default 'default' = thinking on).
    other:   omit (unknown param would 400).
    """
    m = (model or "").lower()
    eff = _os.environ.get("OT_REASONING_EFFORT", "").strip()
    if "gpt-oss" in m:
        return {"reasoning_effort": eff or "low"}
    if "qwen3" in m or "qwen-3" in m:
        return {"reasoning_effort": eff or "default"}
    return {}



import os
import sys
import re
import io
import csv
import json
import time
import uuid
import asyncio
import hashlib
import logging
from typing import Optional

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from contextlib import suppress
from pathlib import Path
from agents import Agent, Runner, function_tool
from utils.bounded_history import BoundedConversationMemory
from .uvicorn_logger import setup_logger
from .client import close_all_open_targets_clients
from .http_client import close_http_client
import pandas as pd
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from utils.service_setup import add_open_cors
from .resolvers import interpreter, set_query_intent_hints, evict_connection
from .redis import pop_connection_csvs
from agents import Agent, Runner, ModelSettings, ItemHelpers
from .utility_target import target_tool
from .utility_drug import drug_tool
from .utility_disease import disease_tool
from .readme import readme_tool
from .utility_graphql_extras import opentargets_graphql_tool
from .web_search import web_search
from .utility_annotation import (
    target_annotation_tool,
    drug_safety_tool,
    drug_profile_tool,
    disease_profile_tool,
)
from .utility_join import join_results_tool, expand_associations, combine, traverse, evidence_tool, filter_targets_by_annotation
from .utility_analyze import analyze_results, ANALYZE_ENABLED
from agents.exceptions import MaxTurnsExceeded
# from config.guardrail import ShareIn, ShareOut
from agents import Agent, ModelSettings

base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.orchestrator")



MAX_SHARE_HTML_BYTES  = int(os.environ.get("MAX_SHARE_HTML_BYTES", str(5 * 1024 * 1024)))  # 5MB
HEARTBEAT_INTERVAL    = float(os.environ.get("WS_HEARTBEAT_INTERVAL", "15.0"))
MAX_ROW_TO_DISPLAY    = int(os.environ.get("OT_PREVIEW_ROWS", "50"))
OT_MAX_TURNS          = int(os.environ.get("OT_MAX_TURNS", "20"))
OT_WS_CHUNK_SIZE      = int(os.environ.get("OT_WS_CHUNK_SIZE", "32"))
OT_WS_MIN_DELAY       = float(os.environ.get("OT_WS_MIN_DELAY", "0.05"))

# =========================
# Basic helpers
# =========================
# Shared CSV/HTML payload helpers — extracted 2026-05-17 to
# `app/utils/table_formatters.py`. Names preserved verbatim so existing
# call sites need no changes. Note: opentarget_service container needs
# the `./app/utils/:/app/app/utils/:ro` bind-mount in docker-compose.yml.
from app.utils.table_formatters import (  # noqa: E402
    _infer_columns_from_rows,
    _rows_to_csv,
    _build_legacy_table_payload,
)
from app.utils.chat_helpers import (  # noqa: E402
    is_orchestrator_metadata,
    _unescape_repr,
    _extract_display_text,
    publish_table_records_legacy as _shared_publish_table_records_legacy,
    publish_table_from_output as _shared_publish_table_from_output,
)


# =========================
# App & Config
# =========================
app = FastAPI(
    title="OpenTarget Service",
    version="1.0.0",
    description="API for Orchestrator Service",
)

add_open_cors(app, allow_credentials=False)
RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT", "/app/results")).resolve()
REDIS_HOST = os.environ.get("REDIS_HOST", "biochirp_redis_tool")  # match redis.py default so the WS relay and tool publishers can't diverge when REDIS_HOST is unset
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
SAFE_BASE_URL = os.environ.get("SAFE_BASE_URL", "")
SHARE_TTL_SECONDS = int(os.environ.get("SHARE_TTL_SECONDS", "86400"))  # 24h default
POSTRUN_PUBLISH_TABLES = True

with open("/app/resources/prompts/opentarget_orchestrator.md", "r", encoding="utf-8") as f:
    prompt_md = f.read()

class ConnectionIdFilter(logging.Filter):
    """Ensure every log record has a connection_id attribute."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "connection_id"):
            record.connection_id = "-"
        return True


# ---------- Lazy, robust Redis ----------
redis_client: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    """Return a live asyncio Redis client. Recreates on first use / reconnect."""
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )
    try:
        await redis_client.ping()
        return redis_client
    except Exception:
        # Recreate on dropped connection or init failure
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )
        await redis_client.ping()
        return redis_client


@app.on_event("startup")
async def startup():
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        await get_redis()
        logger.info("Redis client initialized", extra={"connection_id": "startup"})
    except Exception as e:
        logger.error("Redis init failed at startup: %s", e, extra={"connection_id": "startup"})


@app.on_event("shutdown")
async def shutdown():
    try:
        r = await get_redis()
        await r.close()
        logger.info("Redis client closed", extra={"connection_id": "shutdown"})
    except Exception:
        pass
    try:
        await close_all_open_targets_clients()
        logger.info("OpenTargets clients closed", extra={"connection_id": "shutdown"})
    except Exception:
        pass
    try:
        await close_http_client()
        logger.info("HTTP client closed", extra={"connection_id": "shutdown"})
    except Exception:
        pass


# =========================
# Routes
# =========================
@app.get("/", response_class=PlainTextResponse)
async def root_ok():
    return "OK"


@app.get("/health")
async def health():
    ok = True
    try:
        r = await get_redis()
        ok = bool(await r.ping())
    except Exception:
        ok = False
    return {"status": "ok" if ok else "degraded", "redis": ok}

# ============================================================================
# Multi-DB v2 integration shim (2026-05-18)
# ============================================================================
# `POST /opentargets` makes OT look like any other BioChirp per-DB tool.
# It accepts the same
# QueryInterpreterOutputGuardrail-shaped payload as the other 25 DBs, runs
# OT's own interpreter on cleaned_query (to do NER + ontology resolution),
# routes to the matching disease/drug/target_tool, and returns a
# DatabaseTable. No WebSocket, no LLM-agent — deterministic dispatch.
# ----------------------------------------------------------------------------
from pydantic import BaseModel as _PydBaseModel
from typing import Any as _Any


class _MultiDbRequest(_PydBaseModel):
    """Subset of QueryInterpreterOutputGuardrail that we actually use.
    Loose schema — extra fields are ignored so the upstream pipeline can
    keep sending its full payload without us caring."""
    cleaned_query: Optional[str] = None
    parsed_value: Optional[dict] = None
    status: Optional[str] = None
    route: Optional[str] = None
    message: Optional[str] = None
    tool: Optional[str] = None
    skip_summary: bool = False

    class Config:
        extra = "ignore"


def _pick_ot_tool(parsed_value: dict, query: str) -> str:
    """Decide which OT sub-tool to route to using parsed_value entity types.
    Falls back to text heuristics on the cleaned_query if parsed_value is
    empty/ambiguous. Returns one of: 'drug', 'disease', 'target'."""
    pv = parsed_value or {}
    has_drug    = bool(pv.get("drug_name") or pv.get("compound_name"))
    has_target  = bool(pv.get("gene_symbol") or pv.get("target_name") or pv.get("gene_name"))
    has_disease = bool(pv.get("disease_name") or pv.get("phenotype_name"))

    # Anchor priority: the entity that is BOTH present in parsed_value AND
    # the most specific. Drug > target > disease matches OT's design where
    # drug_tool returns indications (disease) and target_tool returns
    # disease/drug links.
    if has_drug and not (has_target or has_disease):
        return "drug"
    if has_target and not (has_drug or has_disease):
        return "target"
    if has_disease and not (has_drug or has_target):
        return "disease"
    if has_drug:
        return "drug"
    if has_target:
        return "target"
    if has_disease:
        return "disease"

    # No entities found — peek at the query text for an anchor keyword.
    q = (query or "").lower()
    if any(w in q for w in ("drug", "inhibitor", "antagonist", "agonist", "treatment", "approved")):
        return "drug"
    if any(w in q for w in ("gene", "protein", "target", "kinase")):
        return "target"
    return "disease"


async def _invoke_function_tool(ft, args_dict: dict):
    """Invoke an @function_tool-decorated FunctionTool by constructing the
    minimal ToolContext it expects. Used by the multi-DB v2 shim because
    we're outside an Agent's Runner.run loop."""
    from agents.tool_context import ToolContext
    import uuid as _uuid
    ctx = ToolContext(
        context=None,
        tool_name=ft.name,
        tool_call_id=f"shim-{_uuid.uuid4().hex[:8]}",
        tool_arguments=json.dumps(args_dict),
    )
    out = await ft.on_invoke_tool(ctx, json.dumps(args_dict))
    # on_invoke_tool may return a model object, a JSON string, or a dict —
    # normalise to dict.
    if isinstance(out, str):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"message": out}
    if hasattr(out, "model_dump"):
        return out.model_dump()
    return out


@app.post("/opentargets")
async def opentargets_db_shim(req: _MultiDbRequest, connection_id: Optional[str] = Query(None)):
    """HTTP entry point for the per-DB tool pipeline.
    Returns a DatabaseTable-shaped JSON.

    Pipeline:
      1. Run OT's interpreter on cleaned_query (NER + entity resolution).
      2. Route to disease/drug/target_tool based on parsed_value entity types.
      3. Wrap the TableOutput as a DatabaseTable so the upstream pipeline
         can consume it identically to the other 25 DBs.
    """
    pid = os.getpid()
    cid = connection_id or f"http-ot-{pid}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    logger.info(
        "[/opentargets shim] cleaned_query=%r connection_id=%s",
        (req.cleaned_query or "")[:200], cid,
        extra={"connection_id": cid},
    )

    # Seed the requested-output intent hints (same guard the WS path uses at the
    # set_query_intent_hints call below) so the interpreter can recover the
    # requested output type when NER strips intent words ("which genes", "indications").
    # Without this the federated route can return the wrong column set.
    set_query_intent_hints(cid, req.cleaned_query or "")

    try:
        # 1) NER + resolution
        try:
            resolution = await _invoke_function_tool(
                interpreter,
                {"user_query": req.cleaned_query or "", "connection_id": cid},
            )
        except Exception as e:
            logger.exception("[/opentargets shim] interpreter failed: %s", e)
            return {"database": "OpenTargets", "table": [], "row_count": 0,
                    "tool": "opentargets",
                    "message": f"Interpreter failed: {type(e).__name__}: {e}",
                    "csv_path": None}

        # 2) Pick the OT anchor tool. Prefer OT's own look_up_category, fall
        # back to the upstream parsed_value entity inspection.
        look_up = (resolution or {}).get("look_up_category")
        if look_up not in ("drug", "disease", "target"):
            look_up = _pick_ot_tool(req.parsed_value or {}, req.cleaned_query or "")

        tool_map = {"disease": disease_tool, "drug": drug_tool, "target": target_tool}
        ot_tool = tool_map.get(look_up)
        if ot_tool is None:
            return {"database": "OpenTargets", "table": [], "row_count": 0,
                    "tool": "opentargets",
                    "message": f"No suitable OpenTargets sub-tool for category {look_up!r}",
                    "csv_path": None}

        # 3) Dispatch
        try:
            tableout = await _invoke_function_tool(
                ot_tool,
                {"input": resolution, "connection_id": cid},
            )
        except Exception as e:
            logger.exception("[/opentargets shim] %s_tool failed: %s", look_up, e)
            return {"database": "OpenTargets", "table": [], "row_count": 0,
                    "tool": "opentargets",
                    "message": f"{look_up}_tool failed: {type(e).__name__}: {e}",
                    "csv_path": None}

        # TableOutput.table is a hierarchical LLM-safe dict from
        # df_to_llm_safe_hierarchy() — not a row list. But the full rectangular
        # data was written to TableOutput.csv_path. For the multi-DB pipeline
        # preview, read the first N rows from the CSV. This avoids fragile
        # hierarchy-flattening and gives downstream identical row shapes to
        # the other 25 DBs.
        preview_rows: list = []
        csv_path = (tableout or {}).get("csv_path")
        if csv_path:
            try:
                df = pd.read_csv(csv_path, dtype=str).head(50)
                df = df.where(pd.notna(df), None)

                # ── OT sort_order (mirrors the CTD sort_order mechanism) ────────
                # Drug results: put APPROVAL rows first so the synthesizer always
                # sees the approved indication in row 1 before trial-phase rows.
                # This prevents the LLM from focusing on "PHASE_3 TERMINATED"
                # while ignoring an "APPROVAL" row that is present but lower in
                # the default GraphQL ordering.
                #
                # Target/disease results: overall_association_score is already
                # sorted DESC by GraphQL, so no re-sort needed there.
                if "phase" in df.columns:
                    # Handles OT v26 string enums ("APPROVAL", "PHASE_3", "PHASE_2_3",
                    # "PHASE_1_2", "EARLY_PHASE_1") and old-API integer strings ("4"=approved).
                    def _ot_phase_rank(v) -> int:
                        s = "" if v is None else str(v).strip().upper()
                        if not s or s in ("NAN", "NONE", "NULL"):
                            return 99
                        if any(kw in s for kw in ("APPROV", "MARKET")):
                            return 0
                        if "EARLY" in s:
                            return 90
                        import re as _re
                        digits = [int(d) for d in _re.findall(r"\d+", s)]
                        if digits:
                            m = max(digits)
                            return 0 if m >= 4 else (10 - m)  # 4→0(≈APPROVAL), 3→7, 2→8, 1→9
                        return 99

                    df = df.assign(
                        __phase_rank=df["phase"].map(_ot_phase_rank)
                    ).sort_values("__phase_rank", kind="stable").drop(
                        columns=["__phase_rank"]
                    )

                preview_rows = df.to_dict(orient="records")
            except Exception as e:
                logger.warning("[/opentargets shim] failed to read CSV preview: %s", e)

        row_count = (tableout or {}).get("row_count")
        if row_count is None:
            row_count = len(preview_rows)

        return {
            "database": "OpenTargets",
            "table": preview_rows[:50],
            "row_count": row_count,
            "tool": "opentargets",
            "message": (tableout or {}).get("message") or "",
            "csv_path": (tableout or {}).get("csv_path"),
            "db_version": "Open Targets Platform GraphQL v4 (live)",
            "db_snapshot_date": time.strftime("%Y-%m-%d"),
        }
    finally:
        # Evict this connection's per-request resolver state (intent hints +
        # requested_output) so it can't leak into a later request that reuses
        # the same connection_id — the WS handler does this in its finally too.
        evict_connection(cid)


@app.get("/download")
def download(path: str):
    file_path = (RESULTS_ROOT / path).resolve()

    # Safety check (important)
    if not str(file_path).startswith(str(RESULTS_ROOT)):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type="text/csv",
    )


# =========================
# Orchestrator metadata filter
# =========================
# Helpers below were extracted to app/utils/chat_helpers.py on 2026-05-17.
# Imports at the top of this file pull in:
#   is_orchestrator_metadata, _unescape_repr, _extract_display_text,
# plus shared coroutine bodies for the two table publishers. The thin
# wrappers here resolve the per-process redis client and forward.
SUPPORTED_TABLE_TOOLS = {"disease_tool", "target_tool", "drug_tool"}


async def publish_table_records_legacy(
    connection_id: str,
    rows,
    *,
    columns=None,
    event_type: str = "disease_tool_table",
    csv_name: str = "results.csv",
    csv_path: str | None = None,
    limit_rows: int = 1000,
    row_count: int | None = None,
):
    r = await get_redis()
    return await _shared_publish_table_records_legacy(
        r,
        connection_id=connection_id,
        rows=rows,
        columns=columns,
        event_type=event_type,
        csv_name=csv_name,
        csv_path=csv_path,
        limit_rows=limit_rows,
        row_count=row_count,
    )


async def publish_table_from_output(
    *,
    output: dict,
    tool_key: str,
    connection_id: str,
    limit_rows: int = 50,
):
    r = await get_redis()
    return await _shared_publish_table_from_output(
        r,
        output=output,
        tool_key=tool_key,
        connection_id=connection_id,
        limit_rows=limit_rows,
    )


# =========================
# WebSocket Orchestrator
# =========================
@app.websocket("/opentarget")
async def opentarget_ws(websocket: WebSocket):
    pid = os.getpid()
    connection_id = f"ws-{pid}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    logger.warning(f"[Initialized connection]:{connection_id}")


    try:
        ua = websocket.headers.get("user-agent")
        logger.info(
            "WS handshake path=%s conn_id=%s from %s:%s ua=%s",
            websocket.url.path,
            connection_id,
            getattr(websocket.client, "host", "?"),
            getattr(websocket.client, "port", "?"),
            ua,
            extra={"connection_id": connection_id},
        )
    except Exception:
        pass

    await websocket.accept()


    logger.warning(f"[Initialized connection 1]:{connection_id}")

    # Single writer guard for WebSocket
    send_lock = asyncio.Lock()
    _ws_closed = False

    async def ws_send(payload):
        """Send JSON or raw string with a single writer lock. Silently drops if WS closed."""
        nonlocal _ws_closed
        if _ws_closed:
            return
        text = payload if isinstance(payload, str) else json.dumps(payload)
        try:
            async with send_lock:
                await websocket.send_text(text)
        except (RuntimeError, WebSocketDisconnect, Exception) as _e:
            _ws_closed = True
            logger.debug("[ws:%s] ws_send swallowed (socket closed): %s", pid, _e,
                         extra={"connection_id": connection_id})

    # Initial ack to client
    await ws_send({"type": "user_ack", "session_id": connection_id})

    async def stream_message_deltas(
        new_text: str,
        tool_id: str,
        tool_name: str,
        chunk_size: int = 32,
        min_delay: float = 0.05,
    ):
        if not new_text:
            return
        offset = 0
        seq = 0
        for i in range(0, len(new_text), chunk_size):
            delta = new_text[i : i + chunk_size]
            seq += 1
            await ws_send(
                {
                    "type": "delta",
                    "tool_id": tool_id,
                    "name": tool_name,
                    "seq": seq,
                    "offset": offset,
                    "text": delta,
                    "final": False,
                }
            )
            offset += len(delta)
            if min_delay > 0:
                await asyncio.sleep(min_delay)

    async def send_heartbeat():
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws_send({"type": "heartbeat", "ts": time.time()})
            except Exception:
                break

    # Pub/Sub relay uses a live client
    logger.warning(f"[Initialized connection 2]:{connection_id}")
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(connection_id)

    async def relay_pubsub_events():
        try:
            async for msg in pubsub.listen():
                if _ws_closed:
                    break
                if msg["type"] != "message":
                    continue
                raw_payload = msg["data"]
                try:
                    parsed = json.loads(raw_payload)
                    if parsed.get("type") in {"disease_tool_table", "target_tool_table", "drug_tool_table"}:
                        logger.info(
                            "Forwarding DB event: %s (conn=%s)",
                            parsed["type"],
                            connection_id,
                            extra={"connection_id": connection_id},
                        )
                except Exception:
                    pass
                await ws_send(raw_payload)
        except Exception as e:
            logger.error("PubSub error: %s", e, extra={"connection_id": connection_id})
        finally:
            with suppress(Exception):
                await pubsub.unsubscribe(connection_id)
                await pubsub.close()

    heartbeat_task = asyncio.create_task(send_heartbeat())
    relay_task = asyncio.create_task(relay_pubsub_events())

    # Bounded history: deque-backed, holds last 5 Q/A pairs (10 messages).
    # Caps RAM growth per WS session — the prior unbounded list leaked
    # linearly with turn count.
    memory = BoundedConversationMemory(maxlen=10)
    published_table_tools = set()

    # One Agent per WebSocket connection
    orchestrator = Agent(
        name="Orchestrator",
        instructions=prompt_md,
        tools=[
                interpreter,
                disease_tool,
                drug_tool,
                target_tool,
                readme_tool,
                opentargets_graphql_tool,
                web_search,
                target_annotation_tool,
                drug_safety_tool,
                drug_profile_tool,
                disease_profile_tool,
                combine,
                traverse,
                evidence_tool,
                join_results_tool,
                expand_associations,
                filter_targets_by_annotation,
                # Flag-gated analytical SQL over prior result CSVs. Registered ONLY
                # when TEXT2SQL[_OPENTARGETS] is set, so the default build is inert.
                *([analyze_results] if ANALYZE_ENABLED else []),
                ],

        # tools=[memory_tool, web, readme, interpreter, tavily, ttd, ctd, hcdt, router_tool],
        model=_OT_ORCHESTRATOR,
        model_settings=ModelSettings(
            temperature=0,
            # Reasoning effort is model-specific: gpt-oss accepts low|medium|high
            # (tunable via OT_REASONING_EFFORT); qwen3 only accepts none|default
            # (default = thinking on); other models get no reasoning_effort param.
            extra_body=_ot_reasoning_extra(_OT_ORCH_MODEL),
            parallel_tool_calls=True,
        ),
        # max_turns=20
    #     model_settings=ModelSettings(
    #     max_turns=10
    # ),

    )

    try:
        while True:
            logger.warning(f"[Initialized connection 3]:{connection_id}")
            # Receive a message from client
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info(
                    "[ws:%s] client disconnected via receive_text",
                    pid,
                    extra={"connection_id": connection_id},
                )
                break

            # Handle ping / normal user_input
            try:
                payload = json.loads(raw)
                if payload.get("type") == "ping":
                    await ws_send({"type": "pong", "ts": time.time()})
                    continue
                user_input = payload.get("user_input", raw)
            except json.JSONDecodeError:
                user_input = raw

            logger.info(
                "[ws:%s] <<< %s",
                pid,
                user_input[:200],
                extra={"connection_id": connection_id},
            )

            # Last 5 Q/A pairs from memory. Previous logic iterated from
            # index 0 and so kept returning the *first* 5 pairs forever
            # after turn 5 — read from the tail instead.
            recent = memory.chat_memory.messages[-10:]
            last5_pairs = [
                {"question": recent[i].content, "answer": recent[i + 1].content}
                for i in range(0, len(recent) - 1, 2)
            ]
            await ws_send({"last_5_pairs": last5_pairs})

            # Build input for orchestrator
            # (kept as string to match existing Runner.run_streamed behavior)
            input_data = (
                f"user_input: {user_input} | "
                # f"last_5_pairs: {last5_pairs} | "
                f"connection_id: {connection_id}"
            )




            logger.warning(f"[input_data]:{input_data}")
            # Pre-process user_input with keyword rules before orchestrator runs
            # so interpreter can apply deterministic type hints even when the
            # orchestrator model strips "which genes" / "which pathways" context.
            set_query_intent_hints(connection_id, user_input)

# json.dumps(input_payload)
            # stream = Runner.run_streamed(orchestrator, input=input_data)
            input_data = {
                "user_input": user_input,
                "last_5_pairs": last5_pairs,
                "connection_id": connection_id,
            }
            stream = Runner.run_streamed(orchestrator, input=json.dumps(input_data), max_turns = OT_MAX_TURNS)
            # stream = await Runner.run(orchestrator, input=json.dumps(input_data))


            memory.chat_memory.add_user_message(user_input)

            tool_registry = {}
            tool_counter = 0
            active_tools = set()
            orchestrator_text_emitted = False
            fallback_orchestrator_text: Optional[str] = None
            last_tool_text: Optional[str] = None

            def new_tool_id(runner_item_id: str, tool_name: str) -> str:
                nonlocal tool_counter
                tool_counter += 1
                return f"tool-{tool_counter}_{runner_item_id}_{tool_name.replace(' ', '_')}"

            _orch_attempts = 0
            _orch_max_retries = 1  # one retry for transient LLM JSON-parse errors
            while True:
              try:
                async for event in stream.stream_events():
                    etype = getattr(event, "type", None)

                    # Skip raw delta events from model, we only care about tool + final messages
                    if (
                        etype == "raw_response_event"
                        and hasattr(event, "data")
                        and hasattr(event.data, "delta")
                    ):
                        continue

                    if etype == "run_error":
                        if "max_turns" in str(event.error).lower():
                            raise MaxTurnsExceeded(str(event.error))

                    if etype == "run_item_stream_event" and hasattr(event, "item"):
                        item = event.item
                        item_type = getattr(item, "type", None)

                        # Tool call started
                        if item_type == "tool_call_item":
                            runner_item_id = str(getattr(item, "id", uuid.uuid4().hex))
                            raw_item = getattr(item, "raw_item", {})
                            tool_name = (
                                raw_item.get("name", "Unknown Tool")
                                if isinstance(raw_item, dict)
                                else getattr(raw_item, "name", "Unknown Tool")
                            )
                            tool_id = new_tool_id(runner_item_id, tool_name)
                            tool_registry[runner_item_id] = {
                                "tool_id": tool_id,
                                "name": tool_name,
                                "status": "running",
                                "sent_any": False,
                                "output_buffer": "",
                            }
                            active_tools.add(tool_id)
                            await ws_send(
                                {
                                    "type": "tool_called",
                                    "tool_id": tool_id,
                                    "name": tool_name,
                                }
                            )
                            continue

                        # Tool output chunk
                        elif item_type == "tool_call_output_item":
                            runner_item_id = str(getattr(item, "id", ""))
                            tool_info = tool_registry.get(runner_item_id)

                            # Fallback: if we didn't find by ID, attach to any active tool
                            if not tool_info and active_tools:
                                for rid, info in tool_registry.items():
                                    if info["tool_id"] in active_tools:
                                        tool_info = info
                                        runner_item_id = rid
                                        break
                            if not tool_info:
                                continue

                            output = getattr(item, "output", None)

                            # Drop pure metadata
                            if isinstance(output, str) and is_orchestrator_metadata(
                                output, tool_info["name"]
                            ):
                                if getattr(item, "is_final", True):
                                    tool_info["status"] = "completed"
                                    active_tools.discard(tool_info["tool_id"])
                                    await ws_send(
                                        {
                                            "type": "tool_result",
                                            "tool_id": tool_info["tool_id"],
                                            "ok": False,
                                        }
                                    )
                                continue

                            # JSON-string outputs → parse
                            if isinstance(output, str) and output.strip().startswith("{"):
                                try:
                                    output = json.loads(output)
                                except json.JSONDecodeError:
                                    pass

                            msg = _extract_display_text(output, tool_info["name"])
                            if msg:
                                last_tool_text = msg.strip()
                                # web_search's raw result is relayed by the
                                # orchestrator's final message (carrying its _Source
                                # provenance line); streaming it here too would show
                                # the answer and provenance twice. Keep last_tool_text
                                # for the no-message fallback, but skip the raw stream.
                                if (tool_info["name"] or "").strip().lower() != "web_search":
                                    tool_info["output_buffer"] += msg
                                    tool_info["sent_any"] = True
                                    await stream_message_deltas(
                                        new_text=msg,
                                        tool_id=tool_info["tool_id"],
                                        tool_name=tool_info["name"],
                                        chunk_size=OT_WS_CHUNK_SIZE,
                                        min_delay=OT_WS_MIN_DELAY,
                                    )

                            # Publish table events if applicable
                            try:
                                tool_key = (tool_info["name"] or "").strip().lower()
                                if tool_key in SUPPORTED_TABLE_TOOLS and isinstance(output, dict):
                                    await publish_table_from_output(
                                        output=output,
                                        tool_key=tool_key,
                                        connection_id=connection_id,
                                        limit_rows=MAX_ROW_TO_DISPLAY,
                                    )
                                    published_table_tools.add(tool_key)
                            except Exception as _pub_ex:
                                logger.exception(
                                    "Failed to publish %s table event: %s",
                                    tool_info.get("name"),
                                    _pub_ex,
                                    extra={"connection_id": connection_id},
                                )

                            # Finalization for this tool
                            if getattr(item, "is_final", True):
                                tool_info["status"] = "completed"
                                active_tools.discard(tool_info["tool_id"])
                                await ws_send(
                                    {
                                        "type": "tool_result",
                                        "tool_id": tool_info["tool_id"],
                                        "ok": True,
                                    }
                                )
                            continue

                        # Final orchestrator message
                        elif item_type == "message_output_item":
                            text = ItemHelpers.text_message_output(item)
                            if text and text.strip() and not is_orchestrator_metadata(text):
                                orchestrator_text_emitted = True
                                memory.chat_memory.add_ai_message(text)
                                logger.info(
                                "[ws:%s] Final orchestrator message: %r",
                                pid,
                                text,
                                extra={"connection_id": connection_id},
                            )

                                await stream_message_deltas(
                                    new_text=text,
                                    tool_id="orchestrator",
                                    tool_name="orchestrator",
                                    chunk_size=OT_WS_CHUNK_SIZE,
                                    min_delay=OT_WS_MIN_DELAY,
                                )
                            continue

                # After the run: check for CSVs for any DB we didn't publish yet
                if POSTRUN_PUBLISH_TABLES:
                    try:
                        for which in ("drug_tool", "disease_tool", "target_tool"):
                            if which in published_table_tools:
                                continue
                            tmp_csv = RESULTS_ROOT / f"{which}_{connection_id}.csv"
                            if not tmp_csv.exists():
                                continue

                            final_name = f"{which}_results_{int(time.time())}.csv"
                            final_path = (RESULTS_ROOT / final_name).resolve()
                            df = pd.read_csv(tmp_csv, dtype=str)
                            df.to_csv(final_path, index=False)



                            await publish_table_records_legacy(
                                connection_id=connection_id,
                                rows=df.head(MAX_ROW_TO_DISPLAY).to_dict(orient="records"),
                                columns=df.columns.tolist(),
                                event_type=f"{which}_table",
                                csv_name=final_name,
                                csv_path=str(final_path),
                                limit_rows=MAX_ROW_TO_DISPLAY,
                                row_count=int(getattr(df, "shape", (0, 0))[0]),
                            )
                    except Exception as ex:
                        logger.exception(
                            "Failed to publish csv_path events: %s",
                            ex,
                            extra={"connection_id": connection_id},
                        )

                # Fallback summary: emit one final orchestrator message even if
                # the model ended without a message_output_item.
                if not orchestrator_text_emitted:
                    stream_final_output = getattr(stream, "final_output", None)
                    if stream_final_output is not None:
                        fallback_orchestrator_text = _extract_display_text(
                            stream_final_output, "orchestrator"
                        )
                        if (
                            not fallback_orchestrator_text
                            and isinstance(stream_final_output, str)
                            and stream_final_output.strip()
                            and not is_orchestrator_metadata(stream_final_output)
                        ):
                            fallback_orchestrator_text = stream_final_output.strip()

                    if not fallback_orchestrator_text and last_tool_text:
                        fallback_orchestrator_text = last_tool_text

                    if fallback_orchestrator_text:
                        memory.chat_memory.add_ai_message(fallback_orchestrator_text)
                        logger.info(
                            "[ws:%s] Emitting fallback orchestrator summary.",
                            pid,
                            extra={"connection_id": connection_id},
                        )
                        await stream_message_deltas(
                            new_text=fallback_orchestrator_text,
                            tool_id="orchestrator",
                            tool_name="orchestrator",
                            chunk_size=OT_WS_CHUNK_SIZE,
                            min_delay=OT_WS_MIN_DELAY,
                        )
                        orchestrator_text_emitted = True

                # Ensure all active tools get a final tool_result
                for tool_id in list(active_tools):
                    await ws_send(
                        {
                            "type": "tool_result",
                            "tool_id": tool_id,
                            "ok": True,
                        }
                    )

                # Give relay_pubsub_events time to forward any Redis messages
                # that were published just before this point (avoids the race
                # where `final` reaches the client before a `*_table` event).
                await asyncio.sleep(0.25)

                final_payload = {"type": "final"}
                if fallback_orchestrator_text:
                    final_payload["text"] = fallback_orchestrator_text
                # Belt-and-suspenders: include csv_paths registered via _publish_ws
                # so the client can recover if it missed the Redis-relayed *_table event.
                registered_csvs = pop_connection_csvs(connection_id)
                if registered_csvs:
                    final_payload["csv_paths"] = registered_csvs
                await ws_send(final_payload)
                logger.info(
                        "[ws:%s] Final message sent to client.",
                        pid,
                        extra={"connection_id": connection_id},
                    )

                break  # success — exit retry loop
              except WebSocketDisconnect:
                logger.info(
                    "[ws:%s] client disconnected mid-run",
                    pid,
                    extra={"connection_id": connection_id},
                )
                return
              except Exception as e:
                _is_json_parse_err = "parse tool call" in str(e).lower() or "parse_tool_call" in str(e).lower()
                if _is_json_parse_err and _orch_attempts < _orch_max_retries:
                    _orch_attempts += 1
                    logger.warning(
                        "[ws:%s] Orchestrator JSON parse error (attempt %d/%d); retrying",
                        pid, _orch_attempts, _orch_max_retries + 1,
                        extra={"connection_id": connection_id},
                    )
                    # Re-init stream state and retry
                    tool_registry = {}
                    tool_counter = 0
                    active_tools = set()
                    orchestrator_text_emitted = False
                    fallback_orchestrator_text = None
                    last_tool_text = None
                    stream = Runner.run_streamed(orchestrator, input=json.dumps(input_data), max_turns=20)
                    continue
                logger.exception(
                    "[ws:%s] run error: %s",
                    pid,
                    e,
                    extra={"connection_id": connection_id},
                )
                await ws_send({"type": "error", "message": str(e)})
                break

    except WebSocketDisconnect:
        logger.info("[ws] client disconnected", extra={"connection_id": connection_id})
    except Exception as e:
        logger.exception(
            "[ws] unhandled error: %s", e, extra={"connection_id": connection_id}
        )
    finally:
        # Stop background tasks
        with suppress(Exception):
            relay_task.cancel()
        with suppress(Exception):
            heartbeat_task.cancel()
        # Close pubsub
        with suppress(Exception):
            await pubsub.unsubscribe(connection_id)
            await pubsub.close()
        # Evict per-connection intent / requested-output caches in resolvers
        with suppress(Exception):
            evict_connection(connection_id)
        # Clean up any leftover CSV registry entries for this connection
        with suppress(Exception):
            pop_connection_csvs(connection_id)


# Accept *both* /chat and /chat/ to avoid trailing-slash 403s on WS handshakes.
app.add_api_websocket_route("/opentarget/", opentarget_ws)
