

import os
import sys
import re
import io
import csv
import json
import time
import uuid
import asyncio
import hashlib
import logging
from typing import Optional
from contextlib import suppress
from pathlib import Path

import pandas as pd
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from langchain.memory import ConversationBufferMemory

from agents import Agent, Runner, ModelSettings, ItemHelpers

# ----- Project-specific imports -----
from app.web_tool import web
from app.interpreter_tool import interpreter
from app.readme_tool import readme
from app.tavily_tool import tavily
from app.ttd_tool import ttd
from app.ctd_tool import ctd
from app.hcdt_tool import hcdt
from app.memory_tool import memory_tool
from config.guardrail import ShareIn, ShareOut


MAX_SHARE_HTML_BYTES = int(os.environ.get("MAX_SHARE_HTML_BYTES", str(5 * 1024 * 1024)))  # 5MB
HEARTBEAT_INTERVAL = float(os.environ.get("WS_HEARTBEAT_INTERVAL", "15.0"))

# =========================
# Basic helpers
# =========================
def _new_share_id(raw_hint: Optional[str] = None) -> str:
    seed = f"{time.time()}:{uuid.uuid4().hex}:{raw_hint or ''}".encode()
    return hashlib.sha1(seed).hexdigest()[:10]


def _esc_srcdoc(html: str) -> str:
    return (
        html.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("'", "&#39;")
    )


def _sanitize_html_for_storage(html: str) -> str:
    # Strip <script> blocks
    html = re.sub(
        r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>",
        "",
        html,
        flags=re.I | re.S,
    )
    # Strip inline event handlers: onclick=..., onload=..., etc.
    html = re.sub(
        r"\son[a-zA-Z]+\s*=\s*([\"']).*?\1",
        "",
        html,
        flags=re.I | re.S,
    )
    return html


def _infer_columns_from_rows(rows):
    cols, seen = [], set()
    for r in rows or []:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(str(k))
    return cols


def _rows_to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows or []:
        w.writerow({k: r.get(k, "") for k in columns})
    return buf.getvalue()


