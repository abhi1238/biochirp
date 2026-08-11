import os
import sys
import time
import logging
import asyncio
from typing import List, Optional, Dict, Any
from pathlib import Path

import pandas as pd

# Guardrail framework
from config.guardrail import (
    ParsedValue,
)

# ML/AI libraries
from sentence_transformers import SentenceTransformer

# Vector database
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# Local imports
from .filter import search_reference_terms_BATCH
from config.settings import (
    BIOMEDICAL_MODELS,
    embedding_revision,
)
from utils.concept_values import get_db_concept_values

# External libraries
try:
    from kneed import KneeLocator
except ImportError:
    KneeLocator = None
    logging.warning("kneed not installed. KneeLocator functionality disabled.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
# Master switch for the adaptive cutoff system. When False, neither the
# knee detector nor the score floor is applied — Qdrant returns up to
# QDRANT_LIMIT_PER_MODEL hits per (model, db, term) regardless of score.
USE_KNEE_CUT_OFF = os.getenv("USE_KNEE_CUT_OFF", "True").lower() in {"1", "true", "yes"}
# Cosine-similarity floor used ONLY when USE_KNEE_CUT_OFF=True. Applied
# both server-side to Qdrant (score_threshold on SearchRequest, drops weak
# hits before the network round-trip) and client-side as a minimum the
# adaptive knee detector cannot undo. The documented default (.env.example)
# is 0.50; the in-code default 0.0 means "off until you opt in via env".
KNEE_CUT_OFF = float(os.getenv("KNEE_CUT_OFF", "0.0"))
QDRANT_LIMIT_PER_MODEL = int(os.getenv("QDRANT_LIMIT_PER_MODEL", "200"))
QDRANT_HNSW_EF = int(os.getenv("QDRANT_HNSW_EF", "512"))
QDRANT_SEARCH_TIMEOUT = float(os.getenv("QDRANT_SEARCH_TIMEOUT", "60"))

# Module-level caches (loaded at startup, not on import or first request)
_model_cache: Optional[Dict[str, SentenceTransformer]] = None
_qdrant_client: Optional[QdrantClient] = None


def load_models() -> Dict[str, SentenceTransformer]:
    """
    Load all SentenceTransformer models.
    
    This should be called during app startup, not on module import.
    
    Returns:
        Dict mapping model name to loaded model
        
    Raises:
        RuntimeError: If no models can be loaded
    """
    logger.info(f"Loading {len(BIOMEDICAL_MODELS)} SentenceTransformer models...")
    start_time = time.time()
    
    models = {}
    failed_models = []
    
    for model_name in BIOMEDICAL_MODELS:
        # Pin to the exact commit registered in config/settings.py
        # EMBEDDING_MODELS. Without a revision pin, SentenceTransformer
        # silently follows the model's `main` branch, which causes the
        # query-time embeddings to drift away from the Qdrant index.
        from config.settings import EMBEDDING_MODELS

        revision = embedding_revision(model_name)
        if revision is None:
            logger.error(
                f"Model {model_name} is not registered in "
                f"config.settings.EMBEDDING_MODELS — refusing to load "
                f"unpinned. Add it (with a revision) before using."
            )
            failed_models.append(model_name)
            continue
        try:
            # Check if this is a local model (EMBEDDING_MODELS entry with a local_path)
            model_entry = EMBEDDING_MODELS.get(model_name, {})
            local_path = model_entry.get("local_path")

            if local_path:
                # Local model: resolve path relative to repo root
                model_id = str(Path(__file__).resolve().parents[4] / local_path)
                logger.info(f"Loading local model: {model_name} from {model_id}  (hash {revision[:12]})")
            else:
                # HuggingFace model
                model_id = model_name
                logger.info(f"Loading HuggingFace model: {model_name}  (revision {revision[:12]})")

            models[model_name] = SentenceTransformer(model_id, revision=revision)
            logger.info(f"Successfully loaded model: {model_name} (hash/revision {revision[:12]})")
        except Exception as e:
            logger.error(f"Failed to load model {model_name} (hash/revision {revision[:12]}): {e}")
            failed_models.append(model_name)
    
    elapsed = time.time() - start_time
    
    if not models:
        raise RuntimeError(
            f"Failed to load any SentenceTransformer models. "
            f"Failed models: {failed_models}"
        )
    
    logger.info(
        f"Loaded {len(models)}/{len(BIOMEDICAL_MODELS)} models in {elapsed:.2f}s"
    )

    if failed_models:
        logger.warning(f"Failed to load models: {failed_models}")

    # Warm-up: first inference on a sentence-transformer is 5-10× slower than
    # subsequent calls (CUDA kernel selection, cuDNN autotune, tokenizer regex
    # JIT, allocator sizing). Pay that cost ONCE at startup so the FIRST real
    # user query doesn't see an unexplained 1-2 second latency spike.
    warm_t0 = time.time()
    for _name, _m in models.items():
        try:
            _ = _m.encode(["warmup"], convert_to_numpy=True, show_progress_bar=False)
        except Exception as _e:
            logger.warning(f"Warmup encode failed for {_name}: {_e}")
    logger.info(f"Warm-up encode done in {time.time() - warm_t0:.2f}s")

    return models


def create_qdrant_client() -> QdrantClient:
    """
    Create Qdrant client.

    This should be called during app startup.
    Prefers gRPC for better performance; falls back to HTTP if unavailable.

    Returns:
        Configured QdrantClient
    """
    try:
        logger.info("Connecting to Qdrant via gRPC...")
        client = QdrantClient(
            host="bioc_qdrant",
            port=6333,
            grpc_port=6334,
            prefer_grpc=True,
            timeout=300.0
        )
        logger.info("Connected to Qdrant via gRPC successfully")
        return client
        
    except Exception as e:
        logger.warning(f"gRPC connection failed ({e}), falling back to HTTP")
        
        try:
            client = QdrantClient(
                host="bioc_qdrant",
                port=6333,
                prefer_grpc=False,
                timeout=300.0
            )
            logger.info("Connected to Qdrant via HTTP successfully")
            return client
            
        except Exception as e2:
            logger.error(f"Failed to connect to Qdrant: {e2}")
            raise


def initialize_resources():
    """
    Initialize all resources (models, Qdrant client).

    Call this from FastAPI startup event (@app.on_event("startup")).
    DB concept values are loaded lazily per-DB on first request via
    utils.concept_values.get_db_concept_values.

    Raises:
        Exception: If any critical resource fails to load
    """
    global _model_cache, _qdrant_client

    logger.info("=" * 70)
    logger.info("Initializing Semantic Similarity Service resources...")
    logger.info("=" * 70)

    start_time = time.time()

    try:
        # Load SentenceTransformer models
        logger.info("[1/2] Loading SentenceTransformer models...")
        _model_cache = load_models()
        logger.info(f"[1/2] ✓ Loaded {len(_model_cache)} models")

        # Connect to Qdrant
        logger.info("[2/2] Connecting to Qdrant...")
        _qdrant_client = create_qdrant_client()
        logger.info("[2/2] ✓ Connected to Qdrant")

        elapsed = time.time() - start_time
        logger.info("=" * 70)
        logger.info(f"All resources initialized successfully in {elapsed:.2f}s")
        logger.info("=" * 70)

    except Exception as e:
        logger.exception("Failed to initialize resources")
        raise


def get_model_cache() -> Dict[str, SentenceTransformer]:
    """
    Get loaded SentenceTransformer models.
    
    Returns:
        Dict mapping model name to loaded model
        
    Raises:
        RuntimeError: If models not initialized
    """
    if _model_cache is None:
        raise RuntimeError(
            "Models not initialized. "
            "Call initialize_resources() during app startup."
        )
    return _model_cache


def get_qdrant_client() -> QdrantClient:
    """
    Get Qdrant client.
    
    Returns:
        QdrantClient instance
        
    Raises:
        RuntimeError: If Qdrant client not initialized
    """
    if _qdrant_client is None:
        raise RuntimeError(
            "Qdrant client not initialized. "
            "Call initialize_resources() during app startup."
        )
    return _qdrant_client




async def compute_similarity_filtered_outputs(
    parsed: Dict[str, Any],
    db: str,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    Compute similarity-filtered outputs using Qdrant. Always returns raw
    Qdrant candidates; LLM filtering is handled by the caller
    (expand_and_match_db unified filter). The `raw` parameter is kept for
    API compatibility but has no effect.
    """
    logger.info(f"[similarity filter] Input: {parsed}, Database: {db}")
    
    # Get pre-loaded resources (fail-fast if not initialized)
    try:
        model_cache = get_model_cache()
        client = get_qdrant_client()
    except RuntimeError as e:
        logger.error(f"[similarity filter] Resources not initialized: {e}")
        return ParsedValue().model_dump()
    except Exception as e:
        logger.error(f"[similarity filter] Failed to get resources: {e}")
        return ParsedValue().model_dump()
    
    # Normalize database name to lowercase for ALL lookups
    # This ensures consistency with pickle file AND Qdrant collections
    db_lookup_key = db.lower()
    
    logger.info(
        f"[similarity filter] Using database key '{db_lookup_key}' "
        f"(original: '{db}') for pickle and Qdrant lookups"
    )
    
    # Initialize output
    similarity_filtered_by_db = ParsedValue().model_dump()
    
    for field_name, user_terms in parsed.items():
        # Handle string fields (already matched)
        if isinstance(user_terms, str):
            similarity_filtered_by_db[field_name] = user_terms
            logger.debug(f"[similarity filter] Field '{field_name}' is string, keeping as-is")
            continue
        
        # Skip non-list fields
        if not isinstance(user_terms, list):
            logger.warning(
                f"[similarity filter] Skipping non-list field '{field_name}': {type(user_terms)}"
            )
            similarity_filtered_by_db[field_name] = []
            continue
        
        # Skip empty lists
        if not user_terms:
            logger.info(f"[similarity filter] Field '{field_name}' has empty user_terms")
            similarity_filtered_by_db[field_name] = []
            continue
        
        # Get database choices from per-DB concept-value cache
        db_choices = get_db_concept_values(db_lookup_key).get(field_name) or []
        
        if not db_choices:
            logger.warning(
                f"[similarity filter] No database choices for {db_lookup_key}.{field_name}"
            )
            similarity_filtered_by_db[field_name] = []
            continue
        
        logger.info(
            f"[similarity filter] Processing {len(user_terms)} terms for field '{field_name}' "
            f"against {len(db_choices)} database choices"
        )
        
        # Aggregate Qdrant matches for all terms — BATCHED.
        # One round-trip per model (was: N round-trips per model, one per term).
        # All terms are encoded in one GPU pass; Qdrant search_batch issues
        # one HTTP request with N vector+filter packed payloads.
        aggregated_matches_set: set = set()
        try:
            logger.debug(
                f"[QDRANT BATCH] Searching {len(user_terms)} terms in "
                f"{db_lookup_key}.{field_name}"
            )
            matched_df = await asyncio.wait_for(
                asyncio.to_thread(
                    search_reference_terms_BATCH,
                    client,
                    list(user_terms),
                    field_name,
                    model_cache,
                    limit_per_model=QDRANT_LIMIT_PER_MODEL,
                    use_knee_cutoff=USE_KNEE_CUT_OFF,
                    # Floor is only honoured when the adaptive cutoff is on.
                    score_threshold=KNEE_CUT_OFF if USE_KNEE_CUT_OFF else 0.0,
                    db_whitelist=[db_lookup_key],
                    hnsw_ef=QDRANT_HNSW_EF,
                ),
                timeout=QDRANT_SEARCH_TIMEOUT,
            )
            if isinstance(matched_df, pd.DataFrame) and not matched_df.empty:
                for txt in matched_df["text"].tolist():
                    if txt:
                        aggregated_matches_set.add(str(txt).lower())
            logger.debug(
                f"[QDRANT BATCH] Returned {len(aggregated_matches_set)} unique "
                f"matches for {db_lookup_key}.{field_name}"
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[QDRANT BATCH] Timeout for {db_lookup_key}.{field_name} "
                f"after {QDRANT_SEARCH_TIMEOUT}s"
            )
        except Exception as e:
            logger.exception(
                f"[QDRANT BATCH] Error for {db_lookup_key}.{field_name}: {e}"
            )
        
        logger.info(
            f"[QDRANT] Total unique matches (across all terms): "
            f"{len(aggregated_matches_set)}"
        )
        
        # Filter database choices to only those found by Qdrant
        final_filtered = [
            val for val in db_choices
            if str(val).lower() in aggregated_matches_set
        ]
        
        logger.debug(
            f"[QDRANT] After filtering db_choices: {len(final_filtered)} candidates"
        )
        
        if not final_filtered:
            logger.info(f"[similarity filter] No Qdrant matches for field '{field_name}'")
            similarity_filtered_by_db[field_name] = []
            continue

        # ── Bypass the LLM judge for structured fields ────────────────────
        # Same rationale as STRUCTURED_FIELDS_BYPASS_LLM in the fuzzy tool:
        # for ID-like or short-categorical-like fields the LLM has no useful
        # prior and tends to reject perfectly valid Qdrant matches (e.g.
        # "Glivec" → "Glivec (TN)" for `synonym`, or "EGFR_HUMAN" →
        # "EGFR_HUMAN" for `uniprot_xref`). Qdrant's HNSW + knee cutoff is
        # already a tight relevance filter; the LLM second pass is harmful
        # for these fields.
        _SEMANTIC_LLM_BYPASS = {
            "synonym",
            "uniprot_xref", "uniprot_id",
            "pubchem_cid", "pubchem_sid",
            "cas_number", "cas",
            "chebi_xref", "chebi_id",
            "superdrug_atc", "superdrug_cas",
            "drug_compound_id", "entrez_id", "ensembl_id", "hgnc_id",
            "gene_partner_id", "protein_partner_id", "substrate_id",
            "activity_type", "activity_operator", "activity_unit",
            "target_type", "approval_status",
            "evidence_type", "evidence_direction", "evidence_level",
            "clinical_significance", "review_status", "variant_type",
            "regulation_type", "organism", "species",
            "locus_type", "locus_group", "tier", "role_in_cancer",
            "gene_type", "xref_db", "formula",
        }
        if field_name in _SEMANTIC_LLM_BYPASS:
            # Categorical/enum fields (e.g. regulation_type ∈ {Activation, Repression,
            # Unknown}) must NOT be widened to every choice — Qdrant's HNSW on a
            # 3-element choice list will return all 3 candidates, which then collapses
            # the user's intent ("repression only") into "any of the 3". Accept only
            # candidates that case-insensitively equal one of the user terms.
            _user_lc = {str(t).strip().lower() for t in user_terms if t}
            exact = [c for c in final_filtered if str(c).strip().lower() in _user_lc]
            if exact:
                logger.info(
                    f"[LLM filter] Field '{field_name}' is structured — "
                    f"accepting {len(exact)} exact-match Qdrant matches "
                    f"(dropped {len(final_filtered)-len(exact)} non-exact)"
                )
                similarity_filtered_by_db[field_name] = exact
            else:
                logger.info(
                    f"[LLM filter] Field '{field_name}' is structured — "
                    f"no exact-match candidate; accepting {len(final_filtered)} Qdrant matches as-is"
                )
                similarity_filtered_by_db[field_name] = list(final_filtered)
            continue

        # Return Qdrant candidates — LLM filtering is handled by the caller
        # (expand_and_match_db unified filter). The `raw` parameter is kept
        # for API compatibility but has no effect.
        logger.info(
            f"[similarity filter][RAW] Returning {len(final_filtered)} "
            f"raw Qdrant candidates for '{field_name}'"
        )
        similarity_filtered_by_db[field_name] = list(final_filtered)
    
    logger.info(f"[similarity filter] Finished for database {db_lookup_key}")
    return similarity_filtered_by_db
