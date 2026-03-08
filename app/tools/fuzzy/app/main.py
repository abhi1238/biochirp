

import logging
import os
import sys
import uuid
import time
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config.guardrail import (
    FuzzyFilteredOutputs,
    QueryInterpreterOutputGuardrail,
    OutputFields
)
from .fuzzy_search_db_wise import compute_fuzzy_filtered_outputs

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

logger = logging.getLogger(__name__)

# Environment configuration
FUZZY_TIMEOUT_SEC = float(os.getenv("FUZZY_TIMEOUT_SEC", "90"))
VALID_DATABASES = set(os.getenv("VALID_DATABASES", "TTD,CTD,HCDT").split(","))

# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Fuzzy Service",
    version="1.0.0",
    description="API for Fuzzy Search Service"
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
    return {"message": "Fuzzy service tool is running"}


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
            "database": "unknown",
            "value": {},
            "tool": "fuzzy"
        }
    )


@app.post("/fuzzy", response_model=FuzzyFilteredOutputs)
async def fuzzy(
    input: QueryInterpreterOutputGuardrail,
    database: str = None
):
    """
    Fuzzy matching endpoint for database field values.
    
    Accepts database names in any case (e.g., 'hcdt', 'HCDT', 'Hcdt' all work).
    
    Args:
        input: Query interpreter output with parsed values
        database: Target database name (TTD, CTD, or HCDT - case insensitive)
        
    Returns:
        FuzzyFilteredOutputs: Matched database values
    """
    tool = "fuzzy"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Normalize database to uppercase FIRST
    if database:
        database_original = database
        database = database.strip().upper()
        if database != database_original:
            logger.debug(
                f"[{tool} API][{request_id}] Normalized database: "
                f"'{database_original}' → '{database}'"
            )
    
    logger.info(
        f"[{tool} API][{request_id}] Started. Database: {database}"
    )
    
    # Input validation
    if not input:
        logger.warning(f"[{tool} API][{request_id}] No input provided")
        raise HTTPException(status_code=400, detail="Input is required")
    
    if not database:
        logger.warning(f"[{tool} API][{request_id}] No database specified")
        raise HTTPException(status_code=400, detail="Database parameter is required")
    
    # FIX: Now 'HCDT' will match correctly
    if database not in VALID_DATABASES:
        logger.warning(
            f"[{tool} API][{request_id}] Invalid database: '{database}'. "
            f"Valid options: {sorted(VALID_DATABASES)}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid database '{database}'. Valid options: {', '.join(sorted(VALID_DATABASES))}"
        )
    
    # Validate parsed_value exists
    if not hasattr(input, 'parsed_value') or not input.parsed_value:
        logger.warning(f"[{tool} API][{request_id}] No parsed_value in input")
        raise HTTPException(status_code=400, detail="parsed_value is required")

    try:
        # Convert to dict
        try:
            parsed_value = input.parsed_value.model_dump(exclude_none=True)
        except Exception as e:
            logger.error(
                f"[{tool} API][{request_id}] Failed to dump parsed_value: {e}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parsed_value: {str(e)}"
            )
        
        if not parsed_value:
            logger.warning(f"[{tool} API][{request_id}] Empty parsed_value")
            return FuzzyFilteredOutputs(
                database=database,
                value=OutputFields(),
                tool=tool
            )
        
        # Add timeout
        payload = await asyncio.wait_for(
            compute_fuzzy_filtered_outputs(parsed=parsed_value, database=database),
            timeout=FUZZY_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        logger.info(
            f"[{tool} API][{request_id}] Success. Duration: {elapsed:.2f}s"
        )
        return payload

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {FUZZY_TIMEOUT_SEC}s"
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

    # Safe error fallback
    logger.info(f"[{tool} API][{request_id}] Finished with error, returning empty result")
    
    try:
        error_value = input.parsed_value.model_dump(exclude_none=True)
    except Exception:
        logger.warning(f"[{tool} API][{request_id}] Cannot dump parsed_value in error handler")
        error_value = OutputFields()
    
    return FuzzyFilteredOutputs(
        database=database,
        value=error_value,
        tool=tool
    )