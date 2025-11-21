# tavily_main.py:

from fastapi import FastAPI
from config.guardrail import TavilyInput, TavilyOutput
from app.tavily import run_tavily
import logging
import os
import uuid
import time
import asyncio
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

app = FastAPI(
    title="BioChirp Tavily Service",
    version="1.0.0",
    description="API for Tavily Service"
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
    return {"message": "Tavily service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/tavily", response_model=TavilyOutput)
async def tavily(input: TavilyInput):
    tool = "tavily"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool} API][{request_id}] Start. Input: {input}")

    try:
        # Run the web search with the provided input
        result = await asyncio.wait_for(run_tavily(input), timeout=60.0)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Output: {result} | elapsed={elapsed:.2f}s")
        return TavilyOutput(message=result, tool=tool)

    except asyncio.TimeoutError as te:
        elapsed = time.perf_counter() - start_time
        msg = f"Timeout after 60s: {te}"
        logger.error(f"[{tool} API][{request_id}] [TIMEOUT] {msg} | elapsed={elapsed:.2f}s")
    except ConnectionError as ce:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {ce}"
        logger.error(f"[{tool} API][{request_id}] [NETWORK ERROR] {msg} | elapsed={elapsed:.2f}s")
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool} API][{request_id}] [EXCEPTION] {msg} | elapsed={elapsed:.2f}s", exc_info=True)

    # Always return schema-valid error object, never a raw error or stack trace
    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return TavilyOutput(message=msg, tool=tool)