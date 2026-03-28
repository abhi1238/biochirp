import asyncio
import pandas as pd
from typing import Dict, Any, List

from .config import OTClientConfig
from .client import OTGraphQLClient
from .graphql import (
    DISEASE_KNOWN_DRUGS_QUERY,
    DISEASE_TARGETS_PAGED_QUERY,
)
from .dataframe import empty_df, ensure_cols
from .resolvers import resolve_disease_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.disease_data")
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)


async def _get_disease_known_drugs_raw(disease_id: str, disease_name: str) -> pd.DataFrame:
    """
    Internal helper to fetch drugs using cursor pagination.

    Args:
        disease_id: The EFO/MONDO ID of the disease (e.g., 'EFO_0000768')
        disease_name: The disease name (passed in to avoid re-fetching)
    """
    rows = await _ot.fetch_cursor_rows(
        DISEASE_KNOWN_DRUGS_QUERY,
        {"efoId": disease_id, "size": _cfg.page_size},
        root="disease",
        node="knownDrugs",
    )

    if not rows:
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
    Returns the 'Known Drugs' for a disease, merged with the 'Association Score'
    of each drug's target.
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
