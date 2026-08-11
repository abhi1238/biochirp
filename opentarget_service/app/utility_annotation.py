"""Annotation / property tools for the OpenTargets agent.

Thin wrappers over the existing data-layer fetchers (`get_target_biological_info`,
`get_drug_enriched_info`, `get_disease_enriched_info`). Those fetchers already
resolve a free-text name to an Open Targets ID and run the rich per-entity
GraphQL query — but `target_tool`/`disease_tool`/`drug_tool` only use them for
association rows, leaving the annotation fields (tractability, safety, expression,
adverse events, phenotypes, ontology, …) unsurfaced. These tools expose those
fields so questions that previously fell through to `web_search` get answered
from curated Open Targets data instead.

Each tool takes a free-text name (the fetcher resolves it internally — no
`map_ids` pre-step) and returns a compact JSON string `{"ok": bool, ...}`, the
same return contract as `opentargets_graphql_tool` / `web_search`, so the
orchestrator's existing "read the JSON and summarize" handling applies unchanged.
"""
import asyncio
import json
import logging

from agents import function_tool

from .target_data import get_target_biological_info, get_target_baseline_expression
from .drug_data import get_drug_enriched_info
from .disease_data import get_disease_enriched_info

logger = logging.getLogger("uvicorn.error").getChild("opentargets.annotation")


def _slice(info: dict, keys: list[str], list_max: int = 15) -> str:
    """Slice the requested keys, capping long list fields so the JSON the agent
    must read stays bounded (the full firehose — e.g. 119 tissue rows — chokes
    the synthesizer). Truncated fields are noted under '_truncated'."""
    out = {"ok": True}
    truncated = {}
    for k in keys:
        v = info.get(k)
        if isinstance(v, list):
            # Authoritative total: prefer a sibling `{k}_count` (computed by the data
            # layer BEFORE its own pre-cap) so we never report the already-capped
            # length as the total. Generic — no per-field hardcoding.
            cnt = info.get(f"{k}_count")
            total = cnt if isinstance(cnt, int) and cnt >= len(v) else len(v)
            if total > list_max:
                truncated[k] = total
                v = v[:list_max]
        out[k] = v
    if truncated:
        out["_truncated"] = {k: f"showing top {list_max} of {n}" for k, n in truncated.items()}
    return json.dumps(out, default=str)


