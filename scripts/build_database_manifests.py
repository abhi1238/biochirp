#!/usr/bin/env python3
"""Generate per-database provenance manifests for the parquet snapshots.

For every directory under database/<db>/ that has at least one parquet
file, write two artifacts:

  1. database/<db>/MANIFEST.json   — structured JSON readable by tools.
  2. database/<db>/CHECKSUMS.txt   — `sha256sum`-format text, so an
                                    auditor can verify with the standard
                                    coreutils tool:
                                        cd database/<db>
                                        sha256sum -c CHECKSUMS.txt

Fields written into MANIFEST.json:

  - database            short name (directory name)
  - version             from SOURCE.md `**Version:**`
  - snapshot_date       from SOURCE.md `**Snapshot date:**`
  - license             from SOURCE.md `**License:**`
  - url                 from SOURCE.md `**URL:**`
  - generated_at        ISO 8601 UTC timestamp when the manifest was built
  - generator           script identifier + commit-of-this-script (if git)
  - files               list of {path, bytes, sha256, n_rows}
                        — n_rows is from the parquet metadata (cheap; no
                          full read of the data).

Why this exists
---------------
The README claims "every row carries verifiable per-DB provenance" via
`_db_version` and `_db_snapshot_date` columns. Reality: those columns
are added at request time by the orchestrator from in-Python constants,
NOT persisted in the parquet schema. An auditor who downloads the
Zenodo bundle and inspects the parquet alone cannot verify provenance.

This manifest closes that gap WITHOUT having to rewrite every parquet
file. The parquet + sidecar JSON + CHECKSUMS.txt together are
verifiable from the artifact alone.

(A separate task remains: at the next ETL run, also stamp the columns
into the parquet itself. The manifest is the no-data-rewrite fix.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_ROOT = REPO_ROOT / "database"


SOURCE_FIELDS = {
    "version":       re.compile(r"\*\*Version:\*\*\s*(.+)"),
    "snapshot_date": re.compile(r"\*\*Snapshot date:\*\*\s*(.+)"),
    "license":       re.compile(r"\*\*License:\*\*\s*(.+)"),
    "url":           re.compile(r"\*\*URL:\*\*\s*(.+)"),
    "reference":     re.compile(r"\*\*Reference:\*\*\s*(.+)"),
}


def parse_source_md(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for key, rx in SOURCE_FIELDS.items():
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def parquet_row_count(path: Path) -> int | None:
    """Cheap row count from parquet metadata (no full read of the data)."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    try:
        return pq.ParquetFile(str(path)).metadata.num_rows
    except Exception:
        return None


def git_commit() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None


def build_one(db_dir: Path, dry_run: bool) -> dict:
    db_name = db_dir.name
    # Skip `_raw_*.parquet` (intermediate ETL artefacts) and any parquets
    # inside hidden directories (those whose name starts with `.`, such as
    # `.backup_*` or `.git`) or decommissioned archives (`_decommissioned*`,
    # e.g. retired v1 snapshots) — these are not part of the published snapshot.
    parquet_files = sorted(
        p for p in db_dir.rglob("*.parquet")
        if not p.name.startswith("_raw_")
        and not any(
            part.startswith(".") or part.startswith("_decommissioned")
            for part in p.relative_to(db_dir).parts[:-1]
        )
    )
    source = parse_source_md(db_dir / "SOURCE.md")

    # Preserve curated provenance: if SOURCE.md does not yield a metadata
    # field (e.g. a SOURCE.md whose key format the parser doesn't match),
    # fall back to the value already present in the existing MANIFEST.json
    # rather than silently overwriting it with null. Never overrides a value
    # that SOURCE.md *does* provide.
    existing: dict = {}
    manifest_path = db_dir / "MANIFEST.json"
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    def _meta(field: str):
        return source.get(field) or existing.get(field)

    files_meta = []
    for p in parquet_files:
        rel = p.relative_to(db_dir).as_posix()
        files_meta.append({
            "path":   rel,
            "bytes":  p.stat().st_size,
            "sha256": sha256_of(p),
            "n_rows": parquet_row_count(p),
        })

    manifest = {
        "database":      db_name,
        "version":       _meta("version"),
        "snapshot_date": _meta("snapshot_date"),
        "license":       _meta("license"),
        "url":           _meta("url"),
        "reference":     _meta("reference"),
        "generated_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator":     "scripts/build_database_manifests.py",
        "git_commit":    git_commit(),
        "files":         files_meta,
    }

    checksums_path = db_dir / "CHECKSUMS.txt"

    if dry_run:
        print(f"[dry-run] would write {manifest_path} ({len(files_meta)} files)")
        return manifest

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # `sha256sum -c CHECKSUMS.txt` format: two spaces between hash and path
    # (single space + asterisk for binary mode also valid; we use the
    # two-space "text" form because it works with `sha256sum -c`).
    checksums_path.write_text(
        "".join(f"{f['sha256']}  {f['path']}\n" for f in files_meta),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written; do not write files")
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to these database names")
    args = ap.parse_args(argv)

    if not DB_ROOT.is_dir():
        print(f"ERROR: {DB_ROOT} not found", file=sys.stderr)
        return 1

    db_dirs = sorted(d for d in DB_ROOT.iterdir() if d.is_dir())
    if args.only:
        db_dirs = [d for d in db_dirs if d.name in args.only]

    total_files = 0
    total_bytes = 0
    skipped = []
    for d in db_dirs:
        parquets = list(d.rglob("*.parquet"))
        if not parquets:
            skipped.append(d.name)
            continue
        m = build_one(d, dry_run=args.dry_run)
        total_files += len(m["files"])
        total_bytes += sum(f["bytes"] for f in m["files"])
        print(f"{d.name:14s}  {len(m['files']):3d} parquet(s)  "
              f"v={m['version'] or '(none)'!r:<48s}  date={m['snapshot_date'] or '(none)'}")

    print(f"\nDone. {total_files} parquet files, "
          f"{total_bytes/1e9:.2f} GB total.")
    if skipped:
        print(f"Skipped (no parquet): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
