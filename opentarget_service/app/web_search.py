"""
Web-search fallback for the OpenTargets orchestrator.

Calls Groq's browser_search API directly (no biochirp_web_tool container hop),
mirroring the pattern in app/per_db_tool/schema_kg_worker.call_web_tool.
"""
import os
import logging
import httpx
from agents import function_tool

logger = logging.getLogger("uvicorn.error").getChild("opentargets.web_search")

_GROQ_WEB_URL        = "https://api.groq.com/openai/v1/chat/completions"
_WEB_SEARCH_TIMEOUT  = float(os.environ.get("OT_WEB_SEARCH_TIMEOUT", "30"))
_WEB_MAX_TOKENS      = int(os.environ.get("OT_WEB_MAX_TOKENS", "256"))
_WEB_SYSTEM = (
    "You are a biomedical assistant with an optional web_search tool. Decide whether "
    "to search the way a careful expert would:\n"
    "- ANSWER DIRECTLY from your own knowledge for well-established, stable facts "
    "(mechanisms, drug targets, gene functions, classic/approved indications, "
    "definitions) you are confident about.\n"
    "- USE web_search ONLY when the answer genuinely depends on current, recent, or "
    "time-sensitive information ('latest', 'recent', 'as of <year>', newest "
    "approval/trial/guideline) or the entity is obscure and you are truly unsure. "
    "Do not search just to confirm something you already know.\n"
    "- When you DO search, perform AT MOST ONE search.\n"
    "Answer in AT MOST 3 concise sentences. No preamble, no bullet lists, no "
    "caveats — just the direct factual answer."
)

# Provenance tags — prepended to the returned answer so the OT agent (and the
# eval that captures the final text) can always tell a live web answer apart
# from one the model produced from its own training data. Mirrors the
# web-vs-memory separation the HCDT chat does via _web_provenance_header.
_PROV_WEB = (
    "_Source: live web search (not Open Targets' curated data — verify against "
    "primary sources)._"
)
_PROV_MODEL = (
    "_Source: AI model's own knowledge — no web search was performed; not from "
    "Open Targets' curated data. Verify every claim against authoritative primary "
    "sources._"
)


@function_tool(
    strict_mode=False,
    name_override="web_search",
    description_override=(
        "Search the web for biomedical information not available in Open Targets. "
        "Use when interpreter returns look_up_category='web', when a target/disease/drug "
        "tool fails, or when the user asks a general biomedical question outside OT scope."
    ),
)
async def web_search(query: str) -> str:
    """Groq browser-search fallback for out-of-scope or failed OT queries."""
    from config import settings as _settings
    api_key = _settings.get_groq_key("opentargets").strip()
    model   = _settings.WEB_MODEL_NAME
    if not api_key:
        return "Web search unavailable: GROQ_API_KEY not set."
    try:
        async with httpx.AsyncClient(timeout=_WEB_SEARCH_TIMEOUT) as client:
            resp = await client.post(
                _GROQ_WEB_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _WEB_SYSTEM},
                        {"role": "user",   "content": query},
                    ],
                    "tools": [{"type": "browser_search"}],
                    # "auto" (not "required"): with "required" Groq returns HTTP 400
                    # tool_use_failed whenever the model answers a well-known fact
                    # directly without searching, discarding the answer. "auto" lets
                    # it search when needed and answer from memory otherwise.
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_completion_tokens": _WEB_MAX_TOKENS,
                    "reasoning_effort": "low",
                },
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            answer = (msg.get("content") or "").strip()
            if not answer:
                return "No answer found."
            # `executed_tools` is present only when a live browser_search ran;
            # absent when the model answered from its own parametric knowledge.
            searched = bool(msg.get("executed_tools"))
            tag = _PROV_WEB if searched else _PROV_MODEL
            return f"{tag}\n\n{answer}"
    except Exception as exc:
        # Log the detail, but never surface raw exception text to the user — on the
        # no-message fallback path this tool string can become the final answer.
        logger.warning("[OT web_search] Groq browser-search failed: %s", exc)
        return "Web search was unavailable for this query. Please try again."
