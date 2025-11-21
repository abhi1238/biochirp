# Core dependencies
from typing import List, Optional, Union, Any, Dict, Literal, Tuple
from dataclasses import dataclass
import sys
import time
import logging
import pickle
import numpy as np
import pandas as pd
import os
# Guardrail framework
from config.guardrail import (
    ParsedValue
)
from fastapi.concurrency import run_in_threadpool

import requests
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input

# from semantic_matching import find_semantic_matches

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

# External libraries (conditional imports if needed)
try:
    from kneed import KneeLocator
except ImportError:
    KneeLocator = None
    logging.warning("kneed not installed. KneeLocator functionality disabled.")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")


USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))  # slightly under route budget
MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))
USE_KNEE_CUT_OFF    = bool(os.getenv("USE_KNEE_CUT_OFF", "True"))
KNEE_CUT_OFF    = float(os.getenv("KNEE_CUT_OFF", "90"))



with open(DB_VALUE_PATH, 'rb') as f:
    db_value = pickle.load(f)
# -----------------------
# Embedding/semantic filter
# -----------------------

model_names = BIOMEDICAL_MODELS

model_cache = {name: SentenceTransformer(name) for name in model_names}


@dataclass
class QdrantConfig:
    url: str = "http://localhost:6333"
    distance: qm.Distance = qm.Distance.COSINE
    create_payload_indexes: bool = True
    index_text_field: bool = False  # ? DISABLED for maximum speed
    
    # HNSW optimization parameters
    hnsw_m: int = 16                # edges per node (16 is balanced)
    hnsw_ef_construct: int = 100    # construction quality
    hnsw_ef: int = 64               # search quality (runtime tunable)

def get_client(cfg: QdrantConfig) -> QdrantClient:
    """Prefer gRPC for heavy ingest; fall back to HTTP if 6334 is closed."""
    try:
        return QdrantClient(
            host="bioc_qdrant", port=6333, grpc_port=6334,
            prefer_grpc=True, timeout=300.0
        )
    except Exception:
        logger.warning("gRPC unavailable, falling back to HTTP")
        return QdrantClient(host="bioc_qdrant", port=6333, prefer_grpc=False, timeout=300.0)

# Setup
cfg = QdrantConfig(index_text_field=False)  # Fast version
client = get_client(cfg)




async def compute_similarity_filtered_outputs(parsed: dict, db: str) -> dict:

    logger.info(f"[similarity filter code] input: {parsed}")
    
    similarity_filtered_by_db = dict()

    similarity_filtered_by_db = ParsedValue().model_dump()

    logger.info(f"[similarity filter] initial output: {similarity_filtered_by_db}")

    for field_name, user_terms in parsed.items():

        if isinstance(user_terms, str):
            similarity_filtered_by_db[field_name] = user_terms
            continue

        if not isinstance(user_terms, list):
            continue

        db_choices = (db_value.get(db, {}) or {}).get(field_name) or []

        # if not db_choices:
        #     logger.warning("[QDRANT] Missing choices for %s.%s", db, field_name)
        #     similarity_filtered_by_db[field_name] = []
        #     continue

        aggregated_matches: List[str] = []

        for term in user_terms:
            try:
                logger.info("[QDRANT] Searching '%s' in %s.%s", term, db, field_name)
                # qdrant returns a list of strings; tolerate empty
                matched_texts = list(set(search_reference_term_all_models_FAST(
                client=client,
                reference_term=term,
                target_field=field_name,
                model_cache=model_cache,
                limit_per_model=200,
                use_knee_cutoff=True,
                db_whitelist=[db],
                hnsw_ef=512,  # ? Tune: 32=fastest, 64=balanced, 128=accurate
            )["text"]))
                aggregated_matches.extend(matched_texts)
            except Exception:
                logger.exception("[QDRANT] Error searching for '%s' in %s.%s", term, db, field_name)


        logger.info(f"[QDRANT] The output of qdrant without cutoff: {aggregated_matches}")

        matched_lower = {str(r).lower() for r in aggregated_matches}
        final_filtered = [val for val in db_choices if str(val).lower() in matched_lower]




        llm_filter_url = f"http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

        input_filtered= Llm_Member_Selector_Input(category = field_name, single_term=term, string_list=final_filtered).model_dump()

        matches = requests.post(llm_filter_url, json=input_filtered).json()["value"]
        # matches = await find_semantic_matches(
        #     category=field_name,
        #     single_term=term,
        #     string_list=final_filtered,
        #     # agent=semantic_match_agent
        # )

        logger.info(f"[QDRANT+LLM filter] The output of LLM: {matches}")

        # find_semantic_matches
        similarity_filtered_by_db[field_name] = matches
        logger.info("[QDRANT+LLM filter] %s.%s: %d match(es)", db, field_name, len(matches))

    return similarity_filtered_by_db


# async def compute_similarity_filtered_outputs(parsed: dict, db: str) -> dict:
#     logger.info(f"[similarity filter code] input: {parsed}")
#     output: Dict[str, List[str]] = {}

#     for field_name, user_terms in parsed.items():
#         if isinstance(user_terms, str):
#             output[field_name] = [user_terms]
#             continue
#         if not isinstance(user_terms, list):
#             output[field_name] = []
#             continue

#         db_choices = (db_value.get(db, {}) or {}).get(field_name) or []
#         aggregated: List[str] = []

#         for term in user_terms:
#             t0 = time.perf_counter()
#             try:
#                 logger.info(f"[QDRANT] Searching '{term}' in {db}.{field_name}")
#                 # embed & search
#                 matches_df = await run_in_threadpool(
#                     search_reference_term_all_models_FAST,
#                     client, term, field_name, model_cache,
#                     limit_per_model=200, use_knee_cutoff=True,
#                     db_whitelist=[db], hnsw_ef=512
#                 )
#                 logger.info(f"[QDRANT] term='{term}' encode+search time={(time.perf_counter()-t0):.2f}s hits={len(matches_df)}")
#                 texts = matches_df["text"].tolist() if not matches_df.empty else []
#                 aggregated.extend(texts)
#             except Exception:
#                 logger.exception(f"[QDRANT] Error searching for '{term}' in {db}.{field_name}")
#                 aggregated.extend([])

#         logger.info(f"[QDRANT] The output of qdrant without cutoff: {aggregated}")

#         matched_lower = {str(r).lower() for r in aggregated}
#         final_filtered = [val for val in db_choices if str(val).lower() in matched_lower]

#         llm_filter_url = f"http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

#         # call LLM filter
#         llm_input = Llm_Member_Selector_Input(category=field_name, single_term=term, string_list=final_filtered).model_dump()
#         t1 = time.perf_counter()
#         # using httpx or threadpool
#         matches = await run_in_threadpool(
#             requests.post, llm_filter_url, json=llm_input
#         )
#         matches = matches.json().get("value", [])
#         logger.info(f"[QDRANT+LLM filter] term='{term}' field='{field_name}' output={matches} elapsed={(time.perf_counter()-t1):.2f}s")

#         output[field_name] = matches
#         logger.info(f"[QDRANT+LLM filter] {db}.{field_name}: {len(matches)} match(es)")

#     return output
