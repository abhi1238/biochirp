
import logging
import os
import asyncio
from fastapi import FastAPI
from config.guardrail import ParsedValue, SimilarityFilteredOutputs, QueryInterpreterOutputGuardrail
from .similarity_filtered import compute_similarity_filtered_outputs
import torch
import time, uuid
# Check GPU availability
device = "cuda" if torch.cuda.is_available() else "cpu"
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

USE_THREAD_WRAPPER = os.getenv("USE_THREAD_WRAPPER", "True").lower() in {"1", "true", "yes"}
AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))  # slightly under route budget
MODEL_NAME        = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")
OPENAI_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT", "30"))  # per request
WEB_TOOL_TIMEOUT    = float(os.getenv("WEB_TOOL_TIMEOUT", "90"))

# Initialize FastAPI app
app = FastAPI(
    title="BioChirp Semantic Service",
    version="1.0.0",
    description="API for Semantic Service")

# Root endpoint
@app.get("/")
def root():
    return {"message": "Semantic service tool is running"}

# Health check endpoint (replaces /ws/health)
@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/semantic", response_model=SimilarityFilteredOutputs)
async def semantic(input: QueryInterpreterOutputGuardrail, database: str):

    tool = "semantic"

    rid = uuid.uuid4().hex[:8]

    start = time.time()
    logger.info(f"[semantic] rid={rid} start database={database}")

    logger.info(f"[{tool} api] Running")

    try:
        payload = await compute_similarity_filtered_outputs(parsed = input.parsed_value.model_dump(), db = database)

        logger.info(f"[{tool} api] output: {payload}")

        logger.info(f"[{tool} api] Finished")

        logger.info(f"[semantic] rid={rid} done in {time.time()-start:.3f}s")


        return SimilarityFilteredOutputs(database=database, value=payload, tool = tool)
    
    except (asyncio.TimeoutError, ConnectionError) as net_exc:
        msg = f"Network error: {net_exc}"
        logger.error(f"[{tool} api] Network error: {msg}")

    except Exception as e:
        msg = f"Internal error: {e}"
        logger.error(f"[{tool} api] Exception: {msg}", exc_info=True)

        logger.info(f"[semantic] rid={rid} done in {time.time()-start:.3f}s")


        return SimilarityFilteredOutputs(database=database, value=input, tool = tool)
    