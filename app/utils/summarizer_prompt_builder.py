"""Per-DB summarizer-prompt builder (2-part: shared body + per-DB notes).

Assembles the per-DB user-facing summary prompt at module import from:
  - resources/prompts/summarizer_shared.md   (shared body)
  - resources/prompts/db_notes.yaml          (per-DB `display`, `description`,
    optional `extra_constraints` / `tips` surfaced as highlights)

Adding a new DB only requires one entry in db_notes.yaml — no prompt edit.

Tool containers mount this dir as /app/utils (NOT /app/app/utils, which
is the chat-service pattern), so callers in tool code import as
`from utils.summarizer_prompt_builder import build_summarizer_prompt`.
Chat-service callers can also use `from app.utils...` — both work.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATE_ROOTS = [
    os.path.normpath(os.path.join(_HERE, "..", "..")),  # repo root in dev
    "/app",                                              # container root
]


def _find_prompts_dir() -> str:
    for root in _CANDIDATE_ROOTS:
        p = os.path.join(root, "resources", "prompts")
        if os.path.isdir(p):
            return p
    raise FileNotFoundError(
        "Could not locate resources/prompts/ from any of: " + ", ".join(_CANDIDATE_ROOTS)
    )


@lru_cache(maxsize=1)
def _shared_template() -> str:
    with open(os.path.join(_find_prompts_dir(), "summarizer_shared.md"), "r", encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=1)
def _db_notes() -> dict[str, dict[str, Any]]:
    with open(os.path.join(_find_prompts_dir(), "db_notes.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _format_highlights(rec: dict[str, Any], db_display: str) -> str:
    """Optional per-DB highlight hints appended to the prompt.

    Sourced from db_notes.yaml's `extra_constraints` and `tips` when present.
    These tell the summarizer which DB-specific fields are worth mentioning
    (e.g. for CIViC: evidence_level + clinical_significance). If neither is
    present, return empty string so the prompt stays slim.
    """
    bullets: list[str] = []
    for c in rec.get("extra_constraints") or []:
        if isinstance(c, str) and c.strip():
            bullets.append(f"* {c.strip()}")
    tips = rec.get("tips")
    body = "\n".join(bullets)
    if tips:
        body = (body + "\n\n" if body else "") + tips.strip()
    if not body:
        return ""
    return (
        f"\n## `<{db_display.upper()}-SPECIFIC HIGHLIGHTS>`\n\n"
        f"When summarizing {db_display} results, prefer to surface these "
        f"DB-specific signals if present in the table:\n\n{body}\n"
    )


@lru_cache(maxsize=128)
def build_summarizer_prompt(db: str) -> str:
    """Return the assembled summarizer prompt for `db`.

    Raises KeyError if `db` is not present in db_notes.yaml.

    2026-05-19: maxsize bumped 64→128 (concurrency-headroom; the live
    multi-DB front-doors hold caches for all 26 DBs simultaneously). Call
    `preload_summarizer_prompts()` once at service startup to warm every
    slot before the first request — eliminates per-request LRU contention
    under load.
    """
    notes = _db_notes()
    if db not in notes:
        raise KeyError(f"No summarizer notes for db={db!r}; add it to db_notes.yaml")
    rec = notes[db]

    out = (
        _shared_template()
        .replace("{{DB_DISPLAY}}", str(rec["display"]))
        .replace("{{DB_DESCRIPTION}}", str(rec["description"]))
        .replace("{{DB_HIGHLIGHTS}}", _format_highlights(rec, str(rec["display"])))
    )
    return out


def preload_summarizer_prompts() -> int:
    """Warm every cache (_shared_template, _db_notes, build_summarizer_prompt)
    at startup so the first concurrent request doesn't see lock contention
    on the LRU caches.

    Cheap: one read of summarizer_shared.md (~4 KB) + db_notes.yaml (~10 KB)
    + N string-replace passes (N = 26 DBs at the time of writing). Safe to
    call repeatedly — `lru_cache` makes every call after the first a no-op.

    Returns the number of per-DB prompts compiled.
    """
    _shared_template()           # populate the shared-body cache slot
    notes = _db_notes()          # populate the YAML cache slot
    compiled = 0
    for slug in notes.keys():
        try:
            build_summarizer_prompt(slug)
            compiled += 1
        except Exception:
            # Skip malformed entries — the per-request path raises a clear
            # KeyError on first use, which is the right surface for that bug.
            continue
    return compiled
