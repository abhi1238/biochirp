from fastapi import FastAPI
import logging
import os
import uuid
import time
import asyncio
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input
from .filter import run_llm_member_selection_filter
from fastapi.middleware.cors import CORSMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))
LLM_FILTER_MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-4.1-nano")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))

app = FastAPI(
    title="BioChirp LLM filter Service",
    version="1.0.0",
    description="API for LLM filter Service"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "LLM filter service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/llm_member_selection_filter", response_model=Llm_Member_Selector_Output)
async def llm_member_selection_filter(input: Llm_Member_Selector_Input):
    tool = "llm_member_selection_filter"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool} API][{request_id}] Start. Input: {input}")

    try:
        payload = await asyncio.wait_for(run_llm_member_selection_filter(input), timeout=AGENT_TIMEOUT_SEC)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Output: {payload} | elapsed={elapsed:.2f}s")
        logger.info(f"[{tool} API][{request_id}] Finished")
        return payload

    except asyncio.TimeoutError as te:
        elapsed = time.perf_counter() - start_time
        msg = f"Timeout after {AGENT_TIMEOUT_SEC}s: {te}"
        logger.error(f"[{tool} API][{request_id}] [TIMEOUT] {msg} | elapsed={elapsed:.2f}s")
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] [NETWORK ERROR] {msg} | elapsed={elapsed:.2f}s")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool} API][{request_id}] [EXCEPTION] {msg} | elapsed={elapsed:.2f}s", exc_info=True)

    # Always return schema-valid error object, even on failure
    error_obj = Llm_Member_Selector_Output(
        value=[],
        # tool="llm_member_selection_filter" # Uncomment if your schema includes this field
    )
    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return error_obj
