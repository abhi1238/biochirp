
from app.services.synonyms import target_family_retriver
from app.services.synonyms.gene_synonyms import GeneSynonymAggregator
from app.services.synonyms import target_family_retriver
from app.services.synonyms.disease_synonyms import DiseaseSynonymAggregator
from app.services.synonyms.drug_synonyms import DrugSynonymAggregator, PubChemFetcher

import copy
import logging

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

async def expand_field_synonyms(field_outputs: dict, task_list: list) -> dict:
    """
    Given raw field_outputs, returns a processed version with added synonyms for supported fields.
    Fields updated:
      - target_name (? adds to gene_name)
      - gene_name
      - drug_name
      - disease_name
    """
    processed = copy.deepcopy(field_outputs)

    # -------------------- Target ? Gene Synonyms --------------------
    if isinstance(processed.get("target_name"), list):

        try:
            all_syns = set()
            for target in processed["target_name"]:
                try:
                    syns = await target_family_retriver.TargetMemberAggregator().get_all_synonyms(target)
                    all_syns.update(syns.get("combined_synonyms", []))
                    logger.info(f"[TargetSynonyms] '{target}' ? {len(syns['combined_synonyms'])} synonyms")
                except Exception as e:
                    logger.exception(f"[TargetSynonyms] Failed for '{target}': {e}")


            original = processed.get("target_name", [])
            if not isinstance(original, list):
                original = [original]
            seen = set(original)
            processed["target_name"] = original + [g for g in all_syns if g not in seen and not seen.add(g)]

            processed["gene_name"] = original + [g for g in all_syns if g not in seen and not seen.add(g)]

        except Exception as e:
            logger.exception("[TargetSynonyms] Block failed: %s", e)

    # -------------------- Drug Synonyms --------------------
    if isinstance(processed.get("drug_name"), list):

        try:
            all_syns = set()
            for drug in processed["drug_name"]:
                try:
                    syns = await DrugSynonymAggregator().get_all_synonyms(drug)
                    all_syns.update(syns.get("combined_synonyms", []))
                    logger.info(f"[DrugSynonyms] '{drug}' ? {len(syns['combined_synonyms'])} synonyms")
                except Exception as e:
                    logger.exception(f"[DrugSynonyms] Failed for '{drug}': {e}")

            original = processed["drug_name"]
            seen = set(original)
            processed["drug_name"] = original + [d for d in all_syns if d not in seen and not seen.add(d)]

        except Exception as e:
            logger.exception("[DrugSynonyms] Block failed: %s", e)

    # -------------------- Gene Synonyms --------------------
    if isinstance(processed.get("gene_name"), list):

        try:
            all_syns = set()
            for gene in processed["gene_name"]:
                try:
                    syns = await GeneSynonymAggregator().get_all_synonyms(gene)
                    all_syns.update(syns.get("combined_synonyms", []))
                    logger.info(f"[GeneSynonyms] '{gene}' ? {len(syns['combined_synonyms'])} synonyms")
                except Exception as e:
                    logger.exception(f"[GeneSynonyms] Failed for '{gene}': {e}")

            original = processed["gene_name"]
            seen = set(original)
            processed["gene_name"] = original + [g for g in all_syns if g not in seen and not seen.add(g)]

        except Exception as e:
            logger.exception("[GeneSynonyms] Block failed: %s", e)

    # -------------------- Disease Synonyms --------------------
    if isinstance(processed.get("disease_name"), list):

        try:
            all_syns = set()
            for disease in processed["disease_name"]:
                try:
                    syns = await DiseaseSynonymAggregator().get_all_synonyms(disease)
                    all_syns.update(syns.get("combined_synonyms", []))
                    logger.info(f"[DiseaseSynonyms] '{disease}' ? {len(syns['combined_synonyms'])} synonyms")
                except Exception as e:
                    logger.exception(f"[DiseaseSynonyms] Failed for '{disease}': {e}")

            original = processed["disease_name"]
            seen = set(original)
            processed["disease_name"] = original + [d for d in all_syns if d not in seen and not seen.add(d)]

        except Exception as e:
            logger.exception("[DiseaseSynonyms] Block failed: %s", e)


    return processed


