import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    DRUG_ENRICHED_QUERY,
    DRUG_INDICATIONS_QUERY_V26,
    DRUG_MOA_QUERY,
)
from .resolvers import resolve_drug_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.drug_data")
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)


def _statuses_from_reports(reports: List[Dict[str, Any]]) -> str | None:
    statuses = sorted(
        {
            (r.get("trialOverallStatus") or "").strip()
            for r in (reports or [])
            if (r.get("trialOverallStatus") or "").strip()
        }
    )
    return "; ".join(statuses) if statuses else None


def _parse_drug_indications_v26_rows(
    rows: List[Dict[str, Any]],
    drug_id: str,
    drug_name: str,
) -> pd.DataFrame:
    if not rows:
        return empty_df(extra_cols=["phase", "status"])

    recs: List[Dict[str, Any]] = []
    for r in rows:
        disease = r.get("disease") or {}
        reports = r.get("clinicalReports") or []
        recs.append(
            {
                "gene_id": None,
                "gene_symbol": None,
                "drug_id": drug_id,
                "drug_name": drug_name,
                "disease_id": disease.get("id"),
                "disease_name": disease.get("name"),
                "phase": r.get("maxClinicalStage"),
                "status": _statuses_from_reports(reports),
            }
        )
    return ensure_cols(pd.DataFrame.from_records(recs), extra_cols=["phase", "status"])


async def get_drug_known_diseases_targets(drug_name_or_id: str) -> pd.DataFrame:
    drug_id, drug_name = await resolve_drug_id(drug_name_or_id)
    data = await _ot.run(DRUG_INDICATIONS_QUERY_V26, {"chemblId": drug_id})
    rows = (((data.get("drug") or {}).get("indications") or {}).get("rows")) or []
    return _parse_drug_indications_v26_rows(rows, drug_id, drug_name)


async def get_drug_mechanisms_of_action(drug_name_or_id: str) -> pd.DataFrame:
    drug_id, drug_name = await resolve_drug_id(drug_name_or_id)
    data = await _ot.run(DRUG_MOA_QUERY, {"chemblId": drug_id})
    drug = data.get("drug") or {}
    rows = ((drug.get("mechanismsOfAction") or {}).get("rows")) or []
    if not rows:
        return empty_df(extra_cols=["mechanism_of_action", "references"])
    recs: List[Dict[str, Any]] = []
    for r in rows:
        refs = sorted({ref.get("source") for ref in (r.get("references") or []) if ref.get("source")})
        ref_str = ", ".join(refs) if refs else None
        targets = r.get("targets") or []
        if not targets:
            recs.append(
                {
                    "gene_id": None,
                    "gene_name": r.get("targetName"),
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "disease_id": None,
                    "disease_name": None,
                    "mechanism_of_action": r.get("mechanismOfAction"),
                    "references": ref_str,
                }
            )
            continue
        for t in targets:
            recs.append(
                {
                    "gene_id": t.get("id"),
                    "gene_name": t.get("approvedSymbol") or t.get("approvedName") or r.get("targetName"),
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "disease_id": None,
                    "disease_name": None,
                    "mechanism_of_action": r.get("mechanismOfAction"),
                    "references": ref_str,
                }
            )
    df = pd.DataFrame.from_records(recs)
    return ensure_cols(df, extra_cols=["mechanism_of_action", "references"])


