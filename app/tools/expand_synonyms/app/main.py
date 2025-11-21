
from fastapi import FastAPI
import logging
import os
import uuid
import time
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from config.guardrail import ExpandSynonymsOutput, QueryInterpreterOutputGuardrail
from .synonym_expander import synonyms_expander
from typing import Optional

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

app = FastAPI(
    title="BioChirp Expand Synonyms Service",
    version="1.0.0",
    description="API for Expand Synonyms Service"
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
    return {"message": "Expand Synonyms service tool is running"}

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/expand_synonyms", response_model=ExpandSynonymsOutput)
async def expand_synonyms(input: QueryInterpreterOutputGuardrail, database: Optional[str] = None):
    tool = "expand_synonyms"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool}][{request_id}] [START] database={database}")
    logger.info(f"[{tool}][{request_id}] [INPUT] {input}")

    parsed_value = input.model_dump(exclude_none=True).get("parsed_value", {})

    try:
        payload = await synonyms_expander(data=parsed_value)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool}][{request_id}] [SUCCESS] | elapsed={elapsed:.2f}s")

        # logger.info(f"[{tool}][{request_id}] [SUCCESS] Output: {payload} | elapsed={elapsed:.2f}s")
        result = ExpandSynonymsOutput(
            database=database,
            value=payload,
            tool=tool
        )
        return result

    except (asyncio.TimeoutError, ConnectionError) as net_exc:
        elapsed = time.perf_counter() - start_time
        msg = f"Network error: {net_exc}"
        logger.error(f"[{tool}][{request_id}] [NETWORK ERROR] {msg} | elapsed={elapsed:.2f}s")

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        msg = f"Internal error: {e}"
        logger.error(f"[{tool}][{request_id}] [EXCEPTION] {msg} | elapsed={elapsed:.2f}s", exc_info=True)

    # On any exception, always return a valid ExpandSynonymsOutput with input's parsed_value
    return ExpandSynonymsOutput(
        database=database,
        value=parsed_value,
        tool=tool
    )
