#!/usr/bin/env python3
"""CI gate: verify per-database provenance manifests exist and match.

For each database/<db>/ that contains parquet files, assert:

  1. database/<db>/MANIFEST.json exists.
  2. database/<db>/CHECKSUMS.txt exists.
  3. The set of parquet files on disk matches MANIFEST.json["files"].
  4. (Optional, controlled by --verify-hashes) Recompute SHA-256 of
     every file and compare with MANIFEST.json. Off by default in CI
     because the dataset is multi-GB; on for local pre-release checks.

Run scripts/build_database_manifests.py after changing any parquet file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_ROOT = REPO_ROOT / "database"

# Databases that are live-API integrations rather than parquet snapshots.
# They may still cache responses to parquet for performance, but they
# have no fixed snapshot_date semantics. The checker reports info on
# them but doesn't require version/snapshot_date.
LIVE_API_DATABASES = {"opentargets"}


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def check_one(db_dir: Path, verify_hashes: bool) -> list[str]:
    db = db_dir.name
    failures: list[str] = []
    parquets = sorted(p for p in db_dir.rglob("*.parquet"))
    if not parquets:
        return failures   # No parquets → no manifest required.

    manifest_path = db_dir / "MANIFEST.json"
    checksums_path = db_dir / "CHECKSUMS.txt"

    if not manifest_path.exists():
        failures.append(f"{db}: MANIFEST.json missing "
                        f"(run scripts/build_database_manifests.py)")
        return failures
    if not checksums_path.exists():
        failures.append(f"{db}: CHECKSUMS.txt missing "
                        f"(run scripts/build_database_manifests.py)")

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        failures.append(f"{db}: MANIFEST.json is not valid JSON: {e}")
        return failures

    if db not in LIVE_API_DATABASES:
        if not manifest.get("version"):
            failures.append(f"{db}: MANIFEST.json has no `version` "
                            f"(check SOURCE.md `**Version:**` line)")
        if not manifest.get("snapshot_date"):
            failures.append(f"{db}: MANIFEST.json has no `snapshot_date` "
                            f"(check SOURCE.md `**Snapshot date:**` line)")

    listed = {f["path"]: f for f in manifest.get("files", [])}
    # Mirror build_database_manifests.py's exclusion policy exactly: skip
    # `_raw_*` ETL intermediates and any parquet inside a hidden directory
    # (name starts with `.`, e.g. `.backup_*`) or a decommissioned archive
    # (`_decommissioned*`). These are deliberately omitted from the published
    # MANIFEST.json, so the checker must not flag them as "on disk but missing".
    on_disk = {p.relative_to(db_dir).as_posix() for p in parquets
               if not p.name.startswith("_raw_")
               and not any(
                   part.startswith(".") or part.startswith("_decommissioned")
                   for part in p.relative_to(db_dir).parts[:-1]
               )}
    missing_from_manifest = on_disk - set(listed)
    missing_from_disk     = set(listed) - on_disk

    if missing_from_manifest:
        failures.append(f"{db}: files on disk but not in MANIFEST.json: "
                        f"{sorted(missing_from_manifest)} — regenerate")
    if missing_from_disk:
        failures.append(f"{db}: files in MANIFEST.json but not on disk: "
                        f"{sorted(missing_from_disk)}")

    if verify_hashes:
        for rel, entry in listed.items():
            p = db_dir / rel
            if not p.exists():
                continue
            got = sha256_of(p)
            if got != entry.get("sha256"):
                failures.append(f"{db}/{rel}: sha256 mismatch — "
                                f"manifest={entry.get('sha256')[:12]}…, "
                                f"actual={got[:12]}… — regenerate or "
                                f"investigate data tampering")

    return failures


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-hashes", action="store_true",
                    help="recompute SHA-256 of every parquet and compare "
                         "(slow; off by default for CI)")
    args = ap.parse_args(argv)

    if not DB_ROOT.is_dir():
        print(f"ERROR: {DB_ROOT} not found", file=sys.stderr)
        return 1

    all_failures: list[str] = []
    n_db_with_parquets = 0
    for d in sorted(DB_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if not any(d.rglob("*.parquet")):
            continue
        n_db_with_parquets += 1
        all_failures.extend(check_one(d, args.verify_hashes))

    if all_failures:
        print(f"Database manifest check FAILED ({len(all_failures)} issue(s)):",
              file=sys.stderr)
        for f in all_failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    mode = "with hash verification" if args.verify_hashes else "metadata-only"
    print(f"Database manifest check OK ({n_db_with_parquets} databases, {mode}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
