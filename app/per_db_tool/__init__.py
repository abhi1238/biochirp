"""Shared per-DB tool-backend library.

Replaces the ~70-line `main.py` FastAPI shell duplicated across the single-DB
data-tool services (all now under `app/tools/<db>/`). Each service that opts
in becomes a ~15-line wrapper:

    # app/tools/ttd/app/main.py
    from app.per_db_tool import build_app
    from app.ttd import return_ttd_result, get_ttd_db

    app = build_app(
        db_short="ttd",
        return_result_fn=return_ttd_result,
        get_db_fn=get_ttd_db,
        display_name="TTD",
    )

"""
from ._main import build_app
from ._httpx_client import get_httpx_client
from ._finalize import QueryState, finalize_db_result
from ._orchestrator import WorkerCtx, execute_db_query, make_db_result_handler
from ._service_factory import setup_service_globals
from ._execute_endpoint import ExecuteRequest, register_execute_endpoint
from .schema_kg_worker import (
    SchemaKgConfig, make_schema_kg_handler, call_orchestrator, call_web_tool,
)
from .schema_kg_planner import get_planner
from .schema_kg_chat import ChatSpec, build_chat_router

__all__ = [
    "build_app",
    "get_httpx_client",
    "QueryState", "finalize_db_result",
    "WorkerCtx", "execute_db_query", "make_db_result_handler",
    "setup_service_globals",
    "ExecuteRequest", "register_execute_endpoint",
    # schema_kg shared pipeline
    "SchemaKgConfig", "make_schema_kg_handler", "call_orchestrator", "call_web_tool",
    "get_planner", "ChatSpec", "build_chat_router",
]
