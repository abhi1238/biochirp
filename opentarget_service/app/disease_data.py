import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    DISEASE_DRUG_AND_CLINICAL_CANDIDATES_QUERY_V26,
    DISEASE_KNOWN_DRUGS_QUERY,
    DISEASE_TARGETS_PAGED_QUERY,
)
from .resolvers import resolve_disease_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.disease_data")
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)
_USE_LEGACY_DISEASE_KNOWN_DRUGS = True


def _is_missing_field_error(exc: Exception, field: str, type_name: str) -> bool:
    msg_parts = [str(exc)]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            msg_parts.append(resp.text or "")
        except Exception:
            pass
    msg = " ".join(msg_parts)
    return (
        "Cannot query field" in msg
        and f"'{field}'" in msg
        and f"type '{type_name}'" in msg
    )


def _statuses_from_reports(reports: List[Dict[str, Any]]) -> str | None:
    statuses = sorted(
        {
            (r.get("trialOverallStatus") or "").strip()
            for r in (reports or [])
            if (r.get("trialOverallStatus") or "").strip()
        }
    )
    return "; ".join(statuses) if statuses else None


def _mechanism_from_drug(drug: Dict[str, Any]) -> str | None:
    moa_rows = ((drug.get("mechanismsOfAction") or {}).get("rows")) or []
    moas = sorted(
        {
            (row.get("mechanismOfAction") or "").strip()
            for row in moa_rows
            if (row.get("mechanismOfAction") or "").strip()
        }
    )
    return "; ".join(moas) if moas else None


def _empty_drug_df() -> pd.DataFrame:
    return empty_df(
        extra_cols=[
            "indication_id",
            "indication_name",
            "drug_id",
            "drug_name",
            "gene_id",
            "gene_name",
            "phase",
            "status",
            "drug_type",
            "mechanism_of_action",
        ]
    )


async def _get_disease_known_drugs_raw(disease_id: str, disease_name: str) -> pd.DataFrame:
    global _USE_LEGACY_DISEASE_KNOWN_DRUGS
    if not _USE_LEGACY_DISEASE_KNOWN_DRUGS:
        data = await _ot.run(
            DISEASE_DRUG_AND_CLINICAL_CANDIDATES_QUERY_V26,
            {"efoId": disease_id},
        )
        rows = (((data.get("disease") or {}).get("drugAndClinicalCandidates") or {}).get("rows")) or []
        if not rows:
            return _empty_drug_df()

        recs: List[Dict[str, Any]] = []
        for r in rows:
            drug = r.get("drug") or {}
            reports = r.get("clinicalReports") or []
            recs.append(
                {
                    "disease_id": disease_id,
                    "disease_name": disease_name,
                    "indication_id": r.get("id") or disease_id,
                    "indication_name": disease_name,
                    "drug_id": drug.get("id"),
                    "drug_name": drug.get("name"),
                    "gene_id": None,
                    "gene_name": None,
                    "phase": r.get("maxClinicalStage"),
                    "status": _statuses_from_reports(reports),
                    "drug_type": drug.get("drugType"),
                    "mechanism_of_action": _mechanism_from_drug(drug),
                }
            )
        return pd.DataFrame.from_records(recs)

    try:
        rows = await _ot.fetch_cursor_rows(
            DISEASE_KNOWN_DRUGS_QUERY,
            {"efoId": disease_id, "size": _cfg.page_size},
            root="disease",
            node="knownDrugs",
        )
        if not rows:
            return _empty_drug_df()

        recs: List[Dict[str, Any]] = []
        for r in rows:
            drug = r.get("drug") or {}
            target = r.get("target") or {}
            row_disease = r.get("disease") or {}

            recs.append(
                {
                    "disease_id": disease_id,
                    "disease_name": disease_name,
                    "indication_id": row_disease.get("id"),
                    "indication_name": row_disease.get("name"),
                    "drug_id": drug.get("id"),
                    "drug_name": drug.get("name"),
                    "gene_id": target.get("id"),
                    "gene_name": target.get("approvedSymbol") or target.get("approvedName"),
                    "phase": r.get("phase"),
                    "status": r.get("status"),
                    "drug_type": r.get("drugType"),
                    "mechanism_of_action": r.get("mechanismOfAction"),
                }
            )
        return pd.DataFrame.from_records(recs)
    except Exception as e:
        # OpenTargets API 26.03+ removed Disease.knownDrugs in favor of drugAndClinicalCandidates.
        if not _is_missing_field_error(e, "knownDrugs", "Disease"):
            raise
        _USE_LEGACY_DISEASE_KNOWN_DRUGS = False
        logger.warning(
            "Disease.knownDrugs unavailable for %s; falling back to Disease.drugAndClinicalCandidates",
            disease_id,
        )

        data = await _ot.run(
            DISEASE_DRUG_AND_CLINICAL_CANDIDATES_QUERY_V26,
            {"efoId": disease_id},
        )
        rows = (((data.get("disease") or {}).get("drugAndClinicalCandidates") or {}).get("rows")) or []
        if not rows:
            return _empty_drug_df()

        recs: List[Dict[str, Any]] = []
        for r in rows:
            drug = r.get("drug") or {}
            reports = r.get("clinicalReports") or []

            recs.append(
                {
                    "disease_id": disease_id,
                    "disease_name": disease_name,
                    "indication_id": r.get("id") or disease_id,
                    "indication_name": disease_name,
                    "drug_id": drug.get("id"),
                    "drug_name": drug.get("name"),
                    "gene_id": None,
                    "gene_name": None,
                    "phase": r.get("maxClinicalStage"),
                    "status": _statuses_from_reports(reports),
                    "drug_type": drug.get("drugType"),
                    "mechanism_of_action": _mechanism_from_drug(drug),
                }
            )
        return pd.DataFrame.from_records(recs)


