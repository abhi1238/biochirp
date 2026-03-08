


import os
import sys
import time
import logging
import asyncio
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path
import pickle
import httpx

import numpy as np
import pandas as pd

# Guardrail framework
from config.guardrail import (
    ParsedValue,
    Llm_Member_Selector_Output,
    Llm_Member_Selector_Input
)

# ML/AI libraries
from sentence_transformers import SentenceTransformer

# Vector database
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# Local imports
from .filter import search_reference_term_all_models_FAST
from config.settings import (
    BIOMEDICAL_MODELS,
    SUPPORTED_DBS,
    DB_VALUE_PATH
)

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
USE_KNEE_CUT_OFF = os.getenv("USE_KNEE_CUT_OFF", "True").lower() in {"1", "true", "yes"}
KNEE_CUT_OFF = float(os.getenv("KNEE_CUT_OFF", "1"))
def _build_llm_filter_url() -> str:
    url = os.getenv("LLM_FILTER_URL")
    if url:
        return url
    host = os.getenv("LLM_FILTER_HOST", "biochirp_llm_filter_tool")
    port = os.getenv("LLM_FILTER_PORT", "8017")
    return f"http://{host}:{port}/llm_member_selection_filter"


LLM_FILTER_URL = _build_llm_filter_url()
LLM_FILTER_TIMEOUT = float(os.getenv("LLM_FILTER_TIMEOUT", "30"))
MAX_CONCURRENT_LLM_REQUESTS = int(os.getenv("MAX_CONCURRENT_LLM_REQUESTS", "5"))
QDRANT_LIMIT_PER_MODEL = int(os.getenv("QDRANT_LIMIT_PER_MODEL", "200"))
QDRANT_HNSW_EF = int(os.getenv("QDRANT_HNSW_EF", "512"))
QDRANT_SEARCH_TIMEOUT = float(os.getenv("QDRANT_SEARCH_TIMEOUT", "60"))
USE_DOUBLE_FILTER = os.getenv("USE_DOUBLE_FILTER", "False").lower() in {"1", "true", "yes"}

# Module-level caches (loaded at startup, not on import or first request)
_db_value_cache: Optional[Dict] = None
_model_cache: Optional[Dict[str, SentenceTransformer]] = None
_qdrant_client: Optional[QdrantClient] = None


@dataclass
class QdrantConfig:
    """Configuration for Qdrant client."""
    url: str = "http://localhost:6333"
    distance: qm.Distance = qm.Distance.COSINE
    create_payload_indexes: bool = True
    index_text_field: bool = False
    
    # HNSW optimization parameters
    hnsw_m: int = 16
    hnsw_ef_construct: int = 100
    hnsw_ef: int = 64


def load_db_values() -> Dict:
    """
    Load database values from pickle file.
    
    This should be called during app startup, not on module import.
    
    Returns:
        Dict with database values
        
    Raises:
        FileNotFoundError: If pickle file doesn't exist
        Exception: If loading fails
    """
    try:
        db_path = Path(DB_VALUE_PATH)
        
        if not db_path.exists():
            raise FileNotFoundError(f"Database values file not found: {db_path}")
        
        logger.info(f"Loading database values from {db_path}")
        
        with open(db_path, 'rb') as f:
            db_values = pickle.load(f)
        
        available_dbs = list(db_values.keys())
        logger.info(
            f"Loaded database values successfully. "
            f"Available databases: {available_dbs}"
        )
        
        return db_values
        
    except Exception as e:
        logger.exception(f"Failed to load database values: {e}")
        raise


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
        try:
            logger.info(f"Loading model: {model_name}")
            models[model_name] = SentenceTransformer(model_name)
            logger.info(f"Successfully loaded model: {model_name}")
        except Exception as e:
            logger.error(f"Failed to load model {model_name}: {e}")
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
    
    return models


