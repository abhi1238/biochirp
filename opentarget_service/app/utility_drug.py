


"""
Drug tool for fetching drug-related data with ontology-aware filtering
WITH structured execution logging, semantic filtering, and comprehensive console logs
"""

from typing import Set, List, Optional, Dict, Any
import asyncio
import logging
import pandas as pd
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
from .drug_data import get_drug_enriched_info, get_drug_master
from .utility_join import _phase_ordinal
from .ontology import (
    get_disease_and_descendant_synonyms,
    get_drug_synonyms,
    get_target_synonyms,
    get_drug_description,
)
from .utility import df_to_llm_safe_hierarchy
from .member_selector import member_selection
from .generate_log import ToolExecutionLog
from .utility_shared import (
    _safe, _csv_path, RESULTS_ROOT, MAX_PREVIEW_ROWS,
    extract_surface_forms, is_explicit_entity, save_and_publish_csv,
    apply_ontology_filter,
)
from .resolvers import get_requested_output

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.drug")

SERVICE_NAME = "drug_tool"


# ==============================================================================
# DRUG TOOL
# ==============================================================================
@function_tool(
    strict_mode=False,
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
    drug_meta: dict = {}

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

        # When only the drug anchor reaches the tool (no disease/target/
        # mechanism filter present — which happens when the orchestrator
        # strips "requested" entities before calling the tool), default to
        # fetching the full drug→disease+target association table. Without
        # this, the association block below is skipped and row_count=0 → no
        # table is published to the UI.
        drug_only_default = (
            len(effective_filter_types) == 0
            and present_types.issubset({"drug", "pathway"})
        )

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

        # Prefer the resolved CHEMBL ID so resolve_drug_id avoids a
        # secondary search that may fail on OT's canonical spelling.
        _drug_ent = drugs[0]
        drug_name = (
            _drug_ent.id
            if (_drug_ent.id or "").upper().startswith("CHEMBL")
            else _drug_ent.surface_form
        )
        logger.info(f"[DRUG TOOL] Primary drug: {drug_name}")

        description, drug_meta = await asyncio.gather(
            get_drug_description(drug_name),
            get_drug_enriched_info(drug_name),
        )
        logger.info(f"[DRUG TOOL] Description retrieved: {description[:100] if description else None}...")
        logger.info(f"[DRUG TOOL] Drug meta keys: {list(drug_meta.keys())}")

        # ------------------------------------------------------------------
        # ASSOCIATION RETRIEVAL (filters beyond pathway/drug, OR default
        # path when only the drug anchor was passed)
        # ------------------------------------------------------------------
        if len(effective_filter_types) > 0 or drug_only_default:
            logger.info(f"[DRUG TOOL] Fetching associations for drug: {drug_name}")

            # When the user asks about targets, force MoA target rows explicitly
            # so the v26 path returns one row per target not per indication.
            # requested_output is NOT in the QueryResolution JSON (omitted to avoid
            # orchestrator confusion) — look it up from the connection cache instead.
            # The cache is populated by interpreter() from the NER model's output;
            # "target" in present_types catches it when the orchestrator correctly
            # forwards the implicit-request entity from the interpreter result.
            _requested_output = getattr(input, "requested_output", None) or get_requested_output(connection_id)
            logger.info(f"[DRUG TOOL] requested_output={_requested_output!r} (from input or cache)")
            _wants_targets = (
                "target" in effective_filter_types
                or "target" in present_types
                or _requested_output == "target"
            )
            _drug_master_mode = (
                "targets"
                if _wants_targets and "disease" not in effective_filter_types
                else "auto"
            )
            df = await get_drug_master(drug_name, how="left", mode=_drug_master_mode)

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
                elif pd.api.types.is_float_dtype(df[col]):
                    df[col] = df[col].fillna(0.0)

            logger.info(f"[DRUG TOOL] Base rows before filtering: {len(df)}")

            # ------------------------------------------------------------------
            # DISEASE FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if diseases and "disease_name" in df.columns:
                logger.info(f"[DRUG TOOL] Applying disease filter for: {disease_names}")

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
                    log_prefix="[DRUG TOOL]",
                )

            # ------------------------------------------------------------------
            # TARGET FILTER (LOG OVERLAPPING EXPANSIONS)
            # ------------------------------------------------------------------
            if targets and "gene_name" in df.columns:
                logger.info(f"[DRUG TOOL] Applying target filter for: {target_names}")

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
                    log_prefix="[DRUG TOOL]",
                    expand_noun="target terms",
                )

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

            # Reuse the same requested_output determined above (from input or cache).
            want_disease = "disease" in present_types or _requested_output == "disease"
            want_target = "target" in present_types or _requested_output == "target"

            # General "what does drug X do?" — no specific output intent → keep all columns.
            if drug_only_default and not want_disease and not want_target:
                pass
            else:
                if not want_disease:
                    columns_to_drop.extend(["disease_id", "disease_name", "phase", "status"])
                if not want_target:
                    columns_to_drop.extend(["gene_id", "gene_name"])

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

            # Sort by phase (higher = more advanced).
            # Use _phase_ordinal so string "APPROVAL" ranks at 5 (above all trial phases)
            # rather than being coerced to NaN/-1 by pd.to_numeric.
            if 'phase' in df.columns:
                df['phase_numeric'] = df['phase'].map(_phase_ordinal)
                df = df.sort_values(by='phase_numeric', ascending=False)
                df = df.drop(columns=['phase_numeric'])
                logger.info(f"[DRUG TOOL] Sorted by phase (APPROVAL-aware)")

            df = df.reset_index(drop=True)

            # Drop columns where every value is NaN / empty string / None
            df = df.replace("", pd.NA).dropna(axis=1, how="all")
            df = df.dropna(axis=0, how="all")
            df = df.reset_index(drop=True)

            final_row_count = len(df)
            logger.info(f"[DRUG TOOL] Final associations result: {final_row_count} rows")

            # ------------------------------------------------------------------
            # PREVIEW + SAVE ASSOCIATIONS
            # ------------------------------------------------------------------
            preview_rows = df_to_llm_safe_hierarchy(
                df.head(MAX_PREVIEW_ROWS), root_col="drug_name"
            )

            csv_path = await save_and_publish_csv(
                df, connection_id, "drug_tool_associations", "Associations", SERVICE_NAME, final_row_count
            )

        # Get drug synonyms
        try:
            synonym_list = await get_drug_synonyms(drug_name)
            if not isinstance(synonym_list, list):
                synonym_list = []
            logger.info(f"[DRUG TOOL] Retrieved {len(synonym_list)} synonyms")
        except Exception as e:
            logger.warning(f"[DRUG TOOL] Failed to get drug synonyms: {e}")
            synonym_list = []

        # Build result message: approval summary + top-5 hint so the synthesizer
        # reads correct APPROVAL status from structured text, not the cardinality-
        # ordered hierarchy JSON (which loses row-order information).
        message_parts = [f"Retrieved {final_row_count} associations for drug {drug_name}"]
        if diseases:
            message_parts.append(f"filtered by {len(diseases)} disease(s)")
        if targets:
            message_parts.append(f"filtered by {len(targets)} target(s)")
        final_message = ". ".join(message_parts) + "."

        if df is not None and not df.empty:
            # APPROVAL summary — list every indication with APPROVAL phase so the
            # synthesizer never misses an approved indication buried in the table.
            if "phase" in df.columns and "disease_name" in df.columns:
                _approval_df = df[df["phase"].astype(str).str.contains("APPROV", case=False, na=False)]
                if not _approval_df.empty:
                    _approved = _approval_df["disease_name"].dropna().unique().tolist()
                    final_message += (
                        f" SYNTHESIZER-HINT — APPROVAL CHECK: {drug_name} HAS {len(_approved)} APPROVED"
                        f" indication(s): {'; '.join(_approved[:15])}."
                        " Your prose MUST acknowledge ALL of these as approved."
                    )
                else:
                    final_message += (
                        f" SYNTHESIZER-HINT — APPROVAL CHECK: no APPROVAL-phase row found in the"
                        f" top-{min(final_row_count, MAX_PREVIEW_ROWS)} results."
                        " If the question is about approval, state 'not recorded as approved in OT'."
                    )
            # Top-25 hint (phase-sorted → APPROVAL first) so the synthesizer reads the
            # correct disease in row 0, not an arbitrary training-knowledge answer.
            _t5cols = [c for c in ("disease_name", "phase", "association_score") if c in df.columns]
            if _t5cols:
                _t5 = df.head(25)[_t5cols].to_dict("records")
                _t5_str = "; ".join(
                    f"#{i+1} {r.get('disease_name','?')} phase={r.get('phase','?')}"
                    + (f" score={float(r['association_score']):.3f}" if r.get("association_score") else "")
                    for i, r in enumerate(_t5)
                )
                final_message += f" Top-{min(25, final_row_count)} phase-sorted indications: {_t5_str}."
            # Mechanism-of-action hint — surfaces the drug's target mechanism so the
            # synthesizer can add molecular-subtype qualifiers (e.g. EGFR-mutant NSCLC).
            if "mechanism_of_action" in df.columns:
                _moa_vals = df["mechanism_of_action"].dropna().unique().tolist()
                _moa_vals = [m for m in _moa_vals if m and str(m).strip()]
                if _moa_vals:
                    final_message += (
                        f" MECHANISM-OF-ACTION: {'; '.join(str(m) for m in _moa_vals[:5])}."
                        " Use this to qualify the disease name if the mechanism implies a molecular subtype"
                        " (e.g. EGFR inhibitor → 'EGFR-mutant NSCLC', ERBB2/HER2 → 'HER2-positive')."
                    )

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
            metadata=drug_meta if drug_meta else None,
            filter_trace=exec_log.to_filter_trace(),
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
            metadata=drug_meta if drug_meta else None,
        )
