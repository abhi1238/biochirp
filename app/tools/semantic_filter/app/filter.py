# Standard library imports
import os
import sys
import logging
from typing import Any, Dict, List, Optional

# Third-party imports
import numpy as np
import pandas as pd

# ML/AI libraries
from sentence_transformers import SentenceTransformer

# Vector database
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# External libraries
try:
    from kneed import KneeLocator
    KNEED_AVAILABLE = True
except ImportError:
    KneeLocator = None
    KNEED_AVAILABLE = False

# Configure logging (ONCE!)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Silence noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if not KNEED_AVAILABLE:
    logger.warning("kneed not installed - KneeLocator functionality disabled")

# Configuration
KNEE_S_PARAMETER = float(os.getenv("KNEE_S_PARAMETER", "0.5"))


def model_to_collection(model_name: str) -> str:
    """
    Convert model name to collection name format.
    
    Args:
        model_name: Model name (e.g., "sentence-transformers/all-MiniLM-L6-v2")
        
    Returns:
        Collection name (e.g., "emb_sentence-transformers_all-MiniLM-L6-v2")
    """
    if not model_name:
        raise ValueError("model_name cannot be empty")
    
    return f"emb_{model_name.replace('/', '_')}"


def search_reference_terms_BATCH(
    client: QdrantClient,
    reference_terms: List[str],
    target_field: str,
    model_cache: Dict[str, SentenceTransformer],
    limit_per_model: int = 50,
    use_knee_cutoff: bool = True,
    db_whitelist: Optional[List[str]] = None,
    hnsw_ef: int = 64,
    search_timeout: float = 60.0,
    score_threshold: float = 0.0,
) -> pd.DataFrame:
    """Batched multi-term Qdrant search.

    Replaces N round-trips (one per term) with ONE round-trip per (model, db)
    using qdrant-client.search_batch. Encoding is also batched on the GPU.

    Returns a DataFrame with columns: reference_term, model, db, field, score,
    cutoff_used, text, and any additional payload fields.
    """
    if not reference_terms:
        return pd.DataFrame()
    reference_terms = [t for t in reference_terms if isinstance(t, str) and t.strip()]
    if not reference_terms:
        return pd.DataFrame()
    if not target_field or not isinstance(target_field, str):
        raise ValueError("target_field must be a non-empty string")
    if not model_cache:
        raise ValueError("model_cache cannot be empty")
    try:
        limit_per_model = int(limit_per_model)
        hnsw_ef = int(hnsw_ef)
        search_timeout = int(search_timeout)
    except Exception as e:
        raise ValueError(f"limit_per_model/hnsw_ef/search_timeout must be ints: {e}")

    if db_whitelist:
        db_whitelist = [db.lower() for db in db_whitelist if db]

    rows: List[Dict[str, Any]] = []

    dbs = db_whitelist or [None]

    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _do_search(model_name, coll, q_vecs, db_name):
        must = [qm.FieldCondition(key="model", match=qm.MatchValue(value=model_name)),
                qm.FieldCondition(key="field", match=qm.MatchValue(value=target_field))]
        if db_name:
            must.append(qm.FieldCondition(key="db", match=qm.MatchValue(value=db_name)))
        flt = qm.Filter(must=must)
        requests_batch = [
            qm.SearchRequest(
                vector=q_vecs[i].tolist(),
                filter=flt,
                limit=limit_per_model,
                score_threshold=score_threshold if score_threshold > 0 else None,
                with_payload=True,
                params=qm.SearchParams(hnsw_ef=hnsw_ef, exact=False),
            )
            for i in range(len(reference_terms))
        ]
        try:
            results = client.search_batch(
                collection_name=coll, requests=requests_batch, timeout=search_timeout
            )
            return (model_name, coll, db_name, results, None)
        except Exception as e:
            return (model_name, coll, db_name, None, e)

    # Encode+search pipelining. When enabled, each model's Qdrant search starts
    # as soon as that model's encode finishes, overlapping network I/O with
    # subsequent encodes on the same GPU. Default off — set
    # SEMANTIC_PIPELINE_ENCODE=1 to enable and benchmark.
    pipeline_encode = os.getenv("SEMANTIC_PIPELINE_ENCODE", "0") == "1"

    _phase_start = _time.perf_counter()

    if pipeline_encode:
        # Pre-flight: which models have an existing collection?
        eligible = []
        for model_name, model in model_cache.items():
            coll = model_to_collection(model_name)
            try:
                if not client.collection_exists(coll):
                    logger.warning("Collection '%s' does not exist, skipping model '%s'", coll, model_name)
                    continue
            except Exception as e:
                logger.error("Error checking collection '%s': %s", coll, e)
                continue
            eligible.append((model_name, model, coll))

        if not eligible:
            search_results = []
        else:
            # Pool size: enough threads so the LAST encode can start before
            # the FIRST search finishes. With N models × D dbs we need at
            # least N + D workers to fully pipeline.
            n_models = len(eligible)
            n_dbs = max(1, len(dbs))
            pool_size = max(1, min(n_models + n_dbs, 8))

            search_futures = []
            search_results: List[Any] = []
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                # Submit encode jobs one at a time (CUDA serializes them on a
                # single GPU regardless); each completed encode immediately
                # submits its (model × db) search jobs to the same pool.
                encode_futures = {}
                for (model_name, model, coll) in eligible:
                    encode_futures[
                        pool.submit(
                            model.encode,
                            reference_terms,
                            convert_to_numpy=True,
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        )
                    ] = (model_name, coll)

                for enc_fut in as_completed(encode_futures):
                    model_name, coll = encode_futures[enc_fut]
                    try:
                        q_vecs = enc_fut.result()
                    except Exception as e:
                        logger.error(
                            "Failed to encode batch with model '%s': %s", model_name, e
                        )
                        continue
                    for db_name in dbs:
                        search_futures.append(
                            pool.submit(_do_search, model_name, coll, q_vecs, db_name)
                        )

                for s_fut in as_completed(search_futures):
                    search_results.append(s_fut.result())

        logger.info(
            "[PIPELINE] encode+search elapsed=%.2fs (models=%d, dbs=%d)",
            _time.perf_counter() - _phase_start, len(model_cache), len(dbs),
        )
    else:
        # ── Phase 1: GPU-batch encode per model (sequential — single GPU) ──
        encoded_models = []  # list of (model_name, coll, q_vecs)
        for model_name, model in model_cache.items():
            coll = model_to_collection(model_name)
            try:
                if not client.collection_exists(coll):
                    logger.warning("Collection '%s' does not exist, skipping model '%s'", coll, model_name)
                    continue
            except Exception as e:
                logger.error("Error checking collection '%s': %s", coll, e)
                continue
            try:
                q_vecs = model.encode(
                    reference_terms,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                encoded_models.append((model_name, coll, q_vecs))
            except Exception as e:
                logger.error("Failed to encode batch with model '%s': %s", model_name, e)
                continue

        # ── Phase 2: issue all (model × db) Qdrant search_batch calls in parallel ──
        tasks = [
            (model_name, coll, q_vecs, db_name)
            for (model_name, coll, q_vecs) in encoded_models
            for db_name in dbs
        ]
        pool_size = max(1, min(len(tasks), 6))
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = [pool.submit(_do_search, *t) for t in tasks]
            search_results = [f.result() for f in as_completed(futures)]

        logger.info(
            "[SEQUENTIAL] encode+search elapsed=%.2fs (models=%d, dbs=%d)",
            _time.perf_counter() - _phase_start, len(model_cache), len(dbs),
        )

    # ── Phase 3: post-process all hits (knee cutoff + row collection) ──
    for model_name, coll, db_name, results, err in search_results:
        if err is not None:
            logger.error("search_batch failed for '%s' db '%s': %s", coll, db_name, err)
            continue
        if not results:
            continue
        for term_idx, hits in enumerate(results):
            if not hits:
                continue
            term = reference_terms[term_idx]
            threshold = float(score_threshold) if score_threshold > 0 else 0.0
            if use_knee_cutoff and KNEED_AVAILABLE and len(hits) > 1:
                scores = np.array([h.score for h in hits], dtype=np.float32)
                sorted_scores = np.sort(scores)[::-1]
                try:
                    knee = KneeLocator(range(len(sorted_scores)), sorted_scores,
                                       curve="convex", direction="decreasing",
                                       S=KNEE_S_PARAMETER)
                    if knee.knee is not None:
                        knee_idx = max(0, min(len(sorted_scores) - 1, int(knee.knee)))
                        # Floor at score_threshold so the knee cannot drop
                        # below the configured hard minimum.
                        threshold = max(float(sorted_scores[knee_idx]), threshold)
                except Exception as e:
                    logger.warning("KneeLocator failed: %s", e)
            for h in hits:
                if h.score >= threshold:
                    pl = h.payload or {}
                    row = {
                        "reference_term": term,
                        "model": pl.get("model", model_name),
                        "db": pl.get("db", db_name or ""),
                        "field": pl.get("field", target_field),
                        "score": float(h.score),
                        "cutoff_used": float(threshold),
                        "text": pl.get("text", ""),
                    }
                    for k, v in pl.items():
                        if k not in row:
                            row[k] = v
                    rows.append(row)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    logger.debug(
        "[BATCH] %d total results for %d terms in field '%s' across %d dbs",
        len(df), len(reference_terms), target_field, df["db"].nunique() if "db" in df else 0,
    )
    return df
