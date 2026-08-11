"""Pinned SHA256 digests for service-loaded pickle artefacts.

Pickle deserialisation is RCE-by-design — `__reduce__` runs arbitrary code at
load time. Every `.pkl` file that BioChirp services load at startup MUST be
pinned here so `app.utils.safe_pickle.safe_pickle_load` can refuse to unpickle
a swapped file. `scripts/check_pickle_pins.py` (CI gate) re-computes the
on-disk digests and fails if they drift.

Updating an artefact:
    1. sha256sum resources/values/<file>.pkl
    2. Replace the digest below.
    3. Commit. CI re-verifies on every PR.

Naming: keys are the `name=` value passed to `safe_pickle_load`, normalised
(uppercased, `-`/`.` → `_`) the same way `BIOCHIRP_PICKLE_SHA256__*` env vars
are normalised — see `_normalise_label` below.

Migration note (2026-06-24): the single combined
`resources/values/concept_values_by_db_and_field.pkl` has been replaced by
per-DB pickles (`concept_values_<db>.pkl`). The path on disk is now a
*directory* used as a mount-point. The stale pin was removed from this table;
add per-DB pins here as needed once the new pickles are finalised.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _normalise_label(label: str) -> str:
    return label.upper().replace("-", "_").replace(".", "_")


# {normalised label: (relative path under repo, sha256 hex)}
# Path is informational only — used by the CI checker to find the file. The
# runtime loader compares against whatever path the caller passes.
PINNED_PICKLES: dict[str, dict[str, str]] = {
    # Add per-DB pickle pins here as needed, e.g.:
    # _normalise_label("concept_values_ttd.pkl"): {
    #     "path": "resources/values/concept_values_ttd.pkl",
    #     "sha256": "<sha256sum output>",
    # },
}


def pinned_sha256_for(label: str) -> Optional[str]:
    """Return the pinned digest for `label`, or None if not pinned here."""
    entry = PINNED_PICKLES.get(_normalise_label(label))
    return entry["sha256"] if entry else None


def all_pins() -> list[tuple[str, Path, str]]:
    """Iterate (label, repo-relative path, sha256) for every pinned pickle."""
    return [
        (label, Path(entry["path"]), entry["sha256"])
        for label, entry in PINNED_PICKLES.items()
    ]
