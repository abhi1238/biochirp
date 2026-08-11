"""BioChirp share service.

Serves the two standalone routes the frontend's ★ Share button, per-turn
share icon, and "copy BibTeX" action need: `POST /share` stores an HTML
snapshot in Redis and returns a short-lived link; `GET /s/{id}` renders it
back read-only. This logic used to live inside the decommissioned bio_chat
orchestrator (port 8030, removed 2026-06-18) — extracted here as its own
lean service so the share feature works without reviving the rest of that
28-DB orchestrator.
"""
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Optional

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from utils.service_setup import add_open_cors, add_health_endpoint
from utils.logging_setup import setup_logging

setup_logging(stream=sys.stdout)
logger = logging.getLogger("uvicorn.error")

REDIS_HOST = os.environ.get("REDIS_HOST", "biochirp_redis_tool")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
MAX_SHARE_HTML_BYTES = int(os.environ.get("MAX_SHARE_HTML_BYTES", str(5 * 1024 * 1024)))  # 5MB
SHARE_TTL_SECONDS = int(os.environ.get("SHARE_TTL_SECONDS", "86400"))  # 24h default
SAFE_BASE_URL = os.environ.get("SAFE_BASE_URL", "")


class ShareIn(BaseModel):
    html: str
    title: Optional[str] = "BioChirp Chat"
    # When true, the snapshot HTML is stored verbatim (no <script>/onclick
    # stripping) and served inside a sandboxed iframe srcdoc by /s/{id}.
    # Required for interactive snapshots that need to re-run their inline
    # bootstrap (collapse panels, table rendering, CSV downloads). The
    # frontend share button sets this to true.
    unsafe: bool = False


class ShareOut(BaseModel):
    id: str
    url: str
    expires_in_seconds: int


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
    html = re.sub(
        r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>",
        "",
        html,
        flags=re.I | re.S,
    )
    html = re.sub(
        r"\son[a-zA-Z]+\s*=\s*([\"']).*?\1",
        "",
        html,
        flags=re.I | re.S,
    )
    return html


app = FastAPI(
    title="BioChirp Share Service",
    version="1.0.0",
    description="Stores and serves read-only shared BioChirp chat snapshots.",
)
add_open_cors(app)
add_health_endpoint(app)


@app.get("/")
def root():
    return {"message": "Share service is running"}


# ---------- Lazy, robust Redis ----------
_redis_client: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    """Return a live asyncio Redis client. Recreates on first use / reconnect."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        await _redis_client.ping()
        return _redis_client
    except Exception:
        _redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        await _redis_client.ping()
        return _redis_client


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
        unsafe = bool(payload.unsafe)
        body = payload.html if unsafe else _sanitize_html_for_storage(payload.html)

        share_id = _new_share_id(payload.title)
        key = f"share:{share_id}"
        blob = json.dumps({"unsafe": unsafe, "html": body})

        ok = await r.setex(key, SHARE_TTL_SECONDS, blob)
        if not ok:
            logger.error("Redis setex returned falsy for key=%s", key)
            raise HTTPException(status_code=500, detail="Failed to persist snapshot.")

        url = f"{SAFE_BASE_URL}/s/{share_id}" if SAFE_BASE_URL else f"/s/{share_id}"
        logger.info(
            "Share created id=%s size=%dB ip=%s ua=%s",
            share_id,
            len(blob.encode("utf-8")),
            request.client.host if request.client else "?",
            request.headers.get("user-agent"),
        )
        return ShareOut(id=share_id, url=url, expires_in_seconds=SHARE_TTL_SECONDS)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled /share error")
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
