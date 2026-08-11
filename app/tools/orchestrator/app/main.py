"""BioChirp orchestrator service — single coordinator over the tool fleet.

Mirrors the reference repo's `orchestrator_service/app/`: the orchestrator holds
NO heavy logic. It owns a set of thin `*_tool.py` clients (generic_tool base +
router_tool + per-backend wrappers) and composes them over HTTP, emitting a
tool_called / tool_result event stream.

  POST /orchestrate?database=<db>
    body: { "query": "...", "display_name"?, "capabilities"?, "limitations"? }
   ->  { action, plan?, production_plan?, parsed_value?, answer?, events:[...] }

Flow (STRANGLER stage — see schema_mapper_tool.py):
  router_tool.route(query)
    ├─ direct_answer → return the answer
    ├─ web_search    → web_tool.search (Groq direct) → return the answer
    └─ query_db
         schema_mapper_tool.map_query   (expand→ANN→filter→value-map; coarse, temporary)
           → derive (kept, parsed_value)
         planner_tool.plan              (deterministic prune+Steiner — the new tool)
           → production_plan
         return the plan

As expander_tool / retrieval_tool / filter_tool / value_mapper_tool are
extracted, the single schema_mapper_tool call is replaced by that chain — the
router/planner wrappers and this flow stay unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

import redis.asyncio as _aioredis
from fastapi import FastAPI
from pydantic import BaseModel

from utils.service_setup import add_open_cors, add_health_endpoint

from .execute_tool import ExecuteTool
from .expand_and_match_tool import ExpandAndMatchTool
from .planner_tool import PlannerTool
from .router_tool import RouterTool
from .schema_mapper_tool import SchemaMapperTool
from . import web_tool

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

DEFAULT_DB = os.getenv("ORCHESTRATOR_DEFAULT_DB", "hcdt")


def _label_web_answer(res: dict, display_name: str) -> str:
    """Prepend an honest provenance note to a web-tool answer.

    The web prompt frames every answer as coming from web evidence, but under
    ``tool_choice="auto"`` the model may answer from its own training data
    without searching. When that happens (``searched`` is False) we prepend a
    correction so a parametric answer is never mislabelled as a live web search.
    When a search actually ran, the prompt's own provenance disclaimer is
    already accurate, so we leave the answer unchanged.
    """
    answer = res.get("answer") or ""
    if not answer or res.get("searched"):
        return answer
    note = (
        f"> *⚠️ {display_name} structured retrieval returned nothing, and no live "
        f"web search was performed — the answer below is AI-generated from the "
        f"model's training data. Verify every claim against authoritative primary "
        f"sources.*\n\n"
    )
    return note + answer


def _label_schema_mismatch_answer(res: dict, display_name: str) -> str:
    """Prepend a disclaimer for the schema_mapper "matched=False" fallback path.

    This is a stronger disclaimer than ``_label_web_answer``: the query didn't
    match any concept in ``display_name``'s schema at all, so the DB was never
    actually queried — distinct from a query that ran and returned zero rows.
    Without this, the per-DB proxy endpoints (e.g. hcdt/app/main.py) forward
    ``answer`` straight into a DatabaseTable message with no indication it
    isn't real DB data, violating the "report gaps literally, don't
    substitute" contract.
    """
    answer = res.get("answer") or ""
    if not answer:
        return answer
    if res.get("searched"):
        note = (
            f"> *⚠️ This query has no matching concept in {display_name}'s schema — "
            f"it was not answered from {display_name} data. The answer below comes "
            f"from a live web search instead.*\n\n"
        )
    else:
        note = (
            f"> *⚠️ This query has no matching concept in {display_name}'s schema, "
            f"and no live web search was performed — the answer below is AI-generated "
            f"from the model's training data. Verify every claim against authoritative "
            f"primary sources.*\n\n"
        )
    return note + answer

# Live-progress pub/sub: each pipeline step-event is published to a Redis channel
# keyed by connection_id so the chat WebSocket can relay it to the frontend in
# REAL TIME, instead of replaying the whole batch after the pipeline finishes.
_PROGRESS_REDIS = None


def _get_progress_redis():
    global _PROGRESS_REDIS
    if _PROGRESS_REDIS is None:
        _PROGRESS_REDIS = _aioredis.Redis(
            host=os.getenv("REDIS_HOST", "biochirp_redis_tool"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
        )
    return _PROGRESS_REDIS


class OrchestrateRequest(BaseModel):
    query: str
    display_name: str = "BioChirp"
    capabilities: str = ""
    limitations: str = ""
    connection_id: str = ""
    # Per-DB, per-layer LLM rules from resources/prompts/db_llm_rules.yaml.
    # Keys: router, rewriter, col_selection, mapper, tiebreaker.
    # Each key is forwarded ONLY to its specific LLM layer — nothing leaks across layers.
    db_llm_rules: dict = {}


app = FastAPI(title="BioChirp Orchestrator Service", version="1.0.0",
              description="Single orchestrator over the schema_kg tool fleet")
add_open_cors(app)
add_health_endpoint(app)


@app.get("/")
def root():
    return {"message": "orchestrator service is running", "default_db": DEFAULT_DB}


@app.post("/orchestrate")
async def orchestrate(input_value: OrchestrateRequest, database: str = ""):
    db = (database or DEFAULT_DB).strip()
    request_id = str(uuid.uuid4())
    query = (input_value.query or "").strip()
    events: list = []

    conn_id = (input_value.connection_id or "").strip()
    _progress_redis = _get_progress_redis() if conn_id else None
    _progress_chan = f"orch_progress:{conn_id}" if conn_id else None

    async def sink(evt: dict) -> None:
        events.append(evt)
        # Fire-and-forget live publish so the frontend sees each step as it runs.
        if _progress_redis is not None and _progress_chan:
            try:
                await _progress_redis.publish(_progress_chan, json.dumps(evt, default=str))
            except Exception as _pub_err:
                logger.debug("[orchestrator] progress publish failed: %s", _pub_err)

    async def publish_step_summary(tool: str, summary: dict) -> None:
        """Publish a step's DATA (parsed_value / tables / canonical_pv / row_count)
        live, so the chat relay can render a rich card body in real time instead
        of an empty placeholder."""
        if _progress_redis is not None and _progress_chan:
            try:
                await _progress_redis.publish(_progress_chan, json.dumps(
                    {"type": "orch_step_summary", "tool": tool, "summary": summary},
                    default=str))
            except Exception:
                pass

    router = RouterTool()
    mapper = SchemaMapperTool(event_sink=sink)
    planner = PlannerTool(event_sink=sink)
    expander = ExpandAndMatchTool(event_sink=sink)
    executor = ExecuteTool(event_sink=sink)

    if not query:
        return {"action": "direct_answer", "answer": "Please provide a query.",
                "events": events}

    # 1) Route
    rules = input_value.db_llm_rules or {}
    decision = await router.route(query, display_name=input_value.display_name,
                                  capabilities=input_value.capabilities,
                                  limitations=input_value.limitations,
                                  db_llm_rules=rules,
                                  db_name=db)
    action = decision.get("action", "query_db")
    # Router expands abbreviations (NSCLC→Non-Small Cell Lung Cancer, etc.) in
    # rephrased_query. Use it for schema_mapper + expander so abbreviation-only
    # queries don't produce sentinel parsed_values (disease_name='requested').
    rephrased_query = decision.get("rephrased_query") or query
    logger.info("[orchestrator][%s] db=%s route=%s rephrased=%r", request_id, db, action, rephrased_query[:80])
    events.append({"type": "route", "action": action,
                   "rationale": decision.get("rationale", "")})

    if action == "direct_answer":
        return {"action": action, "answer": decision.get("answer", ""),
                "events": events}

    if action == "web_search":
        res = await web_tool.search_ex(query, request_id=request_id)
        answer = _label_web_answer(res, input_value.display_name)
        return {"action": action, "answer": answer,
                "source": "web" if res["searched"] else "model",
                "searched": res["searched"], "events": events}

    # 2) query_db — retrieval+map (coarse, strangler) then deterministic planning
    mapped = await mapper.map_query(db, rephrased_query,
                                    db_llm_rules=rules,
                                    request_id=request_id)
    if not mapped or not mapped.get("matched"):
        res = await web_tool.search_ex(query, request_id=request_id)
        answer = _label_schema_mismatch_answer(res, input_value.display_name)
        return {"action": "web_search", "answer": answer,
                "source": "web" if res["searched"] else "model",
                "searched": res["searched"],
                "note": "no schema_kg match — fell back to web", "events": events}

    sk_plan = mapped.get("plan") or {}
    parsed_value = sk_plan.get("parsed_value") or {}
    await publish_step_summary("schema_mapper", {
        "parsed_value":    parsed_value,
        "rephrased_query": rephrased_query,
    })

    # schema_mapper already pruned the plan; planner only does the deterministic
    # to_production_plan conversion (no re-prune, no lossy kept reconstruction).
    planned = await planner.plan_from_pruned(db, sk_plan, request_id=request_id)
    if not planned or not planned.get("matched"):
        return {"action": "query_db", "matched": False,
                "parsed_value": parsed_value,
                "note": "planner produced no connectable plan", "events": events}

    production_plan = planned.get("production_plan") or {}
    if not production_plan:
        return {"action": "query_db", "matched": False,
                "parsed_value": parsed_value,
                "note": "planner returned empty production_plan", "events": events}
    await publish_step_summary("schema_planner", {"tables": list(production_plan.get("tables", []))})

    # 3) Normalize entity values: expand raw LLM-extracted names to canonical
    #    DB vocabulary via synonym + fuzzy + semantic + LLM-filter pipeline.
    #    Falls back to raw parsed_value on any failure.
    canonical_pv = await expander.expand(
        db=db,
        query=rephrased_query,
        parsed_value=parsed_value,
        request_id=request_id,
    )
    # The global expand_and_match returns a flat cross-DB dict where DB-specific
    # output-selector columns (partner_gene_symbol, experimental, fusion, etc.)
    # come back as null instead of "requested". Restore them from the schema_mapper
    # parsed_value so the execute endpoint's _plan_intercept includes them in
    # out_cols (columns to project into the result table).
    if canonical_pv is None:
        canonical_pv = {}
    for k, v in parsed_value.items():
        if v == "requested" and not canonical_pv.get(k):
            canonical_pv[k] = "requested"
    await publish_step_summary("expand_and_match", {"canonical_pv": canonical_pv or {}})

    # 4) Execute: join + finalize in the per-DB execute tool server.
    #    The execute endpoint has access to the DB parquets and HCDT hooks.
    result = await executor.execute(
        db=db,
        query=query,
        parsed_value=canonical_pv,
        production_plan=production_plan,
        connection_id=input_value.connection_id,
        request_id=request_id,
    )

    # Retry-with-variation: a 0-row result can be a genuine gap OR the
    # entity-extraction LLM misreading narrative/motivation context in the
    # question as an extra filter (e.g. "...which keeps showing up in
    # relation to epilepsy..." wrongly extracted as a disease/geneset
    # filter — see value_mapper.py's CONTEXT vs. FILTER rule). One
    # deterministic retry, re-running mapper->planner->expander->executor
    # with a corrective hint injected via the existing mapper_note plumbing,
    # catches this server-side instead of depending on the calling client to
    # notice and rephrase on its own. Only replaces the result if the retry
    # actually finds rows — never lets a failed/errored retry clobber a
    # legitimate (if genuinely empty) original result.
    if isinstance(result, dict) and not (result.get("row_count") or 0):
        retry_rules = {**rules, "mapper": (
            "Your previous extraction on this exact question returned ZERO "
            "rows. Re-examine every filter you emitted: for each one, remove "
            "the clause containing it and check whether the question still "
            "asks for the same thing. If it does, that term was background/"
            "motivation context, not a real filter -- drop it (mark the "
            "column 'requested' or omit it) and keep only the filters that "
            "are the actual subject of the question."
        )}
        retry_mapped = await mapper.map_query(db, rephrased_query,
                                              db_llm_rules=retry_rules,
                                              request_id=request_id)
        if retry_mapped and retry_mapped.get("matched"):
            retry_sk_plan = retry_mapped.get("plan") or {}
            retry_pv = retry_sk_plan.get("parsed_value") or {}
            retry_planned = await planner.plan_from_pruned(db, retry_sk_plan, request_id=request_id)
            retry_prod_plan = (retry_planned or {}).get("production_plan") or {}
            if retry_planned and retry_planned.get("matched") and retry_prod_plan:
                retry_canonical_pv = await expander.expand(
                    db=db, query=rephrased_query, parsed_value=retry_pv,
                    request_id=request_id,
                )
                if retry_canonical_pv is None:
                    retry_canonical_pv = {}
                for k, v in retry_pv.items():
                    if v == "requested" and not retry_canonical_pv.get(k):
                        retry_canonical_pv[k] = "requested"
                retry_result = await executor.execute(
                    db=db, query=query, parsed_value=retry_canonical_pv,
                    production_plan=retry_prod_plan,
                    connection_id=input_value.connection_id, request_id=request_id,
                )
                if isinstance(retry_result, dict) and (retry_result.get("row_count") or 0) > 0:
                    events.append({
                        "type": "retry",
                        "reason": "0-row result -- re-extracted with corrective hint",
                        "new_row_count": retry_result.get("row_count"),
                    })
                    parsed_value, production_plan, canonical_pv, result = (
                        retry_pv, retry_prod_plan, retry_canonical_pv, retry_result
                    )

    # Attach the accumulated event log and per-step summaries so the chat UI
    # can relay intermediate tool steps as rich progress cards. The /hcdt proxy
    # strips both via response_model=DatabaseTable serialization.
    if result is None:
        # Execute timed out or crashed — return a well-formed error dict so that
        # schema_kg_chat.py never receives a null JSON body (which crashes data.pop()).
        # Include database+tool so DatabaseTable(**data) succeeds in schema_kg_chat.
        return {
            "database": db,
            "tool": db,
            "row_count": 0,
            "status": "error",
            "message": "Execute step timed out — try a more specific query.",
            "_orch_events": events,
            "_tool_summaries": {},
        }
    if isinstance(result, dict):
        await publish_step_summary("execute", {
            "row_count": result.get("row_count") or 0,
            "filter_trace": result.get("filter_trace") or [],
        })
        result["_orch_events"] = events
        # Structured per-step data for rich card summaries (not part of DatabaseTable).
        result["_tool_summaries"] = {
            "schema_mapper": {
                "parsed_value": parsed_value,
            },
            "schema_planner": {
                "tables": list(production_plan.get("tables", [])),
            },
            "expand_and_match": {
                "canonical_pv": canonical_pv or {},
                # DB-level match counts for each filter column (from execute result).
                # filter_trace entries without "JOIN(" are column filters; rows_after
                # is the number of DB records that matched the canonical term.
                "filter_trace": [
                    ft for ft in (result.get("filter_trace") or [])
                    if not ft.get("column", "").startswith("JOIN(")
                ],
            },
            "execute": {
                "row_count": result.get("row_count") or 0,
                # Full operation trace (filters + joins + per-DB hook steps such
                # as confidence-tier / intersection) so the execute card can show
                # a plain-English "operations performed" summary.
                "filter_trace": result.get("filter_trace") or [],
            },
        }
    return result
