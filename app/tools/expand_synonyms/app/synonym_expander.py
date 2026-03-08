import asyncio
import copy
import logging
import os
import pickle
import sys
from pathlib import Path
from typing import Dict, Any, Optional, Set, List

import httpx

from synonyms.target_family_retriver import TargetMemberAggregator
from synonyms.disease_synonyms import DiseaseSynonymAggregator
from synonyms.drug_synonyms import DrugSynonymAggregator
from synonyms.gene_synonyms import GeneSynonymAggregator
from config.guardrail import ParsedValue, Llm_Member_Selector_Input

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# LLM filter configuration (used only for disease_name)
def _build_llm_filter_url() -> str:
    url = os.getenv("LLM_FILTER_URL")
    if url:
        return url
    host = os.getenv("LLM_FILTER_HOST", "biochirp_llm_filter_tool")
    port = os.getenv("LLM_FILTER_PORT", "8017")
    return f"http://{host}:{port}/llm_member_selection_filter"


LLM_FILTER_URL = _build_llm_filter_url()
LLM_FILTER_TIMEOUT = float(os.getenv("LLM_FILTER_TIMEOUT", "30"))
MAX_CONCURRENT_LLM_REQUESTS = int(os.getenv("MAX_CONCURRENT_LLM_REQUESTS", "5"))

# Database values (for overlap filtering)
DB_VALUE_PATH = os.getenv(
    "DB_VALUE_PATH",
    "resources/values/concept_values_by_db_and_field.pkl"
)
_db_value_cache: Optional[Dict[str, Any]] = None

# Cache aggregators to avoid recreating them every call
_aggregator_cache: Optional[Dict[str, Any]] = None


def load_db_values() -> Dict[str, Any]:
    """
    Lazy load database values for overlap filtering.
    """
    global _db_value_cache

    if _db_value_cache is not None:
        return _db_value_cache

    try:
        db_path = Path(DB_VALUE_PATH)
        if not db_path.exists():
            logger.warning(f"[expand_synonyms] DB values file not found: {db_path}")
            _db_value_cache = {}
            return _db_value_cache

        with open(db_path, "rb") as f:
            _db_value_cache = pickle.load(f)

        logger.info(
            f"[expand_synonyms] Loaded DB values from {db_path}. "
            f"Databases: {list(_db_value_cache.keys())}"
        )
        return _db_value_cache
    except Exception as e:
        logger.exception(f"[expand_synonyms] Failed to load DB values: {e}")
        _db_value_cache = {}
        return _db_value_cache


def filter_candidates_by_db(
    db_name: Optional[str],
    field_name: str,
    candidates: List[str]
) -> List[str]:
    """
    Keep only candidates that overlap with database choices (case-insensitive).
    """
    if not db_name or not candidates:
        return candidates

    values = load_db_values()
    if not values:
        return candidates

    db_key = next(
        (k for k in values.keys() if k.lower() == db_name.lower()),
        None
    )
    if not db_key:
        logger.warning(f"[expand_synonyms] Database '{db_name}' not found in values")
        return candidates

    db_fields = values.get(db_key, {})
    db_choices = db_fields.get(field_name) if isinstance(db_fields, dict) else None
    if not db_choices:
        logger.warning(
            f"[expand_synonyms] No DB choices for {db_key}.{field_name}"
        )
        return candidates

    db_choice_set = {
        norm for c in db_choices
        if (norm := safe_normalize(c)) is not None
    }

    filtered = [
        c for c in candidates
        if (norm := safe_normalize(c)) is not None and norm in db_choice_set
    ]

    logger.info(
        f"[expand_synonyms] DB overlap filter for {db_key}.{field_name}: "
        f"{len(candidates)} → {len(filtered)}"
    )
    return filtered


