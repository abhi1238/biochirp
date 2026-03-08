import os
import polars as pl
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")



def read_parquet_polars(path: str, database: str, name: str) -> pl.LazyFrame:
    """
    Lazily reads a Parquet file using Polars and casts all columns to string (Utf8).
    No data is loaded until .collect() is called on the returned LazyFrame.
    """
    file_path = os.path.join(path, database, name)
    # print(file_path)
    try:
        df = pl.scan_parquet(file_path)

        df = df.with_columns([pl.col(col).cast(pl.Utf8) for col in df.columns])


        
        logger.info(f"[{database}] Successfully loaded '{name}' from: {file_path} (LAZY)")
        return df
    except Exception as e:
        logger.info(f"[{database}] Failed to load '{name}' from: {file_path}\nException: {e}")
        raise



def strip_all_whitespace(lf: pl.LazyFrame) -> pl.LazyFrame:
    str_cols = [col for col, dtype in lf.schema.items() if dtype == pl.Utf8]
    return lf.with_columns([
        pl.col(col).str.strip_chars().alias(col) 
        for col in str_cols
    ])

