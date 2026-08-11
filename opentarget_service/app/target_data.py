import asyncio
from typing import Any, Dict, List

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .dataframe import empty_df, ensure_cols
from .graphql import (
    BASELINE_EXPRESSION_QUERY,
    TARGET_ASSOC_PAGED_QUERY,
    TARGET_ASSOC_PAGED_QUERY_V2,
    TARGET_DRUGS_QUERY_V26,
    TARGET_INFO_QUERY,
    TARGET_PATHWAYS_QUERY,
)
from .resolvers import resolve_target_id
from .uvicorn_logger import setup_logger

logger = setup_logger("biochirp.opentargets.target_data")
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


async def _get_target_drug_rows(target_id: str) -> List[Dict[str, Any]]:
    """
    Return rows normalized to a knownDrugs-like shape:
      {
        phase, status,
        disease: {id, name},
        drug: {...}
      }
    """
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

        # Use primary disease only — expanding all diseases creates one row per
        # drug-disease pair, inflating count beyond OT's unique drug candidate count.
        d = diseases[0]
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


async def get_target_drugs_all(gene_symbol_or_ensembl: str) -> pd.DataFrame:
    """Return all drugs linked to a target via drugAndClinicalCandidates as a flat DataFrame."""
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)
    rows = await _get_target_drug_rows(target_id)
    if not rows:
        return empty_df(extra_cols=["phase", "status"])

    recs: List[Dict[str, Any]] = []
    for r in rows:
        disease = r.get("disease") or {}
        drug = r.get("drug") or {}
        recs.append(
            {
                "gene_id": target_id,
                "gene_symbol": target_name,
                "drug_id": drug.get("id"),
                "drug_name": drug.get("name"),
                "disease_id": disease.get("id"),
                "disease_name": disease.get("name"),
                "phase": r.get("phase"),
                "status": r.get("status"),
            }
        )
    df = pd.DataFrame.from_records(recs)
    return ensure_cols(df, extra_cols=["phase", "status"])


async def get_target_baseline_expression(
    gene_symbol_or_ensembl: str,
    max_rows: int = 200,
) -> pd.DataFrame:
    """Return tissue baseline expression for a target using the OT `baselineExpression` field.

    Fetches up to *max_rows* records (paged at 200 per call) sorted by
    distribution_score. Returns a flat DataFrame with columns:
      gene_symbol, tissue_name, tissue_id, parent_tissue, cell_type, cell_type_id,
      datasource, datatype, median, max, specificity_score, distribution_score, unit
    """
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)
    page_size = min(max_rows, 200)
    data = await _ot.run(BASELINE_EXPRESSION_QUERY, {"id": target_id, "index": 0, "size": page_size})
    tgt = data.get("target") or {}
    bl = tgt.get("baselineExpression") or {}
    raw_rows = bl.get("rows") or []
    total = bl.get("count") or 0

    if not raw_rows:
        return pd.DataFrame(columns=[
            "gene_symbol", "tissue_name", "tissue_id", "parent_tissue",
            "cell_type", "cell_type_id",
            "datasource", "datatype", "median", "max",
            "specificity_score", "distribution_score", "unit",
        ])

    # If total > page_size, fetch remaining pages (cap at max_rows total)
    if total > page_size and max_rows > page_size:
        import math
        n_pages = min(math.ceil(max_rows / page_size), math.ceil(total / page_size))
        extras = await asyncio.gather(*[
            _ot.run(BASELINE_EXPRESSION_QUERY, {"id": target_id, "index": i, "size": page_size})
            for i in range(1, n_pages)
        ], return_exceptions=True)
        for ex in extras:
            if isinstance(ex, Exception):
                continue
            more = ((ex.get("target") or {}).get("baselineExpression") or {}).get("rows") or []
            raw_rows.extend(more)
        raw_rows = raw_rows[:max_rows]

    recs = []
    for r in raw_rows:
        ts = r.get("tissueBiosample") or {}
        tp = r.get("tissueBiosampleParent") or {}
        ct = r.get("celltypeBiosample") or {}
        recs.append({
            "gene_symbol":       target_name,
            "tissue_name":       ts.get("biosampleName") or r.get("tissueBiosampleFromSource"),
            "tissue_id":         ts.get("biosampleId"),
            "parent_tissue":     tp.get("biosampleName"),
            "cell_type":         ct.get("biosampleName") or r.get("celltypeBiosampleFromSource"),
            "cell_type_id":      ct.get("biosampleId"),
            "datasource":        r.get("datasourceId"),
            "datatype":          r.get("datatypeId"),
            "median":            r.get("median"),
            "max":               r.get("max"),
            "specificity_score": r.get("specificity_score"),
            "distribution_score":r.get("distribution_score"),
            "unit":              r.get("unit"),
        })

    df = pd.DataFrame.from_records(recs)
    if "distribution_score" in df.columns:
        df = df.sort_values("distribution_score", ascending=False, na_position="last")
    return df.reset_index(drop=True)


