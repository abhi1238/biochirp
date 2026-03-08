# Standard library imports
import os
import sys
import time
import logging
from typing import Any, Dict, List, Optional, Set

# Third-party imports
import numpy as np
import pandas as pd

# ML/AI libraries
from sentence_transformers import SentenceTransformer

# Vector database
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# External libraries
try:
    from kneed import KneeLocator
    KNEED_AVAILABLE = True
except ImportError:
    KneeLocator = None
    KNEED_AVAILABLE = False

# Configure logging (ONCE!)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Silence noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if not KNEED_AVAILABLE:
    logger.warning("kneed not installed - KneeLocator functionality disabled")

# Configuration
QDRANT_SCROLL_LIMIT = int(os.getenv("QDRANT_SCROLL_LIMIT", "100"))
QDRANT_FACET_TIMEOUT = float(os.getenv("QDRANT_FACET_TIMEOUT", "30"))
MAX_UNIQUE_VALUES = int(os.getenv("MAX_UNIQUE_VALUES", "10000"))
KNEE_S_PARAMETER = float(os.getenv("KNEE_S_PARAMETER", "0.5"))


def model_to_collection(model_name: str) -> str:
    """
    Convert model name to collection name format.
    
    Args:
        model_name: Model name (e.g., "sentence-transformers/all-MiniLM-L6-v2")
        
    Returns:
        Collection name (e.g., "emb_sentence-transformers_all-MiniLM-L6-v2")
    """
    if not model_name:
        raise ValueError("model_name cannot be empty")
    
    return f"emb_{model_name.replace('/', '_')}"


def distinct_values(
    client: QdrantClient,
    collection_name: str,
    field_name: str,
    limit: int = 1000,
    timeout: float = QDRANT_FACET_TIMEOUT
) -> List[str]:
    """
    Get distinct values for a payload field using Qdrant's Facet API.
    
    Falls back to scroll method if Facet API fails.
    
    Args:
        client: QdrantClient instance
        collection_name: Name of the collection
        field_name: Payload field to get distinct values from
        limit: Maximum number of unique values to return
        timeout: Timeout for operations in seconds
    
    Returns:
        List of distinct values for the field
        
    Raises:
        ValueError: If collection_name or field_name is empty
        RuntimeError: If both facet and scroll methods fail
    """
    if not collection_name:
        raise ValueError("collection_name cannot be empty")
    if not field_name:
        raise ValueError("field_name cannot be empty")
    
    try:
        # Try Qdrant Facet API (available in Qdrant 1.12+)
        logger.debug(f"Fetching distinct values for '{field_name}' using Facet API")
        
        facet_result = client.facet(
            collection_name=collection_name,
            key=field_name,
            limit=limit,
            timeout=timeout
        )
        
        # Extract unique values from facet hits
        values = [hit.value for hit in facet_result.hits if hit.value]
        logger.debug(f"Facet API returned {len(values)} distinct values")
        return values
    
    except Exception as e:
        logger.warning(
            f"Facet API failed for field '{field_name}' in collection '{collection_name}': {e}. "
            f"Falling back to scroll method"
        )
        
        # Fallback: scroll through points and collect unique values
        unique_values: Set[str] = set()
        offset = None
        scroll_count = 0
        
        try:
            while True:
                # Prevent infinite loops
                if len(unique_values) >= MAX_UNIQUE_VALUES:
                    logger.warning(
                        f"Reached max unique values limit ({MAX_UNIQUE_VALUES}) "
                        f"for field '{field_name}'"
                    )
                    break
                
                records, offset = client.scroll(
                    collection_name=collection_name,
                    limit=QDRANT_SCROLL_LIMIT,
                    offset=offset,
                    with_payload=[field_name],
                    with_vectors=False,
                    timeout=timeout
                )
                
                if not records:
                    break
                
                scroll_count += 1
                
                for record in records:
                    if record.payload and field_name in record.payload:
                        value = record.payload[field_name]
                        
                        if isinstance(value, list):
                            unique_values.update(str(v) for v in value if v)
                        elif value:
                            unique_values.add(str(value))
                
                if offset is None:
                    break
            
            result = list(unique_values)
            logger.info(
                f"Scroll method found {len(result)} distinct values "
                f"for field '{field_name}' after {scroll_count} scroll(s)"
            )
            return result
            
        except Exception as e2:
            logger.error(f"Scroll fallback also failed: {e2}")
            raise RuntimeError(
                f"Failed to get distinct values for field '{field_name}': {e2}"
            ) from e2


