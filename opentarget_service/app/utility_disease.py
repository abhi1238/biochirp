



"""
Disease tool for fetching disease-related data with ontology-aware filtering
WITH structured execution logging, semantic filtering, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import asyncio
import logging
import pandas as pd
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
from .disease_data import get_disease_combined_knowledge, get_disease_enriched_info, get_targets_for_disease_all
from .utility_join import _phase_ordinal
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_synonyms,
    get_disease_description,
)
from .utility import df_to_llm_safe_hierarchy
from .member_selector import member_selection
from .generate_log import ToolExecutionLog
from .utility_shared import (
    _safe, _csv_path, RESULTS_ROOT, MAX_PREVIEW_ROWS, MAX_ASSOC_ROWS,
    extract_surface_forms, is_explicit_entity, save_and_publish_csv,
    apply_ontology_filter,
)
from .resolvers import get_requested_output

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.disease")

SERVICE_NAME = "disease_tool"


# ==============================================================================
# DISEASE TOOL
# ==============================================================================
@function_tool(
    strict_mode=False,
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
    disease_meta: dict = {}

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

        # Look up cached requested_output (set by interpreter, excluded from JSON
        # to prevent orchestrator retry loops). This is the only reliable signal
        # for intent when the user asks "which genes?" because those requested_types
        # entities don't appear in resolved_entities and thus don't populate
        # present_types.
        _requested_output = get_requested_output(connection_id)
        logger.info(f"[DISEASE TOOL] requested_output={_requested_output!r} (from cache)")

        # When only the disease anchor reaches the tool (no drug/target/mechanism
        # filter present — which happens when the orchestrator strips
        # "requested" entities before calling the tool), default to fetching
        # the full disease→drug+target association table. Without this, the
        # association block below is skipped entirely and row_count=0 → no
        # table is published to the UI.
        disease_only_default = (
            len(effective_filter_types) == 0
            and present_types.issubset({"disease", "pathway"})
            and _requested_output not in ("target",)
        )

        # Routing guard: if the interpreter set look_up_category to a non-disease
        # anchor (e.g. "target" for a gene query like "diseases of TP53"),
        # disease_tool is the wrong tool.  Return an explicit error so the
        # orchestrator can correct itself rather than returning garbage results.
        _look_up = (input.look_up_category or "").lower()
        if _look_up and _look_up != "disease":
            logger.warning(
                "[DISEASE TOOL] Routing mismatch: look_up_category=%r — "
                "disease_tool should only be called for disease-anchored queries. "
                "Returning error to force orchestrator to use %s_tool.",
                _look_up, _look_up,
            )
            return TableOutput(
                status="error",
                raw_query=input.query,
                message=(
                    f"Routing error: the interpreter identified this as a '{_look_up}' query "
                    f"(look_up_category='{_look_up}'). "
                    f"Call {_look_up}_tool instead of disease_tool."
                ),
                table={},
                csv_path=None,
                row_count=0,
                tool=SERVICE_NAME,
                database="OpenTargets",
            )

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

        description, disease_meta = await asyncio.gather(
            get_disease_description(disease_name),
            get_disease_enriched_info(disease_name),
        )
        logger.info(f"[DISEASE TOOL] Description retrieved: {description[:100] if description else None}...")
        logger.info(f"[DISEASE TOOL] Disease meta keys: {list(disease_meta.keys())}")

        # ------------------------------------------------------------------
        # DIRECT TARGETS ONLY (disease + target requested, no explicit target/drug filters)
        # "target" appears in present_types only when the orchestrator passes a
        # target entity; more commonly the user just asks "which genes?" and the
        # interpreter emits requested_output="target" (cached by connection_id).
        # ------------------------------------------------------------------
        requested_target_only = (
            ("target" in present_types or _requested_output == "target")
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
            df = await get_targets_for_disease_all(disease_name, max_rows=MAX_ASSOC_ROWS)

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
            logger.info(f"[DISEASE TOOL] Direct targets result: {final_row_count} rows")
            preview_row_count = min(MAX_PREVIEW_ROWS, final_row_count)
            is_truncated = final_row_count > preview_row_count

            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="disease_name"
            )

            csv_path = await save_and_publish_csv(
                df, connection_id, "disease_tool_targets", "Targets", SERVICE_NAME, final_row_count
            )

            # Build top-25 hint so the LLM reads correct rank order from the
            # message text, not from the cardinality-ordered hierarchy JSON.
            _top5_cols = [c for c in ("gene_symbol", "association_score", "score_genetic_association") if c in df.columns]
            _top5 = df.head(25)[_top5_cols].to_dict("records")
            _top5_str = "; ".join(
                f"{r.get('gene_symbol','?')} (score={r.get('association_score',0):.4f}"
                + (f", genetic={r['score_genetic_association']:.4f}" if "score_genetic_association" in r and r.get("score_genetic_association") else "")
                + ")"
                for r in _top5
            )
            _msg = (
                f"Retrieved {final_row_count} targets for disease {disease_name}. "
                f"SYNTHESIZER-HINT — TOP GENE IS: {_top5[0].get('gene_symbol', '?') if _top5 else '?'}"
                f" (score={float(_top5[0].get('association_score', 0)) if _top5 else 0:.4f}). "
                f"Full top-{min(25, final_row_count)} by association_score (pre-sorted DESC — row 0 is the strongest): {_top5_str}."
                " For list/which-genes questions, enumerate ALL genes shown above (not just the top-ranked one)."
            )
            # Second ranking by score_genetic_association (for mutation/causation questions).
            if "score_genetic_association" in df.columns:
                _gdf = df[df["score_genetic_association"] > 0].nlargest(25, "score_genetic_association")
                if not _gdf.empty:
                    _gcols = [c for c in ("gene_symbol", "score_genetic_association") if c in _gdf.columns]
                    _g5_str = "; ".join(
                        f"{r.get('gene_symbol','?')} (genetic={r.get('score_genetic_association',0):.4f})"
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
                metadata=disease_meta if disease_meta else None,
            )

        # ------------------------------------------------------------------
        # ASSOCIATION RETRIEVAL (filters beyond pathway/disease, OR default
        # path when only the disease anchor was passed)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0 or disease_only_default:
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
                elif pd.api.types.is_float_dtype(df[col]):
                    df[col] = df[col].fillna(0.0)

            logger.info(f"[DISEASE TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DRUG FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if drugs and "drug_name" in df.columns:
                logger.info(f"[DISEASE TOOL] Applying drug filter for: {drug_names}")

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
                    log_prefix="[DISEASE TOOL]",
                    expand_noun="drug terms",
                )

            # ------------------------------------------------------------------
            # TARGET FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if targets and "gene_name" in df.columns:
                logger.info(f"[DISEASE TOOL] Applying target filter for: {target_names}")

                async def _target_exp(n):
                    b = await get_target_synonyms(n)
                    return b.get("combined", []) if (isinstance(b, dict) and "error" not in b) else []

                df = await apply_ontology_filter(
                    df,
                    col="gene_name",
                    input_names=target_names,
                    expander=_target_exp,
                    exec_log=exec_log,
                    step="target_filter",
                    action="Applied target synonym filtering",
                    detail_key="input_targets",
                    log_prefix="[DISEASE TOOL]",
                    expand_noun="target terms",
                )

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

            # On the disease-only default path, keep all columns so the user
            # sees both drug and target associations for the disease.
            if not disease_only_default:
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

            # Sort: primary by association_score DESC, secondary by phase ordinal DESC.
            # Using _phase_ordinal so "APPROVAL" ranks at 5 (above all trial phases)
            # rather than being coerced to NaN/-1 by pd.to_numeric.
            sort_cols, sort_asc = [], []
            if 'association_score' in df.columns:
                df['_score_num'] = pd.to_numeric(df['association_score'], errors='coerce').fillna(-1)
                sort_cols.append('_score_num'); sort_asc.append(False)
            if 'phase' in df.columns:
                df['_phase_num'] = df['phase'].map(_phase_ordinal)
                sort_cols.append('_phase_num'); sort_asc.append(False)
            if sort_cols:
                df = df.sort_values(by=sort_cols, ascending=sort_asc)
                df = df.drop(columns=[c for c in ['_score_num', '_phase_num'] if c in df.columns])
                logger.info(f"[DISEASE TOOL] Sorted by {sort_cols}")

            df = df.reset_index(drop=True)

            # Drop columns where every value is NaN / empty string / None
            df = df.replace("", pd.NA).dropna(axis=1, how="all")
            # Drop rows where every value is NaN / empty
            df = df.dropna(axis=0, how="all")
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

            csv_path = await save_and_publish_csv(
                df, connection_id, "disease_tool_associations", "Associations", SERVICE_NAME, final_row_count
            )

        # Get disease synonyms
        try:
            bundle = await get_disease_and_descendant_synonyms(disease_name)
            synonym_list = bundle.get("combined", [])
            logger.info(f"[DISEASE TOOL] Retrieved {len(synonym_list)} synonyms")
        except Exception as e:
            logger.warning(f"[DISEASE TOOL] Failed to get disease synonyms: {e}")
            synonym_list = []

        # Build result message from structured data fields (no LLM call needed)
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
            metadata=disease_meta if disease_meta else None,
            filter_trace=exec_log.to_filter_trace(),
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
            metadata=disease_meta if disease_meta else None,
        )
