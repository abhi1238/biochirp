
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
from .biochirp_agent import biochirp_agent_output

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


MD_FILE_PATH_BIOMEDICAL_CLASSIFIER_AGENT = "/app/resources/prompts/biomedical_query_classifier.md"
with open(MD_FILE_PATH_BIOMEDICAL_CLASSIFIER_AGENT, "r", encoding="utf-8") as f:
    prompt_md_biomedical_classifier = f.read()

InterpreterInput.model_rebuild()
ParsedValue.model_rebuild()
QueryInterpreterOutputGuardrail.model_rebuild()

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "False").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "60"))  # slightly under route budget
INTERPRETER_MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-4.1-mini")
INTERPRETER_AGENT_SCHEMA_MAPPER_MODEL_NAME  = os.getenv("INTERPRETER_AGENT_SCHEMA_MAPPER_MODEL_NAME", "gpt-4.1-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "30"))
BIOCHIRP_SCOPE_MODEL_NAME = os.getenv("BIOCHIRP_SCOPE_MODEL_NAME", "gpt-4.1-mini")
JUDGE_MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-4.1-mini")


interpreter_agent_schema_mapper = Agent(
    name="interpreter_agent_schema_mapper",
    model=INTERPRETER_AGENT_SCHEMA_MAPPER_MODEL_NAME,
    instructions=prompt_md_clarifier,
    tools=[WebSearchTool()],
    output_type=str,
)


biochirp_scope_checker_agent = Agent(
    name="biochirp_scope_checker",
    model=BIOCHIRP_SCOPE_MODEL_NAME,
    instructions=prompt_md_biomedical_classifier,
    tools=[],
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
        # response = await Runner.run(interpreter_agent_schema_mapper, input.query)
        response = await biochirp_agent_output(
        user_prompt=input.query,
        system_prompt=prompt_md_clarifier,
        # low_cost_model_results=low_cost_model_results,
        judge_model=JUDGE_MODEL_NAME)
        elapsed = time.perf_counter() - start_time

        # output = (response.final_output or "").strip()
        rephrased = response
        # elapsed = time.perf_counter() - start_time
        # # responses API convenience property; falls back to first text chunk
        # rephrased = (response.final_output or "").strip()

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
    



@function_tool(
    name_override="biochirp_scope_checker",
    description_override=(
        "Biomedical validity and scope classification tool for BioChirp. "
        "Determines whether a query is biomedical, whether it falls within BioChirp scope, "
        "and whether it is fully answerable, partially answerable, or out of scope. "
        "Does NOT parse schema fields and does NOT answer the query."
    ),
)
async def biochirp_scope_checker(input: str) -> str:
    """
    Determines:
    1) Whether the query is biomedical
    2) Whether it is within BioChirp scope
    3) Whether it is fully or partially answerable
    Returns a short, structured natural-language explanation (not JSON).
    """
    tool = "biochirp_scope_checker"
    start_time = time.perf_counter()

    query = str(input)

    # query = input.query.strip() if input.query else ""
    logger.info(f"[{tool}] Started. Query: '{query}'")

    if not query:
        msg = (
            "Invalid query. The input is empty. "
            "Status is invalid and route should be set to web."
        )
        logger.warning(f"[{tool}] Empty query.")
        return msg

    try:
        # response = await Runner.run(biochirp_scope_checker_agent, query)
        response = await biochirp_agent_output(
        user_prompt=query,
        system_prompt=prompt_md_biomedical_classifier,
        # low_cost_model_results=low_cost_model_results,
        judge_model=JUDGE_MODEL_NAME)
        elapsed = time.perf_counter() - start_time

        # output = (response.final_output or "").strip()
        output = response

        print(output)

        if not output:
            logger.error(f"[{tool}] Empty LLM output.")
            return (
                "Invalid query. The biomedical scope could not be determined. "
                "Status is invalid and route should be set to web."
            )

        logger.info(
            f"[{tool}] Success. Duration: {elapsed:.2f}s. "
            f"Output: '{output}'"
        )
        return output

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.exception(f"[{tool}] Error after {elapsed:.2f}s: {e}")
        return (
            "Invalid query due to internal error. "
            "Status is invalid and route should be set to web."
        )



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
            model=INTERPRETER_MODEL_NAME,
            instructions=prompt_md,
            tools=[WebSearchTool(), interpreter_schema_mapper, biochirp_scope_checker],
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

        logger.info(f"[interpreter code output]: {fo}")

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