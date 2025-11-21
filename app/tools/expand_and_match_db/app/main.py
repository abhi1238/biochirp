from fastapi import FastAPI, Query
from typing import Any, List, Optional, Union
import logging
import uuid
import os
import time
import asyncio
import httpx

from config.guardrail import (
    ExpandMemberOutput,
    QueryInterpreterOutputGuardrail,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("expand_and_match_db")

app = FastAPI(
    title="BioChirp Expand and Match Database Service",
    version="1.0.0",
    description="API for Expand and Match Database Service"
)

app.add_middleware(
    # CORS config as before
    CORSMiddleware := __import__("fastapi.middleware.cors", fromlist=["CORSMiddleware"]).CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def union_of_lists(*args: Optional[Union[List[Any], str]]) -> Optional[Union[List[Any], str]]:
    """
    Set-union and normalization across lists/strings/None.
    """
    if not args or all(a is None for a in args):
        return None
    has_list = any(isinstance(a, list) for a in args)
    if has_list:
        out: List[Any] = []
        for a in args:
            if isinstance(a, list):
                for x in a:
                    out.append(x.lower() if isinstance(x, str) else x)
        return out
    strings = [a for a in args if isinstance(a, str)]
    non_none_non_str = [a for a in args if (a is not None and not isinstance(a, str))]
    if strings and all(s.lower() == "requested" for s in strings) and not non_none_non_str:
        return "requested"
    return [s.lower() for s in strings]

async def call_service(
    label: str,
    url: str,
    client: httpx.AsyncClient,
    params: dict,
    body: dict,
    request_id: str
) -> dict:
    """
    Helper to call a downstream service, logs timing + errors.
    Returns a dict that either contains result value or an "__error__" key.
    """
    start = time.perf_counter()
    try:
        logger.info(f"[{request_id}] [{label.upper()}] POST {url} params={params}")
        resp = await client.post(url, params=params, json=body, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        # Count entries heuristically
        n_entries = 0
        if isinstance(data, dict) and "value" in data and isinstance(data["value"], dict):
            # sum lengths of lists under each key
            n_entries = sum(len(v) if isinstance(v, list) else 1 for v in data["value"].values())
        elapsed = time.perf_counter() - start
        logger.info(f"[{request_id}] [{label.upper()}] SUCCESS ({n_entries} entries) elapsed={elapsed:.2f}s")
        return {"value": data.get("value", {}), "__elapsed__": elapsed}
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.error(f"[{request_id}] [{label.upper()}] FAILED after {elapsed:.2f}s: {repr(e)}")
        return {"__error__": repr(e), "__elapsed__": elapsed}

@app.get("/")
def root():
    return {"message": "Expand and Match Database service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/expand_and_match_db", response_model=ExpandMemberOutput)
async def expand_and_match_db(
    input: QueryInterpreterOutputGuardrail,
    database: str = Query(..., description="Which DB to use (ttd, ctd, hcdt)")
):
    tool = "expand_and_match_db"
    request_id = str(uuid.uuid4())
    overall_start = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] [START] database={database}")
    input_filtered = input.model_dump(exclude_none=True)
    logger.info(f"[{tool}][{request_id}] [INPUT] {input_filtered}")

    params = {"database": database}
    fuzzy_url = f"http://biochirp_fuzzy_tool:8013/fuzzy"
    semantic_url = f"http://biochirp_semantic_tool:8015/semantic"
    expand_url = f"http://biochirp_synonyms_expander:8014/expand_synonyms"

    async with httpx.AsyncClient() as client:
        tasks = {
            "fuzzy": call_service("fuzzy", fuzzy_url, client, params, input_filtered, request_id),
            "semantic": call_service("semantic", semantic_url, client, params, input_filtered, request_id),
            "expand_synonyms": call_service("expand_synonyms", expand_url, client, params, input_filtered, request_id),
        }
        raw_results = await asyncio.gather(*tasks.values())

    service_outputs = dict(zip(tasks.keys(), raw_results))

    # Log elapsed for each service
    for name, result in service_outputs.items():
        elapsed = result.get("__elapsed__")
        if elapsed is not None:
            logger.debug(f"[{request_id}] [{name.upper()}] elapsed={elapsed:.2f}s")

    # Build counts + errors
    counts = {}
    error_log: dict = {}
    for name, result in service_outputs.items():
        count = 0
        if isinstance(result, dict) and "value" in result:
            count = sum(len(v) if isinstance(v, list) else 1 for v in result["value"].values())
        counts[name] = count
        if isinstance(result, dict) and "__error__" in result:
            error_log[name] = result["__error__"]
        logger.info(f"[{tool}][{request_id}] [{name.upper()}] returned {count} entries")

    if error_log:
        logger.warning(f"[{tool}][{request_id}] [PARTIAL ERRORS] {error_log}")

    # Combine results
    combined_member: dict = {}
    parsed_value = input_filtered.get("parsed_value", {})
    for key in parsed_value.keys():
        fuzzy_val = (service_outputs.get("fuzzy") or {}).get("value", {}).get(key)
        synonyms_val = (service_outputs.get("expand_synonyms") or {}).get("value", {}).get(key)
        similarity_val = (service_outputs.get("semantic") or {}).get("value", {}).get(key)
        combined = union_of_lists(fuzzy_val, synonyms_val, similarity_val)
        if isinstance(combined, list):
            combined_member[key] = sorted(set(item.lower() for item in combined if isinstance(item, str)))
        else:
            combined_member[key] = combined

    overall_elapsed = time.perf_counter() - overall_start
    logger.info(
        f"[{tool}][{request_id}] [RESULT] combined_keys={len(combined_member)} | "
        f"fuzzy={counts.get('fuzzy',0)} | semantic={counts.get('semantic',0)} | "
        f"expand_synonyms={counts.get('expand_synonyms',0)} | elapsed={overall_elapsed:.2f}s"
    )

    if len(error_log) == len(service_outputs):
        msg = f"All expand/match services failed: {error_log}"
        logger.error(f"[{tool}][{request_id}] [FAILURE] {msg}")
        result = ExpandMemberOutput(
            database=database,
            value={},
            tool=tool,
            message=msg,
            errors=error_log
        )
    else:
        msg = None
        if error_log:
            msg = f"Partial error(s) encountered: {error_log}"
        result = ExpandMemberOutput(
            database=database,
            value=combined_member,
            tool=tool,
            message=msg,
            errors=error_log if error_log else None
        )

    return result
