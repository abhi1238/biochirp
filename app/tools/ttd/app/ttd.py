# # app/ttd.py
# import logging
# from typing import List, Optional
# import os
# import json
# import redis.asyncio as redis
# import polars as pl
# import requests
# from agents import Agent, Runner
# from config.schema import database_schemas
# from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
# from .database_loader import return_preprocessed_ttd
# from utils.dataframe_filtering import join_and_filter_database
# from utils.preprocess import _safe, _csv_path

# # --------------------------------------------------------------------------- #
# #  CONFIG
# # --------------------------------------------------------------------------- #
# SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")  # Generalized via env var
# DB_NAME = "Therapeutic Target Database"
# HEAD_VIEW_ROW_COUNT = int(os.getenv("HEAD_VIEW_ROW_COUNT", "50"))
# RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")

# # --------------------------------------------------------------------------- #
# #  LOGGING
# # --------------------------------------------------------------------------- #

# # Configure logging
# logging.basicConfig(
#     level=logging.INFO,
#     format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
#     datefmt="%Y-%m-%d %H:%M:%S"
# )
# log = logging.getLogger("uvicorn.error")

# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("httpcore").setLevel(logging.WARNING)

# from dotenv import load_dotenv
# load_dotenv(override=True)

# # Load prompt
# md_file_path = "/app/resources/prompts/agent_summarizer.md"
# with open(md_file_path, "r", encoding="utf-8") as f:
#     prompt_md = f.read()

# # --------------------------------------------------------------------------- #
# #  REDIS (lazy async client)
# # --------------------------------------------------------------------------- #
# REDIS_HOST = os.getenv("REDIS_HOST", "biochirp_redis_tool")
# REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
# _redis_client: Optional[redis.Redis] = None

# async def _get_redis() -> Optional[redis.Redis]:
#     global _redis_client
#     if _redis_client is None:
#         try:
#             _redis_client = redis.Redis(
#                 host=REDIS_HOST,
#                 port=REDIS_PORT,
#                 decode_responses=True,
#                 socket_connect_timeout=5,
#                 socket_timeout=5,
#             )
#             await _redis_client.ping()
#             log.info("[%s function] Redis connected", SERVICE_NAME)
#         except Exception as e:
#             log.error("[%s function] Redis init failed: %s", SERVICE_NAME, e, exc_info=True)
#             _redis_client = None
#     return _redis_client

# # --------------------------------------------------------------------------- #
# #  JSON (orjson fallback)
# # --------------------------------------------------------------------------- #
# try:
#     import orjson as _orjson
#     def _dumps(o): return _orjson.dumps(o).decode()
# except Exception:  # pragma: no cover
#     import json
#     def _dumps(o): return json.dumps(o, ensure_ascii=False)

# def _post(url: str, **kw) -> Optional[requests.Response]:
#     try:
#         r = requests.post(url, timeout=kw.pop("timeout", 12), **kw)
#         r.raise_for_status()
#         return r
#     except requests.RequestException as e:
#         log.error("[%s function] POST %s failed: %s", SERVICE_NAME, url, e, exc_info=True)
#         return None

# def _valid_columns(req: dict, db: str) -> List[str]:
#     schema = {c for tbl in database_schemas[db].values() for c in tbl}
#     return [
#         col for col, val in req.items()
#         if col in schema and (val == "requested" or (isinstance(val, list) and val))
#     ]

# async def _publish_ws(conn_id: str, csv_path: str, rows: int) -> None:
#     if not conn_id or not csv_path:
#         return
#     payload = {"type": f"{SERVICE_NAME}_table", "csv_path": csv_path, "row_count": rows}
#     client = await _get_redis()
#     if not client:
#         log.warning("[%s function][ws] Redis unavailable", SERVICE_NAME)
#         return
#     try:
#         await client.publish(conn_id, _dumps(payload))
#         log.info("[%s function][ws] Published %s_table – %s rows", SERVICE_NAME, SERVICE_NAME, rows)
#     except Exception as e:
#         log.error("[%s function][ws] Publish failed: %s", SERVICE_NAME, e, exc_info=True)

