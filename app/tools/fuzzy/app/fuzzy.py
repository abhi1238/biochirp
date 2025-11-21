from typing import Union
import numpy as np
from rapidfuzz import process, fuzz
from typing import List, Sequence, Union
import logging
import os
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input
import requests


FUZZY_SEARCH_CUT_OFF = float(os.getenv("FUZZY_SEARCH_CUT_OFF", "90"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

def _clean_strings(name: str, seq: Sequence[str]) -> List[str]:
    if not isinstance(seq, (list, tuple)):
        raise TypeError(f"'{name}' must be a list/tuple of strings, got {type(seq).__name__}")
    cleaned = []
    dropped = 0
    for i, s in enumerate(seq):
        if isinstance(s, str):
            cleaned.append(s)
        else:
            dropped += 1
            logger.debug("Dropping non-string in %s at index %d: %r", name, i, s)
    if dropped:
        logger.warning("Dropped %d non-string items from %s.", dropped, name)
    return cleaned


def fuzzy_filter_choices_multi_scorer(
    queries: Union[str, List[str]],
    choices: Sequence[str],
    min_score: float = FUZZY_SEARCH_CUT_OFF,
    *,
    partial_min_len: int = 6,
    case_insensitive: bool = True,
) -> List[str]:
    """
    Select choices if ANY of these scorers for ANY query >= min_score:
      1) QRatio
      2) partial_ratio   (disabled per-query if len(query) < partial_min_len)
      3) token_sort_ratio
      4) token_set_ratio

    Returns a unique flat list of matching choices.
    """

    tool = "fuzzy"

    logger.info(f"[{tool} code] Running")

    try:
        # normalize queries
        if isinstance(queries, str):
            queries_list = [queries]
        elif isinstance(queries, list) and all(isinstance(q, str) for q in queries):
            queries_list = queries
        else:
            raise TypeError("'queries' must be str or List[str]")

        cleaned_choices = _clean_strings("choices", choices)
        if not cleaned_choices or not queries_list:
            return []

        # case normalization (preserve originals for return)
        if case_insensitive:
            q_proc = [q.casefold() for q in queries_list]
            c_proc = [c.casefold() for c in cleaned_choices]
            processor = None  # we already normalized strings
        else:
            q_proc = queries_list
            c_proc = cleaned_choices
            processor = None  # keep default None to avoid double-processing

        scorers = [
            ("QRatio", fuzz.QRatio),
            # ("partial_ratio", fuzz.partial_ratio),
            ("token_sort_ratio", fuzz.token_sort_ratio),
            ("token_set_ratio", fuzz.token_set_ratio),
        ]

        score_mats = []
        for name, scorer in scorers:
            logger.debug("Computing cdist: %s on %d x %d", name, len(q_proc), len(c_proc))
            mat = process.cdist(q_proc, c_proc, scorer=scorer, processor=processor)

            # PER-QUERY masking for partial_ratio when query too short
            if name == "partial_ratio" and partial_min_len > 0:
                short_mask = np.array([len(q) < partial_min_len for q in q_proc], dtype=bool)
                if short_mask.any():
                    # Set to -inf so it cannot win vs min_score threshold
                    mat[short_mask, :] = -np.inf
            score_mats.append(mat)

        stacked = np.stack(score_mats, axis=0)       # S x Q x C
        max_over_scorers = stacked.max(axis=0)       # Q x C
        keep_choice_mask = (max_over_scorers >= min_score).any(axis=0)

        selected = [c for c, keep in zip(cleaned_choices, keep_choice_mask) if keep]

        logger.info("Selected %d choices with min_score >= %.2f.", len(selected), min_score)

        
        
        
        logger.info(f"[Fuzzy with cut off] : {selected}")
        logger.info(f"[Fuzzy with cut off] Number of selected member : {len(selected)}")



        
        # llm_filter_url = "http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

        # llm_filter_url = f"http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

        # input_filtered_llm= Llm_Member_Selector_Input(category = field_name, single_term=term, string_list=final_filtered).model_dump()

        # matches = requests.post(llm_filter_url, json=input_filtered_llm).json()["value"]
        
        
        # payload = {
        # "category": "disease_name",
        # "single_term": "cancer",
        # "string_list": [
        #     "fever",
        #     "melanoma"
        # ]
        # }

        # llm_filter_url = "http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"

        # input_filtered= Llm_Member_Selector_Input(category = field_name, single_term=term, string_list=selected).model_dump()

        logger.info(f"[{tool} code] Output:  {selected}")
        logger.info(f"[{tool} code] Finished")

        return selected

    except Exception as e:
        logger.exception("fuzzy_filter_choices_multi_scorer failed: %s", e)
        logger.info(f"[{tool} code] Finished in exception block")
        return []
