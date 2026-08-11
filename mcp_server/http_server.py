"""HTTP / SSE / Streamable-HTTP transport for the BioChirp MCP server.

Listens on 127.0.0.1:8765; nginx terminates TLS and reverse-proxies:
  https://biochirp.iiitd.edu.in/mcp      → /streamable/  (claude.ai connector)
  https://biochirp.iiitd.edu.in/mcp/sse  → /sse          (Claude Desktop legacy)
  https://biochirp.iiitd.edu.in/mcp/messages/ → /messages/

Run:
    python -m mcp_server.http_server --host 127.0.0.1 --port 8765

Claude Desktop config (remote SSE):
    {
      "mcpServers": {
        "biochirp": {
          "type": "sse",
          "url": "https://biochirp.iiitd.edu.in/mcp/sse"
        }
      }
    }
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from mcp_server.server import server as biochirp_server

_WEB_DIR = Path(__file__).resolve().parent / "web"

# ── Optional bearer-token gate ────────────────────────────────────────────────
# Set BIOCHIRP_MCP_TOKEN to require Authorization: Bearer <token> on /sse,
# /streamable, and /messages/. Discovery + install pages stay public.
_PROTECTED_PREFIXES = ("/sse", "/streamable", "/messages/")


class _OptionalBearerAuth:
    """Pure ASGI middleware — does NOT buffer responses.

    BaseHTTPMiddleware buffers the response body stream and enforces a strict
    one-response-per-request contract via assert. StreamableHTTPSessionManager
    sends two http.response.start ASGI events (202 ACK + SSE stream) for slow
    tool calls, which trips that assert and closes the connection before the
    result is delivered. A plain ASGI middleware passes send() through
    transparently and has no such constraint.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = os.environ.get("BIOCHIRP_MCP_TOKEN", "").strip()
        if not token:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path == p or path.startswith(p) for p in _PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")

        if not auth.lower().startswith("bearer "):
            await send({
                "type": "http.response.start", "status": 401,
                "headers": [
                    [b"content-type", b"text/plain; charset=utf-8"],
                    [b"www-authenticate", b'Bearer realm="biochirp-mcp"'],
                ],
            })
            await send({"type": "http.response.body", "body": b"missing bearer token"})
            return

        import hmac as _hmac
        supplied = auth.split(" ", 1)[1].strip()
        if not _hmac.compare_digest(supplied, token):
            await send({
                "type": "http.response.start", "status": 403,
                "headers": [[b"content-type", b"text/plain; charset=utf-8"]],
            })
            await send({"type": "http.response.body", "body": b"invalid bearer token"})
            return

        await self.app(scope, receive, send)


# ── Resilience patch: suppress ClosedResourceError on client disconnect ───────
def _apply_mcp_resilience_patches() -> None:
    import logging as _logging
    _log = _logging.getLogger("biochirp.mcp.patches")
    try:
        import anyio as _anyio
        from mcp.server.lowlevel import server as _ll_server
    except Exception as e:
        _log.warning("resilience patch: import failed (%s); skipping", e)
        return

    _orig = _ll_server.Server._handle_request

    async def _safe(self, message, req, session, lifespan_context, raise_exceptions):
        try:
            await _orig(self, message, req, session, lifespan_context, raise_exceptions)
        except (_anyio.ClosedResourceError, _anyio.BrokenResourceError) as e:
            _log.info(
                "client stream closed before response (req_type=%s, id=%s): %s — suppressed",
                type(req).__name__, getattr(message, "request_id", "?"), e,
            )

    _ll_server.Server._handle_request = _safe


_apply_mcp_resilience_patches()

# ── SSE transport ─────────────────────────────────────────────────────────────
# MCP_PUBLIC_PREFIX must match the nginx path-prefix so the SSE handshake
# tells clients to POST to /mcp/messages/ (not the bare /messages/).
_PUBLIC_PREFIX = os.environ.get("MCP_PUBLIC_PREFIX", "/mcp").rstrip("/")
_MESSAGES_PATH = f"{_PUBLIC_PREFIX}/messages/"
sse = SseServerTransport(_MESSAGES_PATH)


