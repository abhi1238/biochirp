"""ExpandAndMatchTool — orchestrator client for biochirp_expand_and_match_db_tool (port 8009).

Called after schema_mapper + planner to normalize raw LLM-extracted entity values
(drug names, gene symbols, disease names, …) against actual DB vocabulary before
passing canonical values to the per-DB execute endpoint.
"""
from __future__ import annotations

from .generic_tool import GenericTool


class ExpandAndMatchTool(GenericTool):
    name = "expand_and_match"
    env_host = "EXPAND_AND_MATCH_DB_HOST"
    env_port = "EXPAND_AND_MATCH_DB_PORT"
    default_host = "biochirp_expand_and_match_db_tool"
    default_port = "8009"
    path = "/expand_and_match_db"
    timeout = 60.0

    async def expand(
        self,
        db: str,
        query: str,
        parsed_value: dict,
        request_id: str = "",
    ) -> dict:
        """Normalize raw LLM-extracted entity values to canonical DB vocabulary.

        No per-DB LLM rule is injected here. The unified candidate filter judges
        semantic/fuzzy/synonym candidates and is deliberately left rule-free; the
        per-DB `tiebreaker` rule belongs to the dual-mapper disagreement resolver
        in the schema_mapper service, not this candidate filter.
        Returns the expanded parsed_value dict; falls back to raw parsed_value on failure.
        """
        payload = {"cleaned_query": query, "parsed_value": parsed_value}
        result = await self.call(
            payload,
            db=db,
            request_id=request_id,
        )
        if not result:
            return parsed_value
        value = result.get("value")
        if not value:
            return parsed_value
        return value