def search_reference_term_all_models_FAST(
    client: QdrantClient,
    reference_term: str,
    target_field: str,
    model_cache: Dict[str, SentenceTransformer],
    limit_per_model: int = 50,
    use_knee_cutoff: bool = True,
    db_whitelist: Optional[List[str]] = None,
    hnsw_ef: int = 64,
    search_timeout: float = 60.0
) -> pd.DataFrame:
    """
    Search for similar terms across multiple models using Qdrant vector search.
    
    Args:
        client: QdrantClient instance
        reference_term: Search term to find similar matches for
        target_field: Field to search in (e.g., "drug_name", "disease_name")
        model_cache: Dict mapping model names to loaded SentenceTransformer models
        limit_per_model: Maximum results per model
        use_knee_cutoff: If True, use KneeLocator to find optimal score threshold
        db_whitelist: Optional list of database names to search (lowercase)
        hnsw_ef: HNSW ef parameter for search accuracy/speed tradeoff
        search_timeout: Timeout for Qdrant operations in seconds
        
    Returns:
        DataFrame with columns: reference_term, model, db, field, score, cutoff_used, text, etc.
        Empty DataFrame if no results found.
        
    Raises:
        ValueError: If inputs are invalid
    """
    # Input validation
    if not reference_term or not isinstance(reference_term, str):
        raise ValueError("reference_term must be a non-empty string")
    
    if not target_field or not isinstance(target_field, str):
        raise ValueError("target_field must be a non-empty string")
    
    if not model_cache:
        raise ValueError("model_cache cannot be empty")
    
    # Defensive casts (env/config can pass floats)
    try:
        limit_per_model = int(limit_per_model)
    except Exception:
        raise ValueError("limit_per_model must be an integer")
    try:
        hnsw_ef = int(hnsw_ef)
    except Exception:
        raise ValueError("hnsw_ef must be an integer")

    try:
        search_timeout = int(search_timeout)
    except Exception:
        raise ValueError("search_timeout must be an integer")

    if limit_per_model < 1:
        raise ValueError("limit_per_model must be at least 1")
    
    logger.info(
        f"Searching for '{reference_term}' in field '{target_field}' "
        f"using {len(model_cache)} models"
    )
    
    # Normalize db_whitelist to lowercase
    if db_whitelist:
        db_whitelist = [db.lower() for db in db_whitelist if db]
        logger.debug(f"Using database whitelist: {db_whitelist}")
    
    rows: List[Dict[str, Any]] = []
    
    for model_name, model in model_cache.items():
        coll = model_to_collection(model_name)
        
        # Check if collection exists
        try:
            if not client.collection_exists(coll):
                logger.warning(
                    f"Collection '{coll}' does not exist, skipping model '{model_name}'"
                )
                continue
        except Exception as e:
            logger.error(f"Error checking collection existence for '{coll}': {e}")
            continue
        
        # Encode reference term
        try:
            logger.debug(f"Encoding '{reference_term}' with model '{model_name}'")
            q_vec = model.encode(
                reference_term,
                convert_to_tensor=True,
                normalize_embeddings=True
            )
        except Exception as e:
            logger.error(f"Failed to encode with model '{model_name}': {e}")
            continue
        
        # Get list of databases to search
        if db_whitelist is not None:
            dbs = db_whitelist
        else:
            try:
                dbs = distinct_values(
                    client,
                    coll,
                    "db",
                    timeout=search_timeout
                )
                # Normalize to lowercase
                dbs = [db.lower() for db in dbs if db]
                logger.debug(f"Found {len(dbs)} databases in '{coll}': {dbs}")
            except Exception as e:
                logger.error(f"Failed to get databases for collection '{coll}': {e}")
                continue
        
        # Search each database
        for db_name in dbs:
            try:
                # Build filter
                flt = qm.Filter(must=[
                    qm.FieldCondition(
                        key="model",
                        match=qm.MatchValue(value=model_name)
                    ),
                    qm.FieldCondition(
                        key="db",
                        match=qm.MatchValue(value=db_name)
                    ),
                    qm.FieldCondition(
                        key="field",
                        match=qm.MatchValue(value=target_field)
                    ),
                ])
                
                # Execute search
                logger.debug(
                    f"Searching in collection '{coll}', "
                    f"db '{db_name}', field '{target_field}'"
                )
                
                hits = client.search(
                    collection_name=coll,
                    query_vector=q_vec,
                    limit=limit_per_model,
                    with_payload=True,
                    query_filter=flt,
                    search_params=qm.SearchParams(
                        hnsw_ef=hnsw_ef,
                        exact=False,
                    ),
                    timeout=search_timeout
                )
                
                if not hits:
                    logger.debug(f"No hits found for db '{db_name}'")
                    continue
                
                logger.debug(f"Found {len(hits)} hits for db '{db_name}'")
                
                # Apply knee cutoff if enabled
                threshold = 0.0
                if use_knee_cutoff and KNEED_AVAILABLE and len(hits) > 1:
                    scores = np.array([h.score for h in hits], dtype=np.float32)
                    sorted_scores = np.sort(scores)[::-1]
                    
                    try:
                        knee = KneeLocator(
                            range(len(sorted_scores)),
                            sorted_scores,
                            curve="convex",
                            direction="decreasing",
                            S=KNEE_S_PARAMETER
                        )
                        
                        if knee.knee is not None:
                            # KneeLocator may return a float index; cast safely
                            knee_idx = int(knee.knee)
                            if knee_idx < 0 or knee_idx >= len(sorted_scores):
                                knee_idx = max(0, min(len(sorted_scores) - 1, knee_idx))
                            threshold = float(sorted_scores[knee_idx])
                            logger.debug(
                                f"Knee cutoff threshold: {threshold:.4f} "
                                f"(at index {knee.knee})"
                            )
                    except Exception as e:
                        logger.warning(f"KneeLocator failed: {e}, using threshold=0")
                        threshold = 0.0
                
                # Collect results above threshold
                results_count = 0
                for h in hits:
                    if h.score >= threshold:
                        pl = h.payload or {}
                        row = {
                            "reference_term": reference_term,
                            "model": pl.get("model", model_name),
                            "db": pl.get("db", db_name),
                            "field": pl.get("field", target_field),
                            "score": float(h.score),
                            "cutoff_used": float(threshold),
                            "text": pl.get("text", ""),
                        }
                        
                        # Add any additional payload fields
                        for k, v in pl.items():
                            if k not in row:
                                row[k] = v
                        
                        rows.append(row)
                        results_count += 1
                
                logger.debug(
                    f"Added {results_count} results above threshold "
                    f"for db '{db_name}'"
                )
                
            except Exception as e:
                logger.error(
                    f"Error searching db '{db_name}' in collection '{coll}': {e}"
                )
                continue
    
    # Create DataFrame
    if not rows:
        logger.info(f"No results found for '{reference_term}'")
        return pd.DataFrame()
    
    df = pd.DataFrame(rows)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    
    logger.info(
        f"Found {len(df)} total results for '{reference_term}' "
        f"across {df['db'].nunique()} databases"
    )
    
    return df
