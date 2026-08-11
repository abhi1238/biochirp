"""Shared logging initialisation for BioChirp support services.

Each support service (`app/tools/<name>/app/main.py` for fuzzy, tavily,
readme, llm_member_filter, expand_synonyms,
expand_synonyms_unrestricted) used to repeat the same ~10-line basicConfig
+ httpx/httpcore silencing block. Call `setup_logging()` once at module
load to apply the canonical config and silence the chatty HTTP libs.
"""
from __future__ import annotations

import logging

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: int = logging.INFO,
    fmt: str = LOG_FORMAT,
    stream=None,
) -> None:
    """Apply BioChirp's canonical logging config + silence noisy deps.

    `semantic_filter` uses a slightly different format
    ('%(asctime)s - %(name)s - %(levelname)s - %(message)s'); pass it via
    `fmt=` to override. `fuzzy` pins to `sys.stdout`; pass `stream=sys.stdout`
    to match.
    """
    kwargs: dict = {"level": level, "format": fmt, "datefmt": DATE_FORMAT}
    if stream is not None:
        kwargs["stream"] = stream
    logging.basicConfig(**kwargs)
    # httpx and httpcore log every request at INFO; that drowns out the
    # actually-interesting per-request lines from this service.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


__all__ = ["setup_logging"]