# # --------------------------------------------------------------------------- #
# #  MAIN WORKER
# # --------------------------------------------------------------------------- #
# async def return_ttd_result(
#     input: QueryInterpreterOutputGuardrail,
#     connection_id: Optional[str] = None,
# ) -> DatabaseTable:
#     db = SERVICE_NAME
#     tool = SERVICE_NAME
#     error_msg = None
#     data = None
#     inp = None
#     expand = None
#     filter_val = {}
#     out_cols = []
#     plan = None
#     df = pl.DataFrame()
#     preview = []
#     csv_path = ""
#     message = ""

#     # ------------------------------------------------- Load DB
#     try:
#         data = return_preprocessed_ttd()
#         log.info("[%s function] DB loaded", db)
#     except Exception as e:
#         error_msg = f"Failed to load {DB_NAME}: {str(e)}"
#         log.error("[%s function] DB load error: %s", db, e, exc_info=True)

#     # ------------------------------------------------- Parse input
#     if not error_msg:
#         try:
#             inp = input.model_dump(exclude_none=True)
#         except Exception as e:
#             error_msg = "Invalid input format."
#             log.error("[%s function] Input parse error: %s", db, e, exc_info=True)

#     # ------------------------------------------------- Expand & Match
#     if not error_msg:
#         expand_resp = _post(
#             f"http://biochirp_expand_and_match_db_tool:8009/expand_and_match_db?database={db}",
#             json=inp)

#         if not expand_resp:
#             error_msg = "Expand and match database tool unreachable."
#         else:
#             try:
#                 expand = expand_resp.json()
#             except Exception as e:
#                 error_msg = "Expand tool returned malformed JSON."
#                 log.error("[%s function] Expand JSON error: %s", db, e, exc_info=True)

#         if not error_msg:
#             filter_val = expand.get("value", {}) or {}
#             out_cols = _valid_columns(filter_val, db)

#     # ------------------------------------------------- Validate columns
#     if not error_msg:
#         # schema_cols = {c for tbl in database_schemas[db].values() for c in tbl}
#         schema_cols = {c for tbl in database_schemas[db].values() for c in tbl}
#         used_cols = [
#                 c for c, v in filter_val.items()
#                 if v == "requested" or (isinstance(v, list) and v)
#             ]
#         missing = [c for c in used_cols if c not in schema_cols]
#         if missing:
#             error_msg = f"Columns not in {DB_NAME}: `{', '.join(missing)}` – skipping."
#             log.warning("[%s function] %s", db, error_msg)

#         #         missing = [c for c in out_cols if c not in schema_cols]
#         # if missing:
#         #     error_msg = f"Columns not in {DB_NAME}: `{', '.join(missing)}` – skipping."
#         #     log.warning("[%s function] %s", db, error_msg)

#     # ------------------------------------------------- Planner
#     if not error_msg:
#         plan_resp = _post(
#             f"http://biochirp_planner_tool:8011/planner?database={db}",
#             json=expand)

#         if not plan_resp:
#             error_msg = "Planner tool unreachable."
#         else:
#             try:
#                 plan = plan_resp.json().get("plan")
#                 plan = plan.get("plan") if isinstance(plan, dict) and "plan" in plan else plan
#             except Exception as e:
#                 log.error("[%s function] Planner JSON error: %s", db, e, exc_info=True)
#                 plan = None

#             if not plan:
#                 error_msg = "Planner failed to provide a valid plan."
#                 log.error("[%s function] %s", db, error_msg)

#     # ------------------------------------------------- Query
#     if not error_msg:
#         try:
#             df = join_and_filter_database(data, plan, db, out_cols, filter_val)
#             log.info("[%s function] Query result: %s", db, df.shape)
#         except Exception as e:
#             error_msg = f"Query failed: {str(e)}"
#             log.error("[%s function] Query error: %s", db, e, exc_info=True)

#         if df.is_empty():
#             error_msg = "No rows matched."

#     if not error_msg:
#         preview = df.head(HEAD_VIEW_ROW_COUNT).to_dicts()

#     if not error_msg and connection_id:
#         csv_path = _csv_path(f"{tool}_results")
#         try:
#             os.makedirs(os.path.dirname(csv_path), exist_ok=True)
#             df.write_csv(csv_path)  # **FULL** data
#             log.info("[%s function] CSV saved: %s (%d rows)", tool, csv_path, df.height)
#         except Exception as e:
#             error_msg = f"CSV write failed: {str(e)}"
#             log.error("[%s function] CSV write failed: %s", tool, e, exc_info=True)
#             csv_path = ""
#         await _publish_ws(connection_id, csv_path, df.height)

