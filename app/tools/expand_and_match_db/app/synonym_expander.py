
import copy
import logging

from synonyms import target_family_retriver
from synonyms.disease_synonyms import DiseaseSynonymAggregator
from synonyms.drug_synonyms import DrugSynonymAggregator
from synonyms.gene_synonyms import GeneSynonymAggregator
from config.guardrail import ExpandSynonymsOutput, QueryInterpreterOutputGuardrail, ParsedValue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

async def synonyms_expander(data: ParsedValue) -> dict:

    tool = "expand_synonyms"

    try:
        field_outputs = data.model_dump(exclude_none=True)
    except:
        field_outputs= data


    logger.info(f"[{tool} code] Running")

    processed = copy.deepcopy(field_outputs)
    processed = dict(sorted(processed.items()))

    processed = {k: processed[k] for k in sorted(processed, reverse=False)}

    logger.info(f"[{tool} code] Input: {processed}")

    for key in list(processed.keys()):

        if key=="target_name":

            # -------------------- Target - Gene Synonyms --------------------
            if isinstance(processed.get("target_name"), list):
                # task = await add_progress_task(task_list, "Obtaining target synonyms", delay=0.05)
                try:
                    all_syns = set()
                    for target in field_outputs["target_name"]:
                        try:
                            syns = await target_family_retriver.TargetMemberAggregator().get_all_synonyms(target)
                            all_syns.update(syns.get("combined_synonyms", []))
                            logger.info(f"[{tool} code] '{target}' : {len(syns['combined_synonyms'])} synonyms")
                        except Exception as e:
                            logger.exception(f"[{tool} code] Failed for '{target}': {e}")


                    # original = processed.get("target_name", [])
                    if isinstance(processed.get("gene_name"), list):
                        processed["gene_name"].extend(all_syns)
                        processed["gene_name"] = list(set(processed["gene_name"]))
                    else:
                        processed["gene_name"] = list(all_syns)
                except Exception as e:
                    logger.exception(f"[{tool} code] Block failed: %s", e)


        if key=="drug_name":

            # -------------------- Drug Synonyms --------------------
            if isinstance(processed.get("drug_name"), list):
                # task = await add_progress_task(task_list, "Obtaining drug synonyms", delay=0.05)
                try:
                    all_syns = set()
                    for drug in field_outputs["drug_name"]:
                        try:
                            syns = await DrugSynonymAggregator().get_all_synonyms(drug)
                            all_syns.update(syns.get("combined_synonyms", []))
                            processed["drug_name"].extend(all_syns)
                            processed["drug_name"] = list(set(processed["drug_name"]))
                            logger.info(f"[{tool} code] '{drug}' : {len(syns['combined_synonyms'])} synonyms")
                        except Exception as e:
                            logger.exception(f"[{tool} code] Failed for '{drug}': {e}")

                except Exception as e:
                    logger.exception("[{tool} code] Block failed: %s", e)

    # -------------------- Gene Synonyms --------------------
        if key=="gene_name":
            if isinstance(processed.get("gene_name"), list):
                try:
                    all_syns = set()
                    for gene in field_outputs["gene_name"]:
                        try:
                            syns = await GeneSynonymAggregator().get_all_synonyms(gene)
                            all_syns.update(syns.get("combined_synonyms", []))
                            processed["gene_name"].extend(all_syns)
                            processed["gene_name"] = list(set(processed["gene_name"]))
                            logger.info(f"[{tool} code] '{gene}' : {len(syns['combined_synonyms'])} synonyms")
                        except Exception as e:
                            logger.exception(f"[{tool} code] Failed for '{gene}': {e}")

                except Exception as e:
                    logger.exception(f"[{tool} code] Block failed: %s", e)


    # -------------------- Disease Synonyms --------------------

        if key=="disease_name":
            if isinstance(processed.get("disease_name"), list):
                try:
                    all_syns = set()
                    for disease in field_outputs["disease_name"]:
                        try:
                            syns = await DiseaseSynonymAggregator().get_all_synonyms(disease)
                            all_syns.update(syns.get("combined_synonyms", []))
                            processed["disease_name"].extend(all_syns)
                            processed["disease_name"] = list(set(processed["disease_name"]))
                            
                            logger.info(f"[{tool} code] '{disease}' : {len(syns['combined_synonyms'])} synonyms")
                        except Exception as e:
                            logger.exception(f"[{tool} code] Failed for '{disease}': {e}")


                except Exception as e:
                    logger.exception("[{tool} code] Block failed: %s", e)

    
    output_data = {k: (sorted(v) if v else None) for k, v in processed.items() if isinstance(v, list)}

    logger.info(f"[{tool} code] Output : {output_data}")

    logger.info(f"[{tool} code] Finished")

    return output_data