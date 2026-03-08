
import asyncio
import logging
import os
import sys
from config.guardrail import WebToolInput, WebToolOutput
from agents import Agent, Runner, WebSearchTool
from pydantic import ValidationError
from typing import Any

# Setup logging
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

# Load prompt
MD_FILE_PATH = "/app/resources/prompts/web_tool_prompt.md"
with open(MD_FILE_PATH, "r", encoding="utf-8") as f:
    prompt_md = f.read()

# Rebuild Pydantic models
WebToolInput.model_rebuild()
WebToolOutput.model_rebuild()

# Environment configuration
USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))
MODEL_NAME = os.getenv("WEB_MODEL_NAME", "gpt-4o-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "5000"))


def _run_agent_sync(agent: Agent, query: str) -> Any:
    """
    Runs the async Runner in a dedicated loop (inside a worker thread).
    Used only when USE_THREAD_WRAPPER=true.
    
    Args:
        agent: The Agent instance to run
        query: The query string to process
        
    Returns:
        The agent's execution result
    """
    import asyncio as _asyncio

    async def _inner():
        return await Runner.run(agent, query)

    return _asyncio.run(_inner())


async def run_web_search(input_data: WebToolInput) -> WebToolOutput:
    """
    Runs a web search using a dedicated LLM agent and returns a WebToolOutput object.
    
    Args:
        input_data: WebToolInput containing the search query and parameters
        
    Returns:
        WebToolOutput: Structured search results or error information
        
    Note:
        All exceptions are caught and returned as WebToolOutput objects,
        ensuring consistent API response format.
    """
    tool = "web"
    
    # Input validation
    if not input_data or not input_data.query:
        logger.warning("[run_web_search] Empty or None query received")
        return WebToolOutput(
            message="Error: Empty query provided",
            tool=tool
        )
    
    # Strip whitespace and validate
    query = input_data.query.strip()
    if not query:
        logger.warning("[run_web_search] Query contains only whitespace")
        return WebToolOutput(
            message="Error: Query contains only whitespace",
            tool=tool
        )
    
    # Check query length
    if len(query) > MAX_QUERY_LENGTH:
        logger.warning(f"[run_web_search] Query too long: {len(query)} > {MAX_QUERY_LENGTH}")
        return WebToolOutput(
            message=f"Error: Query exceeds maximum length ({len(query)} > {MAX_QUERY_LENGTH})",
            tool=tool
        )
    
    # Log query (truncated for security)
    query_preview = query[:100] + "..." if len(query) > 100 else query
    logger.info(f"[run_web_search] Starting search with {MODEL_NAME}. Query: '{query_preview}'")
    
    try:
        # Create agent with structured output
        web_agent = Agent(
            name="WebAgent",
            model=MODEL_NAME,
            instructions=prompt_md,
            tools=[WebSearchTool()],
            output_type=WebToolOutput  # Critical: ensures structured output
        )

        # Run agent with appropriate wrapper
        if not USE_THREAD_WRAPPER:
            # Preferred: true-async path
            result = await asyncio.wait_for(
                Runner.run(web_agent, query),
                timeout=AGENT_TIMEOUT_SEC,
            )
        else:
            # Fallback: offload to thread to avoid blocking event loop
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_agent_sync, web_agent, query),
                timeout=AGENT_TIMEOUT_SEC,
            )

        # Extract output (should already be WebToolOutput due to output_type)
        output = result.final_output
        
        # Validate output type
        if not isinstance(output, WebToolOutput):
            logger.error(f"[run_web_search] Unexpected output type: {type(output)}")
            return WebToolOutput(
                message=f"Error: Agent returned unexpected type {type(output).__name__}",
                tool=tool
            )
        
        logger.info(f"[run_web_search] Success. Message: {output.message[:100]}...")
        return output

    except asyncio.TimeoutError:
        logger.error(f"[run_web_search] Timeout after {AGENT_TIMEOUT_SEC} seconds")
        return WebToolOutput(
            message=f"Timeout: Web search exceeded {AGENT_TIMEOUT_SEC}s time limit",
            tool=tool
        )
        
    except ValidationError as ve:
        logger.error(f"[run_web_search] Validation error: {ve}", exc_info=True)
        return WebToolOutput(
            message=f"Validation error: {str(ve)}",
            tool=tool
        )
        
    except ConnectionError as ce:
        logger.error(f"[run_web_search] Connection error: {ce}", exc_info=True)
        return WebToolOutput(
            message=f"Connection error: {str(ce)}",
            tool=tool
        )
        
    except Exception as e:
        logger.exception(f"[run_web_search] Unexpected error: {e}")
        return WebToolOutput(
            message=f"Internal error: {str(e)}",
            tool=tool
        )