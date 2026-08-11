import os
import uuid
import logging

RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")

logger = logging.getLogger("uvicorn.error")





def _safe(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum() or c in ("-_"))



def _csv_path(prefix: str, suffix: str = "") -> str:
    suffix = _safe(suffix) or uuid.uuid4().hex
    return os.path.join(RESULTS_ROOT, f"{prefix}_{suffix}.csv")

