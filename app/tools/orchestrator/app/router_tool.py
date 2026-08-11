"""router_tool — the routing decision (query_db / web_search / direct_answer).

Self-contained on purpose: the orchestrator is a LEAN service, so this does not
import app.per_db_tool.schema_kg_worker (which pulls in polars / config.schema /
the join engine). It replicates the same small OpenRouter call + safe-default
behaviour. The routing LLM is light enough to live in-process; it can be
promoted to its own backend container later with no change to callers (they only
depend on `RouterTool.route`).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from config.settings import get_openrouter_key

logger = logging.getLogger("uvicorn.error")

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_ORCHESTRATOR_MODEL = settings.SCHEMA_KG_ORCHESTRATOR_MODEL


def _route_system_prompt(display_name: str, capabilities: str, limitations: str,
                         router_note: str = "", rewriter_note: str = "",
                         rewriter_examples: str = "", router_examples: str = "") -> str:
    """Build the router system prompt with optional per-DB, per-layer notes.

    router_note   → appended to STEP 2 routing rules (changes routing decisions).
    rewriter_note → appended to STEP 1 rephrasing rules (changes query expansion).
    *_examples    → top-K dynamic few-shot blocks (from fewshot_bank) for each layer.
    All come from per-DB config/banks and are empty strings when absent — the prompt
    is identical to the old behaviour in that case.
    """
    caps = capabilities or f"the {display_name} database"
    lims = (f"\n\n{display_name} does NOT contain: {limitations}" if limitations else "")
    rewriter_extra = (f"\nADDITIONAL REWRITE RULE: {rewriter_note}" if rewriter_note else "")
    router_extra   = (f"\nADDITIONAL ROUTING RULE: {router_note}"   if router_note   else "")
    return (
        f"You are the {display_name} query router.\n\n"
        f"{display_name} contains:\n{caps}{lims}\n\n"
        "STEP 1 — ALWAYS: Rephrase the query in plain simple terms (one sentence, no answer); "
        "expand any rare or domain abbreviations to their full form "
        "(e.g. TB → Tuberculosis, CML → Chronic Myeloid Leukemia, NSCLC → Non-Small Cell Lung Cancer). "
        f"Store the result in `rephrased_query`.{rewriter_extra}{rewriter_examples}\n\n"
        "STEP 2 — ROUTING (pick exactly one action):\n"
        "1. query_db — USE THIS for any query about specific drugs, genes, diseases, "
        "variants, interactions, pathways, or other biomedical entities that the DB "
        "above might contain. ALWAYS prefer query_db when the topic overlaps with "
        "the DB scope, even if you think you already know the answer from training.\n"
        "2. web_search — use when the question is clearly outside the database scope "
        "AND requires current/external information (e.g. recent news, methods). "
        "ALSO use web_search for ANY question asking for a trade name, brand name, "
        "generic name, investigational name, INN, alternative name, alias, or synonym "
        "of any biomedical entity (drug, disease, gene, etc.) — UNLESS the DB "
        "capabilities section above explicitly mentions a curated synonym or trade-name "
        "table (e.g. drug_synonyms_association), in which case those synonym queries "
        "MUST go to query_db — the ADDITIONAL ROUTING RULE below takes final precedence.\n"
        "   Default (when DB has NO curated synonym table → web_search):\n"
        "   • 'What is the generic name of Xofluza?' → web_search\n"
        "   • 'What is the synonym of MK-1602?' → web_search\n"
        "   • 'What is the alias / also known as / AKA for [drug/gene]?' → web_search\n"
        "   Override (when DB capabilities mention a curated synonym/trade-name table → query_db):\n"
        "   • 'What is the trade name of sildenafil?' → query_db\n"
        "   • 'What are the brand names of imatinib?' → query_db\n"
        "   • 'RTA-408 is the investigational name of which drug?' → query_db\n"
        "   • 'What is the alternative name of RTA 408?' → query_db\n"
        "   • 'DX-88 is the investigational name of which drug?' → query_db\n"
        "3. direct_answer — use ONLY for questions that are completely unrelated to "
        f"biomedical data (e.g. 'What is 2+2?', 'Who wrote Hamlet?').{router_extra}{router_examples}\n\n"
        "Respond with a JSON object ONLY — no other text:\n"
        '  {"action": "query_db", "rephrased_query": "...", "rationale": "..."}\n'
        '  {"action": "web_search", "rephrased_query": "...", "rationale": "..."}\n'
        '  {"action": "direct_answer", "rephrased_query": "...", "answer": "...", "rationale": "..."}'
    )


async def _call_openrouter(model: str, system_prompt: str, user_content: str, db_name: str = "") -> dict:
    """Call the router LLM; return a parsed JSON decision.

    Falls back to {"action": "query_db"} on any error so the DB pipeline
    continues rather than silently rerouting.
    """
    # Provider routing:
    #  • gpt-oss-* → Groq directly (open-weight model; id starts with openai/ but
    #    is NOT an OpenAI-portal model). Keep the full id; it's a reasoning model
    #    so cap reasoning_effort=low or it returns empty content.
    #  • other openai/* → OpenAI portal directly (strip the prefix).
    #  • everything else → OpenRouter.
    extra: dict = {}
    if "gpt-oss" in model:
        base = "https://api.groq.com/openai/v1"
        api_key = os.getenv("GROQ_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        extra = {"reasoning_effort": "low"}
        key_name = "GROQ_API_KEY"
    elif model.startswith("openai/"):
        base = "https://api.openai.com/v1"
        api_key = os.getenv("OPENAI_API_KEY", "")
        model = model[len("openai/"):]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        key_name = "OPENAI_API_KEY"
    else:
        base = _OPENROUTER_BASE
        api_key = get_openrouter_key(db_name)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://biochirp.iiitd.edu.in",
            "X-Title": "BioChirp Orchestrator",
        }
        key_name = f"OPENROUTER_API_KEY_{db_name.upper()}" if db_name else "OPENROUTER_API_KEY"
    if not api_key:
        return {"action": "query_db", "rationale": f"no {key_name} — defaulting to DB"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0,
                    "max_tokens": 600,  # headroom for gpt-oss reasoning + the JSON
                    **extra,
                },
            )
            resp.raise_for_status()
            text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
            if not text:
                return {"action": "query_db",
                        "rationale": f"router model {model} returned no content — defaulting to DB"}
            m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group())
    except Exception as exc:
        logger.warning("[orchestrator:router] LLM call failed: %s", exc)
    return {"action": "query_db", "rationale": "router error — defaulting to DB"}


def _render_rewriter_ex(e: dict) -> str:
    rq = e.get("answer")
    rq = rq if isinstance(rq, str) else (rq or {}).get("rephrased_query", "")
    return f'   ✓ "{e["question"]}"  →  rephrased: "{rq}"'


def _render_router_ex(e: dict) -> str:
    a = e.get("answer") or {}
    action = a.get("action", "") if isinstance(a, dict) else str(a)
    note = f"  ({e['note']})" if e.get("note") else ""
    return f'   ✓ "{e["question"]}"  →  action: {action}{note}'


async def _fewshot_block(db_name: str, stage: str, query: str, header: str, render) -> str:
    """Top-K dynamic few-shot block for a router-call layer. Returns "" on any
    miss/error (bank empty, schema_kg/deps absent in this lean image, Qdrant down)
    so the prompt is byte-identical to today. Retrieval runs off the event loop."""
    if not db_name or not query:
        return ""
    try:
        import asyncio
        from schema_kg.src.fewshot_bank import select_fewshots
        picked = await asyncio.to_thread(select_fewshots, query, db_name, stage)
    except Exception as exc:  # noqa: BLE001 — never break routing
        logger.debug("router fewshot(%s/%s) unavailable: %s", db_name, stage, exc)
        return ""
    if not picked:
        return ""
    return "\n" + header + "\n" + "\n".join(render(e) for e in picked)


class RouterTool:
    name = "router"

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or _ORCHESTRATOR_MODEL

    async def route(self, query: str, *, display_name: str = "BioChirp",
                    capabilities: str = "", limitations: str = "",
                    db_llm_rules: dict | None = None, db_name: str = "") -> dict:
        """Return {"action": "query_db"|"web_search"|"direct_answer", ...}.

        db_llm_rules is the per-DB rules dict from resources/prompts/db_llm_rules.yaml.
        Only the "router" and "rewriter" keys are used here — other keys are
        ignored so each layer stays isolated.
        """
        rules = db_llm_rules or {}
        rewriter_examples = await _fewshot_block(
            db_name, "rewriter", query, "REWRITE EXAMPLES for this database:",
            _render_rewriter_ex)
        router_examples = await _fewshot_block(
            db_name, "router", query, "ROUTING EXAMPLES for this database:",
            _render_router_ex)
        system_prompt = _route_system_prompt(
            display_name, capabilities, limitations,
            router_note=rules.get("router", ""),
            rewriter_note=rules.get("rewriter", ""),
            rewriter_examples=rewriter_examples,
            router_examples=router_examples,
        )
        return await _call_openrouter(self.model, system_prompt, f"Query: {query}", db_name=db_name)
