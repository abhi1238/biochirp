"""
Staging queue for user-feedback-driven few-shot bank promotion.

Flow
----
  User thumbs-up/down
    → POST /feedback on each per-DB tool server
    → rate-limit check  (per session, per hour, via Redis INCR)
    → LPUSH to Redis list  fewshot_staging:{db}
    → nightly promote_fewshots.py reads queue, validates, upserts to fewshot_bank

Promotion gates (all must pass)
--------------------------------
  1. verdict == "up"            — only upvotes promote
  2. consensus ≥ CONSENSUS_MIN  — distinct session count
  3. trust-weighted sum ≥ TRUST_MIN — guards against single adversarial session
  4. quarantine ≥ QUARANTINE_SECS  — 48 h window for coordinated-attack detection
  5. cosine similarity < DEDUP_THRESHOLD vs existing bank — no near-duplicates

Redis keys (all decode_responses=True)
---------------------------------------
  fewshot_staging:{db}              LIST   — JSON FeedbackEntry objects
  fewshot_rl:{session}:{epoch_h}    STRING — per-hour vote counter (TTL 1 h)
  fewshot_trust:{session}           HASH   — {correct, total} accuracy accumulator
  fewshot_first_ts:{db}:{qhash}     STRING — unix ts of first vote for (db, query)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Tuneable constants (all overridable via env) ──────────────────────────────
RL_MAX              = int(os.getenv("FEWSHOT_RL_MAX",            "20"))
CONSENSUS_MIN_VOTES = int(os.getenv("FEWSHOT_CONSENSUS_MIN",      "3"))
CONSENSUS_TRUST_MIN = float(os.getenv("FEWSHOT_TRUST_MIN",        "2.0"))
QUARANTINE_SECS     = int(os.getenv("FEWSHOT_QUARANTINE_SECS",    str(48 * 3600)))
DEDUP_THRESHOLD     = float(os.getenv("FEWSHOT_DEDUP_THRESHOLD",  "0.92"))
BANK_MAX_PER_STAGE  = int(os.getenv("FEWSHOT_BANK_MAX",           "500"))
STAGING_TTL_DAYS    = int(os.getenv("FEWSHOT_STAGING_TTL_DAYS",   "8"))


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FeedbackEntry:
    session_id:      str
    db:              str
    query:           str
    rephrased_query: str
    parsed_value:    dict
    verdict:         str    # "up" | "down"
    ts:              float  # unix timestamp (time.time())


# ── Key helpers ───────────────────────────────────────────────────────────────

def _staging_key(db: str) -> str:
    return f"fewshot_staging:{db.lower()}"

def _rl_key(session_id: str) -> str:
    return f"fewshot_rl:{session_id}:{int(time.time()) // 3600}"

def _trust_key(session_id: str) -> str:
    return f"fewshot_trust:{session_id}"

def _first_ts_key(db: str, query: str) -> str:
    qhash = query_hash(db, query)
    return f"fewshot_first_ts:{db.lower()}:{qhash}"

def query_hash(db: str, query: str) -> str:
    """Stable 16-char hex identifier for a (db, query) pair."""
    return hashlib.sha1(f"{db}|{query}".lower().encode()).hexdigest()[:16]


# ── Rate limiting ─────────────────────────────────────────────────────────────

def check_rate_limit(r, session_id: str) -> bool:
    """Returns True (within limit) or False (exceeded). Works with sync redis."""
    key = _rl_key(session_id)
    count = r.incr(key)
    if count == 1:
        r.expire(key, 3600)
    return count <= RL_MAX


# ── Push to staging queue ────────────────────────────────────────────────────

def push_feedback(r, entry: FeedbackEntry) -> bool:
    """
    Validate, rate-limit, and push one feedback entry to the staging queue.
    Returns True if queued, False if rate-limited or invalid.
    """
    if entry.verdict not in ("up", "down"):
        return False
    if not entry.query.strip() or not entry.db.strip():
        return False
    if not check_rate_limit(r, entry.session_id):
        logger.info("Feedback rate-limited: session=%s db=%s", entry.session_id, entry.db)
        return False

    payload = json.dumps(asdict(entry), ensure_ascii=False)
    r.lpush(_staging_key(entry.db), payload)

    # Record first-vote timestamp (NX = only if key does not already exist).
    ts_key = _first_ts_key(entry.db, entry.query)
    r.setnx(ts_key, str(entry.ts))
    r.expire(ts_key, STAGING_TTL_DAYS * 24 * 3600)

    return True


# ── Trust scoring ─────────────────────────────────────────────────────────────

def get_trust(r, session_id: str) -> float:
    """
    Trust weight in [0.1, 1.0].  New sessions start at 0.3 (moderate prior).
    Sessions with a poor accuracy record (< 30 % after ≥ 5 votes) are
    penalised to 0.1 — a single adversarial session cannot reach consensus
    alone (would need trust-weighted sum ≥ 2.0, but gets at most 0.1 × N).
    """
    raw = r.hgetall(_trust_key(session_id)) or {}
    correct = int(raw.get("correct", 0))
    total   = int(raw.get("total",   1))
    ratio   = correct / max(total, 1)
    if total >= 5 and ratio < 0.3:
        return 0.1   # likely adversarial
    if total >= 20:
        return min(1.0, 0.3 + 0.7 * ratio)
    return 0.3 + 0.4 * ratio   # moderate while sample is small


def update_trust(r, session_id: str, correct: bool) -> None:
    """Called by the promotion script after judge-verifying a promoted entry."""
    key = _trust_key(session_id)
    r.hincrby(key, "total", 1)
    if correct:
        r.hincrby(key, "correct", 1)
    r.expire(key, 90 * 24 * 3600)  # 90-day rolling window


# ── Load / clear staging queue ───────────────────────────────────────────────

def load_staged(r, db: str) -> List[FeedbackEntry]:
    """Non-destructive read of all staged entries for a database."""
    raw_list = r.lrange(_staging_key(db), 0, -1) or []
    entries: List[FeedbackEntry] = []
    for raw in raw_list:
        try:
            entries.append(FeedbackEntry(**json.loads(raw)))
        except Exception as exc:
            logger.warning("Skipping malformed staging entry: %s", exc)
    return entries


def clear_staged(r, db: str) -> None:
    """Delete the staging queue for a DB after a successful promotion run."""
    r.delete(_staging_key(db))


# ── First-vote timestamp ──────────────────────────────────────────────────────

def get_first_ts(r, db: str, query: str) -> Optional[float]:
    """Return the unix timestamp of the first vote for this (db, query), or None."""
    val = r.get(_first_ts_key(db, query))
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None
