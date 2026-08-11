#!/usr/bin/env python3
"""CI gate: verify on-disk pickle SHA256s match config/pickle_pins.py.

Iterates the `PINNED_PICKLES` table in `config/pickle_pins.py`, recomputes
the SHA256 of each named file, and fails non-zero on any mismatch (or any
file missing from disk). Runs in <1s on a checkout that contains the
pickles; in a CI clone without them (e.g. an LFS-less workflow) the script
exits with an explicit "no pickle artefacts on disk" status code.

Usage:
    python scripts/check_pickle_pins.py             # verify (default)
    python scripts/check_pickle_pins.py --allow-missing
        # treat missing files as OK (CI workflows that don't pull artefacts)
    python scripts/check_pickle_pins.py --print-current
        # recompute and emit the current digest in copy-pasteable form
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from config.pickle_pins import all_pins  # noqa: E402
except Exception as exc:  # pragma: no cover
    print(f"[check_pickle_pins] cannot import config/pickle_pins.py: {exc}", file=sys.stderr)
    sys.exit(2)


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="treat files absent from disk as OK (CI workflows that skip LFS).",
    )
    ap.add_argument(
        "--print-current",
        action="store_true",
        help="print the current on-disk digest for each pin (no verification).",
    )
    args = ap.parse_args()

    pins = list(all_pins())
    if not pins:
        print("[check_pickle_pins] no pinned pickles declared — nothing to check")
        return 0

    failures: list[str] = []
    missing: list[str] = []
    ok: list[str] = []

    for label, rel_path, expected in pins:
        abs_path = REPO / rel_path
        if not abs_path.is_file():
            missing.append(f"{label} → {rel_path} (file not on disk)")
            continue
        actual = _sha256_file(abs_path)
        if args.print_current:
            print(f"{label}: {actual}  ({rel_path})")
            continue
        if actual != expected.lower():
            failures.append(
                f"{label}: expected {expected[:16]}…, got {actual[:16]}… "
                f"(path={rel_path})"
            )
        else:
            ok.append(f"{label} ({rel_path}, sha256={actual[:16]}…)")

    if args.print_current:
        return 0

    for line in ok:
        print(f"  OK    {line}")
    for line in missing:
        print(f"  MISS  {line}")
    for line in failures:
        print(f"  FAIL  {line}", file=sys.stderr)

    if failures:
        print(
            f"\n[check_pickle_pins] {len(failures)} pickle digest(s) drifted. "
            f"Either revert the artefact change or update config/pickle_pins.py "
            f"with the new SHA256.",
            file=sys.stderr,
        )
        return 1
    if missing and not args.allow_missing:
        print(
            f"\n[check_pickle_pins] {len(missing)} pinned pickle(s) absent on "
            f"disk. Pass --allow-missing in CI workflows that don't pull "
            f"the artefacts (e.g. without git LFS).",
            file=sys.stderr,
        )
        return 1
    print(
        f"[check_pickle_pins] OK — {len(ok)} pin(s) verified, "
        f"{len(missing)} missing on disk (allowed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
