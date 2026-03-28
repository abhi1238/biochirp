import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    TARGET_ASSOC_PAGED_QUERY,
    TARGET_DRUGS_QUERY,
    TARGET_DRUGS_QUERY_V26,
    TARGET_PATHWAYS_QUERY,
)
from .resolvers import resolve_target_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.target_data")
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)
_USE_LEGACY_TARGET_KNOWN_DRUGS = True


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


async def _get_target_drug_rows(target_id: str) -> List[Dict[str, Any]]:
    """
    Return rows normalized to a knownDrugs-like shape:
      {
        phase, status,
        disease: {id, name},
        drug: {...}
      }
    """
    global _USE_LEGACY_TARGET_KNOWN_DRUGS

    if not _USE_LEGACY_TARGET_KNOWN_DRUGS:
        data = await _ot.run(TARGET_DRUGS_QUERY_V26, {"id": target_id})
        rows = (((data.get("target") or {}).get("drugAndClinicalCandidates") or {}).get("rows")) or []

        normalized: List[Dict[str, Any]] = []
        for r in rows:
            drug = r.get("drug") or {}
            diseases = r.get("diseases") or []
            status = _statuses_from_reports(r.get("clinicalReports") or [])
            phase = r.get("maxClinicalStage")

            if not diseases:
                normalized.append(
                    {
                        "phase": phase,
                        "status": status,
                        "disease": {},
                        "drug": drug,
                    }
                )
                continue

            for d in diseases:
                disease_obj = d.get("disease") or {}
                normalized.append(
                    {
                        "phase": phase,
                        "status": status,
                        "disease": {
                            "id": disease_obj.get("id"),
                            "name": disease_obj.get("name") or d.get("diseaseFromSource"),
                        },
                        "drug": drug,
                    }
                )
        return normalized

    try:
        return await _ot.fetch_cursor_rows(
            TARGET_DRUGS_QUERY,
            {"id": target_id, "size": 1000},
            root="target",
            node="knownDrugs",
        )
    except Exception as e:
        # OpenTargets API 26.03+ removed Target.knownDrugs in favor of drugAndClinicalCandidates.
        if not _is_missing_field_error(e, "knownDrugs", "Target"):
            raise
        _USE_LEGACY_TARGET_KNOWN_DRUGS = False
        logger.warning(
            "Target.knownDrugs unavailable for %s; falling back to Target.drugAndClinicalCandidates",
            target_id,
        )
        data = await _ot.run(TARGET_DRUGS_QUERY_V26, {"id": target_id})
        rows = (((data.get("target") or {}).get("drugAndClinicalCandidates") or {}).get("rows")) or []

        normalized: List[Dict[str, Any]] = []
        for r in rows:
            drug = r.get("drug") or {}
            diseases = r.get("diseases") or []
            status = _statuses_from_reports(r.get("clinicalReports") or [])
            phase = r.get("maxClinicalStage")

            if not diseases:
                normalized.append(
                    {
                        "phase": phase,
                        "status": status,
                        "disease": {},
                        "drug": drug,
                    }
                )
                continue

            for d in diseases:
                disease_obj = d.get("disease") or {}
                normalized.append(
                    {
                        "phase": phase,
                        "status": status,
                        "disease": {
                            "id": disease_obj.get("id"),
                            "name": disease_obj.get("name") or d.get("diseaseFromSource"),
                        },
                        "drug": drug,
                    }
                )
        return normalized


async def get_target_pathways_only(gene_symbol_or_ensembl: str) -> pd.DataFrame:
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    data = await _ot.run(TARGET_PATHWAYS_QUERY, {"id": target_id})
    tgt = data.get("target") or {}
    rows = tgt.get("pathways") or []

    if not rows:
        return pd.DataFrame(
            columns=["gene_id", "gene_name", "pathway_id", "pathway_name", "top_level_term"]
        )

    return pd.DataFrame.from_records(
        [
            {
                "gene_id": tgt.get("id") or target_id,
                "gene_name": tgt.get("approvedSymbol") or target_name,
                "pathway_id": p.get("pathwayId"),
                "pathway_name": p.get("pathway"),
                "top_level_term": p.get("topLevelTerm"),
            }
            for p in rows
        ]
    )


async def get_target_associations_no_pathways(gene_symbol_or_ensembl: str) -> pd.DataFrame:
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    drug_rows_task = asyncio.create_task(_get_target_drug_rows(target_id))

    # Paged associated diseases for full score map
    assoc_rows: List[Dict[str, Any]] = []
    index = 0
    total = None
    while True:
        assoc = await _ot.run(
            TARGET_ASSOC_PAGED_QUERY,
            {"id": target_id, "index": index, "size": _cfg.page_size},
        )
        assoc_block = ((assoc.get("target") or {}).get("associatedDiseases") or {})
        rows = assoc_block.get("rows") or []
        total = assoc_block.get("count")
        if not rows:
            break
        assoc_rows.extend(rows)
        if total is not None and len(assoc_rows) >= total:
            break
        index += 1

    score_map = {(r.get("disease") or {}).get("id"): r.get("score") for r in assoc_rows}

    extra_cols = ["phase", "status", "action_types", "mechanism_of_action", "association_score"]

    drug_rows = await drug_rows_task
    if not drug_rows:
        return empty_df(extra_cols=extra_cols)

    recs: List[Dict[str, Any]] = []

    for r in drug_rows:
        disease = r.get("disease") or {}
        drug = r.get("drug") or {}

        raw_moas = (drug.get("mechanismsOfAction") or {}).get("rows") or []
        valid_actions = set()
        valid_mechanisms = set()

        for m in raw_moas:
            moa_target_ids = {t.get("id") for t in (m.get("targets") or [])}
            if target_id in moa_target_ids:
                if m.get("actionType"):
                    valid_actions.add(m.get("actionType"))
                if m.get("mechanismOfAction"):
                    valid_mechanisms.add(m.get("mechanismOfAction"))

        recs.append(
            {
                "gene_id": target_id,
                "gene_name": target_name,
                "drug_id": drug.get("id"),
                "drug_name": drug.get("name"),
                "disease_id": disease.get("id"),
                "disease_name": disease.get("name"),
                "phase": r.get("phase"),
                "status": r.get("status"),
                "action_types": ", ".join(sorted(valid_actions)) if valid_actions else None,
                "mechanism_of_action": "; ".join(sorted(valid_mechanisms)) if valid_mechanisms else None,
                "association_score": score_map.get(disease.get("id")),
            }
        )

    df = pd.DataFrame.from_records(recs)
    return ensure_cols(df, extra_cols=extra_cols)


async def get_target_diseases_all(
    gene_symbol_or_ensembl: str, page_size: int = 1000
) -> pd.DataFrame:
    """
    Fetch ALL diseases associated with a target (no drug filtering).
    Uses paged associatedDiseases.
    """
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    all_rows: List[Dict[str, Any]] = []
    index = 0
    total = None

    while True:
        data = await _ot.run(
            TARGET_ASSOC_PAGED_QUERY,
            {"id": target_id, "index": index, "size": page_size},
        )
        assoc = ((data.get("target") or {}).get("associatedDiseases") or {})
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
                "gene_id",
                "gene_name",
                "disease_id",
                "disease_name",
                "association_score",
            ]
        )

    df = pd.DataFrame.from_records(
        [
            {
                "gene_id": target_id,
                "gene_name": target_name,
                "disease_id": (r.get("disease") or {}).get("id"),
                "disease_name": (r.get("disease") or {}).get("name"),
                "association_score": r.get("score"),
            }
            for r in all_rows
        ]
    )
    return df
