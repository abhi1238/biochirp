from fastapi import FastAPI
from config.guardrail import WebToolInput, WebToolOutput
from app.web import run_web_search
import logging
import os
import uuid
import time
import asyncio
from fastapi.middleware.cors import CORSMiddleware

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("uvicorn.error")

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))
MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))

app = FastAPI(
    title="BioChirp Web Service",
    version="1.0.0",
    description="API for Web Service"
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
    return {"message": "Web service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}
@app.post("/web", response_model=WebToolOutput)
async def web(input: WebToolInput):
    tool = "web"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    
    # FIX: Initialize error message
    error_msg = "Unknown error occurred"
    
    # FIX: Add input validation
    if not input.query or not input.query.strip():
        logger.warning(f"[{tool} API][{request_id}] Empty query")
        return WebToolOutput(message="Error: Empty query", tool=tool)
    
    logger.info(f"[{tool} API][{request_id}] Started. Query: '{input.query[:100]}...'")

    try:
        payload = await asyncio.wait_for(run_web_search(input), timeout=WEB_TOOL_TIMEOUT)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Duration: {elapsed:.2f}s")
        return payload

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Timeout after {WEB_TOOL_TIMEOUT}s"
        logger.error(f"[{tool} API][{request_id}] [TIMEOUT] {error_msg}. Duration: {elapsed:.2f}s")
        
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] [NETWORK ERROR] {error_msg}. Duration: {elapsed:.2f}s")
        
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        error_msg = f"Internal error: {e}"
        logger.exception(f"[{tool} API][{request_id}] [EXCEPTION] {error_msg}. Duration: {elapsed:.2f}s")

    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return WebToolOutput(message=error_msg, tool=tool)  # Now guaranteed to exist