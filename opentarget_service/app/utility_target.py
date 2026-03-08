"""
Target tool for fetching target-related data with ontology-aware filtering
WITH structured execution logging, pathway routing, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
import redis.asyncio as redis
from agents import Agent, Runner, function_tool, WebSearchTool
from .guard_rail import TableOutput, QueryResolution
from .target_data import get_target_associations_no_pathways, get_target_diseases_all
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_description,
    get_target_synonyms,
    get_gene_pathways_df,
)
from .utility import df_to_llm_safe_hierarchy
from .fuzzy_search import fuzzy_filter_choices_multi_scorer
from .trace_explainer import ExecutionTraceExplainer
import json
from .member_selector import member_selection
from .redis import _get_redis, _publish_ws
from .generate_log import ToolExecutionLog

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")

SERVICE_NAME = "target_tool"
RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")
MAX_PREVIEW_ROWS = int(os.environ.get("OT_PREVIEW_ROWS", "50"))


# ------------------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------------------
def _safe(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum() or c in ("-_"))


def _csv_path(prefix: str, suffix: str = "") -> str:
    suffix = _safe(suffix) or uuid.uuid4().hex
    path = os.path.join(RESULTS_ROOT, f"{prefix}_{suffix}.csv")
    logger.info(f"[csv path]: {path}")
    return path


def extract_surface_forms(entities, entity_type: str) -> List[str]:
    return [e.surface_form for e in entities if e.type == entity_type and e.surface_form]


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


def is_explicit_entity(e) -> bool:
    return (
        e.surface_form is not None
        and e.type is not None
        and e.id != "requested"
        and getattr(e, "resolution_method", None) != "implicit_request"
    )


# ==============================================================================
# TARGET TOOL
# ==============================================================================
@function_tool(
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
        
        mechanism_names = extract_surface_forms(explicit, "mechanism_of_action")

        logger.info("[TARGET TOOL][ENTITY] Diseases (%d): %s", len(disease_names), disease_names)
        logger.info("[TARGET TOOL][ENTITY] Targets (%d): %s", len(target_name_list), target_name_list)
        logger.info("[TARGET TOOL][ENTITY] Drugs (%d): %s", len(drug_names), drug_names)
        logger.info("[TARGET TOOL][ENTITY] Pathways (%d): %s", len(pathway_names), pathway_names)
        logger.info("[TARGET TOOL][ENTITY] Mechanisms (%d): %s", len(mechanism_names), mechanism_names)
        logger.info(f"[TARGET TOOL] Present types: {present_types}")
        logger.info(f"[TARGET TOOL] Effective filter types (excl. pathway/target): {effective_filter_types}")

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
        
        description = await get_target_description(target_name)
        logger.info(f"[TARGET TOOL] Description retrieved: {description[:100] if description else None}...")

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

        if requested_disease_only:
            logger.info(f"[TARGET TOOL] Direct disease-only query for target: {target_name}")
            df = await get_target_diseases_all(target_name)

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

            # Normalize string columns
            for col in df.columns:
                if df[col].dtype == "object":
                    df[col] = df[col].fillna("").astype(str).str.strip()

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
                df.head(MAX_PREVIEW_ROWS), root_col="gene_name"
            )

            if connection_id:
                csv_path = _csv_path("target_tool_diseases")
                try:
                    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                    df.to_csv(csv_path, index=False)
                    logger.info(
                        "[%s function] Diseases CSV saved: %s (%d rows)",
                        SERVICE_NAME,
                        csv_path,
                        df.shape[0],
                    )
                    await _publish_ws(
                        connection_id,
                        csv_path,
                        final_row_count,
                        service_name=SERVICE_NAME,
                    )
                except Exception as e:
                    logger.error(
                        "[%s function] Diseases CSV write failed: %s",
                        SERVICE_NAME,
                        e,
                        exc_info=True
                    )
                    csv_path = None

            return TableOutput(
                status="success",
                raw_query=input.query,
                message=f"Retrieved {final_row_count} diseases for target {target_name}.",
                table=preview_rows,
                csv_path=csv_path,
                row_count=final_row_count,
                preview_row_count=preview_row_count,
                is_truncated=is_truncated,
                tool=SERVICE_NAME,
                database="OpenTargets",
                description=description,
            )

        # ------------------------------------------------------------------
        # ASSOCIATION RETRIEVAL (only if filters beyond pathway/target exist)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0:
            logger.info(f"[TARGET TOOL] Fetching associations for target: {target_name}")
            
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
            
            # for col in ["disease_name", "drug_name", "mechanism_of_action", "target_name"]:
            #     if col in df.columns:
            #         df[col] = df[col].fillna("").astype(str).str.lower().str.strip()
            
            logger.info(f"[TARGET TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DISEASE FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if diseases and "disease_name" in df.columns:
                logger.info(f"[TARGET TOOL] Applying disease filter for: {disease_names}")
                
                universe_lc = {u.lower() for u in df["disease_name"] if u}
                expanded_terms: Set[str] = set()

                for dname in disease_names:
                    logger.debug(f"[TARGET TOOL] Expanding disease ontology for: {dname}")
                    try:
                        bundle = await get_disease_and_descendant_synonyms(dname)
                        expanded = [t.lower().strip() for t in bundle.get("combined", [])]
                        expanded_terms.update(expanded)
                        logger.info(f"[TARGET TOOL] Expanded '{dname}' to {len(expanded)} terms")
                    except Exception as e:
                        logger.warning(f"[TARGET TOOL] Disease expansion failed for '{dname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and disease_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in disease_names} & universe_lc
                    )
                logger.info(
                    f"[TARGET TOOL] Disease overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )

                before = len(df)
                # df = df[
                #     df["disease_name"].isin(overlapping_terms)
                #     | (df["disease_name"] == "")
                #     | df["disease_name"].isna()
                # ]

                overlap_lc = set(overlapping_terms)

                mask = (
                    df["disease_name"].notna()
                    & df["disease_name"].str.lower().isin(overlap_lc)
                ) | df["disease_name"].isna() | (df["disease_name"] == "")

                df = df[mask]
                
                after = len(df)

                exec_log.add(
                    step="disease_filter",
                    action="Applied disease ontology filtering",
                    before=before,
                    after=after,
                    details={
                        "input_diseases": disease_names,
                        "expanded_terms_used": overlapping_terms[:10],
                        "expanded_terms_used_count": len(overlapping_terms),
                    },
                )
                logger.info(f"[TARGET TOOL] Disease filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # DRUG FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if drugs and "drug_name" in df.columns:
                logger.info(f"[TARGET TOOL] Applying drug filter for: {drug_names}")
                
                universe_lc = {u.lower() for u in df["drug_name"] if u}
                expanded_terms: Set[str] = set()

                for dname in drug_names:
                    logger.debug(f"[TARGET TOOL] Expanding drug synonyms for: {dname}")
                    try:
                        bundle = await get_drug_synonyms(dname)
                        if isinstance(bundle, list):
                            expanded = [t.lower().strip() for t in bundle]
                            expanded_terms.update(expanded)
                            logger.info(f"[TARGET TOOL] Expanded '{dname}' to {len(expanded)} drug terms")
                    except Exception as e:
                        logger.warning(f"[TARGET TOOL] Drug expansion failed for '{dname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and drug_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in drug_names} & universe_lc
                    )
                logger.info(
                    f"[TARGET TOOL] Drug overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )

                before = len(df)
                # df = df[
                #     df["drug_name"].isin(overlapping_terms)
                #     | (df["drug_name"] == "")
                #     | df["drug_name"].isna()
                # ]

                overlap_lc = set(overlapping_terms)

                mask = (
                    df["drug_name"].notna()
                    & df["drug_name"].str.lower().isin(overlap_lc)
                ) | df["drug_name"].isna() | (df["drug_name"] == "")

                df = df[mask]
                
                after = len(df)

                exec_log.add(
                    step="drug_filter",
                    action="Applied drug synonym filtering",
                    before=before,
                    after=after,
                    details={
                        "input_drugs": drug_names,
                        "expanded_terms_used": overlapping_terms[:10],
                        "expanded_terms_used_count": len(overlapping_terms),
                    },
                )
                logger.info(f"[TARGET TOOL] Drug filter: {before} → {after} rows")

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
            final_row_count = len(df)
            logger.info(f"[TARGET TOOL] Final associations result: {final_row_count} rows")

            # ------------------------------------------------------------------
            # PREVIEW + SAVE ASSOCIATIONS
            # ------------------------------------------------------------------
            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="gene_name"
            )

            if connection_id:
                csv_path = _csv_path("target_tool_associations")
                try:
                    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                    df.to_csv(csv_path, index=False)
                    logger.info(
                        "[%s function] Associations CSV saved: %s (%d rows)",
                        SERVICE_NAME,
                        csv_path,
                        df.shape[0],
                    )
                    await _publish_ws(
                        connection_id,
                        csv_path,
                        final_row_count,
                        service_name=SERVICE_NAME,
                    )
                except Exception as e:
                    logger.error(
                        "[%s function] Associations CSV write failed: %s",
                        SERVICE_NAME,
                        e,
                        exc_info=True
                    )
                    csv_path = None

        # ------------------------------------------------------------------
        # PATHWAY RETRIEVAL (if pathway in present_types)
        # ------------------------------------------------------------------
        if "pathway" in present_types:
            try:
                logger.info(f"[TARGET TOOL] ========== PATHWAY RETRIEVAL ==========")
                logger.info(f"[TARGET TOOL] Fetching pathway data for: {target_name}")
                
                pathway_df = await get_gene_pathways_df(target_name)

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
                            df["pathway_name"].notna()
                            & df["pathway_name"].str.lower().isin(overlap_lc)
                        ) | df["pathway_name"].isna() | (df["pathway_name"] == "")

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
                    if connection_id:
                        pathway_csv_path = _csv_path("target_tool_pathways")
                        try:
                            os.makedirs(os.path.dirname(pathway_csv_path), exist_ok=True)
                            pathway_df.to_csv(pathway_csv_path, index=False)
                            logger.info(
                                "[%s function] Pathway CSV saved: %s (%d rows)",
                                SERVICE_NAME,
                                pathway_csv_path,
                                pathway_df.shape[0],
                            )
                            await _publish_ws(
                                connection_id,
                                pathway_csv_path,
                                len(pathway_df),
                                service_name=SERVICE_NAME,
                            )
                        except Exception as e:
                            logger.error(
                                "[%s function] Pathway CSV write failed: %s",
                                SERVICE_NAME,
                                e,
                                exc_info=True
                            )

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

        # Generate execution trace explanation
        try:
            logger.info("[TARGET TOOL] Generating execution trace explanation")
            # final_message= "Success"
            execution_trace_explanation = await Runner.run(
                ExecutionTraceExplainer,
                str(exec_log)
            )
            final_message = execution_trace_explanation.final_output
            logger.info(f"[TARGET TOOL] Execution trace generated: {final_message[:100]}...")
        except Exception as e:
            logger.warning(f"[TARGET TOOL] Execution trace generation failed: {e}")
            
            # FIXED: Pathway-aware fallback message generation
            if pathway_df is not None and not pathway_df.empty and len(effective_filter_types) == 0:
                # PATHWAY-ONLY QUERY
                message_parts = [f"Retrieved {len(pathway_df)} pathways for target {target_name}"]
                if pathway_names:
                    message_parts.append(f"filtered by {len(pathway_names)} pathway name(s)")
                final_message = ". ".join(message_parts) + "."
            else:
                # ASSOCIATION QUERY or MIXED QUERY
                message_parts = [f"Retrieved {final_row_count} associations for target {target_name}"]
                
                if disease_names:
                    message_parts.append(f"filtered by {len(disease_names)} disease(s)")
                if drug_names:
                    message_parts.append(f"filtered by {len(drug_names)} drug(s)")
                if pathway_df is not None and not pathway_df.empty:
                    message_parts.append(f"and {len(pathway_df)} pathways")
                
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
        )
