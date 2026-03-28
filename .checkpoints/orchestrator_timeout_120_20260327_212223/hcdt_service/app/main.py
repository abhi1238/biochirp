
import os
import sys
import re
import io
import csv
import json
import time
import uuid
import asyncio
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path

import pandas as pd
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from langchain.memory import ConversationBufferMemory

# Single consolidated import
from agents import (
    Agent, Runner, ModelSettings, ItemHelpers,
    function_tool, WebSearchTool
)

# Project-specific imports
from app.web_tool import web
from app.interpreter_tool import interpreter
from app.readme_tool import readme
from app.tavily_tool import tavily
from app.hcdt_tool import hcdt
from config.guardrail import ShareIn, ShareOut
from app.memory_tool import memory_tool
# =========================
# Configuration
# =========================
MAX_SHARE_HTML_BYTES = int(os.environ.get("MAX_SHARE_HTML_BYTES", str(5 * 1024 * 1024)))
HEARTBEAT_INTERVAL = float(os.environ.get("WS_HEARTBEAT_INTERVAL", "15.0"))
ORCHESTRATOR_MODEL_NAME = os.environ.get("ORCHESTRATOR_MODEL_NAME", "gpt-4.1-mini")
ORCHESTRATOR_TIMEOUT_SEC = float(os.environ.get("ORCHESTRATOR_TIMEOUT_SEC", "120"))
RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT", "/app/results")).resolve()
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
SAFE_BASE_URL = os.environ.get("SAFE_BASE_URL", "")
SHARE_TTL_SECONDS = int(os.environ.get("SHARE_TTL_SECONDS", "86400"))
POSTRUN_PUBLISH_TABLES = True
MAX_ROW_TO_DISPLAY = 50
SUPPORTED_TABLE_TOOLS = {"ttd", "ctd", "hcdt"}

# =========================
# Logging Setup
# =========================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("output.log", mode="a"),
    ],
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger("uvicorn.error")

# =========================
# Utility Functions
# =========================
def safe_json_parse(text: str) -> Optional[Dict]:
    """Safely parse JSON, returning None on failure."""
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_tool_key(tool_name: str) -> Optional[str]:
    """Extract standardized tool key from tool name."""
    if not tool_name:
        return None
    tool_name_lower = tool_name.lower()
    for key in SUPPORTED_TABLE_TOOLS:
        if key in tool_name_lower:
            return key
    return None


def _unescape_repr(s: str) -> str:
    """Unescape Python repr-style strings."""
    return (
        s.replace(r"\\", "\\")
         .replace(r"\'", "'")
         .replace(r"\"", '"')
         .replace(r"\n", "\n")
         .replace(r"\r", "\r")
         .replace(r"\t", "\t")
    )


def is_orchestrator_metadata(text: str, tool_name: str = None) -> bool:
    """Check if text is orchestrator metadata that should be filtered."""
    if tool_name and tool_name.lower() == "interpreter":
        return False
    
    obj = safe_json_parse(text)
    if obj and isinstance(obj, dict):
        metadata_keys = {
            "inputquery", "inputcleaned_query", "parsed_value",
            "status", "route", "tool", "messages"
        }
        if any(k in obj for k in metadata_keys):
            result_keys = {"result", "data", "table", "rows", "answer", "reasoning"}
            if "tool" in obj and not any(k in obj for k in result_keys):
                return True
            return len(obj) < 5 and "reasoning" not in obj
    
    # Fallback to string matching
    return any(
        p in text.lower()
        for p in ['inputquery', 'parsed_value', '"tool":"ttd', '"tool":"ctd', '"tool":"hcdt']
    )


