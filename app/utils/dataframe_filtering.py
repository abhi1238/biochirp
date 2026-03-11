

# dataframe_filtering.py

"""
Production-grade database join and filter operations.

This module provides memory-efficient joining and filtering of database tables
according to query plans, with strict validation and cross-join detection.

Updates (high-value, low-risk):
1) Cached cardinality estimates (per-query, concurrency-safe via ContextVar)
2) Better root selection (choose smallest root table by estimated rows)
"""

import os
import logging
from typing import Any, Dict, List, Tuple, Optional, Set
from functools import reduce
from dataclasses import dataclass
from contextvars import ContextVar

import polars as pl

logger = logging.getLogger(__name__)

# Configuration
MAX_UNIQUE_ROWS = int(os.getenv("MAX_UNIQUE_ROWS", "1000000"))
JOIN_BATCH_SIZE = int(os.getenv("JOIN_BATCH_SIZE", "100000"))
STRICT_JOIN_MODE = os.getenv("STRICT_JOIN_MODE", "true").lower() == "true"
CROSS_JOIN_THRESHOLD = float(os.getenv("CROSS_JOIN_THRESHOLD", "5000"))
MAX_RESULT_SIZE = int(os.getenv("MAX_RESULT_SIZE", "10000000"))
ENABLE_STREAMING = os.getenv("ENABLE_STREAMING", "true").lower() == "true"

# Per-query cardinality cache (concurrency-safe)
_CARDINALITY_CACHE: ContextVar[Dict[int, int]] = ContextVar("_CARDINALITY_CACHE", default={})

@dataclass
class FilterStat:
    column: str
    input_values: list
    rows_before: int
    rows_after: int


@dataclass
class JoinMetrics:
    """Metrics for monitoring join operations."""
    pre_join_rows: int
    post_join_rows: int
    parent_table: str
    child_table: str

    @property
    def explosion_factor(self) -> float:
        """Calculate how much the join exploded the data."""
        if self.pre_join_rows == 0:
            return 0.0
        return self.post_join_rows / self.pre_join_rows

    @property
    def is_suspicious(self) -> bool:
        """Check if join looks like a cross-join or data explosion."""
        return self.explosion_factor > CROSS_JOIN_THRESHOLD


class DatabaseJoinError(Exception):
    """Base exception for database join operations."""
    pass


class CrossJoinDetectedError(DatabaseJoinError):
    """Detected a suspicious cross-join or data explosion."""
    pass


class MissingJoinError(DatabaseJoinError):
    """Required join columns not found."""
    pass


def required_columns_for_table(
    table: str,
    output_columns: List[str],
    table_info: Dict[str, Any]
) -> Set[str]:
    """
    Determine which columns are needed from a table.

    Args:
        table: Fully-qualified table name
        output_columns: Output columns requested by user
        table_info: Table metadata from query plan

    Returns:
        Set of required column names
    """
    cols = set()

    # Output columns that live in this table
    concept_cols = table_info.get(table, {}).get("concept_columns", [])
    cols |= set(col for col in output_columns if col in concept_cols)

    # Join columns needed from this table
    cols |= set(table_info.get(table, {}).get("join_columns", []))

    return cols


def estimate_cardinality(df: pl.LazyFrame) -> int:
    """
    Estimate row count for a LazyFrame efficiently, with per-query caching.

    NOTE:
    - This calls collect() for count, so caching is critical.
    - Cache is stored in a ContextVar, so concurrent requests don't conflict.
    """
    cache = _CARDINALITY_CACHE.get()
    df_id = id(df)

    if df_id in cache:
        return cache[df_id]

    try:
        count = df.select(pl.len()).collect().item()
        cache[df_id] = int(count)
        return cache[df_id]
    except Exception as e:
        logger.warning(f"Could not estimate cardinality: {e}")
        cache[df_id] = -1
        return -1


def validate_join_columns(
    left_schema: Dict[str, Any],
    right_schema: Dict[str, Any],
    left_on: List[str],
    right_on: List[str],
    parent_table: str,
    child_table: str
) -> None:
    """
    Validate that join columns exist in both tables.
    """
    for col in left_on:
        if col not in left_schema:
            raise MissingJoinError(
                f"Join column '{col}' not found in parent table '{parent_table}'. "
                f"Available columns: {sorted(left_schema.keys())}"
            )

    for col in right_on:
        if col not in right_schema:
            raise MissingJoinError(
                f"Join column '{col}' not found in child table '{child_table}'. "
                f"Available columns: {sorted(right_schema.keys())}"
            )


