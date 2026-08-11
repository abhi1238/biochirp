import pandas as pd
from typing import List

import logging
# =========================================================
# Logging
# =========================================================
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.dataframe")


CANONICAL_6 = [
    "gene_id", "gene_name",
    "drug_id", "drug_name",
    "disease_id", "disease_name"
]



def empty_df(*, extra_cols: List[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=CANONICAL_6 + extra_cols)

def ensure_cols(df: pd.DataFrame, *, extra_cols: List[str]) -> pd.DataFrame:
    # target_data.py keys the gene column as "gene_symbol" instead of the
    # canonical "gene_name" (utility_join.py's _normalize_cols already aliases
    # the same mismatch for join_tool). Without this, reindexing to CANONICAL_6
    # silently drops the real gene_symbol column and injects an all-None
    # gene_name, so target+drug joint-filter queries return zero rows even
    # though the underlying data exists.
    if "gene_name" not in df.columns and "gene_symbol" in df.columns:
        df["gene_name"] = df["gene_symbol"]
    keep_extra = [c for c in ("gene_symbol",) if c in df.columns]
    for c in CANONICAL_6 + extra_cols:
        if c not in df.columns:
            df[c] = None
    return df[CANONICAL_6 + extra_cols + keep_extra]