async def get_target_pathways_only(gene_symbol_or_ensembl: str) -> pd.DataFrame:
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    data = await _ot.run(TARGET_PATHWAYS_QUERY, {"id": target_id})
    tgt = data.get("target") or {}
    rows = tgt.get("pathways") or []

    if not rows:
        return pd.DataFrame(
            columns=["gene_id", "gene_symbol", "pathway_id", "pathway_name", "top_level_term"]
        )

    return pd.DataFrame.from_records(
        [
            {
                "gene_id": tgt.get("id") or target_id,
                "gene_symbol": tgt.get("approvedSymbol") or target_name,
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

    # Paged associated diseases — page 0 first to get total, then fetch all remaining in parallel
    use_v2 = True
    assoc_query = TARGET_ASSOC_PAGED_QUERY_V2
    try:
        assoc0_data = await _ot.run(assoc_query, {"id": target_id, "index": 0, "size": _cfg.page_size})
    except Exception as e:
        resp_body = getattr(getattr(e, "response", None), "text", "") or ""
        if "datatypeScores" in resp_body or "componentId" in resp_body:
            logger.warning("datatypeScores unavailable in association query; using legacy")
            use_v2 = False
            assoc_query = TARGET_ASSOC_PAGED_QUERY
            assoc0_data = await _ot.run(assoc_query, {"id": target_id, "index": 0, "size": _cfg.page_size})
        else:
            raise

    assoc_block0 = ((assoc0_data.get("target") or {}).get("associatedDiseases") or {})
    first_assoc_rows = assoc_block0.get("rows") or []
    total_assoc = assoc_block0.get("count") or 0
    assoc_rows: List[Dict[str, Any]] = list(first_assoc_rows)

    if first_assoc_rows and total_assoc > len(assoc_rows):
        remaining_pages = (total_assoc - len(assoc_rows) + _cfg.page_size - 1) // _cfg.page_size

        async def _fetch_assoc_page(idx: int) -> List[Dict[str, Any]]:
            d = await _ot.run(assoc_query, {"id": target_id, "index": idx, "size": _cfg.page_size})
            return ((d.get("target") or {}).get("associatedDiseases") or {}).get("rows") or []

        for batch_start in range(0, remaining_pages, 8):
            indices = range(1 + batch_start, 1 + min(batch_start + 8, remaining_pages))
            results = await asyncio.gather(*[_fetch_assoc_page(i) for i in indices], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    raise r
                assoc_rows.extend(r)

    # Build score maps: overall + per-datatype
    score_map: Dict[str, Any] = {}
    datatype_score_map: Dict[str, Dict[str, Any]] = {}
    for r in assoc_rows:
        did = (r.get("disease") or {}).get("id")
        if did:
            score_map[did] = r.get("score")
            datatype_score_map[did] = _unpack_datatype_scores(r.get("datatypeScores") or [])

    extra_cols = [
        "phase", "status", "action_types", "mechanism_of_action", "association_score",
    ] + _DATATYPE_COLS

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

        did = disease.get("id")
        rec = {
            "gene_id": target_id,
            "gene_symbol": target_name,
            "drug_id": drug.get("id"),
            "drug_name": drug.get("name"),
            "disease_id": did,
            "disease_name": disease.get("name"),
            "phase": r.get("phase"),
            "status": r.get("status"),
            "action_types": ", ".join(sorted(valid_actions)) if valid_actions else None,
            "mechanism_of_action": "; ".join(sorted(valid_mechanisms)) if valid_mechanisms else None,
            "association_score": score_map.get(did),
        }
        rec.update(datatype_score_map.get(did) or {col: None for col in _DATATYPE_COLS})
        recs.append(rec)

    df = pd.DataFrame.from_records(recs)
    return ensure_cols(df, extra_cols=extra_cols)


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


async def get_target_diseases_all(
    gene_symbol_or_ensembl: str, page_size: int = 1000, max_rows: int = 0
) -> pd.DataFrame:
    """
    Fetch diseases associated with a target including per-datatype evidence scores.
    If max_rows > 0, stop after collecting that many rows (returns top results by OT score).
    Remaining pages after page-0 are fetched in parallel.
    """
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    effective_page_size = min(page_size, max_rows) if max_rows > 0 else page_size

    # --- Page 0: determines total count and which query variant works ---
    use_v2 = True
    query = TARGET_ASSOC_PAGED_QUERY_V2
    try:
        data = await _ot.run(query, {"id": target_id, "index": 0, "size": effective_page_size})
    except Exception as e:
        resp_body = getattr(getattr(e, "response", None), "text", "") or ""
        if "datatypeScores" in resp_body or "componentId" in resp_body:
            logger.warning("datatypeScores unavailable; falling back to legacy query")
            use_v2 = False
            query = TARGET_ASSOC_PAGED_QUERY
            data = await _ot.run(query, {"id": target_id, "index": 0, "size": effective_page_size})
        else:
            raise

    assoc = ((data.get("target") or {}).get("associatedDiseases") or {})
    first_rows: List[Dict[str, Any]] = assoc.get("rows") or []
    total: int = assoc.get("count") or 0

    if not first_rows:
        cols = ["gene_id", "gene_symbol", "disease_id", "disease_name", "association_score"]
        return pd.DataFrame(columns=cols + _DATATYPE_COLS)

    effective_total = min(total, max_rows) if max_rows > 0 else total

    if effective_total <= len(first_rows):
        all_rows = first_rows[:effective_total]
    else:
        # Fire remaining pages in parallel
        remaining = effective_total - len(first_rows)
        n_pages = (remaining + effective_page_size - 1) // effective_page_size

        async def _fetch_page(idx: int) -> List[Dict[str, Any]]:
            d = await _ot.run(query, {"id": target_id, "index": idx, "size": effective_page_size})
            return ((d.get("target") or {}).get("associatedDiseases") or {}).get("rows") or []

        extra = await asyncio.gather(*[_fetch_page(i + 1) for i in range(n_pages)])
        all_rows = list(first_rows)
        for page_rows in extra:
            all_rows.extend(page_rows)
        if max_rows > 0:
            all_rows = all_rows[:max_rows]

    records = [
        {
            "gene_id": target_id,
            "gene_symbol": target_name,
            "disease_id": (r.get("disease") or {}).get("id"),
            "disease_name": (r.get("disease") or {}).get("name"),
            "association_score": r.get("score"),
            **_unpack_datatype_scores(r.get("datatypeScores") or []),
        }
        for r in all_rows
    ]
    return pd.DataFrame.from_records(records)


async def get_target_biological_info(gene_symbol_or_ensembl: str) -> Dict[str, Any]:
    """
    Fetch comprehensive biological annotation for a target from OpenTargets Platform.
    Covers: identifiers, location, function, GO, tractability, constraint, safety,
    expression, mouse phenotypes, pharmacogenomics, prioritisation, hallmarks,
    homologues, chemical probes, and DepMap essentiality.
    Returns a dict for inclusion in TableOutput.metadata.
    """
    target_id, target_name = await resolve_target_id(gene_symbol_or_ensembl)

    try:
        data = await _ot.run(TARGET_INFO_QUERY, {"id": target_id})
    except Exception:
        logger.exception("TARGET_INFO_QUERY failed for %s", target_id)
        return {}

    tgt = data.get("target") or {}
    if not tgt:
        return {}

    # ── Synonyms & cross-references ──────────────────────────────────────────
    symbol_syns = [s.get("label") for s in (tgt.get("symbolSynonyms") or []) if s.get("label")]
    name_syns = [s.get("label") for s in (tgt.get("nameSynonyms") or []) if s.get("label")]
    all_syns = [s.get("label") for s in (tgt.get("synonyms") or []) if s.get("label")]
    db_xrefs = [{"source": x.get("source"), "id": x.get("id")} for x in (tgt.get("dbXrefs") or []) if x.get("id")]
    protein_ids = [{"source": x.get("source"), "id": x.get("id")} for x in (tgt.get("proteinIds") or []) if x.get("id")]
    # Canonical UniProt accession from the FULL list (computed before the [:10] cap
    # below, since the swissprot entry is not guaranteed to be in the first 10).
    # AlphaFold DB is keyed by this accession; prefer reviewed swissprot, else trembl.
    uniprot_accession = (
        next((p["id"] for p in protein_ids
              if str(p.get("source", "")).lower() == "uniprot_swissprot"), None)
        or next((p["id"] for p in protein_ids
                 if "uniprot" in str(p.get("source", "")).lower()), None)
    )

    # ── Subcellular locations (correct field names: location, termSL, labelSL) ──
    subcel_seen: set = set()
    subcel = []
    for loc in (tgt.get("subcellularLocations") or []):
        label = (loc.get("location") or loc.get("labelSL") or "").strip()
        if label and label not in subcel_seen:
            subcel_seen.add(label)
            subcel.append({"location": label, "swissprot_id": loc.get("termSL"), "source": loc.get("source")})

    # ── Target classes ────────────────────────────────────────────────────────
    target_classes = [
        {"id": tc.get("id"), "label": tc.get("label"), "level": tc.get("level")}
        for tc in (tgt.get("targetClass") or []) if tc.get("label")
    ]

    # ── Tractability ──────────────────────────────────────────────────────────
    tractability: Dict[str, Any] = {}
    for tr in (tgt.get("tractability") or []):
        modality = tr.get("modality") or "unknown"
        label = tr.get("label")
        val = tr.get("value")
        if label:
            tractability.setdefault(modality, {})[label] = val

    # ── Genomic location ─────────────────────────────────────────────────────
    gloc = tgt.get("genomicLocation") or {}
    genomic_location = {
        "chromosome": gloc.get("chromosome"),
        "start": gloc.get("start"),
        "end": gloc.get("end"),
        "strand": gloc.get("strand"),
    } if gloc else None

    # ── gnomAD genetic constraint (corrected: geneticConstraint not constraint) ──
    constraint = [
        {
            "type": c.get("constraintType"),
            "score": c.get("score"),
            "exp": c.get("exp"),
            "obs": c.get("obs"),
            "oe": c.get("oe"),
            "oe_lower": c.get("oeLower"),
            "oe_upper": c.get("oeUpper"),
        }
        for c in (tgt.get("geneticConstraint") or [])
        if c.get("constraintType")
    ]

    # ── Gene Ontology (corrected: geneOntology, not go; aspect on entry not term) ──
    go_bp: List[str] = []
    go_mf: List[str] = []
    go_cc: List[str] = []
    seen_go: set = set()
    for entry in (tgt.get("geneOntology") or []):
        term = entry.get("term") or {}
        label = term.get("label") or ""
        tid = term.get("id") or ""
        aspect = entry.get("aspect") or ""
        key = (tid, aspect)
        if not label or key in seen_go:
            continue
        seen_go.add(key)
        if aspect == "P":
            go_bp.append(label)
        elif aspect == "F":
            go_mf.append(label)
        elif aspect == "C":
            go_cc.append(label)

    # ── Safety liabilities (corrected field names) ────────────────────────────
    safety = []
    for s in (tgt.get("safetyLiabilities") or []):
        if not s.get("event"):
            continue
        effects = [{"direction": e.get("direction"), "dosing": e.get("dosing")} for e in (s.get("effects") or [])]
        biosamples = [{"tissue": b.get("tissueLabel"), "tissue_id": b.get("tissueId"), "cell": b.get("cellLabel"), "cell_format": b.get("cellFormat")} for b in (s.get("biosamples") or [])]
        studies = [{"name": st.get("name"), "type": st.get("type"), "description": st.get("description")} for st in (s.get("studies") or [])]
        safety.append({
            "event": s.get("event"),
            "event_id": s.get("eventId"),
            "datasource": s.get("datasource"),
            "url": s.get("url"),
            "effects": effects,
            "biosamples": biosamples,
            "studies": studies,
        })

    # ── Tissue/RNA expressions ────────────────────────────────────────────────
    expressions = []
    for ex in (tgt.get("expressions") or []):
        tissue = ex.get("tissue") or {}
        rna = ex.get("rna") or {}
        prot = ex.get("protein") or {}
        if not tissue.get("label"):
            continue
        expressions.append({
            "tissue_id": tissue.get("id"),
            "tissue_label": tissue.get("label"),
            "anatomical_systems": tissue.get("anatomicalSystems") or [],
            "rna_value": rna.get("value"),
            "rna_zscore": rna.get("zscore"),
            "rna_unit": rna.get("unit"),
            "protein_level": prot.get("level"),
            "protein_reliability": prot.get("reliability"),
        })

    # Open Targets returns tissues in a fixed anatomical order, NOT by expression
    # level. The downstream annotation preview caps this list (_slice, top 15), so
    # without sorting the most-expressed tissues get truncated out and the model
    # cannot answer "where is gene X most highly expressed". Sort by RNA value
    # (then protein level) descending so the cap keeps the strongest signals.
    # Generic — no per-gene/per-tissue assumptions.
    _PROT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Not detected": 0}
    expressions.sort(
        key=lambda e: (
            e["rna_value"] if isinstance(e.get("rna_value"), (int, float)) else -1.0,
            _PROT_RANK.get(e.get("protein_level"), -1),
        ),
        reverse=True,
    )

    # ── Mouse phenotypes ──────────────────────────────────────────────────────
    mouse_phenotypes = [
        {
            "phenotype_id": mp.get("modelPhenotypeId"),
            "phenotype_label": mp.get("modelPhenotypeLabel"),
            "target_in_model": mp.get("targetInModel"),
        }
        for mp in (tgt.get("mousePhenotypes") or [])
        if mp.get("modelPhenotypeLabel")
    ]

    # ── Pharmacogenomics ──────────────────────────────────────────────────────
    pgx = [
        {
            "category": pgx_entry.get("pgxCategory"),
            "evidence_level": pgx_entry.get("evidenceLevel"),
            "variant_id": pgx_entry.get("variantId"),
            "variant_rsid": pgx_entry.get("variantRsId"),
            "genotype": pgx_entry.get("genotype"),
            "phenotype": pgx_entry.get("phenotypeText"),
            "annotation": pgx_entry.get("genotypeAnnotationText"),
            "is_direct_target": pgx_entry.get("isDirectTarget"),
            "datasource": pgx_entry.get("datasourceId"),
            "drugs": [{"id": d.get("drugId"), "name": d.get("drugFromSource")} for d in (pgx_entry.get("drugs") or [])],
        }
        for pgx_entry in (tgt.get("pharmacogenomics") or [])
        if pgx_entry.get("phenotypeText") or pgx_entry.get("pgxCategory")
    ]

    # ── Target prioritisation ─────────────────────────────────────────────────
    prioritisation = {
        item.get("key"): item.get("value")
        for item in ((tgt.get("prioritisation") or {}).get("items") or [])
        if item.get("key")
    }

    # ── Cancer hallmarks ──────────────────────────────────────────────────────
    hallmarks_obj = tgt.get("hallmarks") or {}
    cancer_hallmarks = [
        {"label": h.get("label"), "impact": h.get("impact"), "pmid": h.get("pmid"), "description": h.get("description")}
        for h in (hallmarks_obj.get("cancerHallmarks") or [])
        if h.get("label")
    ]
    hallmark_attributes = [
        {"name": a.get("name"), "description": a.get("description"), "pmid": a.get("pmid")}
        for a in (hallmarks_obj.get("attributes") or [])
        if a.get("name")
    ]

    # ── Homologues ────────────────────────────────────────────────────────────
    homologues = [
        {
            "gene_id": h.get("targetGeneId"),
            "gene_symbol": h.get("targetGeneSymbol"),
            "species": h.get("speciesName"),
            "species_id": h.get("speciesId"),
            "homology_type": h.get("homologyType"),
            "is_high_confidence": h.get("isHighConfidence"),
            "query_identity_pct": h.get("queryPercentageIdentity"),
            "target_identity_pct": h.get("targetPercentageIdentity"),
        }
        for h in (tgt.get("homologues") or [])
        if h.get("targetGeneSymbol")
    ]

    # ── Chemical probes ────────────────────────────────────────────────────────
    chemical_probes = [
        {
            "id": cp.get("id"),
            "mechanism": cp.get("mechanismOfAction"),
            "is_high_quality": cp.get("isHighQuality"),
            "probes_drugs_score": cp.get("probesDrugsScore"),
            "probe_miner_score": cp.get("probeMinerScore"),
            "origin": cp.get("origin"),
            "drug_id": cp.get("drugId"),
        }
        for cp in (tgt.get("chemicalProbes") or [])
        if cp.get("id")
    ]

    # ── DepMap essentiality ────────────────────────────────────────────────────
    depmap = [
        {
            "tissue_id": dm.get("tissueId"),
            "tissue_name": dm.get("tissueName"),
            "screen_count": len(dm.get("screens") or []),
            "avg_gene_effect": (
                sum(s.get("geneEffect", 0) or 0 for s in (dm.get("screens") or []) if s.get("geneEffect") is not None)
                / max(1, sum(1 for s in (dm.get("screens") or []) if s.get("geneEffect") is not None))
            ) if dm.get("screens") else None,
        }
        for dm in (tgt.get("depMapEssentiality") or [])
        if dm.get("tissueName")
    ]

    # ── TEP ───────────────────────────────────────────────────────────────────
    tep = tgt.get("tep")

    return {
        # Core identifiers
        "ensembl_id": tgt.get("id"),
        "approved_symbol": tgt.get("approvedSymbol"),
        "approved_name": tgt.get("approvedName"),
        "biotype": tgt.get("biotype"),
        "is_essential": tgt.get("isEssential"),
        "function_descriptions": tgt.get("functionDescriptions") or [],
        # Synonyms & XRefs
        "symbol_synonyms": symbol_syns,
        "name_synonyms": name_syns,
        "all_synonyms": all_syns,
        "db_xrefs": db_xrefs,
        "protein_ids": protein_ids[:10],
        "uniprot_accession": uniprot_accession,
        # Location
        "genomic_location": genomic_location,
        "subcellular_locations": subcel,
        # Classification
        "target_classes": target_classes,
        # Druggability
        "tractability": tractability,
        "chemical_probes_count": len(chemical_probes),
        "high_quality_probes": [p for p in chemical_probes if p.get("is_high_quality")],
        "all_chemical_probes": chemical_probes[:20],
        # Genetic constraint (gnomAD)
        "genetic_constraint": constraint,
        # GO
        "go_biological_process": go_bp[:40],
        "go_molecular_function": go_mf[:40],
        "go_cellular_component": go_cc[:25],
        # Expression
        "tissue_expressions": expressions,
        # Safety
        "safety_liabilities": safety,
        # Mouse phenotypes
        "mouse_phenotypes_count": len(mouse_phenotypes),
        "mouse_phenotypes": mouse_phenotypes[:30],
        # Pharmacogenomics
        "pharmacogenomics_count": len(pgx),
        "pharmacogenomics": pgx[:20],
        # Prioritisation
        "prioritisation": prioritisation,
        # Cancer hallmarks
        "cancer_hallmarks": cancer_hallmarks,
        "hallmark_attributes": hallmark_attributes,
        # Homologues
        "homologues_count": len(homologues),
        "human_paralogs": [h for h in homologues if h.get("species") == "Human"],
        "mouse_orthologs": [h for h in homologues if "mouse" in (h.get("species") or "").lower() or h.get("species_id") == "10090"],
        # DepMap
        "depmap_essentiality": depmap,
        # TEP
        "tep": {"name": tep.get("name"), "therapeutic_area": tep.get("therapeuticArea"), "uri": tep.get("uri")} if tep else None,

        # ── ADDED 2026-05 (Option A full-coverage refresh) ──────────────────
        # Protein-protein interactions from OT (IntAct + Reactome + STRINGdb)
        "interactions_total": ((tgt.get("interactions") or {}).get("count")),
        "interactions_top": [
            {
                "partner_a": r.get("intA"),
                "role_a":    r.get("intABiologicalRole"),
                "partner_b": r.get("intB"),
                "role_b":    r.get("intBBiologicalRole"),
                "score":     r.get("score"),
                "evidence_count": r.get("count"),
                "source":    r.get("sourceDatabase"),
            }
            for r in ((tgt.get("interactions") or {}).get("rows") or [])
        ],
        # Similar targets (OT embedding-based neighbours)
        "similar_targets": [
            {"score": s.get("score"),
             "ensembl_id": (s.get("object") or {}).get("id"),
             "approved_symbol": (s.get("object") or {}).get("approvedSymbol"),
             "approved_name": (s.get("object") or {}).get("approvedName")}
            for s in (tgt.get("similarEntities") or [])
        ],
        # Literature mention count
        "literature_mention_count": ((tgt.get("literatureOcurrences") or {}).get("count")),
        # Pseudogenes / paralogous gene IDs
        "alternative_genes": tgt.get("alternativeGenes") or [],
        # Ensembl transcript IDs
        "transcript_ids": tgt.get("transcriptIds") or [],

        # ── ADDED 2026-05-18 (deeper sub-field coverage) ────────────────
        # GWAS credible sets colocalising with this target.
        "credible_sets_count": ((tgt.get("credibleSets") or {}).get("count")),
        "credible_sets": [
            {
                "study_locus_id":   r.get("studyLocusId"),
                "study_id":         r.get("studyId"),
                "study_type":       r.get("studyType"),
                "chromosome":       r.get("chromosome"),
                "position":         r.get("position"),
                "p_value_mantissa": r.get("pValueMantissa"),
                "p_value_exponent": r.get("pValueExponent"),
                "finemapping_method": r.get("finemappingMethod"),
                "beta":             r.get("beta"),
                "z_score":          r.get("zScore"),
            }
            for r in ((tgt.get("credibleSets") or {}).get("rows") or [])
        ],
        # Full transcript list (canonicalTranscript already exposed separately).
        "transcripts": [
            {
                "transcript_id":         t.get("transcriptId"),
                "translation_id":        t.get("translationId"),
                "biotype":               t.get("biotype"),
                "is_ensembl_canonical":  t.get("isEnsemblCanonical"),
                "is_uniprot_reviewed":   t.get("isUniprotReviewed"),
                "uniprot_id":            t.get("uniprotId"),
                "uniprot_isoform_id":    t.get("uniprotIsoformId"),
                "alphafold_id":          t.get("alphafoldId"),
            }
            for t in (tgt.get("transcripts") or [])
            if t.get("transcriptId")
        ],
        # Protein-coding coordinates (per-residue annotations).
        "protein_coding_coordinates_count": ((tgt.get("proteinCodingCoordinates") or {}).get("count")),
        "protein_coding_coordinates": [
            {
                "amino_acid_position":   r.get("aminoAcidPosition"),
                "reference_amino_acid":  r.get("referenceAminoAcid"),
                "alternate_amino_acid":  r.get("alternateAminoAcid"),
                "uniprot_accessions":    r.get("uniprotAccessions"),
                "variant_effect":        r.get("variantEffect"),
                "datasources": [
                    {"id": ds.get("datasourceId"),
                     "name": ds.get("datasourceNiceName"),
                     "count": ds.get("datasourceCount")}
                    for ds in (r.get("datasources") or [])
                ],
            }
            for r in ((tgt.get("proteinCodingCoordinates") or {}).get("rows") or [])
        ],
    }
