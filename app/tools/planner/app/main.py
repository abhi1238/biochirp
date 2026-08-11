from fastapi import FastAPI
import logging
import os
import uuid
import time
import asyncio
from utils.service_setup import add_open_cors, add_health_endpoint
from config.guardrail import PlanGenerator, FuzzyFilteredOutputs
from .planner import generate_plan

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "90"))

app = FastAPI(
    title="BioChirp Planner Service",
    version="1.0.0",
    description="API for Planner Service"
)

add_open_cors(app)
@app.get("/")
def root():
    return {"message": "Planner service tool is running"}

add_health_endpoint(app)
@app.post("/planner", response_model=PlanGenerator)
async def plan(input_value: FuzzyFilteredOutputs, database: str):
    tool = "planner"
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    logger.info(f"[{tool} API][{request_id}] Start. Input: {input_value}, database={database}")

    plan_val = None
    try:
        # Optionally use timeout for safety if generate_plan can block
        plan_val = await asyncio.wait_for(generate_plan(input_value, database), timeout=AGENT_TIMEOUT_SEC)
        elapsed = time.perf_counter() - start_time
        logger.info(f"[{tool} API][{request_id}] Success. Output: {plan_val} | elapsed={elapsed:.2f}s")
        return PlanGenerator(database=database, tool=tool, plan=plan_val)

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

    # Always return a schema-valid PlanGenerator output on error
    logger.info(f"[{tool} API][{request_id}] Finished with error")
    return PlanGenerator(
        database=database,
        tool=tool,
        plan=plan_val
    )


