from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import os
import uuid
import requests
import logging

RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")

logger = logging.getLogger("uvicorn.error")




def normalize_dict_values(data: dict) -> dict:
    """
    Iterates through all keys in the input dict.
    For each key:
      - If the value is a list of length 1:
        - If the value is ["None"], [None], or ["None"], set value to None.
        - If the value is ["requested"], set value to "requested".
    Returns a new dictionary with normalized values.
    """

    data = data.model_dump().get("parsed_value")
    result = {}
    for key, value in data.items():
        if isinstance(value, list) and len(value) == 1:
            item = value[0]
            if item is None or item == "None":
                result[key] = None
            elif item == "requested":
                result[key] = "requested"
            else:
                result[key] = value
        else:
            result[key] = value
    return result



def _safe(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum() or c in ("-_"))

def _csv_path(prefix: str, suffix: str = "") -> str:
    suffix = _safe(suffix) or uuid.uuid4().hex
    return os.path.join(RESULTS_ROOT, f"{prefix}_{suffix}.csv")

