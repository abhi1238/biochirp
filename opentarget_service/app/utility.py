

import pandas as pd
from typing import Dict, Any, List


def df_to_llm_safe_hierarchy(
    df: pd.DataFrame,
    root_col: str,
) -> Dict[str, Any]:
    """
    Convert a DataFrame into an LLM-safe hierarchical dictionary
    with explicit attribute names derived from column names.

    Parameters
    ----------
    df : pd.DataFrame
        Input dataframe
    root_col : str
        Root column (e.g. "gene")

    Returns
    -------
    dict
        Explicit, variance-ordered, LLM-interpretable hierarchy
    """

    if root_col not in df.columns:
        raise ValueError(f"'{root_col}' not found in DataFrame")

    # --------------------------------------------------
    # 1. Cardinality (variance) per column
    # --------------------------------------------------
    cardinality = {
        col: df[col].dropna().nunique()
        for col in df.columns
    }

    # --------------------------------------------------
    # 2. Order columns by increasing variance
    #    (excluding root)
    # --------------------------------------------------
    ordered_cols = sorted(
        [c for c in df.columns if c != root_col],
        key=lambda c: cardinality[c]
    )

    # --------------------------------------------------
    # 3. Initialize root
    # --------------------------------------------------
    result: Dict[str, Any] = {
        root_col: df[root_col].dropna().iloc[0]
    }

    # --------------------------------------------------
    # 4. Recursive builder with explicit keys
    # --------------------------------------------------
    def build_level(
        sub_df: pd.DataFrame,
        cols: List[str],
    ) -> Dict[str, Any]:

        col = cols[0]

        # Leaf column → return unique list
        if len(cols) == 1:
            return {
                col + "s": sorted(sub_df[col].dropna().unique().tolist())
            }

        node: Dict[str, Any] = {}

        for value, group in sub_df.groupby(col):
            node[value] = build_level(group, cols[1:])

        return {col: node}

    # --------------------------------------------------
    # 5. Build hierarchy
    # --------------------------------------------------
    hierarchy = build_level(df, ordered_cols)

    # Attach hierarchy under the first non-root column
    first_key = ordered_cols[0]
    result[first_key] = hierarchy[first_key]

    return result
