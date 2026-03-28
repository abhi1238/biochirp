import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    DRUG_INDICATIONS_QUERY_V26,
    DRUG_KNOWN_DISEASES_QUERY,
    DRUG_MOA_QUERY,
)
from .resolvers import resolve_drug_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.drug_data")
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)
_USE_LEGACY_DRUG_KNOWN_DRUGS = True


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
                "gene_name": None,
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
    global _USE_LEGACY_DRUG_KNOWN_DRUGS
    drug_id, drug_name = await resolve_drug_id(drug_name_or_id)

    if not _USE_LEGACY_DRUG_KNOWN_DRUGS:
        data = await _ot.run(DRUG_INDICATIONS_QUERY_V26, {"chemblId": drug_id})
        rows = (((data.get("drug") or {}).get("indications") or {}).get("rows")) or []
        return _parse_drug_indications_v26_rows(rows, drug_id, drug_name)

    try:
        rows = await _ot.fetch_cursor_rows(
            DRUG_KNOWN_DISEASES_QUERY,
            {"chemblId": drug_id, "size": _cfg.page_size},
            root="drug",
            node="knownDrugs",
        )
        if not rows:
            return empty_df(extra_cols=["phase", "status"])

        recs: List[Dict[str, Any]] = []
        for r in rows:
            disease = r.get("disease") or {}
            target = r.get("target") or {}
            recs.append(
                {
                    "gene_id": target.get("id"),
                    "gene_name": target.get("approvedSymbol") or target.get("approvedName"),
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "disease_id": disease.get("id"),
                    "disease_name": disease.get("name"),
                    "phase": r.get("phase"),
                    "status": r.get("status"),
                }
            )
        return ensure_cols(pd.DataFrame.from_records(recs), extra_cols=["phase", "status"])
    except Exception as e:
        # OpenTargets API 26.03+ removed Drug.knownDrugs in favor of Drug.indications.
        if not _is_missing_field_error(e, "knownDrugs", "Drug"):
            raise
        _USE_LEGACY_DRUG_KNOWN_DRUGS = False
        logger.warning(
            "Drug.knownDrugs unavailable for %s; falling back to Drug.indications",
            drug_id,
        )
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


async def get_drug_master(drug_name_or_id: str, *, how: str = "left") -> pd.DataFrame:
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
        # v26 path has no target in indications; attach MOA targets by drug_id
        attach = moa_agg.copy()
        attach["gene_name"] = attach["_moa_target_name"]
        attach = attach.drop(columns=["_moa_target_name"])

        base_cols = [c for c in df_base.columns if c not in {"gene_id", "gene_name"}]
        df = df_base[base_cols].merge(
            attach,
            on="drug_id",
            how="left",
            validate="m:m",
        )
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
