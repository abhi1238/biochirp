
import logging
import os
import uuid
import time
import json
import httpx
from typing import Optional
from agents import function_tool
from config.guardrail import WebToolInput, WebToolOutput
from pydantic import ValidationError

# Configuration
TOOL_NAME = "web"
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))
WEB_TOOL_URL = os.getenv("WEB_TOOL_URL", "http://biochirp_web_tool:8006/web")

logger = logging.getLogger(__name__)


def create_error_response(error_type: str, details: str, request_id: str, elapsed: float) -> WebToolOutput:
    """Create standardized error response with logging."""
    message = f"{error_type}: {details}"
    logger.error(f"[{TOOL_NAME}][{request_id}] {message} (Duration: {elapsed:.2f}s)")
    return WebToolOutput(message=message, tool=TOOL_NAME)


@function_tool(
    name_override=TOOL_NAME,
    description_override="Performs real-time web search and returns a concise textual answer.",
)
async def web(input: WebToolInput) -> WebToolOutput:
    """
    Executes a web search query and returns structured results.
    
    Args:
        input: WebToolInput containing the search query
        
    Returns:
        WebToolOutput with search results or error message
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    logger.info(f"[{TOOL_NAME}][{request_id}] Query: '{input.query}'")
    
    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(WEB_TOOL_URL, json={"query": input.query})
        
        elapsed = time.perf_counter() - start_time
        
        # Handle non-200 responses
        if response.status_code != 200:
            return create_error_response(
                "HTTP Error",
                f"Service returned status {response.status_code}: {response.text[:200]}",
                request_id,
                elapsed
            )
        
        # Parse JSON response
        try:
            result = response.json()
        except json.JSONDecodeError as e:
            return create_error_response(
                "Parse Error",
                f"Invalid JSON response: {e}",
                request_id,
                elapsed
            )
        
        # Validate response structure
        try:
            output = WebToolOutput.model_validate(result)
        except ValidationError as e:
            return create_error_response(
                "Validation Error",
                f"Unexpected response format: {e}",
                request_id,
                elapsed
            )
        
        # Success
        logger.info(
            f"[{TOOL_NAME}][{request_id}] Success in {elapsed:.2f}s "
            f"(message: {len(output.message)} chars)"
        )
        logger.debug(f"[{TOOL_NAME}][{request_id}] Full output: {output}")
        
        return output
    
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            "Timeout",
            f"Request exceeded {MAX_TIMEOUT}s limit",
            request_id,
            elapsed
        )
    
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        return create_error_response(
            "Connection Error",
            f"Unable to reach web service: {e}",
            request_id,
            elapsed
        )
    
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.exception(f"[{TOOL_NAME}][{request_id}] Unexpected error: {e}")
        return create_error_response(
            "Internal Error",
            "An unexpected error occurred. Please try again later.",
            request_id,
            elapsed
        )