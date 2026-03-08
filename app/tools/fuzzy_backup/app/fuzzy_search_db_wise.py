from typing import List, Union, Any, Dict
import pickle
import logging
from config.guardrail import ParsedValue, OutputFields, FuzzyFilteredOutputs
from .fuzzy import fuzzy_filter_choices_multi_scorer
import os
import requests
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

DB_VALUE_PATH = "resources/values/concept_values_by_db_and_field.pkl"
with open(DB_VALUE_PATH, 'rb') as f:
    db_value = pickle.load(f)


USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))  # slightly under route budget
MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))
FUZZY_SCORE_CUT_SCORE = float(os.getenv("FUZZY_SCORE_CUT_SCORE", "90"))

async def compute_fuzzy_filtered_outputs(parsed: Union[ParsedValue, Dict[str, Any]], database: str) -> FuzzyFilteredOutputs:

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

    for db_name in [database]:
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
            matches = fuzzy_filter_choices_multi_scorer(queries=user_terms, choices=db_choices, min_score=FUZZY_SCORE_CUT_SCORE)

            
            
            logger.info(f"[{tool} search raw db wise code] Running")

            logger.info(f"[{tool} search raw db wise code] Output: {matches}")
            
            llm_filter_url = f"http://biochirp_llm_filter_tool:8017/llm_member_selection_filter"


            llm_filter_match = list()

            for i in user_terms:


                input_filtered= Llm_Member_Selector_Input(category = field_name, single_term=i, string_list=matches).model_dump()
                llm_filter_match.extend(requests.post(llm_filter_url, json=input_filtered).json()["value"])
                # matches = requests.post(llm_filter_url, json=input_filtered).json()["value"]

            logger.info(f"[{tool} search llm filtered db wise code] Running")

            logger.info(f"[{tool} search llm filtered db wise code] Output: {llm_filter_match}")
 

            if isinstance(llm_filter_match, list):
                field_matches[field_name] = llm_filter_match

            else:
                field_matches[field_name] = []

            

        if field_matches:
            db_result["value"] = OutputFields(**field_matches)
            db_result["database"] = db_name
            db_result["tool"] = tool

    final_output = FuzzyFilteredOutputs(**db_result)

    logger.info(f"[{tool} db wise code] output: {final_output}")

    logger.info(f"[{tool} db wise code] Finished")

    return final_output