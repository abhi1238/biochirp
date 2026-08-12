"""
Schema KG column retrieval — single and ensemble modes.

Ensemble (default) — 6 LLM calls, 3 parallel waves:
  Wave 1 (parallel): MODEL_1 expand+clean  /  MODEL_2 expand+clean
  Wave 2 (independent ANN): ANN-1 on expansion_1  /  ANN-2 on expansion_2
  Wave 3a (parallel, each filters OWN candidates):
      filter_1 (MODEL_1) on cands_1  /  filter_2 (MODEL_2) on cands_2
      → union kept
  Wave 3b (parallel): mapper_1 (MODEL_1)  /  mapper_2 (MODEL_2)
    → consensus check → [orchestrator (MODEL_2) if disagree]

  Wall-clock ≈ 3 sequential LLM latencies.
  clean_query (from MODEL_1 expander) is used for value mapping
  so abbreviations/trade-names/aliases are resolved before extraction.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from .graph  import SchemaGraph
from .embed  import _encode
from .config import TOP_K, THRESHOLD

logger = logging.getLogger(__name__)

# ── Model config (resolved from the SSOT — declared in .env) ──────────────────
MODEL_1 = settings.SCHEMA_KG_FILTER_MODEL          # lane-1: expand → ANN → filter
MODEL_2 = settings.SCHEMA_KG_ENSEMBLE_MODEL_2      # lane-2: expand → ANN → filter → map + orchestrator
# Separate mapper model for lane-1 (no silent fallback to MODEL_1).
MAPPER_MODEL_1 = settings.SCHEMA_KG_MAPPER_MODEL_1


# ── Schema context builder ────────────────────────────────────────────────────

# Column notes are now entirely DB-specific — each DB's schema_rules.json
# supplies column_notes_override; _build_schema_context merges it below.
_COL_NOTES: dict[str, str] = {}


def _build_schema_context(graph, column_notes_override: dict | None = None) -> str:
    """Build a compact schema context string from all queryable columns.

    column_notes_override, when provided, takes precedence over _COL_NOTES for
    any column it covers.  Remaining columns fall back to _COL_NOTES, then to
    the column's own description from the schema graph.
    """
    merged = dict(_COL_NOTES)
    if column_notes_override:
        merged.update(column_notes_override)
    lines = ["Queryable columns for this database:"]
    for col in sorted(graph.queryable_columns, key=lambda c: c.column):
        note = merged.get(col.column, col.description)
        lines.append(f"  {col.column}: {note}")
    return "\n".join(lines)


# ── Core ANN search ───────────────────────────────────────────────────────────

def _ann_search_text(
    text:          str,
    qdrant_client,
    collection:    str,
    graph:         SchemaGraph,
    top_k:         int   = TOP_K,
    ann_threshold: float = THRESHOLD,
) -> List[Tuple[str, float, str]]:
    """ANN search on an already-prepared text (raw or expanded).

    Only returns results for queryable columns — non-queryable columns
    are filtered out to avoid wasting computation on unretrievable results.
    """
    from qdrant_client.http import models as qmodels

    # Build set of queryable column IDs for filtering
    queryable_col_ids = {col.col_id for col in graph.queryable_columns}

    q_vec = _encode([text])[0]
    hits  = qdrant_client.query_points(
        collection,
        query           = q_vec.tolist(),
        limit           = top_k,
        score_threshold = ann_threshold,
        with_payload    = True,
    ).points

    # ── QUERYABILITY FILTER: Only return candidates from queryable columns
    # This prevents ANN from wasting computation on non-queryable fields
    results = []
    for h in hits:
        col_id = h.payload["col_id"]
        if col_id in queryable_col_ids:
            results.append((col_id, h.score, graph.node_description(col_id)))
    return results


# ── Public retrieval entry point ──────────────────────────────────────────────

def retrieve_columns(
    question:      str,
    qdrant_client,
    collection:    str,
    graph:         SchemaGraph,
    top_k:         int   = TOP_K,
    ann_threshold: float = THRESHOLD,
    ensemble:      bool  = True,
    with_mapping:  bool  = False,
    rules:         Optional[dict] = None,
) -> Tuple[List[Tuple[str, float]], dict]:
    """
    Full pipeline: expand → ANN → filter → kept columns.

    Parameters
    ----------
    ensemble     : if True (default), 2 expanders + 2 filters + union.
    with_mapping : if True, run dual value mapper (mapper_1 + mapper_2 parallel,
                   consensus check, orchestrator on disagreement).
                   meta gains "parsed_value", "mapper_agreement", etc.
    rules        : schema_rules dict from the DB's schema_rules.json.
                   Drives dynamic prompts in expander, filter, and mapper.
                   Also provides column_notes_override for schema context.

    Returns
    -------
    (kept, meta)  kept = [(col_id, score), ...]
    """
    if ensemble:
        kept, meta = _ensemble_retrieve(question, qdrant_client, collection, graph,
                                        top_k, ann_threshold, rules=rules)
    else:
        kept, meta = _single_retrieve(question, qdrant_client, collection, graph,
                                      top_k, ann_threshold, rules=rules)

    if with_mapping:
        from .value_mapper import map_values
        clean_q  = meta.get("clean_query_1") or question
        clean_q2 = meta.get("clean_query_2")   # None in single-lane mode
        parsed_value, map_meta = map_values(
            question=question,
            kept=kept,
            graph=graph,
            clean_query=clean_q,
            clean_query_2=clean_q2,
            rules=rules,
            model_1=MAPPER_MODEL_1,
            model_2=MODEL_2,
        )
        meta["parsed_value"]      = parsed_value
        meta["map_reasoning"]     = map_meta.get("reasoning", "")
        meta["mapper_agreement"]  = map_meta.get("mapper_agreement", True)
        meta["orchestrator_used"] = map_meta.get("orchestrator_used", False)
        meta["mapper_1_pv"]       = map_meta.get("mapper_1_pv", {})
        meta["mapper_2_pv"]       = map_meta.get("mapper_2_pv", {})
        meta["map_reasoning_1"]   = map_meta.get("map_reasoning_1", "")
        meta["map_reasoning_2"]   = map_meta.get("map_reasoning_2", "")
        if not map_meta.get("mapper_agreement", True):
            meta["resolution"] = map_meta.get("resolution", "")
        meta["map_wall_s"]     = map_meta.get("map_wall_s", 0.0)
        meta["mapper1_s"]      = map_meta.get("mapper1_s", 0.0)
        meta["mapper2_s"]      = map_meta.get("mapper2_s", 0.0)
        meta["orchestrator_s"] = map_meta.get("orchestrator_s", 0.0)

    return kept, meta


# ── Single mode ───────────────────────────────────────────────────────────────

def _single_retrieve(question, qdrant_client, collection, graph, top_k, ann_threshold,
                     rules=None):
    from .query_expander import expand_query
    from .llm_filter     import llm_filter_columns

    col_notes  = rules.get("column_notes_override") if rules else None
    schema_ctx = _build_schema_context(graph, col_notes)
    result     = expand_query(question, model=MODEL_1, schema_context=schema_ctx,
                              rules=rules)
    expanded   = result["expansion"]
    cands      = _ann_search_text(expanded, qdrant_client, collection, graph,
                                  top_k, ann_threshold)
    kept, meta = llm_filter_columns(question, cands, model=MODEL_1, graph=graph,
                                    rules=rules)
    meta["clean_query_1"] = result["clean_query"]
    return kept, meta


# ── Ensemble mode ─────────────────────────────────────────────────────────────

def _ensemble_retrieve(question, qdrant_client, collection, graph, top_k, ann_threshold,
                       rules=None):
    from .query_expander import expand_query
    from .llm_filter     import llm_filter_columns

    col_notes  = rules.get("column_notes_override") if rules else None
    schema_ctx = _build_schema_context(graph, col_notes)

    # ── Wave 1: two expansions in parallel (both schema-aware) ───────────────
    _t_w1 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(expand_query, question, MODEL_1, schema_ctx, rules)
        f2 = ex.submit(expand_query, question, MODEL_2, schema_ctx, rules)
        result1 = f1.result()
        result2 = f2.result()
    _t_w1_done = time.perf_counter()

    expand1_s = result1.get("elapsed_s", 0.0)
    expand2_s = result2.get("elapsed_s", 0.0)

    exp1          = result1["expansion"]
    clean_query_1 = result1["clean_query"]
    exp2          = result2["expansion"]
    clean_query_2 = result2["clean_query"]

    logger.info("clean_query_1 (MODEL_1): %s", clean_query_1)
    logger.info("clean_query_2 (MODEL_2): %s", clean_query_2)

    # ── Wave 2: each model searches its OWN expansion (no union yet) ─────────
    _t_w2 = time.perf_counter()
    cands1 = _ann_search_text(exp1, qdrant_client, collection, graph, top_k, ann_threshold)
    cands2 = _ann_search_text(exp2, qdrant_client, collection, graph, top_k, ann_threshold)
    _t_w2_done = time.perf_counter()

    logger.info("ANN: lane1=%d cands  lane2=%d cands", len(cands1), len(cands2))

    # ── Wave 3a: each model filters its OWN candidates in parallel ───────────
    _t_w3a = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        ff1 = ex.submit(llm_filter_columns, question, cands1, MODEL_1, graph, rules)
        ff2 = ex.submit(llm_filter_columns, question, cands2, MODEL_2, graph, rules)
        kept1, meta1 = ff1.result()
        kept2, meta2 = ff2.result()
    _t_w3a_done = time.perf_counter()

    filter1_s = meta1.get("elapsed_s", 0.0)
    filter2_s = meta2.get("elapsed_s", 0.0)

    # Union: keep best score per col_id across both lanes
    best_kept: dict = {}
    for col_id, score in kept1 + kept2:
        if col_id not in best_kept or score > best_kept[col_id]:
            best_kept[col_id] = score
    kept = sorted(best_kept.items(), key=lambda x: -x[1])

    logger.info("Filter: lane1=%d  lane2=%d  →  union=%d",
                len(kept1), len(kept2), len(kept))

    meta = {
        "kept":            [c.split(".")[-1] for c, _ in kept],
        "dropped":         [],
        "reasoning":       f"[Lane1] {meta1.get('reasoning','')} | [Lane2] {meta2.get('reasoning','')}",
        "raw_response":    "",
        "expansion_1":     exp1,
        "expansion_2":     exp2,
        "clean_query_1":   clean_query_1,
        "clean_query_2":   clean_query_2,
        "cands1_count":    len(cands1),
        "cands2_count":    len(cands2),
        "filter1_kept":    meta1.get("kept", []),
        "filter2_kept":    meta2.get("kept", []),
        "expand_wall_s":   _t_w1_done - _t_w1,
        "expand1_s":       expand1_s,
        "expand2_s":       expand2_s,
        "ann_s":           _t_w2_done - _t_w2,
        "filter_wall_s":   _t_w3a_done - _t_w3a,
        "filter1_s":       filter1_s,
        "filter2_s":       filter2_s,
    }
    return kept, meta

