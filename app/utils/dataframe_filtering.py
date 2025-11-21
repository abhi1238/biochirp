
from typing import Any, Dict, List, Tuple, Optional
import polars as pl
from functools import reduce
import logging

logger = logging.getLogger("uvicorn.error")


def join_and_filter_database(dataset, plan, db_name, output_columns, filtered_outputs):
    fq_tables = plan["tables"]
    table_info = plan["table_columns"]

    def get_df(fq_table):
        tbl = fq_table.split(".")[1]
        # Ensure lazy: If dataset returns eager DF, convert; else assume scan/lazy
        df = dataset[db_name][f"{tbl}_{db_name}"]
        if isinstance(df, pl.DataFrame):
            df = df.lazy()  # Force lazy if eager
        return df

    # NEW: Pre-filter ALL tables upfront (prune inputs before joins)
    # This is the key addition: Apply filters to each df independently, reducing join sizes massively
    pre_filtered_dfs = {}
    for fq in fq_tables:
        df = get_df(fq)
        # Filter early on this table's available cols (lazy, so cheap)
        pre_filtered_dfs[fq] = fast_filter_dataframe(df, filtered_outputs)
        # logger.info(f"[{db_name}] Pre-filtered {fq}: Estimated rows {pre_filtered_dfs[fq].collect().height} (post-filter)")

    dfs = pre_filtered_dfs  # Use pre-filtered versions

    # Start with the first (already filtered) table as the join chain (lazy)
    join_chain = dfs[fq_tables[0]]

    if len(fq_tables) == 1:
        # Single-table: Already filtered; proceed
        pass
    else:
        # Multi-table: Join on pre-filtered tables
        for i in range(1, len(fq_tables)):
            left_table = fq_tables[i-1]
            right_table = fq_tables[i]
            # left_cols = set(join_chain.collect_schema().names())
            left_cols = set(join_chain.schema.names())
            right_cols = set(dfs[right_table].schema.names())

            

            # right_cols = set(dfs[right_table].collect_schema().names())

                # Use collect_schema() instead of .schema
            # left_schema = join_chain.collect_schema()
            # right_schema = dfs[right_table].collect_schema()

            # left_cols = set(left_schema.names())
            # right_cols = set(right_schema.names())

            plan_left = set(table_info[left_table]["join_columns"])
            plan_right = set(table_info[right_table]["join_columns"])
            candidate_join_cols = (plan_left | plan_right) & left_cols & right_cols
            if not candidate_join_cols:
                candidate_join_cols = left_cols & right_cols
            join_cols = list(candidate_join_cols)

            logger.info(f"[{db_name}] \n--- Joining {left_table} with {right_table} (pre-filtered) ---")
            logger.info(f"[{db_name}]  Plan join_columns: left={plan_left}, right={plan_right}")
            logger.info(f"[{db_name}]  Actual left cols: {left_cols}")
            logger.info(f"[{db_name}] Actual right cols: {right_cols}")
            logger.info(f"[{db_name}]  Chosen join_cols: {join_cols}")
            if not join_cols:
                raise ValueError(
                    f"Could not find join columns between {left_table} and {right_table}.\n"
                    f"Left columns: {left_cols}\nRight columns: {right_cols}\n"
                    f"Plan join_columns: {plan_left} | {plan_right}"
                )
            
            # Inner join on pre-pruned data (less memory for hashing)
            join_chain = join_chain.join(dfs[right_table], on=join_cols, how="inner", suffix="_y")
            # No need for extra filter here—already pre-applied; but if new cols from right enable more, optional re-filter

    # Schema check (cheap)
    schema = join_chain.collect_schema()
    cols_to_use = [col for col in output_columns if col in schema.names()]

    logger.info(f"[{db_name}] Columns to use: {cols_to_use}")

    if not cols_to_use or len(cols_to_use) < len(output_columns):
        logger.info(f"[{db_name}] Skipping: Not all output columns present. Needed: {output_columns}, Got: {schema.names()}")
        return pl.DataFrame({})

    # Early select (projection pushdown)
    join_chain = join_chain.select(cols_to_use)
    
    # NEW: Streaming collect for large data (processes in chunks, lower peak RAM)
    # # Set streaming=True; falls back to in-mem if small. Enable with Polars 0.20+
    # collected = join_chain.collect(streaming=True)
    
    # # NEW: Targeted unique (only on output cols; less hash overhead)
    # # If full unique is overkill, comment out or use collected.drop_duplicates(subset=cols_to_use)
    # result = collected.unique(subset=cols_to_use)

    result = join_chain.unique(subset=cols_to_use).collect(streaming=True)


    if result.height == 0:
        # Ensure DataFrame has the expected columns, even if empty
        result = pl.DataFrame({col: [] for col in cols_to_use})
        
    logger.info(f"[{db_name}] Final result shape: {result.shape} (RAM-friendly)")
    return result


def fast_filter_dataframe(df: pl.LazyFrame, filters: dict) -> pl.LazyFrame:
    or_columns = ["target_name", "gene_name"]
    or_masks = []
    and_mask = pl.lit(True)
    
    # NEW: Early exit if no filters (avoids unnecessary ops)
    if not any(filters.values()):
        return df
    
    for col in or_columns:
        if col in filters and col in df.schema and isinstance(filters[col], list) and filters[col]:
            vals_lower = [str(v).lower() for v in filters[col]]
            or_masks.append(pl.col(col).cast(str).str.to_lowercase().is_in(vals_lower))
    
    for col, val in filters.items():
        if col in or_columns or col not in df.schema or not isinstance(val, list) or not val:
            continue
        vals_lower = [str(v).lower() for v in val]
        and_mask &= pl.col(col).cast(str).str.to_lowercase().is_in(vals_lower)
    
    if or_masks:
        or_mask = reduce(lambda a, b: a | b, or_masks)
        final_mask = and_mask & or_mask
    else:
        final_mask = and_mask

    return df.filter(final_mask)