def detect_cross_join(metrics: JoinMetrics) -> None:
    """
    Detect and warn about suspicious joins.
    """
    if metrics.is_suspicious:
        msg = (
            f"Suspicious join detected: {metrics.parent_table} -> {metrics.child_table}. "
            f"Rows exploded from {metrics.pre_join_rows:,} to {metrics.post_join_rows:,} "
            f"({metrics.explosion_factor:.2f}x increase). "
            f"This may indicate a cross-join or missing filter. "
            f"Set CROSS_JOIN_THRESHOLD higher to allow this."
        )
        logger.error(msg)
        raise CrossJoinDetectedError(msg)

    if metrics.explosion_factor > 1.0:
        logger.info(
            f"Join {metrics.parent_table} -> {metrics.child_table}: "
            f"{metrics.pre_join_rows:,} -> {metrics.post_join_rows:,} rows "
            f"({metrics.explosion_factor:.2f}x)"
        )


def optimize_join_order(
    remaining_tables: Set[str],
    joined_tables: Set[str],
    parents: Dict[str, Optional[str]],
    pre_filtered_dfs: Dict[str, pl.LazyFrame]
) -> List[str]:
    """
    Optimize join order by preferring smaller tables first,
    subject to the tree constraint (parent must already be joined).
    """
    ready_tables = [
        t for t in remaining_tables
        if parents.get(t) in joined_tables
    ]

    if not ready_tables:
        return []

    table_sizes = []
    for table in ready_tables:
        size = estimate_cardinality(pre_filtered_dfs[table])
        table_sizes.append((table, size))

    # Sort by size (ascending), with -1 (unknown) at the end.
    # Final tie-break by table name to avoid nondeterminism from set iteration.
    table_sizes.sort(key=lambda x: (x[1] < 0, x[1], x[0]))
    ordered = [t for t, _ in table_sizes]

    logger.debug(f"Join order for next batch: {ordered}")
    return ordered


def perform_join_with_validation(
    join_chain: pl.LazyFrame,
    right_df: pl.LazyFrame,
    left_on: List[str],
    right_on: List[str],
    parent_table: str,
    child_table: str
) -> Tuple[pl.LazyFrame, JoinMetrics]:
    """
    Perform join with validation and metrics collection.
    """
    pre_join_rows = estimate_cardinality(join_chain)

    if left_on == right_on:
        result = join_chain.join(right_df, on=left_on, how="inner")
    else:
        result = join_chain.join(right_df, left_on=left_on, right_on=right_on, how="inner")

    post_join_rows = estimate_cardinality(result)

    metrics = JoinMetrics(
        pre_join_rows=pre_join_rows,
        post_join_rows=post_join_rows,
        parent_table=parent_table,
        child_table=child_table
    )

    detect_cross_join(metrics)
    return result, metrics