def get_aggregators() -> Dict[str, Any]:
    """
    Lazy load and cache synonym aggregators.
    
    Returns:
        Dict with aggregator instances
    """
    global _aggregator_cache
    
    if _aggregator_cache is not None:
        return _aggregator_cache
    
    logger.info("[expand_synonyms] Initializing synonym aggregators...")
    
    _aggregator_cache = {
        "target": TargetMemberAggregator(),
        "drug": DrugSynonymAggregator(),
        "gene": GeneSynonymAggregator(),
        "disease": DiseaseSynonymAggregator()
    }
    
    logger.info("[expand_synonyms] Aggregators initialized")
    return _aggregator_cache


def safe_normalize(value: Any) -> Optional[str]:
    """
    Safely normalize a value to lowercase string.
    
    Args:
        value: Any value to normalize
        
    Returns:
        Normalized string or None if invalid
    """
    if value is None:
        return None
    
    if not isinstance(value, str):
        # Try to convert to string
        try:
            value = str(value)
        except Exception:
            logger.warning(f"Cannot normalize non-string value: {type(value)}")
            return None
    
    normalized = value.strip().lower()
    return normalized if normalized else None


async def call_llm_filter_service(
    client: httpx.AsyncClient,
    field_name: str,
    single_term: str,
    matches: List[str]
) -> List[str]:
    """
    Call LLM filter service for a single term with error handling.
    """
    try:
        input_data = Llm_Member_Selector_Input(
            category=field_name,
            single_term=single_term,
            string_list=matches
        ).model_dump()

        response = await client.post(
            LLM_FILTER_URL,
            json=input_data,
            timeout=LLM_FILTER_TIMEOUT
        )
        response.raise_for_status()

        result = response.json()

        if not isinstance(result, dict) or "value" not in result:
            logger.warning(
                f"[expand_synonyms] Invalid LLM filter response for term '{single_term}': {result}"
            )
            return []

        filtered = result["value"]
        if not isinstance(filtered, list):
            logger.warning(
                f"[expand_synonyms] LLM filter returned non-list for term '{single_term}': {type(filtered)}"
            )
            return []

        return filtered

    except httpx.TimeoutException:
        logger.error(f"[expand_synonyms] Timeout calling LLM filter for term '{single_term}'")
        return []
    except httpx.HTTPError as e:
        logger.error(f"[expand_synonyms] HTTP error calling LLM filter for term '{single_term}': {e}")
        return []
    except Exception as e:
        logger.exception(f"[expand_synonyms] Error calling LLM filter for term '{single_term}': {e}")
        return []


async def filter_candidates_with_llm(
    field_name: str,
    user_terms: List[str],
    candidates: List[str]
) -> List[str]:
    """
    Run LLM filter for each user term and return a unique filtered list.
    Falls back to empty list on failure.
    """
    if not user_terms or not candidates:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)

    async with httpx.AsyncClient() as client:
        async def filtered_call(term: str) -> List[str]:
            async with semaphore:
                return await call_llm_filter_service(client, field_name, term, candidates)

        try:
            llm_filter_results = await asyncio.gather(
                *[filtered_call(term) for term in user_terms],
                return_exceptions=True
            )
        except Exception as e:
            logger.exception(f"[expand_synonyms] LLM filtering failed for field '{field_name}': {e}")
            return []

    llm_filter_matches: List[str] = []
    error_count = 0

    for i, result in enumerate(llm_filter_results):
        if isinstance(result, Exception):
            error_count += 1
            logger.error(
                f"[expand_synonyms] LLM filter error for term '{user_terms[i]}': {result}"
            )
            continue
        if isinstance(result, list):
            llm_filter_matches.extend(result)
        else:
            logger.warning(
                f"[expand_synonyms] Unexpected LLM filter result type for term '{user_terms[i]}': "
                f"{type(result)}"
            )

    if error_count > 0:
        logger.warning(
            f"[expand_synonyms] {error_count}/{len(user_terms)} LLM filter calls failed "
            f"for field '{field_name}'"
        )

    # Normalize + dedupe
    seen: Set[str] = set()
    filtered_unique: List[str] = []
    for match in llm_filter_matches:
        norm = safe_normalize(match)
        if norm and norm not in seen:
            seen.add(norm)
            filtered_unique.append(norm)

    return filtered_unique