def _extract_display_text(output: Any, tool_name: Optional[str] = None) -> Optional[str]:
    """Extract displayable text from tool output."""
    if output is None:
        return None

    # Dict-like structured output
    if isinstance(output, dict):
        # Check for direct message field
        if "message" in output and output["message"] is not None:
            return str(output["message"])
        
        # Check common text fields
        for key in ("output", "text", "answer", "explanation", "detail"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        
        # Check nested message fields
        for value in output.values():
            if isinstance(value, dict) and "message" in value:
                msg = value.get("message")
                if msg is not None:
                    return str(msg)

    # Convert to string for pattern matching
    text = str(output)

    # Try to parse as JSON and recurse
    if '"message"' in text or text.strip().startswith("{"):
        parsed = safe_json_parse(text)
        if parsed:
            return _extract_display_text(parsed, tool_name)

    # Try regex patterns for message extraction
    patterns = [
        r"message\s*=\s*'((?:\\'|[^'])*)'",
        r'message\s*=\s*"((?:\\"|[^"])*)"',
        r'"message"\s*:\s*"((?:\\.|[^"\\])*)"'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _unescape_repr(match.group(1))

    # Return text if not metadata
    if not is_orchestrator_metadata(text, tool_name or ""):
        return text
    
    return None


def _infer_columns_from_rows(rows: List[Dict]) -> List[str]:
    """Infer column names from list of row dictionaries."""
    columns, seen = [], set()
    for row in rows or []:
        if isinstance(row, dict):
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    columns.append(str(key))
    return columns


def _rows_to_csv(rows: List[Dict], columns: List[str]) -> str:
    """Convert rows to CSV string."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows or []:
        writer.writerow({k: row.get(k, "") for k in columns})
    return buf.getvalue()


def _build_legacy_table_payload(
    *,
    columns: List[str],
    rows: List[Dict],
    csv_text: str,
    csv_name: str,
    event_type: str,
    csv_path: Optional[str] = None,
    row_count: Optional[int] = None,
) -> Dict:
    """Build standardized table payload for publishing."""
    payload = {
        "type": event_type,
        "columns": columns,
        "rows": rows,
        "csv": csv_text,
        "csv_name": csv_name,
        "csv_path": csv_path,
    }
    if row_count is not None:
        payload["row_count"] = row_count
    return payload


# =========================
# FastAPI App
# =========================
app = FastAPI(
    title="Highly Confident Drug-Target (HCDT) Orchestrator",
    version="1.0.0",
    description=(
        "Orchestration API for structured querying and integration of the "
        "Highly Confident Drug-Target (HCDT) database, enabling evidence-based "
        "retrieval of high-confidence drug–target associations, associated "
        "diseases, mechanisms of action, and regulatory approval status."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Redis Client Management
# =========================
redis_client: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    """Return a live asyncio Redis client, recreating if necessary."""
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )
    try:
        await redis_client.ping()
        return redis_client
    except Exception as e:
        logger.warning(f"Redis reconnection needed: {e}")
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )
        await redis_client.ping()
        return redis_client


# =========================
# Startup/Shutdown
# =========================
@app.on_event("startup")
async def startup():
    """Initialize application resources."""
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        await get_redis()
        logger.info("Redis client initialized")
    except Exception as e:
        logger.error(f"Redis init failed at startup: {e}")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup application resources."""
    global redis_client
    if redis_client:
        try:
            await redis_client.close()
            logger.info("Redis client closed")
        except Exception as e:
            logger.error(f"Error closing Redis: {e}")
        finally:
            redis_client = None


# =========================
# HTTP Routes
# =========================
@app.get("/", response_class=PlainTextResponse)
async def root_ok():
    return "OK"


@app.get("/health")
async def health():
    """Health check endpoint."""
    redis_ok = False
    try:
        r = await get_redis()
        redis_ok = bool(await r.ping())
    except Exception as e:
        logger.error(f"Health check Redis error: {e}")
    
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok
    }


@app.get("/download")
def download_file(path: str = Query(...)):
    """Download a file from the results directory."""
    file_path = Path(path).resolve()
    if not str(file_path).startswith(str(RESULTS_ROOT)) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        str(file_path),
        media_type="text/csv; charset=utf-8",
        filename=file_path.name,
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )


# =========================
# Publishing Functions
# =========================
async def publish_table_records_legacy(
    connection_id: str,
    rows: List[Dict],
    *,
    columns: Optional[List[str]] = None,
    event_type: str = "ttd_table",
    csv_name: str = "results.csv",
    csv_path: Optional[str] = None,
    limit_rows: int = 1000,
    row_count: Optional[int] = None,
):
    """Publish table records via Redis pub/sub."""
    if not connection_id:
        raise ValueError("publish_table_records_legacy requires a connection_id")

    rows = rows or []
    columns = columns or _infer_columns_from_rows(rows)
    rows_view = rows[:limit_rows]
    csv_text = _rows_to_csv(rows_view, columns)
    
    payload = _build_legacy_table_payload(
        columns=columns,
        rows=rows_view,
        csv_text=csv_text,
        csv_name=csv_name,
        event_type=event_type,
        csv_path=csv_path,
        row_count=row_count,
    )
    
    r = await get_redis()
    await r.publish(connection_id, json.dumps(payload))


