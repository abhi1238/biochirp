"""Target↔disease evidence (per datasource) for the OpenTargets evidence_tool.

Open Targets associations are backed by individual *evidence* records from many
datasources (genetics, somatic mutations, literature/text-mining, animal models,
pathways, expression, …). The heavy tools only surface the aggregated
association score; this fetches the underlying per-source evidence rows that
answer "what is the evidence linking gene X to disease Y?".
"""
from typing import Any, Optional, Tuple

import pandas as pd

from .client import OTGraphQLClient
from .config import OTClientConfig
from .resolvers import resolve_target_id, resolve_disease_id

_ot = OTGraphQLClient(OTClientConfig())

EVIDENCE_QUERY = """
query($efoId:String!, $ensg:[String!]!, $ds:[String!], $size:Int!){
  disease(efoId:$efoId){
    id name
    evidences(ensemblIds:$ensg, datasourceIds:$ds, size:$size){
      count
      rows {
        score datasourceId datatypeId
        diseaseFromSource literature publicationYear confidence
        clinicalSignificances clinicalStage
        drug { id name }
        target { id approvedSymbol }
      }
    }
  }
}
"""


async def get_target_disease_evidence(
    target_name_or_id: str,
    disease_name_or_id: str,
    datasource: Optional[str] = None,
    size: int = 50,
) -> Tuple[pd.DataFrame, Tuple[str, str], int]:
    """Return (evidence DataFrame, (resolved_target_name, resolved_disease_name),
    total_count) — total is the full GraphQL evidences.count, independent of `size`,
    so the caller can report 'N of TOTAL' honestly when the rows are truncated."""
    tid, tname = await resolve_target_id(target_name_or_id)
    did, dname = await resolve_disease_id(disease_name_or_id)
    if not tid or not did:
        return pd.DataFrame(), (tname or target_name_or_id, dname or disease_name_or_id), 0

    variables: dict[str, Any] = {
        "efoId": did, "ensg": [tid],
        "ds": ([datasource] if datasource else None),
        "size": max(1, min(int(size or 50), 200)),
    }
    data = await _ot.run(EVIDENCE_QUERY, variables)
    dis = (data or {}).get("disease") or {}
    ev = (dis.get("evidences") or {})
    rows = ev.get("rows") or []
    total = ev.get("count")
    if total is None:
        total = len(rows)
    resolved_dname = dis.get("name") or dname

    recs = []
    for r in rows:
        tgt = r.get("target") or {}
        drg = r.get("drug") or {}
        lit = r.get("literature") or []
        cs = r.get("clinicalSignificances")
        recs.append({
            "gene_id": tgt.get("id") or tid,
            "gene_name": tgt.get("approvedSymbol") or tname,
            "disease_id": did,
            "disease_name": resolved_dname,
            "datasource": r.get("datasourceId"),
            "datatype": r.get("datatypeId"),
            "evidence_score": r.get("score"),
            "drug_id": drg.get("id"),
            "drug_name": drg.get("name"),
            "clinical_stage": r.get("clinicalStage"),
            "clinical_significances": (", ".join(cs) if isinstance(cs, list) else cs),
            "confidence": r.get("confidence"),
            "disease_from_source": r.get("diseaseFromSource"),
            "publication_year": r.get("publicationYear"),
            "literature": ", ".join(str(x) for x in lit[:5]),
        })
    return pd.DataFrame(recs), (tname, resolved_dname), int(total)
