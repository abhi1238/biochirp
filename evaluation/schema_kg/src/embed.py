"""
Embedding pipeline for the Schema KG.

Step 1 — Embed every node (DB / Table / Column) from its text description
          using BAAI/bge-small-en-v1.5 (384-dim) loaded from HuggingFace.

Step 2 — Neighbourhood aggregation for queryable columns only:
          weighted_sum of neighbour base-vectors → aggregated vector.

Step 3 — Blend and unit-normalise:
          final = normalise(alpha * base[col] + (1-alpha) * aggregated)

The result is a dict: col_id → numpy array (384,)  for every queryable column.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
from sentence_transformers import SentenceTransformer

from .config import ALPHA, BFS_DEPTH, EDGE_WEIGHTS, EMBED_MODEL
from .graph import SchemaGraph

logger = logging.getLogger(__name__)


# ─── Model loading ───────────────────────────────────────────────────────────

_model: Optional[SentenceTransformer] = None


def _load_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("Loading embedding model via sentence-transformers: %s …", EMBED_MODEL)
        _model = SentenceTransformer(EMBED_MODEL)
        logger.info("Model loaded (device: %s)", _model.device)
    return _model


def _encode(texts: List[str]) -> np.ndarray:
    """Encode a list of strings → float32 array of shape (N, 384)."""
    model = _load_model()
    vecs = model.encode(
        texts,
        batch_size=64,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=False,   # we normalise ourselves after aggregation
    )
    return vecs.astype(np.float32)


# ─── Main embedding function ─────────────────────────────────────────────────

def compute_embeddings(graph: SchemaGraph) -> Dict[str, np.ndarray]:
    """
    Compute neighbourhood-aggregated, unit-normalised vectors for every
    queryable column in `graph`.

    Returns
    -------
    final : dict[col_id → np.ndarray(384,)]
    """
    # ── Step 1: embed ALL nodes from text ───────────────────────────────────
    all_node_ids = (
        list(graph.db_nodes.keys())
        + list(graph.table_nodes.keys())
        + list(graph.col_nodes.keys())
    )
    logger.info("Embedding %d nodes …", len(all_node_ids))
    texts = [graph.node_description(nid) for nid in all_node_ids]
    raw_vecs = _encode(texts)

    base: Dict[str, np.ndarray] = dict(zip(all_node_ids, raw_vecs))

    # ── Step 2 + 3: neighbourhood aggregation for queryable columns ──────────
    final: Dict[str, np.ndarray] = {}
    for col in graph.queryable_columns:
        neighbours: List[Tuple[str, str]] = graph.bfs_neighbours(col.col_id, BFS_DEPTH)

        dim = base[col.col_id].shape[0]
        if neighbours:
            weighted_sum   = np.zeros(dim, dtype=np.float32)
            weight_total   = 0.0
            for nbr_id, etype in neighbours:
                if nbr_id not in base:
                    continue
                w             = EDGE_WEIGHTS.get(etype, 0.1)
                weighted_sum += w * base[nbr_id]
                weight_total += w
            aggregated = weighted_sum / weight_total if weight_total > 0 else np.zeros(dim, dtype=np.float32)
        else:
            aggregated = np.zeros(dim, dtype=np.float32)

        blended = ALPHA * base[col.col_id] + (1.0 - ALPHA) * aggregated
        norm    = np.linalg.norm(blended)
        final[col.col_id] = blended / norm if norm > 0 else blended

    logger.info("Embeddings computed for %d queryable columns", len(final))
    return final
