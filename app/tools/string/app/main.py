"""STRING service — thin proxy entry + execute tool server.

POST /string      → thin proxy to biochirp_orchestrator_tool (all logic runs there)
POST /execute     → join+finalize on a pre-computed plan (called by orchestrator)
WebSocket /string_chat/ → full pipeline via orchestrator_url
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx
from fastapi import FastAPI

from app.per_db_tool import (
    ChatSpec, build_chat_router,
    register_execute_endpoint,
)
from app.string_db import (
    return_string_result, get_string_db, _STRING_CAPABILITIES,
    _STRING_LIMITATIONS, SUMMARIZER_MODEL_NAME, prompt_md,
    _annotation_output_only, _STRING_TERM_REWRITE,
)
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from utils.service_setup import add_open_cors, add_health_endpoint, add_download_endpoint

logger = logging.getLogger("uvicorn.error")

ORCHESTRATOR_HOST = os.getenv("ORCHESTRATOR_HOST", "biochirp_orchestrator_tool")
ORCHESTRATOR_PORT = os.getenv("ORCHESTRATOR_PORT", "8021")
ORCHESTRATOR_URL = f"http://{ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}"

app = FastAPI(
    title="STRING Service",
    version="2.0.0",
    description="STRING data-tool: thin proxy entry + execute tool server",
)
add_open_cors(app)
add_health_endpoint(app)
add_download_endpoint(app)


@app.on_event("startup")
async def _schema_gate():
    """Schema/parquet integrity gate — logs mismatches; SCHEMA_VALIDATION=block raises."""
    from app.per_db_tool._schema_guard import assert_db_schema
    assert_db_schema("string", get_string_db)


@app.on_event("startup")
async def _warmup():
    """Fire-and-forget pipeline warm-up: pre-builds schema planner index and caches prompts."""
    import asyncio
    from app.per_db_tool.schema_kg_chat import warm_pipeline
    asyncio.create_task(warm_pipeline("string"))


@app.get("/")
def root():
    return {"message": "STRING service is up"}


# ─── 1. Thin entry proxy ──────────────────────────────────────────────────────
# All query logic (routing, schema mapping, planning, join, finalize) lives in
# biochirp_orchestrator_tool. This endpoint is the HTTP face for the chat
# frontend and bio_chat — it forwards the natural-language query and returns
# whatever the orchestrator produces.

@app.post("/string", response_model=DatabaseTable)
async def string_endpoint(
    payload: QueryInterpreterOutputGuardrail,
    connection_id: Optional[str] = None,
):
    query = (payload.cleaned_query or "").strip()
    if not query:
        pv = payload.parsed_value.model_dump(exclude_none=True) if payload.parsed_value else {}
        parts = []
        for v in pv.values():
            if isinstance(v, list):
                parts.extend(str(x) for x in v if x)
            elif v:
                parts.append(str(v))
        query = " ".join(parts) or "unknown query"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/orchestrate?database=string",
                json={
                    "query": query,
                    "display_name": "STRING",
                    "capabilities": _STRING_CAPABILITIES,
                    "limitations": _STRING_LIMITATIONS,
                    "connection_id": connection_id or "",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(f"[string proxy] orchestrator call failed: {exc}")
        return DatabaseTable(database="string", tool="string",
                             message=f"Orchestrator error: {exc}")

    if data.get("action") in ("direct_answer", "web_search"):
        return DatabaseTable(
            database="string", tool="string",
            message=data.get("answer", ""),
        )

    return data


# ─── 2. Execute tool server ───────────────────────────────────────────────────
# Called by biochirp_orchestrator_tool after route + schema_mapper + schema_planner.
# Receives the pre-computed production_plan, runs join → on_empty_fallback → finalize.

# ── post_join semantics: confidence-tier filter + self-PPI intersection ───────
# STRING combined-score confidence tiers (STRING's own cutoffs):
#   >=900 highest, >=700 high, >=400 medium, >=150 low.
_INTERSECT_RX = re.compile(
    r"\b(both|all of|each of|common to|shared by|in common)\b", re.I)
# qualitative-confidence phrases that imply a score floor: a magnitude word
# (high/highest/strong/top/…) within a few chars of a confidence/score noun.
# Requiring the noun avoids false-firing on a bare "high" in an unrelated query.
_HICONF_RX = re.compile(
    r"\b(high(?:est)?|strong(?:est)?|top|most|very)\b[- \w]{0,20}?"
    r"\b(confiden\w*|score[ds]?|scoring|reliab\w*|significant)\b",
    re.I)


def _string_score_floor(q: str) -> Optional[int]:
    """Map a qualitative-confidence phrase to a STRING combined-score floor."""
    ql = q.lower()
    if re.search(r"highest|strongest|most (?:confident|reliable|significant)|very high|top",
                 ql):
        return 900
    if re.search(r"high|strong|significant|confident|reliable", ql):
        return 700
    return None


def _string_query_semantics(ctx) -> None:
    """Translate two relational/numeric semantics the planner leaves as literals:

      (A) "high / highest confidence" → filter the combined-score column to the
          matching STRING tier (the planner applies no score predicate, so an
          unqualified neighborhood is returned).
      (B) "interact with BOTH X and Y" → keep only partners linked to ALL named
          anchors. STRING is self-referential (anchor ↔ partner), so the generic
          require_all path keys off the wrong column and falls back to an OR
          (union of the two neighborhoods); we intersect on the partner column
          here instead.
    """
    import polars as pl
    try:
        from app.utils.dataframe_filtering import FilterStat
    except Exception:
        FilterStat = None

    def _record(column, values, before, after):
        # Append a human-readable operation to the execute trace so the
        # "operations performed" card explains the confidence / intersection
        # steps (which happen AFTER join_and_filter, so aren't in its trace).
        if FilterStat is not None and isinstance(getattr(ctx, "filter_stats", None), list):
            ctx.filter_stats.append(
                FilterStat(column=column, input_values=list(values),
                           rows_before=int(before), rows_after=int(after)))

    df = ctx.df
    if df is None or df.is_empty():
        return
    q = (getattr(ctx.input, "cleaned_query", "") or "")
    cols = df.columns
    fv = ctx.filter_val or {}

    # ── (A) confidence-tier floor ────────────────────────────────────────────
    if _HICONF_RX.search(q):
        floor = _string_score_floor(q)
        score_col = next(
            (c for c in cols if c.endswith("combined_score") or c.endswith("_score")),
            None)
        if floor is not None and score_col is not None:
            before = df.height
            filt = df.filter(
                pl.col(score_col).cast(pl.Int64, strict=False) >= floor)
            if not filt.is_empty():
                ctx.df = df = filt
                _record(f"{score_col} >= {floor} (high-confidence tier)",
                        [f">={floor}"], before, df.height)
                ctx.log.info(
                    "[string] confidence floor %s>=%d: %d→%d rows",
                    score_col, floor, before, df.height)

    # ── (B) self-PPI 'both X and Y' intersection ─────────────────────────────
    if _INTERSECT_RX.search(q):
        # the anchor column actually filtered with ≥2 distinct named entities
        anchor = next(
            (c for c, v in fv.items()
             if c.endswith("_gene_symbol") and not c.endswith("partner_gene_symbol")
             and isinstance(v, (list, tuple))
             and len({str(x).strip().lower() for x in v if str(x).strip()}) >= 2),
            None)
        if anchor and anchor in cols:
            partner = anchor.replace("_gene_symbol", "_partner_gene_symbol")
            n = len({str(x).strip().lower() for x in fv[anchor] if str(x).strip()})
            if partner in cols and n >= 2:
                before = df.height
                keep = (
                    df.group_by(partner)
                      .agg(pl.col(anchor).cast(pl.Utf8).str.to_lowercase()
                           .n_unique().alias("__n_anchor"))
                      .filter(pl.col("__n_anchor") >= n)
                      .select(partner)
                )
                common = df.join(keep, on=partner, how="inner")
                # Project to the PARTNER side only. The anchor columns
                # (association_gene_symbol / gene_symbol / protein_id / score)
                # are edge-specific: each common partner has one row per anchor,
                # so collapsing to one row would show just ONE of the named
                # anchors (e.g. only EGFR) and read as if the protein interacts
                # with that anchor alone. The answer to "interact with BOTH" is
                # the set of partners — keep partner-side columns, drop the rest.
                _partner_cols = [c for c in common.columns if "partner" in c]
                inter = (common.select(_partner_cols).unique()
                         if _partner_cols else
                         common.unique(subset=[partner], keep="first"))
                ctx.df = inter   # may be empty → orchestrator emits honest no-match
                _record(
                    f"intersection: kept proteins interacting with ALL {n} named "
                    f"targets",
                    [str(x) for x in fv[anchor]], before, inter.height)
                ctx.log.info(
                    "[string] intersection on %s (≥%d of %s): %d→%d rows",
                    partner, n, anchor, before, inter.height)


def _string_execute_pre_join(ctx) -> None:
    """Move annotation-only queries to output before join."""
    _annotation_output_only(ctx)


register_execute_endpoint(
    app,
    db="string",
    display_name="STRING",
    get_db=get_string_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    pre_join=_string_execute_pre_join,
    post_join=_string_query_semantics,
)


# ─── 3. WebSocket chat ───────────────────────────────────────────────────────
# orchestrator_url routes the chat through the full schema_kg pipeline so each
# step (schema_mapper / planner / expander / execute) appears as a progress
# card in the frontend before synthesis.
app.include_router(build_chat_router(ChatSpec(
    db="string",
    display_name="STRING",
    long_name="STRING Protein-Protein Interaction Network",
    return_result_fn=return_string_result,
    orchestrator_url=f"{ORCHESTRATOR_URL}/orchestrate?database=string",
    capabilities=_STRING_CAPABILITIES,
    limitations=_STRING_LIMITATIONS,
    term_rewrite=_STRING_TERM_REWRITE,
)))
