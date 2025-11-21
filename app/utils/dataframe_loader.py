import os
import polars as pl
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")

# def read_parquet_polars(path: str, database: str, name: str) -> pl.DataFrame:
#     """Reads a Parquet file using Polars, casts all columns to string (Utf8)."""
#     file_path = os.path.join(path, database, name)
#     print(file_path)
#     try:
#         df = pl.read_parquet(file_path)
#         df = df.with_columns([pl.col(col).cast(pl.Utf8) for col in df.columns])
#         # print(df.head(2))
#         logger.info(f"[{database}] Successfully loaded '{name}' from: {file_path}")
#         return df
#     except Exception as e:
#         logger.info(f"[{database}] Failed to load '{name}' from: {file_path}\nException: {e}")
#         raise


def read_parquet_polars(path: str, database: str, name: str) -> pl.LazyFrame:
    """
    Lazily reads a Parquet file using Polars and casts all columns to string (Utf8).
    No data is loaded until .collect() is called on the returned LazyFrame.
    """
    file_path = os.path.join(path, database, name)
    print(file_path)
    try:
        df = pl.scan_parquet(file_path)
        # Cast all columns to Utf8 (string) lazily
        df = df.with_columns([pl.col(col).cast(pl.Utf8) for col in df.columns])
        # logger.info(f"[{database}] Successfully loaded '{name}' from: {file_path} (LAZY)")
        return df
    except Exception as e:
        logger.info(f"[{database}] Failed to load '{name}' from: {file_path}\nException: {e}")
        raise


def strip_all_whitespace(df: pl.DataFrame) -> pl.DataFrame:
    """Strip leading/trailing whitespace from all string (Utf8) columns in a Polars DataFrame."""
    
    str_cols = [col for col, dtype in df.schema.items() if dtype == pl.Utf8]

    return df.with_columns([pl.col(col).str.strip_chars().alias(col) for col in str_cols])


# def strip_all_whitespace_lazy(lf: pl.LazyFrame) -> pl.LazyFrame:
#     # Get string columns from schema
#     str_cols = [col for col, dtype in lf.schema.items() if dtype == pl.Utf8]
#     return lf.with_columns([pl.col(col).str.strip_chars().alias(col) for col in str_cols])
