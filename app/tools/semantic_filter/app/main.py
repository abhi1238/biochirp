
import os
import sys
import logging
import asyncio
import time
import uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import torch

from config.guardrail import (
    ParsedValue,
    SimilarityFilteredOutputs,
    QueryInterpreterOutputGuardrail
)
from .similarity_filtered import (
    compute_similarity_filtered_outputs,
    initialize_resources  # ← ADD THIS IMPORT
)

# Check GPU availability (log for debugging)
device = "cuda" if torch.cuda.is_available() else "cpu"

# Configure logging
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Log GPU availability
logger.info(f"PyTorch device: {device}")
if device == "cuda":
    logger.info(f"CUDA available: {torch.cuda.device_count()} GPU(s)")

# Environment configuration
SEMANTIC_TIMEOUT_SEC = float(os.getenv("SEMANTIC_TIMEOUT_SEC", "90"))
VALID_DATABASES = set(os.getenv("VALID_DATABASES", "TTD,CTD,HCDT").split(","))

# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Semantic Similarity Service",
    version="1.0.0",
    description="Semantic similarity search using Qdrant vector database and SentenceTransformer models"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== ADD THIS STARTUP EVENT ==========
@app.on_event("startup")
async def startup_event():
    """
    Load all resources when the app starts.
    
    This loads:
      - Database values from pickle file
      - SentenceTransformer models (3 large models)
      - Qdrant client connection
    
    Resources are loaded ONCE at startup, making all requests fast.
    If loading fails, the service will not start (fail-fast).
    """
    logger.info("=" * 70)
    logger.info("Starting Semantic Similarity Service...")
    logger.info(f"PyTorch device: {device}")
    if device == "cuda":
        logger.info(f"CUDA GPUs available: {torch.cuda.device_count()}")
    logger.info("=" * 70)
    
    try:
        # Load all resources (database values, models, Qdrant client)
        initialize_resources()
        
        logger.info("=" * 70)
        logger.info("✓ Semantic Similarity Service ready to accept requests!")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.exception(f"✗ Failed to initialize Semantic Similarity Service: {e}")
        logger.error("Service will NOT start due to initialization failure")
        # Re-raise to prevent service from starting with broken state
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Semantic Similarity Service...")
    # Add any cleanup logic here if needed (e.g., closing connections)
# ============================================


@app.get("/")
def root():
    """Root endpoint for service health check."""
    return {
        "message": "Semantic Similarity Service is running",
        "device": device,
        "cuda_available": torch.cuda.is_available(),
        "service": "semantic"
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "OK",
        "device": device,
        "service": "semantic"
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to catch any unhandled exceptions."""
    logger.exception(f"[GlobalExceptionHandler] Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "database": "unknown",
            "value": {},
            "tool": "semantic",
            "error": "Internal server error"
        }
    )


@app.post("/semantic", response_model=SimilarityFilteredOutputs)
async def semantic(
    input: QueryInterpreterOutputGuardrail,
    database: str = None
):
    """
    Semantic similarity search endpoint using Qdrant vector database.
    
    Accepts database names in any case (e.g., 'hcdt', 'HCDT', 'Hcdt' all work).
    
    Process:
      1. Validates input and database name
      2. Searches Qdrant vector database for semantically similar terms
      3. Filters results using LLM
      4. Returns matched database values
    
    Args:
        input: Query interpreter output with parsed values
        database: Target database name (TTD, CTD, or HCDT - case insensitive)
        
    Returns:
        SimilarityFilteredOutputs: Semantically matched database values
    """
    tool = "semantic"
    request_id = uuid.uuid4().hex[:8]
    start_time = time.perf_counter()
    
    # Normalize database to uppercase for validation/logging
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
    
    # Validate database name
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
            parsed_dict = input.parsed_value.model_dump(exclude_none=True)
        except Exception as e:
            logger.error(
                f"[{tool} API][{request_id}] Failed to dump parsed_value: {e}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parsed_value: {str(e)}"
            )
        
        if not parsed_dict:
            logger.warning(f"[{tool} API][{request_id}] Empty parsed_value")
            return SimilarityFilteredOutputs(
                database=database,
                value=ParsedValue().model_dump(),
                tool=tool
            )
        
        logger.info(
            f"[{tool} API][{request_id}] Processing {len(parsed_dict)} fields"
        )
        
        # Call compute function with timeout
        # Note: database is passed as uppercase, will be normalized to lowercase inside
        payload = await asyncio.wait_for(
            compute_similarity_filtered_outputs(
                parsed=parsed_dict,
                db=database  # Will be normalized to lowercase inside function
            ),
            timeout=SEMANTIC_TIMEOUT_SEC
        )
        
        elapsed = time.perf_counter() - start_time
        
        # Count total matches
        total_matches = sum(
            len(v) if isinstance(v, list) else 0
            for v in payload.values()
        )
        
        logger.info(
            f"[{tool} API][{request_id}] Success. "
            f"Found {total_matches} total matches. Duration: {elapsed:.2f}s"
        )
        
        return SimilarityFilteredOutputs(
            database=database,
            value=payload,
            tool=tool
        )

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {SEMANTIC_TIMEOUT_SEC}s"
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

    # Safe error fallback with correct type
    logger.info(
        f"[{tool} API][{request_id}] Finished with error, returning empty result"
    )
    
    # Return empty ParsedValue on error
    return SimilarityFilteredOutputs(
        database=database,
        value=ParsedValue().model_dump(),  # Empty dict, correct type
        tool=tool
    )