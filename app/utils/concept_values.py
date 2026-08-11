"""Per-DB concept-value loader.

Replaces the single combined pickle (concept_values_by_db_and_field.pkl) with
per-DB pickles produced by scripts/build_concept_values.py.

Each pickle: resources/values/concept_values_<db>.pkl → {field: [values]}

Consumers call get_db_concept_values(db) to get the candidate pool for a DB.
Results are cached in-process — the first call for each DB pays the disk I/O;
subsequent calls return the cached dict instantly.

Backward-compat fallback: if the per-DB pickle is missing, the function falls
back to the legacy combined pickle so the service keeps working during migration.
"""

import logging
import os
import pickle
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Resolve the values dir robustly. `_ROOT` assumes this module lives three
# levels under the repo root (…/app/utils/concept_values.py → repo). When the
# module is bind-mounted one level shallower (e.g. /app/utils/ instead of
# /app/app/utils/), `_ROOT` collapses to "/" and `/resources/values` does not
# exist — so honour an explicit CONCEPT_VALUES_DIR override and fall back to the
# canonical in-container mount point before giving up.
_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_DIR = os.getenv("CONCEPT_VALUES_DIR")
if _ENV_DIR:
    _PKL_DIR = Path(_ENV_DIR)
else:
    _candidate = _ROOT / "resources" / "values"
    _PKL_DIR = _candidate if _candidate.exists() else Path("/app/resources/values")
_LEGACY = _PKL_DIR / "concept_values_by_db_and_field.pkl"

# {db_name: {field_name: [values]}}
_cache: Dict[str, Dict[str, List[str]]] = {}
_legacy_cache: Optional[Dict] = None

# {db_name: {canonical_field: {normalized_alias: canonical_value}}}
_alias_cache: Dict[str, Dict[str, Dict[str, str]]] = {}


def _load_legacy(db: str) -> Dict[str, List[str]]:
    global _legacy_cache
    if _legacy_cache is None:
        if not _LEGACY.exists():
            logger.warning("Legacy combined pickle not found: %s", _LEGACY)
            return {}
        with open(_LEGACY, "rb") as f:
            _legacy_cache = pickle.load(f)
        logger.info("Loaded legacy combined pickle (%d DBs)", len(_legacy_cache))
    entry = _legacy_cache.get(db) or _legacy_cache.get(db.lower()) or {}
    # normalise values to lists
    return {k: sorted(v) if isinstance(v, set) else list(v) for k, v in entry.items()}


def get_db_concept_values(db: str) -> Dict[str, List[str]]:
    """Return {field_name: [values]} for *db*.  Thread-safe for read-only access."""
    key = db.lower()
    if key in _cache:
        return _cache[key]

    pkl = _PKL_DIR / f"concept_values_{key}.pkl"
    if pkl.exists():
        try:
            with open(pkl, "rb") as f:
                data = pickle.load(f)
            logger.info("Loaded concept values for %s: %d fields", key, len(data))
            _cache[key] = data
            return data
        except Exception as e:
            logger.error("Failed to load %s: %s — trying legacy fallback", pkl, e)

    # Per-DB pkl missing or failed — fall back to legacy combined pickle.
    # Note: the combined pkl is 600 MB+ and may OOM small containers.
    # Prefer per-DB pkls via the '*v-values' docker-compose anchor.
    logger.warning(
        "Per-DB pickle missing for %r — falling back to legacy combined pickle. "
        "Run: python scripts/build_concept_values.py %s",
        key, key,
    )
    data = _load_legacy(key)
    _cache[key] = data
    return data


def validate_concept_values_mount() -> list[str]:
    """Check that the resources/values/ directory is correctly mounted and
    contains per-DB concept-values pkls.  Returns a list of error strings
    (empty = OK).  Called at service startup so a missing mount surfaces as
    an unhealthy container instead of silent 0-row query results.

    Strategy: file-existence checks only — do NOT load the pkls at startup.
    Per-DB pkls can be 100 MB – 2 GB each; loading all of them at boot would
    OOM the container. Existence + size > 0 is sufficient to confirm the
    bind-mount landed correctly.

    Only checks per-DB files (concept_values_<db>.pkl). DBs without a pkl
    (e.g. opentargets, live-API DBs) are skipped — their absence is expected.
    """
    errors: list[str] = []

    if not _PKL_DIR.exists():
        errors.append(
            f"resources/values/ directory not found at {_PKL_DIR}. "
            "Add '*v-values' to this service in docker-compose.yml "
            "then `docker compose up -d --force-recreate <service>`."
        )
        return errors

    per_db_pkls = [
        p for p in sorted(_PKL_DIR.glob("concept_values_*.pkl"))
        if p.name != "concept_values_by_db_and_field.pkl"
        and ".bak" not in p.name
    ]

    if not per_db_pkls:
        errors.append(
            f"No per-DB concept_values_<db>.pkl files in {_PKL_DIR}. "
            "Mount the FULL resources/values/ directory (not individual files) "
            "via the '*v-values' anchor in docker-compose.yml, then recreate."
        )
        return errors

    # Verify each pkl is non-empty (a zero-byte file means a broken mount).
    for pkl in per_db_pkls:
        if pkl.stat().st_size == 0:
            errors.append(f"{pkl.name} is 0 bytes — broken bind-mount or truncated file")

    logger.info(
        "validate_concept_values_mount: %d per-DB pkls found in %s",
        len(per_db_pkls), _PKL_DIR,
    )
    return errors


def get_db_alias_map(db: str) -> Dict[str, Dict[str, str]]:
    """Return {canonical_field: {normalized_alias: canonical_value}} for *db*.

    Built by scripts/build_alias_map.py from the DB's own alias/xref table
    (e.g. STRING `protein_alias_string`). Maps a protein/gene nickname or full
    name to the DB-canonical value (e.g. "p110alpha" → "PIK3CA") so the resolver
    can rescue terms the external KBs don't know. Returns {} when absent
    (degrades safely to KB-only resolution). Cached in-process.
    """
    key = db.lower()
    if key in _alias_cache:
        return _alias_cache[key]
    pkl = _PKL_DIR / f"alias_map_{key}.pkl"
    data: Dict[str, Dict[str, str]] = {}
    if pkl.exists():
        try:
            with open(pkl, "rb") as f:
                data = pickle.load(f)
            logger.info(
                "Loaded alias map for %s: %d fields / %d aliases",
                key, len(data), sum(len(v) for v in data.values()),
            )
        except Exception as e:
            logger.error("Failed to load alias map %s: %s", pkl, e)
            data = {}
    _alias_cache[key] = data
    return data
