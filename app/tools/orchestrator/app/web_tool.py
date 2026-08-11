"""web_tool — inline Groq browser-search for the orchestrator.

Prompts are loaded directly from resources/prompts/ (out_of_domain_web.md)
so they remain editable without touching code.  No container hop required.
The former biochirp_web_tool container has been removed; this module is the
sole web-search entry point.
"""
from __future__ import annotations

import logging
import os

import httpx

from config import settings

logger = logging.getLogger("uvicorn.error")

_PROMPTS_DIR = "/app/resources/prompts"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_TIMEOUT = 90.0


def _load_prompt(filename: str) -> str:
    path = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            from utils.disclaimers import splice_disclaimers
        except ImportError:
            from app.utils.disclaimers import splice_disclaimers
        return splice_disclaimers(text)
    except FileNotFoundError:
        logger.warning("[web_tool] prompt file not found: %s", path)
        return "You are a helpful biomedical assistant."


_prompt_general: str | None = None


def _get_prompt() -> str:
    global _prompt_general
    if _prompt_general is None:
        _prompt_general = _load_prompt("out_of_domain_web.md")
    return _prompt_general


async def search_ex(query: str, mode: str = "general", request_id: str = "") -> dict:
    """Call Groq browser-search directly.

    Returns ``{"answer": str, "searched": bool}``. ``searched`` is True only when
    a live browser_search actually ran (Groq reports this via the response's
    ``executed_tools`` field); when the model answers from its own parametric
    knowledge it is False. Callers use this to label provenance honestly.
    """
    api_key    = os.getenv("GROQ_API_KEY", "").strip()
    model_name = settings.WEB_MODEL_NAME
    system     = _get_prompt()

    if not api_key:
        logger.warning("[web_tool][%s] GROQ_API_KEY not set", request_id)
        return {"answer": "", "searched": False}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": query},
                    ],
                    "tools":        [{"type": "browser_search"}],
                    # "auto" (not "required"): with "required" Groq returns HTTP 400
                    # tool_use_failed whenever the model answers a well-known fact
                    # directly without searching, discarding the answer. "auto" lets
                    # it search when needed and answer from memory otherwise.
                    "tool_choice":  "auto",
                    "temperature":  0,
                    "max_completion_tokens": 2048,
                    "reasoning_effort": "medium",
                },
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            return {"answer": (msg.get("content") or "").strip(),
                    "searched": bool(msg.get("executed_tools"))}
    except Exception as exc:
        logger.warning("[web_tool][%s] Groq web search failed: %s", request_id, exc)
        return {"answer": "", "searched": False}
