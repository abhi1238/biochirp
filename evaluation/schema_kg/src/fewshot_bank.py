"""
Dynamic per-DB, per-stage few-shot retrieval.

Instead of injecting an entire few-shot block into every LLM prompt, each
pipeline STAGE (router / rewriter / col_selection / mapper / tiebreaker) keeps a
small bank of ~25 curated example questions PER DATABASE. At runtime we embed the
incoming question and pull only the top-K (default 15) most similar examples from
that DB+stage bank — so prompts stay small and the examples are always relevant.

Storage
-------
A single Qdrant collection ``fewshot_bank`` (shared with the existing BioChirp
Qdrant service). Every point:

    vector  = SapBERT-from-PubMedBERT-fulltext-mean-token(question)  # 768-dim, cosine
    payload = { db, stage, question, answer, note, pin }  # answer is a JSON string

SapBERT (the biomedical entity encoder used across BioChirp) is chosen over a
general text embedder so similarity tracks biomedical meaning, not surface words.

``db`` and ``stage`` carry KEYWORD payload indexes so a (db, stage) filter is
exact and cheap. Banks are tiny, so retrieval is effectively exact.

Idempotency
-----------
Point IDs are ``uuid5(db|stage|question)`` and ``ingest_bank`` pre-deletes the
(db, stage) slice before upserting — re-running a notebook cell never duplicates
or strands points.

Graceful degradation
---------------------
``select_fewshots`` NEVER raises: any Qdrant/embedding error returns ``[]`` so
the caller transparently falls back to its prior behaviour (full-inject for the
mapper, no-shot elsewhere). A cold/evicted Qdrant must not be able to hang or
break a live chat.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from .config import QDRANT_HOST, QDRANT_PORT

logger = logging.getLogger(__name__)


_st_model = None


def _embed(texts: List[str]):
    """Encode with SapBERT (mean-token, 768-dim, L2-normalised). Loaded lazily and
    cached so importing this module never pulls in sentence-transformers (heavy,
    and absent in some tooling envs)."""
    global _st_model
    if _st_model is None:
        import numpy as _np  # noqa: F401 — ensure numpy present before model load
        from sentence_transformers import SentenceTransformer
        from config import settings
        name = settings.SAPBERT_MODEL
        rev = (settings.EMBEDDING_MODELS.get(name) or {}).get("revision")
        logger.info("Loading few-shot embedder: %s (rev=%s)", name, rev)
        _st_model = SentenceTransformer(name, revision=rev)
    import numpy as np
    vecs = _st_model.encode(list(texts), batch_size=64, normalize_embeddings=True,
                            show_progress_bar=False, convert_to_numpy=True)
    return vecs.astype(np.float32)

# ─── Constants ────────────────────────────────────────────────────────────────

COLLECTION = "fewshot_bank"

STAGES = ("router", "rewriter", "col_selection", "mapper", "tiebreaker")

# Stable namespace so uuid5(db|stage|question) is reproducible across runs/hosts.
_NS = uuid.UUID("6f1c2a4e-7b3d-5e8f-9a0b-1c2d3e4f5061")

# Default top-K pulled into a stage prompt. Banks hold ~25/stage, so 10 keeps the
# most relevant ~40% and drops the long tail. Override per-deploy via env.
DEFAULT_TOP_K = int(os.getenv("FEWSHOT_TOP_K", "10"))


def _point_id(db: str, stage: str, question: str) -> str:
    return str(uuid.uuid5(_NS, f"{db}|{stage}|{question}"))


# ─── Client (module-cached) ───────────────────────────────────────────────────

_client: Optional[QdrantClient] = None


def _get_client(host: Optional[str] = None, port: Optional[int] = None) -> QdrantClient:
    global _client
    if _client is None:
        _host = host or "127.0.0.1"
        _port = port or int(os.getenv("QDRANT_PORT", str(QDRANT_PORT)))
        _client = QdrantClient(url=f"http://{_host}:{_port}", timeout=10)
    return _client


# ─── Source → per-stage banks ─────────────────────────────────────────────────

def build_stage_banks(source: dict) -> Dict[str, List[dict]]:
    """
    Expand ONE multi-labelled question set into the five per-stage banks.

    `source` shape (one file per DB, e.g. schema_kg/inputs/<db>/fewshots.json):
        {
          "db": "ttd",
          "questions": [
            {"q": str,
             "pv": {<col>: ["v"]|"requested"},   # mapper / tiebreaker answer
             "keep": [<col>, ...],               # col_selection keep
             "drop": [<col>, ...]   (optional),  # col_selection drop hints
             "note": str (optional),
             "pin":  bool (optional),
             "rephrased": str (optional),        # rewriter answer
             "action": "query_db"|"web_search"|"direct_answer" (default query_db)},
            ...
          ],
          "router_boundary": [                    # optional out-of-scope router shots
            {"q": str, "action": str, "note": str, "pin": bool (optional)}, ...
          ]
        }

    Returns {stage -> [{question, answer, note, pin}]} ready for ingest_bank().
    """
    qs = source.get("questions", [])
    mapper, col_sel, router, rewriter = [], [], [], []
    for e in qs:
        q = e["q"]; note = e.get("note", ""); pin = bool(e.get("pin", False))
        if "pv" in e:
            mapper.append({"question": q, "answer": e["pv"], "note": note, "pin": pin})
        if "keep" in e:
            col_sel.append({"question": q,
                            "answer": {"keep": e["keep"], "drop": e.get("drop", [])},
                            "note": note, "pin": pin})
        router.append({"question": q,
                       "answer": {"action": e.get("action", "query_db")},
                       "note": note, "pin": pin})
        if e.get("rephrased"):
            rewriter.append({"question": q, "answer": e["rephrased"],
                             "note": note, "pin": pin})
    for b in source.get("router_boundary", []):
        router.append({"question": b["q"], "answer": {"action": b["action"]},
                       "note": b.get("note", ""), "pin": bool(b.get("pin", False))})
    return {"mapper": mapper, "tiebreaker": list(mapper),
            "col_selection": col_sel, "router": router, "rewriter": rewriter}


# ─── Ingest (called from preprocess_v2.ipynb) ─────────────────────────────────

def ensure_bank_collection(client: QdrantClient, dim: int) -> None:
    """Create ``fewshot_bank`` with db/stage payload indexes, or validate its dim."""
    if not client.collection_exists(COLLECTION):
        logger.info("Creating collection %s (dim=%d)", COLLECTION, dim)
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )
        for fld in ("db", "stage"):
            client.create_payload_index(
                COLLECTION, field_name=fld, field_schema=qm.PayloadSchemaType.KEYWORD
            )
    else:
        info = client.get_collection(COLLECTION)
        v = info.config.params.vectors
        stored = v.size if isinstance(v, qm.VectorParams) else next(iter(v.values())).size
        assert stored == dim, (
            f"{COLLECTION} dim mismatch: stored={stored}, model produces={dim}. "
            f"Delete the collection and re-ingest."
        )


def _stage_filter(db: str, stage: str) -> qm.Filter:
    return qm.Filter(must=[
        qm.FieldCondition(key="db", match=qm.MatchValue(value=db)),
        qm.FieldCondition(key="stage", match=qm.MatchValue(value=stage)),
    ])


def ingest_bank(
    db: str,
    banks: Dict[str, List[dict]],
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Dict[str, int]:
    """
    Embed and upsert a DB's few-shot banks. Idempotent: each (db, stage) slice is
    pre-deleted, then re-upserted.

    Parameters
    ----------
    db    : database tag, e.g. "ttd" (lower-cased here).
    banks : {stage -> [ {"question": str,
                         "answer":  <stage-specific obj, e.g. parsed_value dict>,
                         "note":    str (optional)}, ... ]}
            Only keys in STAGES are processed; unknown stages are skipped.

    Returns {stage -> n_points_ingested}.
    """
    db = db.lower()
    _host = host or "127.0.0.1"
    _port = port or int(os.getenv("QDRANT_PORT", str(QDRANT_PORT)))
    client = QdrantClient(url=f"http://{_host}:{_port}", timeout=60)

    counts: Dict[str, int] = {}
    for stage, examples in banks.items():
        if stage not in STAGES:
            logger.warning("ingest_bank: skipping unknown stage %r", stage)
            continue
        examples = [e for e in (examples or []) if e.get("question", "").strip()]
        if not examples:
            counts[stage] = 0
            continue

        questions = [e["question"].strip() for e in examples]
        vecs = _embed(questions)
        ensure_bank_collection(client, vecs.shape[1])

        # idempotent: drop the prior (db, stage) slice first
        if client.count(COLLECTION, count_filter=_stage_filter(db, stage),
                        exact=True).count > 0:
            client.delete(COLLECTION,
                          points_selector=qm.FilterSelector(filter=_stage_filter(db, stage)),
                          wait=True)

        points = []
        for ex, vec in zip(examples, vecs):
            q = ex["question"].strip()
            points.append(qm.PointStruct(
                id=_point_id(db, stage, q),
                vector=vec.astype("float32").tolist(),
                payload={
                    "db": db,
                    "stage": stage,
                    "question": q,
                    "answer": json.dumps(ex.get("answer"), ensure_ascii=False),
                    "note": ex.get("note", ""),
                    "pin": bool(ex.get("pin", False)),
                },
            ))
        client.upsert(COLLECTION, points=points, wait=True)
        counts[stage] = len(points)
        logger.info("ingest_bank[%s/%s]: %d examples", db, stage, len(points))

    return counts


# ─── Retrieval (called at runtime by each stage) ──────────────────────────────

def _to_item(payload: Optional[dict], score: float) -> dict:
    p = payload or {}
    raw = p.get("answer")
    try:
        answer = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        answer = raw
    return {
        "question": p.get("question", ""),
        "answer": answer,
        "note": p.get("note", ""),
        "score": float(score),
        "pinned": bool(p.get("pin", False)),
    }


def select_fewshots(
    question: str,
    db: str,
    stage: str,
    k: int = DEFAULT_TOP_K,
) -> List[dict]:
    """
    Return examples for (db, stage): ALL pinned examples first (deterministic),
    then the remaining ``k - n_pinned`` slots filled by similarity to ``question``.

    Pinning guarantees a pattern-teaching example is never dropped just because an
    unrelated question doesn't retrieve it. If pinned alone exceed ``k`` they are
    all still returned (mandatory beats the cap).

    Each item: {"question", "answer" (decoded), "note", "score", "pinned"}.

    NEVER raises — on any error (Qdrant down, collection missing, bad input)
    returns ``[]`` so the caller falls back to its prior prompt unchanged.
    """
    if not question or not question.strip() or not db or stage not in STAGES:
        return []
    try:
        db = db.lower()
        k = max(1, int(k))
        client = _get_client()

        # 1) pinned — always included, fetched by payload filter (no vector rank)
        pin_filter = qm.Filter(must=[
            qm.FieldCondition(key="db", match=qm.MatchValue(value=db)),
            qm.FieldCondition(key="stage", match=qm.MatchValue(value=stage)),
            qm.FieldCondition(key="pin", match=qm.MatchValue(value=True)),
        ])
        pinned_pts, _ = client.scroll(
            COLLECTION, scroll_filter=pin_filter, limit=256,
            with_payload=True, with_vectors=False,
        )
        pinned = sorted((_to_item(p.payload, 1.0) for p in pinned_pts),
                        key=lambda x: x["question"])
        seen = {x["question"] for x in pinned}

        # 2) similarity fill for the leftover slots (skip anything already pinned)
        fill: List[dict] = []
        fill_n = max(0, k - len(pinned))
        if fill_n:
            q_vec = _embed([question.strip()])[0]
            hits = client.query_points(
                collection_name=COLLECTION,
                query=q_vec.tolist(),
                query_filter=_stage_filter(db, stage),
                limit=k + len(pinned),   # over-fetch so skipping pinned still leaves enough
                with_payload=True,
            ).points
            hits = sorted(hits, key=lambda h: (-h.score, h.payload.get("question", "")))
            for h in hits:
                q = (h.payload or {}).get("question", "")
                if q in seen:
                    continue
                fill.append(_to_item(h.payload, h.score))
                seen.add(q)
                if len(fill) >= fill_n:
                    break

        return pinned + fill
    except Exception as exc:  # noqa: BLE001 — must never break a live request
        logger.warning("select_fewshots(%s/%s) failed, falling back to no-shot: %s",
                       db, stage, exc)
        return []