def _build_legacy_table_payload(
    *,
    columns,
    rows,
    csv_text,
    csv_name,
    event_type,
    csv_path=None,
    row_count=None,
):
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
# App & Config
# =========================
app = FastAPI(
    title="Orchestrator Service",
    version="1.0.0",
    description="API for Orchestrator Service",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT", "/app/results")).resolve()
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")  # default to Docker service name, not localhost
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
SAFE_BASE_URL = os.environ.get("SAFE_BASE_URL", "")
SHARE_TTL_SECONDS = int(os.environ.get("SHARE_TTL_SECONDS", "86400"))  # 24h default
POSTRUN_PUBLISH_TABLES = True

with open("/app/resources/prompts/agent_orchestrator.md", "r", encoding="utf-8") as f:
    prompt_md = f.read()

class ConnectionIdFilter(logging.Filter):
    """Ensure every log record has a connection_id attribute."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "connection_id"):
            record.connection_id = "-"
        return True


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

# class Tee(object):
#     def __init__(self, *files):
#         self.files = files
#     def write(self, obj):
#         for f in self.files:
#             f.write(obj)
#             f.flush()  # If you want real-time writing
#     def flush(self):
#         for f in self.files:
#             f.flush()

# log_file = open("output.log", "a")
# tee = Tee(sys.stdout, log_file)
# sys.stdout = tee
# sys.stderr = tee


logger = logging.getLogger("uvicorn.error")
# logger.setLevel(logging.INFO)  # Or logging.DEBUG for more details

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)

# # Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)

# if not logger.handlers:
#     formatter = logging.Formatter(
#         "%(asctime)s [%(levelname)s] [cid=%(connection_id)s] %(name)s: %(message)s"
#     )

#     file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
#     file_handler.setFormatter(formatter)
#     file_handler.addFilter(ConnectionIdFilter())

#     console_handler = logging.StreamHandler()
#     console_handler.setFormatter(formatter)
#     console_handler.addFilter(ConnectionIdFilter())

#     logger.addHandler(file_handler)
#     logger.addHandler(console_handler)

#     root_logger = logging.getLogger()
#     root_logger.setLevel(logging.INFO)

#     # Avoid adding duplicate handlers if this code runs multiple times
#     if not any(
#         isinstance(h, logging.FileHandler)
#         and getattr(h, "baseFilename", None) == str(LOG_FILE)
#         for h in root_logger.handlers
#     ):
#         root_logger.addHandler(file_handler)

# logger.info(
#     "Logging initialized; writing to %s",
#     LOG_FILE,
#     extra={"connection_id": "startup"},
# )

# ---------- Lazy, robust Redis ----------
redis_client: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    """Return a live asyncio Redis client. Recreates on first use / reconnect."""
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
    except Exception:
        # Recreate on dropped connection or init failure
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )
        await redis_client.ping()
        return redis_client


@app.on_event("startup")
async def startup():
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        await get_redis()
        logger.info("Redis client initialized", extra={"connection_id": "startup"})
    except Exception as e:
        logger.error("Redis init failed at startup: %s", e, extra={"connection_id": "startup"})


@app.on_event("shutdown")
async def shutdown():
    try:
        r = await get_redis()
        await r.close()
        logger.info("Redis client closed", extra={"connection_id": "shutdown"})
    except Exception:
        pass


# =========================
# Routes
# =========================
@app.get("/", response_class=PlainTextResponse)
async def root_ok():
    return "OK"


@app.get("/health")
async def health():
    ok = True
    try:
        r = await get_redis()
        ok = bool(await r.ping())
    except Exception:
        ok = False
    return {"status": "ok" if ok else "degraded", "redis": ok}


@app.get("/download")
def download_file(path: str = Query(...)):
    p = Path(path).resolve()
    if not str(p).startswith(str(RESULTS_ROOT)) or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        str(p),
        media_type="text/csv; charset=utf-8",
        filename=p.name,
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@app.post("/share", response_model=ShareOut)
async def create_share(payload: ShareIn, request: Request):
    try:
        if not payload.html or len(payload.html) < 100:
            raise HTTPException(status_code=400, detail="Snapshot HTML is too short.")

        html_bytes = payload.html.encode("utf-8", errors="ignore")
        if len(html_bytes) > MAX_SHARE_HTML_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Snapshot too large ({len(html_bytes)} bytes). "
                    "Increase client_max_body_size / MAX_SHARE_HTML_BYTES."
                ),
            )

        r = await get_redis()

        # Tolerate older ShareIn without .unsafe
        unsafe = bool(getattr(payload, "unsafe", False))

        # Sanitize unless explicitly unsafe
        body = payload.html if unsafe else _sanitize_html_for_storage(payload.html)

        share_id = _new_share_id(getattr(payload, "title", None))
        key = f"share:{share_id}"
        blob = json.dumps({"unsafe": unsafe, "html": body})

        ok = await r.setex(key, SHARE_TTL_SECONDS, blob)
        if not ok:
            logger.error(
                "Redis setex returned falsy for key=%s", key, extra={"connection_id": "share"}
            )
            raise HTTPException(status_code=500, detail="Failed to persist snapshot.")

        url = f"{SAFE_BASE_URL}/s/{share_id}" if SAFE_BASE_URL else f"/s/{share_id}"
        logger.info(
            "Share created id=%s size=%dB ip=%s ua=%s",
            share_id,
            len(blob.encode("utf-8")),
            request.client.host if request.client else "?",
            request.headers.get("user-agent"),
            extra={"connection_id": share_id},
        )
        return ShareOut(id=share_id, url=url, expires_in_seconds=SHARE_TTL_SECONDS)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled /share error", extra={"connection_id": "share"})
        raise HTTPException(
            status_code=500,
            detail=f"Share failed: {type(e).__name__}: {e}",
        )


@app.get("/s/{share_id}", response_class=HTMLResponse)
async def get_share(share_id: str):
    r = await get_redis()
    raw = await r.get(f"share:{share_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Snapshot not found or expired.")
    try:
        stored = json.loads(raw)
    except Exception:
        stored = {"unsafe": False, "html": raw}
    html = stored.get("html", "")
    unsafe = bool(stored.get("unsafe"))
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if not unsafe:
        return HTMLResponse(
            content=html,
            media_type="text/html; charset=utf-8",
            headers=headers,
        )
    viewer = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared snapshot</title>
<style>html,body,iframe{{margin:0;padding:0;height:100%;width:100%}}body{{background:#0B1222}}</style>
</head>
<body>
<iframe sandbox="allow-scripts allow-same-origin allow-downloads allow-popups allow-popups-to-escape-sandbox"
        srcdoc='{_esc_srcdoc(html)}'></iframe>
</body></html>"""
    return HTMLResponse(
        content=viewer,
        media_type="text/html; charset=utf-8",
        headers=headers,
    )


# =========================
# Orchestrator metadata filter
# =========================
def is_orchestrator_metadata(text: str, tool_name: str = None) -> bool:
    if tool_name and tool_name.lower() == "interpreter":
        return False
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            keys = {
                "inputquery",
                "inputcleaned_query",
                "parsed_value",
                "status",
                "route",
                "tool",
                "messages",
            }
            if any(k in obj for k in keys):
                if "tool" in obj and not any(
                    k in obj for k in ["result", "data", "table", "rows", "answer", "reasoning"]
                ):
                    return True
                # Heuristic: small dict with no reasoning keys is likely metadata
                return len(obj) < 5 and "reasoning" not in obj
        return False
    except json.JSONDecodeError:
        return any(
            p in text.lower()
            for p in ['inputquery', 'parsed_value', '"tool":"ttd', '"tool":"ctd', '"tool":"hcdt']
        )


# =========================
# Escape-aware extractors
# =========================
def _unescape_repr(s: str) -> str:
    return (
        s.replace(r"\\", "\\")
         .replace(r"\'", "'")
         .replace(r"\"", '"')
         .replace(r"\n", "\n")
         .replace(r"\r", "\r")
         .replace(r"\t", "\t")
    )


def _extract_display_text(output, tool_name: Optional[str] = None) -> Optional[str]:
    if output is None:
        return None

    # Dict-like structured output
    if isinstance(output, dict):
        if "message" in output and output["message"] is not None:
            return str(output["message"])
        for k in ("output", "text", "answer", "explanation", "detail"):
            v = output.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for k, v in output.items():
            if isinstance(v, dict) and "message" in v:
                mv = v.get("message")
                if mv is not None:
                    return str(mv)

    # Fallback: stringify
    s = str(output)

    # If it looks like JSON, try to parse then recurse
    if '"message"' in s or s.strip().startswith("{"):
        try:
            j = json.loads(s)
            return _extract_display_text(j, tool_name)
        except Exception:
            pass

    # Try common repr patterns: message='...' or "message"="..."
    m = re.search(r"message\s*=\s*'((?:\\'|[^'])*)'", s)
    if not m:
        m = re.search(r'message\s*=\s*"((?:\\"|[^"])*)"', s)
    if m:
        return _unescape_repr(m.group(1))

    # Try JSON-style "message": "..."
    m = re.search(r'"message"\s*:\s*"((?:\\.|[^"\\])*)"', s)
    if m:
        return _unescape_repr(m.group(1))

    # If not detected as metadata, show as-is
    if not is_orchestrator_metadata(s, tool_name or ""):
        return s
    return None


# =========================
# Pub/Sub table publisher
# =========================
async def publish_table_records_legacy(
    connection_id: str,
    rows,
    *,
    columns=None,
    event_type: str = "ttd_table",  # "ctd_table" / "hcdt_table"
    csv_name: str = "results.csv",
    csv_path: str = None,
    limit_rows: int = 1000,
    row_count: int = None,
):
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


# =========================
# Immediate publish from tool output (for ttd/ctd/hcdt)
# =========================
SUPPORTED_TABLE_TOOLS = {"ttd", "ctd", "hcdt"}


async def publish_table_from_output(
    *,
    output: dict,
    tool_key: str,
    connection_id: str,
    limit_rows: int = 50,
):
    rows = None
    if isinstance(output, dict):
        rows = output.get("table") or output.get("rows")

    if not isinstance(rows, list) or not rows:
        return

    csv_path = None
    row_count = None
    if isinstance(output, dict):
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
@app.websocket("/chat")
async def orchestrator_ws(websocket: WebSocket):
    MAX_ROW_TO_DISPLAY = 50

    pid = os.getpid()
    connection_id = f"ws-{pid}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    # Optional: per-connection log file
    # per_conn_log_path = LOG_DIR / f"ws_{connection_id}.log"
    # per_conn_handler = logging.FileHandler(per_conn_log_path, encoding="utf-8")
    # per_conn_handler.setFormatter(
    #     logging.Formatter(
    #         "%(asctime)s [%(levelname)s] [cid=%(connection_id)s] %(name)s: %(message)s"
    #     )
    # )
    # per_conn_handler.addFilter(ConnectionIdFilter())
    # logger.addHandler(per_conn_handler)

    # Log handshake info
    try:
        ua = websocket.headers.get("user-agent")
        logger.info(
            "WS handshake path=%s conn_id=%s from %s:%s ua=%s",
            websocket.url.path,
            connection_id,
            getattr(websocket.client, "host", "?"),
            getattr(websocket.client, "port", "?"),
            ua,
            extra={"connection_id": connection_id},
        )
    except Exception:
        pass

    await websocket.accept()

    # Single writer guard for WebSocket
    send_lock = asyncio.Lock()

    async def ws_send(payload):
        """Send JSON or raw string with a single writer lock."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        async with send_lock:
            await websocket.send_text(text)

    # Initial ack to client
    await ws_send({"type": "user_ack", "session_id": connection_id})

    async def stream_message_deltas(
        new_text: str,
        tool_id: str,
        tool_name: str,
        chunk_size: int = 8,
        min_delay: float = 0.05,
    ):
        if not new_text:
            return
        offset = 0
        seq = 0
        for i in range(0, len(new_text), chunk_size):
            delta = new_text[i : i + chunk_size]
            seq += 1
            await ws_send(
                {
                    "type": "delta",
                    "tool_id": tool_id,
                    "name": tool_name,
                    "seq": seq,
                    "offset": offset,
                    "text": delta,
                    "final": False,
                }
            )
            offset += len(delta)
            if min_delay > 0:
                await asyncio.sleep(min_delay)

    async def send_heartbeat():
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws_send({"type": "heartbeat", "ts": time.time()})
            except Exception:
                break

    # Pub/Sub relay uses a live client
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(connection_id)

    async def relay_pubsub_events():
        try:
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                raw_payload = msg["data"]
                try:
                    parsed = json.loads(raw_payload)
                    if parsed.get("type") in {"ttd_table", "ctd_table", "hcdt_table"}:
                        logger.info(
                            "Forwarding DB event: %s (conn=%s)",
                            parsed["type"],
                            connection_id,
                            extra={"connection_id": connection_id},
                        )
                except Exception:
                    pass
                await ws_send(raw_payload)
        except Exception as e:
            logger.error("PubSub error: %s", e, extra={"connection_id": connection_id})
        finally:
            with suppress(Exception):
                await pubsub.unsubscribe(connection_id)
                await pubsub.close()

    heartbeat_task = asyncio.create_task(send_heartbeat())
    relay_task = asyncio.create_task(relay_pubsub_events())

    memory = ConversationBufferMemory(return_messages=True)
    published_table_tools = set()

    # One Agent per WebSocket connection
    orchestrator = Agent(
        name="Orchestrator",
        instructions=prompt_md,
        tools=[memory_tool, web, readme, interpreter, tavily, ttd, ctd, hcdt],
        model="gpt-4o-mini",
        model_settings=ModelSettings(tool_choice="memory_tool"),
    )

    try:
        while True:
            # Receive a message from client
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info(
                    "[ws:%s] client disconnected via receive_text",
                    pid,
                    extra={"connection_id": connection_id},
                )
                break

            # Handle ping / normal user_input
            try:
                payload = json.loads(raw)
                if payload.get("type") == "ping":
                    await ws_send({"type": "pong", "ts": time.time()})
                    continue
                user_input = payload.get("user_input", raw)
            except json.JSONDecodeError:
                user_input = raw

            logger.info(
                "[ws:%s] <<< %s",
                pid,
                user_input[:200],
                extra={"connection_id": connection_id},
            )

            # Last 5 Q/A pairs from memory
            messages = memory.chat_memory.messages
            pairs = []
            for i in range(0, min(10, len(messages)), 2):
                if i + 1 < len(messages):
                    pairs.append(
                        {"question": messages[i].content, "answer": messages[i + 1].content}
                    )
            last5_pairs = pairs[-5:]
            await ws_send({"last_5_pairs": last5_pairs})

            # Build input for orchestrator
            # (kept as string to match existing Runner.run_streamed behavior)
            input_data = (
                f"user_input: {user_input} | "
                f"last_5_pairs: {last5_pairs} | "
                f"connection_id: {connection_id}"
            )

            stream = Runner.run_streamed(orchestrator, input=input_data)
            memory.chat_memory.add_user_message(user_input)

            tool_registry = {}
            tool_counter = 0
            active_tools = set()

            def new_tool_id(runner_item_id: str, tool_name: str) -> str:
                nonlocal tool_counter
                tool_counter += 1
                return f"tool-{tool_counter}_{runner_item_id}_{tool_name.replace(' ', '_')}"

            try:
                async for event in stream.stream_events():
                    etype = getattr(event, "type", None)

                    # Skip raw delta events from model, we only care about tool + final messages
                    if (
                        etype == "raw_response_event"
                        and hasattr(event, "data")
                        and hasattr(event.data, "delta")
                    ):
                        continue

                    if etype == "run_item_stream_event" and hasattr(event, "item"):
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
                            await ws_send(
                                {
                                    "type": "tool_called",
                                    "tool_id": tool_id,
                                    "name": tool_name,
                                }
                            )
                            continue

                        # Tool output chunk
                        elif item_type == "tool_call_output_item":
                            runner_item_id = str(getattr(item, "id", ""))
                            tool_info = tool_registry.get(runner_item_id)

                            # Fallback: if we didn't find by ID, attach to any active tool
                            if not tool_info and active_tools:
                                for rid, info in tool_registry.items():
                                    if info["tool_id"] in active_tools:
                                        tool_info = info
                                        runner_item_id = rid
                                        break
                            if not tool_info:
                                continue

                            output = getattr(item, "output", None)

                            # Drop pure metadata
                            if isinstance(output, str) and is_orchestrator_metadata(
                                output, tool_info["name"]
                            ):
                                if getattr(item, "is_final", True):
                                    tool_info["status"] = "completed"
                                    active_tools.discard(tool_info["tool_id"])
                                    await ws_send(
                                        {
                                            "type": "tool_result",
                                            "tool_id": tool_info["tool_id"],
                                            "ok": False,
                                        }
                                    )
                                continue

                            # JSON-string outputs → parse
                            if isinstance(output, str) and output.strip().startswith("{"):
                                try:
                                    output = json.loads(output)
                                except json.JSONDecodeError:
                                    pass

                            msg = _extract_display_text(output, tool_info["name"])
                            if msg:
                                tool_info["output_buffer"] += msg
                                tool_info["sent_any"] = True
                                await stream_message_deltas(
                                    new_text=msg,
                                    tool_id=tool_info["tool_id"],
                                    tool_name=tool_info["name"],
                                    chunk_size=8,
                                    min_delay=0.05,
                                )

                            # Publish table events if applicable
                            try:
                                tool_key = (tool_info["name"] or "").strip().lower()
                                if tool_key in SUPPORTED_TABLE_TOOLS and isinstance(output, dict):
                                    await publish_table_from_output(
                                        output=output,
                                        tool_key=tool_key,
                                        connection_id=connection_id,
                                        limit_rows=MAX_ROW_TO_DISPLAY,
                                    )
                                    published_table_tools.add(tool_key)
                            except Exception as _pub_ex:
                                logger.exception(
                                    "Failed to publish %s table event: %s",
                                    tool_info.get("name"),
                                    _pub_ex,
                                    extra={"connection_id": connection_id},
                                )

                            # Finalization for this tool
                            if getattr(item, "is_final", True):
                                tool_info["status"] = "completed"
                                active_tools.discard(tool_info["tool_id"])
                                await ws_send(
                                    {
                                        "type": "tool_result",
                                        "tool_id": tool_info["tool_id"],
                                        "ok": True,
                                    }
                                )
                            continue

                        # Final orchestrator message
                        elif item_type == "message_output_item":
                            text = ItemHelpers.text_message_output(item)
                            if text and text.strip() and not is_orchestrator_metadata(text):
                                memory.chat_memory.add_ai_message(text)
                                logger.info(
                                "[ws:%s] Final orchestrator message: %r",
                                pid,
                                text,
                                extra={"connection_id": connection_id},
                            )

                                await stream_message_deltas(
                                    new_text=text,
                                    tool_id="orchestrator",
                                    tool_name="orchestrator",
                                    chunk_size=8,
                                    min_delay=0.05,
                                )
                            continue

                # After the run: check for CSVs for any DB we didn't publish yet
                if POSTRUN_PUBLISH_TABLES:
                    try:
                        for which in ("ttd", "ctd", "hcdt"):
                            if which in published_table_tools:
                                continue
                            tmp_csv = RESULTS_ROOT / f"{which}_{connection_id}.csv"
                            if not tmp_csv.exists():
                                continue

                            final_name = f"{which}_results_{int(time.time())}.csv"
                            final_path = (RESULTS_ROOT / final_name).resolve()
                            df = pd.read_csv(tmp_csv, dtype=str)
                            df.to_csv(final_path, index=False)

                            await publish_table_records_legacy(
                                connection_id=connection_id,
                                rows=df.head(MAX_ROW_TO_DISPLAY).to_dict(orient="records"),
                                columns=df.columns.tolist(),
                                event_type=f"{which}_table",
                                csv_name=final_name,
                                csv_path=str(final_path),
                                limit_rows=MAX_ROW_TO_DISPLAY,
                                row_count=int(getattr(df, "shape", (0, 0))[0]),
                            )
                    except Exception as ex:
                        logger.exception(
                            "Failed to publish csv_path events: %s",
                            ex,
                            extra={"connection_id": connection_id},
                        )

                # Ensure all active tools get a final tool_result
                for tool_id in list(active_tools):
                    await ws_send(
                        {
                            "type": "tool_result",
                            "tool_id": tool_id,
                            "ok": True,
                        }
                    )

                await ws_send({"type": "final"})
                logger.info(
                        "[ws:%s] Final message sent to client.",
                        pid,
                        extra={"connection_id": connection_id},
                    )

            except WebSocketDisconnect:
                logger.info(
                    "[ws:%s] client disconnected mid-run",
                    pid,
                    extra={"connection_id": connection_id},
                )
                return
            except Exception as e:
                logger.exception(
                    "[ws:%s] run error: %s",
                    pid,
                    e,
                    extra={"connection_id": connection_id},
                )
                await ws_send({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        logger.info("[ws] client disconnected", extra={"connection_id": connection_id})
    except Exception as e:
        logger.exception(
            "[ws] unhandled error: %s", e, extra={"connection_id": connection_id}
        )
    finally:
        # Stop background tasks
        with suppress(Exception):
            relay_task.cancel()
        with suppress(Exception):
            heartbeat_task.cancel()
        # Close pubsub
        with suppress(Exception):
            await pubsub.unsubscribe(connection_id)
            await pubsub.close()


# Accept *both* /chat and /chat/ to avoid trailing-slash 403s on WS handshakes.
app.add_api_websocket_route("/chat/", orchestrator_ws)