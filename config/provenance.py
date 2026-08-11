"""Read SOURCE.md for a database and return (version, snapshot_date)."""
from __future__ import annotations
import re
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=64)
def get_db_provenance(db_name: str) -> tuple[str | None, str | None]:
    """Return (db_version, db_snapshot_date) for a database.

    Source order:
    1. database/<db>/SOURCE.md fields **Version:** and **Snapshot date:**.
    2. If SOURCE.md is missing or a field can't be parsed, fall back to the
       most recent mtime among that DB's parquet files. This prevents
       responses from advertising a stale version when the parquet was
       refreshed but SOURCE.md wasn't updated.

    Returns (None, None) only when neither source yields a value.
    """
    import datetime as _dt

    source = _REPO_ROOT / "database" / db_name / "SOURCE.md"
    version: str | None = None
    date: str | None = None
    if source.exists():
        text = source.read_text()
        version = _extract(text, r"\*\*Version:\*\*\s*(.+)")
        date = _extract(text, r"\*\*Snapshot date:\*\*\s*(\d{4}-\d{2}-\d{2})")

    if version is None or date is None:
        db_dir = _REPO_ROOT / "database" / db_name
        if db_dir.exists():
            parquets = list(db_dir.glob("*.parquet"))
            if parquets:
                latest = max(p.stat().st_mtime for p in parquets)
                mtime_date = _dt.datetime.fromtimestamp(latest).strftime("%Y-%m-%d")
                if version is None:
                    version = f"parquet mtime {mtime_date}"
                if date is None:
                    date = mtime_date
    return version, date


def _extract(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None
