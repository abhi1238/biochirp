import logging
import os
import uuid
import time
import asyncio
from fastapi import FastAPI
from config.guardrail import FuzzyFilteredOutputs, QueryInterpreterOutputGuardrail
from .fuzzy_search_db_wise import compute_fuzzy_filtered_outputs
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
MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))
WEB_TOOL_TIMEOUT = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))

# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Fuzzy Service",
    version="1.0.0",
    description="API for Fuzzy Service"
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
    return {"message": "Fuzzy service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/fuzzy", response_model=FuzzyFilteredOutputs)
async def fuzzy(input: QueryInterpreterOutputGuardrail, database: str):
    tool = "fuzzy"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool}][{request_id}] [START] database={database}")
    logger.info(f"[{tool}][{request_id}] [INPUT] {input}")

    try:
        parsed_value = input.parsed_value.model_dump(exclude_none=True)
        payload = await compute_fuzzy_filtered_outputs(parsed=parsed_value, database=database)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool}][{request_id}] [SUCCESS] Output: {payload} | elapsed={elapsed:.2f}s")
        return payload

    except (asyncio.TimeoutError, ConnectionError) as net_exc:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {net_exc}"
        logger.error(f"[{tool}][{request_id}] [NETWORK ERROR] {msg} | elapsed={elapsed:.2f}s")

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool}][{request_id}] [EXCEPTION] {msg} | elapsed={elapsed:.2f}s", exc_info=True)

    # Always return a valid FuzzyFilteredOutputs on error
    return FuzzyFilteredOutputs(
        database=database,
        value=input.parsed_value.model_dump(exclude_none=True),
        tool=tool
    )
