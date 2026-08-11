#!/usr/bin/env python3
"""CI gate: no FK-ISOLATED tables in the schema_kg planner inputs.

The schema_kg planner (app/per_db_tool/schema_kg_planner.py) builds its join
graph from schema_kg/inputs/<db>/{schema.json,queryable.json}. It treats a
NON-QUERYABLE column shared by >=2 tables as a foreign-key edge (see
_build_pruned_subgraph). A table whose ONLY cross-table columns are QUERYABLE
shares no join key -> it is unreachable in the Steiner graph -> "FK-ISOLATED".

That is exactly the bug class that broke orphanet & msigdb gene_master this
session: their gene_master shared only the queryable gene_symbol with the
association, so the planner could not route a disease/geneset filter through to
the gene table ("how many genes for <disease>" returned ALL genes). The fix was
a non-queryable gene_id join key (PK in master, FK in association) — the uniform
v2 standard. This linter enforces that invariant for EVERY db so the next one is
caught automatically instead of by hand.

DB-agnostic: reads only the schema_kg inputs; no per-DB logic. Single-table DBs
and genuine standalone lookup tables (e.g. chembl activity_stds_lookup, queried
single-table) are legitimately isolated — list them in the baseline.

Exit 1 on any NEW isolated table not in scripts/.schema_kg_fk_isolation_baseline.txt.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INPUTS = REPO / "evaluation" / "schema_kg" / "inputs"
BASELINE = REPO / "scripts" / ".schema_kg_fk_isolation_baseline.txt"

# Non-queryable columns that are FREE-TEXT/descriptive payload, NOT join keys —
# mirrors _NON_FK_DESCRIPTIVE_COLS in app/per_db_tool/schema_kg_planner.py so this
# linter classifies edges exactly as the planner does.
_NON_FK_DESCRIPTIVE = frozenset({
    "synonyms", "definition", "description", "name", "gene_name", "drug_definition",
    "tree_numbers", "parent_ids", "slim_mappings", "gene_forms",
    "alt_gene_ids", "alt_disease_ids", "alt_drug_ids", "alt_ids",
    "uniprot_ids", "interaction_text", "comment", "comments", "notes",
})


def _load(db: str) -> tuple[dict, dict]:
    schema = json.loads((INPUTS / db / "schema.json").read_text()).get(db, {})
    qpath = INPUTS / db / "queryable.json"
    q = {}
    if qpath.is_file():
        q = {k: v for k, v in json.loads(qpath.read_text()).items() if not k.startswith("_")}
    return schema, q


def find_isolated() -> list[str]:
    findings: list[str] = []
    for db_dir in sorted(p for p in INPUTS.iterdir() if p.is_dir()):
        db = db_dir.name
        if not (db_dir / "schema.json").is_file():
            continue
        schema, q = _load(db)
        tables = {t: c for t, c in schema.items() if isinstance(c, dict)}
        if len(tables) < 2:
            continue  # a one-table DB cannot have isolation
        # Mirror the planner's FK-edge logic (_build_pruned_subgraph): an FK edge
        # forms on a column that is NON-QUERYABLE in >=2 tables. A column that is
        # non-queryable in only one table (or queryable here) is NOT a join key —
        # e.g. target_name non-queryable in drug_mechanism but queryable in
        # uniprot_xwalk is the C5 name-based-join smell, not a real edge.
        col_tables: dict[str, set] = defaultdict(set)        # any occurrence
        nonq_tables: dict[str, set] = defaultdict(set)       # non-queryable occurrences
        for t, cols in tables.items():
            for c in cols:
                if c.startswith("_"):
                    continue
                col_tables[c].add(t)
                if q.get(f"{db}.{t}.{c}") is False and c.lower() not in _NON_FK_DESCRIPTIVE:
                    nonq_tables[c].add(t)
        fk_cols = {c for c, ts in nonq_tables.items() if len(ts) >= 2}
        for t, cols in tables.items():
            real_cols = [c for c in cols if not c.startswith("_")]
            # this table participates in an FK edge only via a column that is
            # non-queryable HERE and an fk_col (non-queryable in >=2 tables).
            shares_fk = any(c in fk_cols and t in nonq_tables[c] for c in real_cols)
            shares_any = any(len(col_tables[c]) >= 2 for c in real_cols)
            if shares_any and not shares_fk:
                qshared = [c for c in real_cols if len(col_tables[c]) >= 2]
                findings.append(f"{db}.{t} :: links only via queryable col(s) {sorted(qshared)} — no non-queryable id key")
    return findings


def _load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    return {ln.split("::")[0].strip() + " :: " + ln.split("::", 1)[1].strip()
            if "::" in ln else ln.strip()
            for ln in BASELINE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}


def main() -> int:
    findings = find_isolated()
    # Match on the "db.table" prefix so the descriptive tail can evolve.
    def key(f: str) -> str:
        return f.split("::")[0].strip()
    baseline_keys = {key(b) for b in _load_baseline()}
    new = [f for f in findings if key(f) not in baseline_keys]
    n_db = len([p for p in INPUTS.iterdir() if p.is_dir() and (p / "schema.json").is_file()])
    if new:
        print(f"schema_kg FK-isolation check FAILED: {len(new)} NEW isolated table(s):", file=sys.stderr)
        for f in new:
            print(f"  {f}", file=sys.stderr)
        print("\nFix: give the table a NON-QUERYABLE *_id join key shared with a master "
              "(the v2 standard — see orphanet/msigdb gene_id). If it is a genuine "
              f"single-table lookup, add its 'db.table' to {BASELINE.name}.", file=sys.stderr)
        return 1
    print(f"schema_kg FK-isolation OK ({n_db} databases; {len(findings)} known isolated in baseline).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