#     if not error_msg:
#         summarizer_agent = Agent(name="summarizer", model="gpt-4o-mini",
#                                  instructions=prompt_md,
#                                 tools=[], output_type=str)

#         database_summarizer = await Runner.run(
#             summarizer_agent,
#             str({
#                 "database": db,
#                 "table": preview,
#                 "row_count": df.height,
#                 "plan": plan,
#                 "filter_value": filter_val,
#                 "parsed_value": input.parsed_value,
#                 "query": input.cleaned_query
#             })
#         )

#     if error_msg:
#         message = error_msg
#     else:
#         message = database_summarizer.final_output
#         log.info("[%s function] NL language: %s", db, df.shape)

#     return DatabaseTable(
#         database=db,
#         table=preview if not error_msg else None,  # <-- 50-row preview for UI if success
#         csv_path=csv_path if not error_msg else None,  # <-- full file path if success
#         row_count=df.height if not error_msg else None,
#         tool=tool,
#         message=message,
#     )


# # app/ttd.py

# import logging
# import os
# import json
# import redis.asyncio as redis
# import polars as pl
# import requests

# from typing import List, Optional
# from config.schema import database_schemas
# from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
# from .database_loader import return_preprocessed_ttd
# from utils.dataframe_filtering import join_and_filter_database
# from utils.preprocess import _csv_path, _safe, _dumps

# SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")
# DB_NAME = "Therapeutic Target Database"
# HEAD_VIEW_ROW_COUNT = int(os.getenv("HEAD_VIEW_ROW_COUNT", "50"))
# RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")

# log = logging.getLogger("uvicorn.error")

# # Redis client (lazy)
# REDIS_HOST = os.getenv("REDIS_HOST", "biochirp_redis_tool")
# REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
# _redis_client: Optional[redis.Redis] = None

# async def _get_redis() -> Optional[redis.Redis]:
#     global _redis_client
#     if _redis_client is None:
#         try:
#             _redis_client = redis.Redis(
#                 host=REDIS_HOST,
#                 port=REDIS_PORT,
#                 decode_responses=True,
#                 socket_connect_timeout=5,
#                 socket_timeout=5,
#             )
#             await _redis_client.ping()
#             log.info(f"[{SERVICE_NAME} function] Redis connected")
#         except Exception as e:
#             log.error(f"[{SERVICE_NAME} function] Redis init failed: {e}", exc_info=True)
#             _redis_client = None
#     return _redis_client

# def _post(url: str, **kw) -> Optional[requests.Response]:
#     try:
#         r = requests.post(url, timeout=kw.pop("timeout", 12), **kw)
#         r.raise_for_status()
#         return r
#     except requests.RequestException as e:
#         log.error(f"[{SERVICE_NAME} function] POST {url} failed: {e}", exc_info=True)
#         return None

# def _valid_columns(req: dict, db: str) -> List[str]:
#     schema_cols = {c for tbl in database_schemas[db].values() for c in tbl}
#     return [col for col, val in req.items() if col in schema_cols and (val == "requested" or (isinstance(val, list) and val))]

# async def _publish_ws(conn_id: str, csv_path: str, rows: int) -> None:
#     if not conn_id or not csv_path:
#         return
#     payload = {"type": f"{SERVICE_NAME}_table", "csv_path": csv_path, "row_count": rows}
#     client = await _get_redis()
#     if not client:
#         log.warning(f"[{SERVICE_NAME} function][ws] Redis unavailable")
#         return
#     try:
#         await client.publish(conn_id, _dumps(payload))
#         log.info(f"[{SERVICE_NAME} function][ws] Published {SERVICE_NAME}_table – {rows} rows")
#     except Exception as e:
#         log.error(f"[{SERVICE_NAME} function][ws] Publish failed: {e}", exc_info=True)

# async def return_ttd_result(
#     input: QueryInterpreterOutputGuardrail,
#     connection_id: Optional[str] = None,
# ) -> DatabaseTable:
#     db = SERVICE_NAME
#     tool = SERVICE_NAME
#     error_msg = None
#     df: pl.DataFrame = pl.DataFrame()
#     preview = []
#     csv_path = ""
#     message = ""

#     # Load DB
#     try:
#         data = return_preprocessed_ttd()
#         log.info(f"[{db} function] DB loaded")
#     except Exception as e:
#         error_msg = f"Failed to load {DB_NAME}: {e}"
#         log.error(f"[{db} function] DB load error: {e}", exc_info=True)

