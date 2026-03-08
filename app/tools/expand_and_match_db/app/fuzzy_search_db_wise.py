import os
import sys
import logging
import asyncio
from typing import List, Union, Any, Dict, Optional
from pathlib import Path
import httpx

from config.guardrail import (
    ParsedValue,
    OutputFields,
    FuzzyFilteredOutputs,
    Llm_Member_Selector_Input
)
from .fuzzy import fuzzy_filter_choices_multi_scorer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
FUZZY_SCORE_CUT_SCORE = float(os.getenv("FUZZY_SCORE_CUT_SCORE", "90"))
def _build_llm_filter_url() -> str:
    url = os.getenv("LLM_FILTER_URL")
    if url:
        return url
    host = os.getenv("LLM_FILTER_HOST", "biochirp_llm_filter_tool")
    port = os.getenv("LLM_FILTER_PORT", "8017")
    return f"http://{host}:{port}/llm_member_selection_filter"


LLM_FILTER_URL = _build_llm_filter_url()
LLM_FILTER_TIMEOUT = float(os.getenv("LLM_FILTER_TIMEOUT", "30"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "5"))

# Database values path
DB_VALUE_PATH = Path(__file__).parent.parent / "resources" / "values" / "concept_values_by_db_and_field.pkl"
_db_value_cache: Optional[Dict] = None


def load_db_values() -> Dict:
    """
    Lazy load database values with error handling.
    
    Returns:
        Dict containing database values
        
    Raises:
        FileNotFoundError: If pickle file doesn't exist
        Exception: If pickle loading fails
    """
    global _db_value_cache
    
    if _db_value_cache is not None:
        return _db_value_cache
    
    try:
        if not DB_VALUE_PATH.exists():
            raise FileNotFoundError(f"Database values file not found: {DB_VALUE_PATH}")
        
        import pickle
        with open(DB_VALUE_PATH, 'rb') as f:
            _db_value_cache = pickle.load(f)
        
        # Log available databases for debugging
        available_dbs = list(_db_value_cache.keys())
        logger.info(
            f"Loaded database values from {DB_VALUE_PATH}. "
            f"Available databases: {available_dbs}"
        )
        return _db_value_cache
        
    except Exception as e:
        logger.exception(f"Failed to load database values: {e}")
        raise


