#!/usr/bin/env python3
"""CI gate: lint config/schema.py for FK consistency issues.

Schema FK relationships in BioChirp are derived programmatically in
generate_foreign_keys() — any `_id`-suffixed column in an `_association`
table that matches an `_id`-suffixed column in a `_master_table` of the
same DB becomes an FK. The derivation is permissive: columns that don't
match are silently dropped. This linter surfaces the dropped/broken cases
that would otherwise cause cross-DB joins to produce wrong rows.

Checks performed (per database):
  C1  Every `_master_table` has exactly one `*_id` primary key.
  C2  Every `_master_table` PK is referenced by at least one
      `_association` table (orphan masters are a smell).
  C3  Every `_association` table has at least two `*_id` columns
      (otherwise it cannot be a relationship table).
  C4  Every `*_id` column in an `_association` table resolves to a
      `_master_table` in the same DB (no dangling FKs).
  C5  Suspicious name-based FK: if an `_association` table has a
      `<entity>_name` (or other non-`_id`) text column AND a
      `<entity>_master_table` with `<entity>_id` PK exists in the same DB,
      flag — the join column is almost certainly the wrong one and the
      column should be `<entity>_id`. This is the bug class flagged in
      the 2026-05-12 reviewer audit for CIViC.variant_evidence_association
      (drug_name should be drug_id) and DrugCentral.{drug_disease,
      drug_target}_association.

Exit 1 if any C1–C4 violation is found, or any C5 violation that is not
in the documented exemption list at the bottom of this file. Print a
single-line summary on success.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = REPO_ROOT / "config" / "schema.py"


def load_database_schemas() -> dict:
    """Extract the `database_schemas = {...}` dict literal via AST.

    Avoids importing config.schema (which pulls pydantic + the rest of the
    app), so this linter runs in a stdlib-only CI job.
    """
    tree = ast.parse(SCHEMA_FILE.read_text(), filename=str(SCHEMA_FILE))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "database_schemas"
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(
        f"Could not find `database_schemas = {{...}}` literal in {SCHEMA_FILE}"
    )


database_schemas = load_database_schemas()


# (db, table, column) triples explicitly allowed to be name-based even
# though a matching <entity>_master_table exists. Document the reason
# next to each entry. Empty by default — add only after a manual review
# that confirms the name-based join is semantically intended.
NAME_FK_EXEMPTIONS: set[tuple[str, str, str]] = set()


def lint() -> list[str]:
    findings: list[str] = []

    for db, tables in database_schemas.items():
        master_pks: dict[str, str] = {}        # master_table_name → its <entity>_id column
        master_entity_to_id: dict[str, str] = {}  # "<entity>" → "<entity>_id"
        for tname, cols in tables.items():
            if tname.endswith("_master_table"):
                id_cols = [c for c in cols if c.endswith("_id")]
                if len(id_cols) != 1:
                    findings.append(
                        f"[C1] {db}.{tname}: must have exactly one *_id PK, got {id_cols}"
                    )
                if id_cols:
                    master_pks[tname] = id_cols[0]
                    entity = id_cols[0][:-3]  # strip "_id"
                    master_entity_to_id[entity] = id_cols[0]

        # C2: orphan master tables
        referenced_ids: set[str] = set()
        for tname, cols in tables.items():
            if "_association" in tname:
                for c in cols:
                    if c.endswith("_id"):
                        referenced_ids.add(c)
        for mtable, pk in master_pks.items():
            if pk not in referenced_ids:
                findings.append(
                    f"[C2] {db}.{mtable}: PK {pk!r} is not referenced by any "
                    f"_association table (orphan master)"
                )

        # C3, C4, C5: association table checks
        valid_pk_cols = set(master_pks.values())
        for tname, cols in tables.items():
            if "_association" not in tname:
                continue
            id_cols = [c for c in cols if c.endswith("_id")]
            if len(id_cols) < 2:
                findings.append(
                    f"[C3] {db}.{tname}: association tables need ≥2 *_id columns, "
                    f"got {id_cols}"
                )
            for c in id_cols:
                if c not in valid_pk_cols:
                    findings.append(
                        f"[C4] {db}.{tname}: FK column {c!r} has no matching "
                        f"_master_table in this database (dangling FK)"
                    )
            # C5: name-based FK smell
            for c in cols:
                if c.endswith("_id"):
                    continue
                # Strip common text suffixes to derive entity stem.
                m = re.match(r"^(.+?)_(name|symbol|label|title)$", c)
                if not m:
                    continue
                entity = m.group(1)
                if entity in master_entity_to_id and (db, tname, c) not in NAME_FK_EXEMPTIONS:
                    findings.append(
                        f"[C5] {db}.{tname}: column {c!r} appears to be a "
                        f"name-based join key while {db} also has "
                        f"{entity}_master_table with PK "
                        f"{master_entity_to_id[entity]!r}. Use the *_id "
                        f"column for joins, or add the (db, table, col) "
                        f"triple to NAME_FK_EXEMPTIONS with justification."
                    )

    return findings


BASELINE = REPO_ROOT / "scripts" / ".schema_fks_baseline.txt"


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    return {
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def main() -> int:
    findings = lint()
    baseline = load_baseline()

    # Ratchet: CI fails only on findings not in the baseline. Findings that
    # have been fixed are also reported so the baseline can be tightened.
    new_issues = [f for f in findings if f not in baseline]
    fixed_in_baseline = [b for b in baseline if b not in findings]

    n_dbs = len(database_schemas)
    n_tables = sum(len(t) for t in database_schemas.values())

    if fixed_in_baseline:
        print(
            f"NOTE: {len(fixed_in_baseline)} baseline issue(s) appear fixed — "
            f"regenerate {BASELINE.name} to tighten the gate:",
            file=sys.stderr,
        )
        for f in fixed_in_baseline:
            print(f"  - {f}", file=sys.stderr)

    if new_issues:
        print(
            f"Schema FK lint FAILED: {len(new_issues)} NEW issue(s) not in "
            f"baseline ({BASELINE.name}):",
            file=sys.stderr,
        )
        for f in new_issues:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nTo accept these as pre-existing tech debt, append them to "
            f"{BASELINE.name}. To fix, edit config/schema.py.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Schema FK lint OK ({n_dbs} databases, {n_tables} tables; "
        f"{len(findings)} known issue(s) in baseline)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
