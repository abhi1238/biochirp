

import os
import sys
import json
import pathlib
import logging
import uuid
import time
import asyncio
from typing import Any, List, Optional, Union, Dict

import httpx
from fastapi import FastAPI, Query, HTTPException
from utils.service_setup import add_open_cors, add_health_endpoint

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from config.settings import get_openrouter_key
from config.guardrail import (
    ExpandMemberOutput,
    QueryInterpreterOutputGuardrail,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
FUZZY_URL = os.getenv("FUZZY_URL", "http://biochirp_fuzzy_tool:8013/fuzzy")
SEMANTIC_URL = os.getenv("SEMANTIC_URL", "http://biochirp_semantic_tool:8015/semantic")
EXPAND_SYNONYMS_URL = os.getenv(
    "EXPAND_SYNONYMS_URL",
    "http://biochirp_synonyms_expander:8014/expand_synonyms"
)

# Unified LLM filter: when enabled, fuzzy/semantic/synonyms each return their
# raw candidate pool, expand_and_match_db unions them per (field, user_term),
# and calls the LLM filter ONCE per (field, user_term). Saves 2 LLM calls per
# term vs. the legacy path (one LLM call inside each of the three services).
UNIFIED_LLM_FILTER = os.getenv("UNIFIED_LLM_FILTER", "1") == "1"
LLM_FILTER_MAX_CONCURRENCY = int(os.getenv("LLM_FILTER_MAX_CONCURRENCY", "4"))
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
UNIFIED_LLM_MODEL = settings.UNIFIED_LLM_MODEL

UNIFIED_LLM_PROMPT_PATH = os.getenv(
    "UNIFIED_LLM_PROMPT_PATH",
    "/app/resources/prompts/semantic_match_agent.md",
)
# Pre-screen cap. qwen2.5-coder:7b is ~2s on 30 candidates, ~5s on 60,
# but jumps to ~57s on 100+ (context-attention cliff). Keep the per-(field,
# user_term) candidate list at most this many items before sending to the
# LLM; ranking is by difflib quick-ratio similarity to the user term.
UNIFIED_LLM_PRESCREEN_TOPK = int(os.getenv("UNIFIED_LLM_PRESCREEN_TOPK", "50"))
_UNIFIED_LLM_PROMPT: Optional[str] = None


def _load_unified_llm_prompt() -> str:
    """Load the filter system prompt once. Falls back to a minimal inline
    prompt if the file is missing (e.g. running outside Docker)."""
    global _UNIFIED_LLM_PROMPT
    if _UNIFIED_LLM_PROMPT is not None:
        return _UNIFIED_LLM_PROMPT
    try:
        with open(UNIFIED_LLM_PROMPT_PATH, "r", encoding="utf-8") as f:
            _UNIFIED_LLM_PROMPT = f.read()
    except Exception as e:
        logger.warning(
            f"[unified-llm] prompt file not found at {UNIFIED_LLM_PROMPT_PATH}: {e}. "
            f"Using minimal inline prompt."
        )
        _UNIFIED_LLM_PROMPT = (
            "You are a deterministic semantic-matching function. Given a Category, "
            "a Term, and a closed List of Strings, return a JSON list containing "
            "ONLY the items from the list that are equivalent entities or ontology "
            "descendants of the term within the category. Return [] if none match."
        )
    return _UNIFIED_LLM_PROMPT

# Timeouts
SERVICE_TIMEOUT_SEC = float(os.getenv("SERVICE_TIMEOUT_SEC", "60"))
OVERALL_TIMEOUT_SEC = float(os.getenv("OVERALL_TIMEOUT_SEC", "90"))
LLM_FILTER_TIMEOUT  = float(os.getenv("LLM_FILTER_TIMEOUT", "45"))

# ── Local reverse synonym map ─────────────────────────────────────────────
# Pre-built from DB parquets: maps {db: {field: {synonym_lower: [canonical...]}}}
# Used as a zero-latency fallback when all three services (fuzzy/semantic/
# external-API synonyms) return nothing for a term. Catches dev-code aliases
# like STI571 → imatinib and brand names like Herceptin → trastuzumab.
_REVERSE_SYNONYM_MAP: dict | None = None
_REVERSE_SYN_MAP_PATH = os.getenv(
    "REVERSE_SYN_MAP_PATH",
    "/app/resources/values/synonym_reverse_map.pkl",
)


def _get_reverse_synonym_map() -> dict:
    global _REVERSE_SYNONYM_MAP
    if _REVERSE_SYNONYM_MAP is not None:
        return _REVERSE_SYNONYM_MAP
    try:
        import pickle
        with open(_REVERSE_SYN_MAP_PATH, "rb") as f:
            _REVERSE_SYNONYM_MAP = pickle.load(f)
        total = sum(
            len(v) for db_fields in _REVERSE_SYNONYM_MAP.values()
            for v in db_fields.values()
        )
        logger.info(f"[reverse-syn] loaded {total} synonym entries from {_REVERSE_SYN_MAP_PATH}")
    except Exception as e:
        logger.warning(f"[reverse-syn] could not load map: {e} — local reverse lookup disabled")
        _REVERSE_SYNONYM_MAP = {}
    return _REVERSE_SYNONYM_MAP


def _lookup_reverse_synonyms(database: str, field: str, terms: list) -> list:
    """Return canonical field values for any input terms found in the
    pre-built reverse synonym map.  Returns [] when the map has no entry."""
    rev = _get_reverse_synonym_map()
    db_map = rev.get(database.lower(), {}).get(field, {})
    if not db_map:
        return []
    found: set = set()
    for term in terms:
        if not isinstance(term, str):
            continue
        t = term.strip().lower()
        if t in db_map:
            found.update(db_map[t])
    return sorted(found)
# Cap expand_synonyms separately — it's an optional enrichment; if slow or
# disconnected we fall back gracefully to fuzzy+semantic results alone.
# Must exceed the expander's inner KB-fetch budget (HTTP_TIMEOUT_SEC=12s, run
# concurrently across HGNC/NCBI/OpenTargets) — a 6s cap tripped before the KBs
# returned, dropping even the literal term for gene/drug (KB-only) fields and
# yielding intermittent "no results". 30s covers the concurrent fetch + margin;
# the KB-literal fallback below guarantees non-empty even if this still trips.
EXPAND_SYNONYMS_TIMEOUT_SEC = float(os.getenv("EXPAND_SYNONYMS_TIMEOUT_SEC", "30"))

# Valid databases
VALID_DATABASES = set(os.getenv("VALID_DATABASES", "CLINVAR,CTD,HCDT,HPO,MSIGDB,ORPHANET,REACTOME,STRING,TTD,UNIPROT").split(","))

# HTTP client (reused across requests)
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client."""
    global _http_client
    
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(SERVICE_TIMEOUT_SEC),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
        )
        logger.info("Created shared HTTP client")
    
    return _http_client


# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Expand and Match Database Service",
    version="1.0.0",
    description="API for Expand and Match Database Service"
)

# Add CORS middleware
add_open_cors(app)
@app.on_event("startup")
async def startup_event():
    """Pre-create HTTP client on startup."""
    await get_http_client()
    logger.info("Expand and Match Database service started")


@app.on_event("shutdown")
async def shutdown_event():
    """Close HTTP client on shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("Closed HTTP client")


def union_of_lists(
    *args: Optional[Union[List[Any], str]]
) -> Optional[Union[List[Any], str]]:
    """
    Set-union and normalization across lists/strings/None.
    
    Args:
        *args: Variable number of lists, strings, or None
        
    Returns:
        Combined and normalized list, string, or None
    """
    if not args or all(a is None for a in args):
        return None
    
    has_list = any(isinstance(a, list) for a in args)

    if has_list:
        # BUG FIX (2026-05-14): when ANY arg is a list, we were silently
        # dropping bare-string args from the union (semantic tool returns
        # e.g. "metformin" instead of ["metformin"]). That made downstream
        # filter_value empty even when one of the three matchers had found
        # the term. Now: coerce strings to single-item lists before union.
        out: List[Any] = []
        for a in args:
            if isinstance(a, list):
                items = a
            elif isinstance(a, str):
                items = [a]
            else:
                continue  # None / other → skip
            for x in items:
                normalized = x.lower() if isinstance(x, str) else x
                if normalized:  # Skip empty strings
                    out.append(normalized)
        return out
    
    strings = [a for a in args if isinstance(a, str)]
    non_none_non_str = [a for a in args if (a is not None and not isinstance(a, str))]
    
    if strings and all(s.lower() == "requested" for s in strings) and not non_none_non_str:
        return "requested"
    
    return [s.lower() for s in strings if s]


# Fields with authoritative synonym KBs (HGNC for genes, WHO INN/DrugBank for
# drugs). These bypass fuzzy + semantic AND bypass the LLM filter entirely.
# Rationale: gene symbols (BRCA1/BRCA2 differ by 1 char) and drug INNs
# (tramadol/toradol) carry high fuzzy-collision risk with pharmacologically or
# biologically distinct entities — a wrong match here causes wrong biology.
# target_name is intentionally excluded: it is free-text (e.g. "Epidermal
# growth factor receptor") and needs fuzzy + semantic coverage.
KB_ONLY_FIELDS: frozenset = frozenset({
    "drug_name",
    "gene_name",
    "gene_symbol",
    "enzyme_genesymbol",
    "substrate_genesymbol",
    # STRING uses table-prefixed gene symbol columns; concept_type.json maps these
    # to "gene_symbol" but that file isn't mounted in this container, so list them
    # explicitly so they get KB-only treatment (expand_synonyms + alias_map, no LLM).
    "association_gene_symbol",
    "association_partner_gene_symbol",
    "physical_gene_symbol",
    "physical_partner_gene_symbol",
    "channel_gene_symbol",
    "channel_partner_gene_symbol",
})

# Concept types (from schema_kg/inputs/{db}/concept_type.json) that must use
# KB-only expansion. Any column whose concept_type matches one of these bypasses
# fuzzy / semantic / LLM — regardless of what the column is named on disk.
# This lets DBs use table-prefixed column names (e.g. association_gene_symbol)
# without breaking gene/drug synonym expansion.
KB_CONCEPT_TYPES: frozenset = frozenset({"gene_symbol", "drug_name"})

# Mounted path to per-DB concept_type.json files.
_CONCEPT_TYPE_DIR = os.getenv("CONCEPT_TYPE_DIR", "/app/schema_kg/inputs")

# Per-DB cache: {db_lower: frozenset of unqualified column names that are KB-eligible}
_KB_COLS_CACHE: dict = {}


def _get_kb_cols(database: str) -> frozenset:
    """Return unqualified column names whose concept_type is KB-eligible for this DB.

    Loads schema_kg/inputs/{db}/concept_type.json once per DB and caches.
    Falls back to the hardcoded KB_ONLY_FIELDS when the file is absent or unreadable,
    so existing DBs without concept_type.json continue to work unchanged.
    """
    db_lower = database.lower()
    if db_lower in _KB_COLS_CACHE:
        return _KB_COLS_CACHE[db_lower]

    path = pathlib.Path(_CONCEPT_TYPE_DIR) / db_lower / "concept_type.json"
    kb_cols: set = set()
    try:
        with open(path) as fh:
            ct = json.load(fh)
        for fq_col, ctype in ct.items():
            if isinstance(fq_col, str) and isinstance(ctype, str):
                col_name = fq_col.rsplit(".", 1)[-1]  # last segment: db.table.col → col
                if ctype in KB_CONCEPT_TYPES:
                    kb_cols.add(col_name)
        logger.info(f"[concept-type] {db_lower}: KB cols from concept_type.json = {sorted(kb_cols)}")
    except Exception as exc:
        logger.warning(
            f"[concept-type] could not load {path}: {exc} — "
            f"falling back to hardcoded KB_ONLY_FIELDS"
        )
        kb_cols = set(KB_ONLY_FIELDS)

    result = frozenset(kb_cols)
    _KB_COLS_CACHE[db_lower] = result
    return result

# Fields whose canonicalisation must NOT pass through the LLM filter — IDs,
# cross-refs, enum-style categoricals, and a few drug/synonym fields where the
# LLM judge has been observed to reject valid exact matches. Kept in sync with
# STRUCTURED_FIELDS_BYPASS_LLM in fuzzy and _SEMANTIC_LLM_BYPASS in semantic.
STRUCTURED_FIELDS_BYPASS_LLM: set = {
    "uniprot_xref", "uniprot_id",
    "pubchem_cid", "pubchem_sid",
    "cas_number", "cas",
    "chebi_xref", "chebi_id",
    "superdrug_atc", "superdrug_cas",
    "drug_compound_id",
    "entrez_id", "ensembl_id", "hgnc_id",
    "gene_partner_id", "protein_partner_id", "substrate_id",
    "activity_type", "activity_operator", "activity_unit",
    "target_type", "approval_status",
    "evidence_type", "evidence_direction", "evidence_level",
    "clinical_significance", "review_status", "variant_type",
    "regulation_type", "organism", "species", "collection",
    "locus_type", "locus_group", "tier", "role_in_cancer", "gene_type",
    "xref_db",
    "formula",
    "synonym",
}

# Exact-first free-text fields: long free-text NAME columns where one value is a
# substring of another (e.g. "Signaling by EGFR" ⊂ "Signaling by EGFR in Cancer").
# When the user's term is an EXACT member of the DB value pool, lock onto it and
# bypass fuzzy/semantic/LLM — that pipeline non-deterministically broadens to
# superstring variants (same query → 56 vs 2915 rows run-to-run). Falls through to
# the normal free-text path when there is NO exact hit, so paraphrases still work.
# Env-overridable (comma-separated). Unlike STRUCTURED_FIELDS_BYPASS_LLM, this is a
# per-VALUE decision (only exact hits bypass), not a blanket per-field one.
EXACT_FIRST_FIELDS: set = {
    f.strip() for f in os.getenv("EXACT_FIRST_FIELDS", "pathway_name").split(",") if f.strip()
}


def _exact_pool_match(database: str, field: str, raw_val) -> Optional[list]:
    """Return canonical-cased exact matches iff EVERY user term for ``field`` is an
    exact (case-insensitive) member of the DB value pool; else ``None`` so the
    caller falls through to the fuzzy/semantic/LLM path. Conservative on purpose:
    a single non-exact term defers the whole field to fuzzy matching."""
    terms = raw_val if isinstance(raw_val, list) else [raw_val]
    terms = [t for t in terms if isinstance(t, str) and t.strip()]
    if not terms:
        return None
    try:
        from utils.concept_values import get_db_concept_values
        pool = (get_db_concept_values(database) or {}).get(field) or []
    except Exception:
        return None
    lc_map = {v.lower(): v for v in pool if isinstance(v, str)}
    out, seen = [], set()
    for t in terms:
        canon = lc_map.get(t.strip().lower())
        if canon is None:
            return None  # any non-exact term → use the full fuzzy/LLM path
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _prescreen_candidates(
    term: str, candidates: List[str], top_k: int
) -> List[str]:
    """Rank candidates by character-level similarity to the user term and
    return the top_k. Cheap stdlib-only pre-screen so the LLM only sees a
    short, plausible list. Always includes any candidate that contains the
    term as a substring (case-insensitive)."""
    if not term or not candidates or top_k <= 0:
        return list(candidates)[:top_k] if top_k > 0 else []
    if len(candidates) <= top_k:
        return list(candidates)
    from difflib import SequenceMatcher
    t = term.strip().lower()
    t_tokens = set(t.split())
    scored: List[tuple] = []
    for c in candidates:
        if not isinstance(c, str):
            continue
        c_lc = c.lower()
        # Boost: substring containment in either direction
        contains = 1.0 if (t in c_lc or c_lc in t) else 0.0
        # Cheap quick_ratio (upper bound on real ratio, very fast)
        sm = SequenceMatcher(None, t, c_lc)
        qr = sm.quick_ratio()
        # Token overlap as a secondary signal (cheap)
        c_tokens = set(c_lc.split())
        tok = (
            len(t_tokens & c_tokens) / max(1, len(t_tokens | c_tokens))
            if t_tokens else 0.0
        )
        score = contains * 2.0 + qr + 0.5 * tok
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def _parse_llm_list_output(text: str) -> List[str]:
    """Parse an LLM response into a list of strings. Tolerates code fences,
    plain JSON, and Python literal lists."""
    import json as _json
    import ast as _ast
    import re as _re
    if not text:
        return []
    cleaned = text.strip()
    # Strip markdown code fences
    cleaned = _re.sub(r"^```(?:json|python)?\s*", "", cleaned)
    cleaned = _re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
    # Strip <think>...</think> blocks (reasoning models)
    cleaned = _re.sub(r"<think>.*?</think>", "", cleaned, flags=_re.DOTALL).strip()
    try:
        out = _json.loads(cleaned)
        if isinstance(out, list):
            return out
    except Exception:
        pass
    try:
        out = _ast.literal_eval(cleaned)
        if isinstance(out, (list, tuple, set)):
            return list(out)
    except Exception:
        pass
    # Last resort — split on newlines
    return [line.strip().strip(',"\'') for line in cleaned.splitlines() if line.strip()]


def _filter_and_dedup_against_candidates(parsed: List[str], candidates: List[str]) -> List[str]:
    """Keep only items that appear in *candidates* (case-insensitive) and deduplicate."""
    cand_lc = {s.lower(): s for s in candidates if isinstance(s, str)}
    filtered = [cand_lc[p.lower()] for p in parsed if isinstance(p, str) and p.lower() in cand_lc]
    seen: set = set()
    out: List[str] = []
    for s in filtered:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


async def _call_unified_llm_filter_direct(
    client: httpx.AsyncClient,
    field_name: str,
    single_term: str,
    candidates: List[str],
    request_id: str,
    model_override: Optional[str] = None,
    db_name: str = "",
) -> List[str]:
    """Call LiteLLM directly with the local filter model, bypassing the
    `llm_member_filter` ensemble service (Grok + Semantic + paid fallback).
    For the unified path the candidate list is already a deduped union of
    fuzzy + semantic + synonyms — a single local-model pass is enough.

    No per-DB LLM rule is injected here: this unified candidate filter judges
    semantic/fuzzy/synonym candidates and is deliberately left rule-free. The
    per-DB `tiebreaker` rule belongs to the dual-mapper disagreement resolver in
    the schema_mapper service, not this filter.
    """
    if not candidates:
        return []
    system_prompt = _load_unified_llm_prompt()
    user_prompt = (
        f"Category: {field_name}, Term: {single_term}, "
        f"List of Strings: {list(candidates)}"
    )
    body = {
        "model": model_override or UNIFIED_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": 2000,
    }
    try:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {get_openrouter_key(db_name)}"},
            timeout=LLM_FILTER_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_llm_list_output(content)
        # Post-filter: keep only items that exist in the original candidate list
        # (case-insensitive). Prevents the model from inventing strings.
        return _filter_and_dedup_against_candidates(parsed, candidates)
    except Exception as e:
        logger.warning(
            f"[{request_id}] [UNIFIED-LLM-DIRECT] field={field_name} "
            f"term='{single_term}' failed: {e!r}"
        )
        return []


async def _apply_unified_llm_filter(
    client: httpx.AsyncClient,
    combined_member: Dict[str, Any],
    parsed_value: Dict[str, Any],
    request_id: str,
    fuzzy_outputs: Optional[Dict[str, Any]] = None,
    db_name: str = "",
) -> Dict[str, Any]:
    """Run the unified LLM filter on each (field, user_term) pair in parallel.

    Implements disjoint-population validation per the diagram:
      Population A — fuzzy candidates (char-similarity ≥ 0.9 by service design).
                     These are string-similar; LLM checks for homograph collisions.
      Population B — everything else (semantic + KB-synonym candidates that did
                     not pass the fuzzy threshold). Conceptually similar; LLM
                     checks for true synonymy.

    Both populations run concurrently inside one asyncio.gather. Results for
    the same field are merged into a single bucket — the caller sees one list.
    When fuzzy_outputs is absent or empty (e.g. KB-only queries), all candidates
    land in Population B and a single job per (field, term) is created.

    KB_ONLY_FIELDS (drug/gene) are never present in combined_member at this
    point — they were separated into kb_direct before this call.
    Structured fields (IDs, enums) are skipped as before.
    """
    sem = asyncio.Semaphore(max(1, LLM_FILTER_MAX_CONCURRENCY))

    async def _filtered_call(field: str, term: str, candidates: List[str]) -> List[str]:
        async with sem:
            return await _call_unified_llm_filter_direct(
                client, field, term, candidates, request_id, db_name=db_name,
            )

    # Pre-build lowercase fuzzy candidate sets per field for O(1) membership checks.
    fuzzy_sets: Dict[str, set] = {}
    if fuzzy_outputs:
        for f, vals in fuzzy_outputs.items():
            if isinstance(vals, list):
                fuzzy_sets[f] = {v.lower() for v in vals if isinstance(v, str)}

    # Build the list of (field, term, candidates) work-items.
    # Each field/term pair produces at most TWO jobs — one per population —
    # both submitted to asyncio.gather so they run concurrently.
    jobs: List[tuple] = []
    for field, candidates in combined_member.items():
        if not isinstance(candidates, list) or not candidates:
            continue
        if field in STRUCTURED_FIELDS_BYPASS_LLM:
            # Already-tight candidates; LLM judge is harmful here. Keep as-is.
            continue
        # Heuristic bypass: any *_id field (e.g. gene_id, pathway_id,
        # target_id) is an opaque structured identifier — running the
        # LLM filter over numeric IDs is wasteful and can stall the
        # whole expand_and_match_db request when the upstream DB
        # returns hundreds of IDs (observed: wikipathways gene_id).
        if field == "id" or field.endswith("_id"):
            continue
        user_terms_raw = parsed_value.get(field)
        if isinstance(user_terms_raw, str):
            user_terms = [user_terms_raw]
        elif isinstance(user_terms_raw, list):
            user_terms = [t for t in user_terms_raw if isinstance(t, str) and t.strip()]
        else:
            user_terms = []
        if not user_terms:
            continue

        # Disjoint split: Population A = fuzzy candidates; Population B = the rest.
        fset = fuzzy_sets.get(field, set())
        if fset:
            pop_a = [c for c in candidates if c in fset]
            pop_b = [c for c in candidates if c not in fset]
        else:
            # No fuzzy data available — treat all candidates as Population B.
            pop_a, pop_b = [], list(candidates)

        for term in user_terms:
            if pop_a:
                screened_a = _prescreen_candidates(term, pop_a, UNIFIED_LLM_PRESCREEN_TOPK)
                if len(screened_a) < len(pop_a):
                    logger.info(
                        f"[{request_id}] [UNIFIED-LLM] prescreen "
                        f"field={field} term='{term}' pop=fuzzy: "
                        f"{len(pop_a)} -> {len(screened_a)} candidates"
                    )
                jobs.append((field, term, screened_a))
            if pop_b:
                screened_b = _prescreen_candidates(term, pop_b, UNIFIED_LLM_PRESCREEN_TOPK)
                if len(screened_b) < len(pop_b):
                    logger.info(
                        f"[{request_id}] [UNIFIED-LLM] prescreen "
                        f"field={field} term='{term}' pop=semantic: "
                        f"{len(pop_b)} -> {len(screened_b)} candidates"
                    )
                jobs.append((field, term, screened_b))

    if not jobs:
        logger.info(f"[{request_id}] [UNIFIED-LLM] no fields to filter")
        return combined_member

    logger.info(
        f"[{request_id}] [UNIFIED-LLM] running {len(jobs)} LLM filter calls "
        f"across {len({f for f, _, _ in jobs})} fields"
    )

    results = await asyncio.gather(
        *(_filtered_call(f, t, c) for (f, t, c) in jobs),
        return_exceptions=True,
    )

    # Bucket results per field, dedupe (case-insensitive, preserving lower-case form).
    per_field: Dict[str, set] = {}
    for (field, term, candidates_for_job), res in zip(jobs, results):
        if isinstance(res, Exception):
            logger.warning(
                f"[{request_id}] [UNIFIED-LLM] field={field} term='{term}' "
                f"exception: {res}"
            )
            # Don't skip exact-match recovery just because the LLM call failed.
            res = []
        if not isinstance(res, list):
            res = []
        bucket = per_field.setdefault(field, set())
        for item in res:
            if isinstance(item, str) and item.strip():
                bucket.add(item.strip().lower())
        # Deterministic safety net: an LLM filter can hallucinate-OUT (drop a
        # candidate that is an exact match for the user term). Force-include
        # any candidate whose normalized form == normalized term. This is
        # exact equality only — NOT substring — so zero false positives:
        # "Tuberculosis" matches term "tuberculosis" but "tuberculin skin
        # test" does not.
        t_norm = term.strip().lower() if isinstance(term, str) else ""
        if t_norm:
            recovered = 0
            for cand in candidates_for_job:
                if not isinstance(cand, str):
                    continue
                c_norm = cand.strip().lower()
                if c_norm == t_norm and c_norm not in bucket:
                    bucket.add(c_norm)
                    recovered += 1
            if recovered:
                logger.info(
                    f"[{request_id}] [UNIFIED-LLM] field={field} term='{term}' "
                    f"exact-match safety net recovered {recovered} "
                    f"candidate(s) the LLM dropped"
                )

    # Replace combined_member values for filtered fields. If LLM returned
    # nothing for a field, drop to an empty list rather than keeping the
    # un-filtered union — same behaviour as the legacy per-service path.
    for field in {f for f, _, _ in jobs}:
        combined_member[field] = sorted(per_field.get(field, set()))

    return combined_member


async def call_service(
    label: str,
    url: str,
    client: httpx.AsyncClient,
    params: Dict[str, str],
    body: Dict[str, Any],
    request_id: str,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Helper to call a downstream service, logs timing + errors."""
    start = time.perf_counter()

    try:
        logger.info(f"[{request_id}] [{label.upper()}] POST {url} params={params}")

        call_timeout = httpx.Timeout(timeout) if timeout is not None else None
        resp = await client.post(url, params=params, json=body, timeout=call_timeout)
        resp.raise_for_status()
        
        data = resp.json()
        
        # Count entries heuristically
        n_entries = 0
        if isinstance(data, dict) and "value" in data and isinstance(data["value"], dict):
            n_entries = sum(
                len(v) if isinstance(v, list) else 1
                for v in data["value"].values()
            )
        
        elapsed = time.perf_counter() - start
        logger.info(
            f"[{request_id}] [{label.upper()}] SUCCESS ({n_entries} entries) "
            f"elapsed={elapsed:.2f}s"
        )
        
        return {
            "value": data.get("value", {}),
            "__elapsed__": elapsed
        }
        
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start
        error_msg = f"Timeout after {SERVICE_TIMEOUT_SEC}s"
        logger.error(
            f"[{request_id}] [{label.upper()}] TIMEOUT after {elapsed:.2f}s"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }
        
    except httpx.HTTPStatusError as e:
        elapsed = time.perf_counter() - start
        error_msg = f"HTTP {e.response.status_code}"
        logger.error(
            f"[{request_id}] [{label.upper()}] FAILED with {error_msg} "
            f"after {elapsed:.2f}s"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }
        
    except Exception as e:
        elapsed = time.perf_counter() - start
        error_msg = repr(e)
        logger.exception(
            f"[{request_id}] [{label.upper()}] FAILED after {elapsed:.2f}s: {e}"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }


@app.get("/")
def root():
    """Root endpoint."""
    return {"message": "Expand and Match Database service tool is running"}


add_health_endpoint(app)
@app.post("/expand_and_match_db", response_model=ExpandMemberOutput)
async def expand_and_match_db(
    input: QueryInterpreterOutputGuardrail,
    database: str = Query(..., description="Which DB to use (clinvar, ctd, hcdt, hpo, msigdb, orphanet, reactome, string, ttd, uniprot)")
):
    """
    Expand and match database endpoint.
    
    Calls three services in parallel:
      - Fuzzy matching
      - Semantic similarity
      - Synonym expansion
    
    Combines results and returns unified output.
    
    Args:
        input: Query interpreter output with parsed values
        database: Target database name (case insensitive)
        
    Returns:
        ExpandMemberOutput with combined results from all services
    """
    tool = "expand_and_match_db"
    request_id = str(uuid.uuid4())
    overall_start = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] [START] database={database}")

    # Input validation
    if not input:
        raise HTTPException(status_code=400, detail="Input is required")
    
    # Validate database
    database_upper = database.strip().upper()
    if database_upper not in VALID_DATABASES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid database '{database}'. Valid options: {', '.join(sorted(VALID_DATABASES))}"
        )
    
    # Convert input to dict
    try:
        input_filtered = input.model_dump(exclude_none=True)
    except Exception as e:
        logger.error(f"[{tool}][{request_id}] Failed to dump input: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid input: {str(e)}")
    
    # Empty / missing parsed_value: the interpreter extracted no concrete
    # entities (typical for generic "what … in DB" queries). Don't 400 — the
    # legitimate behaviour is "no entities to expand, downstream DB tool
    # will see an empty filter and return No rows matched / scope-narrowed
    # response." Returning 200 + empty value keeps the per-DB error path
    # accurate (no more mislabelling 400 as 'Expand … unreachable').
    if not input_filtered.get("parsed_value"):
        logger.info(f"[{tool}][{request_id}] Empty parsed_value — returning empty expansion result")
        # value MUST be {} (empty dict), not None — planner.py:24 does
        # `fo["value"].items()` which raises AttributeError on None.
        return ExpandMemberOutput(
            database=database,
            value={},
            tool=tool,
            message="parsed_value was empty — nothing to expand",
        )

    # Short-circuit "requested" wildcard values — they are not real terms and must be
    # passed through as-is so downstream _valid_columns() can include them as output columns.
    pv = input_filtered.get("parsed_value", {})
    requested_passthrough = {k: "requested" for k, v in pv.items() if v == "requested"}
    if requested_passthrough:
        # Remove wildcard fields from parsed_value before expansion so services don't
        # waste time searching for the literal string "requested".
        for k in requested_passthrough:
            pv.pop(k, None)
        input_filtered["parsed_value"] = pv
        logger.info(f"[{tool}][{request_id}] [REQUESTED PASSTHROUGH] {list(requested_passthrough.keys())}")

    logger.info(f"[{tool}][{request_id}] [INPUT] {input_filtered}")

    # FIX: Normalize database to lowercase for services
    database_lower = database_upper.lower()
    params = {"database": database_lower}

    # Resolve KB-eligible columns for this DB from concept_type.json.
    # Falls back to hardcoded KB_ONLY_FIELDS if the file is absent.
    _kb_cols = _get_kb_cols(database_lower)

    logger.info(
        f"[{tool}][{request_id}] Calling services with database='{database_lower}' "
        f"(original: '{database}')"
    )

    # Get HTTP client
    client = await get_http_client()

    # When UNIFIED_LLM_FILTER is on, each downstream service returns its raw
    # candidate pool (no LLM filtering); we union and run ONE LLM filter call
    # per (field, user_term) below.
    service_params = dict(params)
    if UNIFIED_LLM_FILTER:
        service_params["raw"] = "true"

    # ── Entity-type routing ────────────────────────────────────────────────
    # Split parsed_value into THREE processing pools:
    #
    # 1. KB_ONLY_FIELDS (drug/gene symbols) → expand_synonyms only
    #    Rationale: authoritative KBs (WHO INN, HGNC); fuzzy collisions risky.
    #
    # 2. STRUCTURED_FIELDS_BYPASS_LLM (IDs/enums) → ID passthrough only
    #    Rationale: opaque identifiers (HP:0000001, rs1234, etc.) need exact
    #    case-insensitive matching, not fuzzy/semantic/LLM judgment.
    #
    # 3. FREE-TEXT fields (disease/target/pathway) → full pipeline
    #    Rationale: synonyms and semantic similarity needed.
    #
    kb_only_keys = frozenset(k for k in pv if k in _kb_cols)
    structured_keys = frozenset(k for k in pv if k in STRUCTURED_FIELDS_BYPASS_LLM)
    fuzzy_sem_pv = {
        k: v for k, v in pv.items()
        if k not in _kb_cols and k not in STRUCTURED_FIELDS_BYPASS_LLM
    }

    # Build a payload for fuzzy/semantic that contains only free-text fields.
    fuzzy_sem_input = (
        {**input_filtered, "parsed_value": fuzzy_sem_pv}
        if fuzzy_sem_pv else None
    )
    if kb_only_keys:
        logger.info(
            f"[{tool}][{request_id}] [KB-ONLY] fields={sorted(kb_only_keys)} "
            f"→ expand_synonyms only, bypassing fuzzy/semantic/LLM"
        )
    if structured_keys:
        logger.info(
            f"[{tool}][{request_id}] [STRUCTURED] fields={sorted(structured_keys)} "
            f"→ exact case-insensitive match only, bypassing fuzzy/semantic/LLM"
        )

    # expand_synonyms always runs (handles all field types).
    # fuzzy + semantic only run when there are non-KB fields to match.
    tasks: Dict[str, Any] = {
        "expand_synonyms": call_service(
            "expand_synonyms", EXPAND_SYNONYMS_URL, client, service_params, input_filtered, request_id,
            timeout=EXPAND_SYNONYMS_TIMEOUT_SEC,
        ),
    }
    if fuzzy_sem_input:
        tasks["fuzzy"] = call_service(
            "fuzzy", FUZZY_URL, client, service_params, fuzzy_sem_input, request_id
        )
        tasks["semantic"] = call_service(
            "semantic", SEMANTIC_URL, client, service_params, fuzzy_sem_input, request_id
        )
    else:
        logger.info(
            f"[{tool}][{request_id}] [KB-ONLY] all fields are KB-only — "
            f"skipping fuzzy and semantic services entirely"
        )
    
    # FIX: Add overall timeout
    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=OVERALL_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        overall_elapsed = time.perf_counter() - overall_start
        error_msg = f"Overall timeout ({OVERALL_TIMEOUT_SEC}s) exceeded"
        logger.error(
            f"[{tool}][{request_id}] [TIMEOUT] {error_msg} after {overall_elapsed:.2f}s"
        )
        
        return ExpandMemberOutput(
            database=database_upper,  # Return original case
            value={},
            tool=tool,
            message=error_msg,
            errors={"overall": error_msg}
        )
    
    # Handle any exceptions from gather
    processed_results = []
    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            service_name = list(tasks.keys())[i]
            logger.error(
                f"[{tool}][{request_id}] [{service_name.upper()}] Exception: {result}"
            )
            processed_results.append({
                "__error__": repr(result),
                "__elapsed__": 0
            })
        else:
            processed_results.append(result)
    
    service_outputs = dict(zip(tasks.keys(), processed_results))
    
    # Log elapsed for each service
    for name, result in service_outputs.items():
        elapsed = result.get("__elapsed__")
        if elapsed is not None:
            logger.debug(f"[{request_id}] [{name.upper()}] elapsed={elapsed:.2f}s")
    
    # Build counts + errors
    counts = {}
    error_log: Dict[str, str] = {}
    
    for name, result in service_outputs.items():
        count = 0
        if isinstance(result, dict) and "value" in result:
            count = sum(
                len(v) if isinstance(v, list) else 1
                for v in result["value"].values()
            )
        counts[name] = count
        
        if isinstance(result, dict) and "__error__" in result:
            error_log[name] = result["__error__"]
        
        logger.info(f"[{tool}][{request_id}] [{name.upper()}] returned {count} entries")
    
    if error_log:
        logger.warning(f"[{tool}][{request_id}] [PARTIAL ERRORS] {error_log}")
    
    # Combine results into three separate pools:
    #   kb_direct:        KB-only fields (drug/gene symbols) — expand_synonyms only
    #   structured_exact: Structured fields (IDs/enums) — exact case-insensitive match
    #   combined_member:  Free-text fields — full pipeline + LLM filter
    kb_direct: Dict[str, Any] = {}
    structured_exact: Dict[str, Any] = {}
    combined_member: Dict[str, Any] = {}
    # syn_direct (2026-06-22): for FREE-TEXT fields (e.g. disease_name), the
    # synonym expander's output is already DB-overlap-matched to DB-canonical
    # values (filter_candidates_by_db). Those are trusted and unioned into the
    # final result DIRECTLY — they must NOT be dropped by the LLM filter. Fuzzy
    # + semantic candidates still flow through combined_member → LLM as before;
    # this lane only guarantees the authoritative synonym hits always survive.
    syn_direct: Dict[str, Any] = {}
    parsed_value = input_filtered.get("parsed_value", {})

    synonyms_outputs = (service_outputs.get("expand_synonyms") or {}).get("value", {})
    fuzzy_outputs    = (service_outputs.get("fuzzy")           or {}).get("value", {})
    semantic_outputs = (service_outputs.get("semantic")        or {}).get("value", {})

    for key in parsed_value.keys():
        synonyms_val   = synonyms_outputs.get(key)
        fuzzy_val      = fuzzy_outputs.get(key)
        similarity_val = semantic_outputs.get(key)

        if key in _kb_cols:
            # Authoritative KB result — trust it, skip fuzzy/semantic/LLM.
            if isinstance(synonyms_val, list):
                kb_direct[key] = sorted(
                    {s.lower() for s in synonyms_val if isinstance(s, str) and s}
                )
            elif isinstance(synonyms_val, str) and synonyms_val and synonyms_val != "requested":
                kb_direct[key] = [synonyms_val.lower()]
            else:
                kb_direct[key] = []
        elif key in STRUCTURED_FIELDS_BYPASS_LLM:
            # Structured field (ID/enum) — exact case-insensitive match only.
            # IDs never go through fuzzy/semantic; just normalize case.
            raw_val = parsed_value.get(key)
            if raw_val is None or raw_val == "requested":
                structured_exact[key] = raw_val
            elif isinstance(raw_val, list):
                structured_exact[key] = sorted(
                    {v.lower() for v in raw_val if isinstance(v, str) and v.strip()}
                )
            elif isinstance(raw_val, str) and raw_val.strip() and raw_val != "requested":
                structured_exact[key] = [raw_val.lower()]
            else:
                structured_exact[key] = []
            logger.info(
                f"[{tool}][{request_id}] [STRUCTURED] {key} = {structured_exact[key]} "
                f"(exact case-insensitive match)"
            )
        elif key in EXACT_FIRST_FIELDS and (
            (_exact_first := _exact_pool_match(database_lower, key, parsed_value.get(key))) is not None
        ):
            # Exact-first: the user's term IS an exact DB value, so lock onto it and
            # bypass fuzzy/semantic/LLM broadening (which non-deterministically pulls
            # in superstring variants, e.g. "Signaling by EGFR" → "...in Cancer").
            structured_exact[key] = _exact_first
            logger.info(
                f"[{tool}][{request_id}] [EXACT-FIRST] {key} = {_exact_first} "
                f"→ exact pool match, bypassing fuzzy/semantic/LLM"
            )
        else:
            # Free-text field — disjoint pipeline:
            #   fuzzy candidates (score ≥ threshold, sorted desc) → LLM Pop A
            #   synonym candidates not already in fuzzy             → LLM Pop A
            #   semantic candidates not in fuzzy OR synonyms        → LLM Pop B
            # The two populations are completely disjoint: every candidate
            # appears in exactly one of (fuzzy∪synonyms) or (semantic-only).
            def _norm_list(v) -> list:
                if isinstance(v, list):
                    return [c.lower() for c in v if isinstance(c, str) and c.strip()]
                return []

            fuzzy_norm = _norm_list(fuzzy_val)       # already score-sorted (fuzzy_match.py)
            syn_norm   = _norm_list(synonyms_val)
            sem_raw    = _norm_list(similarity_val)

            fuzzy_set  = set(fuzzy_norm)
            syn_dedup  = [c for c in syn_norm  if c not in fuzzy_set]
            high_set   = fuzzy_set | set(syn_dedup)
            sem_only   = [c for c in sem_raw   if c not in high_set]

            # Preserve order: fuzzy (score-desc) → synonyms → semantic-only
            combined_member[key] = list(dict.fromkeys(fuzzy_norm + syn_dedup + sem_only))

            # Trust the synonym-expander output directly (2026-06-22): these are
            # DB-overlap-matched DB-canonical values, so they are unioned back in
            # AFTER the LLM filter and can never be dropped by it. The LLM still
            # judges the full fuzzy+syn+semantic pool above (unchanged); this only
            # guarantees the authoritative synonym hits always survive.
            if syn_norm:
                syn_direct[key] = list(dict.fromkeys(syn_norm))

    # ── Opaque-identifier passthrough (2026-05-23, updated 2026-06-18) ────────
    # For structured-ID fields (HP:NNN, R-HSA-NNN, WPNNN, rsNNN, MONDO:NNN,
    # ORPHA:NNN, P-accessions, …) that are NOT in STRUCTURED_FIELDS_BYPASS_LLM,
    # the fuzzy/synonym/semantic vocabularies are empty or too large to match against.
    # The union step would leave combined_member[<id_field>] = []. The planner then
    # drops the field (planner.py: `len(v) > 0` gate) and the user's literal ID
    # never reaches a DataFrame filter — query returns wrong/empty rows with no error.
    #
    # Fix: when the user supplied a concrete value (not "requested") for an ID field,
    # echo the literal lowercased value. The downstream equality filter in
    # app/utils/dataframe_filtering.py lowercases both sides so case-insensitive
    # matching still works.
    #
    # NOTE: Fields in STRUCTURED_FIELDS_BYPASS_LLM are already handled by the
    # structured_exact pool above (exact case-insensitive match). This fallback
    # only applies to IDs that don't have an explicit bypass rule.
    _OPAQUE_ID_EXTRA = {
        "accession", "entry_name", "uniprot_accession", "uniprot_xref",
        "rsid", "pmid",
        "pubchem_cid", "pubchem_sid",
        "cas_number", "cas", "superdrug_cas", "superdrug_atc",
        "chebi_xref",
        "formula",
    }
    for key, raw in parsed_value.items():
        if key in _kb_cols:
            continue  # KB fields are in kb_direct
        if key in STRUCTURED_FIELDS_BYPASS_LLM:
            continue  # Structured fields already in structured_exact
        if combined_member.get(key):
            continue  # matcher already found candidates — keep them
        if not (key == "id" or key.endswith("_id") or key in _OPAQUE_ID_EXTRA):
            continue  # not an ID field
        if raw is None or raw == "requested":
            continue  # no concrete value
        vals = raw if isinstance(raw, list) else [raw]
        passthrough_vals = sorted({
            v.strip().lower() for v in vals
            if isinstance(v, str) and v.strip() and v != "requested"
        })
        if passthrough_vals:
            combined_member[key] = passthrough_vals
            logger.info(
                f"[{tool}][{request_id}] [ID-PASSTHROUGH-FALLBACK] field={key} "
                f"values={passthrough_vals} (no vocab match, echoing literal)"
            )

    # Unified LLM filter: one LLM call per (field, user_term) against the
    # union of raw fuzzy + semantic + synonyms candidates. Saves 2 LLM calls
    # per term vs. the legacy path. Skips structured fields (IDs/categoricals)
    # which never benefited from the LLM judge.
    if UNIFIED_LLM_FILTER:
        try:
            combined_member = await _apply_unified_llm_filter(
                client, combined_member, parsed_value, request_id,
                fuzzy_outputs=fuzzy_outputs,
                db_name=database,
            )
        except Exception as e:
            logger.exception(
                f"[{tool}][{request_id}] [UNIFIED-LLM] failed: {e}"
            )

    # ── Trusted-synonym union (2026-06-22) ────────────────────────────────
    # Re-add the synonym expander's DB-overlap (DB-canonical) candidates for
    # free-text fields AFTER the LLM filter, so authoritative synonym hits are
    # never dropped by the (non-deterministic) LLM judge. Fuzzy + semantic
    # results remain LLM-filtered; this only unions the trusted synonym values
    # back on top. Order: LLM-kept results first, then any synonym hits the LLM
    # dropped. Fixes e.g. "breast cancer" → "Breast Neoplasms" being discarded.
    for key, syn_vals in syn_direct.items():
        if not syn_vals:
            continue
        existing = combined_member.get(key) or []
        if not isinstance(existing, list):
            existing = []
        merged = list(dict.fromkeys(existing + syn_vals))
        if len(merged) != len(existing):
            logger.info(
                f"[{tool}][{request_id}] [SYN-DIRECT] {key}: unioned "
                f"{len(merged) - len(existing)} trusted synonym value(s) "
                f"past the LLM filter"
            )
        combined_member[key] = merged

    # ── Reverse-map canonical resolution (2026-06-22) ─────────────────────
    # Authoritative DB-native synonym→canonical lookup, built offline by each
    # DB's own preprocess_v2 `build_reverse_synonym_map()` step (CTD/HCDT
    # preprocess_v2.py; TTD preprocess_v2.ipynb), from that DB's master synonym
    # sources. A hit is a DB-canonical value, so it is
    # unioned in TRUSTED-DIRECT — never LLM-filtered — and runs REGARDLESS of
    # whether fuzzy/semantic/LLM already produced candidates (the empty-only
    # fallback below cannot fix the case where the wrong candidates were kept).
    # Routes to kb_direct for KB-only fields (drug) and combined_member for
    # free-text (disease). Fixes e.g. "breast cancer" → "Breast Neoplasms".
    for key, raw_terms in parsed_value.items():
        if not isinstance(raw_terms, list) or not raw_terms:
            continue
        input_terms = [
            t for t in raw_terms
            if isinstance(t, str) and t.strip() and t != "requested"
        ]
        if not input_terms:
            continue
        rev_hits = _lookup_reverse_synonyms(database, key, input_terms)
        if not rev_hits:
            continue
        rev_norm = [h.lower() for h in rev_hits]
        pool = kb_direct if key in _kb_cols else combined_member
        existing = pool.get(key) or []
        if not isinstance(existing, list):
            existing = []
        merged = list(dict.fromkeys(existing + rev_norm))
        if len(merged) != len(existing):
            logger.info(
                f"[{tool}][{request_id}] [REVERSE-MAP] {key}: {input_terms} → "
                f"unioned canonical {rev_hits} (trusted, past LLM)"
            )
        pool[key] = merged

    # ── Reverse-synonym fallback (2026-05-25) ─────────────────────────────
    # Applied AFTER the LLM filter so the LLM never second-guesses curated DB
    # data. Fires for any field (KB-direct or LLM-filtered) that came up empty.
    # For KB-only fields this is the only fallback path — fuzzy/semantic are not
    # attempted. Catches dev-code aliases like STI571→imatinib that KB APIs miss.
    for pool in (kb_direct, combined_member):
        for key, current_val in list(pool.items()):
            if current_val:
                continue
            raw_terms = parsed_value.get(key)
            if not raw_terms or not isinstance(raw_terms, list):
                continue
            input_terms = [
                t for t in raw_terms
                if isinstance(t, str) and t.strip() and t != "requested"
            ]
            if not input_terms:
                continue
            rev_hits = _lookup_reverse_synonyms(database, key, input_terms)
            if rev_hits:
                pool[key] = rev_hits
                logger.info(
                    f"[{tool}][{request_id}] [REVERSE-SYN] {key}: "
                    f"{input_terms} → {rev_hits}"
                )
            elif pool is kb_direct:
                # KB-literal fallback (2026-06-22): for gene/drug (KB-only)
                # fields the synonym expander is the ONLY matcher. If it returned
                # nothing — typically an external-KB timeout/gateway error, which
                # also discards the literal term the expander would otherwise
                # union back in — fall back to the user's original queried value.
                # DB matching is case-insensitive, so the exact symbol still
                # resolves and a gene/drug filter can never go fully empty.
                pool[key] = sorted({t.lower() for t in input_terms})
                logger.info(
                    f"[{tool}][{request_id}] [KB-LITERAL-FALLBACK] {key}: "
                    f"expander returned nothing → using literal {pool[key]}"
                )

    # ── Literal-term floor (2026-06-23, extended to KB fields 2026-06-23) ──
    # GUARANTEE the user's original literal term is always present in the final
    # match set, so the result can never drop below what matching the literal
    # term alone would have returned. Without this floor, a flaky/narrow synonym
    # set (external-KB timeout) or an over-zealous LLM filter can shrink the
    # result below the canonical literal match — producing NON-DETERMINISTIC
    # results (e.g. CTD "asthma" exposure studies 24/57/73 instead of a stable
    # 77; "cisplatin" drug lookup returning 1 row one run, 0 the next when the
    # drug-synonym API times out). Synonyms still ADD on top.
    #
    # Routes the literal to the SAME pool the field's matcher uses:
    #   - KB fields (drug_name / gene_symbol) → kb_direct  (EXACT-matched downstream
    #     by dataframe_filtering: literal "cisplatin"=="Cisplatin", "tnf"=="TNF" —
    #     safe, no loose substring over-match).
    #   - free-text fields → combined_member (substring/contains downstream).
    # Structured/ID fields are skipped — structured_exact / ID-passthrough already
    # echo the literal. Generic & scalable: NO per-DB / per-term hardcoding.
    for _key, _raw_terms in parsed_value.items():
        if _key in STRUCTURED_FIELDS_BYPASS_LLM:
            continue
        if not isinstance(_raw_terms, list) or not _raw_terms:
            continue
        _literal = sorted({
            t.strip().lower() for t in _raw_terms
            if isinstance(t, str) and t.strip() and t != "requested"
        })
        if not _literal:
            continue
        _pool = kb_direct if _key in _kb_cols else combined_member
        _existing = _pool.get(_key) or []
        if not isinstance(_existing, list):
            _existing = []
        _merged = list(dict.fromkeys(_existing + _literal))
        if len(_merged) != len(_existing):
            logger.info(
                f"[{tool}][{request_id}] [LITERAL-FLOOR] {_key}: ensured literal "
                f"{_literal} present (deterministic floor; "
                f"+{len(_merged) - len(_existing)})"
            )
        _pool[_key] = _merged

    # Merge results in priority order:
    # 1. Structured fields first (exact matches, no fuzzy/semantic/LLM)
    # 2. KB-direct fields (KB synonyms only)
    # 3. Free-text LLM-filtered results
    # 4. Restore "requested" wildcard fields
    # This ensures structured fields are never overwritten by LLM output.
    llm_filtered_keys = len(combined_member)
    combined_member.update(structured_exact)  # Exact case-insensitive matches
    combined_member.update(kb_direct)         # KB-only synonyms
    combined_member.update(requested_passthrough)  # "requested" sentinels

    overall_elapsed = time.perf_counter() - overall_start

    logger.info(
        f"[{tool}][{request_id}] [RESULT] total_output_keys={len(combined_member)} | "
        f"free_text_llm_filtered={llm_filtered_keys} | kb_direct={len(kb_direct)} | "
        f"structured_exact={len(structured_exact)} | "
        f"fuzzy={counts.get('fuzzy', 0)} | semantic={counts.get('semantic', 0)} | "
        f"expand_synonyms={counts.get('expand_synonyms', 0)} | elapsed={overall_elapsed:.2f}s"
    )

    # Determine response
    if len(error_log) == len(service_outputs):
        # All services failed
        msg = f"All expand/match services failed: {error_log}"
        logger.error(f"[{tool}][{request_id}] [FAILURE] {msg}")

        result = ExpandMemberOutput(
            database=database_upper,  # Return original case
            value={},
            tool=tool,
            message=msg,
            errors=error_log
        )
    else:
        # At least one service succeeded
        msg = None
        if error_log:
            msg = f"Partial error(s) encountered: {error_log}"
        
        result = ExpandMemberOutput(
            database=database_upper,  # Return original case
            value=combined_member,
            tool=tool,
            message=msg,
            errors=error_log if error_log else None
        )
    
    return result
