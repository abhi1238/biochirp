"""schema_mapper_tool — orchestrator client for biochirp_schema_mapper_tool (8019).

STRANGLER WRAPPER. Today the expand -> ANN -> filter -> value-map stages are
still fused inside the shared schema_mapper service (it returns a full pruned
plan including `parsed_value` and `kept`-equivalent info). The orchestrator uses
this one coarse wrapper for that whole stage now; as each fine-grained backend
is extracted (expander_tool, retrieval_tool, filter_tool, value_mapper_tool),
this wrapper is replaced by those — without ever breaking the flow.

Returns {"matched": bool, "plan": {...}|None}. The pruned plan carries
`parsed_value` (for the planner) and `plan_tables` (already-planned tables, since
schema_mapper currently also runs the planner internally).
"""
from __future__ import annotations

from typing import Optional

from .generic_tool import GenericTool


class SchemaMapperTool(GenericTool):
    name = "schema_mapper"
    env_host = "SCHEMA_MAPPER_HOST"
    env_port = "SCHEMA_MAPPER_PORT"
    default_port = "8019"
    path = "/schema_mapper"
    timeout = 60.0

    async def map_query(self, db: str, query: str,
                        db_llm_rules: dict | None = None,
                        request_id: str = "") -> Optional[dict]:
        """Map a query to schema columns + values.

        The "col_selection", "mapper" and "tiebreaker" keys from db_llm_rules are
        forwarded — each injected into its specific schema_mapper LLM stage:
          col_selection → query_expander (column selection)
          mapper        → value_mapper mapper_1/mapper_2 (entity extraction)
          tiebreaker    → value_mapper orchestrator (dual-mapper disagreement
                          resolver — breaks ties between mapper_1 and mapper_2)
        Other keys are ignored here so each layer stays isolated.
        """
        payload = {"query": query}
        rules = db_llm_rules or {}
        col_sel    = (rules.get("col_selection") or "").strip()
        mapper     = (rules.get("mapper") or "").strip()
        tiebreaker = (rules.get("tiebreaker") or "").strip()
        if col_sel:
            payload["col_selection_note"] = col_sel
        if mapper:
            payload["mapper_note"] = mapper
        if tiebreaker:
            payload["tiebreaker_note"] = tiebreaker
        return await self.call(payload, db=db, request_id=request_id)