#     # Parse input
#     if not error_msg:
#         try:
#             inp = input.model_dump(exclude_none=True)
#         except Exception as e:
#             error_msg = "Invalid input format."
#             log.error(f"[{db} function] Input parse error: {e}", exc_info=True)

#     # Expand & match
#     if not error_msg:
#         expand_resp = _post(
#             f"http://biochirp_expand_and_match_db_tool:8009/expand_and_match_db?database={db}",
#             json=inp
#         )
#         if not expand_resp:
#             error_msg = "Expand and match database tool unreachable."
#         else:
#             try:
#                 expand = expand_resp.json()
#             except Exception as e:
#                 error_msg = "Expand tool returned malformed JSON."
#                 log.error(f"[{db} function] Expand JSON error: {e}", exc_info=True)

#         if not error_msg:
#             filter_val = expand.get("value", {}) or {}
#             out_cols = _valid_columns(filter_val, db)

#     # Validate columns
#     if not error_msg:
#         schema_cols = {c for tbl in database_schemas[db].values() for c in tbl}
#         used_cols = [c for c, v in filter_val.items() if v == "requested" or (isinstance(v, list) and v)]
#         missing = [c for c in used_cols if c not in schema_cols]
#         if missing:
#             error_msg = f"Columns not in {DB_NAME}: `{', '.join(missing)}` – skipping."
#             log.warning(f"[{db} function] {error_msg}")

#     # Planner
#     if not error_msg:
#         plan_resp = _post(
#             f"http://biochirp_planner_tool:8011/planner?database={db}",
#             json=expand
#         )
#         if not plan_resp:
#             error_msg = "Planner tool unreachable."
#         else:
#             try:
#                 plan = plan_resp.json().get("plan")
#                 plan = plan.get("plan") if isinstance(plan, dict) and "plan" in plan else plan
#             except Exception as e:
#                 log.error(f"[{db} function] Planner JSON error: {e}", exc_info=True)
#                 plan = None
#             if not plan:
#                 error_msg = "Planner failed to provide a valid plan."
#                 log.error(f"[{db} function] {error_msg}")

#     # Query
#     if not error_msg:
#         try:
#             df = join_and_filter_database(data, plan, db, out_cols, filter_val)
#             log.info(f"[{db} function] Query result: {db} shape = {df.shape}")
#         except Exception as e:
#             error_msg = f"Query failed: {e}"
#             log.error(f"[{db} function] Query error: {e}", exc_info=True)
#         if df.is_empty():
#             error_msg = "No rows matched."

#     if not error_msg:
#         preview = df.head(HEAD_VIEW_ROW_COUNT).to_dicts()

#     if not error_msg and connection_id:
#         csv_path = _csv_path(f"{tool}_results")
#         try:
#             os.makedirs(os.path.dirname(csv_path), exist_ok=True)
#             df.write_csv(csv_path)
#             log.info(f"[{db} function] CSV saved: {csv_path} ({df.height} rows)")
#         except Exception as e:
#             error_msg = f"CSV write failed: {e}"
#             log.error(f"[{db} function] CSV write failed: {e}", exc_info=True)
#             csv_path = ""
#         await _publish_ws(connection_id, csv_path, df.height)

#     if not error_msg:
#         # Optionally summariser agent logic here...
#         message = f"Retrieved {df.height} rows from {DB_NAME}."
#         log.info(f"[{db} function] NL language: {df.shape}")
#     else:
#         message = error_msg

#     return DatabaseTable(
#         database=db,
#         table=preview if not error_msg else None,
#         csv_path=csv_path if not error_msg else None,
#         row_count=df.height if not error_msg else None,
#         tool=tool,
#         message=message,
#     )





# app/ttd.py
import logging
import os
from typing import List, Optional

import polars as pl
import redis.asyncio as redis
import requests
from dotenv import load_dotenv

from agents import Agent, Runner
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from config.schema import database_schemas
from utils.dataframe_filtering import join_and_filter_database
from utils.preprocess import _csv_path, _safe
from .database_loader import return_preprocessed_ttd

# --------------------------------------------------------------------------- #
#  CONFIG
# --------------------------------------------------------------------------- #
SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")  # Generalized via env var
DB_NAME = "Therapeutic Target Database"
HEAD_VIEW_ROW_COUNT = int(os.getenv("HEAD_VIEW_ROW_COUNT", "50"))
RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")

