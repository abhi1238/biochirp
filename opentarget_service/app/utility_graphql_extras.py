"""
Lightweight passthrough tool for Open Targets Platform GraphQL root queries
that are NOT covered by target_tool / disease_tool / drug_tool.

Exposes a single function_tool, `opentargets_graphql_tool`, that maps a
query_type string + JSON params to one of the verified queries in
`graphql.py` and returns the raw JSON response. No NER, fuzzy resolution,
ontology expansion, or CSV publishing — callers pass IDs directly.

Supported query_type values:
    meta
    credible_set                 params: {"id": "<studyLocusId>"}
    credible_sets                params: {"index":0,"size":10,"studyIds":[...], ...}
    variant                      params: {"id": "<variantId>"}
    study                        params: {"id": "<studyId>"}
    studies                      params: {"index":0,"size":10,"diseaseIds":[...]}
    targets_batch                params: {"ids": ["ENSG..."]}
    diseases_batch               params: {"ids": ["EFO_..."]}
    drugs_batch                  params: {"ids": ["CHEMBL..."]}
    map_ids                      params: {"terms":["TP53"], "entities":["target"]}
    facets                       params: {"queryString":"breast","index":0,"size":10}
    association_datasources
    interaction_resources
    gene_ontology_terms          params: {"ids": ["GO:0008150"]}
    clinical_report              params: {"id": "<clinicalReportId>"}
    clinical_reports             params: {"ids": ["NCT..."]}
    search                       params: {"queryString":"TP53","entities":["target"],"index":0,"size":10}
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from agents import function_tool

from .client import OTGraphQLClient
from .config import OTClientConfig
from .graphql import (
    META_QUERY,
    CREDIBLESET_QUERY,
    CREDIBLESETS_QUERY,
    VARIANT_INFO_QUERY,
    STUDY_INFO_QUERY,
    STUDIES_BATCH_QUERY,
    TARGETS_BATCH_QUERY,
    DISEASES_BATCH_QUERY,
    DRUGS_BATCH_QUERY,
    MAP_IDS_QUERY,
    FACETS_QUERY,
    ASSOCIATION_DATASOURCES_QUERY,
    INTERACTION_RESOURCES_QUERY,
    GENE_ONTOLOGY_TERMS_QUERY,
    CLINICAL_REPORT_QUERY,
    CLINICAL_REPORTS_QUERY,
    SEARCH_QUERY_TOOL,
)

logger = logging.getLogger("uvicorn.error").getChild("opentargets.graphql_extras")

_client: Optional[OTGraphQLClient] = None


def _get_client() -> OTGraphQLClient:
    global _client
    if _client is None:
        _client = OTGraphQLClient(OTClientConfig())
    return _client


_RSID_RE = re.compile(r"^rs\d+$", re.IGNORECASE)


async def _resolve_variant_id(raw: str) -> Optional[str]:
    """OT's `variant(variantId:)` is keyed by the chr_pos_ref_alt id, NOT by rsID,
    so passing an rsID returns null. Resolve an rsID to its canonical variant id
    via the search index first. Returns None if unresolved (caller leaves the id
    untouched so the behaviour is unchanged for already-canonical ids)."""
    q = ('query($q:String!){search(queryString:$q,entityNames:["variant"])'
         '{hits{id name entity}}}')
    data = await _get_client().run(q, {"q": raw})
    hits = (data.get("search") or {}).get("hits") or []
    return hits[0]["id"] if hits else None


def _build(query_type: str, params: Dict[str, Any]):
    qt = (query_type or "").strip().lower()
    if qt == "meta":
        return META_QUERY, {}
    if qt == "credible_set":
        return CREDIBLESET_QUERY, {"id": params["id"]}
    if qt == "credible_sets":
        return CREDIBLESETS_QUERY, {
            "index": int(params.get("index", 0)),
            "size": int(params.get("size", 10)),
            "studyLocusIds": params.get("studyLocusIds"),
            "studyIds": params.get("studyIds"),
            "variantIds": params.get("variantIds"),
            "studyTypes": params.get("studyTypes"),
            "regions": params.get("regions"),
        }
    if qt == "variant":
        return VARIANT_INFO_QUERY, {"variantId": params["id"]}
    if qt == "study":
        return STUDY_INFO_QUERY, {"studyId": params["id"]}
    if qt == "studies":
        return STUDIES_BATCH_QUERY, {
            "index": int(params.get("index", 0)),
            "size": int(params.get("size", 10)),
            "studyId": params.get("studyId"),
            "diseaseIds": params.get("diseaseIds"),
            "enableIndirect": params.get("enableIndirect"),
        }
    if qt == "targets_batch":
        return TARGETS_BATCH_QUERY, {"ids": list(params["ids"])}
    if qt == "diseases_batch":
        return DISEASES_BATCH_QUERY, {"ids": list(params["ids"])}
    if qt == "drugs_batch":
        return DRUGS_BATCH_QUERY, {"ids": list(params["ids"])}
    if qt == "map_ids":
        return MAP_IDS_QUERY, {
            "terms": list(params["terms"]),
            "entities": params.get("entities"),
        }
    if qt == "facets":
        return FACETS_QUERY, {
            "queryString": params.get("queryString"),
            "entities": params.get("entities"),
            "category": params.get("category"),
            "index": int(params.get("index", 0)),
            "size": int(params.get("size", 10)),
        }
    if qt == "association_datasources":
        return ASSOCIATION_DATASOURCES_QUERY, {}
    if qt == "interaction_resources":
        return INTERACTION_RESOURCES_QUERY, {}
    if qt == "gene_ontology_terms":
        return GENE_ONTOLOGY_TERMS_QUERY, {"ids": list(params["ids"])}
    if qt == "clinical_report":
        # Open Targets clinicalReport IDs are lowercase (e.g. "nct04739566");
        # callers commonly pass uppercase "NCT…", which silently returns null.
        return CLINICAL_REPORT_QUERY, {"id": str(params["id"]).lower()}
    if qt == "clinical_reports":
        return CLINICAL_REPORTS_QUERY, {"ids": [str(i).lower() for i in params["ids"]]}
    if qt == "search":
        return SEARCH_QUERY_TOOL, {
            "queryString": params["queryString"],
            "entities": params.get("entities"),
            "index": int(params.get("index", 0)),
            "size": int(params.get("size", 10)),
        }
    raise ValueError(f"Unsupported query_type: {query_type}")


@function_tool(
    strict_mode=False,
    name_override="opentargets_graphql_tool",
    description_override=(
        "Passthrough to Open Targets Platform GraphQL root queries not covered "
        "by target_tool/disease_tool/drug_tool. "
        "Supports: meta, credible_set, credible_sets, variant, study, studies, "
        "targets_batch, diseases_batch, drugs_batch, map_ids, facets, "
        "association_datasources, interaction_resources, gene_ontology_terms, "
        "clinical_report. Returns raw JSON. "
        "Examples: query_type='meta'; "
        "query_type='variant', params_json='{\"id\":\"1_55039774_C_T\"}' (an rsID "
        "like 'rs429358' is also accepted and resolved automatically); "
        "query_type='targets_batch', params_json='{\"ids\":[\"ENSG00000141510\"]}'; "
        "query_type='map_ids', params_json='{\"terms\":[\"TP53\",\"aspirin\"]}'."
    ),
)
async def opentargets_graphql_tool(
    query_type: str,
    params_json: Optional[str] = None,
) -> str:
    """Run a single Open Targets Platform GraphQL query by name and return JSON string."""
    try:
        params: Dict[str, Any] = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"invalid params_json: {e}"})
    # Variant lookups: accept an rsID by resolving it to OT's chr_pos_ref_alt id
    # first (the `variant` root query rejects rsIDs and would return null).
    resolved_rsid = None
    if (query_type or "").strip().lower() == "variant":
        _vid = str(params.get("id", "")).strip()
        if _RSID_RE.match(_vid):
            try:
                _canon = await _resolve_variant_id(_vid)
            except Exception as e:  # noqa: BLE001 — fall through with the raw id
                logger.warning("variant rsID resolve failed for %s: %r", _vid, e)
                _canon = None
            if _canon:
                resolved_rsid = {"from": _vid, "to": _canon}
                params = {**params, "id": _canon}
    try:
        query, variables = _build(query_type, params)
    except (KeyError, ValueError) as e:
        return json.dumps({"ok": False, "error": str(e)})
    try:
        data = await _get_client().run(query, variables)
        out = {"ok": True, "query_type": query_type, "data": data}
        if resolved_rsid:
            out["resolved_variant_id"] = resolved_rsid
        return json.dumps(out)
    except Exception as e:
        logger.warning("opentargets_graphql_tool failed for %s: %r", query_type, e)
        return json.dumps({"ok": False, "query_type": query_type, "error": repr(e)})