async def call_llm_filter_service(
    field_name: str,
    single_term: str,
    matches: List[str]
) -> List[str]:
    """
    Call LLM filter service for a single term with error handling.
    
    Args:
        field_name: Category/field name
        single_term: Single term to filter
        matches: List of candidate matches
        
    Returns:
        List of filtered matches, empty list on error
    """
    try:
        input_data = Llm_Member_Selector_Input(
            category=field_name,
            single_term=single_term,
            string_list=matches
        ).model_dump()
        
        # FIX: Use async HTTP client instead of blocking requests
        async with httpx.AsyncClient() as client:
            response = await client.post(
                LLM_FILTER_URL,
                json=input_data,
                timeout=LLM_FILTER_TIMEOUT
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Validate response structure
            if not isinstance(result, dict) or "value" not in result:
                logger.warning(
                    f"Invalid LLM filter response for term '{single_term}': {result}"
                )
                return []
            
            filtered = result["value"]
            
            if not isinstance(filtered, list):
                logger.warning(
                    f"LLM filter returned non-list for term '{single_term}': {type(filtered)}"
                )
                return []
            
            logger.debug(
                f"LLM filter found {len(filtered)} matches for '{single_term}' in {field_name}"
            )
            return filtered
            
    except httpx.TimeoutException:
        logger.error(f"Timeout calling LLM filter for term '{single_term}'")
        return []
    except httpx.HTTPError as e:
        logger.error(f"HTTP error calling LLM filter for term '{single_term}': {e}")
        return []
    except Exception as e:
        logger.exception(f"Error calling LLM filter for term '{single_term}': {e}")
        return []


async def compute_fuzzy_filtered_outputs(
    parsed: Union[ParsedValue, Dict[str, Any]],
    database: str
) -> FuzzyFilteredOutputs:
    """
    Perform fuzzy matching and LLM filtering for database fields.
    
    Args:
        parsed: Parsed query values (ParsedValue or dict)
        database: Target database name (uppercase for API, normalized to lowercase for lookup)
        
    Returns:
        FuzzyFilteredOutputs with filtered results
    """
    tool = "fuzzy"
    
    logger.info(f"[{tool}] Starting for database: {database}")
    
    # Input validation
    if not parsed:
        logger.warning(f"[{tool}] Empty parsed input")
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    # Load database values
    try:
        db_value = load_db_values()
    except Exception as e:
        logger.error(f"[{tool}] Failed to load database values: {e}")
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    # Convert to dict if needed
    try:
        fields = parsed.model_dump(exclude_none=True) if hasattr(parsed, 'model_dump') else parsed
    except Exception as e:
        logger.warning(f"[{tool}] Failed to convert parsed to dict: {e}")
        fields = parsed if isinstance(parsed, dict) else {}
    
    if not fields:
        logger.warning(f"[{tool}] Empty fields after parsing")
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    # FIX: Normalize database name to lowercase for pickle file lookup
    # The pickle file has lowercase keys ('ttd', 'ctd', 'hcdt')
    # but the API passes uppercase ('TTD', 'CTD', 'HCDT')
    db_lookup_key = database.lower()
    
    logger.debug(f"[{tool}] Looking up database with key: '{db_lookup_key}'")
    
    # Validate database exists in pickle file
    db_fields = db_value.get(db_lookup_key)
    if not db_fields:
        available_dbs = list(db_value.keys())
        logger.warning(
            f"[{tool}] Database '{database}' (lookup key: '{db_lookup_key}') "
            f"not found in pickle file. Available databases: {available_dbs}"
        )
        return FuzzyFilteredOutputs(
            database=database,  # Return original case for API response
            value=OutputFields(),
            tool=tool
        )
    
    if not isinstance(db_fields, dict):
        logger.error(
            f"[{tool}] Database '{database}' fields is not a dict: {type(db_fields)}"
        )
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    logger.info(
        f"[{tool}] Found database '{database}' with {len(db_fields)} fields. "
        f"Processing {len(fields)} parsed fields."
    )
    
    field_matches = {}
    
    for field_name, user_terms in fields.items():
        # Skip if string (already matched)
        if isinstance(user_terms, str):
            field_matches[field_name] = user_terms
            logger.debug(f"[{tool}] Field '{field_name}' is already a string, skipping")
            continue
        
        # Skip if not a list
        if not isinstance(user_terms, list):
            logger.warning(
                f"[{tool}] Skipping non-list field '{field_name}': {type(user_terms)}"
            )
            continue
        
        # Skip empty lists
        if not user_terms:
            logger.info(f"[{tool}] Field '{field_name}' has empty user_terms")
            field_matches[field_name] = []
            continue
        
        # Skip if no choices available in database
        db_choices = db_fields.get(field_name)
        if not db_choices:
            logger.info(
                f"[{tool}] No database choices available for field '{field_name}'"
            )
            field_matches[field_name] = []
            continue
        
        # Fuzzy search
        logger.info(
            f"[{tool}] Fuzzy matching {len(user_terms)} terms against "
            f"{len(db_choices)} choices for field '{field_name}'"
        )
        
        try:
            fuzzy_matches = fuzzy_filter_choices_multi_scorer(
                queries=user_terms,
                choices=db_choices,
                min_score=FUZZY_SCORE_CUT_SCORE
            )
        except Exception as e:
            logger.exception(
                f"[{tool}] Fuzzy matching failed for field '{field_name}': {e}"
            )
            field_matches[field_name] = []
            continue
        
        logger.info(
            f"[{tool}] Fuzzy search found {len(fuzzy_matches)} matches for '{field_name}'"
        )
        
        if not fuzzy_matches:
            logger.info(f"[{tool}] No fuzzy matches found for field '{field_name}'")
            field_matches[field_name] = []
            continue
        
        # Parallel LLM filtering with semaphore for rate limiting
        logger.info(
            f"[{tool}] LLM filtering {len(user_terms)} terms for '{field_name}' "
            f"against {len(fuzzy_matches)} fuzzy matches"
        )
        
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        async def filtered_call(term):
            async with semaphore:
                return await call_llm_filter_service(field_name, term, fuzzy_matches)
        
        try:
            # Run all LLM filter calls in parallel
            llm_filter_results = await asyncio.gather(
                *[filtered_call(term) for term in user_terms],
                return_exceptions=True
            )
        except Exception as e:
            logger.exception(
                f"[{tool}] LLM filtering failed for field '{field_name}': {e}"
            )
            field_matches[field_name] = []
            continue
        
        # Combine results
        llm_filter_matches = []
        error_count = 0
        
        for i, result in enumerate(llm_filter_results):
            if isinstance(result, Exception):
                error_count += 1
                logger.error(
                    f"[{tool}] LLM filter error for term '{user_terms[i]}': {result}"
                )
                continue
            if isinstance(result, list):
                llm_filter_matches.extend(result)
            else:
                logger.warning(
                    f"[{tool}] Unexpected LLM filter result type for term '{user_terms[i]}': "
                    f"{type(result)}"
                )
        
        if error_count > 0:
            logger.warning(
                f"[{tool}] {error_count}/{len(user_terms)} LLM filter calls failed "
                f"for field '{field_name}'"
            )
        
        # Remove duplicates while preserving order
        seen = set()
        unique_matches = []
        for match in llm_filter_matches:
            if match and match not in seen:
                seen.add(match)
                unique_matches.append(match)
        
        logger.info(
            f"[{tool}] LLM filtering found {len(unique_matches)} unique matches "
            f"for field '{field_name}'"
        )
        
        field_matches[field_name] = unique_matches
    
    # Build result
    try:
        result = FuzzyFilteredOutputs(
            database=database,  # Return original uppercase for consistency
            value=OutputFields(**field_matches) if field_matches else OutputFields(),
            tool=tool
        )
    except Exception as e:
        logger.exception(f"[{tool}] Failed to create FuzzyFilteredOutputs: {e}")
        result = FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    total_matches = sum(
        len(matches) if isinstance(matches, list) else 0
        for matches in field_matches.values()
    )
    logger.info(
        f"[{tool}] Finished for database '{database}'. "
        f"Processed {len(field_matches)} fields with {total_matches} total matches."
    )
    
    return result
