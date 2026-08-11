"""Shared FastAPI boilerplate for non-DB tool services and chat services.

Every non-DB tool service (`app/tools/{planner,fuzzy,expand_synonyms,...}/app/main.py`)
and every standalone chat service (`opentarget_service`) was repeating the
same three blocks:

  1. logging.basicConfig + httpx/httpcore WARNING setters
  2. app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)
  3. @app.get("/health") -> {"status": "OK"}

This module collapses them to three one-line calls. The per-DB tool services
already use the higher-level `app.per_db_tool.build_app()` factory which
calls these helpers internally.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI



def add_open_cors(app: "FastAPI", *, allow_credentials: bool = False) -> None:
    """Apply the project's open-CORS middleware to `app`.

    Tool services use `allow_credentials=False`; chat services that rely on
    cookies use `allow_credentials=True`.
    """
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def add_health_endpoint(app: "FastAPI") -> None:
    """Register the standard `GET /health -> {"status": "OK"}` route."""
    @app.get("/health")
    async def health():  # noqa: D401, F811
        return {"status": "OK"}


def add_download_endpoint(app: "FastAPI") -> None:
    """Register `GET /download?path=<abs-path>` that serves result CSVs.

    Only paths that resolve under RESULTS_ROOT (default /app/results) are
    served; anything outside returns 403.
    """
    import os
    from pathlib import Path
    from fastapi import HTTPException, Query
    from fastapi.responses import PlainTextResponse

    @app.get("/download", response_class=PlainTextResponse)
    async def download(path: str = Query(..., description="Absolute path to the result CSV")):
        results_root = Path(os.environ.get("RESULTS_ROOT", "/app/results")).resolve()
        requested = Path(path).resolve()
        if results_root not in requested.parents and requested != results_root:
            raise HTTPException(status_code=403, detail="Path outside results root")
        if not requested.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return requested.read_text()