# def fast_filter_dataframe(df: pl.LazyFrame, filters: Dict[str, Any]) -> pl.LazyFrame:
def fast_filter_dataframe(
    df: pl.LazyFrame,
    filters: Dict[str, Any],
    filter_stats: Optional[list] = None
) -> pl.LazyFrame:

    """
    Apply filters to LazyFrame efficiently.

    Special handling:
      - target_name, gene_name: Combined with OR
      - Other columns: Combined with AND
    """
    if not filters or not any(filters.values()):
        return df

    or_columns = ["target_name", "gene_name"]
    or_masks = []
    and_mask = pl.lit(True)

    schema = df.schema

    # OR columns
    for col in or_columns:
        if col not in filters or col not in schema:
            continue

        filter_val = filters[col]
        if not isinstance(filter_val, list) or not filter_val:
            continue

        col_type = schema[col]
        if col_type not in (pl.Utf8, pl.String, pl.Categorical):
            logger.warning(
                f"Column '{col}' has type {col_type}, expected string type. "
                f"Skipping filter for this column."
            )
            continue

        vals_lower = [str(v).lower() for v in filter_val if v]
        if vals_lower:
            or_masks.append(pl.col(col).str.to_lowercase().is_in(vals_lower))

    # AND columns
    for col, filter_val in filters.items():
        if col in or_columns:
            continue
        if col not in schema:
            continue
        if not isinstance(filter_val, list) or not filter_val:
            continue

        col_type = schema[col]
        if col_type not in (pl.Utf8, pl.String, pl.Categorical):
            logger.warning(
                f"Column '{col}' has type {col_type}, expected string type. "
                f"Skipping filter for this column."
            )
            continue

        vals_lower = [str(v).lower() for v in filter_val if v]
        if vals_lower:
            # and_mask &= pl.col(col).str.to_lowercase().is_in(vals_lower)
            before = estimate_cardinality(df)

            df = df.filter(
                pl.col(col).str.to_lowercase().is_in(vals_lower)
            )

            after = estimate_cardinality(df)

            if filter_stats is not None:
                filter_stats.append(
                    FilterStat(
                        column=col,
                        input_values=vals_lower,
                        rows_before=before,
                        rows_after=after,
                    )
                )


    if or_masks:
        or_mask = reduce(lambda a, b: a | b, or_masks)
        final_mask = and_mask & or_mask
    else:
        final_mask = and_mask

    return df.filter(final_mask)


def deduplicate_results(result: pl.DataFrame, cols_to_use: List[str], db_name: str) -> pl.DataFrame:
    """
    Deduplicate results with memory-efficient handling.
    """
    if result.height == 0:
        logger.info(f"[{db_name}] Result is empty, skipping deduplication")
        return result

    if result.height > MAX_UNIQUE_ROWS:
        logger.warning(
            f"[{db_name}] Result has {result.height:,} rows, which exceeds "
            f"MAX_UNIQUE_ROWS ({MAX_UNIQUE_ROWS:,}). Skipping deduplication "
            f"to avoid memory issues. Consider adding more filters."
        )
        return result

    logger.info(f"[{db_name}] Deduplicating {result.height:,} rows...")
    result_dedup = result.unique(subset=cols_to_use)

    removed = result.height - result_dedup.height
    if removed > 0:
        logger.info(
            f"[{db_name}] Removed {removed:,} duplicate rows "
            f"({removed/result.height*100:.1f}%)"
        )
    return result_dedup


def collect_with_memory_management(join_chain: pl.LazyFrame, db_name: str) -> pl.DataFrame:
    """
    Collect results with memory-efficient options.
    """
    logger.info(f"[{db_name}] Collecting results...")

    estimated_rows = estimate_cardinality(join_chain)
    if estimated_rows > MAX_RESULT_SIZE:
        raise DatabaseJoinError(
            f"Query would return {estimated_rows:,} rows, which exceeds "
            f"MAX_RESULT_SIZE ({MAX_RESULT_SIZE:,}). Please add more filters "
            f"or adjust MAX_RESULT_SIZE environment variable."
        )

    if ENABLE_STREAMING:
        result = join_chain.collect(streaming=True)
    else:
        result = join_chain.collect()

    logger.info(f"[{db_name}] Collected {result.height:,} rows")
    return result


