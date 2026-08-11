#!/usr/bin/env python3
"""Keep config/schema.py and dbs/<db>/manifest.yaml honest about each other.

Two parquet-planner facts about every DB live in two files today:

  * config/schema.py    `database_schemas[db][table] = [col, ...]`
        — the AUTHORITATIVE set the Polars/Steiner planner joins on. Reflects
          actual parquet columns, the single-PK-per-master validator, and the
          hand-curated Phase-3 expansion tables.
  * dbs/<db>/manifest.yaml  `schema.tables.<table>.key_columns[].name`
        — the documentation / KG-facing description (rich per-column prose,
          examples) consumed by onboarding + the schema-mapper embeddings.

They have drifted, partly on purpose (manifest documents xref columns that
schema.py deliberately omits to satisfy the single-PK rule) and partly by
neglect (ctd uses a different `_v2` naming
scheme). This tool surfaces that drift instead of letting it rot silently.

Why not just regenerate schema.py from the manifests?
  Because the manifests are currently not consistently
  named (ctd `_v2`, `gene_master_table_<db>` suffixes), and schema.py encodes
  load-bearing decisions (single-PK subsetting, decoration tables) the manifests
  don't. Regenerating today would BREAK the planner. So `emit` only PRINTS a
  proposed block for review/scaffolding — it never writes schema.py.

Usage:
  python3 scripts/schema_manifest_sync.py                 # check ALL dbs (report)
  python3 scripts/schema_manifest_sync.py --db hcdt       # check one db
  python3 scripts/schema_manifest_sync.py --strict        # exit 1 if any drift
  python3 scripts/schema_manifest_sync.py --emit newdb    # print a schema.py block
                                                          # derived from the manifest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DBS_DIR = ROOT / "dbs"


def _load_database_schemas() -> dict:
    sys.path.insert(0, str(ROOT))
    from config.schema import database_schemas  # noqa: E402 (needs ROOT on path)
    return database_schemas


def _manifest_path(db: str) -> Path:
    return DBS_DIR / db / "manifest.yaml"


def manifest_tables(db: str) -> dict[str, list[str]]:
    """Return {table_name: [col, ...]} from the manifest's schema.tables block.

    Returns {} when the manifest has no tables (remote-API DBs) or is absent.
    """
    p = _manifest_path(db)
    if not p.is_file():
        return {}
    doc = yaml.safe_load(p.read_text()) or {}
    tables = (doc.get("schema") or {}).get("tables") or {}
    out: dict[str, list[str]] = {}
    for tname, spec in tables.items():
        cols = [c["name"] for c in (spec.get("key_columns") or []) if c.get("name")]
        out[tname] = cols
    return out


def _norm(table: str, db: str) -> str:
    """Normalise a table name so the `_<db>` suffix convention is transparent.

    The slug-suffix convention is applied INCONSISTENTLY across the repo: some
    DBs suffix the manifest side (gene_master_table_clinvar) while schema.py uses
    the bare name; others do the reverse (schema.py has disease_xref_orphanet
    while the manifest documents disease_xref). Both are the SAME table, so this
    is stripped from WHICHEVER side carries it — `check_db` applies `_norm` to
    both the manifest and the schema.py table names before matching. Other
    divergences (ctd's `_v2`, genuinely renamed tables) do NOT carry the slug
    suffix and so still surface as unmatched.
    """
    suffix = f"_{db}"
    if table.endswith(suffix) and table != suffix:
        return table[: -len(suffix)]
    return table


def check_db(db: str, schema_tables: dict[str, list[str]]) -> dict:
    """Compare one DB. Returns a findings dict (empty lists == in sync)."""
    man_raw = manifest_tables(db)
    # Normalise BOTH sides through the slug-suffix convention so a table named
    # `disease_xref` (manifest) and `disease_xref_orphanet` (schema.py) — or the
    # reverse — match instead of being false-flagged as table drift. Union the
    # columns on the rare chance two raw names collapse to the same normalised
    # key, so no column is silently dropped from the comparison.
    def _fold(raw: dict[str, list[str]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for t, cols in raw.items():
            out.setdefault(_norm(t, db), set()).update(cols)
        return out

    man = _fold(man_raw)
    sch = _fold(schema_tables)

    only_schema = sorted(set(sch) - set(man))
    only_manifest = sorted(set(man) - set(sch))
    col_findings = {}
    for t in sorted(set(sch) & set(man)):
        schema_only_cols = sorted(sch[t] - man[t])   # in planner, undocumented
        manifest_only_cols = sorted(man[t] - sch[t])  # documented, not joined
        if schema_only_cols or manifest_only_cols:
            col_findings[t] = {
                "schema_only": schema_only_cols,
                "manifest_only": manifest_only_cols,
            }
    return {
        "has_manifest_tables": bool(man_raw),
        "tables_only_in_schema": only_schema,
        "tables_only_in_manifest": only_manifest,
        "column_drift": col_findings,
    }


def _print_db_report(db: str, f: dict) -> bool:
    """Print a human report for one DB. Return True if any drift was found."""
    if not f["has_manifest_tables"]:
        print(f"  {db}: manifest has no schema.tables block (remote/API DB) — skipped")
        return False
    drift = (f["tables_only_in_schema"] or f["tables_only_in_manifest"]
             or f["column_drift"])
    if not drift:
        print(f"  {db}: ✓ in sync")
        return False
    print(f"  {db}: ⚠ drift")
    if f["tables_only_in_schema"]:
        print(f"      tables in schema.py but NOT documented in manifest:")
        for t in f["tables_only_in_schema"]:
            print(f"        - {t}")
    if f["tables_only_in_manifest"]:
        print(f"      tables in manifest but NOT in schema.py (not joinable):")
        for t in f["tables_only_in_manifest"]:
            print(f"        - {t}")
    for t, cols in f["column_drift"].items():
        if cols["schema_only"]:
            print(f"      [{t}] cols joined by planner but undocumented in manifest: "
                  f"{cols['schema_only']}")
        if cols["manifest_only"]:
            print(f"      [{t}] cols documented in manifest but NOT joinable: "
                  f"{cols['manifest_only']}")
    return True


def cmd_check(dbs: list[str], strict: bool) -> int:
    schemas = _load_database_schemas()
    targets = dbs or sorted(schemas)
    print(f"Checking schema.py ↔ manifest for {len(targets)} DB(s):\n")
    any_drift = False
    for db in targets:
        if db not in schemas:
            print(f"  {db}: ✗ not in config.schema.database_schemas")
            any_drift = True
            continue
        f = check_db(db, schemas[db])
        if _print_db_report(db, f):
            any_drift = True
    print()
    if any_drift:
        print("Drift detected. This is a REPORT — schema.py is authoritative for the "
              "planner; reconcile the manifest (or vice-versa) deliberately.")
        if strict:
            return 1
    else:
        print("All checked DBs are in sync. ✓")
    return 0


def cmd_emit(db: str) -> int:
    """Print a config.schema-style dict block derived from the manifest.

    Scaffolding for a NEW DB (or a diff aid for an existing one). NEVER writes
    schema.py — the operator pastes/edits the curated result by hand.
    """
    man = manifest_tables(db)
    if not man:
        print(f"# {db}: manifest has no schema.tables block — nothing to emit",
              file=sys.stderr)
        return 1
    lines = [f'    # ── derived from dbs/{db}/manifest.yaml by '
             f'scripts/schema_manifest_sync.py --emit {db}',
             f'    # REVIEW BEFORE USE: apply single-PK-per-master subsetting and',
             f'    # drop any column absent from the actual parquet.',
             f'    "{db}": {{']
    for t in sorted(man):
        norm = _norm(t, db)
        cols = man[t]
        master_ids = [c for c in cols if c.endswith("_id")]
        warn = ""
        if norm.endswith("_master_table") and len(master_ids) != 1:
            warn = (f"  # ⚠ single-PK validator needs exactly one *_id "
                    f"(found {master_ids})")
        col_repr = ", ".join(f'"{c}"' for c in cols)
        lines.append(f'        "{norm}": [{col_repr}],{warn}')
    lines.append("    },")
    print("\n".join(lines))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", action="append", default=[],
                    help="restrict to this DB slug (repeatable)")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any drift is found (default: report only)")
    ap.add_argument("--emit", metavar="DB",
                    help="print a schema.py dict block derived from the DB's "
                         "manifest (scaffolding only; never writes schema.py)")
    args = ap.parse_args()

    if args.emit:
        return cmd_emit(args.emit)
    return cmd_check(args.db, args.strict)


if __name__ == "__main__":
    sys.exit(main())
