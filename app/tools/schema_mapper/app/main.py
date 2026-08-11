"""BioChirp schema_mapper service — shared in-process schema_kg planner as ONE HTTP tool.

Extracts the schema_kg mapper/planner out of every per-DB container into a single
shared service. The bge embedding model + torch + Qdrant live here ONCE (this is
the only heavy image); the ~10 per-DB tool containers go lean and POST to this
service instead of running the model themselves.

  POST /schema_mapper?database=<db>   body: {"query": "..."}
   ->  {"matched": bool, "db": "<db>", "plan": {...}|null}

`matched=false` mirrors the in-process planner returning None (0 ANN hits /
non-biomedical / DB-irrelevant) — the caller then routes to the web tool.

Reuses app.per_db_tool.schema_kg_planner.get_planner (per-DB graph + collection
cache) and the module-level to-production-plan converter, so there is zero
duplicated planning logic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

from utils.service_setup import add_open_cors, add_health_endpoint
from app.per_db_tool.schema_kg_planner import get_planner

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

# DBs to pre-warm at startup (load graph + bge embeddings + Qdrant once).
# SINGLE SOURCE OF TRUTH: config.schema_kg_dbs discovers the set from the mounted
# schema_kg/inputs/<db>/ tree — the same source scripts/gen_compose.py uses for
# the lean-Dockerfile + nginx routing, so the two can never drift. An explicit
# SCHEMA_MAPPER_DBS env (comma-separated) still overrides for ad-hoc warm sets.
from config.schema_kg_dbs import discover_schema_kg_dbs

_env_dbs = os.getenv("SCHEMA_MAPPER_DBS", "").strip()
SCHEMA_MAPPER_DBS = ([d.strip() for d in _env_dbs.split(",") if d.strip()]
                     if _env_dbs else sorted(discover_schema_kg_dbs()))
PLAN_TIMEOUT_SEC = float(os.getenv("SCHEMA_MAPPER_TIMEOUT_SEC", "90"))


class MapRequest(BaseModel):
    query: str
    # Optional per-DB LLM rules (from resources/prompts/db_llm_rules.yaml, pushed
    # by the orchestrator / in-process worker). Each is appended to a distinct
    # LLM in the schema_kg pipeline: col_selection→expander, mapper→value-mapper,
    # tiebreaker→dual-mapper disagreement resolver. Empty → no change.
    col_selection_note: str = ""
    mapper_note: str = ""
    tiebreaker_note: str = ""


def _serialize_plan(plan: dict) -> dict:
    """Make the plan dict JSON-safe (sets -> sorted lists, tuple join_path -> lists).

    The caller (schema_kg_worker) rehydrates plan_tables/needed_tables back to
    sets and join_path back to tuples before using to_production_plan.
    """
    out = dict(plan)
    for k in ("needed_tables", "plan_tables"):
        if k in out and not isinstance(out[k], list):
            out[k] = sorted(out[k])
    if isinstance(out.get("join_path"), list):
        out["join_path"] = [list(step) for step in out["join_path"]]
    if isinstance(out.get("table_cols"), dict):
        out["table_cols"] = {t: sorted(cols) for t, cols in out["table_cols"].items()}
    return out


app = FastAPI(title="BioChirp Schema Mapper Service", version="1.0.0",
              description="Shared schema_kg planner/mapper as an HTTP tool")
add_open_cors(app)
add_health_endpoint(app)


@app.get("/")
def root():
    return {"message": "schema_mapper service is running",
            "warmed_dbs": SCHEMA_MAPPER_DBS}


@app.on_event("startup")
async def _warm():
    """Pre-load the bge model (once) + each DB's graph/Qdrant in the background.
    Fire-and-forget so the service answers /health immediately."""
    async def _do():
        for db in SCHEMA_MAPPER_DBS:
            try:
                await get_planner(db).warm()
            except Exception as exc:
                logger.warning("[schema_mapper] warm %s failed: %s", db, exc)
        logger.info("[schema_mapper] warm complete for %d DBs", len(SCHEMA_MAPPER_DBS))
    asyncio.create_task(_do())


@app.post("/schema_mapper")
async def schema_mapper(input_value: MapRequest, database: str):
    tool = "schema_mapper"
    request_id = str(uuid.uuid4())
    t0 = time.perf_counter()
    query = (input_value.query or "").strip()
    logger.info("[%s][%s] db=%s query=%r", tool, request_id, database, query[:80])

    if not query:
        return {"matched": False, "db": database, "plan": None}

    try:
        planner = get_planner(database)
        plan = await asyncio.wait_for(
            asyncio.to_thread(planner.plan_query_pruned, query,
                              input_value.col_selection_note,
                              input_value.mapper_note,
                              input_value.tiebreaker_note),
            timeout=PLAN_TIMEOUT_SEC,
        )
        elapsed = time.perf_counter() - t0
        if plan is None or not plan.get("plan_tables"):
            logger.info("[%s][%s] no match (%.2fs)", tool, request_id, elapsed)
            return {"matched": False, "db": database, "plan": None}
        logger.info("[%s][%s] matched tables=%s (%.2fs)", tool, request_id,
                    sorted(plan["plan_tables"]), elapsed)
        return {"matched": True, "db": database, "plan": _serialize_plan(plan)}
    except asyncio.TimeoutError:
        logger.error("[%s][%s] TIMEOUT after %ss", tool, request_id, PLAN_TIMEOUT_SEC)
        return {"matched": False, "db": database, "plan": None, "error": "timeout"}
    except Exception as exc:
        logger.error("[%s][%s] EXCEPTION: %s", tool, request_id, exc, exc_info=True)
        return {"matched": False, "db": database, "plan": None, "error": str(exc)}
