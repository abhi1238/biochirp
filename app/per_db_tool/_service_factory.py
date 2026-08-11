"""One-call initialiser for the standard per-DB service globals."""
from __future__ import annotations

import os

from dotenv import load_dotenv

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from utils.dataframe_loader import ttl_cached_db
from utils.summarizer_prompt_builder import build_summarizer_prompt


def setup_service_globals(db_short: str, display_name: str, loader_fn):
    """Initialise the five standard module-level globals for a per-DB service.

    Returns ``(SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_db)``.
    Replaces the 6-line boilerplate duplicated across every service file::

        SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_ttd_db = \\
            setup_service_globals("ttd", "TTD", return_preprocessed_ttd)
    """
    load_dotenv(override=True)
    service_name = os.getenv("SERVICE_NAME", db_short)
    summarizer_model = settings.SUMMARIZER_MODEL_NAME
    prompt_md = build_summarizer_prompt(db_short)
    get_db = ttl_cached_db(db_short, loader_fn)
    return service_name, display_name, summarizer_model, prompt_md, get_db


__all__ = ["setup_service_globals"]