async def _get_disease_target_scores_raw(disease_id: str) -> pd.DataFrame:
    """Internal helper to fetch ALL target scores for a disease."""
    all_rows: List[Dict[str, Any]] = []
    index = 0
    total = None

    while True:
        data = await _ot.run(
            DISEASE_TARGETS_PAGED_QUERY,
            {"id": disease_id, "index": index, "size": _cfg.page_size},
        )
        disease = data.get("disease") or {}
        assoc = disease.get("associatedTargets") or {}
        rows = assoc.get("rows") or []
        total = assoc.get("count")

        if not rows:
            break

        all_rows.extend(rows)

        if total is not None and len(all_rows) >= total:
            break
        index += 1

    if not all_rows:
        return pd.DataFrame(columns=["gene_id", "association_score"])

    return pd.DataFrame.from_records(
        [
            {
                "gene_id": (r.get("target") or {}).get("id"),
                "association_score": r.get("score"),
            }
            for r in all_rows
        ]
    )


async def get_disease_combined_knowledge(disease_name_or_id: str) -> pd.DataFrame:
    """
    COMBINED TABLE 3 & 4:
    Returns disease drugs merged with association score where gene ids are available.
    """
    disease_id, disease_name = await resolve_disease_id(disease_name_or_id)

    df_drugs, df_scores = await asyncio.gather(
        _get_disease_known_drugs_raw(disease_id, disease_name),
        _get_disease_target_scores_raw(disease_id),
    )

    if df_drugs.empty:
        return ensure_cols(
            df_drugs,
            extra_cols=[
                "phase",
                "status",
                "drug_type",
                "mechanism_of_action",
                "association_score",
                "indication_id",
                "indication_name",
            ],
        )

    if df_scores.empty:
        df_drugs["association_score"] = None
        return ensure_cols(
            df_drugs,
            extra_cols=[
                "phase",
                "status",
                "drug_type",
                "mechanism_of_action",
                "association_score",
                "indication_id",
                "indication_name",
            ],
        )

    df_merged = df_drugs.merge(df_scores, on="gene_id", how="left")
    return ensure_cols(
        df_merged,
        extra_cols=[
            "phase",
            "status",
            "drug_type",
            "mechanism_of_action",
            "association_score",
            "indication_id",
            "indication_name",
        ],
    )


async def get_targets_for_disease_all(
    disease_name: str, page_size: int = 1000
) -> pd.DataFrame:
    disease_id, resolved_name = await resolve_disease_id(disease_name)

    all_rows: List[Dict[str, Any]] = []
    index = 0
    total = None

    while True:
        data = await _ot.run(
            DISEASE_TARGETS_PAGED_QUERY,
            {"id": disease_id, "index": index, "size": page_size},
        )
        disease = data.get("disease") or {}
        assoc = disease.get("associatedTargets") or {}
        rows = assoc.get("rows") or []
        total = assoc.get("count")

        if not rows:
            break

        all_rows.extend(rows)

        if total is not None and len(all_rows) >= total:
            break
        index += 1

    if not all_rows:
        return pd.DataFrame(
            columns=[
                "disease_id",
                "disease_name",
                "gene_id",
                "gene_symbol",
                "target_name",
                "association_score",
            ]
        )

    df = pd.DataFrame.from_records(
        [
            {
                "disease_id": disease_id,
                "disease_name": resolved_name,
                "gene_id": (r.get("target") or {}).get("id"),
                "gene_symbol": (r.get("target") or {}).get("approvedSymbol"),
                "target_name": (r.get("target") or {}).get("approvedName"),
                "association_score": r.get("score"),
            }
            for r in all_rows
        ]
    )
    return df