async def publish_table_from_output(
    *,
    output: Dict,
    tool_key: str,
    connection_id: str,
    limit_rows: int = 50,
):
    """Publish table data immediately from tool output."""
    if not isinstance(output, dict):
        return

    rows = output.get("table") or output.get("rows")
    if not isinstance(rows, list) or not rows:
        return

    csv_path = output.get("csv_path")
    row_count = output.get("row_count")
    
    columns = _infer_columns_from_rows(rows)
    rows_view = rows[:limit_rows]
    csv_text = _rows_to_csv(rows_view, columns)
    csv_name = f"{tool_key}_results_{int(time.time())}.csv"

    await publish_table_records_legacy(
        connection_id=connection_id,
        rows=rows_view,
        columns=columns,
        event_type=f"{tool_key}_table",
        csv_name=csv_name,
        csv_path=csv_path,
        limit_rows=limit_rows,
        row_count=row_count if isinstance(row_count, int) else None,
    )


# =========================
# WebSocket Orchestrator
# =========================
# =========================
# WebSocket Orchestrator
# =========================
@app.websocket("/hcdt_chat")
async def orchestrator_ws(websocket: WebSocket):
    """Main WebSocket endpoint for chat orchestration."""
    pid = os.getpid()
    connection_id = f"ws-{pid}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    # Log connection details
    try:
        ua = websocket.headers.get("user-agent", "unknown")
        client_host = getattr(websocket.client, "host", "?")
        client_port = getattr(websocket.client, "port", "?")
        logger.info(
            f"WS handshake path={websocket.url.path} conn_id={connection_id} "
            f"from {client_host}:{client_port} ua={ua}"
        )
    except Exception as e:
        logger.warning(f"Error logging connection details: {e}")

    await websocket.accept()

    # WebSocket send lock to prevent concurrent writes
    send_lock = asyncio.Lock()

    async def ws_send(payload: Any):
        """Thread-safe WebSocket send."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        async with send_lock:
            await websocket.send_text(text)

    # Send initial acknowledgment
    await ws_send({"type": "user_ack", "session_id": connection_id})

    async def stream_message_deltas(
        new_text: str,
        tool_id: str,
        tool_name: str,
        chunk_size: int = 32,
        min_delay: float = 0.05,
    ):
        """Stream text in chunks to client."""
        if not new_text:
            return
        
        offset = 0
        seq = 0
        for i in range(0, len(new_text), chunk_size):
            delta = new_text[i : i + chunk_size]
            seq += 1
            await ws_send({
                "type": "delta",
                "tool_id": tool_id,
                "name": tool_name,
                "seq": seq,
                "offset": offset,
                "text": delta,
                "final": False,
            })
            offset += len(delta)
            if min_delay > 0:
                await asyncio.sleep(min_delay)

    async def send_heartbeat():
        """Send periodic heartbeat to keep connection alive."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws_send({"type": "heartbeat", "ts": time.time()})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

    # Redis pub/sub setup
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(connection_id)

    async def relay_pubsub_events():
        """Relay Redis pub/sub messages to WebSocket client."""
        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                
                raw_payload = msg["data"]
                
                # Log table events
                parsed = safe_json_parse(raw_payload)
                if parsed and parsed.get("type") in {"ttd_table", "ctd_table", "hcdt_table"}:
                    logger.info(f"Forwarding DB event: {parsed['type']} (conn={connection_id})")
                
                await ws_send(raw_payload)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"PubSub relay error: {e}")
        finally:
            try:
                await pubsub.unsubscribe(connection_id)
                await pubsub.close()
            except Exception as e:
                logger.error(f"Error closing pubsub: {e}")

    # Start background tasks
    heartbeat_task = asyncio.create_task(send_heartbeat())
    relay_task = asyncio.create_task(relay_pubsub_events())

    # Initialize memory and agent
    memory = ConversationBufferMemory(return_messages=True)
    published_table_tools = set()

    # Load orchestrator prompt
    with open("/app/resources/prompts/agent_orchestrator.md", "r", encoding="utf-8") as f:
        prompt_md = f.read()

    orchestrator = Agent(
        name="Orchestrator",
        instructions=prompt_md,
        # tools=[web, readme, interpreter, hcdt],
        model=ORCHESTRATOR_MODEL_NAME,
        tools=[web, readme, interpreter, hcdt, WebSearchTool()],


        # tools=[memory_tool, web, readme, interpreter, hcdt, WebSearchTool()],

    )

    try:
        while True:
            # Receive message from client
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info(f"Client disconnected (conn={connection_id})")
                break

            # Handle ping/pong
            parsed_msg = safe_json_parse(raw)
            if parsed_msg and parsed_msg.get("type") == "ping":
                await ws_send({"type": "pong", "ts": time.time()})
                continue

            # Extract user input
            user_input = parsed_msg.get("user_input", raw) if parsed_msg else raw
            logger.info(f"Received input: {user_input[:200]}... (conn={connection_id})")

            # Get recent conversation history
            messages = memory.chat_memory.messages
            recent_pairs = []
            for i in range(0, min(10, len(messages)), 2):
                if i + 1 < len(messages):
                    recent_pairs.append({
                        "question": messages[i].content,
                        "answer": messages[i + 1].content
                    })
            last_5_pairs = recent_pairs[-5:]

            # Build input for orchestrator
            input_data = {
                "user_input": user_input,
                "last_5_pairs": last_5_pairs,
                "connection_id": connection_id,
            }

            # Run orchestrator
            stream = Runner.run_streamed(orchestrator, input=json.dumps(input_data))
            memory.chat_memory.add_user_message(user_input)

            # Tool tracking - ✅ ENHANCED VERSION
            tool_registry: Dict[str, Dict] = {}
            tool_counter = 0
            active_tools = set()
            timed_out = False
            last_tool_call_id: Optional[str] = None  # ✅ Track last tool for fallback
            orchestrator_text_emitted = False
            fallback_orchestrator_text: Optional[str] = None
            last_tool_text: Optional[str] = None

            def new_tool_id(runner_item_id: str, tool_name: str) -> str:
                nonlocal tool_counter
                tool_counter += 1
                return f"tool-{tool_counter}_{runner_item_id}_{tool_name.replace(' ', '_')}"

            try:
                async def _consume_stream():
                    async for event in stream.stream_events():
                        event_type = getattr(event, "type", None)

                        # Skip raw response deltas
                        if event_type == "raw_response_event":
                            continue

                        if event_type == "run_item_stream_event" and hasattr(event, "item"):
                            item = event.item
                            item_type = getattr(item, "type", None)

                            # Tool call started
                            if item_type == "tool_call_item":
                                runner_item_id = str(getattr(item, "id", uuid.uuid4().hex))
                                raw_item = getattr(item, "raw_item", {})
                                tool_name = (
                                    raw_item.get("name", "Unknown Tool")
                                    if isinstance(raw_item, dict)
                                    else getattr(raw_item, "name", "Unknown Tool")
                                )
                                tool_id = new_tool_id(runner_item_id, tool_name)

                                tool_registry[runner_item_id] = {
                                    "tool_id": tool_id,
                                    "name": tool_name,
                                    "status": "running",
                                    "sent_any": False,
                                    "output_buffer": "",
                                }
                                active_tools.add(tool_id)
                                last_tool_call_id = runner_item_id  # ✅ Track it

                                logger.debug(
                                    f"[{connection_id}] Tool call started: "
                                    f"ID={runner_item_id}, Name={tool_name}, ToolID={tool_id}"
                                )

                                await ws_send({
                                    "type": "tool_called",
                                    "tool_id": tool_id,
                                    "name": tool_name,
                                })
                                continue

                            # Tool output chunk - ✅ ENHANCED FALLBACK LOGIC
                            elif item_type == "tool_call_output_item":
                                runner_item_id = str(getattr(item, "id", ""))
                                tool_info = None

                                # Strategy 1: Match by ID (best case)
                                if runner_item_id:
                                    tool_info = tool_registry.get(runner_item_id)
                                    if tool_info:
                                        logger.debug(
                                            f"[{connection_id}] Matched output by ID: {runner_item_id}"
                                        )

                                # Strategy 2: Use last tool call (for empty/missing IDs)
                                if not tool_info and not runner_item_id and last_tool_call_id:
                                    logger.debug(
                                        f"[{connection_id}] Output has empty ID, "
                                        f"using last tool: {last_tool_call_id}"
                                    )
                                    runner_item_id = last_tool_call_id
                                    tool_info = tool_registry.get(runner_item_id)

                                # Strategy 3: Match by tool name from raw_item
                                if not tool_info:
                                    raw_item = getattr(item, "raw_item", {})
                                    output_tool_name = None

                                    if isinstance(raw_item, dict):
                                        output_tool_name = raw_item.get("name")
                                    else:
                                        output_tool_name = getattr(raw_item, "name", None)

                                    if output_tool_name:
                                        for rid, info in tool_registry.items():
                                            if info["name"] == output_tool_name and info["status"] == "running":
                                                logger.debug(
                                                    f"[{connection_id}] Matched output by name: {output_tool_name}"
                                                )
                                                tool_info = info
                                                runner_item_id = rid
                                                break

                                # Strategy 4: Fallback to any running tool (last resort)
                                if not tool_info:
                                    for rid, info in tool_registry.items():
                                        if info["status"] == "running":
                                            logger.warning(
                                                f"[{connection_id}] Using fallback - attaching output to "
                                                f"first running tool: {info['name']} (ID={rid}). "
                                                f"Original ID was: '{runner_item_id or 'empty'}'"
                                            )
                                            tool_info = info
                                            runner_item_id = rid
                                            break

                                # If still no tool found, skip this output
                                if not tool_info:
                                    logger.error(
                                        f"[{connection_id}] Cannot find tool for output. "
                                        f"ID: '{runner_item_id}', Registry: {list(tool_registry.keys())}, "
                                        f"Active: {active_tools}"
                                    )
                                    continue

                                output = getattr(item, "output", None)

                                # Filter metadata
                                if isinstance(output, str) and is_orchestrator_metadata(
                                    output, tool_info["name"]
                                ):
                                    if getattr(item, "is_final", True):
                                        tool_info["status"] = "completed"
                                        active_tools.discard(tool_info["tool_id"])
                                        await ws_send({
                                            "type": "tool_result",
                                            "tool_id": tool_info["tool_id"],
                                            "ok": False,
                                        })
                                    continue

                                # Parse JSON strings
                                if isinstance(output, str) and output.strip().startswith("{"):
                                    parsed = safe_json_parse(output)
                                    if parsed:
                                        output = parsed

                                # Extract displayable message
                                msg = _extract_display_text(output, tool_info["name"])
                                if msg:
                                    last_tool_text = msg.strip()
                                    tool_info["output_buffer"] += msg
                                    tool_info["sent_any"] = True
                                    await stream_message_deltas(
                                        new_text=msg,
                                        tool_id=tool_info["tool_id"],
                                        tool_name=tool_info["name"],
                                        chunk_size=32,
                                        min_delay=0.05,
                                    )

                                # Publish table events
                                tool_key = extract_tool_key(tool_info["name"])
                                if tool_key and isinstance(output, dict):
                                    try:
                                        await publish_table_from_output(
                                            output=output,
                                            tool_key=tool_key,
                                            connection_id=connection_id,
                                            limit_rows=MAX_ROW_TO_DISPLAY,
                                        )
                                        published_table_tools.add(tool_key)
                                    except Exception as e:
                                        logger.exception(
                                            f"Failed to publish {tool_key} table event: {e}"
                                        )

                                # Tool completion
                                if getattr(item, "is_final", True):
                                    tool_info["status"] = "completed"
                                    active_tools.discard(tool_info["tool_id"])

                                    logger.debug(
                                        f"[{connection_id}] Tool completed: "
                                        f"{tool_info['name']} (ID={runner_item_id})"
                                    )

                                    # Cleanup registry to prevent memory leak
                                    if runner_item_id in tool_registry:
                                        del tool_registry[runner_item_id]

                                    # Clear last_tool_call_id if it matches this tool
                                    if last_tool_call_id == runner_item_id:
                                        last_tool_call_id = None

                                    await ws_send({
                                        "type": "tool_result",
                                        "tool_id": tool_info["tool_id"],
                                        "ok": True,
                                    })
                                continue

                            # Final orchestrator message
                            elif item_type == "message_output_item":
                                text = ItemHelpers.text_message_output(item)
                                if text and text.strip() and not is_orchestrator_metadata(text):
                                    orchestrator_text_emitted = True
                                    memory.chat_memory.add_ai_message(text)
                                    logger.info(f"Final orchestrator message (conn={connection_id})")

                                    await stream_message_deltas(
                                        new_text=text,
                                        tool_id="orchestrator",
                                        tool_name="orchestrator",
                                        chunk_size=32,
                                        min_delay=0.05,
                                    )
                                continue

                try:
                    await asyncio.wait_for(
                        _consume_stream(),
                        timeout=ORCHESTRATOR_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    logger.error(
                        "[ws:%s] run timed out after %ss",
                        pid,
                        ORCHESTRATOR_TIMEOUT_SEC,
                        extra={"connection_id": connection_id},
                    )
                    await ws_send({
                        "type": "error",
                        "message": f"Timeout after {ORCHESTRATOR_TIMEOUT_SEC:.0f}s",
                    })
                # Post-run: publish any unpublished CSV files
                if POSTRUN_PUBLISH_TABLES and not timed_out:
                    for tool_key in SUPPORTED_TABLE_TOOLS:
                        if tool_key in published_table_tools:
                            continue
                        
                        tmp_csv = RESULTS_ROOT / f"{tool_key}_{connection_id}.csv"
                        if not tmp_csv.exists():
                            continue

                        try:
                            final_name = f"{tool_key}_results_{int(time.time())}.csv"
                            final_path = (RESULTS_ROOT / final_name).resolve()
                            
                            df = pd.read_csv(tmp_csv, dtype=str)
                            df.to_csv(final_path, index=False)

                            await publish_table_records_legacy(
                                connection_id=connection_id,
                                rows=df.head(MAX_ROW_TO_DISPLAY).to_dict(orient="records"),
                                columns=df.columns.tolist(),
                                event_type=f"{tool_key}_table",
                                csv_name=final_name,
                                csv_path=str(final_path),
                                limit_rows=MAX_ROW_TO_DISPLAY,
                                row_count=len(df),
                            )
                            
                            # Cleanup temp file
                            tmp_csv.unlink(missing_ok=True)
                            
                        except Exception as e:
                            logger.exception(f"Failed to publish CSV for {tool_key}: {e}")

                # Fallback summary: emit one final orchestrator message even if
                # the model ended without a message_output_item.
                if not timed_out and not orchestrator_text_emitted:
                    stream_final_output = getattr(stream, "final_output", None)
                    if stream_final_output is not None:
                        fallback_orchestrator_text = _extract_display_text(
                            stream_final_output, "orchestrator"
                        )
                        if (
                            not fallback_orchestrator_text
                            and isinstance(stream_final_output, str)
                            and stream_final_output.strip()
                            and not is_orchestrator_metadata(stream_final_output)
                        ):
                            fallback_orchestrator_text = stream_final_output.strip()

                    if not fallback_orchestrator_text and last_tool_text:
                        fallback_orchestrator_text = last_tool_text

                    if fallback_orchestrator_text:
                        memory.chat_memory.add_ai_message(fallback_orchestrator_text)
                        logger.info(
                            f"Emitting fallback orchestrator summary (conn={connection_id})"
                        )
                        await stream_message_deltas(
                            new_text=fallback_orchestrator_text,
                            tool_id="orchestrator",
                            tool_name="orchestrator",
                            chunk_size=32,
                            min_delay=0.05,
                        )
                        orchestrator_text_emitted = True

                # Finalize any remaining active tools
                for tool_id in list(active_tools):
                    await ws_send({
                        "type": "tool_result",
                        "tool_id": tool_id,
                        "ok": not timed_out,
                    })

                final_payload = {"type": "final"}
                if fallback_orchestrator_text:
                    final_payload["text"] = fallback_orchestrator_text
                await ws_send(final_payload)
                logger.info(f"Final message sent (conn={connection_id})")

            except WebSocketDisconnect:
                logger.info(f"Client disconnected mid-run (conn={connection_id})")
                return
            except Exception as e:
                logger.exception(f"Run error (conn={connection_id}): {e}")
                await ws_send({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected (conn={connection_id})")
    except Exception as e:
        logger.exception(f"Unhandled WS error (conn={connection_id}): {e}")
    finally:
        # Cleanup tasks
        heartbeat_task.cancel()
        relay_task.cancel()
        
        try:
            await asyncio.gather(heartbeat_task, relay_task, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error canceling tasks: {e}")
        
        try:
            await pubsub.unsubscribe(connection_id)
            await pubsub.close()
        except Exception as e:
            logger.error(f"Error closing pubsub (conn={connection_id}): {e}")


# Support both /hcdt_chat and /hcdt_chat/ to avoid trailing-slash issues
app.add_api_websocket_route("/hcdt_chat/", orchestrator_ws)
