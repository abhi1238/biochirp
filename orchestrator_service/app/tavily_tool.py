import logging
import os
import uuid
import time
import json
import httpx
import sys
from agents import function_tool
from config.guardrail import TavilyInput, TavilyOutput


# --- LOGGING SETUP: Place at startup (main.py or as early as possible) ---
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)

# # Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)

MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))  # fallback to 60s
# Use environment variable for service URL to avoid hardcoding
TAVILY_SERVICE_URL = os.getenv("TAVILY_SERVICE_URL", "http://biochirp_tavily_tool:8008/tavily")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

@function_tool(
    name_override="tavily",
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
    Queries the Tavily web-search API and returns TavilyOutput.
    """
    tool = "tavily"
    url = TAVILY_SERVICE_URL
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] Started. Query: '{input.query}'")
    payload = {"query": input.query}

    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        elapsed = time.perf_counter() - start_time

        if response.status_code == 200:
            try:
                result = response.json()
                output = TavilyOutput.model_validate(result)
                logger.info(f"[{tool}][{request_id}] Success. Duration: {elapsed:.2f}s. Output: {output}")
                return output
            except json.JSONDecodeError as jde:
                msg = f"{tool} service returned invalid JSON: {jde}"
                logger.error(f"[{tool}][{request_id}] {msg}. Raw response: {response.text}")
                return TavilyOutput(message=msg, tool=tool)
            except Exception as e:
                msg = f"Error building TavilyOutput: {e}"
                logger.error(f"[{tool}][{request_id}] {msg}. Raw response: {response.text}")
                return TavilyOutput(message=msg, tool=tool)
        else:
            msg = f"Tavily tool HTTP error {response.status_code}: {response.text[:200]}"
            logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
            return TavilyOutput(message=msg, tool=tool)

    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        msg = f"{tool} tool timed out after {MAX_TIMEOUT}s (URL: {url})"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. Query: {input.query}")
        return TavilyOutput(message=f"Timeout: {msg}.", tool=tool)
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Connection error: {e}"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. URL: {url}")
        return TavilyOutput(message="Connection error: Unable to reach the Tavily service. Please check network/connectivity.", tool=tool)
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.exception(f"[{tool}][{request_id}] Unexpected error: {e}. Duration: {elapsed:.2f}s")
        return TavilyOutput(message="Internal error: Something went wrong while processing your Tavily search request. Please try again later.", tool=tool)