async def get_drug_master(drug_name_or_id: str, *, how: str = "left", mode: str = "auto") -> pd.DataFrame:
    """
    mode:
      "auto"        – heuristic: prefer disease rows (n_indications >= n_targets) or
                      target rows (n_targets > n_indications) on the v26 path.
      "indications" – always return disease-indication rows (v26: collapsed MoA summary).
      "targets"     – always return MoA target rows (v26: one row per MoA target).
    """
    df_base, df_moa = await asyncio.gather(
        get_drug_known_diseases_targets(drug_name_or_id),
        get_drug_mechanisms_of_action(drug_name_or_id),
    )

    if not df_moa.empty:
        moa_agg = (
            df_moa.groupby(["drug_id", "gene_id"], dropna=False, as_index=False)
            .agg(
                mechanism_of_action=("mechanism_of_action", lambda x: "; ".join(sorted({v for v in x if v}))),
                references=("references", lambda x: "; ".join(sorted({v for v in x if v}))),
                _moa_target_name=("gene_name", lambda x: next((v for v in x if v), None)),
            )
        )
    else:
        moa_agg = pd.DataFrame(
            columns=["drug_id", "gene_id", "mechanism_of_action", "references", "_moa_target_name"]
        )

    if df_base.empty:
        df_out = df_moa.copy()
        return ensure_cols(df_out, extra_cols=["phase", "status", "mechanism_of_action", "references"])

    has_target_ids = "gene_id" in df_base.columns and df_base["gene_id"].notna().any()
    if not has_target_ids and not moa_agg.empty:
        # v26 path: drug.indications has no gene_id; MoA gives separate target rows.
        # Choose which set becomes the primary rows based on `mode`.
        use_targets = (
            mode == "targets"
            or (mode == "auto" and len(moa_agg) > len(df_base))
        )

        if use_targets:
            # Return one row per MoA target (drug_targets query intent).
            # Attach drug_name from df_base so downstream callers (e.g. df_to_llm_safe_hierarchy)
            # have the expected drug_name column.
            attach = moa_agg.rename(columns={"_moa_target_name": "gene_name"})
            if "drug_name" not in attach.columns and "drug_name" in df_base.columns:
                drug_name_map = df_base[["drug_id", "drug_name"]].drop_duplicates()
                attach = attach.merge(drug_name_map, on="drug_id", how="left")
            return ensure_cols(attach, extra_cols=["phase", "status", "mechanism_of_action", "references"])
        else:
            # Return one row per disease indication (drug_indications query intent)
            # Collapse MoA targets into a single summary row so we get 1 row per disease.
            moa_summary = (
                moa_agg.groupby("drug_id", as_index=False)
                .agg(
                    gene_id=("gene_id", lambda x: "; ".join(str(v) for v in x if pd.notna(v))),
                    gene_name=("_moa_target_name", lambda x: "; ".join(str(v) for v in x if v)),
                    mechanism_of_action=("mechanism_of_action", lambda x: "; ".join(v for v in x if v)),
                    references=("references", lambda x: "; ".join(v for v in x if v)),
                )
            )
            base_cols = [c for c in df_base.columns if c not in {"gene_id", "gene_name"}]
            df = df_base[base_cols].merge(moa_summary, on="drug_id", how="left", validate="m:1")
            return ensure_cols(df, extra_cols=["phase", "status", "mechanism_of_action", "references"])

    df = df_base.merge(
        moa_agg,
        on=["drug_id", "gene_id"],
        how=how,
        validate="m:1",
    )

    if "_moa_target_name" in df.columns:
        df["gene_name"] = df["gene_name"].fillna(df["_moa_target_name"])
        df.drop(columns=["_moa_target_name"], inplace=True)

    if "mechanism_of_action" not in df.columns:
        df["mechanism_of_action"] = None
    if "references" not in df.columns:
        df["references"] = None

    return ensure_cols(df, extra_cols=["phase", "status", "mechanism_of_action", "references"])


