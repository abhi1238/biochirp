#!/usr/bin/env python3
"""CI gate: every active parquet-backed DB is registered in VALID_DATABASES.

The expand_and_match_db tool (app/tools/expand_and_match_db/app/main.py)
validates the requested `database` against the VALID_DATABASES env var and
returns HTTP 400 for anything not in the list. A DB missing from VALID_DATABASES
silently fails EVERY query (400 -> empty filter -> 0 rows -> web fallback) — this
is exactly how msigdb was broken (present everywhere else, absent from the list).

This linter asserts that every DB with a schema_kg/inputs/<db>/ directory (i.e.
every parquet-backed DB the expand tool can be asked about) appears, upper-cased,
in EVERY VALID_DATABASES occurrence across docker-compose.yml and the
gen_compose source scripts/compose_head.yaml. DB-agnostic: the active-DB list is
discovered from disk, not hardcoded.

Exit 1 if any active DB is missing from any VALID_DATABASES list.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INPUTS = REPO / "evaluation" / "schema_kg" / "inputs"
COMPOSE_FILES = [REPO / "docker-compose.yml", REPO / "scripts" / "compose_head.yaml"]
_VD_RE = re.compile(r"VALID_DATABASES\s*=\s*([A-Za-z0-9_,]+)")


def active_dbs() -> set[str]:
    return {p.name.upper() for p in INPUTS.iterdir()
            if p.is_dir() and (p / "schema.json").is_file()}


def main() -> int:
    active = active_dbs()
    findings: list[str] = []
    n_lists = 0
    for f in COMPOSE_FILES:
        if not f.is_file():
            continue
        for ln in f.read_text().splitlines():
            m = _VD_RE.search(ln)
            if not m:
                continue
            n_lists += 1
            listed = {d.strip().upper() for d in m.group(1).split(",") if d.strip()}
            missing = sorted(active - listed)
            if missing:
                findings.append(f"{f.name}: VALID_DATABASES is missing {missing}")
    if not n_lists:
        print("check_valid_databases: no VALID_DATABASES lines found — nothing to check.", file=sys.stderr)
        return 0
    if findings:
        print(f"VALID_DATABASES coverage FAILED ({len(findings)} list(s) incomplete):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("\nAdd the missing DB(s) to every VALID_DATABASES line in docker-compose.yml "
              "AND scripts/compose_head.yaml, then recreate biochirp_expand_and_match_db_tool.",
              file=sys.stderr)
        return 1
    print(f"VALID_DATABASES coverage OK ({len(active)} active DBs across {n_lists} list(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
