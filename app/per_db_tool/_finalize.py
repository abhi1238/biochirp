"""Shared response-building tail for `return_<db>_result`.

The bottom 82 lines of `return_<db>_result` (web-evidence fetch +
summarizer call + filter_trace build + DatabaseTable construction) were
byte-for-byte identical across the migrated workers after DB-name
normalization. Those workers now call `finalize_db_result(state)`
instead of carrying their own copy.

All active parquet workers are migrated onto
`execute_db_query` + `finalize_db_result`. Any post-join column shaping
runs in a `post_join` hook. No per-DB worker carries its own orchestration
copy any more.

The state object is intentionally a plain dataclass with explicit fields
so each worker call site is self-documenting — no `**kwargs` magic.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

from openai import AsyncOpenAI
from config import settings
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from utils.web_evidence import fetch_web_evidence

from ._httpx_client import get_httpx_client
from ._worker_helpers import get_redis

# Generic approval-tier regexes for the RERANK secondary sort.
# Column detector: any column whose name contains "approval", "status",
# "phase", or "stage" is treated as a clinical/regulatory-status column.
# Value patterns are intentionally broad — they match across DB conventions
# (e.g. TTD's "Phase 3", CTD's "Inferred", HCDT's "Approved Drug").
_RERANK_STATUS_COL = re.compile(
    r"(approval|status|phase|stage)", re.IGNORECASE
)
_RERANK_APPROVED_VAL = re.compile(r"\bapproved\b", re.IGNORECASE)
_RERANK_PHASE_VAL = re.compile(r"\bphase\s*([0-9]+)\b|\bpreclinical\b", re.IGNORECASE)

# Max characters allowed in the JSON-encoded summariser user-message. OpenAI
# silently truncates input from the END when context overflows, which drops
# `web_evidence`, `parsed_value`, and `query` — exactly the fields the
# summariser most needs to write a faithful answer. A 24K-char cap leaves
# ~18K tokens of headroom for system_prompt + 800-token response on a
# 128K-context model and well under gpt-4.1-nano's effective budget for
# fast responses. Override per-environment with SUMMARIZER_PROMPT_MAX_CHARS.
_SUMMARIZER_PROMPT_MAX_CHARS = int(os.getenv("SUMMARIZER_PROMPT_MAX_CHARS", "24000"))
# When relevance_score sorting is active, only the top-N rows are sent to the
# LLM synthesizer — the rest are still visible to the user via the CSV download.
# Set SYNTHESIZER_TABLE_CAP=0 to pass all preview rows (old behaviour).
_SYNTHESIZER_TABLE_CAP = int(os.getenv("SYNTHESIZER_TABLE_CAP", "50"))

_HGNC_CSV_CANDIDATES = [
    "/app/resources/prompts/hgnc_gene_names.csv",
    "/app/resources/values/hgnc_gene_names.csv",
    os.path.join(os.path.dirname(__file__), "..", "..", "resources", "prompts", "hgnc_gene_names.csv"),
    os.path.join(os.path.dirname(__file__), "..", "..", "resources", "values", "hgnc_gene_names.csv"),
]


# SYNTHESIZER_MODE toggles which system prompt every per-DB tool call's
# summarizer uses: "story" (default) = warm narrative prose, no bullet/
# numbered lists — for real end users. "eval" = terse Yes/No verdicts +
# numbered-list enumerations that BioASQ/db-stability-judge benchmarks
# can score exactly. Same env var as schema_kg_chat.py's front-door
# synthesizer, so the two always stay in sync.
_SYNTHESIZER_MODE = os.getenv("SYNTHESIZER_MODE", "story").strip().lower()
_SYNTH_PROMPT_FILE = "synthesizer_eval.md" if _SYNTHESIZER_MODE == "eval" else "synthesizer.md"

_SYNTH_PROMPT_CANDIDATES = [
    f"/app/resources/prompts/{_SYNTH_PROMPT_FILE}",
    os.path.join(os.path.dirname(__file__), "..", "..", "resources", "prompts", _SYNTH_PROMPT_FILE),
]


@lru_cache(maxsize=1)
def _load_synthesizer_prompt() -> str:
    """Load the mode-selected synthesizer prompt (see SYNTHESIZER_MODE
    above) and splice in medical disclaimer."""
    for path in _SYNTH_PROMPT_CANDIDATES:
        p = os.path.normpath(path)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                from utils.disclaimers import splice_disclaimers
                return splice_disclaimers(raw)
            except Exception:
                return raw
    return ""  # fallback: empty system prompt


@lru_cache(maxsize=1)
def _disclaimer_texts() -> tuple[str, str]:
    """Return (medical_advice, provenance) sentences from the disclaimers SSOT.

    Falls back to a literal copy on any load failure so the zero-row
    guard below (which does not depend on an LLM call) never ships an
    un-disclaimed answer even if the YAML is missing in some environment.
    """
    try:
        from utils.disclaimers import load_disclaimers
        d = load_disclaimers()
        return d["medical_advice"], d["provenance"]
    except Exception as e:
        log = logging.getLogger("uvicorn.error")
        log.warning("[_finalize] disclaimer YAML load failed: %s", e)
        return (
            "Note: I'm not a medical professional. This information is for "
            "educational purposes only and is not medical advice.",
            "⚠️ Answer below is from a web search and AI synthesis — not from "
            "BioChirp's curated biomedical databases. Verify every claim "
            "against the cited primary sources.",
        )


@lru_cache(maxsize=1)
def _load_gene_name_map() -> dict[str, str]:
    """Load gene_symbol → gene_full_name from the first available CSV path."""
    for path in _HGNC_CSV_CANDIDATES:
        p = os.path.normpath(path)
        if os.path.isfile(p):
            with open(p, newline="", encoding="utf-8") as fh:
                return {row["gene_symbol"]: row["gene_full_name"] for row in csv.DictReader(fh)}
    return {}


def _inject_gene_full_names(preview: list[dict]) -> list[dict]:
    """Enrich preview rows with HGNC full gene names.

    For every column in every row, if the value is an exact HGNC symbol in
    the lookup map, inject a sibling column ``{col}_full_name`` with the
    official name.  No column names are hardcoded — the lookup CSV is the
    sole authority on what counts as a gene symbol.  DB-native ``*_full_name``
    values are never overwritten.
    """
    if not preview:
        return preview
    gene_map = _load_gene_name_map()
    if not gene_map:
        return preview
    enriched = []
    for row in preview:
        extra: dict[str, str] = {}
        for col, val in row.items():
            if not isinstance(val, str) or not val:
                continue
            full_name_col = f"{col}_full_name"
            if full_name_col in row:
                continue
            name = gene_map.get(val)
            if name:
                extra[full_name_col] = name
        enriched.append({**row, **extra} if extra else row)
    return enriched


def _build_prompt_with_budget(
    payload: dict,
    *,
    max_chars: int,
    db: str,
    log: logging.Logger,
) -> str:
    """Serialise `payload` to JSON, iteratively halving `payload['table']`
    (the preview rows) until the result fits in `max_chars`.

    The preview is the only safely-trimmable field — `filter_stats`, `plan`,
    `filter_value`, `parsed_value`, `query`, and `web_evidence` all feed
    directly into the summariser's reasoning and must reach it intact. The
    preview rows are illustrative; even 1 row is enough for the LLM to
    describe the result shape. Always keeps ≥1 row.

    Logs a warning whenever trimming fires so the rate is observable.
    """
    prompt = json.dumps(payload, default=str, ensure_ascii=False)
    if len(prompt) <= max_chars:
        return prompt
    original_rows = len(payload.get("table") or [])
    rows = original_rows
    while len(prompt) > max_chars and rows > 1:
        rows = max(1, rows // 2)
        payload = dict(payload)  # shallow copy so we don't mutate caller's dict
        payload["table"] = (payload.get("table") or [])[:rows]
        prompt = json.dumps(payload, default=str, ensure_ascii=False)
    log.warning(
        "[%s] summariser prompt trimmed: %d → %d preview rows (size %d → %d chars, cap %d)",
        db, original_rows, len(payload.get("table") or []),
        # Approx the original size from the original-rows count for the log.
        len(prompt) + max(0, original_rows - rows) * 200, len(prompt), max_chars,
    )
    return prompt


@dataclass
class QueryState:
    """Snapshot of a `return_<db>_result` pipeline at the point where the
    response narrative gets built. Workers fill this in from their local
    variables, then call `finalize_db_result(state)`.
    """
    db: str                      # SERVICE_NAME (lowercase)
    tool: str                    # usually == db
    DB_NAME: str                 # display name, e.g. "ChEMBL"
    input: QueryInterpreterOutputGuardrail
    df: Any                      # polars.DataFrame
    filter_stats: list           # list[FilterStat]
    plan: Optional[dict]
    filter_val: dict
    schema_cols: set[str]
    prompt_md: str
    summarizer_model: str
    error_msg: Optional[str] = None
    csv_path: str = ""
    preview: list = field(default_factory=list)
    # When non-None and `error_msg` is None, this exact string is used as
    # the response message — the LLM summarizer call is skipped entirely.
    # Used by workers that opt out of the per-DB LLM summary via
    # `BIOCHIRP_<DB>_WORKER_LLM_SUMMARY=0` (ttd, ctd, hcdt) because the
    # multi-DB front-door synthesizer is the one that builds the user-facing
    # answer; the per-worker LLM call is redundant on that path.
    pre_computed_message: Optional[str] = None


async def finalize_db_result(state: QueryState) -> DatabaseTable:
    """Build the final DatabaseTable from a QueryState.

    Steps performed:
      1. If `error_msg` is set, return early with an error DatabaseTable.
      2. Otherwise: fetch web_evidence (best-effort), call the
         summarizer LLM, fall back to a templated message on failure.
      3. Build the filter_trace for UI funnel rendering.
      4. Return the populated DatabaseTable.

    This function is the byte-identical bottom of every "simple" worker
    in the project — extracting it removes ~82 lines from each.
    """
    log = logging.getLogger("uvicorn.error")
    db = state.db

    _fallback = f"Retrieved {state.df.height} rows from {db.upper()}."

    # Re-rank state.preview so rows that exactly match the queried entity
    # (drug/gene/disease name from parsed_value) float to the top.
    # Runs unconditionally — before the message-building branches — so
    # both the fast-template path in schema_kg_chat (≤3 rows, reads
    # result.table which comes from state.preview) and the LLM synthesizer
    # path see the same re-ranked order.  Fail-open: any error keeps the
    # original order.
    if not state.error_msg:
        try:
            # Build candidate terms from filter_val (all expanded synonyms), then
            # narrow to only terms that appear verbatim in the user's cleaned_query.
            # This prevents all-synonym matching: when expand returns ["NN-7415",
            # "Concizumab", ...] for "Concizumab", every row matches every term and
            # the sort is a no-op. Narrowing to query-text terms keeps only
            # "concizumab" (which appears in the question), excluding "nn-7415"
            # (an alias never mentioned by the user).
            _all_terms: set[str] = set()
            for _val in (state.filter_val or {}).values():
                if _val is None:
                    continue
                if isinstance(_val, list):
                    _all_terms.update(
                        str(v).lower() for v in _val
                        if v and str(v).lower() not in ("requested", "")
                    )
                elif isinstance(_val, str) and _val.lower() not in ("requested", ""):
                    _all_terms.add(_val.lower())
            _query_text = (state.input.cleaned_query or "").lower()
            # Keep only terms that appear verbatim in the question; fall back to
            # all terms (old behaviour) when nothing matches (e.g. yes/no queries).
            _query_terms = {t for t in _all_terms if t and t in _query_text} or _all_terms
            if state.preview:
                # Two-level sort key: (entity_match, approval_status)
                # All tiers are computed per-row; lower number = better rank.
                #
                # Tier 1 — entity match: exact=0, substring=1, no-match=2.
                #   Only active when the question mentions a specific entity
                #   (i.e. _query_terms is non-empty); all rows score 2 otherwise.
                #
                # Tier 2 — approval/clinical status: "Approved" > Phase 3 > Phase 2
                #   > Phase 1 > unknown. Driven by any column whose name contains
                #   "approval", "status", "phase", or "stage" (case-insensitive).
                #   No-op for DBs without such a column.
                #
                # DB-specific ranking (e.g. CTD evidence tiers, HCDT source count)
                # is handled by the SQL ORDER BY in db_llm_rules.yaml col_selection
                # for each DB — not here.

                def _row_rank(row: dict) -> tuple[int, int]:
                    vals = [str(v).lower() for v in row.values() if v is not None and str(v)]

                    # Tier 1: entity match
                    entity = 2
                    if _query_terms:
                        for term in _query_terms:
                            if any(v == term for v in vals):
                                entity = 0
                                break
                            if any(term in v for v in vals):
                                entity = 1
                                # keep scanning — a later term may give exact

                    # Tier 2: approval / clinical-phase status
                    # Scan status-like columns; best (lowest) value wins.
                    approval = 10  # default: no status column found
                    for col, val in row.items():
                        if not _RERANK_STATUS_COL.search(str(col)):
                            continue
                        val_str = str(val or "").strip()
                        if not val_str:
                            continue
                        if _RERANK_APPROVED_VAL.search(val_str):
                            approval = min(approval, 0)  # Approved → best
                        else:
                            m = _RERANK_PHASE_VAL.search(val_str)
                            if m and m.group(1):
                                # Higher phase number = closer to approval = better
                                phase_num = int(m.group(1))
                                approval = min(approval, max(1, 4 - phase_num))
                            elif m:  # "preclinical" match (no digit group)
                                approval = min(approval, 4)
                            else:
                                approval = min(approval, 5)  # other status value

                    return (entity, approval)

                state.preview = sorted(state.preview, key=_row_rank)
        except Exception as _re:
            log.warning("[%s] entity rerank failed: %s", db, _re)

    # Inject gene_full_name from static HGNC lookup so the summarizer can
    # quote full names verbatim from the table (no pretraining needed).
    try:
        state.preview = _inject_gene_full_names(state.preview)
    except Exception as _ge:
        log.warning("[%s] gene_full_name injection failed: %s", db, _ge)

    if state.error_msg:
        message = state.error_msg
    elif state.pre_computed_message is not None:
        # Worker opted out of the LLM summarizer (typically via a
        # BIOCHIRP_<DB>_WORKER_LLM_SUMMARY env var) and pre-built the
        # message itself.
        message = state.pre_computed_message
    elif getattr(state.input, "skip_summary", False):
        message = _fallback
    else:
        # Web-evidence fallback for columns not present in our schema. Reuse
        # the process-wide async httpx client (same one used for expand /
        # planner POSTs) so fan-out web calls amortise the TCP+TLS handshake.
        # Pass the shared Redis client so multi-DB fan-outs (TTD+CTD+HCDT all
        # asking for the same CAS / UniProt ID) hit one web round-trip total
        # instead of N. get_redis() is best-effort: returns None when Redis
        # is unreachable, in which case fetch_web_evidence silently falls
        # back to direct calls.
        try:
            _redis = await get_redis(logger=log)
        except Exception:
            _redis = None
        try:
            web_evidence = await fetch_web_evidence(
                state.input.parsed_value,
                state.df.columns,
                state.input.cleaned_query,
                state.DB_NAME,
                state.schema_cols,
                client=get_httpx_client(),
                redis_client=_redis,
            )
        except Exception as _wee:
            log.warning("[%s function] web_evidence fallback failed: %s", db, _wee)
            web_evidence = None

        filter_stats_payload = [
            {
                "column": s.column,
                "input_values": s.input_values,
                "rows_before": s.rows_before,
                "rows_after": s.rows_after,
                "reduction_pct": (
                    round(100 * (1 - s.rows_after / s.rows_before), 1)
                    if s.rows_before else None
                ),
            }
            for s in state.filter_stats
        ]
        # Build the summariser input as JSON (not Python repr). LLMs parse
        # JSON measurably more reliably than `str(dict)` — single-quoted keys,
        # `None`/`True` instead of `null`/`true`, unescaped Unicode in cell
        # values all degrade smaller models (gpt-4.1-nano, qwen3.5-nothink)
        # disproportionately. `default=str` covers pydantic models /
        # numpy / polars types that aren't natively JSON-serializable. The
        # helper trims preview rows iteratively if the JSON exceeds the
        # SUMMARIZER_PROMPT_MAX_CHARS budget (logs a warning when it does).
        _llm_rows = (
            state.preview[:_SYNTHESIZER_TABLE_CAP]
            if _SYNTHESIZER_TABLE_CAP > 0
            else state.preview
        )

        # Deterministic filter-vs-answer column split. Small synthesizers
        # (8B) cannot reliably infer which column is the queried FILTER
        # (constant across rows, e.g. drug_name=Ivermectin) vs the ANSWER
        # (varies, e.g. disease_name). We compute it here from filter_val +
        # constant-column detection and hand the model the conclusion, so it
        # only RENDERS prose instead of deriving column roles. Generic across
        # all DBs (purely structural, no entity/DB hardcoding); fail-open.
        _answer_cols = _filter_cols = None
        try:
            _df = state.df
            if _df is not None and _df.height > 1 and _df.width > 1:
                _const = {c for c in _df.columns
                          if _df.get_column(c).n_unique() == 1}
                # state.filter_val is a universal per-request template with a key
                # for every column this DB's schema *could* filter on — nearly all
                # of them None for any given query. Only a key holding a REAL
                # filter value (non-empty list, or the literal "requested" marker
                # for an explicitly-requested-but-unfiltered column) reflects an
                # actual filter the user applied; a bare `None` entry is not one.
                # (2026-07-04 fix: the original `{str(k) for k in filter_val}` took
                # every key regardless of value, which — since that template is a
                # superset of every DB's own columns — made `_filt` cover ~all of
                # `_df.columns` for every query, so `_ans` was always empty and
                # this whole answer/filter hint silently never fired.)
                _filt = {
                    str(k) for k, v in (state.filter_val or {}).items()
                    if v not in (None, "requested") and v != []
                } | _const
                _ans = [c for c in _df.columns
                        if c not in _filt and c != "relevance_score"]
                if _ans and _filt:          # only when a real filter/answer split exists
                    _answer_cols, _filter_cols = _ans, sorted(_filt)
        except Exception as _ace:           # never block synthesis on the hint
            log.warning("[%s] answer-column hint failed: %s", db, _ace)

        # Full-distinct-value hint for truncated results (2026-07-04 fix).
        # `_llm_rows` below is capped at _SYNTHESIZER_TABLE_CAP (default 50)
        # rows, relevance-sorted — for a large result (hundreds/thousands of
        # rows) that sample can miss most distinct values of a genuinely
        # low-cardinality answer column (e.g. only 2 of 15 genes represented
        # in a 1272-row variant list survive into the preview). Compute the
        # FULL distinct-value list from state.df (not the trimmed preview)
        # for any answer column whose cardinality is real grouping (more than
        # one value, but well short of one-per-row) and small enough to be
        # useful in a prompt — purely structural (row/column-count based), no
        # DB or column names hardcoded.
        _answer_full_values: dict[str, list] = {}
        try:
            _df = state.df
            if _answer_cols and _df is not None:
                _cap_vals = int(os.getenv("SYNTH_FULL_VALUE_CAP", "150"))
                _total_rows = _df.height
                for _c in _answer_cols:
                    try:
                        _n = _df.get_column(_c).n_unique()
                    except Exception:
                        continue
                    if 1 < _n < _total_rows and _n <= _cap_vals:
                        _vals = [
                            str(v) for v in
                            _df.get_column(_c).drop_nulls().unique(maintain_order=True).to_list()
                        ]
                        if _vals:
                            _answer_full_values[_c] = _vals
        except Exception as _fve:
            log.warning("[%s] full-value-list hint failed: %s", db, _fve)

        # Build synthesizer.md-compatible payload (question + db_rows + web_rows).
        # Row indices follow the [<db>:N] citation convention synthesizer.md expects.
        _db_rows = [
            {"__row_idx": f"{db}:{i + 1}", **row}
            for i, row in enumerate(_llm_rows)
        ]
        _web_rows = [
            {
                "__row_idx": f"web:{i + 1}",
                "snippet": w.get("message", ""),
                "source": "web",
                "source_urls": [],
                "source_titles": [],
            }
            for i, w in enumerate(web_evidence or [])
        ]
        _true_total = state.df.height
        _payload = {
            "_COUNT_INSTRUCTION": (
                f"db_row_count={_true_total} is the TRUE total rows matching the query. "
                f"db_rows is a PREVIEW of only {len(_db_rows)} rows. "
                f"ALWAYS use db_row_count={_true_total} when stating the count in your answer. "
                f"NEVER count the items in db_rows to get the total."
            ),
            "question": state.input.cleaned_query,
            "database": db,
            "db_row_count": _true_total,
            "db_rows": _db_rows,
            "web_rows": _web_rows,
            "web_row_count": len(_web_rows),
            "web_fallback_used": bool(_web_rows),
        }
        if _answer_cols:
            _payload["answer_columns"] = _answer_cols
            _payload["filter_columns"] = _filter_cols
        if _answer_full_values:
            _payload["_LIST_INSTRUCTION"] = (
                "answer_column_full_values lists EVERY distinct value present in the "
                "COMPLETE result set (all db_row_count rows) for each named column — "
                "not just the db_rows preview, which is a small relevance-sorted sample "
                "that can omit most valid entries when db_row_count is large. When the "
                "question asks for a list/set (e.g. 'which genes', 'which diseases are "
                "associated with...'), enumerate from answer_column_full_values for the "
                "relevant column instead of only what appears in db_rows."
            )
            _payload["answer_column_full_values"] = _answer_full_values
        # ── Deterministic zero-row guard (applies to EVERY DB on this shared
        # path — STRING, CTD-siblings, etc. — not a per-DB patch) ───────────
        # synthesizer.md's Branch A/B already instruct the model to disclose
        # a 0-row / web-fallback answer, but that is a PROMPT-level contract:
        # a fast/small summarizer model can — and, per the STRING/ERBB2
        # incident, sometimes does — ignore it and answer fluently from its
        # own training knowledge instead, in direct violation of BioChirp's
        # "never substitute training-knowledge data for missing DB results"
        # rule. When there is literally nothing to synthesize (no db_rows,
        # no web_rows) there is no reason to invoke the LLM at all — short
        # circuit with the exact Branch A message so the "0 rows" case can
        # never be answered from pretraining, regardless of model compliance.
        _zero_row_no_web = _true_total == 0 and not _db_rows and not _web_rows
        if _zero_row_no_web:
            _med_disclaimer, _ = _disclaimer_texts()
            message = (
                f"Hi! The BioChirp curated databases have no matching records "
                f"for your query in {state.DB_NAME}.\n\n"
                f"> *{_med_disclaimer}*"
            )
            log.info(
                "[%s] zero-row deterministic short-circuit — db_row_count=0, "
                "web_row_count=0; skipped LLM synthesizer to guarantee the "
                "no-match disclosure.", db,
            )
        else:
            _prompt = _build_prompt_with_budget(
                _payload, max_chars=_SUMMARIZER_PROMPT_MAX_CHARS, db=db, log=log)

            try:
                _http_timeout = float(os.getenv("OPENAI_HTTP_TIMEOUT", "45"))
                # Route to OpenRouter when model name contains "/" (e.g. "openai/gpt-oss-120b"),
                # otherwise Groq (e.g. "llama-3.1-8b-instant"). Use per-DB OpenRouter key
                # so each DB consumes its own rate-limit bucket.
                _is_openrouter = "/" in state.summarizer_model
                _summ_base = (
                    "https://openrouter.ai/api/v1"
                    if _is_openrouter
                    else os.getenv("SUMMARIZER_BASE_URL", "https://api.groq.com/openai/v1")
                )
                _summ_key = (
                    settings.get_openrouter_key(state.db)
                    if _is_openrouter
                    else settings.get_groq_key(state.db)
                )
                _synth_system = _load_synthesizer_prompt() or state.prompt_md
                _client = AsyncOpenAI(
                    base_url=_summ_base,
                    api_key=_summ_key,
                    max_retries=int(os.getenv("SUMMARIZER_MAX_RETRIES", "2")),
                    timeout=_http_timeout,
                )
                _resp = await asyncio.wait_for(
                    _client.chat.completions.create(
                        model=state.summarizer_model,
                        messages=[
                            {"role": "system", "content": _synth_system},
                            {"role": "user", "content": _prompt},
                        ],
                        max_tokens=int(os.getenv("BIOCHIRP_SYNTH_MAX_TOKENS", "2000")),
                    ),
                    timeout=_http_timeout + 10,
                )
                message = (_resp.choices[0].message.content or "").strip() or _fallback
            except Exception as _e:
                log.error("[%s] Summarizer failed: %s", db, _e)
                message = _fallback

            # ── Code-level provenance-disclaimer enforcement (defense in
            # depth) ─────────────────────────────────────────────────────
            # Branch B requires the canonical provenance sentence verbatim
            # whenever db_row_count==0 and web evidence was the only source.
            # Don't just trust the model to have emitted it — the whole
            # point of this guard is that trust is exactly what failed for
            # STRING/ERBB2. If it's missing, prepend it in code so a
            # non-DB-grounded answer can NEVER ship without disclosure,
            # regardless of which summarizer model is behind this DB.
            if _true_total == 0 and _web_rows:
                _, _provenance = _disclaimer_texts()
                if _provenance not in message:
                    message = f"> *{_provenance}*\n\n{message}"
                    log.warning(
                        "[%s] summarizer omitted the required provenance "
                        "disclaimer on a 0-row/web-fallback answer — "
                        "prepended it in code.", db,
                    )

    # filter_trace for UI funnel rendering (TTD-grade).
    _trace = None
    try:
        if not state.error_msg and state.filter_stats:
            _trace = [
                {
                    "column": fs.column,
                    "input_values": list(fs.input_values or [])[:8],
                    "rows_before": int(fs.rows_before),
                    "rows_after":  int(fs.rows_after),
                }
                for fs in state.filter_stats
            ]
    except Exception:
        pass

    return DatabaseTable(
        database=db,
        table=state.preview if not state.error_msg else None,
        csv_path=state.csv_path if not state.error_msg else None,
        row_count=state.df.height if not state.error_msg else None,
        tool=state.tool,
        message=message,
        filter_trace=_trace,
        filter_val=dict(state.filter_val or {}),
    )
