import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    DISEASE_DRUG_AND_CLINICAL_CANDIDATES_QUERY_V26,
    DISEASE_INFO_QUERY,
    DISEASE_TARGETS_PAGED_QUERY,
    DISEASE_TARGETS_PAGED_QUERY_V2,
)
from .resolvers import resolve_disease_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.disease_data")
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


def _targets_from_drug(drug: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Unique MoA targets for a drug, keyed by gene id (dedup across MoA rows)."""
    moa_rows = ((drug.get("mechanismsOfAction") or {}).get("rows")) or []
    seen: Dict[str, str | None] = {}
    for row in moa_rows:
        for t in (row.get("targets") or []):
            tid = t.get("id")
            if tid and tid not in seen:
                seen[tid] = t.get("approvedSymbol")
    return [{"gene_id": tid, "gene_symbol": sym} for tid, sym in seen.items()]


def _empty_drug_df() -> pd.DataFrame:
    return empty_df(
        extra_cols=[
            "drug_id",
            "drug_name",
            "gene_id",
            "gene_symbol",
            "phase",
            "status",
            "drug_type",
            "mechanism_of_action",
        ]
    )


async def _get_disease_known_drugs_raw(disease_id: str, disease_name: str) -> pd.DataFrame:
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
        base = {
            "disease_id": disease_id,
            "disease_name": disease_name,
            "drug_id": drug.get("id"),
            "drug_name": drug.get("name"),
            "phase": r.get("maxClinicalStage"),
            "status": _statuses_from_reports(reports),
            "drug_type": drug.get("drugType"),
            "mechanism_of_action": _mechanism_from_drug(drug),
        }
        targets = _targets_from_drug(drug)
        if not targets:
            recs.append({**base, "gene_id": None, "gene_symbol": None})
            continue
        for t in targets:
            recs.append({**base, **t})
    return pd.DataFrame.from_records(recs)


_DATATYPE_COLS = [
    "score_genetic_association",
    "score_somatic_mutation",
    "score_drugs",
    "score_affected_pathway",
    "score_literature",
    "score_animal_model",
    "score_rna_expression",
    "score_known_variant",
    "score_clinical",            # OT v26+
    "score_genetic_literature",  # OT v26+
]

_DATATYPE_ID_MAP = {
    "genetic_association": "score_genetic_association",
    "somatic_mutation": "score_somatic_mutation",
    "drugs": "score_drugs",
    "affected_pathway": "score_affected_pathway",
    "literature": "score_literature",
    "animal_model": "score_animal_model",
    "rna_expression": "score_rna_expression",
    "known_variant": "score_known_variant",
    "clinical": "score_clinical",                      # OT v26+
    "genetic_literature": "score_genetic_literature",  # OT v26+
}


def _unpack_datatype_scores(datatype_scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {col: 0.0 for col in _DATATYPE_COLS}
    for entry in datatype_scores or []:
        col = _DATATYPE_ID_MAP.get(entry.get("id") or entry.get("componentId") or "")
        if col:
            out[col] = entry.get("score") or 0.0
    return out


async def _get_disease_target_scores_raw(disease_id: str) -> pd.DataFrame:
    """Fetch ALL target scores for a disease including per-datatype breakdown."""
    # --- Page 0: discover total count and first batch of rows ---
    use_v2 = True
    query = DISEASE_TARGETS_PAGED_QUERY_V2
    try:
        data = await _ot.run(query, {"id": disease_id, "index": 0, "size": _cfg.page_size})
    except Exception as e:
        resp_body = getattr(getattr(e, "response", None), "text", "") or ""
        if "datatypeScores" in resp_body or "componentId" in resp_body:
            logger.warning("datatypeScores unavailable; falling back to legacy query")
            use_v2 = False
            query = DISEASE_TARGETS_PAGED_QUERY
            data = await _ot.run(query, {"id": disease_id, "index": 0, "size": _cfg.page_size})
        else:
            raise

    assoc0 = (data.get("disease") or {}).get("associatedTargets") or {}
    first_rows = assoc0.get("rows") or []
    total = assoc0.get("count") or 0
    all_rows: List[Dict[str, Any]] = list(first_rows)

    if first_rows and total > len(all_rows):
        # Remaining pages — fetch all concurrently in batches of 8 to avoid hammering OT
        remaining_pages = (total - len(all_rows) + _cfg.page_size - 1) // _cfg.page_size

        async def _fetch_page(idx: int) -> List[Dict[str, Any]]:
            d = await _ot.run(query, {"id": disease_id, "index": idx, "size": _cfg.page_size})
            return ((d.get("disease") or {}).get("associatedTargets") or {}).get("rows") or []

        for batch_start in range(0, remaining_pages, 8):
            indices = range(1 + batch_start, 1 + min(batch_start + 8, remaining_pages))
            results = await asyncio.gather(*[_fetch_page(i) for i in indices], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    raise r
                all_rows.extend(r)

    if not all_rows:
        return pd.DataFrame(columns=["gene_id", "association_score"] + _DATATYPE_COLS)

    records = []
    for r in all_rows:
        tgt = r.get("target") or {}
        rec = {
            "gene_id": tgt.get("id"),
            "gene_biotype": tgt.get("biotype"),
            "association_score": r.get("score"),
        }
        rec.update(_unpack_datatype_scores(r.get("datatypeScores") or []))
        records.append(rec)

    return pd.DataFrame.from_records(records)


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

    _extra = [
        "phase", "status", "drug_type", "mechanism_of_action",
        "association_score", "gene_biotype", "indication_id", "indication_name",
    ] + _DATATYPE_COLS

    if df_drugs.empty:
        return ensure_cols(df_drugs, extra_cols=_extra)

    if df_scores.empty:
        df_drugs["association_score"] = None
        return ensure_cols(df_drugs, extra_cols=_extra)

    df_merged = df_drugs.merge(df_scores, on="gene_id", how="left")
    return ensure_cols(df_merged, extra_cols=_extra)


async def get_targets_for_disease_all(
    disease_name: str, page_size: int = 1000, max_rows: int = 0
) -> pd.DataFrame:
    """
    Fetch genes/targets associated with a disease including per-datatype evidence scores.
    If max_rows > 0, stop after collecting that many rows (returns top results by OT score).
    Remaining pages after page-0 are fetched in parallel.
    """
    disease_id, resolved_name = await resolve_disease_id(disease_name)

    effective_page_size = min(page_size, max_rows) if max_rows > 0 else page_size

    # --- Page 0: determines total count and which query variant works ---
    use_v2 = True
    query = DISEASE_TARGETS_PAGED_QUERY_V2
    try:
        data = await _ot.run(query, {"id": disease_id, "index": 0, "size": effective_page_size})
    except Exception as e:
        if use_v2 and "datatypeScores" in str(e):
            logger.warning("datatypeScores unavailable; falling back to legacy query")
            use_v2 = False
            query = DISEASE_TARGETS_PAGED_QUERY
            data = await _ot.run(query, {"id": disease_id, "index": 0, "size": effective_page_size})
        else:
            raise

    d0 = data.get("disease") or {}
    assoc0 = d0.get("associatedTargets") or {}
    first_rows: List[Dict[str, Any]] = assoc0.get("rows") or []
    total: int = assoc0.get("count") or 0

    logger.info(
        "[DISEASE_TARGETS] disease_id=%s page0_count=%d page0_rows=%d",
        disease_id, total, len(first_rows),
    )

    if not first_rows:
        base_cols = ["disease_id", "disease_name", "gene_id", "gene_symbol", "target_name",
                     "gene_biotype", "association_score"]
        return pd.DataFrame(columns=base_cols + _DATATYPE_COLS)

    effective_total = min(total, max_rows) if max_rows > 0 else total

    if effective_total <= len(first_rows):
        all_rows = first_rows[:effective_total]
    else:
        remaining = effective_total - len(first_rows)
        n_pages = (remaining + effective_page_size - 1) // effective_page_size

        logger.info(
            "[DISEASE_TARGETS] effective_total=%d remaining=%d n_pages=%d",
            effective_total, remaining, n_pages,
        )

        async def _fetch_page(idx: int) -> List[Dict[str, Any]]:
            d = await _ot.run(query, {"id": disease_id, "index": idx, "size": effective_page_size})
            page_rows = ((d.get("disease") or {}).get("associatedTargets") or {}).get("rows") or []
            logger.debug("[DISEASE_TARGETS] page idx=%d returned %d rows", idx, len(page_rows))
            return page_rows

        extra = await asyncio.gather(*[_fetch_page(i + 1) for i in range(n_pages)])
        all_rows = list(first_rows)
        for page_rows in extra:
            all_rows.extend(page_rows)
        if max_rows > 0:
            all_rows = all_rows[:max_rows]

    records = [
        {
            "disease_id": disease_id,
            "disease_name": resolved_name,
            "gene_id": (r.get("target") or {}).get("id"),
            "gene_symbol": (r.get("target") or {}).get("approvedSymbol"),
            "target_name": (r.get("target") or {}).get("approvedName"),
            "gene_biotype": (r.get("target") or {}).get("biotype"),
            "association_score": r.get("score"),
            **_unpack_datatype_scores(r.get("datatypeScores") or []),
        }
        for r in all_rows
    ]
    return pd.DataFrame.from_records(records)


async def get_disease_enriched_info(disease_name_or_id: str) -> Dict[str, Any]:
    """
    Fetch rich disease annotation: therapeutic areas, phenotypes (HPO), synonyms, dbXRefs.
    Returns a flat dict suitable for inclusion in TableOutput metadata.
    """
    disease_id, disease_name = await resolve_disease_id(disease_name_or_id)

    try:
        data = await _ot.run(DISEASE_INFO_QUERY, {"id": disease_id})
    except Exception:
        logger.exception("DISEASE_INFO_QUERY failed for %s", disease_id)
        return {}

    dis = data.get("disease") or {}
    if not dis:
        return {}

    # Therapeutic areas
    therapeutic_areas = [
        ta.get("name") for ta in (dis.get("therapeuticAreas") or []) if ta.get("name")
    ]

    # HPO phenotypes — each row has phenotypeHPO and/or phenotypeEFO
    phenotypes = []
    for row in ((dis.get("phenotypes") or {}).get("rows") or []):
        hpo = row.get("phenotypeHPO") or {}
        efo = row.get("phenotypeEFO") or {}
        name = hpo.get("name") or efo.get("name")
        if name:
            phenotypes.append(name)

    # Synonyms — OT returns list of {relation, terms[]}
    all_synonyms: List[str] = []
    for syn in (dis.get("synonyms") or []):
        all_synonyms.extend(syn.get("terms") or [])

    # Cross-references
    db_xrefs = dis.get("dbXRefs") or []

    # ── ADDED 2026-05 (Option A full-coverage refresh) ───────────────────
    # Ontology hierarchy: parents/children return Disease objects with id+name.
    parents = [{"id": p.get("id"), "name": p.get("name")}
               for p in (dis.get("parents") or []) if p.get("id")]
    children = [{"id": c.get("id"), "name": c.get("name")}
                for c in (dis.get("children") or []) if c.get("id")]
    # ancestors/descendants are flat string lists (IDs only)
    ancestors = dis.get("ancestors") or []
    descendants = dis.get("descendants") or []
    resolved_ancestors = [{"id": a.get("id"), "name": a.get("name")}
                          for a in (dis.get("resolvedAncestors") or []) if a.get("id")]

    similar_diseases = [
        {"score": s.get("score"),
         "efo_id": (s.get("object") or {}).get("id"),
         "name": (s.get("object") or {}).get("name")}
        for s in (dis.get("similarEntities") or [])
    ]

    otar_projects = [
        {"otar_code": p.get("otarCode"), "project_name": p.get("projectName"),
         "reference": p.get("reference"), "in_ppp": p.get("integratesInPPP")}
        for p in (dis.get("otarProjects") or [])
    ]

    return {
        "description": dis.get("description"),
        "is_therapeutic_area": dis.get("isTherapeuticArea"),
        "therapeutic_areas": therapeutic_areas,
        "phenotypes_hpo": phenotypes[:50],
        "synonyms": list(dict.fromkeys(all_synonyms)),
        "db_xrefs": db_xrefs,
        # ── new fields ──
        "parents": parents,
        "children_subtypes": children,
        "ancestors_ids": ancestors[:50],
        "descendants_ids": descendants[:200],
        "resolved_ancestors": resolved_ancestors[:30],
        "direct_location_ids": dis.get("directLocationIds") or [],
        "indirect_location_ids": (dis.get("indirectLocationIds") or [])[:50],
        # ADDED 2026-05-18 — full UBERON objects (id + name) so callers
        # don't need a second round-trip to resolve location names.
        "direct_locations": [
            {"id": loc.get("id"), "name": loc.get("name")}
            for loc in (dis.get("directLocations") or []) if loc.get("id")
        ],
        "indirect_locations": [
            {"id": loc.get("id"), "name": loc.get("name")}
            for loc in (dis.get("indirectLocations") or []) if loc.get("id")
        ][:50],
        "similar_diseases": similar_diseases,
        "literature_mention_count": ((dis.get("literatureOcurrences") or {}).get("count")),
        "otar_projects": otar_projects,
    }
