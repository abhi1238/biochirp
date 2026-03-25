import logging
import os
import uuid
import time
import json
import httpx
from typing import Optional
from agents import function_tool
from config.guardrail import InterpreterInput, QueryInterpreterOutputGuardrail, ParsedValue
from pydantic import ValidationError

# Configuration
TOOL_NAME = "interpreter"
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "120"))
INTERPRETER_SERVICE_URL = os.getenv(
    "INTERPRETER_SERVICE_URL",
    "http://biochirp_interpreter_tool:8005/interpreter"
)

# Get logger (assuming logging configured in main.py)
logger = logging.getLogger(__name__)


def create_error_response(
    query: str,
    error_type: str,
    details: str,
    request_id: str,
    elapsed: float
) -> QueryInterpreterOutputGuardrail:
    """Create standardized error response with logging."""
    message = f"{error_type}: {details}"
    logger.error(
        f"[{TOOL_NAME}][{request_id}] {message} "
        f"(Duration: {elapsed:.2f}s, Query: '{query[:100]}')"
    )
    return QueryInterpreterOutputGuardrail(
        cleaned_query=query,
        status="invalid",
        route="web",
        message=message,
        parsed_value=ParsedValue(),
        tool=TOOL_NAME
    )


@function_tool(
    name_override=TOOL_NAME,
    description_override=(
        "Biomedical query interpreter for BioChirp. Must be invoked first for queries on drugs, targets, genes, diseases, pathways, biomarkers, "
        "mechanisms, or approval status. Handles typo correction, synonym expansion, field extraction, negation, and validation. "
        "Returns structured output: cleaned_query, status (valid/invalid), route (biochirp/web), message (explanation), parsed_value, tool."
    ),
)
async def interpreter(input: InterpreterInput) -> QueryInterpreterOutputGuardrail:
    """
    Interprets and structures biomedical queries for downstream processing.
    
    Args:
        input: InterpreterInput containing the user query
        
    Returns:
        QueryInterpreterOutputGuardrail with structured query interpretation
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # Sanitize query early
    query = input.query.strip() if input.query else ""
    
    logger.info(f"[{TOOL_NAME}][{request_id}] Query: '{query[:100]}'")
    
    try:
        # Make HTTP request
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(
                INTERPRETER_SERVICE_URL,
                json={"query": query}
            )
        
        elapsed = time.perf_counter() - start_time
        
        # Handle non-200 responses
        if response.status_code != 200:
            return create_error_response(
                query=query,
                error_type="HTTP Error",
                details=f"Service returned status {response.status_code}: {response.text[:1000]}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Parse JSON response
        try:
            result = response.json()
        except json.JSONDecodeError as e:
            return create_error_response(
                query=query,
                error_type="Parse Error",
                details=f"Invalid JSON response: {e}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Validate response structure
        try:
            output = QueryInterpreterOutputGuardrail.model_validate(result)
        except ValidationError as e:
            return create_error_response(
                query=query,
                error_type="Validation Error",
                details=f"Unexpected response format: {e}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Success
        logger.info(
            f"[{TOOL_NAME}][{request_id}] Success in {elapsed:.2f}s. "
            f"Status: {output.status}, Route: {output.route}"
        )
        logger.debug(f"[{TOOL_NAME}][{request_id}] Full output: {output}")
        
        return output
    
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            query=query,
            error_type="Timeout",
            details=f"Request exceeded {MAX_TIMEOUT}s limit",
            request_id=request_id,
            elapsed=elapsed
        )
    
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            query=query,
            error_type="Connection Error",
            details=f"Unable to reach interpreter service: {e}",
            request_id=request_id,
            elapsed=elapsed
        )
    
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.exception(f"[{TOOL_NAME}][{request_id}] Unexpected error: {e}")
        return create_error_response(
            query=query,
            error_type="Internal Error",
            details="An unexpected error occurred. Please try again later.",
            request_id=request_id,
            elapsed=elapsed
        )