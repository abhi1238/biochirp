"""BioChirp schema_planner service — the deterministic prune+Steiner planner as a tool.

This is the **non-LLM** half of the old fused `plan_query_pruned`: given the
columns the retrieval+filter stage kept and the `parsed_value` the value-mapper
produced, it builds the query-specific FK subgraph, runs a Steiner tree to find
the minimal set of tables + the join path that connects them, and converts that
into the `join_and_filter_database()` production plan.

It holds ONLY the schema FK graph (built from schema_kg/inputs/<db>/*.json) — no
embeddings, no Qdrant, no torch — so it is the lean, cheap, fully-deterministic
tool to extract first.

  POST /schema_planner?database=<db>
    body: {
      "kept":         [["<db>.<table>.<col>", 0.91], ...],   # from filter tool
      "parsed_value": {"gene_symbol": ["EGFR"], "drug_name": "requested"},
      "question":     "...",          # optional, echoed back
      "clean_query":  "...",          # optional
      "mapper_agreement":  true,      # optional metadata passthrough
      "orchestrator_used": false      # optional metadata passthrough
    }
   ->  {
      "matched": bool, "db": "<db>",
      "plan": {...pruned plan, JSON-safe...} | null,
      "production_plan": {tables, parents, table_columns, join_pairs} | null
    }

`matched=false` (plan=null) mirrors the planner finding no connectable tables
for the given parsed_value — the caller decides how to degrade (e.g. web tool).

Reuses app.per_db_tool.schema_kg_planner.{get_graph, assemble_pruned_plan,
to_production_plan} so there is ZERO duplicated planning logic; this service is a
thin HTTP shell around the same functions plan_query_pruned() calls in-process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from utils.service_setup import add_open_cors, add_health_endpoint
from app.per_db_tool.schema_kg_planner import (
    get_graph, assemble_pruned_plan, to_production_plan,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

# DBs to pre-build graphs for at startup. SINGLE SOURCE OF TRUTH:
# config.schema_kg_dbs discovers the set from the mounted schema_kg/inputs/<db>/
# tree — the same source gen_compose.py and the schema_mapper use, so the three
# can never drift. An explicit SCHEMA_PLANNER_DBS env still overrides. Graph
# build is cheap (no model), so warming is fast.
from config.schema_kg_dbs import discover_schema_kg_dbs

_env_dbs = os.getenv("SCHEMA_PLANNER_DBS", "").strip()
SCHEMA_PLANNER_DBS = ([d.strip() for d in _env_dbs.split(",") if d.strip()]
                      if _env_dbs else sorted(discover_schema_kg_dbs()))
PLAN_TIMEOUT_SEC = float(os.getenv("SCHEMA_PLANNER_TIMEOUT_SEC", "30"))


class PlanRequest(BaseModel):
    kept: list = []
    parsed_value: dict = {}
    question: str = ""
    clean_query: str = ""
    mapper_agreement: bool = True
    orchestrator_used: bool = False
    # When the caller already has a pruned plan (e.g. from schema_mapper, which
    # runs the prune internally), pass it here to skip the Steiner re-prune and
    # only run the deterministic to_production_plan conversion. This avoids the
    # double-plan / lossy-kept reconstruction in the strangler stage.
    pruned_plan: Optional[dict] = None


def _serialize_plan(plan: dict) -> dict:
    """Make the pruned-plan dict JSON-safe (sets -> sorted lists, tuples -> lists)."""
    out = dict(plan)
    for k in ("needed_tables", "plan_tables"):
        if k in out and not isinstance(out[k], list):
            out[k] = sorted(out[k])
    if isinstance(out.get("join_path"), list):
        out["join_path"] = [list(step) for step in out["join_path"]]
    if isinstance(out.get("table_cols"), dict):
        out["table_cols"] = {t: sorted(cols) for t, cols in out["table_cols"].items()}
    return out


def _serialize_production_plan(prod: dict) -> dict:
    """Stringify tuple-keyed join_pairs so the plan is JSON-safe.

    Mirrors the conversion in schema_kg_worker._build_post_expand so the output
    is a drop-in for join_and_filter_database().
    """
    out = dict(prod)
    if isinstance(out.get("join_pairs"), dict):
        out["join_pairs"] = {
            (f"{k[0]},{k[1]}" if isinstance(k, tuple) else k): v
            for k, v in out["join_pairs"].items()
        }
    return out


def _rehydrate_pruned(plan: dict) -> dict:
    """Reverse _serialize_plan: lists back to the sets/tuples to_production_plan needs."""
    out = dict(plan)
    if isinstance(out.get("plan_tables"), list):
        out["plan_tables"] = set(out["plan_tables"])
    if isinstance(out.get("join_path"), list):
        out["join_path"] = [tuple(step) for step in out["join_path"]]
    return out


def _plan(database: str, req: PlanRequest) -> dict:
    """Synchronous core (run in a thread).

    Two modes:
      * convert-only — `pruned_plan` supplied (already pruned upstream): just run
        the deterministic to_production_plan. No graph, no re-prune. This is the
        strangler path (schema_mapper already pruned) and yields a production plan
        byte-identical to the in-process worker.
      * full — `kept` + `parsed_value` supplied: build graph, prune (Steiner),
        then convert. This is the standalone path once a pre-prune filter tool
        feeds raw kept columns.
    """
    if req.pruned_plan:
        pruned = _rehydrate_pruned(req.pruned_plan)
        prod = to_production_plan(pruned, database)
        return {
            "plan": _serialize_plan(pruned),
            "production_plan": _serialize_production_plan(prod),
        }

    graph, _rules = get_graph(database)
    # _build_pruned_subgraph only reads col_id from each kept entry; normalise to
    # 2-tuples so a [col_id] or [col_id, score, ...] payload both work.
    kept = [(it[0], it[1] if len(it) > 1 else 0.0) for it in req.kept if it]
    pruned = assemble_pruned_plan(
        kept, req.parsed_value, graph, database,
        question=req.question, clean_query=req.clean_query,
        mapper_agreement=req.mapper_agreement,
        orchestrator_used=req.orchestrator_used,
        rules=_rules,
    )
    prod = to_production_plan(pruned, database)
    return {
        "plan": _serialize_plan(pruned),
        "production_plan": _serialize_production_plan(prod),
    }


app = FastAPI(title="BioChirp Schema Planner Service", version="1.0.0",
              description="Deterministic prune+Steiner schema_kg planner as an HTTP tool")
add_open_cors(app)
add_health_endpoint(app)


@app.get("/")
def root():
    return {"message": "schema_planner service is running",
            "warmed_dbs": SCHEMA_PLANNER_DBS}


@app.on_event("startup")
async def _warm():
    """Pre-build each DB's FK graph in the background (cheap, no model).
    Fire-and-forget so the service answers /health immediately."""
    async def _do():
        for db in SCHEMA_PLANNER_DBS:
            try:
                await asyncio.to_thread(get_graph, db)
            except Exception as exc:
                logger.warning("[schema_planner] warm %s failed: %s", db, exc)
        logger.info("[schema_planner] graph warm complete for %d DBs",
                    len(SCHEMA_PLANNER_DBS))
    asyncio.create_task(_do())


@app.post("/schema_planner")
async def schema_planner(input_value: PlanRequest, database: str):
    tool = "schema_planner"
    request_id = str(uuid.uuid4())
    t0 = time.perf_counter()
    logger.info("[%s][%s] db=%s parsed_value=%s kept=%d", tool, request_id,
                database, input_value.parsed_value, len(input_value.kept))

    if not input_value.parsed_value and not input_value.pruned_plan:
        return {"matched": False, "db": database, "plan": None,
                "production_plan": None}

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_plan, database, input_value),
            timeout=PLAN_TIMEOUT_SEC,
        )
        elapsed = time.perf_counter() - t0
        plan_tables = result["plan"].get("plan_tables") or []
        if not plan_tables:
            logger.info("[%s][%s] no connectable tables (%.3fs)", tool, request_id, elapsed)
            return {"matched": False, "db": database, "plan": None,
                    "production_plan": None}
        logger.info("[%s][%s] matched tables=%s (%.3fs)", tool, request_id,
                    plan_tables, elapsed)
        return {"matched": True, "db": database, **result}
    except asyncio.TimeoutError:
        logger.error("[%s][%s] TIMEOUT after %ss", tool, request_id, PLAN_TIMEOUT_SEC)
        return {"matched": False, "db": database, "plan": None,
                "production_plan": None, "error": "timeout"}
    except Exception as exc:
        logger.error("[%s][%s] EXCEPTION: %s", tool, request_id, exc, exc_info=True)
        return {"matched": False, "db": database, "plan": None,
                "production_plan": None, "error": str(exc)}
