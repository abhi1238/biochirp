
import logging
import os
import uuid
import time
import json
import httpx
from typing import Optional
from agents import function_tool
from config.guardrail import TavilyInput, TavilyOutput
from pydantic import ValidationError

# Configuration
TOOL_NAME = "tavily"
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))
TAVILY_SERVICE_URL = os.getenv("TAVILY_SERVICE_URL", "http://biochirp_tavily_tool:8008/tavily")

# Get logger (assuming logging is configured in main.py)
logger = logging.getLogger(__name__)


def create_error_response(error_type: str, details: str, request_id: str, elapsed: float) -> TavilyOutput:
    """Create standardized error response with logging."""
    message = f"{error_type}: {details}"
    logger.error(f"[{TOOL_NAME}][{request_id}] {message} (Duration: {elapsed:.2f}s)")
    return TavilyOutput(message=message, tool=TOOL_NAME)


@function_tool(
    name_override=TOOL_NAME,
    description_override="""
    Web search via Tavily API for biomedical, scientific, or clinical queries.

    **Purpose:**
    - Fetch real-time, factual, or external web info.
    - Clarify ambiguous, rare, or unknown biomedical terms/acronyms.
    - Provide context for unfamiliar biomedical entities.

    **When to Use:**
    - For recent findings, guidelines, or news.
    - When internal DBs can't resolve an entity.
    - For explanations or expansions of biomedical terms.

    **Citations:**
    - Includes title, URL (optional: source/snippet).
    - Always cite alongside facts/snippets.

    **Results:**
    - List of dicts with title, URL, etc.
    """
)
async def tavily(input: TavilyInput) -> TavilyOutput:
    """
    Queries the Tavily web-search API and returns structured results.
    
    Args:
        input: TavilyInput containing the search query
        
    Returns:
        TavilyOutput with search results or error message
    """
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    logger.info(f"[{TOOL_NAME}][{request_id}] Query: '{input.query}'")
    
    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(TAVILY_SERVICE_URL, json={"query": input.query})
        
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
            output = TavilyOutput.model_validate(result)
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
            f"(message: {len(output.message) if output.message else 0} chars)"
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
            f"Unable to reach Tavily service: {e}",
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