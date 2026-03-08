

"""HCDT (Highly Confident Drug-Target) database query tool."""
from __future__ import annotations

import logging
import os
import uuid
import time
import json
from typing import Optional

import httpx
from agents import function_tool
from config.guardrail import QueryInterpreterOutputGuardrail, DatabaseTable
from pydantic import ValidationError

# Configuration
SERVICE_NAME = "hcdt"
HCDT_PORT = os.getenv("HCDT_TOOL_PORT", "8018")
API_URL = f"http://biochirp_{SERVICE_NAME}_tool:{HCDT_PORT}/{SERVICE_NAME}"
TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))

# Get logger (assuming logging configured in main.py)
logger = logging.getLogger(__name__)


def create_error_response(
    error_type: str,
    details: str,
    request_id: str,
    elapsed: float
) -> DatabaseTable:
    """Create standardized error response with logging."""
    error_msg = f"{error_type}: {details}"
    logger.error(
        f"[{SERVICE_NAME}][{request_id}] {error_msg} "
        f"(Duration: {elapsed:.2f}s)"
    )
    return DatabaseTable(
        database=SERVICE_NAME.upper(),
        table=None,
        csv_path=None,
        row_count=None,
        tool=SERVICE_NAME,
        message=error_msg,
    )


@function_tool(
    name_override=SERVICE_NAME,
    description_override=(
        f"Query the {SERVICE_NAME.upper()} (Highly Confident Drug-Target) database and stream back "
        f"a preview of results plus a link to the full CSV via WebSocket. Returns structured data "
        f"with drug-target associations, confidence scores, and supporting evidence."
    )
)
async def hcdt(
    input: QueryInterpreterOutputGuardrail,
    connection_id: Optional[str] = None
) -> DatabaseTable:
    """
    Query the HCDT database for high-confidence drug-target associations.
    
    Args:
        input: Structured query from the interpreter
        connection_id: WebSocket connection ID for streaming results
        
    Returns:
        DatabaseTable with preview data and CSV path, or error message
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    log_prefix = f"[{SERVICE_NAME}][{request_id}]"
    logger.info(
        f"{log_prefix} Query: '{input.cleaned_query[:100]}', "
        f"Status: {input.status}, Route: {input.route}, "
        f"Connection: {connection_id or 'none'}"
    )
    
    # Build request parameters
    params = {"connection_id": connection_id} if connection_id else None
    
    try:
        # Make async HTTP request
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                API_URL,
                json=input.model_dump(),
                params=params
            )
        
        elapsed = time.perf_counter() - start_time
        
        # Handle non-200 responses
        if response.status_code != 200:
            return create_error_response(
                error_type="HTTP Error",
                details=f"Service returned status {response.status_code}: {response.text[:200]}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Parse JSON response
        try:
            result = response.json()
        except json.JSONDecodeError as e:
            return create_error_response(
                error_type="Parse Error",
                details=f"Invalid JSON response: {e}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Validate response structure
        try:
            output = DatabaseTable(**result)
        except (ValidationError, TypeError) as e:
            return create_error_response(
                error_type="Validation Error",
                details=f"Unexpected response format: {e}",
                request_id=request_id,
                elapsed=elapsed
            )
        
        # Success
        logger.info(
            f"{log_prefix} Success in {elapsed:.2f}s. "
            f"Rows: {output.row_count or 'unknown'}, "
            f"CSV: {'yes' if output.csv_path else 'no'}"
        )
        logger.debug(f"{log_prefix} Full response: {output}")
        
        return output
    
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            error_type="Timeout",
            details=f"Request exceeded {TIMEOUT}s limit",
            request_id=request_id,
            elapsed=elapsed
        )
    
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            error_type="Connection Error",
            details=f"Unable to reach {SERVICE_NAME.upper()} service: {e}",
            request_id=request_id,
            elapsed=elapsed
        )
    
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.exception(f"{log_prefix} Unexpected error: {e}")
        return create_error_response(
            error_type="Internal Error",
            details="An unexpected error occurred. Please try again later.",
            request_id=request_id,
            elapsed=elapsed
        )