

import numpy as np
from typing import Dict, List
from .embed import embed_list_multi_model
from .http_client import post_json_with_retries
import logging
import os

try:
    from kneed import KneeLocator
except ImportError:
    KneeLocator = None

base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.semantic_similarity")

LLM_URL = os.getenv(
    "LLM_FILTER_URL",
    "https://biochirp.iiitd.edu.in/services/llm_filter/api/llm_member_selection_filter",
)

def _dynamic_cutoff(scores: np.ndarray) -> float:
    if scores.size < 2:
        return float(scores.min())

    sorted_scores = np.sort(scores)[::-1]

    if KneeLocator is None:
        idx = max(1, int(0.2 * len(sorted_scores)))
        return float(sorted_scores[idx - 1])

    try:
        knee = KneeLocator(
            range(len(sorted_scores)),
            sorted_scores,
            curve="convex",
            direction="decreasing",
            S=0.5,
        )
        if knee.knee is not None:
            return float(sorted_scores[knee.knee])
    except Exception as e:
        logger.warning(f"[semantic cutoff] Knee detection failed: {e!r}")

    return float(np.median(sorted_scores))


def select_by_similarity_dynamic(
    universe_texts: List[str],
    universe_embeddings: Dict[str, List[List[float]]],
    query_embeddings: Dict[str, List[List[float]]],
) -> Dict[str, List[str]]:

    result: Dict[str, List[str]] = {}

    for model, u_emb in universe_embeddings.items():
        U = np.asarray(u_emb, dtype=np.float32)  # (N, D)
        model_hits: set[str] = set()

        Q = np.asarray(query_embeddings[model], dtype=np.float32)  # (M, D)

        for qi, q in enumerate(Q):
            scores = U @ q  # (N,)
            cutoff = _dynamic_cutoff(scores)

            hits = [
                universe_texts[i]
                for i, s in enumerate(scores)
                if s >= cutoff
            ]

            model_hits.update(hits)

            logger.info(
                f"[semantic similarity] model={model} "
                f"query_idx={qi} cutoff={cutoff:.4f} hits={len(hits)}"
            )


        result[model] = sorted(model_hits)

    return result


def union_across_models(modelwise_result: Dict[str, List[str]]) -> List[str]:
    return sorted(
        set(text for texts in modelwise_result.values() for text in texts)
    )


async def return_semantic_similar_member(category,
    q_term: List[str],
    universe_texts: List[str],
) -> List[str]:
    
    if not q_term:
        return []

    # normalize to list[str]
    if isinstance(q_term, str):
        q_term = [q_term]

    if not isinstance(q_term, list):
        logger.warning(
            "[semantic similarity] invalid q_term type: %r", type(q_term)
        )
        return []

    q_term = [t.strip() for t in q_term if isinstance(t, str) and t.strip()]

    if not q_term:
        return []


    universe_embeddings = await embed_list_multi_model(
        texts=universe_texts,
        models=["scincl", "pubmed_marco", "wikimed"],
    )

    query_embeddings = await embed_list_multi_model(
        texts=q_term,
        models=["scincl", "pubmed_marco", "wikimed"],
    )

    modelwise_hits = select_by_similarity_dynamic(
        universe_texts=universe_texts,
        universe_embeddings=universe_embeddings,
        query_embeddings=query_embeddings,
    )

    any_model_hits = union_across_models(modelwise_hits)

    logger.info(
        f"[Semantic similarity] Total unique semantic matches: {len(any_model_hits)}"
    )

    final_result = list()

    for i in q_term:

        payload = {
            "category": category,
            "single_term": i,
            "string_list": universe_texts
        }
        try:
            resp = await post_json_with_retries(LLM_URL, payload, max_retries=2)
            result = resp.json().get("value", [])
        except Exception as e:
            logger.warning("[semantic similarity] LLM filter failed for %s: %s", i, e)
            result = []

        final_result.extend(result)

    logger.info(
        f"[semantic similarity with LLM]] hits={final_result}")


    return final_result
