import logging
import asyncio
import copy
from typing import Dict, Any, Set, List

from app.services.synonyms.target_family_retriver import TargetMemberAggregator
from app.services.synonyms.gene_synonyms import GeneSynonymAggregator
from app.services.synonyms.disease_synonyms import DiseaseSynonymAggregator
from app.services.synonyms.drug_synonyms import DrugSynonymAggregator

# Configure logging
logger = logging.getLogger(__name__)

if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )


def normalize_term(term: str) -> str:
    """Normalize term to lowercase and strip whitespace."""
    return term.strip().lower() if term else ""


async def fetch_synonyms_parallel(
    terms: List[str],
    aggregator: Any,
    category: str
) -> Set[str]:
    """
    Fetch synonyms for multiple terms in parallel.
    
    Args:
        terms: List of terms to fetch synonyms for
        aggregator: Aggregator instance with get_all_synonyms method
        category: Category name for logging
        
    Returns:
        Set of all synonyms (normalized to lowercase)
    """
    async def fetch_one(term: str) -> Set[str]:
        try:
            result = await aggregator.get_all_synonyms(term)
            synonyms = result.get("combined_synonyms", [])
            
            # Normalize all synonyms
            normalized = {
                normalize_term(syn)
                for syn in synonyms
                if syn and isinstance(syn, str)
            }
            
            logger.info(
                f"[{category}] '{term}': {len(synonyms)} synonyms → "
                f"{len(normalized)} normalized"
            )
            return normalized
            
        except Exception as e:
            logger.exception(f"[{category}] Failed for '{term}': {e}")
            return set()
    
    # Run all fetches in parallel
    results = await asyncio.gather(
        *[fetch_one(term) for term in terms],
        return_exceptions=True
    )
    
    # Combine results
    all_synonyms: Set[str] = set()
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"[{category}] Error in parallel fetch: {result}")
        elif isinstance(result, set):
            all_synonyms.update(result)
    
    return all_synonyms


async def expand_field_synonyms(field_outputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Expand field synonyms for supported fields.
    
    Fields processed:
      - target_name → adds to gene_name
      - gene_name → expands in place
      - drug_name → expands in place
      - disease_name → expands in place
    
    Args:
        field_outputs: Dictionary with field values
        
    Returns:
        Processed dictionary with expanded synonyms (all lowercase)
    """
    if not field_outputs or not isinstance(field_outputs, dict):
        logger.warning("Empty or invalid field_outputs provided")
        return field_outputs or {}
    
    logger.info(f"Expanding synonyms for {len(field_outputs)} fields")
    
    processed = copy.deepcopy(field_outputs)
    
    # Create aggregator instances ONCE
    target_agg = TargetMemberAggregator()
    drug_agg = DrugSynonymAggregator()
    gene_agg = GeneSynonymAggregator()
    disease_agg = DiseaseSynonymAggregator()
    
    # -------------------- Target → Gene Synonyms --------------------
    if "target_name" in processed and isinstance(processed.get("target_name"), list):
        target_list = processed["target_name"]
        
        if target_list:
            try:
                logger.info(f"[TargetSynonyms] Expanding {len(target_list)} targets")
                
                target_synonyms = await fetch_synonyms_parallel(
                    target_list,
                    target_agg,
                    "TargetSynonyms"
                )
                
                # Normalize original targets
                original_targets = {
                    normalize_term(t) for t in target_list
                    if t and isinstance(t, str)
                }
                
                # Combine and sort
                all_targets = sorted(original_targets | target_synonyms)
                processed["target_name"] = all_targets
                
                # FIX: Create or extend gene_name (don't overwrite!)
                if "gene_name" not in processed or not isinstance(processed["gene_name"], list):
                    processed["gene_name"] = []
                
                # Normalize existing genes
                existing_genes = {
                    normalize_term(g) for g in processed["gene_name"]
                    if g and isinstance(g, str)
                }
                
                # Merge target synonyms into gene_name
                all_genes = sorted(existing_genes | original_targets | target_synonyms)
                processed["gene_name"] = all_genes
                
                logger.info(
                    f"[TargetSynonyms] Expanded {len(target_list)} targets → "
                    f"{len(all_targets)} total targets, {len(all_genes)} total genes"
                )
                
            except Exception as e:
                logger.exception("[TargetSynonyms] Block failed: %s", e)
    
    # -------------------- Drug Synonyms --------------------
    if "drug_name" in processed and isinstance(processed.get("drug_name"), list):
        drug_list = processed["drug_name"]
        
        if drug_list:
            try:
                logger.info(f"[DrugSynonyms] Expanding {len(drug_list)} drugs")
                
                drug_synonyms = await fetch_synonyms_parallel(
                    drug_list,
                    drug_agg,
                    "DrugSynonyms"
                )
                
                # Normalize original drugs
                original_drugs = {
                    normalize_term(d) for d in drug_list
                    if d and isinstance(d, str)
                }
                
                # Combine and sort
                all_drugs = sorted(original_drugs | drug_synonyms)
                processed["drug_name"] = all_drugs
                
                logger.info(
                    f"[DrugSynonyms] Expanded {len(drug_list)} drugs → "
                    f"{len(all_drugs)} total"
                )
                
            except Exception as e:
                logger.exception("[DrugSynonyms] Block failed: %s", e)
    
    # -------------------- Gene Synonyms --------------------
    if "gene_name" in processed and isinstance(processed.get("gene_name"), list):
        gene_list = processed["gene_name"]
        
        if gene_list:
            try:
                logger.info(f"[GeneSynonyms] Expanding {len(gene_list)} genes")
                
                gene_synonyms = await fetch_synonyms_parallel(
                    gene_list,
                    gene_agg,
                    "GeneSynonyms"
                )
                
                # Normalize original genes
                original_genes = {
                    normalize_term(g) for g in gene_list
                    if g and isinstance(g, str)
                }
                
                # Combine and sort
                all_genes = sorted(original_genes | gene_synonyms)
                processed["gene_name"] = all_genes
                
                logger.info(
                    f"[GeneSynonyms] Expanded {len(gene_list)} genes → "
                    f"{len(all_genes)} total"
                )
                
            except Exception as e:
                logger.exception("[GeneSynonyms] Block failed: %s", e)
    
    # -------------------- Disease Synonyms --------------------
    if "disease_name" in processed and isinstance(processed.get("disease_name"), list):
        disease_list = processed["disease_name"]
        
        if disease_list:
            try:
                logger.info(f"[DiseaseSynonyms] Expanding {len(disease_list)} diseases")
                
                disease_synonyms = await fetch_synonyms_parallel(
                    disease_list,
                    disease_agg,
                    "DiseaseSynonyms"
                )
                
                # Normalize original diseases
                original_diseases = {
                    normalize_term(d) for d in disease_list
                    if d and isinstance(d, str)
                }
                
                # Combine and sort
                all_diseases = sorted(original_diseases | disease_synonyms)
                processed["disease_name"] = all_diseases
                
                logger.info(
                    f"[DiseaseSynonyms] Expanded {len(disease_list)} diseases → "
                    f"{len(all_diseases)} total"
                )
                
            except Exception as e:
                logger.exception("[DiseaseSynonyms] Block failed: %s", e)
    
    # Close aggregators if they have close methods
    for agg in [target_agg, drug_agg, gene_agg, disease_agg]:
        if hasattr(agg, 'close'):
            try:
                await agg.close()
            except Exception as e:
                logger.debug(f"Error closing aggregator: {e}")
    
    logger.info("Synonym expansion complete")
    return processed

