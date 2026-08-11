"""planner_tool — orchestrator client for biochirp_schema_planner_tool (port 8020).

The deterministic prune+Steiner step. Given the columns the filter kept and the
value-mapper's parsed_value, returns the join plan + production plan.
"""
from __future__ import annotations

import os
from typing import Optional

from .generic_tool import GenericTool


class PlannerTool(GenericTool):
    name = "schema_planner"
    env_host = "SCHEMA_PLANNER_HOST"
    env_port = "SCHEMA_PLANNER_PORT"
    default_port = "8020"
    path = "/schema_planner"
    timeout = float(os.getenv("PLANNER_TIMEOUT_SEC", "30"))

    async def plan_from_pruned(self, db: str, pruned_plan: dict,
                               request_id: str = "") -> Optional[dict]:
        """Convert-only mode: the plan is ALREADY pruned upstream (schema_mapper),
        so just run the deterministic to_production_plan. No re-prune, no lossy
        kept reconstruction — output matches the in-process worker exactly."""
        return await self.call({"pruned_plan": pruned_plan}, db=db,
                               request_id=request_id)
