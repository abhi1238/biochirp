
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging
import os
import sys
import uuid
import time
import asyncio
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input
from .filter import run_llm_member_selection_filter
from fastapi.middleware.cors import CORSMiddleware
from fastapi_mcp import FastApiMCP

# Configure logging
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

# Environment configuration
USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
LLM_FILTER_TIMEOUT_SEC = float(os.getenv("LLM_FILTER_TIMEOUT_SEC", "90"))
LLM_FILTER_MODEL_NAME = os.getenv("LLM_FILTER_MODEL_NAME", "gpt-4.1-nano")  # FIX: Correct env var
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
MAX_LIST_LENGTH = int(os.getenv("MAX_LIST_LENGTH", "10000"))

app = FastAPI(
    title="BioChirp LLM Filter Service",
    version="1.0.0",
    description="API for LLM Member Selection Filter Service"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """Root endpoint for service health check."""
    return {"message": "LLM filter service tool is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "OK"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to catch any unhandled exceptions."""
    logger.exception(f"[GlobalExceptionHandler] Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"value": []}
    )


@app.post("/llm_member_selection_filter", response_model=Llm_Member_Selector_Output)
async def llm_member_selection_filter(input: Llm_Member_Selector_Input):
    """
    LLM-based member selection and filtering endpoint.
    
    Args:
        input: Llm_Member_Selector_Input containing category, term, and string list
        
    Returns:
        Llm_Member_Selector_Output: Filtered list of matching members
    """
    tool = "llm_member_selection_filter"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Don't log entire input if string_list is large
    list_size = len(input.string_list) if input.string_list else 0
    logger.info(
        f"[{tool} API][{request_id}] Started. "
        f"Category: '{input.category}', Term: '{input.single_term}', "
        f"List size: {list_size}"
    )
    
    # Input validation
    if not input.string_list:
        logger.warning(f"[{tool} API][{request_id}] Empty string_list provided")
        return Llm_Member_Selector_Output(value=[])
    
    if not isinstance(input.string_list, list):
        logger.warning(f"[{tool} API][{request_id}] string_list is not a list")
        return Llm_Member_Selector_Output(value=[])
    
    if len(input.string_list) > MAX_LIST_LENGTH:
        logger.warning(
            f"[{tool} API][{request_id}] string_list too large: "
            f"{len(input.string_list)} > {MAX_LIST_LENGTH}"
        )
        return Llm_Member_Selector_Output(value=[])

    try:
        # FIX: Use service-specific timeout
        payload = await asyncio.wait_for(
            run_llm_member_selection_filter(input),
            timeout=LLM_FILTER_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        result_count = len(payload.value) if payload and payload.value else 0
        logger.info(
            f"[{tool} API][{request_id}] Success. "
            f"Found {result_count} matches. Duration: {elapsed:.2f}s"
        )
        return payload

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {LLM_FILTER_TIMEOUT_SEC}s"
        logger.error(
            f"[{tool} API][{request_id}] [TIMEOUT] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Network error: {ce}"
        logger.error(
            f"[{tool} API][{request_id}] [NETWORK ERROR] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except ValueError as ve:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Validation error: {ve}"
        logger.error(
            f"[{tool} API][{request_id}] [VALIDATION ERROR] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Internal error: {e}"
        logger.exception(
            f"[{tool} API][{request_id}] [EXCEPTION] {error_msg}. Duration: {elapsed:.2f}s"
        )

    # Always return schema-valid error object with empty list
    logger.info(f"[{tool} API][{request_id}] Finished with error, returning empty result")
    return Llm_Member_Selector_Output(value=[])


# # Create an MCP server attached to this FastAPI app
# mcp = FastApiMCP(
#     app,
#     name="BioChirp Member Filter",
#     description="LLM-powered member selection & filtering for communities"
#     )

# # Mount the MCP server into FastAPI
# mcp.mount_http()
