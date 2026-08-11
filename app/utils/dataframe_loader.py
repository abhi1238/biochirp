import os
import polars as pl
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")


# Databases migrated to the `preprocess_v2.ipynb` pipeline, whose normalized tables
# are written with a `_v2` suffix (e.g. `protein_master_table_uniprot_v2.parquet`).
# For these DBs `read_parquet_polars` prefers the `_v2` sibling when it exists on disk,
# otherwise it transparently loads the legacy file — so a service keeps serving the
# current data until the v2 parquets are generated, then auto-switches with no redeploy.
# Scoped deliberately: `mesh`/`ctd`/`hcdt` also have `*_v2.parquet` files but their loaders
# already request the correct names directly, so they are intentionally NOT listed here.
_V2_DATABASES = {
    "ttd", "uniprot", "reactome", "clinvar", "hpo", "orphanet", "string",
}


def _resolve_v2(path: str, database: str, name: str) -> str:
    """Return the on-disk filename to load, preferring a `_v2` sibling for migrated DBs."""
    if (
        database in _V2_DATABASES
        and name.endswith(".parquet")
        and not name.endswith("_v2.parquet")
    ):
        v2_name = name[: -len(".parquet")] + "_v2.parquet"
        if os.path.exists(os.path.join(path, database, v2_name)):
            logger.info(f"[{database}] repoint: loading '{v2_name}' in place of '{name}'")
            return v2_name
    return name


def read_parquet_polars(
    path: str,
    database: str,
    name: str,
    *,
    cast_all_to_utf8: bool = True,
    keep_native: tuple[str, ...] | list[str] | None = None,
) -> pl.LazyFrame:
    """Lazily read a parquet file with a configurable type-cast strategy.

    Historically every column was eagerly cast to ``pl.Utf8`` so downstream
    filter code could rely on string operations (`.str.contains`,
    `.str.to_lowercase`, etc.) without per-column type checks. That is
    still the **default** (``cast_all_to_utf8=True``) so the 28 existing
    per-DB loaders keep working byte-identical.

    For new code that wants polars' type-specific optimisations on
    numeric / categorical filters (drug_id, phase, year, …) pass either:
      - ``cast_all_to_utf8=False`` to disable the cast entirely, OR
      - ``keep_native=[…]`` to whitelist specific columns to leave native
        while still casting the rest to Utf8.

    Uses ``collect_schema().names()`` to read the parquet footer once
    without triggering polars' PerformanceWarning. No data is loaded
    until ``.collect()`` is called on the returned LazyFrame.

    Migration note (M1, 2026-05-21): the eager-cast-all behaviour was
    the bottleneck on TTD filter funnels — string comparisons on what
    are really numeric / categorical IDs scan ~5× slower than native.
    The right migration path is per-DB: identify which columns are
    actually consumed as strings downstream (synonyms, names, free
    text) and pass the rest in ``keep_native``. Cross-DB regression
    risk is non-trivial; opt-in per loader.
    """
    name = _resolve_v2(path, database, name)
    file_path = os.path.join(path, database, name)
    try:
        df = pl.scan_parquet(file_path)
        if cast_all_to_utf8:
            keep_set = set(keep_native or ())
            cols = df.collect_schema().names()
            to_cast = [c for c in cols if c not in keep_set]
            if to_cast:
                df = df.with_columns([pl.col(c).cast(pl.Utf8) for c in to_cast])
            if keep_set:
                logger.info(
                    f"[{database}] '{name}': native-types preserved for "
                    f"{len(keep_set & set(cols))} col(s), Utf8-cast {len(to_cast)} col(s)"
                )
        # else: leave every column in its native type — caller is
        # responsible for filter-side type discipline.
        logger.info(f"[{database}] Successfully loaded '{name}' from: {file_path} (LAZY)")
        return df
    except Exception as e:
        logger.info(f"[{database}] Failed to load '{name}' from: {file_path}\nException: {e}")
        raise


