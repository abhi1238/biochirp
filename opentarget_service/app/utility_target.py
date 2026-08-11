"""
Target tool for fetching target-related data with ontology-aware filtering
WITH structured execution logging, pathway routing, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import asyncio
import uuid
import logging
import pandas as pd
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
from .target_data import get_target_associations_no_pathways, get_target_baseline_expression, get_target_biological_info, get_target_diseases_all, get_target_drugs_all
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_description,
    get_target_synonyms,
    get_gene_pathways_df,
)
from .utility import df_to_llm_safe_hierarchy
from .fuzzy_search import fuzzy_filter_choices_multi_scorer
import json
from .member_selector import member_selection
from .generate_log import ToolExecutionLog
from .utility_shared import (
    _safe, _csv_path, RESULTS_ROOT, MAX_PREVIEW_ROWS, MAX_ASSOC_ROWS,
    extract_surface_forms, is_explicit_entity, save_and_publish_csv,
    apply_ontology_filter,
)
from .resolvers import get_query_intent_hints, get_requested_output

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")

SERVICE_NAME = "target_tool"


# ------------------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------------------
def recover_explicit_pathways(entities) -> List[str]:
    """Recover pathway names from entities without type (fallback)."""
    has_pathway_request = any(
        e.type == "pathway" and e.id == "requested"
        for e in entities
    )

    if not has_pathway_request:
        return []

    return [
        e.surface_form
        for e in entities
        if e.surface_form and e.type is None
    ]


# ==============================================================================
# TARGET TOOL
# ==============================================================================
@function_tool(
    strict_mode=False,
    name_override="target_tool",
    description_override=(
        "Fetch disease associations, drug interactions, and pathways for a resolved target "
        "with ontology-aware filtering and execution trace logging."
    ),
)
async def target_tool(
    input: QueryResolution,
    connection_id: Optional[str] = None
) -> TableOutput:

    exec_log = ToolExecutionLog()
    csv_path = None
    preview_rows = {}
    preview_row_count = 0
    is_truncated = False
    description = None
    synonym_list = []
    final_row_count = 0
    df = None
    pathway_df = None
    bio_info: dict = {}

    try:
        logger.info("[TARGET TOOL] ========== STARTING TARGET TOOL ==========")
        logger.info("[TARGET TOOL] connection_id: %s", connection_id)
        logger.info(f"[TARGET TOOL] [input.resolved_entities]: {input.resolved_entities}")
        logger.info(f"[TARGET TOOL] [input.resolved_entities] type: {type(input.resolved_entities)}")

        # Debug each entity
        for e in input.resolved_entities:
            logger.info(
                "DEBUG ENTITY | type=%s id=%r (%s) surface=%r bool(id)=%s resolution_method=%s",
                e.type,
                e.id,
                type(e.id),
                e.surface_form,
                bool(e.id),
                getattr(e, "resolution_method", None),
            )

        # ------------------------------------------------------------------
        # ENTITY EXTRACTION
        # ------------------------------------------------------------------
        explicit = [e for e in input.resolved_entities if is_explicit_entity(e)]

        diseases = [e for e in explicit if e.type and e.type.lower() == "disease"]
        targets = [e for e in explicit if e.type and e.type.lower() == "target"]
        drugs = [e for e in explicit if e.type and e.type.lower() == "drug"]
        pathways = [e for e in input.resolved_entities if e.type and e.type.lower() == "pathway"]
        moa = [e for e in input.resolved_entities if e.type and e.type.lower() == "mechanism_of_action"]

        present_types = {e.type.lower() for e in input.resolved_entities if e.type}

        # Effective filter types (exclude pathway and target from filtering logic)
        effective_filter_types = {
            e.type.lower()
            for e in input.resolved_entities
            if e.id and e.type and e.type.lower() not in {"pathway", "target"}
        }

        # Extract surface forms
        disease_names = extract_surface_forms(explicit, "disease")
        target_name_list = extract_surface_forms(explicit, "target")
        drug_names = extract_surface_forms(explicit, "drug")
        pathway_names = extract_surface_forms(explicit, "pathway")

        # Fallback pathway recovery
        if not pathway_names:
            pathway_names = recover_explicit_pathways(input.resolved_entities)

        # Intent fallback: recover pathway intent from either the keyword-detected
        # hint (set_query_intent_hints in main.py) OR the NER model's cached
        # requested_output (set by interpreter).  Using the NER cache as a second
        # path means the model's output is authoritative even when the orchestrator
        # strips implicit-request entities before calling the tool.
        _pathway_from_hint = "pathway" in get_query_intent_hints(connection_id or "")
        _pathway_from_ner = get_requested_output(connection_id or "") == "pathway"
        if not pathways and (_pathway_from_hint or _pathway_from_ner):
            from .guard_rail import ResolvedEntity
            pathways = [ResolvedEntity(surface_form=None, type="pathway", id="requested", resolution_method="implicit_request")]
            if "pathway" not in present_types:
                present_types = present_types | {"pathway"}
            logger.info(
                "[TARGET TOOL] Pathway intent recovered (hint=%s, ner_cache=%s)",
                _pathway_from_hint, _pathway_from_ner,
            )

        mechanism_names = extract_surface_forms(explicit, "mechanism_of_action")

        logger.info("[TARGET TOOL][ENTITY] Diseases (%d): %s", len(disease_names), disease_names)
        logger.info("[TARGET TOOL][ENTITY] Targets (%d): %s", len(target_name_list), target_name_list)
        logger.info("[TARGET TOOL][ENTITY] Drugs (%d): %s", len(drug_names), drug_names)
        logger.info("[TARGET TOOL][ENTITY] Pathways (%d): %s", len(pathway_names), pathway_names)
        logger.info("[TARGET TOOL][ENTITY] Mechanisms (%d): %s", len(mechanism_names), mechanism_names)
        logger.info(f"[TARGET TOOL] Present types: {present_types}")
        logger.info(f"[TARGET TOOL] Effective filter types (excl. pathway/target): {effective_filter_types}")

        # When only the target anchor reaches the tool (orchestrator may strip
        # "requested" entities), default to disease associations — UNLESS the
        # caller explicitly requested drugs. requested_output is stored in the
        # connection cache (not in QueryResolution JSON) to avoid orchestrator confusion.
        _requested_output = getattr(input, "requested_output", None) or get_requested_output(connection_id)
        logger.info(f"[TARGET TOOL] requested_output={_requested_output!r} (from input or cache)")

        # Detect expression / GWAS intent early so we can bypass the default
        # disease-association branch and return the correct data type instead.
        _query_lower = input.query.lower()
        _intent_hints = get_query_intent_hints(connection_id or "")
        _requested_expression = (
            _requested_output == "expression"
            or "expression" in _intent_hints   # set from "expressed"/"tissue"/"organ" tokens in resolvers.py
            or any(kw in _query_lower for kw in (
                # "express" alone omitted — catches "expression evidence" (association queries)
                "where is", "where are",
                "synthesize", "synthesise",
                "mrna level", "rna level",
                " tissue", " tissues",    # leading space avoids matching "endotissue" etc.
            ))
        )
        _requested_gwas = (
            _requested_output == "gwas"
            or "gwas" in _intent_hints
            or any(kw in _query_lower for kw in (
                "gwas", "credible set", "credible-set", "snp", "variant association",
                "study locus", "colocali",
            ))
        )
        logger.info(
            "[TARGET TOOL] expression_intent=%s gwas_intent=%s",
            _requested_expression, _requested_gwas,
        )

        target_only_default = (
            len(effective_filter_types) == 0
            and present_types.issubset({"target", "pathway"})
            and "pathway" not in present_types
            and _requested_output != "drug"
            and not _requested_expression
            and not _requested_gwas
        )

        if not targets:
            logger.error("[TARGET TOOL] No resolved target found")
            return TableOutput(
                status="error",
                raw_query=input.query,
                message="No resolved target found.",
                table={},
                csv_path=None,
                row_count=0,
                tool=SERVICE_NAME,
                database="OpenTargets",
            )

        target_name = targets[0].surface_form
        logger.info(f"[TARGET TOOL] Primary target: {target_name}")

        # ------------------------------------------------------------------
        # DIRECT DISEASES ONLY (target + disease requested, no explicit disease/drug filters)
        # ------------------------------------------------------------------
        requested_disease_only = (
            "disease" in present_types
            and "drug" not in present_types
            and "pathway" not in present_types
            and "mechanism_of_action" not in present_types
            and not diseases
            and not drugs
            and not pathway_names
            and not mechanism_names
        )

        if requested_disease_only or target_only_default:
            logger.info(f"[TARGET TOOL] Direct disease-only query for target: {target_name}")
            # Run annotation + disease fetch concurrently
            (description, bio_info), df = await asyncio.gather(
                asyncio.gather(
                    get_target_description(target_name),
                    get_target_biological_info(target_name),
                ),
                get_target_diseases_all(target_name, max_rows=MAX_ASSOC_ROWS),
            )
            logger.info(f"[TARGET TOOL] Description retrieved: {description[:100] if description else None}...")
            logger.info(f"[TARGET TOOL] Bio info keys: {list(bio_info.keys())}")

            if df.empty:
                logger.warning(f"[TARGET TOOL] No diseases found for {target_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No diseases found for this target.",
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

            # Normalize string columns; fill numeric score columns with 0
            for col in df.columns:
                if df[col].dtype == "object":
                    df[col] = df[col].fillna("").astype(str).str.strip()
                elif pd.api.types.is_float_dtype(df[col]):
                    df[col] = df[col].fillna(0.0)

            # Sort by association score
            if "association_score" in df.columns:
                df["association_score_numeric"] = pd.to_numeric(
                    df["association_score"], errors="coerce"
                ).fillna(-1)
                df = df.sort_values(by="association_score_numeric", ascending=False)
                df = df.drop(columns=["association_score_numeric"])

            df = df.drop_duplicates().reset_index(drop=True)
            final_row_count = len(df)
            logger.info(f"[TARGET TOOL] Direct diseases result: {final_row_count} rows")
            preview_row_count = min(MAX_PREVIEW_ROWS, final_row_count)
            is_truncated = final_row_count > preview_row_count

            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="gene_symbol"
            )

            csv_path = await save_and_publish_csv(
                df, connection_id, "target_tool_diseases", "Diseases", SERVICE_NAME, final_row_count
            )

            # Build top-25 hint so the LLM reads correct rank order from the
            # message text, not from the cardinality-ordered hierarchy JSON.
            _top5_cols = [c for c in ("disease_name", "association_score", "score_genetic_association") if c in df.columns]
            _top5 = df.head(25)[_top5_cols].to_dict("records")
            _top5_str = "; ".join(
                f"{r.get('disease_name','?')} (score={r.get('association_score',0):.4f}"
                + (f", genetic={r['score_genetic_association']:.4f}" if "score_genetic_association" in r and r.get("score_genetic_association") else "")
                + ")"
                for r in _top5
            )
            _msg = (
                f"Retrieved {final_row_count} diseases for target {target_name}. "
                f"SYNTHESIZER-HINT — TOP DISEASE IS: {_top5[0].get('disease_name', '?') if _top5 else '?'}"
                f" (score={float(_top5[0].get('association_score', 0)) if _top5 else 0:.4f}). "
                f"Full top-{min(25, final_row_count)} by association_score (pre-sorted DESC — row 0 is the strongest): {_top5_str}."
                " For list/which-diseases questions, enumerate ALL diseases shown above (not just the top-ranked one)."
            )
            # Second ranking by score_genetic_association (for mutation/causation questions).
            if "score_genetic_association" in df.columns:
                _gdf = df[df["score_genetic_association"] > 0].nlargest(25, "score_genetic_association")
                if not _gdf.empty:
                    _gcols = [c for c in ("disease_name", "score_genetic_association") if c in _gdf.columns]
                    _g5_str = "; ".join(
                        f"{r.get('disease_name','?')} (genetic={r.get('score_genetic_association',0):.4f})"
                        for r in _gdf[_gcols].to_dict("records")
                    )
                    _msg += f" Top-{min(25, len(_gdf))} by score_genetic_association (use THIS list for mutation/causation/list questions): {_g5_str}."

            return TableOutput(
                status="success",
                raw_query=input.query,
                message=_msg,
                table=preview_rows,
                csv_path=csv_path,
                row_count=final_row_count,
                preview_row_count=preview_row_count,
                is_truncated=is_truncated,
                tool=SERVICE_NAME,
                database="OpenTargets",
                description=description,
                metadata=bio_info if bio_info else None,
            )

        # ------------------------------------------------------------------
        # TISSUE EXPRESSION BRANCH
        # Skip the heavyweight get_target_biological_info call (TARGET_INFO_QUERY
        # fetches hundreds of fields). Fetch only description + baseline expression
        # concurrently so the query stays within the WS timeout.
        # ------------------------------------------------------------------
        if _requested_expression:
            logger.info("[TARGET TOOL] Expression branch triggered — fetching baselineExpression")
            try:
                description, tdf = await asyncio.gather(
                    get_target_description(target_name),
                    get_target_baseline_expression(target_name, max_rows=200),
                )
            except Exception as _expr_err:
                logger.warning("[TARGET TOOL] baselineExpression fetch failed: %s", _expr_err)
                tdf = pd.DataFrame()
                if description is None:
                    try:
                        description = await get_target_description(target_name)
                    except Exception:
                        description = None

            if not tdf.empty:
                tdf = tdf.drop_duplicates()
                expr_row_count = len(tdf)
                expr_preview_count = min(MAX_PREVIEW_ROWS, expr_row_count)
                expr_preview = df_to_llm_safe_hierarchy(
                    tdf.head(MAX_PREVIEW_ROWS), root_col="gene_symbol"
                )
                expr_csv = await save_and_publish_csv(
                    tdf, connection_id, "target_tool_expression",
                    "Expression", SERVICE_NAME, expr_row_count,
                )
                logger.info("[TARGET TOOL] Expression: %d records returned", expr_row_count)
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message=(
                        f"Baseline tissue expression for {target_name}: "
                        f"{expr_row_count} records across tissues/cell-types "
                        f"(sorted by distribution_score, highest first). "
                        f"Sources: GTEx, Tabula Sapiens, proteomics and other datasources."
                    ),
                    table=expr_preview,
                    csv_path=expr_csv,
                    row_count=expr_row_count,
                    preview_row_count=expr_preview_count,
                    is_truncated=expr_row_count > MAX_PREVIEW_ROWS,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )
            else:
                logger.info("[TARGET TOOL] Expression: no baseline data in OT for %s", target_name)
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message=(
                        f"No baseline tissue expression data available for {target_name} "
                        "in OpenTargets (baselineExpression returned 0 records)."
                    ),
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

        # ------------------------------------------------------------------
        # GWAS / CREDIBLE SETS BRANCH
        # Fetch description + bio_info together; credible_sets live in
        # TARGET_INFO_QUERY so we need bio_info here (one combined call).
        # ------------------------------------------------------------------
        if _requested_gwas:
            logger.info("[TARGET TOOL] GWAS/credible-sets branch triggered")
            description, bio_info = await asyncio.gather(
                get_target_description(target_name),
                get_target_biological_info(target_name),
            )
            csets = bio_info.get("credible_sets") or []
            if csets:
                gdf = pd.DataFrame(csets)
                gdf.insert(0, "gene_symbol", target_name)
                gdf = gdf.drop_duplicates().reset_index(drop=True)
                gwas_row_count = len(gdf)
                gwas_preview_count = min(MAX_PREVIEW_ROWS, gwas_row_count)
                gwas_preview = df_to_llm_safe_hierarchy(
                    gdf.head(MAX_PREVIEW_ROWS), root_col="gene_symbol"
                )
                gwas_csv = await save_and_publish_csv(
                    gdf, connection_id, "target_tool_gwas",
                    "GWAS", SERVICE_NAME, gwas_row_count,
                )
                total_credible = bio_info.get("credible_sets_count") or gwas_row_count
                logger.info("[TARGET TOOL] GWAS: %d credible sets (total=%s)", gwas_row_count, total_credible)
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message=(
                        f"GWAS credible sets colocalising with {target_name}: "
                        f"{gwas_row_count} shown (OT total={total_credible})."
                    ),
                    table=gwas_preview,
                    csv_path=gwas_csv,
                    row_count=gwas_row_count,
                    preview_row_count=gwas_preview_count,
                    is_truncated=gwas_row_count > MAX_PREVIEW_ROWS,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                    metadata=bio_info if bio_info else None,
                )
            else:
                logger.info("[TARGET TOOL] GWAS: no credible sets in bio_info for %s", target_name)
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message=(
                        f"No GWAS credible sets found colocalising with {target_name} "
                        "in OpenTargets."
                    ),
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                    metadata=bio_info if bio_info else None,
                )

        # For all remaining branches, fetch full annotation now
        description, bio_info = await asyncio.gather(
            get_target_description(target_name),
            get_target_biological_info(target_name),
        )
        logger.info(f"[TARGET TOOL] Description retrieved: {description[:100] if description else None}...")
        logger.info(f"[TARGET TOOL] Bio info keys: {list(bio_info.keys())}")

        # ------------------------------------------------------------------
        # DIRECT DRUGS ONLY (target + drug requested, no explicit disease filter)
        # ------------------------------------------------------------------
        requested_drug_only = (
            ("drug" in present_types or _requested_output == "drug")
            and "disease" not in present_types
            and "pathway" not in present_types
            and "mechanism_of_action" not in present_types
            and not diseases
            and not drugs
            and not pathway_names
            and not mechanism_names
        )

        if requested_drug_only:
            logger.info(f"[TARGET TOOL] Direct drug-only query for target: {target_name}")
            df = await get_target_drugs_all(target_name)

            if df.empty:
                logger.warning(f"[TARGET TOOL] No drugs found for {target_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No drugs found for this target.",
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

            df = df.drop_duplicates().reset_index(drop=True)
            final_row_count = len(df)
            logger.info(f"[TARGET TOOL] Direct drugs result: {final_row_count} rows")
            preview_row_count = min(MAX_PREVIEW_ROWS, final_row_count)
            is_truncated = final_row_count > preview_row_count

            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="gene_symbol"
            )

            csv_path = await save_and_publish_csv(
                df, connection_id, "target_tool_drugs", "Drugs", SERVICE_NAME, final_row_count
            )

            return TableOutput(
                status="success",
                raw_query=input.query,
                message=f"Retrieved {final_row_count} drugs for target {target_name}.",
                table=preview_rows,
                csv_path=csv_path,
                row_count=final_row_count,
                preview_row_count=preview_row_count,
                is_truncated=is_truncated,
                tool=SERVICE_NAME,
                database="OpenTargets",
                description=description,
                metadata=bio_info if bio_info else None,
            )

        # ------------------------------------------------------------------
        # ASSOCIATION RETRIEVAL (only if filters beyond pathway/target exist)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0:
            logger.info(f"[TARGET TOOL] Fetching associations for target: {target_name}")

            # get_target_associations_no_pathways is drug-centric: it builds rows
            # by iterating over drug rows and returns empty if the target has no
            # approved drugs. For disease-filtered queries without a drug filter
            # (e.g. "Is TREM2 associated with Alzheimer's?"), that means targets
            # like TREM2/TRIM37/FGFR3 (research targets with no approved drugs)
            # always return 0 rows even when OT has strong associations. Fix:
            # route disease-only filtered queries through get_target_diseases_all
            # (which fetches direct disease-association rows) and apply the disease
            # filter downstream, exactly as the requested_disease_only path does.
            _disease_filter_only = (
                effective_filter_types == {"disease"}
                and not drugs
                and not mechanism_names
            )

            if _disease_filter_only:
                logger.info("[TARGET TOOL] Disease-filtered query with no drug filter — using get_target_diseases_all")
                df = await get_target_diseases_all(target_name, max_rows=MAX_ASSOC_ROWS)
            else:
                df = await get_target_associations_no_pathways(target_name)

            exec_log.add(
                step="association_retrieval",
                action="Retrieved base target associations",
                after=len(df),
                details={"target": target_name},
            )

            if df.empty:
                logger.warning(f"[TARGET TOOL] No associations found for {target_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No associations found for this target.",
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

            logger.info(f"[TARGET TOOL] Retrieved {len(df)} base associations")

            # ------------------------------------------------------------------
            # NORMALIZATION
            # ------------------------------------------------------------------
            # df.columns = [c.lower().replace(" ", "_") for c in df.columns]

            # ------------------------------------------------------------------
            # SAFE NORMALIZATION (NO LOWER-CASING)
            # ------------------------------------------------------------------
            df.columns = [c.lower().replace(" ", "_") for c in df.columns]

            ID_COL_SUFFIX = "_id"
            NAME_COL_SUFFIX = "_name"

            for col in df.columns:
                if col.endswith(ID_COL_SUFFIX):
                    continue

                if col.endswith(NAME_COL_SUFFIX):
                    continue

                if df[col].dtype == "object":
                    df[col] = df[col].fillna("").astype(str).str.strip()
                elif pd.api.types.is_float_dtype(df[col]):
                    df[col] = df[col].fillna(0.0)

            # for col in ["disease_name", "drug_name", "mechanism_of_action", "target_name"]:
            #     if col in df.columns:
            #         df[col] = df[col].fillna("").astype(str).str.lower().str.strip()

            logger.info(f"[TARGET TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DISEASE FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if diseases and "disease_name" in df.columns:
                logger.info(f"[TARGET TOOL] Applying disease filter for: {disease_names}")

                async def _disease_exp(n):
                    b = await get_disease_and_descendant_synonyms(n)
                    return b.get("combined", []) if isinstance(b, dict) else []

                df = await apply_ontology_filter(
                    df,
                    col="disease_name",
                    input_names=disease_names,
                    expander=_disease_exp,
                    exec_log=exec_log,
                    step="disease_filter",
                    action="Applied disease ontology filtering",
                    detail_key="input_diseases",
                    log_prefix="[TARGET TOOL]",
                )

            # ------------------------------------------------------------------
            # DRUG FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if drugs and "drug_name" in df.columns:
                logger.info(f"[TARGET TOOL] Applying drug filter for: {drug_names}")

                async def _drug_exp(n):
                    b = await get_drug_synonyms(n)
                    return b if isinstance(b, list) else []

                df = await apply_ontology_filter(
                    df,
                    col="drug_name",
                    input_names=drug_names,
                    expander=_drug_exp,
                    exec_log=exec_log,
                    step="drug_filter",
                    action="Applied drug synonym filtering",
                    detail_key="input_drugs",
                    log_prefix="[TARGET TOOL]",
                    expand_noun="drug terms",
                )

            # ------------------------------------------------------------------
            # MECHANISM FILTER (LOG MATCHED TERMS)
            # ------------------------------------------------------------------
            if mechanism_names and "mechanism_of_action" in df.columns:
                logger.info(f"[TARGET TOOL] Applying mechanism filter for: {mechanism_names}")

                matched_terms: Set[str] = set()

                for moa_term in mechanism_names:
                    logger.info(f"[TARGET TOOL] [MOA] Processing: {moa_term}")
                    selected = await member_selection(
                        entity_type="mechanism_of_action",
                        entity_name=moa_term,
                        tool=SERVICE_NAME,
                        data=df
                    )
                    matched_terms.update(selected)

                logger.info(
                    f"[TARGET TOOL] Mechanism matched terms ({len(matched_terms)}): "
                    f"{sorted(list(matched_terms))[:10]}"
                )

                before = len(df)
                overlap_lc = None
                # df = df[df["mechanism_of_action"].isin(matched_terms)]
                overlap_lc = {t.lower() for t in matched_terms}

                mask = (
                    df["mechanism_of_action"].notna()
                    & df["mechanism_of_action"].str.lower().isin(overlap_lc)
                ) | df["mechanism_of_action"].isna() | (df["mechanism_of_action"] == "")

                df = df[mask]

                after = len(df)

                exec_log.add(
                    step="mechanism_filter",
                    action="Filtered by mechanism of action",
                    before=before,
                    after=after,
                    details={
                        "input_mechanisms": mechanism_names,
                        "matched_terms": sorted(list(matched_terms))[:10],
                        "matched_terms_count": len(matched_terms),
                    },
                )
                logger.info(f"[TARGET TOOL] Mechanism filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # COLUMN PRUNING
            # ------------------------------------------------------------------
            columns_to_drop = []

            if "drug" not in present_types:
                columns_to_drop.extend([
                    "drug_id", "drug_name", "phase", "status",
                    "action_types", "mechanism_of_action"
                ])

            if "disease" not in present_types:
                columns_to_drop.extend([
                    "disease_id", "disease_name", "association_score"
                ])

            columns_to_drop = [c for c in columns_to_drop if c in df.columns]
            if columns_to_drop:
                logger.info(f"[TARGET TOOL] Dropping columns: {columns_to_drop}")
                df = df.drop(columns=columns_to_drop)

            # ------------------------------------------------------------------
            # FINAL CLEANUP
            # ------------------------------------------------------------------
            before = len(df)
            df = df.drop_duplicates()
            after = len(df)

            if before != after:
                exec_log.add(
                    step="deduplication",
                    action="Removed duplicate rows",
                    before=before,
                    after=after,
                )
                logger.info(f"[TARGET TOOL] Deduplication: {before} → {after} rows")

            # Sort by association score
            if 'association_score' in df.columns:
                df['association_score_numeric'] = pd.to_numeric(
                    df['association_score'], errors='coerce'
                ).fillna(-1)
                df = df.sort_values(by='association_score_numeric', ascending=False)
                df = df.drop(columns=['association_score_numeric'])
                logger.info(f"[TARGET TOOL] Sorted by association_score")

            df = df.reset_index(drop=True)

            # Drop columns where every value is NaN / empty string / None
            df = df.replace("", pd.NA).dropna(axis=1, how="all")
            df = df.dropna(axis=0, how="all")
            df = df.reset_index(drop=True)

            final_row_count = len(df)
            logger.info(f"[TARGET TOOL] Final associations result: {final_row_count} rows")

            # ------------------------------------------------------------------
            # PREVIEW + SAVE ASSOCIATIONS
            # ------------------------------------------------------------------
            # Guard: df may be empty after filtering (all rows removed by
            # disease/drug/mechanism filters). df_to_llm_safe_hierarchy raises
            # ValueError if root_col is absent (happens when dropna removed all
            # columns from an all-NaN frame). Return a clean success/zero-row
            # response instead of crashing so the orchestrator can surface
            # "no results found" rather than a stack trace.
            if df.empty or "gene_symbol" not in df.columns:
                logger.warning(
                    "[TARGET TOOL] Empty DataFrame (or missing gene_symbol) after filtering; "
                    "returning zero-row success."
                )
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message=(
                        f"No matching associations found for target {target_name} "
                        "with the specified filters."
                    ),
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                    metadata=bio_info if bio_info else None,
                )

            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="gene_symbol"
            )

            csv_path = await save_and_publish_csv(
                df, connection_id, "target_tool_associations", "Associations", SERVICE_NAME, final_row_count
            )

        # ------------------------------------------------------------------
        # PATHWAY RETRIEVAL (if pathway in present_types)
        # ------------------------------------------------------------------
        if "pathway" in present_types:
            try:
                logger.info(f"[TARGET TOOL] ========== PATHWAY RETRIEVAL ==========")
                logger.info(f"[TARGET TOOL] Fetching pathway data for: {target_name}")

                # 30s per-call budget prevents slow OT pathway endpoints from
                # exhausting the overall 120s WS timeout on the client side.
                pathway_df = await asyncio.wait_for(
                    get_gene_pathways_df(target_name), timeout=30.0
                )

                if not pathway_df.empty:
                    logger.info(f"[TARGET TOOL] Retrieved {len(pathway_df)} pathways")

                    # Normalize pathway DataFrame
                    pathway_df.columns = [c.lower().replace(" ", "_") for c in pathway_df.columns]
                    pathway_df = pathway_df.drop_duplicates().reset_index(drop=True)

                    exec_log.add(
                        step="pathway_retrieval",
                        action="Retrieved pathways for target",
                        after=len(pathway_df),
                        details={"target": target_name},
                    )

                    # Filter pathways if specific pathway names requested
                    if pathway_names:
                        logger.info(f"[TARGET TOOL] Filtering pathways by: {pathway_names}")

                        final_pathway_set: Set[str] = set()

                        for p in pathway_names:
                            selected = await member_selection(
                                entity_type="pathway_name",
                                entity_name=p,
                                tool=SERVICE_NAME,
                                data=pathway_df
                            )
                            final_pathway_set.update(selected)

                        final_pathway_list = list(final_pathway_set)
                        logger.info(
                            f"[TARGET TOOL] Final selected pathways ({len(final_pathway_list)}): "
                            f"{final_pathway_list}"
                        )

                        before_pathway = len(pathway_df)
                        # pathway_df = pathway_df[
                        #     pathway_df["pathway_name"].str.lower().isin(final_pathway_list)
                        # ]

                        overlap_lc = None

                        overlap_lc = {t.lower() for t in final_pathway_list}

                        mask = (
                            pathway_df["pathway_name"].notna()
                            & pathway_df["pathway_name"].str.lower().isin(overlap_lc)
                        ) | pathway_df["pathway_name"].isna() | (pathway_df["pathway_name"] == "")

                        pathway_df = pathway_df[mask]



                        after_pathway = len(pathway_df)

                        exec_log.add(
                            step="pathway_filter",
                            action="Filtered pathways by name",
                            before=before_pathway,
                            after=after_pathway,
                            details={
                                "input_pathways": pathway_names,
                                "matched_pathways": final_pathway_list[:10],
                                "matched_pathways_count": len(final_pathway_list),
                            },
                        )
                        logger.info(f"[TARGET TOOL] Pathway filter: {before_pathway} → {after_pathway} rows")

                    # Update final_row_count for pathway-only queries
                    if len(effective_filter_types) == 0:
                        final_row_count = len(pathway_df)
                        logger.info(f"[TARGET TOOL] Pathway-only query: final_row_count = {final_row_count}")

                    # Save pathway data
                    pathway_csv_path = await save_and_publish_csv(
                        pathway_df, connection_id, "target_tool_pathways", "Pathway", SERVICE_NAME, len(pathway_df)
                    )
                    csv_path = pathway_csv_path

                    # Add pathway data to preview
                    pathway_preview = df_to_llm_safe_hierarchy(
                        df=pathway_df.head(MAX_PREVIEW_ROWS),
                        root_col="gene_name"
                    )

                    # Merge pathway data into preview
                    try:
                        if isinstance(preview_rows, dict) and isinstance(pathway_preview, dict):
                            if target_name in preview_rows and target_name in pathway_preview:
                                if "pathway_name" in pathway_preview[target_name]:
                                    preview_rows[target_name]["pathways"] = pathway_preview[target_name]["pathway_name"]
                            else:
                                preview_rows["pathways"] = pathway_preview
                        logger.info(f"[TARGET TOOL] Merged pathway preview into response")
                    except Exception as e:
                        logger.warning(f"[TARGET TOOL] Could not merge pathway preview: {e}")

                else:
                    logger.info(f"[TARGET TOOL] No pathway data found for: {target_name}")

            except Exception as e:
                logger.warning(f"[TARGET TOOL] Pathway fetch failed: {e}", exc_info=True)

        # Get target synonyms
        try:
            bundle = await get_target_synonyms(target_name)
            if isinstance(bundle, dict) and 'error' not in bundle:
                synonym_list = bundle.get("combined", [])
            elif isinstance(bundle, list):
                synonym_list = bundle
            else:
                synonym_list = []
            logger.info(f"[TARGET TOOL] Retrieved {len(synonym_list)} synonyms")
        except Exception as e:
            logger.warning(f"[TARGET TOOL] Failed to get target synonyms: {e}")
            synonym_list = []

        # Build result message from structured data fields (no LLM call needed)
        if pathway_df is not None and not pathway_df.empty and len(effective_filter_types) == 0:
            message_parts = [f"Retrieved {len(pathway_df)} pathways for target {target_name}"]
            if pathway_names:
                message_parts.append(f"filtered by {len(pathway_names)} pathway name(s)")
            final_message = ". ".join(message_parts) + "."
        else:
            message_parts = [f"Retrieved {final_row_count} associations for target {target_name}"]
            if disease_names:
                message_parts.append(f"filtered by {len(disease_names)} disease(s)")
            if drug_names:
                message_parts.append(f"filtered by {len(drug_names)} drug(s)")
            if pathway_df is not None and not pathway_df.empty:
                message_parts.append(f"and {len(pathway_df)} pathways")
            # Surface per-datasource evidence breakdown when associations are present.
            # These columns are always fetched but not always surfaced in prose.
            if df is not None and not df.empty:
                _evidence_cols = [
                    "genetic_association", "somatic_mutation", "drugs",
                    "affected_pathway", "literature", "animal_model",
                    "rna_expression", "known_variant",
                ]
                _present_evidence = [c for c in _evidence_cols if c in df.columns]
                if _present_evidence:
                    message_parts.append(
                        f"Evidence breakdown columns available: {', '.join(_present_evidence)}"
                    )
            # Surface AlphaFold IDs from transcript data when present.
            if bio_info:
                _af_ids = [
                    t.get("alphafold_id") for t in (bio_info.get("transcripts") or [])
                    if t.get("alphafold_id")
                ]
                if _af_ids:
                    message_parts.append(f"AlphaFold ID: {_af_ids[0]}")
            final_message = ". ".join(message_parts) + "."

        logger.info("[TARGET TOOL] ========== TOOL COMPLETE ==========")
        logger.info(f"[TARGET TOOL] Final row count: {final_row_count}")
        logger.info(f"[TARGET TOOL] CSV path: {csv_path}")
        logger.info(f"[TARGET TOOL] Final message: {final_message}")

        logger.debug("[TARGET TOOL] final_message: %s", final_message)


        # message = None
        # table=None

        return TableOutput(
            status="success",
            raw_query=input.query,
            message=final_message,
            table=preview_rows,
            csv_path=csv_path,
            row_count=final_row_count,
            preview_row_count=min(MAX_PREVIEW_ROWS, final_row_count),
            is_truncated=final_row_count > MAX_PREVIEW_ROWS,
            tool=SERVICE_NAME,
            database="OpenTargets",
            description=description,
            synonym=synonym_list if len(synonym_list) > 0 else [],
            metadata=bio_info if bio_info else None,
            filter_trace=exec_log.to_filter_trace(),
        )

    except Exception as e:
        logger.exception("[TARGET TOOL] ========== FATAL ERROR ==========")
        return TableOutput(
            status="error",
            raw_query=input.query,
            message=f"Target tool failed: {str(e)}",
            table=preview_rows,
            csv_path=None,
            row_count=0,
            tool=SERVICE_NAME,
            database="OpenTargets",
            description=description,
            synonym=synonym_list if len(synonym_list) > 0 else [],
            metadata=bio_info if bio_info else None,
        )
