#!/usr/bin/env python3
"""CI smoke test: import the critical non-LLM modules and validate that
their public surfaces are intact. This is the minimum signal we want from
every PR — if the planner or schema modules don't even import, no
downstream benchmark would be trustworthy.

Deliberately does NOT call any LLM, hit the network, or load the
Qdrant/Redis stack. Those belong in a heavier integration job.
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


CHECKS = [
    # (module path, attribute name expected to exist)
    ("config.schema",   "database_schemas"),
    ("config.schema",   "primary_keys_by_db"),
    ("config.schema",   "foreign_keys_by_db"),
    ("app.tools.planner.app.graph",   "concept_table_steiner_coverage_with_columns"),
    ("app.tools.planner.app.planner", "generate_plan"),
]


def main() -> int:
    failures: list[str] = []
    for mod_path, attr in CHECKS:
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:  # noqa: BLE001
            failures.append(f"import {mod_path}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue
        if not hasattr(mod, attr):
            failures.append(f"{mod_path} is missing public attribute {attr!r}")

    if failures:
        print("Smoke test FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"Smoke test OK ({len(CHECKS)} import checks passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
