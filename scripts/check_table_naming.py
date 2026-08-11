#!/usr/bin/env python3
"""Lint the schema_kg table-naming convention: every table ends with `_<db>`.

WHY a convention at all
-----------------------
Table names appear in 6 artifacts per DB (schema_kg/inputs/<db>/schema.json,
queryable.json, concept_type.json; config/schema.py; dbs/<db>/manifest.yaml;
optional parquet_map.json) plus the loader's registered dict keys. The runtime
planner is deliberately `_<db>`-suffix-AGNOSTIC (it normalises the suffix when
resolving — see app/per_db_tool/schema_kg_planner.py), which is why DBs work
today despite an inconsistent convention (hcdt/ttd bare, others suffixed).

So this is a CONSISTENCY lint, not a correctness gate: we want NEW databases to
adopt one convention — the `_<db>` suffix — so the artifacts read uniformly and
scripts/schema_manifest_sync.py reconciles them automatically. Existing
divergence is grandfathered via a baseline so we never force a risky mass-rename
of working data.

Behaviour
---------
  python scripts/check_table_naming.py                # report; exit 1 on NEW violations
  python scripts/check_table_naming.py --strict       # exit 1 on ANY violation (incl. baseline)
  python scripts/check_table_naming.py --update-baseline   # accept current state as the baseline

GROUND TRUTH for the DB set + tables = schema_kg/inputs/<db>/schema.json (the
same files config/schema_kg_dbs.discover_schema_kg_dbs scans).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "evaluation" / "schema_kg" / "inputs"
BASELINE = ROOT / "scripts" / "table_naming_baseline.json"


def _db_tables(db: str) -> list[str]:
    """Top-level table keys from schema_kg/inputs/<db>/schema.json."""
    p = INPUTS / db / "schema.json"
    doc = json.loads(p.read_text())
    body = doc.get(db) or next(iter(doc.values()))
    return list(body.keys())


def _discover_dbs() -> list[str]:
    return sorted(d.name for d in INPUTS.iterdir()
                  if d.is_dir() and (d / "schema.json").is_file())


def _violations() -> dict[str, list[str]]:
    """{db: [tables NOT ending with _<db>]} — the current non-compliant set."""
    out: dict[str, list[str]] = {}
    for db in _discover_dbs():
        bad = sorted(t for t in _db_tables(db) if not t.endswith(f"_{db}"))
        if bad:
            out[db] = bad
    return out


def _load_baseline() -> dict[str, list[str]]:
    if not BASELINE.is_file():
        return {}
    return json.loads(BASELINE.read_text())


def cmd_update_baseline() -> int:
    viol = _violations()
    BASELINE.write_text(json.dumps(viol, indent=2, sort_keys=True) + "\n")
    n = sum(len(v) for v in viol.values())
    print(f"Wrote {BASELINE.relative_to(ROOT)}: {n} grandfathered tables "
          f"across {len(viol)} DB(s).")
    return 0


def cmd_check(strict: bool) -> int:
    viol = _violations()
    baseline = _load_baseline()
    new_viol: dict[str, list[str]] = {}
    stale_baseline: dict[str, list[str]] = {}

    for db, bad in viol.items():
        accepted = set(baseline.get(db, []))
        fresh = [t for t in bad if t not in accepted]
        if fresh:
            new_viol[db] = fresh
    # Baseline entries that are now compliant/removed (baseline can shrink).
    for db, accepted in baseline.items():
        live = set(viol.get(db, []))
        gone = [t for t in accepted if t not in live]
        if gone:
            stale_baseline[db] = gone

    total_viol = sum(len(v) for v in viol.values())
    print(f"Canonical convention: every schema_kg table name ends with `_<db>`.")
    print(f"  {len(_discover_dbs())} DBs scanned · {total_viol} table(s) currently "
          f"non-compliant ({sum(len(v) for v in baseline.values())} grandfathered).\n")

    if strict and viol:
        print("STRICT: tables not following the `_<db>` convention:")
        for db in sorted(viol):
            print(f"  {db}: {viol[db]}")
        return 1

    if new_viol:
        print("✗ NEW convention violations (a DB/table added without the `_<db>` "
              "suffix — rename it or run --update-baseline to accept deliberately):")
        for db in sorted(new_viol):
            print(f"  {db}: {new_viol[db]}")
        return 1

    if stale_baseline:
        print("ℹ Baseline can be trimmed (these grandfathered tables are now "
              "compliant or gone — re-run --update-baseline):")
        for db in sorted(stale_baseline):
            print(f"  {db}: {stale_baseline[db]}")

    print("✓ No new table-naming convention violations.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strict", action="store_true",
                    help="fail on ANY violation, including grandfathered ones")
    ap.add_argument("--update-baseline", action="store_true",
                    help="snapshot the current non-compliant set as the accepted baseline")
    args = ap.parse_args()
    if args.update_baseline:
        return cmd_update_baseline()
    return cmd_check(args.strict)


if __name__ == "__main__":
    sys.exit(main())