def normalize_join_pairs(join_pairs: Dict[Any, Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """
    Normalize join_pairs keys to (left, right) tuples.

    Handles keys as:
      - tuple[str, str]
      - stringified tuple "(a, b)"
      - comma-joined string "a,b"
    """
    import ast

    normalized: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for k, v in join_pairs.items():
        if isinstance(k, tuple):
            if len(k) != 2:
                raise ValueError(f"Invalid join_pairs tuple key: {k}")
            left, right = k

        elif isinstance(k, str):
            if k.strip().startswith("("):
                try:
                    parsed = ast.literal_eval(k)
                    if not isinstance(parsed, tuple) or len(parsed) != 2:
                        raise ValueError
                    left, right = parsed
                except Exception:
                    raise ValueError(f"Invalid join_pairs key string: {k}")
            else:
                parts = [p.strip() for p in k.split(",")]
                if len(parts) != 2:
                    raise ValueError(f"Invalid join_pairs key format: {k}")
                left, right = parts
        else:
            raise TypeError(f"Unsupported join_pairs key type: {type(k)} ({k})")

        normalized[(left, right)] = v

    return normalized

def join_and_filter_database(
    dataset: Dict[str, Dict[str, pl.LazyFrame]],
    plan: Dict[str, Any],
    db_name: str,
    output_columns: List[str],
    filtered_outputs: Dict[str, Any]
) -> Tuple[pl.DataFrame, List[FilterStat]]:
    """
    Join and filter database tables according to query plan.

    Updates:
    - Per-query cardinality cache reset
    - Better root selection: choose smallest root (parent=None) by estimated rows
    """
    # Reset per-query cache (ContextVar to avoid cross-request collisions)
    _CARDINALITY_CACHE.set({})
    filter_stats: List[FilterStat] = []

    logger.info(f"[{db_name}] Starting join_and_filter_database")

    if not plan or "tables" not in plan:
        raise ValueError("Invalid plan: missing 'tables' key")

    if db_name not in dataset:
        raise ValueError(
            f"Database '{db_name}' not found in dataset. "
            f"Available: {list(dataset.keys())}"
        )

    fq_tables = plan["tables"]
    table_info = plan["table_columns"]
    parents = plan["parents"]

    join_pairs_raw = plan.get("join_pairs", {})
    join_pairs = normalize_join_pairs(join_pairs_raw)

    if not fq_tables:
        raise ValueError("No tables in plan")

    logger.info(f"[{db_name}] Plan has {len(fq_tables)} tables: {fq_tables}")
    logger.info(f"[{db_name}] Requested output columns: {output_columns}")

    def get_df(fq_table: str) -> pl.LazyFrame:
        """Get LazyFrame for fully-qualified table name."""
        parts = fq_table.split(".")
        if len(parts) != 2:
            raise ValueError(f"Invalid table name format: '{fq_table}' (expected 'db.table')")

        tbl = parts[1]
        table_key = f"{tbl}_{db_name}"

        if table_key not in dataset[db_name]:
            raise ValueError(
                f"Table '{table_key}' not found in dataset['{db_name}']. "
                f"Available tables: {sorted(dataset[db_name].keys())}"
            )

        df = dataset[db_name][table_key]
        if isinstance(df, pl.DataFrame):
            df = df.lazy()
        return df

    # Pre-filter all tables
    logger.info(f"[{db_name}] Pre-filtering {len(fq_tables)} tables...")
    pre_filtered_dfs: Dict[str, pl.LazyFrame] = {}

    for fq in fq_tables:
        df = get_df(fq)
        # filtered_df = fast_filter_dataframe(df, filtered_outputs)
        filtered_df = fast_filter_dataframe(
            df,
            filtered_outputs,
            filter_stats=filter_stats
        )

        pre_filtered_dfs[fq] = filtered_df

        # Optional logging (now cached)
        pre_count = estimate_cardinality(df)
        post_count = estimate_cardinality(filtered_df)
        if pre_count > 0 and post_count >= 0:
            reduction = (1 - post_count / pre_count) * 100
            logger.info(
                f"[{db_name}] Pre-filtered {fq}: "
                f"{pre_count:,} -> {post_count:,} rows ({reduction:.1f}% reduction)"
            )

    # Single-table case
    if len(fq_tables) == 1:
        logger.info(f"[{db_name}] Single table query (no joins needed)")
        join_chain = pre_filtered_dfs[fq_tables[0]]

    else:
        logger.info(f"[{db_name}] Multi-table query with {len(fq_tables)-1} join(s)")

        # --- Better root selection (smallest valid root) ---
        root_candidates = [t for t in fq_tables if parents.get(t) is None]
        if not root_candidates:
            raise ValueError("No root table found in plan. Expected at least one table with parent=None")

        root = min(root_candidates, key=lambda t: estimate_cardinality(pre_filtered_dfs[t]))
        logger.info(
            f"[{db_name}] Selected root table: {root} "
            f"(estimated rows={estimate_cardinality(pre_filtered_dfs[root])})"
        )

        join_chain = pre_filtered_dfs[root]
        joined_tables = {root}
        remaining_tables = set(fq_tables) - joined_tables
        all_join_metrics: List[JoinMetrics] = []

        while remaining_tables:
            ordered_next = optimize_join_order(remaining_tables, joined_tables, parents, pre_filtered_dfs)
            if not ordered_next:
                missing = remaining_tables
                raise ValueError(
                    f"Cannot join remaining tables: {missing}. "
                    f"Their parents have not been joined yet. "
                    f"Check that parent relationships in plan are correct."
                )

            child_table = ordered_next[0]
            parent_table = parents.get(child_table)

            if parent_table not in joined_tables:
                raise ValueError(
                    f"Parent '{parent_table}' not joined yet for child '{child_table}'. "
                    f"This should not happen after join order optimization."
                )

            join_key = (parent_table, child_table)
            reverse_key = (child_table, parent_table)

            found_join = False
            left_on: Optional[List[str]] = None
            right_on: Optional[List[str]] = None

            if join_key in join_pairs:
                join_spec = join_pairs[join_key]
                left_on = join_spec["left_on"]
                right_on = join_spec["right_on"]
                found_join = True
            elif reverse_key in join_pairs:
                join_spec = join_pairs[reverse_key]
                left_on = join_spec["right_on"]
                right_on = join_spec["left_on"]
                found_join = True

            if not found_join:
                logger.warning(f"[{db_name}] No explicit join_pairs for ({parent_table}, {child_table})")

                if STRICT_JOIN_MODE:
                    error_msg = (
                        f"No join_pairs defined between '{parent_table}' and '{child_table}'. "
                        f"Available join_pairs keys: {list(join_pairs.keys())}. "
                        f"Set STRICT_JOIN_MODE=false to allow auto-inference (not recommended)."
                    )
                    raise MissingJoinError(error_msg)

                # Fallback inference (risky)
                parent_join_cols = table_info.get(parent_table, {}).get("join_columns", [])
                child_join_cols = table_info.get(child_table, {}).get("join_columns", [])

                parent_schema = set(join_chain.schema.keys())
                child_schema = set(pre_filtered_dfs[child_table].schema.keys())

                common = (set(parent_join_cols) | set(child_join_cols)) & parent_schema & child_schema
                if not common:
                    common = parent_schema & child_schema

                if not common:
                    raise MissingJoinError(
                        f"Cannot infer join columns between {parent_table} and {child_table}. "
                        f"Parent schema: {sorted(parent_schema)}, "
                        f"Child schema: {sorted(child_schema)}, "
                        f"No common columns found."
                    )

                left_on = right_on = sorted(list(common))
                logger.warning(
                    f"[{db_name}] Inferred join columns: {left_on} "
                    f"(THIS IS RISKY - add explicit join_pairs!)"
                )

            # Validate join columns exist
            validate_join_columns(join_chain.schema, pre_filtered_dfs[child_table].schema, left_on, right_on, parent_table, child_table)

            logger.info(
                f"[{db_name}] Joining {parent_table} -> {child_table} "
                f"on left={left_on}, right={right_on}"
            )

            join_chain, metrics = perform_join_with_validation(
                join_chain,
                pre_filtered_dfs[child_table],
                left_on,
                right_on,
                parent_table,
                child_table
            )

            all_join_metrics.append(metrics)
            joined_tables.add(child_table)
            remaining_tables.remove(child_table)

        if all_join_metrics:
            avg_explosion = sum(m.explosion_factor for m in all_join_metrics) / len(all_join_metrics)
            logger.info(f"[{db_name}] Completed all joins. Average explosion factor: {avg_explosion:.2f}x")

    final_schema = join_chain.schema
    cols_to_use = [col for col in output_columns if col in final_schema]

    if not cols_to_use:
        logger.warning(
            f"[{db_name}] No requested output columns found in result. "
            f"Requested: {output_columns}, Available: {list(final_schema.keys())}"
        )
        return pl.DataFrame({col: [] for col in output_columns})

    join_chain = join_chain.select(cols_to_use)

    try:
        result = collect_with_memory_management(join_chain, db_name)

        if result.height == 0:
            logger.warning(f"[{db_name}] Query returned 0 rows after joins and filters")
            return pl.DataFrame({col: [] for col in cols_to_use})

        result = deduplicate_results(result, cols_to_use, db_name)

        logger.info(f"[{db_name}] Final result: {result.height:,} rows × {len(cols_to_use)} columns")
        # return result
        return result, filter_stats


    except Exception as e:
        logger.exception(f"[{db_name}] Failed to collect results: {e}")
        raise DatabaseJoinError(f"Failed to execute query: {e}") from e
