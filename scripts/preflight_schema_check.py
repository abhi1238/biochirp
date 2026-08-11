#!/usr/bin/env python3
"""Pre-`docker compose up` gate: every DB's schemas must agree, or block.

For each parquet-backed DB it loads the real preprocessing pipeline
(`return_preprocessed_<db>()`, lazily — only parquet metadata is read) and runs
the same `check_db_schema` the containers run at startup:

  * BLOCK (exit 1): a column in `config/schema.py`'s `database_schemas[db]` that
    is absent from the actual loaded parquet, or a declared table the loader
    never produces. The planner would otherwise select a non-existent column.
  * WARN: a `database_schemas` column/table absent from
    `schema_kg/inputs/<db>/schema.json` (planner-graph coverage gap).

Run it before bringing the stack up:

    python3 scripts/preflight_schema_check.py && docker compose up -d

The in-container counterpart (app/per_db_tool/_schema_guard.py) re-runs the same
check at each service's startup so a drift introduced after preflight still
blocks that container. This script is the fast, all-at-once host gate.

Usage:
    python3 scripts/preflight_schema_check.py              # all parquet DBs
    python3 scripts/preflight_schema_check.py --db hcdt --db ttd
    python3 scripts/preflight_schema_check.py --skip-warn  # silence warnings
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bootstrap_path() -> None:
    # config.schema lives under ROOT; the per-DB loaders do `from utils.X import`,
    # which resolves to ROOT/app/utils — mirror the container's two-target mount.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "app"))


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _loader_fn(db: str):
    """Import app/tools/<db>/app/database_loader.py and return its
    return_preprocessed_<db> callable (falls back to any return_preprocessed_*)."""
    path = ROOT / "app" / "tools" / db / "app" / "database_loader.py"
    if not path.is_file():
        raise FileNotFoundError(f"no loader at {path}")
    mod = _load_by_path(f"{db}_loader", path)
    fn = getattr(mod, f"return_preprocessed_{db}", None)
    if fn is None:
        cands = [getattr(mod, n) for n in dir(mod) if n.startswith("return_preprocessed_")]
        if not cands:
            raise AttributeError(f"{path} has no return_preprocessed_* function")
        fn = cands[0]
    return fn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", action="append", default=[],
                    help="restrict to this DB slug (repeatable)")
    ap.add_argument("--skip-warn", action="store_true",
                    help="don't print schema.json-coverage warnings")
    args = ap.parse_args()

    _bootstrap_path()
    from config.schema import database_schemas
    guard = _load_by_path("schema_guard", ROOT / "app" / "per_db_tool" / "_schema_guard.py")

    targets = args.db or sorted(database_schemas)
    print(f"Preflight schema check for {len(targets)} DB(s):\n")

    total_errors = 0
    failed_loads = 0
    for db in targets:
        try:
            loaded = _loader_fn(db)()
        except Exception as exc:
            print(f"  {db}: ✗ loader failed to run on host: {exc}")
            failed_loads += 1
            continue
        sj = guard.load_schema_json_cols(db)
        errors, warnings = guard.check_db_schema(db, loaded=loaded, schema_json_cols=sj)
        if errors:
            total_errors += len(errors)
            print(f"  {db}: ✗ {len(errors)} BLOCKING mismatch(es)")
            for e in errors:
                print(f"      - {e}")
        else:
            print(f"  {db}: ✓ database_schemas ↔ parquet OK")
        if warnings and not args.skip_warn:
            for w in warnings:
                print(f"      warn: {w}")

    print()
    if failed_loads:
        print(f"⚠ {failed_loads} DB(s) could not be loaded on the host (missing "
              "parquet/deps in this checkout) — they were NOT validated here; "
              "their in-container startup gate still applies.")
    if total_errors:
        print(f"BLOCK: {total_errors} schema/parquet mismatch(es). Fix config/schema.py "
              "or the parquet before `docker compose up`.")
        return 1
    print("All loadable DBs passed the parquet integrity check. ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