async def get_drug_enriched_info(drug_name_or_id: str) -> Dict[str, Any]:
    """
    Fetch rich drug annotation: synonyms, trade names, year of first approval,
    description, black-box warning, withdrawal status, drug warnings, top adverse events,
    linked targets, and linked diseases.
    Returns a flat dict for inclusion in TableOutput metadata.
    """
    drug_id, drug_name = await resolve_drug_id(drug_name_or_id)

    try:
        data = await _ot.run(DRUG_ENRICHED_QUERY, {"chemblId": drug_id})
    except Exception:
        logger.exception("DRUG_ENRICHED_QUERY failed for %s", drug_id)
        return {}

    drug = data.get("drug") or {}
    if not drug:
        return {}

    # Adverse events — top 25 by logLR
    adv = drug.get("adverseEvents") or {}
    adv_rows = adv.get("rows") or []
    adverse_events = [
        {
            "name": ae.get("name"),
            "count": ae.get("count"),
            "log_likelihood_ratio": ae.get("logLR"),
            "meddra_code": ae.get("meddraCode"),
        }
        for ae in adv_rows
        if ae.get("name")
    ]

    # Drug warnings
    warnings = [
        {
            "type": w.get("warningType"),
            "description": w.get("description"),
            "year": w.get("year"),
            "toxicity_class": w.get("toxicityClass"),
            "efo_id": w.get("efoId"),
            "efo_term": w.get("efoTerm"),
            "country": w.get("country"),
        }
        for w in (drug.get("drugWarnings") or [])
        if w.get("warningType") or w.get("description")
    ]

    # Mechanisms of action
    moa_rows = ((drug.get("mechanismsOfAction") or {}).get("rows")) or []
    mechanisms = [
        {
            "mechanism": r.get("mechanismOfAction"),
            "target_name": r.get("targetName"),
            "targets": [
                {"id": t.get("id"), "symbol": t.get("approvedSymbol")}
                for t in (r.get("targets") or [])
                if t.get("id")
            ],
        }
        for r in moa_rows
        if r.get("mechanismOfAction")
    ]

    # Indications from v26 API
    indication_rows = ((drug.get("indications") or {}).get("rows")) or []
    indications_count = (drug.get("indications") or {}).get("count", 0)
    indications = [
        {
            "disease_id": (r.get("disease") or {}).get("id"),
            "disease_name": (r.get("disease") or {}).get("name"),
            "max_clinical_stage": r.get("maxClinicalStage"),
        }
        for r in indication_rows[:20]
        if (r.get("disease") or {}).get("id")
    ]

    # Cross-references
    cross_refs = [
        {"source": cr.get("source"), "ids": cr.get("ids")}
        for cr in (drug.get("crossReferences") or [])
        if cr.get("source")
    ]

    # Parent molecule
    parent = drug.get("parentMolecule")
    parent_molecule = {"id": parent.get("id"), "name": parent.get("name")} if parent else None

    return {
        "drug_id": drug_id,
        "drug_name": drug.get("name") or drug_name,
        "description": drug.get("description"),
        "drug_type": drug.get("drugType"),
        "maximum_clinical_stage": drug.get("maximumClinicalStage"),
        "synonyms": drug.get("synonyms") or [],
        "trade_names": drug.get("tradeNames") or [],
        "cross_references": cross_refs,
        "parent_molecule": parent_molecule,
        "drug_warnings": warnings,
        "top_adverse_events": adverse_events,
        "adverse_events_critical_value": adv.get("criticalValue"),
        "mechanisms_of_action": mechanisms,
        "indications_count": indications_count,
        "indications": indications,

        # ── ADDED 2026-05 (Option A full-coverage refresh) ───────────────
        "child_molecules": [
            {"id": c.get("id"), "name": c.get("name")}
            for c in (drug.get("childMolecules") or []) if c.get("id")
        ],
        "similar_drugs": [
            {"score": s.get("score"),
             "chembl_id": (s.get("object") or {}).get("id"),
             "name": (s.get("object") or {}).get("name")}
            for s in (drug.get("similarEntities") or [])
        ],
        "literature_mention_count": ((drug.get("literatureOcurrences") or {}).get("count")),

        # ── ADDED 2026-05-18 — drug-side pharmacogenomics ───────────────
        # Complements Target.pharmacogenomics (which is anchored on a
        # gene); this is anchored on the drug.
        "pharmacogenomics": [
            {
                "category":          pgx.get("pgxCategory"),
                "evidence_level":    pgx.get("evidenceLevel"),
                "variant_id":        pgx.get("variantId"),
                "variant_rsid":      pgx.get("variantRsId"),
                "genotype":          pgx.get("genotype"),
                "annotation":        pgx.get("genotypeAnnotationText"),
                "phenotype":         pgx.get("phenotypeText"),
                "is_direct_target":  pgx.get("isDirectTarget"),
                "datasource":        pgx.get("datasourceId"),
                "target": {
                    "id":     (pgx.get("target") or {}).get("id"),
                    "symbol": (pgx.get("target") or {}).get("approvedSymbol"),
                    "name":   (pgx.get("target") or {}).get("approvedName"),
                } if pgx.get("target") else None,
            }
            for pgx in (drug.get("pharmacogenomics") or [])
            if pgx.get("phenotypeText") or pgx.get("pgxCategory")
        ],
    }