def strip_all_whitespace(lf: pl.LazyFrame) -> pl.LazyFrame:
    schema = lf.collect_schema()
    str_cols = [col for col, dtype in schema.items() if dtype == pl.Utf8]
    return lf.with_columns([pl.col(col).str.strip_chars().alias(col) for col in str_cols])


def clean_table_dict(db: str, results: dict, log: logging.Logger | None = None) -> dict:
    """Strip whitespace + dedupe every table in `results`, wrap as `{db: results}`.

    Replaces the byte-identical tail that appeared in every per-DB
    `database_loader.return_preprocessed_<db>()`.
    """
    _log = log or logger
    for key, df in results.items():
        try:
            results[key] = strip_all_whitespace(df).unique()
        except Exception as e:
            _log.warning("[%s] Cleaning failed for '%s': %s", db, key, e)
    return {db: results}


# Process-wide cache keyed by db name. Each entry holds (loaded_at_epoch, value).
# Stale-while-revalidate semantics: after TTL, a single async reload kicks off,
# but the current request gets the (slightly-stale) cached value immediately.
# Matches the consumption pattern across app/tools/<db>/app/<db>.py modules:
#     get_<db>_db = ttl_cached_db("<db>", return_preprocessed_<db>)
# Restored 2026-05-19 after string_tool crash-loop traced to its absence — the
# function had been referenced by 10+ tools but missing from this file on disk.
import threading as _threading
import time as _time
_DB_CACHE: dict[str, tuple[float, object]] = {}
# Per-db locks serialise concurrent cache-miss reloads so two coroutines /
# threads can't both rebuild the LazyFrame tree at the same TTL boundary.
# threading.Lock (not asyncio.Lock) because the loader is synchronous; the
# critical section never awaits, so holding a threading lock from an async
# coroutine is safe (no yield → no deadlock).
_DB_CACHE_LOCKS: dict[str, _threading.Lock] = {}
_DB_CACHE_LOCKS_MASTER = _threading.Lock()


def _get_cache_lock(db_name: str) -> _threading.Lock:
    """Lazily create + return the per-db reload lock."""
    lock = _DB_CACHE_LOCKS.get(db_name)
    if lock is None:
        with _DB_CACHE_LOCKS_MASTER:
            lock = _DB_CACHE_LOCKS.get(db_name)
            if lock is None:
                lock = _threading.Lock()
                _DB_CACHE_LOCKS[db_name] = lock
    return lock


def ttl_cached_db(db_name: str, loader, ttl_seconds: int = 3600):
    """Return a zero-arg callable that caches the result of `loader()` for
    `ttl_seconds` seconds. The callable is process-wide cached on `db_name`.

    Args:
        db_name: short DB identifier (used as the cache key + log prefix).
        loader: callable that takes no args and returns the preprocessed
            database dict (typically `return_preprocessed_<db>()`).
        ttl_seconds: how long the cached value is considered fresh. Defaults
            to 1 hour, matching the typical parquet-snapshot refresh cadence.

    Returns:
        A callable `get_db()` that lazily loads on first call and refreshes
        when older than ttl_seconds.
    """
    def get_db():
        now = _time.time()
        entry = _DB_CACHE.get(db_name)
        if entry is not None and (now - entry[0]) <= ttl_seconds:
            return entry[1]
        # Cache miss / expired — serialise the reload so concurrent callers
        # don't each rebuild the LazyFrame tree. Double-check inside the lock
        # in case another caller refreshed while we were waiting.
        lock = _get_cache_lock(db_name)
        with lock:
            now = _time.time()
            entry = _DB_CACHE.get(db_name)
            if entry is not None and (now - entry[0]) <= ttl_seconds:
                return entry[1]
            logger.info(f"[{db_name}] Loading database (cold/expired)")
            value = loader()
            _DB_CACHE[db_name] = (now, value)
            logger.info(f"[{db_name}] Database loaded")
            return value
    get_db.__name__ = f"get_{db_name}_db"
    get_db.cache_clear = lambda: _DB_CACHE.pop(db_name, None)
    return get_db

