#!/usr/bin/env python3
"""Schema invariant checker: queryable columns must be table-unique.

INVARIANT
---------
No *queryable* column name may appear in ≥2 NON-master tables of the same DB.

Rationale: the schema_kg planner resolves a value-mapper output (a bare column
NAME) to tables. When a queryable column is mirrored across several tables the
resolution is ambiguous; if none of those tables is a `*_master_table` the
master-collapse cannot disambiguate and the Steiner tree stitches in a spurious
(often huge) mirror → join explosion / wrong-table results. See the CTD
`interaction_actions` RCA (chemical_gene_association + chemical_phenotype_ixn).

Master↔association concept mirrors (e.g. `gene_symbol` in gene_master AND an
association table) are EXEMPT: that denormalization is intentional and the
planner's master-collapse handles it. Only join keys / internal IDs
(queryable=false) may otherwise be shared.

Use a table-specific prefix to fix a violation, e.g.
  chemical_gene_association_ctd.interaction_actions     → gene_interaction_actions
  chemical_phenotype_ixn_ctd.interaction_actions        → phenotype_interaction_actions

Exit code 0 = clean, 1 = violations found. Run in CI / onboarding to keep the
invariant from drifting as new DBs are added. Generic; no per-DB logic.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_INPUTS_ROOT = os.path.join(os.path.dirname(_HERE), "evaluation", "schema_kg", "inputs")


def _violations_for_db(db: str, queryable_json: dict) -> dict:
    """{column: [tables]} for queryable columns shared across ≥2 non-master tables."""
    col2tabs: dict = defaultdict(set)
    for key, val in queryable_json.items():
        if key == "_comment" or not val:
            continue
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != db:
            continue
        _db, table, col = parts
        col2tabs[col].add(table)
    out = {}
    for col, tabs in col2tabs.items():
        if len(tabs) < 2:
            continue
        if any("_master_table" in t for t in tabs):
            continue  # master-collapse handles concept mirrors — exempt
        out[col] = sorted(tabs)
    return out


def main(argv: list) -> int:
    only = set(argv[1:])  # optional: restrict to named DBs
    total = 0
    dbs_hit = 0
    for qf in sorted(glob.glob(os.path.join(_INPUTS_ROOT, "*", "queryable.json"))):
        db = os.path.basename(os.path.dirname(qf))
        if only and db not in only:
            continue
        try:
            qj = json.load(open(qf))
        except Exception as exc:
            print(f"  [WARN] {db}: cannot read queryable.json ({exc})")
            continue
        viol = _violations_for_db(db, qj)
        if viol:
            dbs_hit += 1
            total += len(viol)
            print(f"  [FAIL] {db}: {len(viol)} queryable column(s) shared across only non-master tables:")
            for col, tabs in sorted(viol.items()):
                print(f"         {col}  ->  {tabs}")
    if total:
        print(f"\nINVARIANT VIOLATED: {total} shared queryable column(s) across {dbs_hit} DB(s).")
        print("Fix with a table-specific prefix (see this file's docstring).")
        return 1
    print("OK: every queryable column is table-unique (or a master-collapse-exempt concept mirror).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
