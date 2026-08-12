"""
Query-time algorithm for the Schema KG.

Given a natural language query string this module:
  1. Embeds the query with the same SapBERT model used at build time.
  2. Runs ANN search over the Qdrant schema-column index.
  3. Walks the graph upward (col → table → DB) for each matched column.
  4. Expands via FK edges to pull in join tables within each DB.
  5. Returns a RetrievalPlan: {db → {tables, filter_cols, output_cols}}.

The result feeds directly into the BioChirp Steiner planner (planner/graph.py).
Zero LLM calls after embedding.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union

import numpy as np

from .config import QDRANT_HOST, QDRANT_PORT, QDRANT_COLLECTION, TOP_K, THRESHOLD
from .embed import _encode
from .graph import SchemaGraph
from .index import search_columns

logger = logging.getLogger(__name__)


# ─── RetrievalPlan ───────────────────────────────────────────────────────────

@dataclass
class DBPlan:
    tables:      Set[str]             = field(default_factory=set)
    filter_cols: List[Tuple[str, float]] = field(default_factory=list)  # (col_id, score)

    def to_dict(self) -> dict:
        return {
            "tables":      sorted(self.tables),
            "filter_cols": [(c, round(s, 4)) for c, s in
                            sorted(self.filter_cols, key=lambda x: -x[1])],
        }


RetrievalPlan = Dict[str, DBPlan]


# ─── query_time ──────────────────────────────────────────────────────────────

def query_time(
    user_query: str,
    graph:      SchemaGraph,
    host:       str   = QDRANT_HOST,
    port:       int   = QDRANT_PORT,
    collection: str   = QDRANT_COLLECTION,
    top_k:      int   = TOP_K,
    threshold:  float = THRESHOLD,
) -> RetrievalPlan:
    """
    Translate a natural language query into a per-DB retrieval plan.

    Parameters
    ----------
    user_query : natural language string, e.g. "which genes does imatinib target"
    graph      : SchemaGraph built by build_graph()
    host/port  : Qdrant connection
    top_k      : max ANN results
    threshold  : minimum cosine score to keep a column match

    Returns
    -------
    RetrievalPlan : {db_name → DBPlan}
    """
    # ── Step 1: embed the query ───────────────────────────────────────────────
    q_vec: np.ndarray = _encode([user_query])[0]

    # ── Step 2: ANN search ───────────────────────────────────────────────────
    hits = search_columns(
        query_vector=q_vec,
        host=host, port=port, collection=collection,
        top_k=top_k, threshold=threshold,
    )
    if not hits:
        logger.warning("No schema columns matched query %r (threshold=%.2f)", user_query, threshold)
        return {}

    logger.info("Query %r → %d column hits", user_query, len(hits))
    for col_id, score in hits:
        logger.debug("  %.3f  %s", score, col_id)

    # ── Step 3: walk graph upward col → table → DB ───────────────────────────
    plan: RetrievalPlan = defaultdict(DBPlan)
    for col_id, score in hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        db_plan = plan[col.db]
        db_plan.tables.add(col.table)
        db_plan.filter_cols.append((col_id, score))

    # ── Step 4: FK expansion — add join tables within each DB ────────────────
    for col_id, _score in hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        for nbr_id, etype in graph.adjacency.get(col_id, []):
            if etype != "fk":
                continue
            nbr = graph.col_nodes.get(nbr_id)
            if nbr and nbr.db == col.db:
                plan[col.db].tables.add(nbr.table)

    return dict(plan)


# ─── Rewrite-augmented query ─────────────────────────────────────────────────

def query_time_with_rewrite(
    user_query: str,
    graph:      SchemaGraph,
    model_id:   str,
    api_key:    Optional[str] = None,
    host:       str   = QDRANT_HOST,
    port:       int   = QDRANT_PORT,
    collection: str   = QDRANT_COLLECTION,
    top_k:      int   = TOP_K,
    threshold:  float = THRESHOLD,
) -> tuple[RetrievalPlan, list[str], float]:
    """
    Like query_time but first decomposes the query into sub-queries via LLM,
    then unions the ANN results across all sub-queries (max score per column).

    Returns
    -------
    (plan, sub_queries, rewrite_latency_s)
        plan              : merged RetrievalPlan across all sub-queries
        sub_queries       : the sub-queries produced by the rewriter
        rewrite_latency_s : wall-clock time spent in the LLM rewrite call
    Falls back to plain query_time if the rewriter returns nothing.
    """
    from .rewriter import rewrite_query

    sub_queries, rw_lat = rewrite_query(user_query, model_id, api_key)

    if not sub_queries:
        logger.warning("Rewriter returned nothing for %r — falling back to direct ANN", user_query)
        return query_time(user_query, graph, host, port, collection, top_k, threshold), [], rw_lat

    # Run ANN on each sub-query; merge: keep max score per col_id
    best_score: dict[str, float] = {}
    for sq in sub_queries:
        q_vec = _encode([sq])[0]
        hits  = search_columns(
            query_vector=q_vec,
            host=host, port=port, collection=collection,
            top_k=top_k, threshold=threshold,
        )
        for col_id, score in hits:
            if score > best_score.get(col_id, -1):
                best_score[col_id] = score

    if not best_score:
        return query_time(user_query, graph, host, port, collection, top_k, threshold), sub_queries, rw_lat

    # Rebuild plan from merged hits
    plan: RetrievalPlan = defaultdict(DBPlan)
    merged_hits = sorted(best_score.items(), key=lambda x: -x[1])
    for col_id, score in merged_hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        db_plan = plan[col.db]
        db_plan.tables.add(col.table)
        db_plan.filter_cols.append((col_id, score))

    # FK expansion (same as query_time)
    for col_id, _score in merged_hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        for nbr_id, etype in graph.adjacency.get(col_id, []):
            if etype != "fk":
                continue
            nbr = graph.col_nodes.get(nbr_id)
            if nbr and nbr.db == col.db:
                plan[col.db].tables.add(nbr.table)

    return dict(plan), sub_queries, rw_lat


# ─── Cascade rewrite (llama-4-scout → gemini-2.5-flash-lite fallback) ────────

def query_time_cascade(
    user_query: str,
    graph:      SchemaGraph,
    api_key:    Optional[str] = None,
    host:       str   = QDRANT_HOST,
    port:       int   = QDRANT_PORT,
    collection: str   = QDRANT_COLLECTION,
    top_k:      int   = TOP_K,
    threshold:  float = THRESHOLD,
) -> tuple[RetrievalPlan, list[str], float, str]:
    """
    Query with cascade rewriting: llama-4-scout first, gemini-2.5-flash-lite
    as fallback if the primary parse-fails. Falls back to direct ANN if both fail.

    Returns
    -------
    (plan, sub_queries, rewrite_latency_s, model_used)
    """
    from .rewriter import rewrite_query_cascade

    sub_queries, rw_lat, model_used = rewrite_query_cascade(user_query, api_key)

    if not sub_queries:
        logger.warning("Both rewriters failed for %r — direct ANN fallback", user_query)
        return query_time(user_query, graph, host, port, collection, top_k, threshold), [], rw_lat, "direct"

    # Union ANN results across all sub-queries (max score per col_id)
    best_score: dict[str, float] = {}
    for sq in sub_queries:
        q_vec = _encode([sq])[0]
        hits  = search_columns(
            query_vector=q_vec,
            host=host, port=port, collection=collection,
            top_k=top_k, threshold=threshold,
        )
        for col_id, score in hits:
            if score > best_score.get(col_id, -1):
                best_score[col_id] = score

    if not best_score:
        return query_time(user_query, graph, host, port, collection, top_k, threshold), sub_queries, rw_lat, model_used

    plan: RetrievalPlan = defaultdict(DBPlan)
    merged_hits = sorted(best_score.items(), key=lambda x: -x[1])
    for col_id, score in merged_hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        plan[col.db].tables.add(col.table)
        plan[col.db].filter_cols.append((col_id, score))

    for col_id, _score in merged_hits:
        col = graph.col_nodes.get(col_id)
        if col is None:
            continue
        for nbr_id, etype in graph.adjacency.get(col_id, []):
            if etype != "fk":
                continue
            nbr = graph.col_nodes.get(nbr_id)
            if nbr and nbr.db == col.db:
                plan[col.db].tables.add(nbr.table)

    return dict(plan), sub_queries, rw_lat, model_used


# ─── Pretty-print ─────────────────────────────────────────────────────────────

def format_plan(plan: RetrievalPlan) -> str:
    lines = []
    for db, dp in sorted(plan.items()):
        lines.append(f"DB: {db}")
        lines.append(f"  tables : {sorted(dp.tables)}")
        for col_id, score in sorted(dp.filter_cols, key=lambda x: -x[1]):
            col_name = col_id.split(".")[-1]
            lines.append(f"  [{score:.3f}] {col_id}  ({col_name})")
    return "\n".join(lines)
