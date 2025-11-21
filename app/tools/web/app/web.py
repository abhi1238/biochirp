import asyncio
import logging
import os
from config.guardrail import WebToolInput, WebToolOutput
from agents import Agent, Runner, WebSearchTool
from pydantic import ValidationError
from typing import Any
from fastapi.middleware.cors import CORSMiddleware

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

# Load prompt
md_file_path = "/app/resources/prompts/web_tool_prompt.md"
with open(md_file_path, "r", encoding="utf-8") as f:
    prompt_md = f.read()

WebToolInput.model_rebuild()
WebToolOutput.model_rebuild()


USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))  # slightly under route budget
MODEL_NAME        = os.getenv("WEB_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))



def _run_agent_sync(agent: Agent, query: str) -> Any:
    """
    Runs the async Runner in a dedicated loop (inside a worker thread).
    Used only when USE_THREAD_WRAPPER=true.
    """
    import asyncio as _asyncio

    async def _inner():
        return await Runner.run(agent, query)

    return _asyncio.run(_inner())


async def run_web_search(input_data: WebToolInput) -> WebToolOutput:
    """
    Runs a web search using a dedicated LLM agent and returns a WebToolOutput object.
    Follows single-return pattern and robust exception handling.
    """
    # Single-return value
    message = "Unknown error occurred."
    tool = "web"
    
    try:
        logger.info(f"[run_web_search] Using {MODEL_NAME} for web search")
        system_prompt = prompt_md

        web_agent = Agent(
            name="WebAgent",
            model=MODEL_NAME,
            instructions=system_prompt,
            tools=[WebSearchTool()],
            # output_type=WebToolOutput
        )

        if not USE_THREAD_WRAPPER:
            # Preferred: true-async path
            result = await asyncio.wait_for(
                Runner.run(web_agent, input_data.query),
                timeout=AGENT_TIMEOUT_SEC,
            )
        else:
            # Fallback: offload to thread to avoid blocking event loop if Runner hides sync I/O
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_agent_sync, web_agent, input_data.query),
                timeout=AGENT_TIMEOUT_SEC,
            )

        msg = getattr(result, "final_output", None)
        if isinstance(msg, WebToolOutput):
            message = msg.message
        elif isinstance(msg, str):
            message = msg
        elif hasattr(msg, "message"):
            message = getattr(msg, "message", "No answer field in output.")
        else:
            logger.error("Unexpected output type from web agent: %s", type(msg))
            message = "Error: Unexpected output type from web agent."
    except asyncio.TimeoutError:
        logger.error("[run_web_search] Timed out waiting for web agent response")
        message = "Timeout error: Web agent took too long to respond."
    except ValidationError as ve:
        logger.error("[run_web_search] Pydantic validation error: %s", ve, exc_info=True)
        message = f"Pydantic validation error: {ve}"
    except Exception as e:
        logger.error("[run_web_search] Internal exception: %s", e, exc_info=True)
        message = f"Internal error: {e}"


    logger.info(f"[run_web_search]: {WebToolOutput(message=message, tool = tool)}")

    return WebToolOutput(message=message, tool = tool)