# --------------------------------------------------------------------------- #
#  LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("uvicorn.error")

load_dotenv(override=True)

# --------------------------------------------------------------------------- #
#  PROMPT LOAD
# --------------------------------------------------------------------------- #
md_file_path = "/app/resources/prompts/agent_summarizer.md"
with open(md_file_path, "r", encoding="utf-8") as f:
    prompt_md = f.read()

# --------------------------------------------------------------------------- #
#  REDIS (lazy async client)
# --------------------------------------------------------------------------- #
REDIS_HOST = os.getenv("REDIS_HOST", "biochirp_redis_tool")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
_redis_client: Optional[redis.Redis] = None


async def _get_redis() -> Optional[redis.Redis]:
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            await _redis_client.ping()
            log.info("[%s function] Redis connected", SERVICE_NAME)
        except Exception as e:
            log.error("[%s function] Redis init failed: %s", SERVICE_NAME, e, exc_info=True)
            _redis_client = None
    return _redis_client


# --------------------------------------------------------------------------- #
#  JSON (orjson fallback)
# --------------------------------------------------------------------------- #
try:
    import orjson as _orjson

    def _dumps(o):
        return _orjson.dumps(o).decode()

except Exception:  # pragma: no cover
    import json

    def _dumps(o):
        return json.dumps(o, ensure_ascii=False)


def _post(url: str, **kw) -> Optional[requests.Response]:
    try:
        r = requests.post(url, timeout=kw.pop("timeout", 12), **kw)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.error("[%s function] POST %s failed: %s", SERVICE_NAME, url, e, exc_info=True)
        return None


def _valid_columns(req: dict, db: str) -> List[str]:
    schema = {c for tbl in database_schemas[db].values() for c in tbl}
    return [
        col
        for col, val in req.items()
        if col in schema and (val == "requested" or (isinstance(val, list) and val))
    ]


async def _publish_ws(conn_id: str, csv_path: str, rows: int) -> None:
    if not conn_id or not csv_path:
        return
    payload = {"type": f"{SERVICE_NAME}_table", "csv_path": csv_path, "row_count": rows}
    client = await _get_redis()
    if not client:
        log.warning("[%s function][ws] Redis unavailable", SERVICE_NAME)
        return
    try:
        await client.publish(conn_id, _dumps(payload))
        log.info(
            "[%s function][ws] Published %s_table – %s rows",
            SERVICE_NAME,
            SERVICE_NAME,
            rows,
        )
    except Exception as e:
        log.error("[%s function][ws] Publish failed: %s", SERVICE_NAME, e, exc_info=True)


# --------------------------------------------------------------------------- #
#  TTD DB CACHE (load once, reuse across requests)
# --------------------------------------------------------------------------- #
TTD_DB_CACHE = None  # type: ignore[assignment]


def get_ttd_db():
    """
    Load TTD DB once and reuse it across all requests.
    """
    global TTD_DB_CACHE
    if TTD_DB_CACHE is None:
        log.info("[%s function] Cold-start: loading TTD database into memory", SERVICE_NAME)
        TTD_DB_CACHE = return_preprocessed_ttd()
        try:
            num_tables = len(TTD_DB_CACHE[SERVICE_NAME])
        except Exception:
            num_tables = -1
        log.info(
            "[%s function] TTD database loaded into cache (tables=%s)",
            SERVICE_NAME,
            num_tables,
        )
    return TTD_DB_CACHE


