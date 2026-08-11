"""Row-level relevance scoring for the per-DB tool pipeline.

After join_and_filter_database produces the final DataFrame, call
score_and_sort(query, df) to:
  1. Serialize each row as a short text string.
  2. BGE-embed all rows + the query in one batch (fastembed ONNX, CPU, ~80 MB).
  3. Add a `relevance_score` (float32, 0–1) column.
  4. Sort descending so the most relevant rows are first.

Falls back to the original DataFrame (no column, no sort) if fastembed is
unavailable or if the encode step fails — the pipeline is never broken.

The fastembed model is a lazy process-level singleton: it loads once on the
first scored request and is reused across all subsequent calls. ONNX inference
is CPU-only so it does not compete with any GPU workloads.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import polars as pl

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
# Set BIOCHIRP_RELEVANCE_SORT=0 to disable entirely (e.g. for a DB whose rows
# are too wide to serialize quickly).
_ENABLED = os.getenv("BIOCHIRP_RELEVANCE_SORT", "1").lower() not in {"0", "false", "no"}
_MODEL_NAME = settings.ROW_RELEVANCE_MODEL
_BATCH_SIZE = int(os.getenv("ROW_RELEVANCE_BATCH", "512"))
# Cap how many fields per row are included in the text representation.
# Keeps encode time predictable even for wide tables (STRING has ~25 cols).
_MAX_FIELDS = int(os.getenv("ROW_RELEVANCE_MAX_FIELDS", "10"))
# Skip scoring when the table has fewer than this many rows — the sort order
# doesn't matter much for tiny result sets and the encode overhead isn't worth it.
_MIN_ROWS = int(os.getenv("ROW_RELEVANCE_MIN_ROWS", "2"))
_MAX_ROWS = int(os.getenv("ROW_RELEVANCE_MAX_ROWS", "2000"))

# ── Lazy singleton ────────────────────────────────────────────────────────────
_model = None          # None = not yet attempted; False = failed to load
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is not None:
        return _model  # False means "disabled after failed load"
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=_MODEL_NAME)
            logger.info("[row_relevance] loaded %s (ONNX, CPU)", _MODEL_NAME)
            # Run one dummy batch so ONNX JIT-compiles its execution plan now.
            # Without this the first real query pays a 3-4× cold-start penalty.
            _warmup(_model)
        except Exception as exc:
            logger.warning("[row_relevance] fastembed unavailable (%s); scoring disabled", exc)
            _model = False
    return _model


def _warmup(model) -> None:
    try:
        import numpy as np
        dummy = ["warmup text"] * _BATCH_SIZE
        list(model.embed(dummy, batch_size=_BATCH_SIZE))
        logger.info("[row_relevance] ONNX warmup done")
    except Exception as exc:
        logger.warning("[row_relevance] warmup failed (non-fatal): %s", exc)


# ── Row serialisation ─────────────────────────────────────────────────────────
_SKIP_VALUES = frozenset({"None", "nan", "NaT", "null", "", "N/A", "n/a"})


def _row_to_text(row: dict, max_fields: int) -> str:
    """Flatten a row dict to 'col: val | col: val ...' skipping nulls."""
    parts: list[str] = []
    for k, v in row.items():
        if k == "relevance_score":
            continue
        if v is None:
            continue
        sv = str(v).strip()
        if sv in _SKIP_VALUES:
            continue
        parts.append(f"{k}: {sv}")
        if len(parts) >= max_fields:
            break
    return " | ".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────
def score_and_sort(query: str, df: pl.DataFrame) -> pl.DataFrame:
    """Add relevance_score column and sort descending. Returns df unchanged on failure.

    Args:
        query: The user's cleaned query string.
        df:    The fully-filtered polars DataFrame from join_and_filter_database.

    Returns:
        A new DataFrame with a `relevance_score` Float32 column, sorted
        descending by that column. Original DataFrame is returned unmodified
        if scoring is disabled, skipped, or fails.
    """
    if not _ENABLED or df.is_empty() or df.height < _MIN_ROWS or not query.strip():
        return df
    if df.height > _MAX_ROWS:
        logger.info("[row_relevance] skipping BGE sort: %d rows > limit %d", df.height, _MAX_ROWS)
        return df

    model = _get_model()
    if not model:
        return df

    try:
        import numpy as np

        rows = df.to_dicts()
        row_texts = [_row_to_text(r, _MAX_FIELDS) for r in rows]

        # Single embed() call — fastembed handles internal batching.
        # Keeping all texts in one call avoids per-call tokenizer overhead.
        all_texts = [query] + row_texts
        n_texts = len(all_texts)
        n_batches = (n_texts + _BATCH_SIZE - 1) // _BATCH_SIZE
        logger.info("[row_relevance] encoding %d texts in %d batch(es) (batch_size=%d)",
                    n_texts, n_batches, _BATCH_SIZE)
        vecs = np.asarray(
            list(model.embed(all_texts, batch_size=_BATCH_SIZE)),
            dtype="float32",
        )
        logger.info("[row_relevance] encode done: vecs shape=%s", vecs.shape)

        # L2-normalise (fastembed already normalises, but be defensive).
        norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
        vecs_unit = vecs / norms

        q_vec = vecs_unit[0]           # (dim,)
        row_vecs = vecs_unit[1:]       # (N, dim)
        scores = (row_vecs @ q_vec).tolist()   # cosine similarity

        del vecs, vecs_unit, row_vecs, norms

        return (
            df
            .with_columns(
                pl.Series("relevance_score", scores, dtype=pl.Float32).round(4)
            )
            .sort("relevance_score", descending=True)
        )

    except Exception as exc:
        logger.warning("[row_relevance] scoring failed (non-fatal): %s", exc)
        return df


# ── Background pre-warm ───────────────────────────────────────────────────────
# Trigger ONNX JIT compilation at module import time so the first real user
# query doesn't pay the cold-start penalty (3-4× slower on first batch).
if _ENABLED:
    threading.Thread(target=_get_model, daemon=True, name="bge-prewarm").start()
