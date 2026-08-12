"""
Qdrant wrapper for the Schema KG column index.

upsert_columns  — write queryable-column vectors + metadata into Qdrant.
search_columns  — ANN search: query_vector → top-K (col_id, score) pairs.

Connects to the existing BioChirp Qdrant instance (bioc_qdrant) via REST on
localhost:6333 when called from the host, or via bioc_qdrant:6333 inside Docker.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from .config import (
    QDRANT_COLLECTION,
    QDRANT_HOST,
    QDRANT_PORT,
    TOP_K,
    THRESHOLD,
)
from .graph import SchemaGraph

logger = logging.getLogger(__name__)

_client: Optional[QdrantClient] = None


def _get_client(host: str = QDRANT_HOST, port: int = QDRANT_PORT) -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(host=host, port=port, timeout=60)
        logger.info("Qdrant client connected to %s:%d", host, port)
    return _client


# ─── Upsert ──────────────────────────────────────────────────────────────────

def upsert_columns(
    graph:       SchemaGraph,
    embeddings:  Dict[str, np.ndarray],
    host:        str = QDRANT_HOST,
    port:        int = QDRANT_PORT,
    collection:  str = QDRANT_COLLECTION,
) -> None:
    """Create (or recreate) the Qdrant collection and upsert all column vectors."""
    client = _get_client(host, port)

    dim = next(iter(embeddings.values())).shape[0]

    # recreate the collection (idempotent for a build step)
    existing = [c.name for c in client.get_collections().collections]
    if collection in existing:
        logger.info("Recreating existing collection %r …", collection)
        client.delete_collection(collection)

    client.create_collection(
        collection_name=collection,
        vectors_config=qmodels.VectorParams(
            size=dim,
            distance=qmodels.Distance.COSINE,
        ),
    )
    logger.info("Created Qdrant collection %r (%d-dim cosine)", collection, dim)

    points = []
    for idx, (col_id, vec) in enumerate(embeddings.items()):
        col = graph.col_nodes[col_id]
        points.append(
            qmodels.PointStruct(
                id=idx,
                vector=vec.tolist(),
                payload={
                    "col_id":       col_id,
                    "db":           col.db,
                    "table":        col.table,
                    "column":       col.column,
                    "concept_type": col.concept_type,
                    "description":  col.description,
                },
            )
        )

    # batch upsert in chunks of 256
    batch_size = 256
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection, points=points[i : i + batch_size])

    logger.info("Upserted %d column vectors into %r", len(points), collection)


# ─── Search ──────────────────────────────────────────────────────────────────

def search_columns(
    query_vector: np.ndarray,
    host:         str   = QDRANT_HOST,
    port:         int   = QDRANT_PORT,
    collection:   str   = QDRANT_COLLECTION,
    top_k:        int   = TOP_K,
    threshold:    float = THRESHOLD,
) -> List[Tuple[str, float]]:
    """
    ANN search over the schema column index.

    Returns a list of (col_id, cosine_score) pairs with score >= threshold,
    ordered by descending score.
    """
    client = _get_client(host, port)
    result = client.query_points(
        collection_name=collection,
        query=query_vector.tolist(),
        limit=top_k,
        score_threshold=threshold,
        with_payload=True,
    )
    return [(h.payload["col_id"], h.score) for h in result.points]
