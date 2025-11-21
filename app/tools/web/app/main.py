from fastapi import FastAPI
from config.guardrail import WebToolInput, WebToolOutput
from app.web import run_web_search
import logging
import os
import uuid
import time
import asyncio
from fastapi.middleware.cors import CORSMiddleware

# logging.basicConfig(
#     level=logging.INFO, 
#     format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
#     datefmt="%Y-%m-%d %H:%M:%S"
# )
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
    logger.info(f"[{tool} API][{request_id}] Start. Input: {input}")

    try:
        payload = await asyncio.wait_for(run_web_search(input), timeout=WEB_TOOL_TIMEOUT)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. | elapsed={elapsed:.2f}s")
        logger.info(f"[{tool} API][{request_id}] Finished")
        return payload

    except asyncio.TimeoutError as te:
        elapsed = time.perf_counter() - start_time
        msg = f"Timeout after {WEB_TOOL_TIMEOUT}s: {te}"
        logger.error(f"[{tool} API][{request_id}] [TIMEOUT] {msg} | elapsed={elapsed:.2f}s")
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] [NETWORK ERROR] {msg} | elapsed={elapsed:.2f}s")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool} API][{request_id}] [EXCEPTION] {msg} | elapsed={elapsed:.2f}s", exc_info=True)

    # Always return a schema-valid error object, even on failure
    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return WebToolOutput(message=msg, tool=tool)

