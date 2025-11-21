from app.utils.fuzzy_utils import fuzzy_filter_choices_multi_scorer
from app.services.semantic_matching import find_semantic_matches
from app.services.vector_search_faiss import search_reference_term_all_models_faiss
from app.utils.fuzzy_utils import fuzzy_filter_choices_multi_scorer
from config.guardrail import (
    ParsedValue, OutputFields, FuzzyFilteredOutputs, SemanticFilteredOutputs
)
from typing import (
    Any, Dict, List, Literal, Optional, Type, Union
)
import copy
import logging
import json

from config.settings import BIOMEDICAL_MODELS

# from app.runtime_context import embeddings_var, model_cache_var, db_value_var

from app.runtime_context import embeddings_var, model_cache_var, db_value_var, prompt_var

logger = logging.getLogger("biochirp.tools.expand_and_match_db")

async def _require_ctx(name: str, var) -> Any:
    """Get a value from a ContextVar or raise a clear error."""
    val = var.get()
    if val is None:
        raise RuntimeError(
            f"{name} not set in runtime context. "
            f"Ensure you call embeddings_var.set(...), model_cache_var.set(...), db_value_var.set(...) "
            f"in your /ws handler before Runner.run_streamed()."
        )
    return val


async def compute_fuzzy_filtered_outputs(
    parsed: Union[ParsedValue, Dict[str, Any]]) -> FuzzyFilteredOutputs:

    """
    For each database, perform fuzzy matching of user terms to db_choices for each field.
    Logs and shows stepwise fuzzy search for full transparency.
    """

    embeddings  = await _require_ctx("embeddings",  embeddings_var)
    model_cache = await _require_ctx("model_cache", model_cache_var)
    db_value    = await _require_ctx("db_value",    db_value_var)
    prompt_content = await _require_ctx("prompt_var", prompt_var)

    # await log_ui(
    # "###  Performing Fuzzy Search Across Databases\n" "For each field, user terms will be matched to each database's canonical values using fuzzy logic.\n", author="BioChirp", tool = "Fuzzy search")

    try:
        fields = parsed.model_dump(exclude_none=True)
    except Exception:
        fields = parsed

    db_result = {}

    for db_name in ['ttd', 'ctd', 'hcdt']:
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
            matches = fuzzy_filter_choices_multi_scorer(
                queries=user_terms, choices=db_choices, min_score=90
            )

            if isinstance(matches, list):
                field_matches[field_name] = matches

            else:
                field_matches[field_name] = []

        if field_matches:
            db_result[db_name] = OutputFields(**field_matches)

    final_output = FuzzyFilteredOutputs(**db_result)

    logger.info(final_output)
    pretty_json = json.dumps(final_output.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
    # await log_ui(
    #     # "#### **Final FuzzyFilteredOutputs**\n"
    #     "<details><summary>Show full list</summary>\n\n"
    #     f"```json\n{pretty_json}\n```\n</details>",
    #     author="BioChirp", tool = "Fuzzy search"
    # )
    # await log_ui(final_output)

    return final_output




# def compute_fuzzy_filtered_outputs(
#     field_outputs: dict,
#     db_value: dict,
#     min_score: int = 90
# ) -> dict:
#     """
#     Compute fuzzy matches of user terms against DB choices for each field and database.

#     Args:
#         field_outputs (dict): Dict of user-provided terms per field.
#         db_value (dict): Dict of valid database choices for each field.
#         min_score (int): Minimum fuzzy matching score threshold.

#     Returns:
#         dict: Nested dictionary of fuzzy-matched terms by database and field.
#     """
#     fuzzy_filtered_outputs_by_database = {}

#     for db_name, db_fields in db_value.items():
#         fuzzy_filtered_outputs_by_database[db_name] = {}

#         for field_name, user_terms in field_outputs.items():
#             if not isinstance(user_terms, list):
#                 continue

#             db_choices = db_fields.get(field_name)
#             if not db_choices:
#                 logger.warning(f"[FuzzyFilter] Missing choices for {db_name}.{field_name}")
#                 continue

#             try:
#                 logger.info(f"[FuzzyFilter] Matching {field_name} in {db_name} with {len(user_terms)} term(s)")
#                 matches = fuzzy_filter_choices_multi_scorer(
#                     queries=user_terms,
#                     choices=db_choices,
#                     min_score=min_score
#                 )
#                 fuzzy_filtered_outputs_by_database[db_name][field_name] = matches
#                 logger.info(f"[FuzzyFilter] {db_name}.{field_name}: {len(matches)} match(es) found")

#             except Exception as e:
#                 logger.exception(f"[FuzzyFilter] Failed matching {db_name}.{field_name}: {e}")

#     return fuzzy_filtered_outputs_by_database




import logging
import pandas as pd
from app.services.vector_search_faiss import search_reference_term_all_models_faiss

logger = logging.getLogger(__name__)

def compute_similarity_filtered_outputs(
    field_outputs: dict,
    db_value: dict,
    dataset: list,
    embedding_content: dict,
    model_cache: dict,
    limit_per_model: int = 200,
    use_knee_cutoff: bool = True
) -> dict:
    """
    Compute similarity-based matches (via FAISS embeddings) of user terms
    against DB choices for each field and database.

    Args:
        field_outputs (dict): Dict of user-provided terms per field.
        db_value (dict): Dict of valid database choices for each DB and field.
        dataset (list): List of DB names to process.
        embedding_content (dict): Precomputed embeddings per model/db/field.
        model_cache (dict): Cache of loaded SentenceTransformer models.
        limit_per_model (int): Max candidates retrieved per model.
        use_knee_cutoff (bool): Whether to apply knee-based similarity cutoff.

    Returns:
        dict: Nested dictionary of similarity-filtered terms by database and field.
    """
    similarity_filtered_outputs_by_database = {}

    for db_name in dataset:
        similarity_filtered_outputs_by_database[db_name] = {}

        for field_name, user_terms in field_outputs.items():
            if not isinstance(user_terms, list):
                continue

            db_choices = db_value.get(db_name, {}).get(field_name)
            if not db_choices:
                logger.warning(f"[FAISS] Missing choices for {db_name}.{field_name}")
                continue

            valid_db_choices_lower = set(d.lower() for d in db_choices)
            aggregated_matches = []

            for term in user_terms:
                try:
                    logger.info(f"[FAISS] Searching for term '{term}' in {db_name}.{field_name}")
                    search_results = search_reference_term_all_models_faiss(
                        reference_term=term,
                        target_field=field_name,
                        model_field_embeddings=embedding_content,
                        model_cache=model_cache,
                        limit_per_model=limit_per_model,
                        use_knee_cutoff=use_knee_cutoff
                    )

                    matched_texts = (
                        list(set(search_results["text"]))
                        if isinstance(search_results, pd.DataFrame) and "text" in search_results.columns
                        else []
                    )
                    aggregated_matches.extend(matched_texts)

                except Exception as e:
                    logger.error(
                        f"[FAISS] Error searching for '{term}' in {db_name}.{field_name}: {e}",
                        exc_info=True
                    )

            # Case-insensitive filtering against valid DB values
            matched_lower = {r.lower() for r in aggregated_matches}
            final_filtered = [val for val in db_choices if val.lower() in matched_lower]

            similarity_filtered_outputs_by_database[db_name][field_name] = final_filtered
            logger.info(f"[FAISS] {db_name}.{field_name}: {len(final_filtered)} match(es) retained")

    return similarity_filtered_outputs_by_database