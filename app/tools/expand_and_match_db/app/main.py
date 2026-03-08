

import os
import sys
import logging
import uuid
import time
import asyncio
from typing import Any, List, Optional, Union, Dict

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config.guardrail import (
    ExpandMemberOutput,
    QueryInterpreterOutputGuardrail,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
FUZZY_URL = os.getenv("FUZZY_URL", "http://biochirp_fuzzy_tool:8013/fuzzy")
SEMANTIC_URL = os.getenv("SEMANTIC_URL", "http://biochirp_semantic_tool:8015/semantic")
EXPAND_SYNONYMS_URL = os.getenv(
    "EXPAND_SYNONYMS_URL",
    "http://biochirp_synonyms_expander:8014/expand_synonyms"
)

# Timeouts
SERVICE_TIMEOUT_SEC = float(os.getenv("SERVICE_TIMEOUT_SEC", "60"))
OVERALL_TIMEOUT_SEC = float(os.getenv("OVERALL_TIMEOUT_SEC", "90"))

# Valid databases
VALID_DATABASES = set(os.getenv("VALID_DATABASES", "TTD,CTD,HCDT").split(","))

# HTTP client (reused across requests)
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client."""
    global _http_client
    
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(SERVICE_TIMEOUT_SEC),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
        )
        logger.info("Created shared HTTP client")
    
    return _http_client


# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Expand and Match Database Service",
    version="1.0.0",
    description="API for Expand and Match Database Service"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """Pre-create HTTP client on startup."""
    await get_http_client()
    logger.info("Expand and Match Database service started")


@app.on_event("shutdown")
async def shutdown_event():
    """Close HTTP client on shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("Closed HTTP client")


def union_of_lists(
    *args: Optional[Union[List[Any], str]]
) -> Optional[Union[List[Any], str]]:
    """
    Set-union and normalization across lists/strings/None.
    
    Args:
        *args: Variable number of lists, strings, or None
        
    Returns:
        Combined and normalized list, string, or None
    """
    if not args or all(a is None for a in args):
        return None
    
    has_list = any(isinstance(a, list) for a in args)
    
    if has_list:
        out: List[Any] = []
        for a in args:
            if isinstance(a, list):
                for x in a:
                    normalized = x.lower() if isinstance(x, str) else x
                    if normalized:  # Skip empty strings
                        out.append(normalized)
        return out
    
    strings = [a for a in args if isinstance(a, str)]
    non_none_non_str = [a for a in args if (a is not None and not isinstance(a, str))]
    
    if strings and all(s.lower() == "requested" for s in strings) and not non_none_non_str:
        return "requested"
    
    return [s.lower() for s in strings if s]


async def call_service(
    label: str,
    url: str,
    client: httpx.AsyncClient,
    params: Dict[str, str],
    body: Dict[str, Any],
    request_id: str
) -> Dict[str, Any]:
    """
    Helper to call a downstream service, logs timing + errors.
    
    Args:
        label: Service label for logging
        url: Service URL
        client: HTTP client
        params: Query parameters
        body: Request body
        request_id: Request ID for tracking
        
    Returns:
        Dict with either result value or "__error__" key
    """
    start = time.perf_counter()
    
    try:
        logger.info(f"[{request_id}] [{label.upper()}] POST {url} params={params}")
        
        resp = await client.post(url, params=params, json=body)
        resp.raise_for_status()
        
        data = resp.json()
        
        # Count entries heuristically
        n_entries = 0
        if isinstance(data, dict) and "value" in data and isinstance(data["value"], dict):
            n_entries = sum(
                len(v) if isinstance(v, list) else 1
                for v in data["value"].values()
            )
        
        elapsed = time.perf_counter() - start
        logger.info(
            f"[{request_id}] [{label.upper()}] SUCCESS ({n_entries} entries) "
            f"elapsed={elapsed:.2f}s"
        )
        
        return {
            "value": data.get("value", {}),
            "__elapsed__": elapsed
        }
        
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start
        error_msg = f"Timeout after {SERVICE_TIMEOUT_SEC}s"
        logger.error(
            f"[{request_id}] [{label.upper()}] TIMEOUT after {elapsed:.2f}s"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }
        
    except httpx.HTTPStatusError as e:
        elapsed = time.perf_counter() - start
        error_msg = f"HTTP {e.response.status_code}"
        logger.error(
            f"[{request_id}] [{label.upper()}] FAILED with {error_msg} "
            f"after {elapsed:.2f}s"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }
        
    except Exception as e:
        elapsed = time.perf_counter() - start
        error_msg = repr(e)
        logger.exception(
            f"[{request_id}] [{label.upper()}] FAILED after {elapsed:.2f}s: {e}"
        )
        return {
            "__error__": error_msg,
            "__elapsed__": elapsed
        }


@app.get("/")
def root():
    """Root endpoint."""
    return {"message": "Expand and Match Database service tool is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "OK"}


@app.post("/expand_and_match_db", response_model=ExpandMemberOutput)
async def expand_and_match_db(
    input: QueryInterpreterOutputGuardrail,
    database: str = Query(..., description="Which DB to use (ttd, ctd, hcdt)")
):
    """
    Expand and match database endpoint.
    
    Calls three services in parallel:
      - Fuzzy matching
      - Semantic similarity
      - Synonym expansion
    
    Combines results and returns unified output.
    
    Args:
        input: Query interpreter output with parsed values
        database: Target database name (case insensitive)
        
    Returns:
        ExpandMemberOutput with combined results from all services
    """
    tool = "expand_and_match_db"
    request_id = str(uuid.uuid4())
    overall_start = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] [START] database={database}")
    
    # Input validation
    if not input:
        raise HTTPException(status_code=400, detail="Input is required")
    
    # Validate database
    database_upper = database.strip().upper()
    if database_upper not in VALID_DATABASES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid database '{database}'. Valid options: {', '.join(sorted(VALID_DATABASES))}"
        )
    
    # Convert input to dict
    try:
        input_filtered = input.model_dump(exclude_none=True)
    except Exception as e:
        logger.error(f"[{tool}][{request_id}] Failed to dump input: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid input: {str(e)}")
    
    if not input_filtered.get("parsed_value"):
        raise HTTPException(status_code=400, detail="parsed_value is required")
    
    logger.info(f"[{tool}][{request_id}] [INPUT] {input_filtered}")
    
    # FIX: Normalize database to lowercase for services
    database_lower = database_upper.lower()
    params = {"database": database_lower}
    
    logger.info(
        f"[{tool}][{request_id}] Calling services with database='{database_lower}' "
        f"(original: '{database}')"
    )
    
    # Get HTTP client
    client = await get_http_client()
    
    # Prepare service calls
    tasks = {
        "fuzzy": call_service(
            "fuzzy", FUZZY_URL, client, params, input_filtered, request_id
        ),
        "semantic": call_service(
            "semantic", SEMANTIC_URL, client, params, input_filtered, request_id
        ),
        "expand_synonyms": call_service(
            "expand_synonyms", EXPAND_SYNONYMS_URL, client, params, input_filtered, request_id
        ),
    }
    
    # FIX: Add overall timeout
    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=OVERALL_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        overall_elapsed = time.perf_counter() - overall_start
        error_msg = f"Overall timeout ({OVERALL_TIMEOUT_SEC}s) exceeded"
        logger.error(
            f"[{tool}][{request_id}] [TIMEOUT] {error_msg} after {overall_elapsed:.2f}s"
        )
        
        return ExpandMemberOutput(
            database=database_upper,  # Return original case
            value={},
            tool=tool,
            message=error_msg,
            errors={"overall": error_msg}
        )
    
    # Handle any exceptions from gather
    processed_results = []
    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            service_name = list(tasks.keys())[i]
            logger.error(
                f"[{tool}][{request_id}] [{service_name.upper()}] Exception: {result}"
            )
            processed_results.append({
                "__error__": repr(result),
                "__elapsed__": 0
            })
        else:
            processed_results.append(result)
    
    service_outputs = dict(zip(tasks.keys(), processed_results))
    
    # Log elapsed for each service
    for name, result in service_outputs.items():
        elapsed = result.get("__elapsed__")
        if elapsed is not None:
            logger.debug(f"[{request_id}] [{name.upper()}] elapsed={elapsed:.2f}s")
    
    # Build counts + errors
    counts = {}
    error_log: Dict[str, str] = {}
    
    for name, result in service_outputs.items():
        count = 0
        if isinstance(result, dict) and "value" in result:
            count = sum(
                len(v) if isinstance(v, list) else 1
                for v in result["value"].values()
            )
        counts[name] = count
        
        if isinstance(result, dict) and "__error__" in result:
            error_log[name] = result["__error__"]
        
        logger.info(f"[{tool}][{request_id}] [{name.upper()}] returned {count} entries")
    
    if error_log:
        logger.warning(f"[{tool}][{request_id}] [PARTIAL ERRORS] {error_log}")
    
    # Combine results
    combined_member: Dict[str, Any] = {}
    parsed_value = input_filtered.get("parsed_value", {})
    synonym_only_fields = {"gene_name", "drug_name"}
    
    for key in parsed_value.keys():

        if key in synonym_only_fields:
            # fuzzy_val = (service_outputs.get("fuzzy") or {}).get("value", {}).get(key)
            synonyms_val = (service_outputs.get("expand_synonyms") or {}).get("value", {}).get(key)
            # similarity_val = (service_outputs.get("semantic") or {}).get("value", {}).get(key)
        
            combined = synonyms_val
        else:
            fuzzy_val = (service_outputs.get("fuzzy") or {}).get("value", {}).get(key)
            synonyms_val = (service_outputs.get("expand_synonyms") or {}).get("value", {}).get(key)
            similarity_val = (service_outputs.get("semantic") or {}).get("value", {}).get(key)
        
            combined = union_of_lists(fuzzy_val, synonyms_val, similarity_val)
        
        if isinstance(combined, list):
            # Deduplicate and sort
            combined_member[key] = sorted(
                set(item.lower() for item in combined if isinstance(item, str) and item)
            )
        else:
            combined_member[key] = combined
    
    overall_elapsed = time.perf_counter() - overall_start
    
    logger.info(
        f"[{tool}][{request_id}] [RESULT] combined_keys={len(combined_member)} | "
        f"fuzzy={counts.get('fuzzy', 0)} | semantic={counts.get('semantic', 0)} | "
        f"expand_synonyms={counts.get('expand_synonyms', 0)} | elapsed={overall_elapsed:.2f}s"
    )
    
    # Determine response
    if len(error_log) == len(service_outputs):
        # All services failed
        msg = f"All expand/match services failed: {error_log}"
        logger.error(f"[{tool}][{request_id}] [FAILURE] {msg}")
        
        result = ExpandMemberOutput(
            database=database_upper,  # Return original case
            value={},
            tool=tool,
            message=msg,
            errors=error_log
        )
    else:
        # At least one service succeeded
        msg = None
        if error_log:
            msg = f"Partial error(s) encountered: {error_log}"
        
        result = ExpandMemberOutput(
            database=database_upper,  # Return original case
            value=combined_member,
            tool=tool,
            message=msg,
            errors=error_log if error_log else None
        )
    
    return result
