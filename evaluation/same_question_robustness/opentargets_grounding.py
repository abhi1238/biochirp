from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

import pandas as pd


_WS_RE = re.compile(r"\s+")
_PARENS_SYMBOL_RE = re.compile(r"\(([A-Za-z0-9_-]{1,20})\)\s*$")


def _norm_ws(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def normalize_disease_name(name: Any) -> str:
    """
    Normalize disease strings to improve matching across sources.

    Key heuristic: handle common \"Last, First\" or \"Head, Tail\" patterns
    e.g. \"Arthritis, Rheumatoid\" -> \"rheumatoid arthritis\".
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = str(name).strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) == 2:
            s = f"{parts[1]} {parts[0]}"
    s = s.lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\\[\\]{}()]", " ", s)
    s = re.sub(r"[:;]", " ", s)
    s = re.sub(r"[^a-z0-9\\s\\-\\+/]", " ", s)
    return _norm_ws(s)


def normalize_drug_name(name: Any) -> str:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = str(name).strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\\[\\]{}()]", " ", s)
    s = re.sub(r"[^a-z0-9\\s\\-\\+/]", " ", s)
    return _norm_ws(s)


def normalize_gene_name(name: Any) -> str:
    # Gene symbols are usually case-sensitive; normalize to upper.
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = str(name).strip()
    s = s.replace("’", "'")
    s = re.sub(r"[\\[\\]{}]", " ", s)
    s = _norm_ws(s)
    return s.upper()


def normalize_pathway_name(name: Any) -> str:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = str(name).strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\\[\\]{}()]", " ", s)
    s = re.sub(r"[^a-z0-9\\s\\-\\+/]", " ", s)
    return _norm_ws(s)


def extract_gene_symbol(target_name: Any) -> Optional[str]:
    """
    Extract gene symbol from strings like:
      - \"Tyrosine-protein kinase Kit (KIT)\" -> \"KIT\"
      - \"Proto-oncogene c-Ret (RET)\" -> \"RET\"
    """
    if target_name is None or (isinstance(target_name, float) and pd.isna(target_name)):
        return None
    s = str(target_name).strip()
    m = _PARENS_SYMBOL_RE.search(s)
    if m:
        sym = m.group(1).strip()
        return sym.upper() if sym else None
    return None


_NORMALIZERS = {
    "disease": normalize_disease_name,
    "drug": normalize_drug_name,
    "gene": normalize_gene_name,
    "pathway": normalize_pathway_name,
}


@dataclass(frozen=True)
class OpenTargetsNameIndex:
    """
    Lightweight, offline name->id index built from OpenTargets results.

    This is meant to *align* other sources to the IDs OpenTargets uses in your
    local benchmark (avoids online search ambiguity / \"first hit\" issues).
    """

    # entity -> normalized_name -> Counter(id)
    _counters: Mapping[str, Mapping[str, Counter]]

    @classmethod
    def from_opentargets_with_id_pickle(cls, path: str) -> "OpenTargetsNameIndex":
        import pickle

        obj = pickle.load(open(path, "rb"))
        if not isinstance(obj, dict) or not obj:
            raise ValueError(f"Unexpected OpenTargets pickle structure at {path!r}")

        model_key = next(iter(obj.keys()))
        queries = obj[model_key]
        if not isinstance(queries, dict):
            raise ValueError(f"Unexpected OpenTargets pickle structure at {path!r}")

        counters: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))

        def add(entity: str, name_val: Any, id_val: Any) -> None:
            if id_val is None or (isinstance(id_val, float) and pd.isna(id_val)):
                return
            norm = _NORMALIZERS[entity](name_val)
            if not norm:
                return
            counters[entity][norm][str(id_val)] += 1

        for _q, runs in queries.items():
            if not isinstance(runs, dict):
                continue
            for _run_id, payload in runs.items():
                df = payload.get("dataframe")
                if not isinstance(df, pd.DataFrame) or df.empty:
                    continue

                cols = set(df.columns)
                # Common OT pairs
                if {"disease_name", "disease_id"}.issubset(cols):
                    for n, i in zip(df["disease_name"], df["disease_id"]):
                        add("disease", n, i)
                if {"indication_name", "indication_id"}.issubset(cols):
                    for n, i in zip(df["indication_name"], df["indication_id"]):
                        add("disease", n, i)

                if {"drug_name", "drug_id"}.issubset(cols):
                    for n, i in zip(df["drug_name"], df["drug_id"]):
                        add("drug", n, i)

                if {"gene_name", "gene_id"}.issubset(cols):
                    for n, i in zip(df["gene_name"], df["gene_id"]):
                        add("gene", n, i)
                # Some OT tables have `target_name` with `gene_id` (or can be used as alias).
                if {"target_name", "gene_id"}.issubset(cols):
                    for n, i in zip(df["target_name"], df["gene_id"]):
                        add("gene", n, i)

                if {"pathway_name", "pathway_id"}.issubset(cols):
                    for n, i in zip(df["pathway_name"], df["pathway_id"]):
                        add("pathway", n, i)

        return cls(_counters={k: dict(v) for k, v in counters.items()})

    def lookup(self, entity: str, name: Any) -> Optional[str]:
        entity = entity.lower()
        if entity not in self._counters:
            return None
        norm = _NORMALIZERS[entity](name)
        if not norm:
            return None
        c = self._counters[entity].get(norm)
        if not c:
            return None
        # Choose the most common ID for that name in OT results.
        return c.most_common(1)[0][0]

    def stats(self) -> Dict[str, int]:
        return {entity: len(names) for entity, names in self._counters.items()}


def remap_dataframe_ids_to_opentargets(
    df: pd.DataFrame,
    df_id_existing: Optional[pd.DataFrame],
    ot_index: OpenTargetsNameIndex,
    *,
    include_pathway: bool = True,
    fallback_to_existing: bool = True,
) -> pd.DataFrame:
    """
    Build a new `dataframe_id` using OT IDs derived from the *name* columns.

    Rules:
    - Prefer OT index match.
    - If OT index doesn't know the name and `fallback_to_existing=True`, keep the
      existing id value if present.
    - Otherwise emit None.
    """
    cols = set(df.columns)

    out_cols: list[str] = []
    out_data: dict[str, list[Optional[str]]] = {}

    def existing_series(col: str) -> Optional[pd.Series]:
        if not fallback_to_existing or df_id_existing is None:
            return None
        if not isinstance(df_id_existing, pd.DataFrame) or col not in df_id_existing.columns:
            return None
        # If the existing df_id has wrong rowcount, don't try to align it.
        if len(df_id_existing) != len(df):
            return None
        return df_id_existing[col]

    # Gene
    gene_name_col = None
    if "gene_name" in cols:
        gene_name_col = "gene_name"
    elif "target_name" in cols:
        gene_name_col = "target_name"

    if gene_name_col is not None:
        out_cols.append("gene_id")
        ex = existing_series("gene_id")
        vals: list[Optional[str]] = []
        for idx, raw in enumerate(df[gene_name_col].tolist()):
            # Special-case `target_name` like \"... (KIT)\" to improve matches.
            lookup_val = raw
            if gene_name_col == "target_name":
                sym = extract_gene_symbol(raw)
                lookup_val = sym if sym else raw
            mapped = ot_index.lookup("gene", lookup_val)
            if mapped is None and ex is not None and pd.notna(ex.iat[idx]):
                mapped = str(ex.iat[idx])
            vals.append(mapped)
        out_data["gene_id"] = vals

    # Disease
    disease_name_col = None
    if "disease_name" in cols:
        disease_name_col = "disease_name"
    elif "indication_name" in cols:
        disease_name_col = "indication_name"

    if disease_name_col is not None:
        out_cols.append("disease_id")
        ex = existing_series("disease_id")
        vals = []
        for idx, raw in enumerate(df[disease_name_col].tolist()):
            mapped = ot_index.lookup("disease", raw)
            if mapped is None and ex is not None and pd.notna(ex.iat[idx]):
                mapped = str(ex.iat[idx])
            vals.append(mapped)
        out_data["disease_id"] = vals

    # Drug
    if "drug_name" in cols:
        out_cols.append("drug_id")
        ex = existing_series("drug_id")
        vals = []
        for idx, raw in enumerate(df["drug_name"].tolist()):
            mapped = ot_index.lookup("drug", raw)
            if mapped is None and ex is not None and pd.notna(ex.iat[idx]):
                mapped = str(ex.iat[idx])
            vals.append(mapped)
        out_data["drug_id"] = vals

    # Pathway (optional)
    if include_pathway and "pathway_name" in cols:
        out_cols.append("pathway_id")
        ex = existing_series("pathway_id")
        vals = []
        for idx, raw in enumerate(df["pathway_name"].tolist()):
            mapped = ot_index.lookup("pathway", raw)
            if mapped is None and ex is not None and pd.notna(ex.iat[idx]):
                mapped = str(ex.iat[idx])
            vals.append(mapped)
        out_data["pathway_id"] = vals

    # Keep a stable, predictable column order.
    stable = [c for c in ["gene_id", "disease_id", "drug_id", "pathway_id"] if c in out_cols]
    return pd.DataFrame({c: out_data.get(c, [None] * len(df)) for c in stable})


def remap_pickle_to_opentargets_ids(
    *,
    in_path: str,
    out_path: str,
    ot_index: OpenTargetsNameIndex,
    include_pathway: bool = True,
    fallback_to_existing: bool = True,
) -> Dict[str, Any]:
    """
    Remap a `*_same_question_response_with_id.pkl` file to OT IDs (offline).

    Returns a small summary dict; the caller is responsible for writing `out_path`.
    """
    import pickle

    obj = pickle.load(open(in_path, "rb"))
    if not isinstance(obj, dict) or not obj:
        raise ValueError(f"Unexpected pickle structure at {in_path!r}")

    top_model = next(iter(obj.keys()))
    queries = obj[top_model]
    if not isinstance(queries, dict):
        raise ValueError(f"Unexpected pickle structure at {in_path!r}")

    remapped = {top_model: {}}

    n_payload = 0
    n_df = 0
    n_dfid = 0
    n_rows = 0
    n_rows_any_nan = 0

    for question, runs in queries.items():
        if not isinstance(runs, dict):
            continue
        remapped[top_model][question] = {}
        for run_id, payload in runs.items():
            if not isinstance(payload, dict):
                remapped[top_model][question][run_id] = payload
                continue

            n_payload += 1
            df = payload.get("dataframe")
            df_id_existing = payload.get("dataframe_id")
            if isinstance(df, pd.DataFrame):
                n_df += 1
                new_dfid = remap_dataframe_ids_to_opentargets(
                    df,
                    df_id_existing if isinstance(df_id_existing, pd.DataFrame) else None,
                    ot_index,
                    include_pathway=include_pathway,
                    fallback_to_existing=fallback_to_existing,
                )
                # Keep payload as-is, only swap dataframe_id.
                new_payload = dict(payload)
                new_payload["dataframe_id"] = new_dfid
                remapped[top_model][question][run_id] = new_payload

                n_dfid += 1
                n_rows += len(new_dfid)
                n_rows_any_nan += int(new_dfid.isna().any(axis=1).sum())
            else:
                remapped[top_model][question][run_id] = payload

    pickle.dump(remapped, open(out_path, "wb"))

    return {
        "model": top_model,
        "in_path": in_path,
        "out_path": out_path,
        "payloads": n_payload,
        "dataframes": n_df,
        "dataframe_ids_written": n_dfid,
        "rows_in_dataframe_id": n_rows,
        "rows_with_any_nan": n_rows_any_nan,
        "frac_rows_with_any_nan": (n_rows_any_nan / n_rows) if n_rows else None,
    }