async def handle_sse(request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await biochirp_server.run(
            streams[0], streams[1],
            biochirp_server.create_initialization_options(),
        )
    return Response()


# ── Streamable-HTTP transport (claude.ai connector) ───────────────────────────
_streamable_session_manager = StreamableHTTPSessionManager(
    app=biochirp_server,
    stateless=False,
)


async def handle_streamable_http(scope, receive, send):
    await _streamable_session_manager.handle_request(scope, receive, send)


@asynccontextmanager
async def lifespan(_app):
    async with _streamable_session_manager.run():
        yield


# ── Static routes ─────────────────────────────────────────────────────────────

async def health(_request):
    return Response(
        content=b'{"status":"ok","service":"biochirp-mcp","version":"2.0.0"}',
        media_type="application/json",
    )


async def install_page(_request):
    f = _WEB_DIR / "install.html"
    if not f.exists():
        return Response("install.html not found", status_code=404)
    return FileResponse(str(f), media_type="text/html; charset=utf-8")


async def manifest(_request):
    f = _WEB_DIR / "manifest.json"
    if not f.exists():
        return JSONResponse({"error": "manifest.json not found"}, status_code=404)
    return FileResponse(str(f), media_type="application/json")


async def view_csv_page(_request):
    f = _WEB_DIR / "view.html"
    if not f.exists():
        return Response("view.html not found", status_code=404)
    return FileResponse(str(f), media_type="text/html; charset=utf-8")


async def well_known_mcp(_request):
    return JSONResponse({
        "name": "biochirp",
        "version": "2.1.0",
        "transports": [
            {"type": "streamable-http", "url": "https://biochirp.iiitd.edu.in/mcp"},
            {"type": "sse",             "url": "https://biochirp.iiitd.edu.in/mcp/sse"},
        ],
        "manifest":     "/mcp/manifest.json",
        "install_page": "/connector",
    })


async def csv_proxy(request):
    """Proxy CSV downloads from per-DB containers through the public MCP endpoint.

    URL pattern (nginx strips /mcp prefix):
        GET /csv/{db}/{filename}
    Proxies to:
        http://localhost:{port}/download?path=/app/results/{filename}
    """
    from mcp_server.server import _DB_CATALOGUE
    db = request.path_params.get("db", "")
    filename = request.path_params.get("filename", "")
    if not db or not filename or db not in _DB_CATALOGUE:
        return Response(f"Unknown database: {db!r}", status_code=404)
    if not filename.endswith(".csv") or "/" in filename or ".." in filename:
        return Response("Invalid filename", status_code=400)
    port = _DB_CATALOGUE[db]["port"]
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"http://localhost:{port}/download",
                params={"path": f"/app/results/{filename}"},
            )
        if resp.status_code == 200:
            return Response(
                content=resp.content,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return Response(f"CSV not found on {db} service (status {resp.status_code})",
                        status_code=resp.status_code)
    except Exception as exc:
        return Response(f"CSV proxy error: {exc}", status_code=502)


# ── Starlette app ─────────────────────────────────────────────────────────────

_starlette = Starlette(
    debug=False,
    lifespan=lifespan,
    routes=[
        # MCP transports
        Route("/health",    health,    methods=["GET"]),
        Route("/sse",       handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
        Mount("/streamable", app=handle_streamable_http),
        # Discovery + install
        Route("/connector",            install_page,   methods=["GET"]),
        Route("/connector/",           install_page,   methods=["GET"]),
        Route("/connector/install.html", install_page, methods=["GET"]),
        Route("/mcp/manifest.json",    manifest,       methods=["GET"]),
        Route("/.well-known/mcp",      well_known_mcp, methods=["GET"]),
        Route("/view.html",            view_csv_page,  methods=["GET"]),
        Route("/connector/view.html",  view_csv_page,  methods=["GET"]),
        # CSV proxy — serves per-DB result CSVs through the public endpoint
        Route("/csv/{db}/{filename}",  csv_proxy,      methods=["GET"]),
    ],
)

# Wrap with pure ASGI auth middleware (NOT BaseHTTPMiddleware) so the
# StreamableHTTPSessionManager's double http.response.start sequence is
# never buffered/asserted on.
app = _OptionalBearerAuth(_starlette)


def main() -> None:
    p = argparse.ArgumentParser(description="BioChirp MCP HTTP server")
    p.add_argument("--host", default=os.environ.get("BIOCHIRP_MCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("BIOCHIRP_MCP_PORT", "8765")))
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()
    import uvicorn
    uvicorn.run(
        "mcp_server.http_server:app",
        host=args.host, port=args.port, reload=args.reload,
    )


if __name__ == "__main__":
    main()
