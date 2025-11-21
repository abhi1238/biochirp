
# app/interpreter.py
from __future__ import annotations
import asyncio
import logging
import os
from typing import Any
from pydantic import ValidationError
from config.guardrail import ParsedValue, QueryInterpreterOutputGuardrail, InterpreterInput
from agents import Agent, Runner, WebSearchTool
import time
from agents import function_tool
import sys

HTTP_200_OK = 200
HTTP_408_REQUEST_TIMEOUT = 408
HTTP_422_UNPROCESSABLE_ENTITY = 422
HTTP_500_INTERNAL_SERVER_ERROR = 500
HTTP_504_GATEWAY_TIMEOUT = 504

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MD_FILE_PATH = "/app/resources/prompts/query_interpreter.md"
with open(MD_FILE_PATH, "r", encoding="utf-8") as f:
    prompt_md = f.read()

MD_FILE_PATH_CLARIFIER_AGENT = "/app/resources/prompts/clarifier_agent.md"
with open(MD_FILE_PATH_CLARIFIER_AGENT, "r", encoding="utf-8") as f:
    prompt_md_clarifier = f.read()

InterpreterInput.model_rebuild()
ParsedValue.model_rebuild()
QueryInterpreterOutputGuardrail.model_rebuild()

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "False").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "60"))  # slightly under route budget
MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "30"))


interpreter_agent_schema_mapper = Agent(
    name="interpreter_agent_schema_mapper",
    model="gpt-4o-mini",
    instructions=prompt_md_clarifier,
    tools=[WebSearchTool()],
    output_type=str,
)

@function_tool(
    name_override="interpreter_schema_mapper",
    description_override=(
        "LLM-backed biomedical query schema-mapping tool for BioChirp. "
        "Takes a raw query and returns ONE clarified, unambiguous, retrieval-ready query sentence. "
        "Does NOT answer the question; only rewrites it."
    ),
)
async def interpreter_schema_mapper(input: InterpreterInput) -> str:
    """
    OpenAI-SDK-based schema-mapping tool.
    Returns a single rephrased query string (never JSON, never multiple sentences).
    """
    tool = "interpreter_schema_mapper"
    # request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    query = input.query.strip() if input.query else ""
    logger.info(f"[{tool}] Started. Query: '{query}'")

    if not query:
        msg = f"[{tool}] Empty query; returning empty string."
        logger.warning(msg)
        return ""

    try:
        # Allow the model to optionally use web search for verification only
        response = await Runner.run(interpreter_agent_schema_mapper, input.query)
        elapsed = time.perf_counter() - start_time
        # responses API convenience property; falls back to first text chunk
        rephrased = (response.final_output or "").strip()

        if not rephrased:
            msg = f"[{tool}] Empty LLM output; falling back to original query."
            logger.error(msg)
            return query

        logger.info(
            f"[{tool}] Success. Duration: {elapsed:.2f}s. "
            f"Rephrased: '{rephrased}'"
        )
        return rephrased

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"[{tool}] OpenAI error: {e}"
        logger.exception(msg)
        # On failure, just return the cleaned original query so pipeline can still proceed.
        return query

def _run_agent_sync(agent: Agent, query: str) -> Any:
    import asyncio as _asyncio
    async def _inner():
        return await Runner.run(agent, query)
    return _asyncio.run(_inner())

async def run_interpreter(input_data: InterpreterInput) -> QueryInterpreterOutputGuardrail:
    """
    Runs the interpreter agent and returns a guardrailed payload.

    - On success: Returns agent's output validated as QueryInterpreterOutputGuardrail.
    - On failure: Returns fallback guardrailed object with error details.
    """
    # Early exit for empty query
    if not input_data.query.strip():
        msg = "Empty query received. Treating as invalid."
        logger.warning(msg)
        return QueryInterpreterOutputGuardrail(
            cleaned_query="",
            status="invalid",
            route="web",
            message=msg,
            parsed_value=ParsedValue(),
            tool="interpreter"
        )

    logger.info(f"[interpreter code] Running for query: '{input_data.query}'")

    try:
        interpreter_agent = Agent(
            name="DrugTargetQueryInterpreterAgent",
            model=MODEL_NAME,
            instructions=prompt_md,
            tools=[WebSearchTool(), interpreter_schema_mapper],
            output_type=QueryInterpreterOutputGuardrail,
        )

        if not USE_THREAD_WRAPPER:
            result = await asyncio.wait_for(
                Runner.run(interpreter_agent, input_data.query),
                timeout=AGENT_TIMEOUT_SEC,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_agent_sync, interpreter_agent, input_data.query),
                timeout=AGENT_TIMEOUT_SEC,
            )

        fo = getattr(result, "final_output", None)

        logger.info(f"The interpreter code output: {fo}")

        # Validate and return
        return QueryInterpreterOutputGuardrail.model_validate(fo.model_dump())

    except asyncio.TimeoutError as e:
        msg = f"Interpreter agent timed out. (code={HTTP_504_GATEWAY_TIMEOUT}) | {e}"
        logger.error(msg)
    except ValidationError as e:
        msg = f"Output validation error: {e} (code={HTTP_422_UNPROCESSABLE_ENTITY})"
        logger.error(msg)
    except Exception as e:
        msg = f"Internal error: {e} (code={HTTP_500_INTERNAL_SERVER_ERROR})"
        logger.exception(msg)

    # Fallback guardrailed response
    logger.info("[interpreter code] Finished with fallback")
    return QueryInterpreterOutputGuardrail(
        cleaned_query=input_data.query,
        status="invalid",
        route="web",
        message=msg,
        parsed_value=ParsedValue(),
        tool="interpreter"
    )