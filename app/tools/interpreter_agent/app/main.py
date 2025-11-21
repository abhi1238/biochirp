import logging
import uuid
import time
import asyncio
import os
from fastapi import FastAPI, HTTPException
from config.guardrail import QueryInterpreterOutputGuardrail, InterpreterInput, ParsedValue
from app.interpreter import run_interpreter
from fastapi.middleware.cors import CORSMiddleware
import sys

# --- LOGGING SETUP ---
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")

# Environment variables
USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "False").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))
MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-4o-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))

app = FastAPI(
    title="BioChirp Interpreter Service",
    version="1.0.0",
    description="API for Interpreter Service",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def normalize_dict_values(data: dict) -> dict:
    """
    Normalize parsed_value dict: For list values of length 1, convert ["None"]/None/["requested"] appropriately.
    Returns a new dict with normalized values.
    """
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

        # Second pass: check the "only one requested" rule
    non_null_keys = [k for k, v in result.items() if v is not None]
    requested_keys = [k for k, v in result.items() if v == "requested"]

    if (
        len(non_null_keys) == 1
        and len(requested_keys) == 1
        and non_null_keys[0] == requested_keys[0]
    ):
        # Only one key is "requested" and everything else is None → zero out
        for k in result.keys():
            result[k] = None

    return result

@app.get("/")
def root():
    return {"message": "Interpreter service is running."}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/interpreter", response_model=QueryInterpreterOutputGuardrail)
async def interpreter_endpoint(input: InterpreterInput):
    service_name = "interpreter"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    logger.info(f"[{service_name} API][{request_id}] Started. Query: '{input.query}'")

    # Early exit for empty query
    if not input.query.strip():
        msg = "Empty query received. Treating as invalid."
        logger.warning(f"[{service_name} API][{request_id}] {msg}")
        return QueryInterpreterOutputGuardrail(
            cleaned_query="",
            status="invalid",
            route="web",
            message=msg,
            parsed_value=ParsedValue(),
            tool=service_name
        )

    try:
        # Run interpreter with timeout
        payload = await asyncio.wait_for(run_interpreter(input), timeout=AGENT_TIMEOUT_SEC)
        
        # Normalize parsed_value if it's a dict
        if isinstance(payload.parsed_value, ParsedValue):
            normalized = normalize_dict_values(payload.parsed_value.model_dump())
            payload.parsed_value = ParsedValue(**normalized)
        
        # Validate and return
        output = QueryInterpreterOutputGuardrail(**payload.model_dump())
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{service_name} API][{request_id}] Success. Duration: {elapsed:.2f}s")
        return output

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        msg = f"Timeout after {AGENT_TIMEOUT_SEC}s during interpreter run."
        logger.error(f"[{service_name} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except HTTPException as he:
        elapsed = time.perf_counter() - start_time
        msg = f"HTTP error: {he.detail}"
        logger.error(f"[{service_name} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {ce}"
        logger.error(f"[{service_name} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except ValueError as ve:
        elapsed = time.perf_counter() - start_time
        msg = f"Validation error: {ve}"
        logger.error(f"[{service_name} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.exception(f"[{service_name} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")

    # Always return schema-valid fallback on error
    fallback = QueryInterpreterOutputGuardrail(
        cleaned_query=input.query,
        status="invalid",
        route="web",
        message=msg,
        parsed_value=ParsedValue(),
        tool=service_name
    )
    logger.info(f"[{service_name} API][{request_id}] Finished with fallback")
    return fallback