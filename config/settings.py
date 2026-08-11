"""
BioChirp runtime settings.

EMBEDDING MODEL PINNING (added 2026-05-12 for paper reproducibility)
---------------------------------------------------------------------
Embedding models are loaded by their Hugging Face repo name AND a specific
commit revision. Without a `revision=` pin, `SentenceTransformer(name)`
silently uses whatever weights happen to be on the `main` branch of that
repo today — which means re-running semantic match six months from now
can return different neighbours from identical code. The Qdrant
collections under qdrant_storage/ were built once; if we drift to a
different revision of the same model later, query-time embeddings will
not be from the same model as the index, producing silent quality loss.

The revisions below were resolved from the Hugging Face API on
2026-05-12. To roll forward to a newer revision:
  1. Re-resolve via:   curl https://huggingface.co/api/models/<repo>/revision/main
  2. Update the entry  in EMBEDDING_MODELS below.
  3. Re-ingest Qdrant  (point IDs are stable, but vectors will change so
                       collections must be rebuilt from the new model).
  4. Re-run scripts/build_qdrant_collection_manifests.py so the new pin
     is recorded alongside the data.
  5. Append a CHANGE-LOG entry below.

CHANGE-LOG:
  2026-06-20  Decommissioned biochirp-bge-v1 (local fine-tuned). Replaced by
              raw BAAI/bge-small-en-v1.5 in all ingest notebooks. Fine-tuned
              model kept for audit trail; local_path still accessible if needed.
  2026-06-19  Registered biochirp-bge-v1 (local fine-tuned 384-dim BGE).
              Fixes query-time blocker: model now discoverable by
              semantic_filter.py. Ingestion notebooks create
              emb_biochirp-bge-v1 collections (TTD, HCDT values).
  2026-05-12  Pinned all 5 embedding models for the first time.
              The Qdrant snapshot in qdrant_storage/ was ingested
              earlier with no revision pin; commit SHAs below are
              best-effort recovery (HF API current `main` as of the
              pinning date). Any drift between the snapshot and these
              pins surfaces as semantic-match quality changes — the
              CI gate in scripts/check_embedding_pins.py blocks
              accidental edits to these revisions.
"""
from __future__ import annotations

import os as _os

# repo name → metadata for the model.
# status:
#   "active"      — loaded at query time; an entry in BIOMEDICAL_MODELS below.
#   "disabled"    — kept here for audit trail; not loaded by any code path.
EMBEDDING_MODELS: dict[str, dict[str, str]] = {
    "malteos/scincl": {
        "revision":    "ebc5348d184ba2fc9beee69b4e394263fce57b2e",
        "resolved_at": "2026-05-12",
        "status":      "disabled",
        "role":        "general scientific text embedder (768d, cosine); Qdrant collection absent",
    },
    "pritamdeka/S-PubMedBERT-MS-MARCO": {
        "revision":    "96786c7024f95c5aac7f2b9a18086c7b97b23036",
        "resolved_at": "2026-05-12",
        "status":      "disabled",
        "role":        "biomedical sentence embedder (768d, cosine); Qdrant collection absent",
    },
    "nuvocare/WikiMedical_sent_biobert": {
        "revision":    "73c69ef2c043764492c98303e1deb6b8d5a7b4fe",
        "resolved_at": "2026-05-12",
        "status":      "disabled",
        "role":        "wiki-medical sentence embedder (768d, cosine); Qdrant collection absent",
    },
    "FremyCompany/BioLORD-2023-S": {
        "revision":    "d5b07a1664df2c989394a249647ca130ba08aafa",
        "resolved_at": "2026-05-12",
        "status":      "disabled",
        "role":        "evaluated, not in active rotation",
    },
    "cambridgeltl/SapBERT-from-PubMedBERT-fulltext": {
        "revision":    "090663c3ae57bf35ffe4d0d468a2a88d03051a4d",
        "resolved_at": "2026-05-12",
        "status":      "disabled",
        "role":        "evaluated, not in active rotation; superseded by -mean-token variant",
    },
    "cambridgeltl/SapBERT-from-PubMedBERT-fulltext-mean-token": {
        "revision":    "9f95c2e962719c70f25bf7a1f33bd8d9e9448750",
        "resolved_at": "2026-06-13",
        "status":      "active",
        "role":        "mean-pooled SapBERT; used in encoder evaluation and HCDT v2 ingest",
    },
    "biochirp-bge-v1": {
        "revision":    "92b8929e7941a6fd1293f57b2b4d68b7",
        "resolved_at": "2026-06-19",
        "status":      "disabled",
        "role":        "local fine-tuned 384-dim BGE (DECOMMISSIONED 2026-06-20); replaced by raw BAAI/bge-small-en-v1.5. Kept for audit trail.",
        "local_path":  "resources/models/biochirp-bge-v1",
    },
    "BAAI/bge-small-en-v1.5": {
        "revision":    "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        "resolved_at": "2026-06-20",
        "status":      "disabled",
        "role":        "raw HuggingFace BGE (384-dim, cosine); Qdrant collection absent",
    },
}