def create_qdrant_client(cfg: Optional[QdrantConfig] = None) -> QdrantClient:
    """
    Create Qdrant client.
    
    This should be called during app startup.
    Prefers gRPC for better performance; falls back to HTTP if unavailable.
    
    Args:
        cfg: Optional QdrantConfig
        
    Returns:
        Configured QdrantClient
    """
    if cfg is None:
        cfg = QdrantConfig(index_text_field=False)
    
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
    Initialize all resources (database values, models, Qdrant client).
    
    Call this from FastAPI startup event (@app.on_event("startup")).
    
    Raises:
        Exception: If any critical resource fails to load
    """
    global _db_value_cache, _model_cache, _qdrant_client
    
    logger.info("=" * 70)
    logger.info("Initializing Semantic Similarity Service resources...")
    logger.info("=" * 70)
    
    start_time = time.time()
    
    try:
        # Load database values
        logger.info("[1/3] Loading database values...")
        _db_value_cache = load_db_values()
        logger.info("[1/3] ✓ Database values loaded")
        
        # Load SentenceTransformer models
        logger.info("[2/3] Loading SentenceTransformer models...")
        _model_cache = load_models()
        logger.info(f"[2/3] ✓ Loaded {len(_model_cache)} models")
        
        # Connect to Qdrant
        logger.info("[3/3] Connecting to Qdrant...")
        _qdrant_client = create_qdrant_client()
        logger.info("[3/3] ✓ Connected to Qdrant")
        
        elapsed = time.time() - start_time
        
        logger.info("=" * 70)
        logger.info(f"All resources initialized successfully in {elapsed:.2f}s")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.exception("Failed to initialize resources")
        raise


def get_db_values() -> Dict:
    """
    Get loaded database values.
    
    Returns:
        Dict with database values
        
    Raises:
        RuntimeError: If database values not initialized
    """
    if _db_value_cache is None:
        raise RuntimeError(
            "Database values not initialized. "
            "Call initialize_resources() during app startup."
        )
    return _db_value_cache


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


async def call_llm_filter_for_term(
    field_name: str,
    single_term: str,
    candidates: List[str],
    double_filter: bool = False
) -> List[str]:
    """
    Call LLM filter service for a single term.
    
    Args:
        field_name: Category/field name
        single_term: Single term to filter
        candidates: List of candidate matches
        double_filter: If True, run LLM filter twice (for extra refinement)
        
    Returns:
        List of filtered matches, empty list on error
    """
    if not candidates:
        return []
    
    try:
        input_data = Llm_Member_Selector_Input(
            category=field_name,
            single_term=single_term,
            string_list=candidates
        ).model_dump()
        
        async with httpx.AsyncClient() as client_http:
            response = await client_http.post(
                LLM_FILTER_URL,
                json=input_data,
                timeout=LLM_FILTER_TIMEOUT
            )
            response.raise_for_status()
            
            result = response.json()
            
            if not isinstance(result, dict) or "value" not in result:
                logger.warning(
                    f"Invalid LLM filter response for term '{single_term}': {result}"
                )
                return []
            
            matches = result["value"]
            
            if not isinstance(matches, list):
                logger.warning(
                    f"LLM filter returned non-list for term '{single_term}': {type(matches)}"
                )
                return []
            
            # Optional: Run second pass for refinement
            if double_filter and matches:
                logger.debug(
                    f"Running second LLM filter pass for term '{single_term}' "
                    f"with {len(matches)} candidates"
                )
                
                input_data_2 = Llm_Member_Selector_Input(
                    category=field_name,
                    single_term=single_term,
                    string_list=matches
                ).model_dump()
                
                response_2 = await client_http.post(
                    LLM_FILTER_URL,
                    json=input_data_2,
                    timeout=LLM_FILTER_TIMEOUT
                )
                response_2.raise_for_status()
                
                result_2 = response_2.json()
                if isinstance(result_2, dict) and "value" in result_2:
                    matches = result_2["value"]
            
            logger.info(
                f"LLM filter found {len(matches)} matches for '{single_term}' "
                f"in {field_name}"
            )
            return matches
            
    except httpx.TimeoutException:
        logger.error(f"Timeout calling LLM filter for term '{single_term}'")
        return []
    except httpx.HTTPError as e:
        logger.error(f"HTTP error calling LLM filter for term '{single_term}': {e}")
        return []
    except Exception as e:
        logger.exception(f"Error calling LLM filter for term '{single_term}': {e}")
        return []


async def compute_similarity_filtered_outputs(
    parsed: Dict[str, Any],
    db: str
) -> Dict[str, Any]:
    """
    Compute similarity-filtered outputs using Qdrant + LLM filtering.
    
    Args:
        parsed: Parsed query values
        db: Target database name (case insensitive)
        
    Returns:
        Dict with filtered results per field
    """
    logger.info(f"[similarity filter] Input: {parsed}, Database: {db}")
    
    # Get pre-loaded resources (fail-fast if not initialized)
    try:
        db_value = get_db_values()
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
        
        # Get database choices with lowercase key
        db_choices = (db_value.get(db_lookup_key, {}) or {}).get(field_name) or []
        
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
        
        # Aggregate Qdrant matches for all terms
        aggregated_matches_set = set()
        
        for term in user_terms:
            try:
                logger.info(f"[QDRANT] Searching '{term}' in {db_lookup_key}.{field_name}")
                
                # Pass lowercase database key to Qdrant whitelist
                matched_df = await asyncio.wait_for(
                    asyncio.to_thread(
                        search_reference_term_all_models_FAST,
                        client,
                        term,
                        field_name,
                        model_cache,
                        limit_per_model=QDRANT_LIMIT_PER_MODEL,
                        use_knee_cutoff=USE_KNEE_CUT_OFF,
                        db_whitelist=[db_lookup_key],  # Use lowercase key
                        hnsw_ef=QDRANT_HNSW_EF,
                    ),
                    timeout=QDRANT_SEARCH_TIMEOUT
                )
                
                # Extract text results
                matched_texts = []
                if isinstance(matched_df, pd.DataFrame) and not matched_df.empty:
                    matched_texts = matched_df["text"].tolist()
                elif isinstance(matched_df, dict) and "text" in matched_df:
                    text_value = matched_df["text"]
                    if isinstance(text_value, list):
                        matched_texts = text_value
                    elif isinstance(text_value, str):
                        matched_texts = [text_value]
                    else:
                        logger.warning(
                            f"[QDRANT] Unexpected text value type: {type(text_value)}"
                        )
                
                # Add to aggregated set (lowercase for matching)
                for text in matched_texts:
                    if text:
                        aggregated_matches_set.add(str(text).lower())
                
                logger.info(
                    f"[QDRANT] Found {len(matched_texts)} matches for '{term}' "
                    f"in {db_lookup_key}.{field_name}"
                )
                
            except asyncio.TimeoutError:
                logger.error(
                    f"[QDRANT] Timeout searching for '{term}' in {db_lookup_key}.{field_name} "
                    f"after {QDRANT_SEARCH_TIMEOUT}s"
                )
            except Exception as e:
                logger.exception(
                    f"[QDRANT] Error searching for '{term}' in {db_lookup_key}.{field_name}: {e}"
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
        
        logger.info(
            f"[QDRANT] After filtering db_choices: {len(final_filtered)} candidates"
        )
        
        if not final_filtered:
            logger.info(f"[similarity filter] No Qdrant matches for field '{field_name}'")
            similarity_filtered_by_db[field_name] = []
            continue
        
        # Call LLM filter for EACH term in parallel
        logger.info(
            f"[LLM filter] Running LLM filter for {len(user_terms)} terms "
            f"against {len(final_filtered)} candidates (double_filter={USE_DOUBLE_FILTER})"
        )
        
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)
        
        async def filtered_call(term):
            async with semaphore:
                return await call_llm_filter_for_term(
                    field_name,
                    term,
                    final_filtered,
                    double_filter=USE_DOUBLE_FILTER
                )
        
        # Run LLM filter for all terms in parallel
        llm_results = await asyncio.gather(
            *[filtered_call(term) for term in user_terms],
            return_exceptions=True
        )
        
        # Combine and deduplicate results
        final_matches_set = set()
        error_count = 0
        
        for i, result in enumerate(llm_results):
            if isinstance(result, Exception):
                error_count += 1
                logger.error(
                    f"[LLM filter] Error for term '{user_terms[i]}': {result}"
                )
                continue
            
            if isinstance(result, list):
                final_matches_set.update(result)
            else:
                logger.warning(
                    f"[LLM filter] Unexpected result type for term '{user_terms[i]}': "
                    f"{type(result)}"
                )
        
        if error_count > 0:
            logger.warning(
                f"[LLM filter] {error_count}/{len(user_terms)} calls failed "
                f"for field '{field_name}'"
            )
        
        final_matches = list(final_matches_set)
        
        logger.info(
            f"[QDRANT+LLM filter] {db_lookup_key}.{field_name}: {len(final_matches)} final matches"
        )
        
        similarity_filtered_by_db[field_name] = final_matches
    
    logger.info(f"[similarity filter] Finished for database {db_lookup_key}")
    return similarity_filtered_by_db
