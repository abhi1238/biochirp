


from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
from .fuzzy_search import fuzzy_filter_choices_multi_scorer
from .semantic_similarity import return_semantic_similar_member
import json

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.member_selector")


async def member_selection(
    entity_type: str, 
    entity_name: str, 
    tool: str, 
    data: pd.DataFrame
) -> List[str]:
    """Combine fuzzy + semantic matching for entity selection."""
    if not entity_name:
        return []

    logger.info(f"[{tool}] [Fuzzy+Semantic] [{entity_type} Input]: {entity_name}")
    
    fuzzy = fuzzy_filter_choices_multi_scorer(
        queries=entity_name,
        choices=list(set(data[entity_type]))
    )
    logger.info(f"[{tool}] Fuzzy {entity_type}: {fuzzy}")
    
    semantic = await return_semantic_similar_member(
        category=entity_type,
        q_term=entity_name,
        universe_texts=list(set(data[entity_type]))
    )
    logger.info(f"[{tool}] Semantic {entity_type}: {semantic}")
    
    final_set = list({s.lower() for s in set(fuzzy) | set(semantic)})
    logger.info(
        f"[{tool}] Combined final semantic member of {entity_type} "
        f"is {len(final_set)}: {final_set}"
    )
    
    return final_set