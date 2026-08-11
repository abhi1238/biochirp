"""ExecuteTool — thin HTTP client wrapping the per-DB /execute endpoint.

Called after schema_mapper + schema_planner to run join+finalize on the
pre-computed production_plan. Each DB's tool container exposes
POST /execute?database=<db>; URL driven by EXECUTE_HOST/EXECUTE_PORT
(default: biochirp_hcdt_tool:8018).

Per-DB overrides: set EXECUTE_HOST_<DB> / EXECUTE_PORT_<DB> in the
orchestrator's env to route a specific DB's execute calls to a different
host (e.g. EXECUTE_HOST_STRING=biochirp_string_tool). Falls back to
the global EXECUTE_HOST/EXECUTE_PORT when no per-DB override is set.
"""
import os
from typing import Optional

from .generic_tool import GenericTool


class ExecuteTool(GenericTool):
    name = "execute"
    env_host = "EXECUTE_HOST"
    env_port = "EXECUTE_PORT"
    default_host = "biochirp_hcdt_tool"
    default_port = "8018"
    path = "/execute"
    timeout = float(os.getenv("EXECUTE_TIMEOUT", "180"))  # join + BGE scoring can take 60-120s on large result sets

    def url(self, db: Optional[str] = None) -> str:
        """Resolve per-DB execute endpoint, falling back to the global EXECUTE_HOST/PORT."""
        if db:
            per_db_host = os.getenv(f"EXECUTE_HOST_{db.upper()}")
            if per_db_host:
                per_db_port = os.getenv(f"EXECUTE_PORT_{db.upper()}", self.default_port)
                return f"http://{per_db_host}:{per_db_port}{self.path}?database={db}"
        return super().url(db)

    async def execute(
        self,
        db: str,
        query: str,
        parsed_value: dict,
        production_plan: dict,
        connection_id: str = "",
        request_id: str = "",
    ) -> dict:
        return await self.call(
            {
                "query": query,
                "parsed_value": parsed_value,
                "production_plan": production_plan,
                "connection_id": connection_id,
            },
            db=db,
            request_id=request_id,
        )
