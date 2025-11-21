# main.py
from fastapi import FastAPI
import logging
import os
import uuid
import time
import asyncio
from app.readme import run_readme
from config.guardrail import ReadmeInput, ReadmeOutput
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

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "False").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "60"))
MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "60"))

app = FastAPI(
    title="BioChirp Readme Service",
    version="1.0.0",
    description="API for Readme Service"
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
    return {"message": "Readme service is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/readme", response_model=ReadmeOutput)
async def readme_endpoint(input: ReadmeInput):
    tool = "readme"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool} API][{request_id}] Started. Input: {input}")

    # Early exit for empty input if needed (though ReadmeInput likely has no fields)
    try:
        payload = await asyncio.wait_for(run_readme(input), timeout=WEB_TOOL_TIMEOUT)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Output: {payload}. Duration: {elapsed:.2f}s")
        return payload

    except asyncio.TimeoutError as te:
        elapsed = time.perf_counter() - start_time
        msg = f"Timeout after {WEB_TOOL_TIMEOUT}s: {te}"
        logger.error(f"[{tool} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] {msg}. Duration: {elapsed:.2f}s")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool} API][{request_id}] {msg}. Duration: {elapsed:.2f}s", exc_info=True)

    # Always return valid ReadmeOutput on error
    error_obj = ReadmeOutput(
        answer="An error occurred while retrieving the README.",
        tool=tool,
        message=msg
    )
    logger.info(f"[{tool} API][{request_id}] Finished with fallback")
    return error_obj