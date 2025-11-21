import logging
import os
import uuid
import time
import json
import httpx
import sys
from agents import function_tool
from config.guardrail import WebToolInput, WebToolOutput


# --- LOGGING SETUP: Place at startup (main.py or as early as possible) ---
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)


# logger = logging.getLogger("uvicorn.error")
logger = logging.getLogger("uvicorn.error")
# logger.setLevel(logging.INFO)  # Or logging.DEBUG for more details

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)

# # Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)

MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))  # fallback to 60s if not set

@function_tool(
    name_override="web",
    description_override="Performs real-time web search and returns a concise textual answer.",
)
async def web(input: WebToolInput) -> WebToolOutput:
    """
    Runs a lightweight web-search agent and returns WebToolOutput(message=str).
    """
    tool = "web"
    request_id = str(uuid.uuid4())
    url = "http://biochirp_web_tool:8006/web"
    start_time = time.perf_counter()

    payload = {"query": input.query}
    logger.info(f"[{tool}][{request_id}] Started. Query: '{input.query}'")

    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(url, json=payload)

        elapsed = time.perf_counter() - start_time

        if response.status_code == 200:
            try:
                result = response.json()
                output = WebToolOutput.model_validate(result)
                logger.info(f"[{tool}][{request_id}] Success. Duration: {elapsed:.2f}s. Output: {output}")
                logger.info("--------------------------------------------------------------------------- \n \n ")
                return output
            except json.JSONDecodeError as jde:
                msg = f"Failed to decode JSON from web tool response: {jde}"
                logger.error(f"[{tool}][{request_id}] {msg}. Raw response: {response.text}")
                return WebToolOutput(message=msg, tool=tool)
            except Exception as e:
                msg = f"Error building WebToolOutput: {e}"
                logger.error(f"[{tool}][{request_id}] {msg}. Raw response: {response.text}")
                return WebToolOutput(message=msg, tool=tool)
        else:
            msg = f"Web tool HTTP error {response.status_code}: {response.text[:200]}"
            logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
            return WebToolOutput(message=msg, tool=tool)

    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        msg = f"Web tool timed out after {MAX_TIMEOUT}s (URL: {url})"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
        return WebToolOutput(message=f"Timeout: {msg}.", tool=tool)
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Connection error: {e}"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
        return WebToolOutput(message=f"Connection error: Unable to reach the web tool. Please check network/connectivity.", tool=tool)
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.exception(f"[{tool}][{request_id}] Unexpected error: {e}. Duration: {elapsed:.2f}s")
        return WebToolOutput(message="Internal error: Something went wrong while processing your web search request. Please try again later.", tool=tool)
