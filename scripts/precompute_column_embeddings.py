#!/usr/bin/env python3
"""Precompute per-column schema-description embeddings on the host.

Reads resources/db_column_descriptions.md, runs BGE-small-en-v1.5 (fastembed)
on every (db, table, column) description, and writes a numpy archive that
column_embeddings.py loads at runtime — no fastembed needed inside the
planner container.

Run from repo root:
    /home/abhishekh/anaconda3/bin/python scripts/precompute_column_embeddings.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.utils.column_embeddings import _parse_corpus, _resolve_corpus_path  # noqa: E402
from config import settings  # noqa: E402  — model SSOT (reads .env); never hardcode models

OUT_NPZ = REPO / "resources" / "db_column_embeddings.npz"
_MODEL = settings.COLUMN_EMBED_MODEL  # must match column_embeddings.py query encoder


def main() -> int:
    corpus_path = _resolve_corpus_path()
    print(f"corpus: {corpus_path}")
    rows = _parse_corpus(corpus_path)
    if not rows:
        print("ERROR: corpus is empty", file=sys.stderr)
        return 1
    print(f"rows: {len(rows)} across {len({r['db'] for r in rows})} DBs")

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print("ERROR: fastembed not installed on host. "
              "Use anaconda's python: /home/abhishekh/anaconda3/bin/python", file=sys.stderr)
        return 1

    model = TextEmbedding(_MODEL)
    texts = [r["description"] for r in rows]
    print(f"embedding {len(texts)} descriptions…")
    vectors = list(model.embed(texts))
    matrix = np.asarray(vectors, dtype="float32")
    # L2-normalise so cosine similarity is dot product
    norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
    matrix = matrix / norms

    meta = [
        {"db": r["db"], "table": r["table"], "column": r["column"], "description": r["description"]}
        for r in rows
    ]
    np.savez_compressed(
        OUT_NPZ,
        matrix=matrix,
        meta=np.array(json.dumps(meta), dtype=object),
        model=_MODEL,
        dim=matrix.shape[1],
    )
    print(f"wrote {OUT_NPZ} ({OUT_NPZ.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