# --------------------------------------------------------------------------- #
#  MAIN WORKER
# --------------------------------------------------------------------------- #
async def return_ttd_result(
    input: QueryInterpreterOutputGuardrail,
    connection_id: Optional[str] = None,
) -> DatabaseTable:
    db = SERVICE_NAME
    tool = SERVICE_NAME
    error_msg = None
    data = None
    inp = None
    expand = None
    filter_val = {}
    out_cols: list[str] = []
    plan = None
    df = pl.DataFrame()
    preview = []
    csv_path = ""
    message = ""

    # ------------------------------------------------- Load DB (from cache)
    try:
        data = get_ttd_db()
        log.info("[%s function] DB cache ready", db)
    except Exception as e:
        error_msg = f"Failed to load {DB_NAME}: {str(e)}"
        log.error("[%s function] DB load error: %s", db, e, exc_info=True)

    # ------------------------------------------------- Parse input
    if not error_msg:
        try:
            inp = input.model_dump(exclude_none=True)
        except Exception as e:
            error_msg = "Invalid input format."
            log.error("[%s function] Input parse error: %s", db, e, exc_info=True)

    # ------------------------------------------------- Expand & Match
    if not error_msg:
        expand_resp = _post(
            f"http://biochirp_expand_and_match_db_tool:8009/expand_and_match_db?database={db}",
            json=inp,
        )

        if not expand_resp:
            error_msg = "Expand and match database tool unreachable."
        else:
            try:
                expand = expand_resp.json()
            except Exception as e:
                error_msg = "Expand tool returned malformed JSON."
                log.error("[%s function] Expand JSON error: %s", db, e, exc_info=True)

        if not error_msg:
            filter_val = expand.get("value", {}) or {}
            out_cols = _valid_columns(filter_val, db)

    # ------------------------------------------------- Validate columns
    if not error_msg:
        schema_cols = {c for tbl in database_schemas[db].values() for c in tbl}
        used_cols = [
            c
            for c, v in filter_val.items()
            if v == "requested" or (isinstance(v, list) and v)
        ]
        missing = [c for c in used_cols if c not in schema_cols]
        if missing:
            error_msg = (
                f"Columns not in {DB_NAME}: `{', '.join(missing)}` – skipping."
            )
            log.warning("[%s function] %s", db, error_msg)

    # ------------------------------------------------- Planner
    if not error_msg:
        plan_resp = _post(
            f"http://biochirp_planner_tool:8011/planner?database={db}",
            json=expand,
        )

        if not plan_resp:
            error_msg = "Planner tool unreachable."
        else:
            try:
                plan_obj = plan_resp.json().get("plan")
                plan = plan_obj.get("plan") if isinstance(plan_obj, dict) and "plan" in plan_obj else plan_obj
            except Exception as e:
                log.error("[%s function] Planner JSON error: %s", db, e, exc_info=True)
                plan = None

            if not plan:
                error_msg = "Planner failed to provide a valid plan."
                log.error("[%s function] %s", db, error_msg)

    # ------------------------------------------------- Execute query
    if not error_msg:
        try:
            df = join_and_filter_database(data, plan, db, out_cols, filter_val)
            log.info("[%s function] Query result: %s", db, df.shape)
        except Exception as e:
            error_msg = f"Query failed: {str(e)}"
            log.error("[%s function] Query error: %s", db, e, exc_info=True)

        if df.is_empty():
            error_msg = "No rows matched."

    log.info("[%s function] After query execution", db)

    # ------------------------------------------------- Preview
    if not error_msg:
        preview = df.head(HEAD_VIEW_ROW_COUNT).to_dicts()
        log.info("[%s function] Preview rows: %d", db, len(preview))

    # ------------------------------------------------- CSV + WebSocket
    if not error_msg and connection_id:
        csv_path = _csv_path(f"{tool}_results")
        log.info("[%s function] CSV path: %s", db, csv_path)
        try:
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            df.write_csv(csv_path)  # **FULL** data
            log.info(
                "[%s function] CSV saved: %s (%d rows)",
                tool,
                csv_path,
                df.height,
            )
        except Exception as e:
            error_msg = f"CSV write failed: {str(e)}"
            log.error("[%s function] CSV write failed: %s", tool, e, exc_info=True)
            csv_path = ""

        await _publish_ws(connection_id, csv_path, df.height)

    # ------------------------------------------------- Summarization
    if not error_msg:
        summarizer_agent = Agent(
            name="summarizer",
            model="gpt-4o-mini",
            instructions=prompt_md,
            tools=[],
            output_type=str,
        )

        database_summarizer = await Runner.run(
            summarizer_agent,
            str(
                {
                    "database": db,
                    "table": preview,
                    "row_count": df.height,
                    "plan": plan,
                    "filter_value": filter_val,
                    "parsed_value": input.parsed_value,
                    "query": input.cleaned_query,
                }
            ),
        )

    # ------------------------------------------------- Final message
    if error_msg:
        message = error_msg
    else:
        message = database_summarizer.final_output
        log.info("[%s function] Natural language summary produced", db)

    return DatabaseTable(
        database=db,
        table=preview if not error_msg else None,      # 50-row preview for UI
        csv_path=csv_path if not error_msg else None,  # full file path if success
        row_count=df.height if not error_msg else None,
        tool=tool,
        message=message,
    )
