
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from config.guardrail import TavilyInput, TavilyOutput
from app.tavily import run_tavily
import logging
import os
import sys
import uuid
import time
import asyncio
from utils.service_setup import add_open_cors, add_health_endpoint

from utils.logging_setup import setup_logging
setup_logging(stream=sys.stdout)

logger = logging.getLogger("uvicorn.error")

# Environment configuration
TAVILY_TIMEOUT_SEC = float(os.getenv("TAVILY_TIMEOUT_SEC", "60"))
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "5000"))

app = FastAPI(
    title="BioChirp Tavily Service",
    version="1.0.0",
    description="API for Tavily Service"
)

add_open_cors(app)
@app.get("/")
def root():
    """Root endpoint for service health check."""
    return {"message": "Tavily service tool is running"}


add_health_endpoint(app)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to catch any unhandled exceptions."""
    logger.exception(f"[GlobalExceptionHandler] Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "message": f"Internal server error: {str(exc)}",
            "tool": "tavily"
        }
    )


@app.post("/tavily", response_model=TavilyOutput)
async def tavily(input: TavilyInput):
    """
    Tavily search endpoint.
    
    Args:
        input: TavilyInput containing the search query
        
    Returns:
        TavilyOutput: Search results or error information
    """
    tool = "tavily"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Initialize error message variable
    error_msg = "Unknown error occurred"
    
    # FIX: Add input validation
    if not input or not input.query:
        logger.warning(f"[{tool} API][{request_id}] Empty or None query")
        return TavilyOutput(
            message="Error: Empty query provided",
            tool=tool
        )
    
    query = input.query.strip()
    if not query:
        logger.warning(f"[{tool} API][{request_id}] Query is whitespace only")
        return TavilyOutput(
            message="Error: Query contains only whitespace",
            tool=tool
        )
    
    if len(query) > MAX_QUERY_LENGTH:
        logger.warning(f"[{tool} API][{request_id}] Query too long: {len(query)} chars")
        return TavilyOutput(
            message=f"Error: Query exceeds maximum length ({len(query)} > {MAX_QUERY_LENGTH})",
            tool=tool
        )
    
    # FIX: Truncate query in logs for security/readability
    query_preview = query[:100] + "..." if len(query) > 100 else query
    logger.info(f"[{tool} API][{request_id}] Started. Query: '{query_preview}'")

    try:
        # FIX: Use environment variable for timeout
        result = await asyncio.wait_for(
            run_tavily(input), 
            timeout=TAVILY_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        
        # FIX: Check if result is already TavilyOutput
        if isinstance(result, TavilyOutput):
            logger.info(f"[{tool} API][{request_id}] Success. Duration: {elapsed:.2f}s")
            return result
        elif isinstance(result, str):
            logger.info(f"[{tool} API][{request_id}] Success (string result). Duration: {elapsed:.2f}s")
            return TavilyOutput(message=result, tool=tool)
        else:
            logger.warning(f"[{tool} API][{request_id}] Unexpected result type: {type(result)}")
            return TavilyOutput(
                message=f"Error: Unexpected result type {type(result).__name__}",
                tool=tool
            )

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {TAVILY_TIMEOUT_SEC}s"
        logger.error(f"[{tool} API][{request_id}] [TIMEOUT] {error_msg}. Duration: {elapsed:.2f}s")
        
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] [NETWORK ERROR] {error_msg}. Duration: {elapsed:.2f}s")
        
    except ValueError as ve:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Validation error: {ve}"
        logger.error(f"[{tool} API][{request_id}] [VALIDATION ERROR] {error_msg}. Duration: {elapsed:.2f}s")
        
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Internal error: {e}"
        logger.exception(f"[{tool} API][{request_id}] [EXCEPTION] {error_msg}. Duration: {elapsed:.2f}s")

    # Always return schema-valid error object
    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return TavilyOutput(message=error_msg, tool=tool)  # Now guaranteed to exist