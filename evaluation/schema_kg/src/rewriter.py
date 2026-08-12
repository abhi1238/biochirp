"""
LLM-based query rewriter for the Schema KG pipeline.

Takes a multi-hop natural-language query and decomposes it into focused
sub-queries — one per column concept — so each ANN call scores cleanly
on a single column rather than having one vector diluted across 4+ targets.

Supported models (via OpenRouter):
  - google/gemini-2.5-flash-lite    (primary: 10/10 at all thresholds, accurate)
  - openai/gpt-4.1-nano             (fallback: 10/10 at all thresholds, OpenAI diversity)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Default config ────────────────────────────────────────────────────────────
OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT  = 15   # seconds per call
MAX_TOKENS       = 600

# ── System prompt template ────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
You are a query decomposer for a database schema router.
Given a complex multi-hop natural-language query, split it into simple,
focused sub-queries — one per distinct column/concept needed.
Each sub-query must be a short phrase that clearly targets exactly one column.

{schema_context}

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{"sub_queries": ["<focused sub-query 1>", "<focused sub-query 2>", ...]}}"""


def _or_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key


def rewrite_query(
    query:          str,
    model_id:       str,
    api_key:        Optional[str] = None,
    timeout:        int = DEFAULT_TIMEOUT,
    schema_context: str = "",
) -> tuple[list[str], float]:
    """
    Decompose *query* into focused sub-queries using *model_id* via OpenRouter.

    Parameters
    ----------
    schema_context : compact column list string from _build_schema_context();
                     must be provided for correct column targeting per DB.

    Returns
    -------
    (sub_queries, latency_s)
        sub_queries : list of focused sub-query strings (empty on failure)
        latency_s   : wall-clock seconds for the API call
    """
    key = api_key or _or_key()
    if not key:
        logger.error("No OPENROUTER_API_KEY available — rewriting disabled")
        return [], 0.0

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(schema_context=schema_context)
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": f"Decompose this query:\n{query}"},
        ],
        "max_tokens":  MAX_TOKENS,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }

    t0 = time.perf_counter()
    try:
        r = requests.post(OPENROUTER_BASE, headers=headers, json=payload, timeout=timeout)
        latency = time.perf_counter() - t0

        if r.status_code != 200:
            logger.warning("Rewriter %s HTTP %d: %s", model_id, r.status_code, r.text[:200])
            return [], latency

        content = (r.json()["choices"][0]["message"]["content"] or "").strip()
        sub_queries = _parse(content)
        if not sub_queries:
            logger.warning("Rewriter %s: could not parse JSON from: %s", model_id, content[:120])
        return sub_queries, latency

    except Exception as exc:
        latency = time.perf_counter() - t0
        logger.warning("Rewriter %s exception: %s", model_id, exc)
        return [], latency


REWRITER_PRIMARY  = "google/gemini-2.5-flash-lite"
REWRITER_FALLBACK = "openai/gpt-4.1-nano"


def rewrite_query_cascade(
    query:          str,
    api_key:        Optional[str] = None,
    timeout:        int = DEFAULT_TIMEOUT,
    schema_context: str = "",
) -> tuple[list[str], float, str]:
    """
    Try REWRITER_PRIMARY (gemini-2.5-flash-lite); fall back to REWRITER_FALLBACK
    (gpt-4.1-nano) if the primary returns an empty / unparseable result.

    Parameters
    ----------
    schema_context : compact column list from _build_schema_context(); pass through
                     from the caller so sub-queries target the correct DB columns.

    Returns
    -------
    (sub_queries, total_latency_s, model_used)
    """
    sqs, lat = rewrite_query(query, REWRITER_PRIMARY, api_key, timeout, schema_context)
    if sqs:
        return sqs, lat, REWRITER_PRIMARY

    logger.info("Primary rewriter failed for %r — trying fallback", query)
    sqs2, lat2 = rewrite_query(query, REWRITER_FALLBACK, api_key, timeout, schema_context)
    return sqs2, lat + lat2, REWRITER_FALLBACK


def _parse(text: str) -> list[str]:
    """Parse JSON sub_queries from model response, stripping markdown fences."""
    text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("` \n")
    try:
        return json.loads(text).get("sub_queries", [])
    except Exception:
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0)).get("sub_queries", [])
            except Exception:
                pass
    return []