# from typing import Any, Dict, List, Tuple, Optional
# import polars as pl
# from functools import reduce
# import logging

# logger = logging.getLogger("uvicorn.error")

# def join_and_filter_database(dataset, plan, db_name, output_columns, filtered_outputs):
#     fq_tables = plan["tables"]
#     table_info = plan["table_columns"]

#     def get_df(fq_table):
#         tbl = fq_table.split(".")[1]
#         df = dataset[db_name][f"{tbl}_{db_name}"]
#         if isinstance(df, pl.DataFrame):
#             df = df.lazy()
#         return df

#     # 1. Pre-filter tables up-front
#     pre_filtered_dfs = {}
#     table_sizes = {}
#     for fq in fq_tables:
#         df = get_df(fq)
#         filtered = fast_filter_dataframe(df, filtered_outputs)
#         pre_filtered_dfs[fq] = filtered
#         # Estimate post-filter size (row count)
#         size = filtered.select(pl.len()).collect()[0, 0]
#         table_sizes[fq] = size
#         logger.info(f"[{db_name}] Pre-filtered {fq}: {size} rows")

#     # 2. Sort table order by size for join efficiency
#     join_order = sorted(fq_tables, key=lambda t: table_sizes[t])
#     logger.info(f"[{db_name}] Join order by row count: {[(t, table_sizes[t]) for t in join_order]}")

#     # 3. Build join chain
#     join_chain = pre_filtered_dfs[join_order[0]]
#     for i in range(1, len(join_order)):
#         left_table = join_order[i-1]
#         right_table = join_order[i]
#         left_cols = set(join_chain.schema.names())
#         right_cols = set(pre_filtered_dfs[right_table].schema.names())

#         # Find candidate join columns
#         plan_left = set(table_info[left_table].get("join_columns", []))
#         plan_right = set(table_info[right_table].get("join_columns", []))
#         candidate_join_cols = (plan_left | plan_right) & left_cols & right_cols
#         if not candidate_join_cols:
#             candidate_join_cols = left_cols & right_cols
#         join_cols = list(candidate_join_cols)

#         logger.info(f"[{db_name}] Joining {left_table} (left) with {right_table} (right)")
#         logger.info(f"  Plan join_columns: left={plan_left}, right={plan_right}")
#         logger.info(f"  Actual left cols: {left_cols}")
#         logger.info(f"  Actual right cols: {right_cols}")
#         logger.info(f"  Chosen join_cols: {join_cols}")

#         if not join_cols:
#             raise ValueError(
#                 f"Could not find join columns between {left_table} and {right_table}.\n"
#                 f"Left columns: {left_cols}\nRight columns: {right_cols}\n"
#                 f"Plan join_columns: {plan_left} | {plan_right}"
#             )

#         join_chain = join_chain.join(
#             pre_filtered_dfs[right_table], 
#             on=join_cols, 
#             how="inner", 
#             suffix="_y"
#         )
#         # No need for extra filter here due to pre-filtering

#     # 4. Projection/column selection
#     schema = join_chain.collect_schema()
#     cols_to_use = [col for col in output_columns if col in schema.names()]
#     logger.info(f"[{db_name}] Columns to use in output: {cols_to_use}")

#     if not cols_to_use or len(cols_to_use) < len(output_columns):
#         logger.info(f"[{db_name}] Skipping: Not all output columns present. Needed: {output_columns}, Got: {schema.names()}")
#         return pl.DataFrame({})

#     join_chain = join_chain.select(cols_to_use)
#     # 5. Unique by output columns, stream results
#     result = join_chain.unique(subset=cols_to_use).collect(streaming=True)

#     if result.height == 0:
#         result = pl.DataFrame({col: [] for col in cols_to_use})

#     logger.info(f"[{db_name}] Final result shape: {result.shape} (RAM-friendly)")
#     return result

# def fast_filter_dataframe(df: pl.LazyFrame, filters: dict) -> pl.LazyFrame:
#     or_columns = ["target_name", "gene_name"]
#     or_masks = []
#     and_mask = pl.lit(True)
#     # Early exit if no filters
#     if not any(filters.values()):
#         return df

#     for col in or_columns:
#         if col in filters and col in df.schema and isinstance(filters[col], list) and filters[col]:
#             vals_lower = [str(v).lower() for v in filters[col]]
#             or_masks.append(pl.col(col).cast(str).str.to_lowercase().is_in(vals_lower))
#     for col, val in filters.items():
#         if col in or_columns or col not in df.schema or not isinstance(val, list) or not val:
#             continue
#         vals_lower = [str(v).lower() for v in val]
#         and_mask &= pl.col(col).cast(str).str.to_lowercase().is_in(vals_lower)
#     if or_masks:
#         or_mask = reduce(lambda a, b: a | b, or_masks)
#         final_mask = and_mask & or_mask
#     else:
#         final_mask = and_mask
#     return df.filter(final_mask)