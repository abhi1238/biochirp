"""Shared /execute endpoint factory for schema_kg DB services.

The orchestrator tool (biochirp_orchestrator_tool) calls /<db>/execute after
running route→schema_mapper→schema_planner, passing a pre-computed production_plan.
Each DB service that participates in the orchestrator path must expose this endpoint.

Usage in a DB main.py:
    from app.per_db_tool import register_execute_endpoint
    register_execute_endpoint(
        app,
        db="ttd",
        display_name="TTD",
        get_db=get_ttd_db,
        prompt_md=prompt_md,
        summarizer_model=SUMMARIZER_MODEL_NAME,
    )
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from ._orchestrator import WorkerCtx, make_db_result_handler
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail

logger = logging.getLogger("uvicorn.error")


class ExecuteRequest(BaseModel):
    query: str
    parsed_value: dict
    production_plan: dict
    connection_id: Optional[str] = None


def _plan_intercept(production_plan: dict, parsed_value: dict):
    """Return a hook that injects a pre-computed plan into WorkerCtx,
    bypassing the expand+planner round-trips (steps 4-6)."""
    def _intercept(ctx: WorkerCtx) -> None:
        ctx.plan = production_plan
        ctx.inp["parsed_value"] = parsed_value
        ctx.filter_val = {
            k: v for k, v in parsed_value.items()
            if v not in (None, "requested") and v != [] and v is not None
        }
        ctx.out_cols = [k for k, v in parsed_value.items() if v == "requested"]
    return _intercept


def register_execute_endpoint(
    app: FastAPI,
    *,
    db: str,
    display_name: str,
    get_db: Callable,
    prompt_md: str,
    summarizer_model: str,
    **hooks,
) -> None:
    """Register POST /execute on *app* for the given DB.

    Called by biochirp_orchestrator_tool after route + schema_mapper + schema_planner.
    Receives the pre-computed production_plan, runs pre_join → join_and_filter →
    on_empty_fallback → finalize. Never does expand or planning.

    Optional per-DB hooks (pre_join, post_join, on_empty_result, …) may be passed
    as keyword args; they are forwarded to execute_db_query alongside the plan
    intercept, so a DB can mutate filter_val/out_cols/plan on the /execute path
    exactly as it does on the chat path.
    """
    @app.post("/execute", response_model=DatabaseTable)
    async def execute_endpoint(
        payload: ExecuteRequest,
        connection_id: Optional[str] = None,
        database: Optional[str] = None,  # passed by execute_tool (?database=<db>), ignored
    ):
        conn_id = connection_id or payload.connection_id
        request_id = str(uuid.uuid4())
        logger.info(
            "[%s execute][%s] tables=%s",
            db,
            request_id,
            payload.production_plan.get("tables", []),
        )
        fake_input = QueryInterpreterOutputGuardrail(cleaned_query=payload.query)
        handler = make_db_result_handler(
            db=db,
            display_name=display_name,
            get_db=get_db,
            prompt_md=prompt_md,
            summarizer_model=summarizer_model,
            intercept=_plan_intercept(payload.production_plan, payload.parsed_value),
            **hooks,
        )
        return await handler(input=fake_input, connection_id=conn_id)
