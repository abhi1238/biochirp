"""Per-DB, per-layer LLM rule loader.

Loads optional, layer-scoped instructions from the prompt SSOT
`resources/prompts/db_llm_rules.yaml` (mounted into the per-DB tool and
orchestrator containers via the shared `v-prompts` volume — the SAME mechanism
the summarizer uses for `db_notes.yaml`). `dbs/{db}/` is build/onboarding-time
metadata and is NOT mounted at runtime, so rules must NOT live there.

Each rule is appended ONLY to its specific LLM prompt — nothing leaks across
layers. All keys are optional; absent/empty keys inject nothing (the prompt is
byte-identical to the default, so no benchmark regression).

YAML shape (every key optional):

    hcdt:
      router:        "Extra routing instructions for query_db/web/direct."
      rewriter:      "Extra abbreviation-expansion rules for rephrasing."
      col_selection: "Hints for which columns the schema-mapper should prefer."
      mapper:        "Hints for entity value extraction in the schema-mapper."
      tiebreaker:    "Rules for the dual-mapper tie-break when mapper_1/mapper_2 disagree."
      synthesizer:   "Extra instructions appended to the synthesizer system prompt."

Layer → LLM mapping (all three schema-mapper layers live in the schema_mapper service):
  router, rewriter → the router LLM (orchestrator router_tool + in-process worker)
  col_selection    → schema_mapper service: query_expander (column selection) LLM
  mapper           → schema_mapper service: value_mapper mapper_1/mapper_2 (entity extraction) LLM
  tiebreaker       → schema_mapper service: value_mapper disagreement RESOLVER
                     (dual-mapper tie-breaker — _build_orchestrator_system)
  synthesizer      → schema_kg_chat.py _synthesize_stream: appended to synthesizer system prompt
  (the expand_and_match candidate filter AND the embedding semantic-similarity
   service are intentionally left rule-free)

Usage:
    from .db_llm_rules import load_db_llm_rules
    rules = load_db_llm_rules("hcdt")     # full 6-key dict, cached after first load
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("uvicorn.error")

RULE_KEYS = ("router", "rewriter", "col_selection", "mapper", "tiebreaker", "synthesizer")

_RULES_FILE = "db_llm_rules.yaml"

# Whole-file cache: {db: {key: str}}. None = not yet loaded.
_ALL: dict | None = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROMPT_ROOTS = (
    "/app",  # in-container: /app/resources/prompts/db_llm_rules.yaml
    os.path.normpath(os.path.join(_HERE, "..", "..")),  # repo root in dev
)


def _find_rules_path() -> str | None:
    for root in _PROMPT_ROOTS:
        path = os.path.join(root, "resources", "prompts", _RULES_FILE)
        if os.path.isfile(path):
            return path
    return None


def _load_all() -> dict:
    """Load and cache the whole YAML once. Returns {} if absent/unparseable."""
    global _ALL
    if _ALL is not None:
        return _ALL
    path = _find_rules_path()
    if path is None:
        logger.info("[db_llm_rules] %s not found on any prompt root — no per-DB rules",
                    _RULES_FILE)
        _ALL = {}
        return _ALL
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError("top-level YAML is not a mapping of db → rules")
        _ALL = raw
        logger.info("[db_llm_rules] loaded %d DB blocks from %s", len(raw), path)
    except Exception as exc:
        logger.warning("[db_llm_rules] failed to parse %s: %s", path, exc)
        _ALL = {}
    return _ALL


def load_db_llm_rules(db: str) -> dict:
    """Return the normalised rules dict for `db`.

    Always returns all six keys (missing → "") so callers can index safely.
    Returns all-empty when the DB has no block or the file is absent.
    """
    block = (_load_all().get(db) or {}) if db else {}
    if not isinstance(block, dict):
        logger.warning("[db_llm_rules:%s] block is not a mapping — ignoring", db)
        block = {}
    return {k: str(block.get(k, "") or "").strip() for k in RULE_KEYS}
