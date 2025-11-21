import logging
import os
import uuid
import time
import json
import httpx
from pydantic import BaseModel, Extra, Field, constr, model_validator, ValidationError
from agents import function_tool
from config.guardrail import ReadmeInput, ReadmeOutput
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")

# logger.setLevel(logging.INFO)  # Or logging.DEBUG for more details

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)

# # Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)

MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))  # fallback to 60s

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

@function_tool(
    name_override="readme",
    description_override=(
        "Fetches a Markdown summary of BioChirp features, capabilities, queries, and usage. "
        "Invoke if user asks about BioChirp's functions, how-to, or guide."
    )
)
async def readme(input: ReadmeInput = ReadmeInput(query="")) -> ReadmeOutput:
    """
    Returns a well-structured Markdown summary of BioChirp's key features, supported queries, and output formats.
    Provides a user-friendly fallback message if any error occurs.
    """
    tool = "readme"
    url = "http://biochirp_readme_tool:8007/readme"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] Started. Query: '{input.query}'")

    # Early exit for empty query
    if not input.query.strip():
        msg = "Empty query received. Returning default readme summary."
        logger.warning(f"[{tool}][{request_id}] {msg}")
        return ReadmeOutput(
            answer="Default readme summary (empty query).",
            tool=tool,
            message=msg
        )

    payload = {"query": input.query}

    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(url, json=payload)

        elapsed = time.perf_counter() - start_time

        if response.status_code == 200:
            try:
                result = response.json()
                output = ReadmeOutput.model_validate(result)
                logger.info(f"[{tool}][{request_id}] Success. Duration: {elapsed:.2f}s")
                return output
            except json.JSONDecodeError as jde:
                msg = f"{tool} tool returned invalid JSON: {jde}. Raw response: {response.text}"
                logger.error(f"[{tool}][{request_id}] {msg}")
                return ReadmeOutput(
                    answer="Error: Invalid response format from readme service.",
                    tool=tool,
                    message=msg
                )
            except ValidationError as ve:
                msg = f"Validation error in ReadmeOutput: {ve}"
                logger.error(f"[{tool}][{request_id}] {msg}. Raw response: {response.text}")
                return ReadmeOutput(
                    answer="Error: Response validation failed.",
                    tool=tool,
                    message=msg
                )
        else:
            msg = f"Readme tool HTTP error {response.status_code}: {response.text[:200]}"
            logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
            return ReadmeOutput(
                answer="Error: Unable to fetch readme due to server error.",
                tool=tool,
                message=msg
            )

    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        msg = f"Readme tool timed out after {MAX_TIMEOUT}s (URL: {url})"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. Query: {input.query}")
        return ReadmeOutput(
            answer="Timeout: Unable to fetch readme in time.",
            tool=tool,
            message=msg
        )
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Connection error: {e}"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. URL: {url}")
        return ReadmeOutput(
            answer="Connection error: Unable to reach the readme service.",
            tool=tool,
            message=msg
        )
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.exception(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")
        return ReadmeOutput(
            answer="Internal error: Something went wrong while processing your request.",
            tool=tool,
            message=msg
        )