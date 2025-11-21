
import logging
import os
import uuid
import time
import json
import httpx
import sys
from agents import function_tool
from config.guardrail import InterpreterInput, QueryInterpreterOutputGuardrail, ParsedValue


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))  # fallback to 60s

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

@function_tool(
    name_override="interpreter",
    description_override=(
        "Biomedical query interpreter for BioChirp. Must be invoked first for queries on drugs, targets, genes, diseases, pathways, biomarkers, "
        "mechanisms, or approval status. Handles typo correction, synonym expansion, field extraction, negation, and validation. "
        "Returns structured output: cleaned_query, status (valid/invalid), route (biochirp/web), message (explanation), parsed_value, tool."
    ),
)
async def interpreter(input: InterpreterInput) -> QueryInterpreterOutputGuardrail:
    """
    Main entry for the BioChirp interpreter microservice, with full error handling and logging.
    """
    tool = "interpreter"
    url = f"http://biochirp_interpreter_tool:8005/{tool}"

    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    logger.info(f"[{tool}][{request_id}] Started. Query: '{input.query}'")

    # Parse input early for defaults
    query = input.query.strip() if input.query else ""

    payload = {"query": query}

    try:
        async with httpx.AsyncClient(timeout=MAX_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        elapsed = time.perf_counter() - start_time

        if response.status_code == 200:
            try:
                result = response.json()
                output = QueryInterpreterOutputGuardrail.model_validate(result)
                logger.info(f"[{tool}][{request_id}] Success. Duration: {elapsed:.2f}s. Query: {query}")
                return output
            except json.JSONDecodeError as jde:
                msg = f"{tool} tool returned invalid JSON: {jde}. Raw response: {response.text}"
                logger.error(f"[{tool}][{request_id}] {msg}")
            except Exception as e:
                msg = f"Error building QueryInterpreterOutputGuardrail: {e}. Raw response: {response.text}"
                logger.error(f"[{tool}][{request_id}] {msg}")
        else:
            msg = f"Interpreter tool HTTP error {response.status_code}: {response.text[:200]}"
            logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s")

    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start_time
        msg = f"Interpreter tool timed out after {MAX_TIMEOUT}s (URL: {url})"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. Query: {query}")
    except httpx.RequestError as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Connection error: {e}"
        logger.error(f"[{tool}][{request_id}] {msg}. Duration: {elapsed:.2f}s. URL: {url}")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.exception(f"[{tool}][{request_id}] Unexpected error: {e}. Duration: {elapsed:.2f}s")

    # If any error happens, fall back to a schema-valid default with error message
    logger.info(f"[{tool}][{request_id}] Finished with error/fallback")
    return QueryInterpreterOutputGuardrail(
        cleaned_query=query,
        status="invalid",
        route="web",
        message=msg,
        parsed_value=ParsedValue(),
        tool=tool
    )