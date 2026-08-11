


from typing import Set, List, Optional, Dict, Any
import os
import re
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


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _literal_floor(entity_name, universe: List[str]) -> Set[str]:
    """Universe members that match the literal term exactly or by word-boundary
    containment (either direction). These are a STABLE FLOOR — they can never be
    dropped below the fuzzy/semantic cutoff, so the canonical substring match is
    always retained. Nothing entity-specific is hardcoded."""
    terms = entity_name if isinstance(entity_name, list) else [entity_name]
    terms = [_norm(t) for t in terms if isinstance(t, str) and t.strip()]
    floor: Set[str] = set()
    for u in universe:
        nu = _norm(u)
        if not nu:
            continue
        for t in terms:
            if not t:
                continue
            # exact, or the term occurs as a whole word inside the member name
            # (term ⊆ member only — the reverse would inject a short fragment like
            # 'lung' when the term is 'lung adenocarcinoma').
            if nu == t or re.search(rf"\b{re.escape(t)}\b", nu):
                floor.add(u.lower())
                break
    return floor


async def member_selection(
    entity_type: str,
    entity_name: str,
    tool: str,
    data: pd.DataFrame
) -> List[str]:
    """Combine fuzzy + semantic matching for entity selection."""
    if not entity_name:
        return []
    if entity_type not in data.columns:
        logger.warning(f"[{tool}] column {entity_type!r} not in data; no members.")
        return []

    logger.info(f"[{tool}] [Fuzzy+Semantic] [{entity_type} Input]: {entity_name}")

    universe = list({str(v) for v in data[entity_type].dropna().tolist()})

    fuzzy = fuzzy_filter_choices_multi_scorer(
        queries=entity_name,
        choices=universe
    )
    logger.info(f"[{tool}] Fuzzy {entity_type}: {fuzzy}")

    semantic = await return_semantic_similar_member(
        category=entity_type,
        q_term=entity_name,
        universe_texts=universe
    )
    logger.info(f"[{tool}] Semantic {entity_type}: {semantic}")

    # Literal-term stable floor: the canonical exact/substring match is always
    # retained, even if fuzzy + semantic both miss it under their cutoffs.
    floor = _literal_floor(entity_name, universe)
    if floor:
        logger.info(f"[{tool}] Literal floor {entity_type}: {sorted(floor)}")

    final_set = list({s.lower() for s in set(fuzzy) | set(semantic)} | floor)
    logger.info(
        f"[{tool}] Combined final semantic member of {entity_type} "
        f"is {len(final_set)}: {final_set}"
    )

    return final_set