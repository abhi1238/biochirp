from typing import List, Optional, Union, Any, Dict, Literal, Tuple
import pickle
import logging
from config.guardrail import ParsedValue, OutputFields, FuzzyFilteredOutputs
from .fuzzy import fuzzy_filter_choices_multi_scorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn.error")


DB_VALUE_PATH = "resources/values/concept_values_by_db_and_field.pkl"
with open(DB_VALUE_PATH, 'rb') as f:
    db_value = pickle.load(f)

async def compute_fuzzy_filtered_outputs(parsed: Union[ParsedValue, Dict[str, Any]], db_name: str) -> FuzzyFilteredOutputs:

    """
    For each database, perform fuzzy matching of user terms to db_choices for each field.
    Logs and shows stepwise fuzzy search for full transparency.
    """

    tool = "fuzzy"

    logger.info(f"[{tool} db wise code] Running")

    try:

        fields = parsed.model_dump(exclude_none=True)

    except Exception:

        fields = parsed

    db_result = {}

    for db_name in [db_name]:
        db_fields = db_value.get(db_name, {})

        field_matches = {}

        for field_name, user_terms in fields.items():

            if isinstance(user_terms, str):
                field_matches[field_name] = user_terms
                continue

            db_choices = db_fields.get(field_name)

            if not db_choices:
                continue

            # Actual fuzzy search
            matches = fuzzy_filter_choices_multi_scorer(queries=user_terms, choices=db_choices, min_score=90)

            if isinstance(matches, list):
                field_matches[field_name] = matches

            else:
                field_matches[field_name] = []

        if field_matches:
            db_result["value"] = OutputFields(**field_matches)
            db_result["database"] = db_name
            db_result["tool"] = "fuzzy"

    final_output = FuzzyFilteredOutputs(**db_result)

    logger.info(f"[{tool} db wise code] output: {final_output}")

    logger.info(f"[{tool} db wise code] Finished")

    return final_output