



"""
Disease tool for fetching disease-related data with ontology-aware filtering
WITH structured execution logging, semantic filtering, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
from .disease_data import get_disease_combined_knowledge, get_targets_for_disease_all
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_synonyms,
    get_disease_description,
)
from .utility import df_to_llm_safe_hierarchy
from .member_selector import member_selection
from .trace_explainer import ExecutionTraceExplainer
from .generate_log import ToolExecutionLog
from .redis import _get_redis, _publish_ws

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.disease")

SERVICE_NAME = "disease_tool"
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


def is_explicit_entity(e) -> bool:
    return (
        e.surface_form is not None
        and e.type is not None
        and e.id != "requested"
        and getattr(e, "resolution_method", None) != "implicit_request"
    )


# ==============================================================================
# DISEASE TOOL
# ==============================================================================
@function_tool(
    name_override="disease_tool",
    description_override=(
        "Fetch known drugs and target associations for a resolved disease "
        "with ontology-aware filtering and execution trace logging."
    ),
)
async def disease_tool(
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

    try:
        logger.info("[DISEASE TOOL] ========== STARTING DISEASE TOOL ==========")
        logger.info("[DISEASE TOOL] connection_id: %s", connection_id)
        logger.info(f"[DISEASE TOOL] [input.resolved_entities]: {input.resolved_entities}")
        logger.info(f"[DISEASE TOOL] [input.resolved_entities] type: {type(input.resolved_entities)}")

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
        
        # Effective filter types (exclude pathway and disease from filtering logic)
        effective_filter_types = {
            e.type.lower()
            for e in input.resolved_entities
            if e.id and e.type and e.type.lower() not in {"pathway", "disease"}
        }

        # Extract surface forms
        disease_name_list = extract_surface_forms(explicit, "disease")
        target_names = extract_surface_forms(explicit, "target")
        drug_names = extract_surface_forms(explicit, "drug")
        pathway_names = extract_surface_forms(explicit, "pathway")
        mechanism_names = extract_surface_forms(explicit, "mechanism_of_action")

        logger.info("[DISEASE TOOL][ENTITY] Diseases (%d): %s", len(disease_name_list), disease_name_list)
        logger.info("[DISEASE TOOL][ENTITY] Targets (%d): %s", len(target_names), target_names)
        logger.info("[DISEASE TOOL][ENTITY] Drugs (%d): %s", len(drug_names), drug_names)
        logger.info("[DISEASE TOOL][ENTITY] Pathways (%d): %s", len(pathway_names), pathway_names)
        logger.info("[DISEASE TOOL][ENTITY] Mechanisms (%d): %s", len(mechanism_names), mechanism_names)
        logger.info(f"[DISEASE TOOL] Present types: {present_types}")
        logger.info(f"[DISEASE TOOL] Effective filter types (excl. pathway/disease): {effective_filter_types}")

        if not diseases:
            logger.error("[DISEASE TOOL] No resolved disease found")
            return TableOutput(
                status="error",
                raw_query=input.query,
                message="No resolved disease found.",
                table={},
                csv_path=None,
                row_count=0,
                tool=SERVICE_NAME,
                database="OpenTargets",
            )

        disease_name = diseases[0].surface_form
        logger.info(f"[DISEASE TOOL] Primary disease: {disease_name}")
        
        description = await get_disease_description(disease_name)
        logger.info(f"[DISEASE TOOL] Description retrieved: {description[:100] if description else None}...")

        # ------------------------------------------------------------------
        # DIRECT TARGETS ONLY (disease + target requested, no explicit target/drug filters)
        # ------------------------------------------------------------------
        requested_target_only = (
            "target" in present_types
            and "drug" not in present_types
            and "pathway" not in present_types
            and "mechanism_of_action" not in present_types
            and not targets
            and not drugs
            and not pathway_names
            and not mechanism_names
        )

        if requested_target_only:
            logger.info(f"[DISEASE TOOL] Direct target-only query for disease: {disease_name}")
            df = await get_targets_for_disease_all(disease_name)

            if df.empty:
                logger.warning(f"[DISEASE TOOL] No targets found for {disease_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No targets found for this disease.",
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
            logger.info(f"[DISEASE TOOL] Direct targets result: {final_row_count} rows")
            preview_row_count = min(MAX_PREVIEW_ROWS, final_row_count)
            is_truncated = final_row_count > preview_row_count

            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="disease_name"
            )

            if connection_id:
                csv_path = _csv_path("disease_tool_targets")
                try:
                    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                    df.to_csv(csv_path, index=False)
                    logger.info(
                        "[%s function] Targets CSV saved: %s (%d rows)",
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
                        "[%s function] Targets CSV write failed: %s",
                        SERVICE_NAME,
                        e,
                        exc_info=True
                    )
                    csv_path = None

            return TableOutput(
                status="success",
                raw_query=input.query,
                message=f"Retrieved {final_row_count} targets for disease {disease_name}.",
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
        # ASSOCIATION RETRIEVAL (only if filters beyond pathway/disease exist)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0:
            logger.info(f"[DISEASE TOOL] Fetching associations for disease: {disease_name}")
            
            df = await get_disease_combined_knowledge(disease_name)
            
            exec_log.add(
                step="association_retrieval",
                action="Retrieved base disease associations",
                after=len(df),
                details={"disease": disease_name},
            )

            if df.empty:
                logger.warning(f"[DISEASE TOOL] No associations found for {disease_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No associations found for this disease.",
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

            logger.info(f"[DISEASE TOOL] Retrieved {len(df)} base associations")

            # ------------------------------------------------------------------
            # NORMALIZATION
            # ------------------------------------------------------------------
            ID_COL_SUFFIX = "_id"
            NAME_COL_SUFFIX = "_name"

            for col in df.columns:
                if col.endswith(ID_COL_SUFFIX):
                    continue
                if col.endswith(NAME_COL_SUFFIX):
                    continue

                if df[col].dtype == "object":
                    df[col] = df[col].fillna("").astype(str).str.strip()

            
            logger.info(f"[DISEASE TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DRUG FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if drugs and "drug_name" in df.columns:
                logger.info(f"[DISEASE TOOL] Applying drug filter for: {drug_names}")
                
                universe_lc = {u.lower() for u in df["drug_name"] if u}
                expanded_terms: Set[str] = set()

                for dname in drug_names:
                    logger.debug(f"[DISEASE TOOL] Expanding drug synonyms for: {dname}")
                    try:
                        bundle = await get_drug_synonyms(dname)
                        if isinstance(bundle, list):
                            expanded = [t.lower().strip() for t in bundle]
                            expanded_terms.update(expanded)
                            logger.info(f"[DISEASE TOOL] Expanded '{dname}' to {len(expanded)} drug terms")
                    except Exception as e:
                        logger.warning(f"[DISEASE TOOL] Drug expansion failed for '{dname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and drug_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in drug_names} & universe_lc
                    )
                logger.info(
                    f"[DISEASE TOOL] Drug overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )


                before = len(df)


                # df = df[
                #     df["drug_name"].isin(overlapping_terms)
                #     | (df["drug_name"] == "")
                #     | df["drug_name"].isna()
                # ]

                # build a lowercase lookup ONLY for comparison
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
                logger.info(f"[DISEASE TOOL] Drug filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # TARGET FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if targets and "gene_name" in df.columns:
                logger.info(f"[DISEASE TOOL] Applying target filter for: {target_names}")
                
                universe_lc = {u.lower() for u in df["gene_name"] if u}
                expanded_terms: Set[str] = set()

                for tname in target_names:
                    logger.debug(f"[DISEASE TOOL] Expanding target synonyms for: {tname}")
                    try:
                        bundle = await get_target_synonyms(tname)
                        if isinstance(bundle, dict) and 'error' not in bundle:
                            expanded = [t.lower().strip() for t in bundle.get("combined", [])]
                            expanded_terms.update(expanded)
                            logger.info(f"[DISEASE TOOL] Expanded '{tname}' to {len(expanded)} target terms")
                    except Exception as e:
                        logger.warning(f"[DISEASE TOOL] Target expansion failed for '{tname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and target_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in target_names} & universe_lc
                    )
                logger.info(
                    f"[DISEASE TOOL] Target overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )

                before = len(df)

                # build a lowercase lookup ONLY for comparison
                overlap_lc = set(overlapping_terms)

                mask = (
                    df["gene_name"].notna()
                    & df["gene_name"].str.lower().isin(overlap_lc)
                ) | df["gene_name"].isna() | (df["gene_name"] == "")

                df = df[mask]
                # df = df[
                #     df["target_name"].isin(overlapping_terms)
                #     | (df["target_name"] == "")
                #     | df["target_name"].isna()
                # ]
                after = len(df)

                exec_log.add(
                    step="target_filter",
                    action="Applied target synonym filtering",
                    before=before,
                    after=after,
                    details={
                        "input_targets": target_names,
                        "expanded_terms_used": overlapping_terms[:10],
                        "expanded_terms_used_count": len(overlapping_terms),
                    },
                )
                logger.info(f"[DISEASE TOOL] Target filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # MECHANISM FILTER (LOG MATCHED TERMS)
            # ------------------------------------------------------------------
            if mechanism_names and "mechanism_of_action" in df.columns:
                logger.info(f"[DISEASE TOOL] Applying mechanism filter for: {mechanism_names}")
                
                matched_terms: Set[str] = set()

                for moa_term in mechanism_names:
                    logger.info(f"[DISEASE TOOL] [MOA] Processing: {moa_term}")
                    selected = await member_selection(
                        entity_type="mechanism_of_action",
                        entity_name=moa_term,
                        tool=SERVICE_NAME,
                        data=df
                    )
                    matched_terms.update(selected)

                logger.info(
                    f"[DISEASE TOOL] Mechanism matched terms ({len(matched_terms)}): "
                    f"{sorted(list(matched_terms))[:10]}"
                )

                before = len(df)
                # df = df[df["mechanism_of_action"].isin(matched_terms)]

                # build a lowercase lookup ONLY for comparison
                overlap_lc = None
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
                logger.info(f"[DISEASE TOOL] Mechanism filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # COLUMN PRUNING
            # ------------------------------------------------------------------
            columns_to_drop = []
            
            if "drug" not in present_types:
                columns_to_drop.extend([
                    "drug_id", "drug_name", "phase", "status",
                    "action_types", "mechanism_of_action", "drug_type"
                ])

            if "target" not in present_types:
                columns_to_drop.extend([
                    "gene_id", "gene_name", "association_score", "mechanism_of_action"
                ])

            columns_to_drop = [c for c in columns_to_drop if c in df.columns]
            if columns_to_drop:
                logger.info(f"[DISEASE TOOL] Dropping columns: {columns_to_drop}")
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

                logger.info(f"[DISEASE TOOL] Deduplication: {before} → {after} rows")

            # Sort by association score
            if 'association_score' in df.columns:
                df['association_score_numeric'] = pd.to_numeric(
                    df['association_score'], errors='coerce'
                ).fillna(-1)
                df = df.sort_values(by='association_score_numeric', ascending=False)
                df = df.drop(columns=['association_score_numeric'])
                logger.info(f"[DISEASE TOOL] Sorted by association_score")
            # Otherwise sort by phase if available
            elif 'phase' in df.columns:
                df['phase_numeric'] = pd.to_numeric(df['phase'], errors='coerce').fillna(-1)
                df = df.sort_values(by='phase_numeric', ascending=False)
                df = df.drop(columns=['phase_numeric'])
                logger.info(f"[DISEASE TOOL] Sorted by phase")

            df = df.reset_index(drop=True)
            final_row_count = len(df)
            logger.info(f"[DISEASE TOOL] Final associations result: {final_row_count} rows")

            # ------------------------------------------------------------------
            # PREVIEW + SAVE ASSOCIATIONS
            # ------------------------------------------------------------------
            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="disease_name"
            )
            preview_row_count = min(MAX_PREVIEW_ROWS, final_row_count)
            is_truncated = final_row_count > preview_row_count

            if connection_id:
                csv_path = _csv_path("disease_tool_associations")
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

        # Get disease synonyms
        try:
            bundle = await get_disease_and_descendant_synonyms(disease_name)
            synonym_list = bundle.get("combined", [])
            logger.info(f"[DISEASE TOOL] Retrieved {len(synonym_list)} synonyms")
        except Exception as e:
            logger.warning(f"[DISEASE TOOL] Failed to get disease synonyms: {e}")
            synonym_list = []

        # Generate execution trace explanation
        try:
            logger.info("[DISEASE TOOL] Generating execution trace explanation")
            # final_message= "Success"
            execution_trace_explanation = await Runner.run(
                ExecutionTraceExplainer,
                str(exec_log)
            )
            final_message = execution_trace_explanation.final_output
            logger.info(f"[DISEASE TOOL] Execution trace generated: {final_message[:100]}...")
        except Exception as e:
            logger.warning(f"[DISEASE TOOL] Execution trace generation failed: {e}")
            message_parts = [f"Retrieved {final_row_count} associations for disease {disease_name}"]
            
            if drugs:
                message_parts.append(f"filtered by {len(drugs)} drug(s)")
            if targets:
                message_parts.append(f"filtered by {len(targets)} target(s)")
            
            final_message = ". ".join(message_parts) + "."

        logger.info("[DISEASE TOOL] ========== TOOL COMPLETE ==========")
        logger.info(f"[DISEASE TOOL] Final row count: {final_row_count}")
        logger.info(f"[DISEASE TOOL] CSV path: {csv_path}")

        logger.debug("[DISEASE TOOL] final_message: %s", final_message)

        return TableOutput(
            status="success",
            raw_query=input.query,
            message=final_message,
            table=preview_rows,
            csv_path=csv_path,
            row_count=final_row_count,
            preview_row_count=preview_row_count,
            is_truncated=is_truncated,
            tool=SERVICE_NAME,
            database="OpenTargets",
            description=description,
            synonym=synonym_list if len(synonym_list) > 0 else [],
        )

    except Exception as e:
        logger.exception("[DISEASE TOOL] ========== FATAL ERROR ==========")
        return TableOutput(
            status="error",
            raw_query=input.query,
            message=f"Disease tool failed: {str(e)}",
            table=preview_rows,
            csv_path=None,
            row_count=0,
            tool=SERVICE_NAME,
            database="OpenTargets",
            description=description,
            synonym=synonym_list if len(synonym_list) > 0 else [],
        )
