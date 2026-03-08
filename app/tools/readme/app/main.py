
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging
import os
import sys
import uuid
import time
import asyncio
from app.readme import run_readme
from config.guardrail import ReadmeInput, ReadmeOutput
from fastapi.middleware.cors import CORSMiddleware

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
USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "False").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "60"))
README_TIMEOUT_SEC = float(os.getenv("README_TIMEOUT_SEC", "60"))
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "5000"))

app = FastAPI(
    title="BioChirp Readme Service",
    version="1.0.0",
    description="API for Readme Service"
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
    return {"message": "Readme service is running"}


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
        content={
            "answer": "An error occurred while processing your request.",
            "tool": "readme",
            "message": f"Internal server error: {str(exc)}"
        }
    )


@app.post("/readme", response_model=ReadmeOutput)
async def readme_endpoint(input: ReadmeInput):
    """
    README retrieval endpoint.
    
    Args:
        input: ReadmeInput containing the query
        
    Returns:
        ReadmeOutput: README content or error information
    """
    tool = "readme"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Initialize error message variable
    error_msg = "Unknown error occurred"
    
    # FIX: Add input validation
    if not input or not input.query:
        logger.warning(f"[{tool} API][{request_id}] Empty or None query")
        return ReadmeOutput(
            answer="Please provide a valid query.",
            tool=tool,
            message="Error: Empty query provided"
        )
    
    query = input.query.strip()
    if not query:
        logger.warning(f"[{tool} API][{request_id}] Query is whitespace only")
        return ReadmeOutput(
            answer="Please provide a valid query.",
            tool=tool,
            message="Error: Query contains only whitespace"
        )
    
    if len(query) > MAX_QUERY_LENGTH:
        logger.warning(f"[{tool} API][{request_id}] Query too long: {len(query)} chars")
        return ReadmeOutput(
            answer="Query is too long. Please shorten your question.",
            tool=tool,
            message=f"Error: Query exceeds maximum length ({len(query)} > {MAX_QUERY_LENGTH})"
        )
    
    # Log query (truncated for security)
    query_preview = query[:100] + "..." if len(query) > 100 else query
    logger.info(f"[{tool} API][{request_id}] Started. Query: '{query_preview}'")

    try:
        # FIX: Use dedicated timeout variable
        payload = await asyncio.wait_for(
            run_readme(input), 
            timeout=README_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Duration: {elapsed:.2f}s")
        return payload

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {README_TIMEOUT_SEC}s"
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

    # Always return valid ReadmeOutput on error
    error_obj = ReadmeOutput(
        answer="An error occurred while retrieving the README. Please try again later.",
        tool=tool,
        message=error_msg  # Now guaranteed to exist
    )
    logger.info(f"[{tool} API][{request_id}] Finished with fallback")
    return error_obj