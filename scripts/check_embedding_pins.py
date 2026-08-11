#!/usr/bin/env python3
"""CI gate: verify embedding models are pinned to specific commit revisions.

Fails if any entry in config/settings.py EMBEDDING_MODELS:
  - has no `revision` field,
  - has a revision that isn't a 40-character hex string (an HF commit SHA),
  - has no `status` field,
  - has `status="active"` but cannot be loaded by the active list.

Also asserts that semantic_filter/app/similarity_filtered.py passes the
`revision=` keyword to SentenceTransformer (the regression check —
catches a future contributor reverting the pin in code while leaving the
config intact).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.settings import EMBEDDING_MODELS, active_embedding_models  # noqa: E402


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOADER = REPO_ROOT / "app" / "tools" / "semantic_filter" / "app" / "similarity_filtered.py"


def check_settings() -> list[str]:
    failures: list[str] = []
    if not EMBEDDING_MODELS:
        return ["EMBEDDING_MODELS is empty in config/settings.py"]
    for name, entry in EMBEDDING_MODELS.items():
        if not isinstance(entry, dict):
            failures.append(f"{name}: entry is not a dict")
            continue
        rev = entry.get("revision")
        if not rev:
            failures.append(f"{name}: missing `revision` field")
        elif not SHA_RE.match(rev):
            failures.append(
                f"{name}: revision {rev!r} is not a 40-char hex SHA "
                f"(use the HF commit hash, not a tag or branch name)"
            )
        if "status" not in entry:
            failures.append(f"{name}: missing `status` field "
                            f"(active|disabled)")
    actives = active_embedding_models()
    if not actives:
        failures.append("active_embedding_models() returned empty — "
                        "no model would load at query time")
    return failures


def check_loader_uses_revision() -> list[str]:
    """AST-walk the loader and assert SentenceTransformer(...) is called
    with a `revision=` keyword."""
    if not LOADER.exists():
        return [f"loader file missing: {LOADER}"]
    tree = ast.parse(LOADER.read_text(), filename=str(LOADER))

    bare_loads: list[str] = []
    has_pinned_load = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_st = (isinstance(func, ast.Name) and func.id == "SentenceTransformer") or \
                (isinstance(func, ast.Attribute) and func.attr == "SentenceTransformer")
        if not is_st:
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if "revision" in kwargs:
            has_pinned_load = True
        else:
            bare_loads.append(f"line {node.lineno}: SentenceTransformer(...) "
                              f"called without `revision=` keyword")
    out: list[str] = []
    if bare_loads:
        out.extend(bare_loads)
    if not has_pinned_load:
        out.append("loader has no SentenceTransformer(..., revision=...) call "
                   "at all — pin must be applied at the load site, not just "
                   "in the config")
    return out


def main() -> int:
    failures = check_settings() + check_loader_uses_revision()
    if failures:
        print("Embedding-pin check FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    n = len(active_embedding_models())
    print(f"Embedding-pin check OK ({n} active model(s), all pinned to 40-char SHA).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
