import copy
import logging
import sys
from typing import Dict, Any, Optional, Set

from synonyms.target_family_retriver import TargetMemberAggregator
from synonyms.disease_synonyms import DiseaseSynonymAggregator
from synonyms.drug_synonyms import DrugSynonymAggregator
from synonyms.gene_synonyms import GeneSynonymAggregator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# NOTE: The LLM-filter and DB-overlap machinery that the restricted
# expand_synonyms variant carries (call_llm_filter_service,
# filter_candidates_with_llm, load_db_values, filter_candidates_by_db, plus
# LLM_FILTER_URL / MAX_CONCURRENT_LLM_REQUESTS / DB_VALUE_PATH constants) was
# removed here 2026-06-17: the unrestricted expander returns the full aggregate
# and never narrows, so none of it was ever called. A fuller dedup (shared
# module between the two services behind a flag) remains as future work.

# Cache aggregators to avoid recreating them every call
_aggregator_cache: Optional[Dict[str, Any]] = None


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


async def synonyms_expander(
    data: Dict[str, Any],
    database: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    Expand biomedical terms with their synonyms and aliases.

    Args:
        data: Parsed query values (dict or ParsedValue)
        database: Optional target database name (kept for interface parity).
        raw: Accepted for interface parity with expand_synonyms (the restricted
             variant). The unrestricted expander already returns the full
             aggregate without LLM narrowing, so raw is a no-op here.

    Returns:
        Dict with expanded synonyms for each field
    """
    _ = raw  # interface parity; unrestricted path never runs the LLM filter
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
    
    # -------- TARGET --------
    if "target_name" in field_outputs:
        target_value = field_outputs.get("target_name")
        
        if isinstance(target_value, list) and target_value:
            logger.info(f"[{tool}] Expanding {len(target_value)} targets")
            
            target_synonyms = await expand_terms(
                target_value,
                aggregators["target"],
                "target"
            )
            
            # Normalize original targets
            original_targets = {
                norm for t in target_value
                if (norm := safe_normalize(t)) is not None
            }

            # Return full aggregate in target_name for unrestricted mode.
            processed["target_name"] = sorted(original_targets | target_synonyms)

            # Keep existing target→gene behavior for compatibility.
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
                f"{len(target_synonyms)} synonyms → target_name has {len(processed['target_name'])} total, "
                f"gene_name has {len(processed['gene_name'])} total"
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
            
            # Combine and sort (full aggregate, unrestricted)
            existing_drugs = {
                norm for d in processed.get("drug_name", [])
                if (norm := safe_normalize(d)) is not None
            }
            processed["drug_name"] = sorted(existing_drugs | original_drugs | drug_synonyms)
            
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
            
            # Combine and sort (full aggregate, unrestricted)
            existing_genes = {
                norm for g in processed.get("gene_name", [])
                if (norm := safe_normalize(g)) is not None
            }
            processed["gene_name"] = sorted(original_genes | gene_synonyms | existing_genes)
            
            logger.info(
                f"[{tool}] Gene expansion: "
                f"{len(gene_synonyms)} synonyms → gene_name has {len(processed['gene_name'])} total"
            )
    
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
            
            # Normalize original diseases
            original_diseases = {
                norm for d in disease_value
                if (norm := safe_normalize(d)) is not None
            }
            
            # Combine as set first (used in set union below), sort only at assignment.
            candidate_diseases = original_diseases | disease_synonyms
            # Full aggregate in unrestricted mode (no DB-overlap and no LLM narrowing)
            existing_diseases = {
                norm for d in processed.get("disease_name", [])
                if (norm := safe_normalize(d)) is not None
            }
            processed["disease_name"] = sorted(existing_diseases | candidate_diseases)

            logger.info(
                f"[{tool}] Disease expansion (unrestricted): "
                f"{len(candidate_diseases)} candidates → {len(processed['disease_name'])} final"
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
