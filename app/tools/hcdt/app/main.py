"""HCDT service — thin proxy entry + execute tool server.

POST /hcdt        → thin proxy to biochirp_orchestrator_tool (all logic runs there)
POST /execute     → join+finalize on a pre-computed plan (called by orchestrator)
WebSocket /hcdt_chat/ → unchanged, uses return_hcdt_result directly
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI

from app.per_db_tool import (
    ChatSpec, build_chat_router,
    register_execute_endpoint,
)
from app.hcdt import (
    return_hcdt_result, get_hcdt_db, _HCDT_CAPABILITIES, _HCDT_LIMITATIONS,
    SUMMARIZER_MODEL_NAME, prompt_md, _hcdt_negative_binding_note,
)
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from utils.service_setup import add_open_cors, add_health_endpoint, add_download_endpoint

logger = logging.getLogger("uvicorn.error")

ORCHESTRATOR_HOST = os.getenv("ORCHESTRATOR_HOST", "biochirp_orchestrator_tool")
ORCHESTRATOR_PORT = os.getenv("ORCHESTRATOR_PORT", "8021")
ORCHESTRATOR_URL = f"http://{ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}"

app = FastAPI(
    title="HCDT Service",
    version="2.0.0",
    description="HCDT data-tool: thin proxy entry + execute tool server",
)
add_open_cors(app)
add_health_endpoint(app)
add_download_endpoint(app)


@app.on_event("startup")
async def _schema_gate():
    """Schema/parquet integrity gate (HCDT builds its FastAPI app directly, so it
    doesn't get build_app's gate). Default mode "warn" logs mismatches;
    SCHEMA_VALIDATION=block raises here → startup fails → container exits →
    blocked. See app/per_db_tool/_schema_guard.py."""
    from app.per_db_tool._schema_guard import assert_db_schema
    assert_db_schema("hcdt", get_hcdt_db)


@app.on_event("startup")
async def _warmup():
    """Fire-and-forget pipeline warm-up: pre-builds schema planner index,
    opens TLS connections to Groq/OpenRouter, and caches the synthesizer
    prompt so the first real user query hits warm caches."""
    import asyncio
    from app.per_db_tool.schema_kg_chat import warm_pipeline
    asyncio.create_task(warm_pipeline("hcdt"))


@app.get("/")
def root():
    return {"message": "HCDT service is up"}


# ─── 1. Thin entry proxy ──────────────────────────────────────────────────────
# All query logic (routing, schema mapping, planning, join, finalize) lives in
# biochirp_orchestrator_tool. This endpoint is the HTTP face for the chat
# frontend and bio_chat — it forwards the natural-language query and returns
# whatever the orchestrator produces (a DatabaseTable for query_db, or a
# message-only DatabaseTable for direct/web answers).

@app.post("/hcdt", response_model=DatabaseTable)
async def hcdt_endpoint(
    payload: QueryInterpreterOutputGuardrail,
    connection_id: Optional[str] = None,
):
    query = (payload.cleaned_query or "").strip()
    if not query:
        pv = payload.parsed_value.model_dump(exclude_none=True) if payload.parsed_value else {}
        parts = []
        for v in pv.values():
            if isinstance(v, list):
                parts.extend(str(x) for x in v if x)
            elif v:
                parts.append(str(v))
        query = " ".join(parts) or "unknown query"

    from app.per_db_tool.db_llm_rules import load_db_llm_rules
    _db_llm_rules = load_db_llm_rules("hcdt")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/orchestrate?database=hcdt",
                json={
                    "query": query,
                    "display_name": "HCDT",
                    "capabilities": _HCDT_CAPABILITIES,
                    "limitations": _HCDT_LIMITATIONS,
                    "connection_id": connection_id or "",
                    "db_llm_rules": _db_llm_rules,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(f"[hcdt proxy] orchestrator call failed: {exc}")
        return DatabaseTable(database="hcdt", tool="hcdt",
                             message=f"Orchestrator error: {exc}")

    # direct_answer / web_search → wrap answer text in a DatabaseTable message
    if data.get("action") in ("direct_answer", "web_search"):
        return DatabaseTable(
            database="hcdt", tool="hcdt",
            message=data.get("answer", ""),
        )

    # query_db path → orchestrator returns a full DatabaseTable-compatible dict
    return data


# ─── 2. Execute tool server ───────────────────────────────────────────────────
# Called by biochirp_orchestrator_tool after route + schema_mapper + schema_planner.
# Receives the pre-computed production_plan, runs HCDT's pre_join →
# join_and_filter → on_empty_fallback → finalize. Never does expand or planning.
register_execute_endpoint(
    app,
    db="hcdt",
    display_name="HCDT",
    get_db=get_hcdt_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    # /execute skips expand+planning (receives an already-computed plan), so
    # only join-phase hooks are meaningful here — post_join fires after
    # join_and_filter_database, same as on the return_hcdt_result fallback path.
    post_join=_hcdt_negative_binding_note,
)


# ─── 3. WebSocket chat ───────────────────────────────────────────────────────
# orchestrator_url routes the chat through the full schema_kg pipeline so each
# step (schema_mapper / planner / expander / execute) appears as a progress
# card in the frontend before synthesis. return_result_fn is kept as the
# old-path fallback for non-orchestrator DBs and direct unit tests.
app.include_router(build_chat_router(ChatSpec(
    db="hcdt",
    display_name="HCDT",
    long_name="High-Confidence Drug-Target",
    return_result_fn=return_hcdt_result,
    orchestrator_url=f"{ORCHESTRATOR_URL}/orchestrate?database=hcdt",
    capabilities=_HCDT_CAPABILITIES,
    limitations=_HCDT_LIMITATIONS,
)))