@function_tool(
    strict_mode=False,
    name_override="target_annotation_tool",
    description_override=(
        "Open Targets functional/safety annotation for ONE target (gene/protein). "
        "Use for: is X druggable / tractability, baseline tissue expression "
        "(GTEx/HPA), safety liabilities, subcellular location, mouse knockout "
        "phenotypes, chemical probes, protein interactions, genetic constraint, "
        "cancer hallmarks, DepMap essentiality, pharmacogenomics (target-level), "
        "gene ontology (GO) terms, target "
        "prioritisation scores, homologues/orthologs/paralogs, TEP, UniProt "
        "accession + AlphaFold structure link. Pass the gene "
        "symbol or Ensembl ID. Returns JSON. NOT for target<->disease/drug associations (use target_tool)."
    ),
)
async def target_annotation_tool(target: str) -> str:
    try:
        info, expr_df = await asyncio.gather(
            get_target_biological_info(target),
            get_target_baseline_expression(target, max_rows=200),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.warning("[target_annotation_tool] failed for %r: %s", target, exc)
        return json.dumps({"ok": False, "error": "target annotation lookup failed"})

    if isinstance(info, Exception):
        logger.warning("[target_annotation_tool] bio_info failed for %r: %s", target, info)
        return json.dumps({"ok": False, "error": "target annotation lookup failed"})
    if not info:
        return json.dumps({"ok": False, "error": f"target not found: {target}"})

    # Replace the deprecated `expressions` field with live baselineExpression data.
    if not isinstance(expr_df, Exception) and expr_df is not None and not expr_df.empty:
        # Convert top rows to compact dicts for the orchestrator to summarize.
        tissue_rows = expr_df.head(50).to_dict(orient="records")
        info["tissue_expressions"] = tissue_rows
        info["tissue_expressions_count"] = len(expr_df)
        logger.info("[target_annotation_tool] tissue_expressions: %d rows (total %d) for %r",
                    len(tissue_rows), len(expr_df), target)
    else:
        if isinstance(expr_df, Exception):
            logger.warning("[target_annotation_tool] baselineExpression failed for %r: %s",
                           target, expr_df)
        info["tissue_expressions"] = []
        info["tissue_expressions_count"] = 0

    # Link-complete pointers: UniProt accession (already fetched in protein_ids) +
    # derived AlphaFold structure link (AlphaFold DB is keyed by the UniProt
    # accession). The full structure/protein data lives in BioChirp's
    # alphafold / pdbe / uniprot DBs — these are just pointers, not a copy.
    # uniprot_accession is computed in get_target_biological_info from the FULL
    # proteinIds list (before truncation), so it is canonical even when the
    # swissprot entry sits past the protein_ids preview cap.
    _uniprot = info.get("uniprot_accession")
    info["alphafold_url"] = (f"https://alphafold.ebi.ac.uk/entry/{_uniprot}"
                             if _uniprot else None)
    return _slice(info, [
        "ensembl_id", "approved_symbol", "approved_name", "tractability",
        "tissue_expressions", "safety_liabilities", "subcellular_locations",
        "mouse_phenotypes", "high_quality_probes", "all_chemical_probes",
        "interactions_top", "genetic_constraint", "cancer_hallmarks",
        "depmap_essentiality", "pharmacogenomics",
        # gene ontology, target-prioritisation scores, homology, TEP
        "go_biological_process", "go_molecular_function", "go_cellular_component",
        "prioritisation", "human_paralogs", "mouse_orthologs", "tep",
        # structure/protein pointers (full data in alphafold/pdbe/uniprot DBs)
        "uniprot_accession", "alphafold_url",
    ])


@function_tool(
    strict_mode=False,
    name_override="drug_safety_tool",
    description_override=(
        "Open Targets safety/pharmacology annotation for ONE drug. Use for: "
        "adverse events / side effects (FAERS pharmacovigilance), black-box / "
        "withdrawn / drug warnings, pharmacogenomics. Pass the drug name or "
        "ChEMBL ID. Returns JSON. NOT for indications / 'what does X treat' "
        "(use drug_tool)."
    ),
)
async def drug_safety_tool(drug: str) -> str:
    try:
        info = await get_drug_enriched_info(drug)
    except Exception as exc:
        logger.warning("[drug_safety_tool] failed for %r: %s", drug, exc)
        return json.dumps({"ok": False, "error": "drug safety lookup failed"})
    if not info:
        return json.dumps({"ok": False, "error": f"drug not found: {drug}"})
    return _slice(info, [
        "drug_id", "drug_name", "description", "drug_warnings",
        "top_adverse_events", "adverse_events_critical_value",
        "pharmacogenomics", "mechanisms_of_action",
    ])


@function_tool(
    strict_mode=False,
    name_override="drug_profile_tool",
    description_override=(
        "Open Targets identity/metadata for ONE drug. Use for: trade/brand names, "
        "drug type (small molecule / antibody / etc.), maximum clinical stage, "
        "synonyms, cross-references (DrugBank/ChEBI/PubChem), parent & child "
        "molecules (salts/derivatives), similar drugs. Pass the drug name or "
        "ChEMBL ID. Returns JSON. NOT for 'what does X treat' / indications / "
        "targets (use drug_tool); NOT for side effects / warnings (use drug_safety_tool)."
    ),
)
async def drug_profile_tool(drug: str) -> str:
    try:
        info = await get_drug_enriched_info(drug)
    except Exception as exc:
        logger.warning("[drug_profile_tool] failed for %r: %s", drug, exc)
        return json.dumps({"ok": False, "error": "drug profile lookup failed"})
    if not info:
        return json.dumps({"ok": False, "error": f"drug not found: {drug}"})
    return _slice(info, [
        "drug_id", "drug_name", "description", "drug_type",
        "maximum_clinical_stage", "trade_names", "synonyms",
        "cross_references", "parent_molecule", "child_molecules",
        "similar_drugs",
    ])


@function_tool(
    strict_mode=False,
    name_override="disease_profile_tool",
    description_override=(
        "Open Targets clinical/ontology profile for ONE disease. Use for: "
        "symptoms / clinical phenotypes (HPO), subtypes / parent disease "
        "(ontology hierarchy incl. ancestors), affected anatomical locations, OTAR "
        "research projects, similar diseases, cross-references (DOID/ICD/UMLS/MeSH). "
        "Pass the disease name, EFO or MONDO ID. "
        "Returns JSON. NOT for 'genes/drugs for disease X' associations (use disease_tool)."
    ),
)
async def disease_profile_tool(disease: str) -> str:
    try:
        info = await get_disease_enriched_info(disease)
    except Exception as exc:
        logger.warning("[disease_profile_tool] failed for %r: %s", disease, exc)
        return json.dumps({"ok": False, "error": "disease profile lookup failed"})
    if not info:
        return json.dumps({"ok": False, "error": f"disease not found: {disease}"})
    return _slice(info, [
        "description", "phenotypes_hpo", "parents", "children_subtypes",
        "direct_locations", "indirect_locations", "therapeutic_areas",
        "synonyms", "otar_projects", "similar_diseases",
        "resolved_ancestors", "db_xrefs",
    ])
