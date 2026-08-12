"""Hyperparameters for the Schema KG pipeline."""
import os

# Model identifiers come from the repo-wide SSOT (config.settings), which reads
# them from .env / docker-compose. Never read os.environ / hardcode a model here.
from config import settings as _settings

# Embedding blend: own-description weight vs neighbourhood-aggregate weight
ALPHA = 0.9

# BFS depth for neighbourhood aggregation (how many hops from each column node)
BFS_DEPTH = 2

# Edge weights used during neighbourhood aggregation
EDGE_WEIGHTS = {
    "concept_bridge": 1.0,
    "fk":             0.7,
    "belongs_to":     0.3,
}

# ANN retrieval parameters — overridable via .env (SCHEMA_KG_ANN_TOP_K /
# SCHEMA_KG_ANN_THRESHOLD). Defaults are the benchmarked production values.
#
# TOP_K: candidates per ANN search before the LLM filter. 25 covers ~86% of
# HCDT's schema; 15 only covered ~52% (entity columns ranked 16–25th on
# ID-heavy queries and were cut entirely).
#
# THRESHOLD: cosine-similarity floor. 0.30 is the minimum safe value — entity
# columns score 0.31–0.34 on queries focused on the opposite entity type.
# Do NOT lower below 0.25 without re-running the bench.
TOP_K     = int(os.getenv("SCHEMA_KG_ANN_TOP_K",    "25"))
THRESHOLD = float(os.getenv("SCHEMA_KG_ANN_THRESHOLD", "0.30"))

# ANN retrieval embedding model (sentence-transformers, 384-dim). Resolved from
# the SSOT (env SCHEMA_KG_EMBED_MODEL; default BAAI/bge-small-en-v1.5).
EMBED_MODEL = _settings.SCHEMA_KG_EMBED_MODEL

# Qdrant connection (shared with the existing BioChirp Qdrant service)
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))   # HTTP REST; use 6334 for gRPC inside Docker
QDRANT_GRPC_PORT = 6334


def collection_for_db(db_name: str) -> str:
    """Return the Qdrant collection name for a given DB name."""
    return f"schema_kg_{db_name.lower()}"


# Default DB for CLI / standalone helpers when no --db is given.
# Each production container sets SCHEMA_KG_DEFAULT_DB explicitly via env.
# No hardcoded fallback — scripts that omit --db and the env var will
# get an empty string, making the missing config explicit rather than silent.
SCHEMA_KG_DEFAULT_DB = os.getenv("SCHEMA_KG_DEFAULT_DB", "")

# Module-level collection default derived from the above.
# Callers that know their DB should call collection_for_db(db) directly.
QDRANT_COLLECTION = collection_for_db(SCHEMA_KG_DEFAULT_DB) if SCHEMA_KG_DEFAULT_DB else ""
