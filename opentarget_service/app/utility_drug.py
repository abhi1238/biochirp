


"""
Drug tool for fetching drug-related data with ontology-aware filtering
WITH structured execution logging, semantic filtering, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
from .drug_data import get_drug_master
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_synonyms,
    get_drug_description,
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
logger = base_logger.getChild("opentargets.drug")

SERVICE_NAME = "drug_tool"
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
# DRUG TOOL
# ==============================================================================
@function_tool(
    name_override="drug_tool",
    description_override=(
        "Fetch known diseases and target associations for a resolved drug "
        "with ontology-aware filtering and execution trace logging."
    ),
)
async def drug_tool(
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
        logger.info("[DRUG TOOL] ========== STARTING DRUG TOOL ==========")
        logger.info("[DRUG TOOL] connection_id: %s", connection_id)
        logger.info(f"[DRUG TOOL] [input.resolved_entities]: {input.resolved_entities}")
        logger.info(f"[DRUG TOOL] [input.resolved_entities] type: {type(input.resolved_entities)}")

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
        
        # Effective filter types (exclude pathway and drug from filtering logic)
        effective_filter_types = {
            e.type.lower()
            for e in input.resolved_entities
            if e.id and e.type and e.type.lower() not in {"pathway", "drug"}
        }

        # Extract surface forms
        disease_names = extract_surface_forms(explicit, "disease")
        target_names = extract_surface_forms(explicit, "target")
        drug_name_list = extract_surface_forms(explicit, "drug")
        pathway_names = extract_surface_forms(explicit, "pathway")
        mechanism_names = extract_surface_forms(explicit, "mechanism_of_action")

        logger.info("[DRUG TOOL][ENTITY] Diseases (%d): %s", len(disease_names), disease_names)
        logger.info("[DRUG TOOL][ENTITY] Targets (%d): %s", len(target_names), target_names)
        logger.info("[DRUG TOOL][ENTITY] Drugs (%d): %s", len(drug_name_list), drug_name_list)
        logger.info("[DRUG TOOL][ENTITY] Pathways (%d): %s", len(pathway_names), pathway_names)
        logger.info("[DRUG TOOL][ENTITY] Mechanisms (%d): %s", len(mechanism_names), mechanism_names)
        logger.info(f"[DRUG TOOL] Present types: {present_types}")
        logger.info(f"[DRUG TOOL] Effective filter types (excl. pathway/drug): {effective_filter_types}")

        if not drugs:
            logger.error("[DRUG TOOL] No resolved drug found")
            return TableOutput(
                status="error",
                raw_query=input.query,
                message="No resolved drug found.",
                table={},
                csv_path=None,
                row_count=0,
                tool=SERVICE_NAME,
                database="OpenTargets",
            )

        drug_name = drugs[0].surface_form
        logger.info(f"[DRUG TOOL] Primary drug: {drug_name}")
        
        description = await get_drug_description(drug_name)
        logger.info(f"[DRUG TOOL] Description retrieved: {description[:100] if description else None}...")

        # ------------------------------------------------------------------
        # ASSOCIATION RETRIEVAL (only if filters beyond pathway/drug exist)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0:
            logger.info(f"[DRUG TOOL] Fetching associations for drug: {drug_name}")
            
            df = await get_drug_master(drug_name, how="left")
            
            exec_log.add(
                step="association_retrieval",
                action="Retrieved base drug associations",
                after=len(df),
                details={"drug": drug_name},
            )

            if df.empty:
                logger.warning(f"[DRUG TOOL] No associations found for {drug_name}")
                return TableOutput(
                    status="success",
                    raw_query=input.query,
                    message="No associations found for this drug.",
                    table={},
                    csv_path=None,
                    row_count=0,
                    tool=SERVICE_NAME,
                    database="OpenTargets",
                    description=description,
                )

            logger.info(f"[DRUG TOOL] Retrieved {len(df)} base associations")

            # ------------------------------------------------------------------
            # NORMALIZATION
            # ------------------------------------------------------------------
            # df.columns = [c.lower().replace(" ", "_") for c in df.columns]
            
            # for col in ["disease_name", "drug_name", "mechanism_of_action", "target_name"]:
            #     if col in df.columns:
            #         df[col] = df[col].fillna("").astype(str).str.lower().str.strip()
            
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


            logger.info(f"[DRUG TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DISEASE FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if diseases and "disease_name" in df.columns:
                logger.info(f"[DRUG TOOL] Applying disease filter for: {disease_names}")
                
                universe_lc = {u.lower() for u in df["disease_name"] if u}
                expanded_terms: Set[str] = set()

                for dname in disease_names:
                    logger.debug(f"[DRUG TOOL] Expanding disease ontology for: {dname}")
                    try:
                        bundle = await get_disease_and_descendant_synonyms(dname)
                        expanded = [t.lower().strip() for t in bundle.get("combined", [])]
                        expanded_terms.update(expanded)
                        logger.info(f"[DRUG TOOL] Expanded '{dname}' to {len(expanded)} terms")
                    except Exception as e:
                        logger.warning(f"[DRUG TOOL] Disease expansion failed for '{dname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and disease_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in disease_names} & universe_lc
                    )
                logger.info(
                    f"[DRUG TOOL] Disease overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )

                before = len(df)

                overlap_lc = set(overlapping_terms)

                mask = (
                    df["disease_name"].notna()
                    & df["disease_name"].str.lower().isin(overlap_lc)
                ) | df["disease_name"].isna() | (df["disease_name"] == "")

                df = df[mask]
                
                
                # df = df[
                #     df["disease_name"].isin(overlapping_terms)
                #     | (df["disease_name"] == "")
                #     | df["disease_name"].isna()
                # ]
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
                logger.info(f"[DRUG TOOL] Disease filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # TARGET FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if targets and "gene_name" in df.columns:
                logger.info(f"[DRUG TOOL] Applying target filter for: {target_names}")
                
                universe_lc = {u.lower() for u in df["gene_name"] if u}
                expanded_terms: Set[str] = set()

                for tname in target_names:
                    logger.debug(f"[DRUG TOOL] Expanding target synonyms for: {tname}")
                    try:
                        bundle = await get_target_synonyms(tname)
                        if isinstance(bundle, dict) and 'error' not in bundle:
                            expanded = [t.lower().strip() for t in bundle.get("combined", [])]
                            expanded_terms.update(expanded)
                            logger.info(f"[DRUG TOOL] Expanded '{tname}' to {len(expanded)} target terms")
                    except Exception as e:
                        logger.warning(f"[DRUG TOOL] Target expansion failed for '{tname}': {e}")

                overlapping_terms = sorted(expanded_terms & universe_lc)
                if not overlapping_terms and target_names:
                    overlapping_terms = sorted(
                        {t.lower().strip() for t in target_names} & universe_lc
                    )
                logger.info(
                    f"[DRUG TOOL] Target overlapping terms ({len(overlapping_terms)}): "
                    f"{overlapping_terms[:10]}{'...' if len(overlapping_terms) > 10 else ''}"
                )

                before = len(df)

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
                logger.info(f"[DRUG TOOL] Target filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # MECHANISM FILTER (LOG MATCHED TERMS)
            # ------------------------------------------------------------------
            if mechanism_names and "mechanism_of_action" in df.columns:
                logger.info(f"[DRUG TOOL] Applying mechanism filter for: {mechanism_names}")
                
                matched_terms: Set[str] = set()

                for moa_term in mechanism_names:
                    logger.info(f"[DRUG TOOL] [MOA] Processing: {moa_term}")
                    selected = await member_selection(
                        entity_type="mechanism_of_action",
                        entity_name=moa_term,
                        tool=SERVICE_NAME,
                        data=df
                    )
                    matched_terms.update(selected)

                logger.info(
                    f"[DRUG TOOL] Mechanism matched terms ({len(matched_terms)}): "
                    f"{sorted(list(matched_terms))[:10]}"
                )

                before = len(df)
                # df = df[df["mechanism_of_action"].isin(matched_terms)]

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
                logger.info(f"[DRUG TOOL] Mechanism filter: {before} → {after} rows")

            # ------------------------------------------------------------------
            # COLUMN PRUNING
            # ------------------------------------------------------------------
            columns_to_drop = []
            
            if "disease" not in present_types:
                columns_to_drop.extend([
                    "disease_id", "disease_name", "phase", "status"
                ])

            if "target" not in present_types:
                columns_to_drop.extend([
                    "gene_id", "gene_name"
                ])

            columns_to_drop = [c for c in columns_to_drop if c in df.columns]
            if columns_to_drop:
                logger.info(f"[DRUG TOOL] Dropping columns: {columns_to_drop}")
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
                logger.info(f"[DRUG TOOL] Deduplication: {before} → {after} rows")

            # Sort by phase (higher = more advanced)
            if 'phase' in df.columns:
                df['phase_numeric'] = pd.to_numeric(df['phase'], errors='coerce').fillna(-1)
                df = df.sort_values(by='phase_numeric', ascending=False)
                df = df.drop(columns=['phase_numeric'])
                logger.info(f"[DRUG TOOL] Sorted by phase")

            df = df.reset_index(drop=True)
            final_row_count = len(df)
            logger.info(f"[DRUG TOOL] Final associations result: {final_row_count} rows")

            # ------------------------------------------------------------------
            # PREVIEW + SAVE ASSOCIATIONS
            # ------------------------------------------------------------------
            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="drug_name"
            )

            if connection_id:
                csv_path = _csv_path("drug_tool_associations")
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

        # Get drug synonyms
        try:
            synonym_list = await get_drug_synonyms(drug_name)
            if not isinstance(synonym_list, list):
                synonym_list = []
            logger.info(f"[DRUG TOOL] Retrieved {len(synonym_list)} synonyms")
        except Exception as e:
            logger.warning(f"[DRUG TOOL] Failed to get drug synonyms: {e}")
            synonym_list = []

        # Generate execution trace explanation
        try:
            logger.info("[DRUG TOOL] Generating execution trace explanation")
            # final_message= "Success"
            execution_trace_explanation = await Runner.run(
                ExecutionTraceExplainer,
                str(exec_log)
            )
            final_message = execution_trace_explanation.final_output
            logger.info(f"[DRUG TOOL] Execution trace generated: {final_message[:100]}...")
        except Exception as e:
            logger.warning(f"[DRUG TOOL] Execution trace generation failed: {e}")
            message_parts = [f"Retrieved {final_row_count} associations for drug {drug_name}"]
            
            if diseases:
                message_parts.append(f"filtered by {len(diseases)} disease(s)")
            if targets:
                message_parts.append(f"filtered by {len(targets)} target(s)")
            
            final_message = ". ".join(message_parts) + "."

        logger.info("[DRUG TOOL] ========== TOOL COMPLETE ==========")
        logger.info(f"[DRUG TOOL] Final row count: {final_row_count}")
        logger.info(f"[DRUG TOOL] CSV path: {csv_path}")

        logger.debug("[DRUG TOOL] final_message: %s", final_message)

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
        logger.exception("[DRUG TOOL] ========== FATAL ERROR ==========")
        return TableOutput(
            status="error",
            raw_query=input.query,
            message=f"Drug tool failed: {str(e)}",
            table=preview_rows,
            csv_path=None,
            row_count=0,
            tool=SERVICE_NAME,
            database="OpenTargets",
            description=description,
            synonym=synonym_list if len(synonym_list) > 0 else [],
        )
