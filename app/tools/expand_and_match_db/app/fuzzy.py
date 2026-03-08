import os
import sys
import logging
from typing import List, Sequence, Union

import numpy as np
from rapidfuzz import process, fuzz

# Configure logging (only if running as main module)
if __name__ != "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout
    )

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
FUZZY_SEARCH_CUT_OFF = float(os.getenv("FUZZY_SEARCH_CUT_OFF", "90"))


def _clean_strings(name: str, seq: Sequence[str]) -> List[str]:
    """
    Filter out non-string elements from a sequence.
    
    Args:
        name: Name of the sequence for logging
        seq: Sequence to clean
        
    Returns:
        List containing only string elements
        
    Raises:
        TypeError: If seq is not a list or tuple
    """
    if not isinstance(seq, (list, tuple)):
        raise TypeError(
            f"'{name}' must be a list/tuple of strings, got {type(seq).__name__}"
        )
    
    cleaned = []
    dropped = 0
    
    for i, s in enumerate(seq):
        if isinstance(s, str) and s.strip():  # Also check for non-empty
            cleaned.append(s)
        else:
            dropped += 1
            logger.debug(
                "Dropping invalid item in %s at index %d: %r (type: %s)",
                name, i, s, type(s).__name__
            )
    
    if dropped:
        logger.warning("Dropped %d invalid items from %s", dropped, name)
    
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
    Select choices where ANY scorer for ANY query achieves >= min_score.
    
    Uses multiple fuzzy matching algorithms:
      1) QRatio - Overall similarity
      2) partial_ratio - Substring matching (optional, controlled by partial_min_len)
      3) token_sort_ratio - Sorted token matching
      4) token_set_ratio - Set-based token matching
    
    Args:
        queries: Single query string or list of query strings
        choices: Sequence of choice strings to match against
        min_score: Minimum score threshold (0-100)
        partial_min_len: Minimum query length to use partial_ratio (0 to disable)
        case_insensitive: Whether to ignore case differences
        
    Returns:
        List of unique matching choices (preserves original case)
        
    Raises:
        TypeError: If queries or choices have invalid types
        ValueError: If min_score is out of valid range
    """
    tool = "fuzzy"
    
    logger.info(
        f"[{tool}] Starting fuzzy matching with min_score={min_score}, "
        f"case_insensitive={case_insensitive}"
    )
    
    try:
        # Input validation
        if not (0 <= min_score <= 100):
            raise ValueError(f"min_score must be between 0 and 100, got {min_score}")
        
        if partial_min_len < 0:
            raise ValueError(f"partial_min_len must be >= 0, got {partial_min_len}")
        
        # Normalize queries
        if isinstance(queries, str):
            if not queries.strip():
                logger.warning(f"[{tool}] Empty query string provided")
                return []
            queries_list = [queries]
        elif isinstance(queries, list):
            if not queries:
                logger.warning(f"[{tool}] Empty queries list provided")
                return []
            if not all(isinstance(q, str) for q in queries):
                raise TypeError("All items in 'queries' list must be strings")
            # Filter out empty strings
            queries_list = [q for q in queries if q.strip()]
            if not queries_list:
                logger.warning(f"[{tool}] All queries were empty strings")
                return []
        else:
            raise TypeError(
                f"'queries' must be str or List[str], got {type(queries).__name__}"
            )
        
        # Clean and validate choices
        cleaned_choices = _clean_strings("choices", choices)
        if not cleaned_choices:
            logger.warning(f"[{tool}] No valid choices after cleaning")
            return []
        
        logger.info(
            f"[{tool}] Processing {len(queries_list)} queries against "
            f"{len(cleaned_choices)} choices"
        )
        
        # Case normalization
        if case_insensitive:
            q_proc = [q.casefold() for q in queries_list]
            c_proc = [c.casefold() for c in cleaned_choices]
            processor = None  # Already normalized
        else:
            q_proc = queries_list
            c_proc = cleaned_choices
            processor = None
        
        # Define scorers
        # FIX: Uncomment partial_ratio or remove the dead masking code
        scorers = [
            ("QRatio", fuzz.QRatio),
            ("partial_ratio", fuzz.partial_ratio),  # Uncommented!
            ("token_sort_ratio", fuzz.token_sort_ratio),
            ("token_set_ratio", fuzz.token_set_ratio),
        ]
        
        score_mats = []
        
        for name, scorer in scorers:
            logger.debug(
                f"[{tool}] Computing {name} scores: {len(q_proc)} x {len(c_proc)}"
            )
            
            mat = process.cdist(q_proc, c_proc, scorer=scorer, processor=processor)
            
            # FIX: Now this code will actually execute
            if name == "partial_ratio" and partial_min_len > 0:
                short_mask = np.array(
                    [len(q) < partial_min_len for q in q_proc],
                    dtype=bool
                )
                if short_mask.any():
                    # Disable partial_ratio for short queries
                    mat[short_mask, :] = -np.inf
                    logger.debug(
                        f"[{tool}] Disabled partial_ratio for "
                        f"{short_mask.sum()} short queries"
                    )
            
            score_mats.append(mat)
        
        # Stack and compute max across scorers
        stacked = np.stack(score_mats, axis=0)       # S x Q x C
        max_over_scorers = stacked.max(axis=0)       # Q x C
        
        # Keep choices where ANY query achieves min_score with ANY scorer
        keep_choice_mask = (max_over_scorers >= min_score).any(axis=0)
        
        selected = [
            c for c, keep in zip(cleaned_choices, keep_choice_mask) if keep
        ]
        
        logger.info(f"[{tool}] Found {len(selected)} matches out of {len(cleaned_choices)} choices")
        return selected
    
    # FIX: More specific exception handling
    except (TypeError, ValueError) as e:
        logger.error(f"[{tool}] Input validation error: {e}")
        raise  # Re-raise validation errors
    except Exception as e:
        logger.exception(f"[{tool}] Unexpected error during fuzzy matching: {e}")
        return []  # Return empty list for unexpected errors

