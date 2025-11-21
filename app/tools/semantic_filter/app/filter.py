# Standard library imports
import sys
import time
import uuid
import logging
import pickle
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union, Tuple, Literal
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input
# Third-party imports
import numpy as np
import pandas as pd

# ML/AI and data processing
try:
    from kneed import KneeLocator
except ImportError:
    KneeLocator = None
    logging.warning("kneed not installed - KneeLocator functionality disabled")


# Vector database
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")


def distinct_values(
    client: QdrantClient,
    collection_name: str,
    field_name: str,
    limit: int = 1000,
) -> List[str]:
    """
    Get distinct values for a payload field using Qdrant's Facet API.
    
    Args:
        client: QdrantClient instance
        collection_name: Name of the collection
        field_name: Payload field to get distinct values from
        limit: Maximum number of unique values to return
    
    Returns:
        List of distinct values for the field
    """
    try:
        # Use Qdrant Facet API (available in Qdrant 1.12+)
        facet_result = client.facet(
            collection_name=collection_name,
            key=field_name,
            limit=limit,
        )
        
        # Extract unique values from facet hits
        return [hit.value for hit in facet_result.hits]
    
    except Exception as e:
        logger.warning(f"Facet API failed for field '{field_name}': {e!r}, falling back to scroll method")
        
        # Fallback: scroll through points and collect unique values
        unique_values = set()
        offset = None
        
        while True:
            records, offset = client.scroll(
                collection_name=collection_name,
                limit=100,
                offset=offset,
                with_payload=[field_name],
                with_vectors=False,
            )
            
            if not records:
                break
                
            for record in records:
                if record.payload and field_name in record.payload:
                    value = record.payload[field_name]
                    if isinstance(value, list):
                        unique_values.update(value)
                    else:
                        unique_values.add(value)
            
            if offset is None:
                break
        
        return list(unique_values)

def model_to_collection(model_name: str) -> str:
    return f"emb_{model_name.replace('/', '_')}"



def search_reference_term_all_models_FAST(
    client: QdrantClient,
    reference_term: str,
    target_field: str,
    model_cache: Dict[str, "SentenceTransformer"],
    limit_per_model: int = 50,
    use_knee_cutoff: bool = True,
    db_whitelist: Optional[List[str]] = None,
    hnsw_ef: int = 64,
) -> pd.DataFrame:
    """
    Fast version with optimized HNSW search parameters.
    """
    rows: List[Dict[str, Any]] = []

    for model_name, model in model_cache.items():
        coll = model_to_collection(model_name)
        if not client.collection_exists(coll):
            logger.warning(f"[{coll}] collection missing; skipping model='{model_name}'")
            continue

        q_vec = model.encode(
            reference_term,
            convert_to_tensor=True,
            normalize_embeddings=True
        )

        #  Use facet API to get distinct db values if no whitelist provided
        if db_whitelist is not None:
            dbs = db_whitelist
        else:
            dbs = distinct_values(client, coll, "db")
            logger.info(f"[{coll}] Found {len(dbs)} databases: {dbs}")

        for db_name in dbs:
            flt = qm.Filter(must=[
                qm.FieldCondition(key="model", match=qm.MatchValue(value=model_name)),
                qm.FieldCondition(key="db", match=qm.MatchValue(value=db_name)),
                qm.FieldCondition(key="field", match=qm.MatchValue(value=target_field)),
            ])

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
            )
            
            if not hits:
                continue

            scores = np.array([h.score for h in hits], dtype=np.float32)
            if use_knee_cutoff and len(scores) > 1:
                sorted_scores = np.sort(scores)[::-1]
                try:
                    
                    knee = KneeLocator(range(len(sorted_scores)), sorted_scores,
                                       curve="convex", direction="decreasing", S=0.5)
                    threshold = float(sorted_scores[knee.knee])
                except Exception:
                    threshold = 0.0
            else:
                threshold = 0.0

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
                    for k, v in pl.items():
                        if k not in row:
                            row[k] = v
                    rows.append(row)

    llm_filter_url = f"http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

    

    # candidate_strings = [r["text"] for r in rows if "text" in r]

    # input_filtered = Llm_Member_Selector_Input(
    #     category = target_field,
    #     single_term = reference_term,
    #     string_list = candidate_strings
    # )

    # input_filtered= Llm_Member_Selector_Input(category = target_field, single_term=reference_term, string_list=set(rows))

    df = pd.DataFrame(rows)
    return df.sort_values("score", ascending=False).reset_index(drop=True) if not df.empty else df