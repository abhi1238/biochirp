

from typing import Set, List, Optional, Dict, Any
import os
import logging
import pandas as pd
import redis.asyncio as redis
import json


# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")

SERVICE_NAME = "target_tool"
RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")
MAX_PREVIEW_ROWS = 50

# ------------------------------------------------------------------------------
# REDIS
# ------------------------------------------------------------------------------
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
            logger.info("[%s function] Redis connected", SERVICE_NAME)
        except Exception as e:
            logger.error("[%s function] Redis init failed: %s", SERVICE_NAME, e, exc_info=True)
            _redis_client = None
    return _redis_client





# Per-connection CSV registry: connection_id → [(tool_key, csv_path), ...]
# Populated by _publish_ws so main.py can include csv_paths in the `final`
# event even when the tool's JSON output is too large to parse.
_connection_csv_registry: Dict[str, List[tuple]] = {}


def register_connection_csv(conn_id: str, tool_key: str, csv_path: str) -> None:
    """Record a (tool_key, csv_path) pair for a connection."""
    if conn_id and csv_path:
        _connection_csv_registry.setdefault(conn_id, []).append((tool_key, csv_path))


def pop_connection_csvs(conn_id: str) -> Dict[str, str]:
    """Return and clear all registered CSV paths for conn_id.

    Returns dict: tool_key → csv_path (last one wins if a tool fires twice).
    """
    entries = _connection_csv_registry.pop(conn_id, [])
    return {k: v for k, v in entries}


async def _publish_ws(
    conn_id: str,
    csv_path: str,
    rows: int,
    service_name: Optional[str] = None,
) -> None:
    if not conn_id or not csv_path:
        return

    client = await _get_redis()
    if not client:
        logger.warning("[%s function][ws] Redis unavailable", SERVICE_NAME)
        return

    name = service_name or SERVICE_NAME

    # Register locally so main.py can include this path in the `final` event
    # as a fallback if the client misses the Redis-relayed *_table event.
    register_connection_csv(conn_id, name, csv_path)

    payload = {
        "type": f"{name}_table",
        "csv_path": csv_path,
        "row_count": rows,
    }

    try:
        await client.publish(conn_id, json.dumps(payload))
        logger.info(
            "[%s function][ws] Published %s_table — %s rows",
            name,
            name,
            rows,
        )
    except Exception as e:
        logger.error("[%s function][ws] Publish failed: %s", name, e, exc_info=True)
