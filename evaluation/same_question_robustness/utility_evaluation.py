import pandas as pd
from typing import Optional, Any, Dict, Tuple
import utility  # evaluation/same_question_robustness/utility.py


def _norm(x: Any) -> str:
    return "" if x is None else str(x).strip().lower()


def _is_missing(x: Any) -> bool:
    if x is None:
        return True
    if pd.isna(x):
        return True
    return isinstance(x, str) and not x.strip()


async def resolve_drug_ids_with_source(
    df_drug: pd.DataFrame,
    groq_api_key: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Resolution policy:
    1) First pass: utility.get_chembl_ids_fast on raw names.
       Preserve resolver-reported source per row (e.g., 'OpenTargets' or 'Llama').
    2) Second pass only for unresolved rows:
       Groq canonicalization + OpenTargets ID lookup.
       If resolved here -> source = 'Groq'

    Returns:
    - updated_df
    - audit_df (row-level trace)
    """
    if "drug_name" not in df_drug.columns:
        raise ValueError("df_drug must contain 'drug_name' column")

    out = df_drug.copy()
    if "drug_id" not in out.columns:
        out["drug_id"] = None
    if "source" not in out.columns:
        out["source"] = None

    # ---------- Pass 1: OpenTargets on raw names ----------
    raw_names = [
        str(x).strip()
        for x in out["drug_name"].dropna().tolist()
        if str(x).strip()
    ]
    raw_names_unique = list(dict.fromkeys(raw_names))

    pass1 = await utility.get_chembl_ids_fast(raw_names_unique)  # drug_name, drug_id, source
    pass1 = pass1.copy()
    if "source" not in pass1.columns:
        pass1["source"] = None
    pass1["drug_name_norm"] = pass1["drug_name"].map(_norm)
    pass1 = pass1.dropna(subset=["drug_id"]).drop_duplicates("drug_name_norm", keep="first")
    raw_to_resolution: Dict[str, Tuple[str, Optional[str]]] = {
        row["drug_name_norm"]: (row["drug_id"], row["source"])
        for _, row in pass1.iterrows()
    }

    for idx, row in out.iterrows():
        if _is_missing(row.get("drug_id")):
            resolved = raw_to_resolution.get(_norm(row.get("drug_name")))
            if resolved is not None:
                rid, rsrc = resolved
                out.at[idx, "drug_id"] = rid
                out.at[idx, "source"] = "OpenTargets" if _is_missing(rsrc) else rsrc

    # ---------- Pass 2: Groq canonicalization only for unresolved ----------
    unresolved_mask = out["drug_id"].map(_is_missing) & (~out["drug_name"].map(_is_missing))
    unresolved = out.loc[unresolved_mask, "drug_name"].astype(str).str.strip()
    unresolved_unique = list(dict.fromkeys(unresolved.tolist()))

    if unresolved_unique:
        canonical_input = {name: None for name in unresolved_unique}
        canonical_map = await utility.canonicalise_drug_dict(
            canonical_input,
            groq_api_key=groq_api_key,
        )  # raw_name -> canonical_name_or_None

        canonical_terms = list(
            dict.fromkeys(
                str(v).strip()
                for v in canonical_map.values()
                if isinstance(v, str) and v.strip()
            )
        )

        canonical_to_id: Dict[str, str] = {}
        if canonical_terms:
            pass2 = await utility.get_chembl_ids_fast(canonical_terms)
            pass2 = pass2.copy()
            pass2["drug_name_norm"] = pass2["drug_name"].map(_norm)
            pass2 = pass2.dropna(subset=["drug_id"]).drop_duplicates("drug_name_norm", keep="first")
            canonical_to_id = dict(zip(pass2["drug_name_norm"], pass2["drug_id"]))

        for idx, row in out.loc[unresolved_mask].iterrows():
            raw_name = str(row["drug_name"]).strip()
            canon = canonical_map.get(raw_name)
            if not isinstance(canon, str) or not canon.strip():
                continue
            cid = canonical_to_id.get(_norm(canon))
            if cid is not None:
                out.at[idx, "drug_id"] = cid
                out.at[idx, "source"] = "Groq"

    # ---------- Audit ----------
    audit = out[["drug_name", "drug_id", "source"]].copy()
    return out, audit
