import os
import sys
import logging
import pickle
from typing import List, Optional, Union, Any, Dict
from pathlib import Path

from config.guardrail import ParsedValue, OutputFields, FuzzyFilteredOutputs
from .fuzzy import fuzzy_filter_choices_multi_scorer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# Configuration
DB_VALUE_PATH = os.getenv(
    "DB_VALUE_PATH",
    "resources/values/concept_values_by_db_and_field.pkl"
)
FUZZY_MIN_SCORE = float(os.getenv("FUZZY_MIN_SCORE", "90"))

# Lazy load database values
_db_value_cache: Optional[Dict] = None


def load_db_values() -> Dict:
    """
    Lazy load database values with error handling.
    
    Returns:
        Dict with database values
        
    Raises:
        FileNotFoundError: If pickle file doesn't exist
    """
    global _db_value_cache
    
    if _db_value_cache is not None:
        return _db_value_cache
    
    try:
        db_path = Path(DB_VALUE_PATH)
        
        if not db_path.exists():
            raise FileNotFoundError(
                f"Database values file not found: {db_path}"
            )
        
        logger.info(f"Loading database values from {db_path}")
        
        with open(db_path, 'rb') as f:
            _db_value_cache = pickle.load(f)
        
        # Log available databases
        available_dbs = list(_db_value_cache.keys())
        logger.info(
            f"Loaded database values. Available databases: {available_dbs}"
        )
        
        return _db_value_cache
        
    except Exception as e:
        logger.exception(f"Failed to load database values: {e}")
        raise


async def compute_fuzzy_filtered_outputs(
    parsed: Union[ParsedValue, Dict[str, Any]],
    db_name: str
) -> FuzzyFilteredOutputs:
    """
    Perform fuzzy matching of user terms to database choices for each field.
    
    Args:
        parsed: Parsed query values (ParsedValue or dict)
        db_name: Target database name (case insensitive)
        
    Returns:
        FuzzyFilteredOutputs with matched values
    """
    tool = "fuzzy"
    
    logger.info(f"[{tool}] Starting for database: {db_name}")
    
    # Input validation
    if not parsed:
        logger.warning(f"[{tool}] Empty parsed value provided")
        return FuzzyFilteredOutputs(
            database=db_name,
            value=OutputFields(),
            tool=tool
        )
    
    if not db_name or not isinstance(db_name, str):
        logger.error(f"[{tool}] Invalid database name: {db_name}")
        return FuzzyFilteredOutputs(
            database=db_name or "unknown",
            value=OutputFields(),
            tool=tool
        )
    
    # Load database values
    try:
        db_value = load_db_values()
    except Exception as e:
        logger.error(f"[{tool}] Failed to load database values: {e}")
        return FuzzyFilteredOutputs(
            database=db_name,
            value=OutputFields(),
            tool=tool
        )
    
    # Convert parsed to dict
    try:
        if hasattr(parsed, 'model_dump'):
            fields = parsed.model_dump(exclude_none=True)
        else:
            fields = parsed if isinstance(parsed, dict) else {}
    except (AttributeError, TypeError) as e:
        logger.error(f"[{tool}] Failed to convert parsed value: {e}")
        return FuzzyFilteredOutputs(
            database=db_name,
            value=OutputFields(),
            tool=tool
        )
    
    if not fields:
        logger.warning(f"[{tool}] No fields to process")
        return FuzzyFilteredOutputs(
            database=db_name,
            value=OutputFields(),
            tool=tool
        )
    
    # FIX: Normalize database name to lowercase for lookup
    db_lookup_key = db_name.lower()
    
    logger.info(
        f"[{tool}] Using database key '{db_lookup_key}' "
        f"(original: '{db_name}') for pickle lookup"
    )
    
    # FIX: Get database fields with lowercase key
    db_fields = db_value.get(db_lookup_key, {})
    
    if not db_fields:
        logger.warning(
            f"[{tool}] Database '{db_lookup_key}' not found in db_value. "
            f"Available: {list(db_value.keys())}"
        )
        return FuzzyFilteredOutputs(
            database=db_name,  # Return original case for consistency
            value=OutputFields(),
            tool=tool
        )
    
    logger.info(
        f"[{tool}] Found database '{db_lookup_key}' with {len(db_fields)} fields. "
        f"Processing {len(fields)} parsed fields."
    )
    
    # Process each field
    field_matches = {}
    
    for field_name, user_terms in fields.items():
        # Handle string fields (like "requested")
        if isinstance(user_terms, str):
            field_matches[field_name] = user_terms
            logger.debug(
                f"[{tool}] Field '{field_name}' is string, keeping as-is: '{user_terms}'"
            )
            continue
        
        # Skip non-list fields
        if not isinstance(user_terms, list):
            logger.warning(
                f"[{tool}] Skipping non-list field '{field_name}': {type(user_terms)}"
            )
            continue
        
        # Skip empty lists
        if not user_terms:
            logger.debug(f"[{tool}] Field '{field_name}' has empty user_terms")
            field_matches[field_name] = []
            continue
        
        # Get database choices for this field
        db_choices = db_fields.get(field_name)
        
        if not db_choices:
            logger.warning(
                f"[{tool}] No database choices for field '{field_name}' "
                f"in database '{db_lookup_key}'"
            )
            field_matches[field_name] = []
            continue
        
        logger.info(
            f"[{tool}] Fuzzy matching {len(user_terms)} terms against "
            f"{len(db_choices)} choices for field '{field_name}'"
        )
        
        # Perform fuzzy matching
        try:
            matches = fuzzy_filter_choices_multi_scorer(
                queries=user_terms,
                choices=db_choices,
                min_score=FUZZY_MIN_SCORE
            )
            
            if isinstance(matches, list):
                field_matches[field_name] = matches
                logger.info(
                    f"[{tool}] Fuzzy search found {len(matches)} matches "
                    f"for '{field_name}'"
                )
            else:
                logger.warning(
                    f"[{tool}] Fuzzy search returned non-list for '{field_name}': "
                    f"{type(matches)}"
                )
                field_matches[field_name] = []
                
        except Exception as e:
            logger.exception(
                f"[{tool}] Fuzzy matching failed for field '{field_name}': {e}"
            )
            field_matches[field_name] = []
    
    # Create output
    try:
        output = FuzzyFilteredOutputs(
            database=db_name,  # Return original case for API consistency
            value=OutputFields(**field_matches),
            tool=tool
        )
        
        # Count total matches
        total_matches = sum(
            len(v) if isinstance(v, list) else 0
            for v in field_matches.values()
        )
        
        logger.info(
            f"[{tool}] Finished for database '{db_name}'. "
            f"Processed {len(field_matches)} fields with {total_matches} total matches."
        )
        
        return output
        
    except Exception as e:
        logger.exception(f"[{tool}] Failed to create output: {e}")
        # Return safe fallback
        return FuzzyFilteredOutputs(
            database=db_name,
            value=OutputFields(),
            tool=tool
        )