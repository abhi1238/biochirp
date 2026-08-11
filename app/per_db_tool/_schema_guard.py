"""Startup schema/parquet integrity gate for the per-DB tools.

Three schema surfaces describe each DB and MUST agree, or queries crash at
runtime in confusing ways:

  1. config/schema.py  `database_schemas[db][table] = [col, ...]`
        — the columns the Polars/Steiner planner is allowed to select/join.
  2. the actual parquet  (database/<db>/*.parquet, AFTER the loader's renames)
        — what columns really exist in memory once `return_preprocessed_<db>()`
          has run.
  3. schema_kg/inputs/<db>/schema.json
        — the planner-graph / ANN concept surface the schema_mapper builds on.

This module validates them for a single DB. The check that BLOCKS startup is
(1) ↔ (2): if `database_schemas` names a table the loader didn't produce, or a
column absent from the real DataFrame, the planner WILL later try to select a
column that doesn't exist — so we fail fast at boot instead. The (1) ↔ (3)
comparison is reported as a warning (a planner-graph coverage gap, not a
guaranteed runtime crash).

Mode is controlled by env `SCHEMA_VALIDATION`:
  * "warn" (default) — log mismatches, do not raise. This is the DEFAULT for the
                       passive container-startup path so an unrelated restart
                       can't suddenly fail a service on a pre-existing,
                       known-benign drift (e.g. a column the loader renames).
  * "block"          — raise RuntimeError on any (1)↔(2) mismatch, failing the
                       FastAPI startup event so the container exits (never
                       becomes healthy → effectively blocked). Turn this on per
                       service once its drift is reconciled.
  * "off"            — skip entirely.

The EXPLICIT, run-on-purpose gate is scripts/preflight_schema_check.py, which
always exits non-zero on a mismatch (intended as `preflight && docker compose
up`). The split is deliberate: the gate you invoke blocks; the passive restart
path warns unless you opt in.

Known-benign exceptions live in _EXPECTED_ABSENT_COLS below: columns a loader
intentionally renames away (so they are absent post-load by design) but which
must remain in database_schemas for the single-PK validator / FK generator.

Used at startup by app/per_db_tool/_main.py:build_app (the 9 lean schema_kg DBs)
and by app/tools/hcdt/app/main.py (HCDT builds its FastAPI app directly). The
host-side, pre-`docker up` counterpart (config-only, no parquet load) is
scripts/preflight_schema_check.py, which reuses check_db_schema().
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("uvicorn.error")


# Columns a loader intentionally renames away, so they are absent from the loaded
# DataFrame BY DESIGN, yet must remain in database_schemas for the single-PK
# validator / FK generator. These are NOT drift and never block. Keep this list
# tiny and well-justified — everything else that's absent is a real bug.
_EXPECTED_ABSENT_COLS: dict[tuple[str, str], set[str]] = {
    # orphanet loader renames gene_id → gene_symbol on the master table
    # (app/tools/orphanet/app/database_loader.py); gene_id stays in schema.py
    # only to satisfy the single-PK-per-master rule + FK generation.
    ("orphanet", "gene_master_table"): {"gene_id"},
}


def _df_columns(df) -> set[str]:
    """Column names for a polars DataFrame OR LazyFrame, without collecting data."""
    if hasattr(df, "collect_schema"):          # LazyFrame (polars ≥1.0)
        try:
            return set(df.collect_schema().names())
        except Exception:
            pass
    return set(getattr(df, "columns", []) or [])


def _schema_json_path(db: str) -> Optional[Path]:
    """Resolve schema_kg/inputs/<db>/schema.json in container or host context."""
    candidates = [
        Path("/app/schema_kg/inputs") / db / "schema.json",
        Path(__file__).resolve().parents[2] / "evaluation" / "schema_kg" / "inputs" / db / "schema.json",
    ]
    env_root = os.getenv("SCHEMA_KG_INPUTS_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root) / db / "schema.json")
    return next((p for p in candidates if p.is_file()), None)


def load_schema_json_cols(db: str) -> Optional[dict[str, set[str]]]:
    """Return {table: {column, ...}} from schema_kg/inputs/<db>/schema.json.

    Returns None when the file is absent (not all DBs are schema_kg-backed).
    schema.json shape is {db: {table: {column: description, ...}}}.
    """
    path = _schema_json_path(db)
    if path is None:
        return None
    doc = json.loads(path.read_text())
    inner = doc.get(db, doc)
    return {t: set(cols.keys()) for t, cols in inner.items()
            if isinstance(cols, dict)}


def check_db_schema(
    db: str,
    loaded: Optional[dict] = None,
    schema_json_cols: Optional[dict[str, set[str]]] = None,
) -> tuple[list[str], list[str]]:
    """Compare database_schemas[db] against the loaded parquet and/or schema.json.

    Args:
        db:               DB slug.
        loaded:           the get_db() result ({db: {<table>_<db>: df}}); when
                          given, columns are checked against the real DataFrames
                          (these mismatches are ERRORS).
        schema_json_cols: {table: {col}} from load_schema_json_cols(); when
                          given, database_schemas columns missing from it are
                          WARNINGS.

    Returns (errors, warnings).
    """
    from config.schema import database_schemas

    errors: list[str] = []
    warnings: list[str] = []

    tables = database_schemas.get(db)
    if not tables:
        errors.append(f"'{db}' is not present in config.schema.database_schemas")
        return errors, warnings

    inner = {}
    if loaded is not None:
        inner = loaded.get(db, loaded) if isinstance(loaded, dict) else {}

    for table, cols in tables.items():
        # (1) ↔ (2): real parquet/loaded-DataFrame columns (ERRORS).
        if loaded is not None:
            df = inner.get(f"{table}_{db}")
            if df is None:
                df = inner.get(table)
            if df is None:
                errors.append(
                    f"{db}.{table}: declared in database_schemas but the loader "
                    f"produced no table '{table}_{db}'"
                )
            else:
                actual = _df_columns(df)
                exempt = _EXPECTED_ABSENT_COLS.get((db, table), set())
                missing = [c for c in cols if c not in actual and c not in exempt]
                if missing:
                    errors.append(
                        f"{db}.{table}: columns in database_schemas absent from the "
                        f"loaded parquet: {missing}"
                    )
        # (1) ↔ (3): schema.json coverage (WARNINGS).
        if schema_json_cols is not None:
            sj_cols = schema_json_cols.get(table)
            if sj_cols is None:
                warnings.append(
                    f"{db}.{table}: table not present in schema.json (planner graph)"
                )
            else:
                sj_missing = [c for c in cols if c not in sj_cols]
                if sj_missing:
                    warnings.append(
                        f"{db}.{table}: columns in database_schemas absent from "
                        f"schema.json: {sj_missing}"
                    )

    return errors, warnings


def assert_db_schema(db: str, get_db) -> None:
    """Runtime startup gate. Loads the DB (cache hit — build_app already did),
    validates, and raises in 'block' mode so the container fails to start.
    """
    mode = os.getenv("SCHEMA_VALIDATION", "warn").strip().lower()
    if mode == "off":
        return

    loaded = get_db()
    schema_json_cols = load_schema_json_cols(db)
    errors, warnings = check_db_schema(db, loaded=loaded, schema_json_cols=schema_json_cols)

    for w in warnings:
        logger.warning("[schema-guard] %s", w)

    if not errors:
        logger.info("[schema-guard] %s: database_schemas ↔ parquet OK (%d tables)",
                    db, len(__import__("config.schema", fromlist=["database_schemas"])
                            .database_schemas.get(db, {})))
        return

    detail = "\n".join(f"  - {e}" for e in errors)
    msg = (f"[schema-guard] {db}: {len(errors)} schema/parquet mismatch(es) — "
           f"the planner would select non-existent columns:\n{detail}")
    if mode == "warn":
        logger.warning(msg)
        return
    logger.error(msg)
    raise RuntimeError(msg)


# ─── EXACT parquet validation against the per-DB SSOT (dbs/<db>/schema.yaml) ──
#
# The check above (database_schemas vs loaded frame) is presence-only and runs on
# the POST-loader, Utf8-cast frames. The validator below is the SSOT-era gate: it
# scans the RAW parquet (independent of the loader's blanket Utf8 cast), applies
# the SSOT's declared loader renames, and requires an EXACT match — every declared
# column present, no undeclared column, and dtypes equal. It blocks.
#
# Reused from three entry points (plan stage 2):
#   (a) database/<db>/preprocess_v2.ipynb  — final cell, blocks the notebook
#   (b) per_db_tool / hcdt startup         — when a schema.yaml exists for the DB
#   (c) scripts/check_parquet_schema.py    — CI / preflight
#
# Returns None (graceful no-op) when the DB has no schema.yaml yet — so this can
# ship before every DB is migrated.


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _schema_yaml_path(db: str) -> Optional[Path]:
    candidates = [
        Path("/app/dbs") / db / "schema.yaml",
        _repo_root() / "dbs" / db / "schema.yaml",
    ]
    env_root = os.getenv("DBS_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root) / db / "schema.yaml")
    return next((p for p in candidates if p.is_file()), None)


def _parquet_path(parquet_dir: str, fname: str) -> Optional[Path]:
    candidates = [
        Path("database") / parquet_dir / fname,          # CWD-relative (matches the loader)
        Path("/app/database") / parquet_dir / fname,
        _repo_root() / "database" / parquet_dir / fname,
    ]
    env_root = os.getenv("BIOCHIRP_DB_DATA_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root) / parquet_dir / fname)
    return next((p for p in candidates if p.is_file()), None)


def assert_db_schema_exact(db: str, mode: str = "block") -> None:
    """Validate every *_v2.parquet for *db* against dbs/<db>/schema.yaml.

    Reads the raw parquet (pre-loader), checks that the column set exactly
    matches the SSOT-declared columns (excluding exec_schema:false and
    added_at_load:true columns).  Raises RuntimeError when mode='block'
    and any drift is found.  Returns None (no-op) when no schema.yaml exists.
    """
    import polars as pl
    import yaml

    yaml_path = _schema_yaml_path(db)
    if yaml_path is None:
        return  # graceful no-op — DB not yet migrated

    with open(yaml_path) as f:
        schema = yaml.safe_load(f)

    parquet_dir = schema.get("parquet_dir", db)
    tables = schema.get("tables", {})
    errors: list[str] = []

    for _table_key, tdef in tables.items():
        fname = tdef.get("parquet", f"{_table_key}_v2.parquet")
        # Loader-derived tables are BUILT at load time (unpivot/explode of a master
        # table), not stored on disk — the schema marks them with a
        # "_loader_derived_:" parquet sentinel. There is no raw parquet to validate.
        if str(fname).startswith("_loader_derived_:"):
            continue
        ppath = _parquet_path(parquet_dir, fname)
        if ppath is None:
            errors.append(f"  MISSING parquet: {fname}")
            continue

        try:
            actual_cols = set(pl.scan_parquet(str(ppath)).collect_schema().names())
        except Exception as exc:
            errors.append(f"  UNREADABLE {fname}: {exc}")
            continue

        # This gate reads the RAW (pre-loader) parquet. A column that the loader
        # renames may sit on disk under EITHER its logical `name` OR the on-disk
        # alias declared in `parquet_name` — DBs are inconsistent about which they
        # materialise (clinvar/ctd/hcdt/msigdb/ttd store the parquet_name and
        # rename up to the logical name at load; orphanet/uniprot already store the
        # logical name). So accept either spelling: a required column is satisfied
        # if the parquet holds ANY of its declared names, and both names are
        # allowed (never "extra"). Aliases are read from schema.yaml — no hardcoding.
        # Expected = columns that MUST be in parquet (exec_schema != false, not added_at_load)
        # Allowed  = all declared column names (logical + parquet_name), optional ones included
        expected: list[tuple[str, set[str]]] = []
        allowed_cols: set[str] = set()
        for col in tdef.get("columns", []):
            names = {col["name"]}
            if col.get("parquet_name"):
                names.add(col["parquet_name"])
            allowed_cols |= names
            if col.get("exec_schema") is False:
                continue  # on disk optionally; don't require
            if col.get("added_at_load"):
                continue  # injected by loader, not in raw parquet
            expected.append((col["name"], names))

        # Missing = a required column with NONE of its declared spellings on disk.
        missing = {logical for logical, names in expected if not (names & actual_cols)}
        # Extra = present in parquet but not declared in schema at all (under any name)
        extra   = actual_cols - allowed_cols
        if missing or extra:
            msg = f"  DRIFT in {fname}:"
            if missing:
                msg += f"\n    missing cols: {sorted(missing)}"
            if extra:
                msg += f"\n    extra cols:   {sorted(extra)}"
            errors.append(msg)

    if errors:
        report = f"Schema drift for db='{db}':\n" + "\n".join(errors)
        if mode == "block":
            raise RuntimeError(report)
        else:
            print(report)
    else:
        print(f"✅ {db}: all parquets match dbs/{db}/schema.yaml")


__all__ = [
    "check_db_schema", "assert_db_schema", "load_schema_json_cols",
    "assert_db_schema_exact",
]
