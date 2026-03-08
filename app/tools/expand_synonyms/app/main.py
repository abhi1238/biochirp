import os
import sys
import logging
import asyncio
import uuid
import time
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config.guardrail import (
    ExpandSynonymsOutput,
    QueryInterpreterOutputGuardrail,
    ParsedValue
)
from .synonym_expander import synonyms_expander

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
SYNONYMS_TIMEOUT_SEC = float(os.getenv("SYNONYMS_TIMEOUT_SEC", "90"))
VALID_DATABASES = set(os.getenv("VALID_DATABASES", "TTD,CTD,HCDT").split(","))

# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Expand Synonyms Service",
    version="1.0.0",
    description="API for Synonym Expansion Service"
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
    return {"message": "Expand Synonyms service tool is running"}


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
            "tool": "expand_synonyms"
        }
    )


@app.post("/expand_synonyms", response_model=ExpandSynonymsOutput)
async def expand_synonyms(
    input: QueryInterpreterOutputGuardrail,
    database: Optional[str] = None
):
    """
    Synonym expansion endpoint.
    
    Expands biomedical terms with their aliases and synonyms.
    Accepts database names in any case (e.g., 'hcdt', 'HCDT', 'Hcdt' all work).
    
    Args:
        input: Query interpreter output with parsed values
        database: Target database name (TTD, CTD, or HCDT - case insensitive, optional)
        
    Returns:
        ExpandSynonymsOutput: Expanded synonyms for each field
    """
    tool = "expand_synonyms"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Normalize database to uppercase (optional parameter)
    if database:
        database_original = database
        database = database.strip().upper()
        if database != database_original:
            logger.debug(
                f"[{tool}][{request_id}] Normalized database: "
                f"'{database_original}' → '{database}'"
            )
    
    logger.info(
        f"[{tool}][{request_id}] Started. Database: {database}"
    )
    
    # Input validation
    if not input:
        logger.warning(f"[{tool}][{request_id}] No input provided")
        raise HTTPException(status_code=400, detail="Input is required")
    
    # Validate database if provided
    if database and database not in VALID_DATABASES:
        logger.warning(
            f"[{tool}][{request_id}] Invalid database: '{database}'. "
            f"Valid options: {sorted(VALID_DATABASES)}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid database '{database}'. Valid options: {', '.join(sorted(VALID_DATABASES))}"
        )
    
    # Validate parsed_value exists
    if not hasattr(input, 'parsed_value') or not input.parsed_value:
        logger.warning(f"[{tool}][{request_id}] No parsed_value in input")
        raise HTTPException(status_code=400, detail="parsed_value is required")

    try:
        # FIX: Safely extract parsed_value
        try:
            parsed_value = input.parsed_value.model_dump(exclude_none=True)
        except Exception as e:
            logger.error(
                f"[{tool}][{request_id}] Failed to dump parsed_value: {e}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parsed_value: {str(e)}"
            )
        
        if not parsed_value:
            logger.warning(f"[{tool}][{request_id}] Empty parsed_value")
            return ExpandSynonymsOutput(
                database=database,
                value={},
                tool=tool
            )
        
        logger.info(
            f"[{tool}][{request_id}] Processing {len(parsed_value)} fields"
        )
        
        # FIX: Add timeout
        payload = await asyncio.wait_for(
            synonyms_expander(data=parsed_value, database=database),
            timeout=SYNONYMS_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        
        # Count expanded terms
        total_expanded = 0
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    total_expanded += len(value)
        
        logger.info(
            f"[{tool}][{request_id}] Success. "
            f"Expanded to {total_expanded} total terms. Duration: {elapsed:.2f}s"
        )
        
        return ExpandSynonymsOutput(
            database=database,
            value=payload,
            tool=tool
        )

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {SYNONYMS_TIMEOUT_SEC}s"
        logger.error(
            f"[{tool}][{request_id}] [TIMEOUT] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Network error: {ce}"
        logger.error(
            f"[{tool}][{request_id}] [NETWORK ERROR] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except ValueError as ve:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Validation error: {ve}"
        logger.error(
            f"[{tool}][{request_id}] [VALIDATION ERROR] {error_msg}. Duration: {elapsed:.2f}s"
        )
        
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Internal error: {e}"
        logger.exception(
            f"[{tool}][{request_id}] [EXCEPTION] {error_msg}. Duration: {elapsed:.2f}s"
        )

    # FIX: Return empty dict on error, not input data
    logger.info(
        f"[{tool}][{request_id}] Finished with error, returning empty result"
    )
    
    # Return empty result to indicate expansion failed
    return ExpandSynonymsOutput(
        database=database,
        value={},  # Empty dict, not original input
        tool=tool
    )