async def synonyms_expander(
    data: Dict[str, Any],
    database: Optional[str] = None
) -> Dict[str, Any]:
    """
    Expand biomedical terms with their synonyms and aliases.
    
    Args:
        data: Parsed query values (dict or ParsedValue)
        
    Returns:
        Dict with expanded synonyms for each field
    """
    tool = "expand_synonyms"
    
    logger.info(f"[{tool}] Starting synonym expansion")
    
    # Convert to dict if needed
    try:
        if hasattr(data, 'model_dump'):
            field_outputs = data.model_dump(exclude_none=True)
        else:
            field_outputs = data
    except Exception as e:
        logger.error(f"[{tool}] Failed to convert input to dict: {e}")
        field_outputs = data if isinstance(data, dict) else {}
    
    if not field_outputs:
        logger.warning(f"[{tool}] Empty input data")
        return {}
    
    # db_name = database.strip() if isinstance(database, str) else None
    db_name = database.strip() if isinstance(database, str) and database.strip() else None

    logger.info(f"[{tool}] Input fields: {list(field_outputs.keys())}")
    
    # Deep copy to avoid mutating input
    processed = copy.deepcopy(field_outputs)
    
    # Get aggregators (cached)
    try:
        aggregators = get_aggregators()
    except Exception as e:
        logger.exception(f"[{tool}] Failed to load aggregators: {e}")
        return processed
    
    # Helper to safely expand a list of terms
    async def expand_terms(
        terms: list,
        aggregator: Any,
        category: str
    ) -> Set[str]:
        """Expand a list of terms using an aggregator."""
        all_synonyms = set()
        
        for term in terms:
            if not term:
                continue
            
            try:
                result = await aggregator.get_all_synonyms(term)
                synonyms = result.get("combined_synonyms", [])
                
                # Safely normalize all synonyms
                normalized = [
                    norm for s in synonyms
                    if (norm := safe_normalize(s)) is not None
                ]
                
                all_synonyms.update(normalized)
                
                logger.info(
                    f"[{tool}] {category} '{term}': "
                    f"{len(synonyms)} synonyms → {len(normalized)} normalized"
                )
                
            except Exception as e:
                logger.exception(f"[{tool}] Failed to expand {category} '{term}': {e}")
        
        return all_synonyms
    
    # -------- TARGET → GENE --------
    if "target_name" in field_outputs:
        target_value = field_outputs.get("target_name")
        
        if isinstance(target_value, list) and target_value:
            logger.info(f"[{tool}] Expanding {len(target_value)} targets")
            
            target_synonyms = await expand_terms(
                target_value,
                aggregators["target"],
                "target"
            )
            
            # FIX: Create gene_name if it doesn't exist!
            if "gene_name" not in processed or not isinstance(processed["gene_name"], list):
                processed["gene_name"] = []
            
            # Normalize existing genes
            existing_genes = {
                norm for g in processed["gene_name"]
                if (norm := safe_normalize(g)) is not None
            }
            
            # Combine and sort
            processed["gene_name"] = sorted(existing_genes | target_synonyms)
            
            logger.info(
                f"[{tool}] Target expansion: "
                f"{len(target_synonyms)} synonyms → gene_name has {len(processed['gene_name'])} total"
            )
    
    # -------- DRUG --------
    if "drug_name" in field_outputs:
        drug_value = field_outputs.get("drug_name")
        
        if isinstance(drug_value, list) and drug_value:
            logger.info(f"[{tool}] Expanding {len(drug_value)} drugs")
            
            drug_synonyms = await expand_terms(
                drug_value,
                aggregators["drug"],
                "drug"
            )
            
            # Normalize original drugs
            original_drugs = {
                norm for d in drug_value
                if (norm := safe_normalize(d)) is not None
            }
            
            # Combine and sort
            processed["drug_name"] = sorted(original_drugs | drug_synonyms)
            
            logger.info(
                f"[{tool}] Drug expansion: "
                f"{len(drug_synonyms)} synonyms → drug_name has {len(processed['drug_name'])} total"
            )
    
    # -------- GENE --------
    if "gene_name" in field_outputs:
        gene_value = field_outputs.get("gene_name")
        
        if isinstance(gene_value, list) and gene_value:
            logger.info(f"[{tool}] Expanding {len(gene_value)} genes")
            
            gene_synonyms = await expand_terms(
                gene_value,
                aggregators["gene"],
                "gene"
            )
            
            # Normalize original genes
            original_genes = {
                norm for g in gene_value
                if (norm := safe_normalize(g)) is not None
            }
            
            # Combine and sort (merge with any existing from target expansion)
            existing = set(processed.get("gene_name", []))
            processed["gene_name"] = sorted(original_genes | gene_synonyms | existing)
            
            logger.info(
                f"[{tool}] Gene expansion: "
                f"{len(gene_synonyms)} synonyms → gene_name has {len(processed['gene_name'])} total"
            )
    
    # -------- DISEASE --------
    # -------- DISEASE --------
    if "disease_name" in field_outputs:
        disease_value = field_outputs.get("disease_name")

        if isinstance(disease_value, list) and disease_value:
            logger.info(f"[{tool}] Expanding {len(disease_value)} diseases")

            disease_synonyms = await expand_terms(
                disease_value,
                aggregators["disease"],
                "disease"
            )

            original_diseases = {
                norm for d in disease_value
                if (norm := safe_normalize(d)) is not None
            }

            candidate_diseases = sorted(original_diseases | disease_synonyms)

            # NEW: if database is None -> return full aggregate directly
            if db_name is None:
                processed["disease_name"] = candidate_diseases
                logger.info(
                    f"[{tool}] Disease expansion (no database): "
                    f"returning {len(processed['disease_name'])} aggregated candidates"
                )
            else:
                # Keep existing behavior when database is provided
                db_filtered_candidates = filter_candidates_by_db(
                    db_name,
                    "disease_name",
                    candidate_diseases
                )
                logger.info(
                    f"[{tool}] Disease candidates after DB overlap: "
                    f"{len(db_filtered_candidates)}"
                )

                user_terms = [
                    d.strip() for d in disease_value
                    if isinstance(d, str) and d.strip()
                ]

                llm_filtered = []
                if db_filtered_candidates:
                    llm_filtered = await filter_candidates_with_llm(
                        field_name="disease_name",
                        user_terms=user_terms,
                        candidates=db_filtered_candidates
                    )
                else:
                    logger.info(f"[{tool}] Disease LLM filter skipped: no DB-overlap candidates")

                if llm_filtered:
                    processed["disease_name"] = sorted(set(llm_filtered))
                    logger.info(
                        f"[{tool}] Disease expansion + LLM filter: "
                        f"{len(db_filtered_candidates)} candidates → {len(processed['disease_name'])} final"
                    )
                else:
                    processed["disease_name"] = sorted(original_diseases)
                    logger.info(
                        f"[{tool}] Disease expansion: LLM returned no matches; "
                        f"keeping {len(processed['disease_name'])} original terms only"
                    )

    # -------- FINAL OUTPUT --------
    output_data = {
        k: (
            v if v == "requested"  # Keep "requested" as-is
            else sorted(v) if isinstance(v, list) and v  # Sort non-empty lists
            else None  # Everything else becomes None
        )
        for k, v in processed.items()
    }
    
    # Log summary
    total_terms = sum(
        len(v) if isinstance(v, list) else 0
        for v in output_data.values()
    )
    
    logger.info(
        f"[{tool}] Finished. "
        f"Output has {len(output_data)} fields with {total_terms} total terms"
    )
    
    return output_data