def active_embedding_models() -> list[str]:
    """Repo names of the embedding models loaded at query time."""
    return [name for name, m in EMBEDDING_MODELS.items() if m["status"] == "active"]


def embedding_revision(name: str) -> str | None:
    """Pinned commit SHA for a model, or None if not registered."""
    entry = EMBEDDING_MODELS.get(name)
    return entry["revision"] if entry else None


def biomedical_models() -> list[str]:
    """Production embedding-model set — semantic value filter + Qdrant value ingest.

    Single source of truth: env ``BIOMEDICAL_EMBEDDING_MODELS`` (comma-separated
    HF repo ids). Declared in .env so production controls the set in one place.
    Falls back to the ``active`` models in EMBEDDING_MODELS when the env var is
    unset (standalone / dev). Every name must be registered in EMBEDDING_MODELS
    (the semantic filter refuses to load an unpinned model).
    """
    raw = _os.environ.get("BIOMEDICAL_EMBEDDING_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return active_embedding_models()


# Back-compat: existing callers import BIOMEDICAL_MODELS as a flat list of names.
# Now env-driven (BIOMEDICAL_EMBEDDING_MODELS) with the active subset as default.
BIOMEDICAL_MODELS = biomedical_models()


DB_VALUE_DIR = "resources/values"  # Per-DB pickles: concept_values_{db}.pkl


# ─────────────────────────────────────────────────────────────────────────────
# MODEL SSOT (added 2026-06-20)
# -----------------------------------------------------------------------------
# Single source of truth for every embedding- and LLM-model identifier used at
# runtime. Values are declared in .env / docker-compose (the deployment source
# of truth) and read HERE, in exactly one place.
#
# RULES (repo-wide):
#   * Application code MUST import model names from config.settings — e.g.
#         from config import settings
#         model = settings.SCHEMA_KG_EMBED_MODEL
#     It must NEVER read os.environ for a model, and NEVER hardcode a model
#     string. schema_kg/src/config.py and opentarget_service re-export from here.
#   * Embedding models carry a canonical default (the value the Qdrant indexes
#     were built with) so ingest tooling and standalone scripts resolve without
#     extra env wiring; .env still overrides. The default is the ONE place the
#     literal may appear — that is what "no hardcoding" means here.
#   * LLM models are required (default=None) — they must be set in .env, so a
#     missing/empty value fails loudly instead of silently picking a fallback.
#
# Access is lazy (PEP 562 __getattr__): a service only resolves the settings it
# actually reads, so importing config.settings never forces unrelated env vars
# to be present.
# ─────────────────────────────────────────────────────────────────────────────

# canonical name → (env var, default).  default=None ⇒ required (raises if unset).
_MODEL_SETTINGS: dict[str, tuple[str, str | None]] = {
    # ── embedding models (HuggingFace repo ids) ──────────────────────────────
    "SCHEMA_KG_EMBED_MODEL": ("SCHEMA_KG_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
    "ROW_RELEVANCE_MODEL":   ("ROW_RELEVANCE_MODEL",   "BAAI/bge-small-en-v1.5"),
    "HYBRID_EMBED_MODEL":    ("HYBRID_EMBED_MODEL",    "BAAI/bge-small-en-v1.5"),
    "COLUMN_EMBED_MODEL":    ("COLUMN_EMBED_MODEL",    "BAAI/bge-small-en-v1.5"),
    "SAPBERT_MODEL":         ("SAPBERT_MODEL",
                              "cambridgeltl/SapBERT-from-PubMedBERT-fulltext-mean-token"),
    # ── LLM models (required — declared in .env / docker-compose) ────────────
    "SUMMARIZER_MODEL_NAME":            ("SUMMARIZER_MODEL_NAME",            None),
    "SYNTHESIZER_MODEL_NAME":           ("SYNTHESIZER_MODEL_NAME",           None),
    "WEB_MODEL_NAME":                   ("WEB_MODEL_NAME",                   None),
    "UNIFIED_LLM_MODEL":                ("UNIFIED_LLM_MODEL",                None),
    "SCHEMA_KG_ORCHESTRATOR_MODEL":     ("SCHEMA_KG_ORCHESTRATOR_MODEL",     None),
    "STEP_SUMMARIZER_MODEL":            ("STEP_SUMMARIZER_MODEL",            None),
    "SCHEMA_KG_FILTER_MODEL":           ("SCHEMA_KG_FILTER_MODEL",           None),
    "SCHEMA_KG_ENSEMBLE_MODEL_2":       ("SCHEMA_KG_ENSEMBLE_MODEL_2",       None),
    "SCHEMA_KG_MAPPER_MODEL_1":         ("SCHEMA_KG_MAPPER_MODEL_1",         None),
    "SCHEMA_KG_MAP_ORCHESTRATOR_MODEL": ("SCHEMA_KG_MAP_ORCHESTRATOR_MODEL", None),
    "ORCHESTRATOR_MODEL_NAME":          ("ORCHESTRATOR_MODEL_NAME",          None),
    "TRACE_EXPLAINER_MODEL_NAME":       ("TRACE_EXPLAINER_MODEL_NAME",       None),
    "TEXT2SQL_MODEL":                   ("TEXT2SQL_MODEL",                   "qwen/qwen3-coder-30b-a3b-instruct"),
    # optional comma-separated list (empty ⇒ none); callers parse the string.
    "SCHEMA_KG_GROQ_MODELS":            ("SCHEMA_KG_GROQ_MODELS",            ""),
}


def resolve_model(name: str) -> str:
    """Resolve a model setting from the environment (the ONE place that reads it).

    `name` is the canonical setting key in `_MODEL_SETTINGS`. Returns the env
    value when set; otherwise the registered default. Raises for a required
    setting (default=None) that is unset/empty — no silent model fallback.
    """
    try:
        env_var, default = _MODEL_SETTINGS[name]
    except KeyError:
        raise KeyError(f"Unknown model setting {name!r} — add it to _MODEL_SETTINGS.")
    val = _os.environ.get(env_var)
    if val is None or val == "":
        if default is None:
            raise RuntimeError(
                f"Required model setting {name!r} (env {env_var!r}) is not set. "
                f"Declare it in .env / docker-compose — code must not hardcode models."
            )
        return default
    return val


def get_groq_key(db_name: str = "") -> str:
    """Return the Groq API key for the given DB, falling back to the shared key.

    Per-DB keys are read from GROQ_API_KEY_<DB> (e.g. GROQ_API_KEY_TTD).
    Falls back to GROQ_API_KEY when no per-DB key is configured.
    This lets each database's LLM calls consume its own rate-limit bucket.
    """
    if db_name:
        per_db = _os.environ.get(
            f"GROQ_API_KEY_{db_name.upper().replace('-', '_')}"
        )
        if per_db:
            return per_db
    return _os.environ.get("GROQ_API_KEY", "")


def get_openrouter_key(db_name: str = "") -> str:
    """Return the OpenRouter API key for the given DB, falling back to the shared key.

    Per-DB keys are read from OPENROUTER_API_KEY_<DB> (e.g. OPENROUTER_API_KEY_TTD).
    Falls back to OPENROUTER_API_KEY when no per-DB key is configured.
    This lets each database's LLM calls consume its own rate-limit bucket.
    """
    if db_name:
        per_db = _os.environ.get(
            f"OPENROUTER_API_KEY_{db_name.upper().replace('-', '_')}"
        )
        if per_db:
            return per_db
    return _os.environ.get("OPENROUTER_API_KEY", "")


def __getattr__(name: str) -> str:  # PEP 562 — lazy module-level model constants
    if name in _MODEL_SETTINGS:
        return resolve_model(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_MODEL_SETTINGS.keys()))
