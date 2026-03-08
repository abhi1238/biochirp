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
    for c in CANONICAL_6 + extra_cols:
        if c not in df.columns:
            df[c] = None
    return df[CANONICAL_6 + extra_cols]




