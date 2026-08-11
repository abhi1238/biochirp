"""Shared, DB-agnostic Schema-KG worker.

Provides the orchestrator-gated routing pipeline that HCDT pioneered, factored
so any DB can reuse it with zero copied code. A DB supplies a `SchemaKgConfig`
(its name, display name, capability description, and a few optional hooks) and
gets back a fully-wired `return_<db>_result` handler.

Structure (identical for every DB — only the injected data/hooks differ):

  intercept   (SHARED)  Llama-4-Maverick routes query_db / web_search /
                        direct_answer, then runs the in-process schema_kg planner
  pre_expand  (per-DB)  optional parsed_value canonicalisation
  post_expand (SHARED)  converts the schema_kg plan → ctx.plan; optional per-DB
                        narrow hook runs first
  pre_join    (per-DB)  optional plan overrides / value resolution
  on_empty    (SHARED)  Maverick decides retry / web / direct; optional per-DB
                        fallback table

Everything orchestration-related (the LLM routing calls, the web-tool fallback,
the schema_kg plan extraction) lives here ONCE. Per-DB modules carry only data:
their capability prompt and any genuinely DB-specific table quirks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

import httpx

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from config.schema import database_schemas

from ._orchestrator import WorkerCtx, make_db_result_handler, _call_hook
from ._worker_helpers import valid_columns
from .schema_kg_chat import _llm_summarize_step, _build_step_data_text
# to_production_plan is a pure networkx transform (no torch) → runs locally in the
# lean per-DB worker. The heavy planning (bge embeddings + ANN + value-mapping)
# is delegated to the shared schema_mapper HTTP service (see _remote_schema_map).
from .schema_kg_planner import to_production_plan

logger = logging.getLogger("uvicorn.error")


def _alias_map_lookup(value: str, field_alias_map: dict) -> str:
    """Exact-match lookup of *value* in the DB's alias_map field dict.

    field_alias_map is {lowercase_alias: canonical} built from alias_map_<db>.pkl.
    Returns the canonical name if found, otherwise returns *value* unchanged.
    O(1) per lookup — safe to call on every parsed_value entry.
    """
    return field_alias_map.get(value.lower(), value)


def _apply_alias_map_to_parsed_value(pv: dict, db_alias_map: dict) -> None:
    """Substitute canonical DB terms in *pv* using the DB's alias_map pkl.

    For each column in parsed_value, find the matching alias_map field by suffix
    (e.g. 'association_gene_symbol' matches alias_map key 'gene_symbol') and
    replace each list value with its canonical form. Modifies *pv* in-place.
    """
    for col, val in list(pv.items()):
        if not isinstance(val, list):
            continue
        amap: Optional[dict] = None
        for field_key, field_dict in db_alias_map.items():
            if col == field_key or col.endswith(f"_{field_key}") or col.endswith(field_key):
                amap = field_dict
                break
        if not amap:
            continue
        pv[col] = [_alias_map_lookup(v, amap) for v in val]


def _apply_term_rewrite_to_value(value: str, term_rewrite: dict) -> str:
    """Apply all term_rewrite substitutions to a single string value.

    Used to canonicalise parsed_value list entries before expand_and_match so
    fuzzy/semantic search sees the DB's canonical term (e.g. "Trichothiodystrophy")
    instead of the user's alias ("Tay syndrome"). Safe no-op when nothing matches.
    """
    for src, dst in term_rewrite.items():
        if dst.lower() not in value.lower():
            value = re.sub(re.escape(src), dst, value, flags=re.IGNORECASE)
    return value


_MAPPER_PLACEHOLDER_RE = re.compile(r"^<\w+>$")   # matches <value>, <entity>, etc.


def _sanitize_parsed_value(
    pv: dict,
    term_rewrite: Optional[dict],
    cleaned_query: str,
) -> dict:
    """Remove raw mapper placeholder tokens from parsed_value filter fields.

    When the dual-mapper tiebreaker selects the candidate that has '<value>'
    (or similar raw schema placeholder) in a filter field, the parquet filter
    will match 0 rows.  This function:
      1. Strips placeholders from every list field.
      2. When the whole field becomes empty AND term_rewrite is set, scans the
         cleaned_query for any term_rewrite *destination* that appears verbatim
         — if found, uses it as the filter value (recovery without hardcoding).
      3. If still empty, leaves the field as [] so the expand_and_match
         semantic search can attempt a recovery.
    """
    if not pv:
        return pv
    result = dict(pv)
    for col, val in list(result.items()):
        if not isinstance(val, list):
            continue
        # Strip mapper placeholders (e.g. "<value>", "<gene>")
        real = [v for v in val if not _MAPPER_PLACEHOLDER_RE.match(str(v).strip())]
        if len(real) == len(val):
            continue   # no placeholders — nothing to do
        # Try to recover from term_rewrite destinations present in cleaned_query
        if not real and term_rewrite:
            # First pass: if a SRC key appears in the original cleaned_query,
            # use the corresponding DST (canonical DB name) as the filter value.
            # This handles "Liebenberg syndrome" → "Brachydactyly-elbow wrist
            # dysplasia syndrome" even when cleaned_query was not pre-rewritten.
            for _src, _dst in term_rewrite.items():
                if re.search(re.escape(_src), cleaned_query, flags=re.IGNORECASE):
                    real = [_dst]
                    break
            # Second pass: DST already present verbatim in cleaned_query (query
            # was already rewritten before reaching this point in a prior step).
            if not real:
                for _dst in term_rewrite.values():
                    if re.search(re.escape(_dst), cleaned_query, flags=re.IGNORECASE):
                        real = [_dst]
                        break
        result[col] = real   # empty → semantic fallback in expand_and_match
    return result


# ── Shared schema_mapper service (one heavy container serves all DBs) ───────────

def _schema_mapper_url() -> str:
    return os.getenv("SCHEMA_MAPPER_TOOL_URL") or (
        f"http://{os.getenv('SCHEMA_MAPPER_HOST', 'biochirp_schema_mapper_tool')}"
        f":{os.getenv('SCHEMA_MAPPER_PORT', '8019')}/schema_mapper"
    )


async def _remote_schema_map(db: str, query: str,
                             db_llm_rules: Optional[dict] = None) -> Optional[dict]:
    """POST {query} to the shared schema_mapper service for `db`.

    db_llm_rules carries the per-DB LLM rules; the `col_selection`, `mapper` and
    `tiebreaker` keys are forwarded — each to its specific schema_mapper LLM
    (expander / value-mapper / dual-mapper disagreement resolver). Other keys
    are ignored here so each layer stays isolated.

    Returns the (rehydrated) plan dict, or None when the service reports an
    explicit NO-MATCH (0 ANN hits → caller routes to the web tool).
    RAISES on transport error (service down/timeout) so the caller can fall back
    to the HTTP expand+planner path — preserving the in-process design's graceful
    degradation.
    """
    payload: dict = {"query": query}
    rules = db_llm_rules or {}
    col_sel    = (rules.get("col_selection") or "").strip()
    mapper     = (rules.get("mapper") or "").strip()
    tiebreaker = (rules.get("tiebreaker") or "").strip()
    if col_sel:
        payload["col_selection_note"] = col_sel
    if mapper:
        payload["mapper_note"] = mapper
    if tiebreaker:
        payload["tiebreaker_note"] = tiebreaker
    url = f"{_schema_mapper_url()}?database={db}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    if not body.get("matched") or not body.get("plan"):
        return None
    return _rehydrate_sk_plan(body["plan"])


def _rehydrate_sk_plan(plan: dict) -> dict:
    """Rehydrate a JSON-serialized schema_kg plan back to the set/tuple shapes the
    downstream code (narrow heuristics + to_production_plan) expects. Shared by the
    schema_mapper and orchestrator intercept paths."""
    plan["plan_tables"] = set(plan.get("plan_tables", []))
    plan["needed_tables"] = set(plan.get("needed_tables", []))
    plan["join_path"] = [tuple(step) for step in plan.get("join_path", [])]
    plan["table_cols"] = {t: set(c) for t, c in (plan.get("table_cols") or {}).items()}
    return plan

Hook = Callable[["WorkerCtx"], Union[None, Awaitable[None]]]
# Called when schema_mapper returns None (no ANN match) for a query routed to
# query_db. Receives (ctx, rephrased_query); returns a rewritten query string
# that schema_mapper should retry with, or None to skip retry and fall through
# to the web tool. HCDT uses this for trade-name → INN rewriting.
SynonymFallback = Callable[["WorkerCtx", str], Optional[str]]

# ── Orchestrator (Llama-4-Maverick via OpenRouter) ──────────────────────────────

_OPENROUTER_BASE    = "https://openrouter.ai/api/v1"
_ORCHESTRATOR_MODEL = settings.SCHEMA_KG_ORCHESTRATOR_MODEL
_RETRY_ON_EMPTY_FILTER_PLAN_ATTEMPTS = 3


@dataclass
class SchemaKgConfig:
    """Per-DB configuration for the shared schema_kg worker.

    Only `db`, `display_name`, `get_db`, `prompt_md` are required. The rest are
    data (capability text) or optional hooks for genuinely DB-specific quirks.
    A DB with clean schema_kg inputs needs no hooks at all.
    """
    db: str
    display_name: str
    get_db: Callable
    prompt_md: str
    summarizer_model: Optional[str] = None
    # Capability description, injected into the router + retry prompts.
    capabilities: str = ""
    limitations: str = ""
    # Per-DB, per-layer LLM rules. Auto-loaded from resources/prompts/db_llm_rules.yaml
    # if not explicitly provided. Each key is used ONLY in its specific LLM layer.
    db_llm_rules: dict = field(default_factory=dict)
    orchestrator_model: str = _ORCHESTRATOR_MODEL
    # Optional per-DB hooks (sync or async). All receive the WorkerCtx.
    pre_expand: Optional[Hook] = None
    narrow: Optional[Hook] = None          # runs inside post_expand, before plan-set
    pre_join: Optional[Hook] = None
    post_join: Optional[Hook] = None
    on_empty_fallback: Optional[Hook] = None
    # Optional synonym fallback: called once when schema_mapper returns None.
    # If it returns a non-None string the schema_mapper is retried with that
    # rewritten query; on a second miss the web tool is used as before.
    # Inert on all DBs that don't supply it (default None).
    on_schema_map_empty: Optional[SynonymFallback] = None
    use_async_post: bool = True
    # Optional per-DB result ordering: list of {"col", "dir"|"order"}. Applied
    # as a stable re-sort after relevance scoring (see _orchestrator
    # _apply_sort_order). Lets a DB rank curated columns (e.g. evidence tier)
    # above semantic relevance without any shared-code change. None → unchanged.
    sort_order: Optional[list] = None
    # Optional pre-mapper query term rewrites: {verbatim_phrase → replacement}.
    # Applied case-insensitively to the rephrased_query BEFORE it reaches the
    # schema_mapper LLM. Needed when the canonical DB term differs from how
    # users phrase it AND the mapper's verbatim-copy rule (Rule 1) prevents
    # the LLM-level mapper_note from working (e.g. MeSH disease synonyms in CTD).
    term_rewrite: Optional[dict] = None
    # Opt-in deterministic-routing guard (default False — no behavior change for
    # any DB unless explicitly enabled). Mirrors the proven, generic fix already
    # live in schema_kg_chat.py's outer orchestrator (2026-06-23, CTD ~58% decline
    # rate): the router LLM unreliably picks direct_answer/web_search for in-scope
    # named-entity queries instead of query_db, and prompt-only fixes (db_llm_rules
    # router text) reduce but don't eliminate this — it's nondeterministic per call.
    # When True, skip the router LLM call entirely for any non-greeting/meta query
    # and force action="query_db"; the existing on_empty orchestrator (already
    # LLM-gated on the ACTUAL 0-row result) remains the sole decider of whether to
    # fall back to web_search/direct_answer. Generic — no per-DB/per-entity text.
    force_query_db_first: bool = False
    # Opt-in retry (default False) for the schema_mapper's dual-mapper/tiebreaker
    # LLM stage (value_mapper.py), which is genuinely non-deterministic: mapper_1,
    # mapper_2, and the tiebreaker orchestrator are all the SAME Groq-served model
    # (SCHEMA_KG_FILTER_MODEL/ENSEMBLE_MODEL_2/MAP_ORCHESTRATOR_MODEL — confirmed
    # identical), and Groq's serving does not reliably honor temperature=0/seed=42
    # (acknowledged in value_mapper.py's own comments). So "agreement" between the
    # two mappers is two samples of the same noisy call, not independent consensus
    # — occasionally BOTH samples (and the tiebreak) miss the entity filter entirely,
    # returning plan["filter_plan"]={} for a query that plainly names an entity.
    # When True, retry _remote_schema_map (same query, fresh LLM calls) up to
    # RETRY_ON_EMPTY_FILTER_PLAN_ATTEMPTS times whenever it returns a plan with an
    # empty filter_plan, keeping the first attempt that produces a real filter.
    # Generic — detects the empty-filter SIGNAL, not any per-DB/per-entity text.
    retry_on_empty_filter_plan: bool = False
    # DB alias map loaded from alias_map_<db>.pkl at startup. Used post-parser to
    # canonicalise parsed_value entries (e.g. "triadin" → "TRDN") without touching
    # the query text. Populated automatically in __post_init__; no per-DB code needed.
    _db_alias_map: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.db_llm_rules:
            from .db_llm_rules import load_db_llm_rules
            self.db_llm_rules = load_db_llm_rules(self.db)
        try:
            from utils.concept_values import get_db_alias_map
            self._db_alias_map = get_db_alias_map(self.db)
        except Exception:
            self._db_alias_map = {}


def _route_system_prompt(cfg: SchemaKgConfig) -> str:
    caps = cfg.capabilities or f"the {cfg.display_name} database"
    lims = (f"\n\n{cfg.display_name} does NOT contain: {cfg.limitations}"
            if cfg.limitations else "")
    rules = cfg.db_llm_rules or {}
    rewriter_extra = (f"\nADDITIONAL REWRITE RULE: {rules['rewriter']}"
                      if rules.get("rewriter") else "")
    router_extra   = (f"\nADDITIONAL ROUTING RULE: {rules['router']}"
                      if rules.get("router") else "")
    return (
        f"You are the {cfg.display_name} query router.\n\n"
        f"{cfg.display_name} contains:\n{caps}{lims}\n\n"
        "STEP 1 — ALWAYS: Rephrase the query in plain simple terms (one sentence, no answer); "
        "expand any rare or domain abbreviations to their full form "
        "(e.g. TB → Tuberculosis, CML → Chronic Myeloid Leukemia, NSCLC → Non-Small Cell Lung Cancer). "
        f"Store the result in `rephrased_query`.{rewriter_extra}\n\n"
        "STEP 2 — ROUTING (pick exactly one action):\n"
        "1. query_db — USE THIS for any query about specific drugs, genes, diseases, "
        "variants, interactions, pathways, or other biomedical entities that the DB "
        "above might contain. ALWAYS prefer query_db when the topic overlaps with "
        "the DB scope, even if you think you already know the answer from training.\n"
        "2. web_search — use only when the question is clearly outside the database "
        "scope AND requires current/external information (e.g. recent news, methods).\n"
        "3. direct_answer — use ONLY for questions that are completely unrelated to "
        f"biomedical data (e.g. 'What is 2+2?', 'Who wrote Hamlet?').{router_extra}\n\n"
        "Respond with a JSON object ONLY — no other text:\n"
        '  {"action": "query_db", "rephrased_query": "...", "rationale": "..."}\n'
        '  {"action": "web_search", "rephrased_query": "...", "rationale": "..."}\n'
        '  {"action": "direct_answer", "rephrased_query": "...", "answer": "...", "rationale": "..."}'
    )


def _retry_system_prompt(cfg: SchemaKgConfig) -> str:
    caps = cfg.capabilities or f"the {cfg.display_name} database"
    lims = (f"\n\n{cfg.display_name} does NOT contain: {cfg.limitations}"
            if cfg.limitations else "")
    rules = cfg.db_llm_rules or {}
    router_extra = (f"\nADDITIONAL ROUTING RULE: {rules['router']}"
                    if rules.get("router") else "")
    return (
        f"You are the {cfg.display_name} result evaluator. A {cfg.display_name} "
        "database query just returned 0 rows.\n\n"
        f"{cfg.display_name} contains:\n{caps}{lims}\n\n"
        "Decide whether the data might exist under a different approach, or is "
        f"genuinely absent and should be answered from the web.{router_extra}\n\n"
        "Respond with a JSON object ONLY — no other text:\n"
        '  {"action": "retry", "rephrased_query": "...", "suggestion": "...", "rationale": "..."}\n'
        '  {"action": "web_search", "rephrased_query": "...", "rationale": "..."}\n'
        '  {"action": "direct_answer", "rephrased_query": "...", "answer": "...", "rationale": "..."}'
    )


async def call_orchestrator(model: str, system_prompt: str, user_content: str) -> dict:
    """Call the orchestrator LLM; return a parsed JSON decision.

    Tries the litellm proxy first (same endpoint the schema_mapper uses), then falls
    back to OpenRouter directly. Degrades to {"action": "query_db"} on all errors.
    """
    # Prefer litellm proxy — avoids direct OpenRouter key exhaustion issues.
    litellm_base = os.getenv("OPENAI_BASE_URL", "")
    openai_key   = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    or_key       = os.getenv("OPENROUTER_API_KEY", "")

    endpoints: list[tuple[str, str, dict]] = []
    if litellm_base:
        endpoints.append((f"{litellm_base}/chat/completions", openai_key,
                          {"Content-Type": "application/json"}))
    if or_key:
        endpoints.append((f"{_OPENROUTER_BASE}/chat/completions", or_key,
                          {"Content-Type": "application/json",
                           "HTTP-Referer": "https://biochirp.iiitd.edu.in",
                           "X-Title": "BioChirp Schema-KG"}))
    if not endpoints:
        return {"action": "query_db", "rationale": "no API key configured — defaulting to DB"}

    # gpt-oss is a reasoning model — without a capped reasoning_effort it can spend
    # the whole max_tokens budget on hidden reasoning and return empty content,
    # silently dropping rephrased_query (see router_tool.py's identical guard).
    extra_body: dict = {"reasoning_effort": "low"} if "gpt-oss" in model else {}

    for url, key, extra_headers in endpoints:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", **extra_headers},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "temperature": 0,
                        "max_tokens": 600,  # headroom for gpt-oss reasoning + the JSON
                        **extra_body,
                    },
                )
                resp.raise_for_status()
                # Some models (reasoning models) return content=None — guard.
                text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                if not text:
                    continue
                m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
                if m:
                    return json.loads(m.group())
        except Exception as exc:
            logger.warning("[schema_kg:%s] orchestrator call failed (%s): %s", model, url, exc)
    return {"action": "query_db", "rationale": "orchestrator error — defaulting to DB"}


# Groq browser-search endpoint — called DIRECTLY (no container hop).
_GROQ_WEB_URL = "https://api.groq.com/openai/v1/chat/completions"
_WEB_SEARCH_SYSTEM = (
    "You are a biomedical assistant with an optional web_search tool. Decide whether "
    "to search the way a careful expert would:\n"
    "- ANSWER DIRECTLY from your own knowledge for well-established, stable facts — "
    "mechanisms of action, drug targets, gene functions, classic/approved indications, "
    "definitions, and other textbook biomedical knowledge you are confident about.\n"
    "- USE web_search ONLY when the answer genuinely depends on current, recent, or "
    "time-sensitive information (phrases like 'latest', 'recent', 'newest', 'as of "
    "<year>', most-recent approval/trial/guideline, prices, ongoing status) OR the "
    "entity is obscure and you are truly unsure. When in doubt and the fact is stable, "
    "answer directly — do not search just to confirm something you already know.\n"
    "- When you DO search, perform AT MOST ONE search and answer from the result "
    "snippets; do not open full pages unless essential.\n"
    "Answer in AT MOST 2 short sentences. No preamble, no citations, no caveats — "
    "just the direct factual answer."
)


async def web_search_ex(query: str) -> dict:
    """Answer via Groq's browser-search API directly (in-process).

    Returns ``{"answer": str, "searched": bool}``. ``searched`` is True only when
    the browser_search tool actually executed — Groq reports this via the
    response message's ``executed_tools`` field. When the model answers a
    well-known fact from its own parametric knowledge it does NOT search, so
    ``searched`` is False; callers use this to label provenance honestly.

    ``tool_choice="auto"`` lets the model decide whether to search. Forcing it
    ("required") made Groq return HTTP 400 ``tool_use_failed`` whenever the model
    answered directly without searching — discarding an otherwise-correct answer
    into the error payload.
    """
    from config import settings as _settings
    api_key = _settings.get_groq_key(os.getenv("SERVICE_NAME", "")).strip()
    model   = settings.WEB_MODEL_NAME
    if not api_key:
        logger.warning("[schema_kg] GROQ_API_KEY not set — web search unavailable")
        return {"answer": "", "searched": False}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _GROQ_WEB_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _WEB_SEARCH_SYSTEM},
                        {"role": "user", "content": query},
                    ],
                    "tools": [{"type": "browser_search"}],
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_completion_tokens": 400,
                    "reasoning_effort": "low",
                },
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            return {"answer": (msg.get("content") or "").strip(),
                    "searched": bool(msg.get("executed_tools"))}
    except Exception as exc:
        logger.warning("[schema_kg] Groq web search failed: %s", exc)
        return {"answer": "", "searched": False}


async def call_web_tool(query: str) -> str:
    """Backward-compatible string wrapper around :func:`web_search_ex`."""
    return (await web_search_ex(query))["answer"]


# ── Optional cutover to the standalone orchestrator service ─────────────────────
# Env-gated, default OFF (zero behavior change). When `cfg.db` is listed in
# SCHEMA_KG_ORCHESTRATOR_DBS the entire route+map+plan intercept is delegated to
# the biochirp_orchestrator_tool over HTTP; ANY failure falls back to the
# in-process path, so the flip is self-healing. SCHEMA_KG_ORCHESTRATOR_SHADOW=1
# instead leaves the live path untouched but fires a fire-and-forget orchestrator
# call and logs a plan_tables diff — for safe pre-cutover comparison.

def _orchestrator_enabled_dbs() -> set:
    return {d.strip() for d in
            os.getenv("SCHEMA_KG_ORCHESTRATOR_DBS", "").split(",") if d.strip()}


def _orchestrator_shadow() -> bool:
    return os.getenv("SCHEMA_KG_ORCHESTRATOR_SHADOW", "0") == "1"


def _orchestrator_url() -> str:
    return os.getenv("ORCHESTRATOR_TOOL_URL") or (
        f"http://{os.getenv('ORCHESTRATOR_HOST', 'biochirp_orchestrator_tool')}"
        f":{os.getenv('ORCHESTRATOR_PORT', '8021')}/orchestrate")


async def _call_orchestrator_http(db: str, query: str,
                                  capabilities: str = "",
                                  limitations: str = "",
                                  db_llm_rules: dict | None = None) -> dict:
    url = f"{_orchestrator_url()}?database={db}"
    body: dict = {"query": query, "capabilities": capabilities, "limitations": limitations}
    if db_llm_rules:
        body["db_llm_rules"] = db_llm_rules
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()


async def _intercept_via_orchestrator(cfg: SchemaKgConfig, ctx: "WorkerCtx",
                                      query: str) -> bool:
    """Run the intercept through the orchestrator service. Return True if it fully
    populated ctx (caller returns), False to fall back to the in-process path."""
    try:
        result = await _call_orchestrator_http(cfg.db, query,
                                               cfg.capabilities, cfg.limitations,
                                               db_llm_rules=cfg.db_llm_rules or None)
    except Exception as exc:
        ctx.log.warning("[%s] orchestrator unreachable (%s)", cfg.db, exc)
        return False
    action = result.get("action", "query_db")
    ctx.log.info("[%s] [orchestrator-path] action=%s", cfg.db, action)
    if action == "direct_answer":
        ctx.error_msg = result.get("answer") or "Please rephrase your query."
        ctx.plan = {}
        return True
    if action == "web_search":
        ctx.error_msg = result.get("answer") or f"No relevant {cfg.display_name} data found."
        ctx.plan = {}
        return True
    if not result.get("matched") or not (result.get("plan") or {}).get("plan_tables"):
        return False  # let the in-process expand+planner path try
    sk_plan = _rehydrate_sk_plan(result["plan"])
    ctx.inp["parsed_value"] = sk_plan.get("parsed_value", {})
    ctx.extras["sk_plan"] = sk_plan
    ctx.log.info("[%s] [orchestrator-path] plan_tables=%s", cfg.db,
                 sorted(sk_plan["plan_tables"]))
    return True


async def _shadow_orchestrator(cfg: SchemaKgConfig, ctx: "WorkerCtx", query: str,
                               inproc_plan: Optional[dict]) -> None:
    """Fire-and-forget: call the orchestrator and log a plan_tables diff vs the
    live in-process plan. Never affects the response."""
    try:
        result = await _call_orchestrator_http(cfg.db, query,
                                               cfg.capabilities, cfg.limitations,
                                               db_llm_rules=cfg.db_llm_rules or None)
        orch_tables = sorted((result.get("plan") or {}).get("plan_tables", []))
        inproc_tables = sorted(inproc_plan.get("plan_tables", [])) if inproc_plan else []
        match = orch_tables == inproc_tables
        ctx.log.info("[%s] [shadow] match=%s inproc=%s orch=%s", cfg.db, match,
                     inproc_tables, orch_tables)
    except Exception as exc:
        ctx.log.warning("[%s] [shadow] orchestrator call failed: %s", cfg.db, exc)


def _build_intercept(cfg: SchemaKgConfig) -> Hook:
    route_prompt = _route_system_prompt(cfg)
    _greeting_re = None
    if cfg.force_query_db_first:
        from .schema_kg_chat import _GREETING_META_RE as _greeting_re

    async def _intercept(ctx: WorkerCtx) -> None:
        # Per-DB parsed_value massaging happens here (before routing) so the
        # cleaned_query the router/planner sees is already canonical. All per-DB
        # LLM rules (router/rewriter here; col_selection/mapper/tiebreaker via
        # _remote_schema_map) are handled in the schema_mapper path — the expand
        # candidate filter stays rule-free.
        await _call_hook(cfg.pre_expand, ctx)

        query = ctx.inp.get("cleaned_query", "")
        if not query:
            return

        # Optional cutover: delegate the whole intercept to the orchestrator
        # service for opted-in DBs. Self-healing — falls through on any failure.
        if cfg.db in _orchestrator_enabled_dbs():
            if await _intercept_via_orchestrator(cfg, ctx, query):
                return
            ctx.log.warning("[%s] orchestrator path did not resolve — using "
                            "in-process pipeline", cfg.db)

        # Step 1: orchestrator routing decision. Still call the LLM (it also does
        # the db_llm_rules["rewriter"] abbreviation-expansion rephrasing, e.g. HPO's
        # "AR" → "Autosomal Recessive" — losing that would be its own regression),
        # but force_query_db_first DBs OVERRIDE its action to query_db for any
        # non-greeting query instead of trusting the LLM's unreliable decision (see
        # field doc on SchemaKgConfig.force_query_db_first). The on_empty
        # orchestrator below still gates the eventual web_search/direct_answer
        # fallback on the query's REAL 0-row result.
        decision = await call_orchestrator(cfg.orchestrator_model, route_prompt,
                                           f"Query: {query}")
        action = decision.get("action", "query_db")
        rephrased_query = decision.get("rephrased_query") or query
        if (cfg.force_query_db_first and action != "query_db"
                and not _greeting_re.match(query or "")):
            ctx.log.info("[%s] force_query_db_first: overriding router action=%s → query_db",
                         cfg.db, action)
            action = "query_db"
        ctx.log.info("[%s] orchestrator route=%s rephrase=%r (%s)", cfg.db, action,
                     rephrased_query[:80], str(decision.get("rationale", ""))[:80])

        if action == "direct_answer":
            ctx.error_msg = decision.get("answer") or "Please rephrase your query."
            ctx.plan = {}
            return
        if action == "web_search":
            answer = await call_web_tool(rephrased_query)
            ctx.error_msg = answer or f"No relevant {cfg.display_name} data found."
            ctx.plan = {}
            return

        # Step 2: query_db → shared schema_mapper service (HTTP).
        # Apply per-DB term rewrites to the query BEFORE the mapper sees it.
        # The mapper's Rule 1 copies values verbatim, so translations must be in
        # the query text itself (not mapper_note) to reliably take effect.
        mapper_query = rephrased_query
        if cfg.term_rewrite:
            for src, dst in cfg.term_rewrite.items():
                # Skip if the destination (canonical name) is already present in the
                # query — re-substituting would produce a double name like
                # "Fatal Familial Insomnia (Fatal familial insomnia)".
                if dst.lower() not in mapper_query.lower():
                    mapper_query = re.sub(re.escape(src), dst, mapper_query, flags=re.IGNORECASE)
            if mapper_query != rephrased_query:
                ctx.log.info("[%s] term_rewrite: %r → %r", cfg.db,
                             rephrased_query[:80], mapper_query[:80])
        try:
            plan = await _remote_schema_map(cfg.db, mapper_query,
                                            db_llm_rules=cfg.db_llm_rules)
        except Exception as exc:
            ctx.log.warning("[%s] schema_mapper unreachable (%s) — falling back to "
                            "HTTP expand+planner", cfg.db, exc)
            return  # ctx.plan stays None → orchestrator runs expand+planner

        if cfg.retry_on_empty_filter_plan and plan is not None and not plan.get("filter_plan"):
            for _retry in range(_RETRY_ON_EMPTY_FILTER_PLAN_ATTEMPTS):
                ctx.log.info("[%s] empty filter_plan (attempt %d) — retrying schema_mapper",
                             cfg.db, _retry + 1)
                try:
                    retry_plan = await _remote_schema_map(cfg.db, mapper_query,
                                                          db_llm_rules=cfg.db_llm_rules)
                except Exception as exc:
                    ctx.log.warning("[%s] schema_mapper retry failed: %s", cfg.db, exc)
                    break
                if retry_plan is not None and retry_plan.get("filter_plan"):
                    plan = retry_plan
                    break

        if plan is None and cfg.on_schema_map_empty is not None:
            # Per-DB synonym fallback (e.g. HCDT trade-name → INN rewrite).
            # Retry schema_mapper once with the rewritten query before giving up.
            rewritten = cfg.on_schema_map_empty(ctx, rephrased_query)
            if rewritten and rewritten != rephrased_query:
                # Apply term_rewrite to the fallback query too.
                if cfg.term_rewrite:
                    for src, dst in cfg.term_rewrite.items():
                        if dst.lower() not in rewritten.lower():
                            rewritten = re.sub(re.escape(src), dst, rewritten, flags=re.IGNORECASE)
                ctx.log.info("[%s] on_schema_map_empty retry: %r → %r",
                             cfg.db, rephrased_query[:50], rewritten[:50])
                try:
                    plan = await _remote_schema_map(cfg.db, rewritten,
                                                    db_llm_rules=cfg.db_llm_rules)
                except Exception as exc:
                    ctx.log.warning("[%s] schema_mapper retry failed: %s", cfg.db, exc)
                    plan = None

        if plan is None:
            # Explicit no-match (0 ANN hits) → web tool.
            ctx.log.info("[%s] schema_mapper: no match for %r — routing to web tool",
                         cfg.db, rephrased_query[:60])
            answer = await call_web_tool(rephrased_query)
            ctx.error_msg = answer or f"No relevant {cfg.display_name} data found."
            ctx.plan = {}
            return

        # DB-relevant → let expand run with schema_kg's parsed_value; stash the
        # pruned plan for post_expand to convert into ctx.plan.
        ctx.inp["parsed_value"] = plan["parsed_value"]

        # Remove raw mapper placeholder tokens ("<value>" etc.) that slipped
        # through the dual-mapper tiebreaker — they cause 0-row parquet filters.
        # Tries to recover the canonical name by matching term_rewrite SRC/DST
        # against the original cleaned_query (not mapper_query, to avoid double-
        # rewrite artefacts from the orchestrator re-adding parenthetical names).
        ctx.inp["parsed_value"] = _sanitize_parsed_value(
            ctx.inp["parsed_value"],
            cfg.term_rewrite,
            ctx.inp.get("cleaned_query", ""),
        )
        if ctx.inp["parsed_value"] != plan["parsed_value"]:
            ctx.log.info("[%s] sanitize_parsed_value: removed placeholders → %s",
                         cfg.db, ctx.inp["parsed_value"])

        # Canonicalise parsed_value entries using (a) the DB's alias_map pkl and
        # (b) any structural term_rewrite rules (mTORC1→MTOR etc.).
        # alias_map is applied first (O(1) exact lookup, 544k+ entries from the
        # DB's own alias parquet). term_rewrite runs after to handle compound/
        # contextual rewrites not expressible as alias lookups.
        _pv = ctx.inp["parsed_value"]
        if cfg._db_alias_map:
            _apply_alias_map_to_parsed_value(_pv, cfg._db_alias_map)
        if cfg.term_rewrite:
            for _col, _val in list(_pv.items()):
                if isinstance(_val, list):
                    _pv[_col] = [_apply_term_rewrite_to_value(v, cfg.term_rewrite) for v in _val]
        ctx.extras["sk_plan"] = plan
        ctx.log.info("[%s] schema_kg: plan_tables=%s filter_cols=%s", cfg.db,
                     sorted(plan["plan_tables"]), list(plan["filter_plan"].keys()))

        # Emit Schema Mapper step event if a ws_send callback is wired.
        # Card body goes through the LLM step-summarizer (same prompt/model
        # the batched fallback path uses) so it reads as plain English
        # instead of a raw "parsed_value: k=v, ..." dump. Guarded: this is a
        # cosmetic card and must NEVER be able to abort the actual query
        # (see 2026-08-03 incident where an unguarded step-summarizer call
        # elsewhere killed the whole request on an unrelated data-shape bug).
        if ctx.ws_send:
            import uuid as _uuid
            _sm_id = f"sm-{_uuid.uuid4().hex[:6]}"
            pv = plan.get("parsed_value") or {}
            try:
                _sm_text = await _llm_summarize_step(
                    "schema_mapper", {"parsed_value": pv}, rephrased_query)
            except Exception as _summ_exc:
                ctx.log.debug("[%s] schema_mapper step-summarizer failed: %s", cfg.db, _summ_exc)
                _sm_text = _build_step_data_text("schema_mapper", {"parsed_value": pv})
            try:
                await ctx.ws_send({"type": "tool_called", "tool_id": _sm_id, "name": "Schema Mapper"})
                await ctx.ws_send({"type": "delta", "tool_id": _sm_id, "name": "Schema Mapper",
                                   "text": _sm_text, "seq": 1, "offset": 0, "final": False})
                await ctx.ws_send({"type": "tool_result", "tool_id": _sm_id,
                                   "name": "Schema Mapper", "ok": True})
            except Exception as _ws_exc:
                ctx.log.debug("[%s] ws_send schema_mapper event failed: %s", cfg.db, _ws_exc)

        # Shadow mode: compare the orchestrator's plan to the live one (logged
        # only, never affects the response). Fire-and-forget.
        if _orchestrator_shadow():
            asyncio.create_task(_shadow_orchestrator(cfg, ctx, query, plan))

    return _intercept


def _build_post_expand(cfg: SchemaKgConfig) -> Hook:

    async def _post_expand(ctx: WorkerCtx) -> None:
        schema_cols = {c for tbl in database_schemas[cfg.db].values() for c in tbl}

        # Drop unsupported fields (cols not present in this DB's schema).
        used_cols = [c for c, v in ctx.filter_val.items()
                     if v == "requested" or (isinstance(v, list) and v)]
        missing = [c for c in used_cols if c not in schema_cols]
        if missing:
            ctx.log.warning("[%s] Dropping unsupported fields: %s", cfg.db, missing)
            for k in missing:
                ctx.filter_val[k] = None
            if ctx.expand_response and ctx.expand_response.get("value") is not None:
                ctx.expand_response["value"] = ctx.filter_val
            ctx.out_cols = valid_columns(ctx.filter_val, cfg.db)

        sk_plan = ctx.extras.get("sk_plan")

        # Per-DB narrow hook (HCDT's query-type heuristics) takes precedence.
        if cfg.narrow is not None:
            await _call_hook(cfg.narrow, ctx)
        elif sk_plan and sk_plan.get("plan_tables"):
            # Generic narrowing: restrict out_cols / filter_val to the columns
            # the schema_kg plan actually uses, preventing the all-columns Polars
            # join collision without any DB-specific knowledge.
            #
            # out_cols = output_plan columns FIRST (what the user asked for),
            # then filter_plan columns that aren't already covered (for context).
            # Separating "SELECT" from "WHERE" ensures the primary answer column
            # is df.columns[0] in the CSV — important for benchmark counting.
            out_cols_set: set = set()
            filter_cols_set: set = set()
            for cols in sk_plan.get("output_plan", {}).values():
                out_cols_set.update(cols)
            # Fallback: parsed_value columns marked "requested" are output columns
            # even when the schema_mapper LLM omits them from output_plan (common
            # failure mode — the mapper sets parsed_value correctly but skips the
            # output_plan entry, causing the column to be silently dropped here).
            for col, val in (sk_plan.get("parsed_value") or {}).items():
                if val == "requested":
                    out_cols_set.add(col)
            for cols in sk_plan.get("filter_plan", {}).values():
                filter_cols_set.update(cols.keys())
            out_cols_set &= schema_cols
            filter_cols_set &= schema_cols
            plan_cols = out_cols_set | filter_cols_set
            if plan_cols:
                # Output columns first (sorted), then filter-only columns (sorted)
                filter_only = filter_cols_set - out_cols_set
                ctx.out_cols = sorted(out_cols_set) + sorted(filter_only)
                new_fv = dict(ctx.filter_val)
                for k, v in list(new_fv.items()):
                    if v == "requested" and k not in plan_cols:
                        new_fv[k] = None
                ctx.filter_val = new_fv
                if ctx.expand_response and ctx.expand_response.get("value") is not None:
                    ctx.expand_response["value"] = ctx.filter_val

        # Restore filter_plan values that expand_and_match dropped because the
        # column names are not in CommonFields (e.g. "attribute", "value").
        # expand_and_match uses OutputFields (CommonFields subclass) which silently
        # ignores unknown fields, so categorical filters like attribute=["inheritance"]
        # never reach ctx.filter_val. Re-inject from sk_plan directly.
        if sk_plan and sk_plan.get("filter_plan"):
            for _tbl_fp, _col_map in sk_plan["filter_plan"].items():
                for _col, _vals in (_col_map.items() if isinstance(_col_map, dict) else []):
                    if _col in schema_cols and not ctx.filter_val.get(_col):
                        ctx.filter_val[_col] = _vals
                        ctx.log.info(
                            "[%s] restored dropped filter_plan value: %s=%r",
                            cfg.db, _col, _vals,
                        )

        # Convert the schema_kg pruned plan → production plan (skips HTTP planner).
        # Pure networkx transform — runs locally in the lean worker, no torch.
        # Skip if the narrow hook already set a production plan directly.
        if sk_plan and sk_plan.get("plan_tables") and ctx.plan is None:
            prod = to_production_plan(sk_plan, cfg.db)
            if prod.get("join_pairs"):
                prod["join_pairs"] = {
                    f"{k[0]},{k[1]}" if isinstance(k, tuple) else k: v
                    for k, v in prod["join_pairs"].items()
                }
            ctx.plan = prod
            ctx.log.info("[%s] plan set from schema_kg — HTTP planner skipped", cfg.db)

    return _post_expand


_CORRECTIVE_MAPPER_NOTE = (
    "Your previous extraction on this exact question returned ZERO rows. "
    "Re-examine every filter you emitted: for each one, remove the clause "
    "containing it and check whether the question still asks for the same "
    "thing. If it does, that term was background/motivation context, not a "
    "real filter -- drop it (mark the column 'requested' or omit it) and "
    "keep only the filters that are the actual subject of the question."
)


async def _retry_with_corrective_hint(cfg: SchemaKgConfig, ctx: "WorkerCtx",
                                      post_expand_hook: Hook) -> None:
    """One deterministic re-extraction attempt before the existing on_empty
    decision (web_search / direct_answer / DB-fallback).

    Narrative/motivation context in a question can get misread by the
    entity-extraction LLM as a real filter (e.g. "...which keeps showing up
    in relation to epilepsy..." wrongly extracted as a disease/geneset
    filter), silently zeroing out a real answer. This re-runs extraction
    with a corrective hint via the SAME schema_mapper + expand_and_match_db
    + join pipeline the main flow uses, and only touches `ctx` if it finds
    real rows. Any failure (exception, still empty, malformed plan) leaves
    `ctx` untouched -- the caller falls straight through to the existing,
    unchanged behaviour.
    """
    query = ctx.inp.get("cleaned_query", "")
    if not query:
        return
    retry_rules = dict(cfg.db_llm_rules or {})
    retry_rules["mapper"] = _CORRECTIVE_MAPPER_NOTE
    plan = await _remote_schema_map(cfg.db, query, db_llm_rules=retry_rules)
    if not plan or not plan.get("parsed_value"):
        return

    ctx.inp["parsed_value"] = plan["parsed_value"]
    ctx.extras["sk_plan"] = plan

    expand_url = os.getenv("EXPAND_TOOL_URL") or (
        f"http://{os.getenv('EXPAND_AND_MATCH_DB_HOST', 'biochirp_expand_and_match_db_tool')}"
        f":{os.getenv('EXPAND_AND_MATCH_DB_PORT', '8009')}/expand_and_match_db"
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{expand_url}?database={cfg.db}", json=ctx.inp)
        resp.raise_for_status()
        expand_json = resp.json()
    ctx.expand_response = expand_json
    ctx.filter_val = expand_json.get("value", {}) or {}
    ctx.out_cols = valid_columns(ctx.filter_val, cfg.db)

    ctx.plan = None  # force post_expand to rebuild the production plan below
    await _call_hook(post_expand_hook, ctx)
    if ctx.plan is None:
        return

    from utils.dataframe_filtering import join_and_filter_database, NoFilterTermsError
    try:
        new_df, new_stats = join_and_filter_database(
            ctx.data, ctx.plan, cfg.db, ctx.out_cols, ctx.filter_val,
        )
    except NoFilterTermsError:
        return

    if new_df is not None and not new_df.is_empty():
        ctx.df = new_df
        ctx.filter_stats = new_stats
        ctx.log.info("[%s] on_empty corrective retry recovered %d rows",
                     cfg.db, new_df.height)


def _build_on_empty(cfg: SchemaKgConfig) -> Hook:
    retry_prompt = _retry_system_prompt(cfg)
    post_expand_hook = _build_post_expand(cfg)

    async def _on_empty(ctx: WorkerCtx) -> None:
        try:
            await _retry_with_corrective_hint(cfg, ctx, post_expand_hook)
        except Exception as exc:
            ctx.log.warning("[%s] on_empty corrective retry failed: %s -- "
                            "falling through to existing behaviour", cfg.db, exc)
        if ctx.df is not None and not ctx.df.is_empty():
            return  # corrective retry recovered real rows -- done

        try:
            query = ctx.inp.get("cleaned_query", "")
            tables_tried = ctx.plan.get("tables", []) if isinstance(ctx.plan, dict) else []
            filter_summary = {k: v for k, v in ctx.filter_val.items()
                              if v and v not in ("requested",)}
            context = (
                f"Query: {query}\n"
                f"Tables queried: {tables_tried}\n"
                f"Filters applied: {filter_summary}\n"
                f"Result: 0 rows returned."
            )
            decision = await call_orchestrator(cfg.orchestrator_model, retry_prompt, context)
            action = decision.get("action", "retry")
            ctx.log.info("[%s] on_empty orchestrator=%s (%s)", cfg.db, action,
                         str(decision.get("rationale", ""))[:80])

            if action == "web_search":
                answer = await call_web_tool(query)
                if answer:
                    ctx.error_msg = answer
                return
            if action == "direct_answer":
                answer = decision.get("answer", "")
                if answer:
                    ctx.error_msg = answer
                return

            # action == "retry" → DB-specific fallback (e.g. alternate table).
            await _call_hook(cfg.on_empty_fallback, ctx)
        except Exception as exc:
            ctx.log.warning("[%s] on_empty orchestrator failed: %s — using fallback",
                            cfg.db, exc)
            await _call_hook(cfg.on_empty_fallback, ctx)

    return _on_empty


# ── Public factory ─────────────────────────────────────────────────────────────

def make_schema_kg_handler(cfg: SchemaKgConfig) -> Callable:
    """Return a pre-wired `return_<db>_result` handler for a schema_kg DB."""
    return make_db_result_handler(
        db=cfg.db,
        display_name=cfg.display_name,
        get_db=cfg.get_db,
        prompt_md=cfg.prompt_md,
        summarizer_model=cfg.summarizer_model
        or settings.SUMMARIZER_MODEL_NAME,
        use_async_post=cfg.use_async_post,
        intercept=_build_intercept(cfg),
        post_expand=_build_post_expand(cfg),
        pre_join=cfg.pre_join,
        post_join=cfg.post_join,
        on_empty_result=_build_on_empty(cfg),
        sort_order=cfg.sort_order,
    )


__all__ = [
    "SchemaKgConfig", "make_schema_kg_handler",
    "call_orchestrator", "call_web_tool", "web_search_ex",
]
