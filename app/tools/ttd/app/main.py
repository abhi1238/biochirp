# # main.py
# from fastapi import FastAPI, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# import logging
# import uuid
# import os
# from config.guardrail import QueryInterpreterOutputGuardrail, DatabaseTable
# from app.ttd import return_ttd_result   # <-- the *only* import we need

# SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")  # Generalized via env var

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
# )
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("httpcore").setLevel(logging.WARNING)
# logger = logging.getLogger(__name__)

# app = FastAPI(title="BioChirp TTD Service", version="1.0.0")
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=False,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# @app.get("/")
# def root():
#     return {"message": f"{SERVICE_NAME.upper()} service is up"}

# @app.get("/health")
# async def health():
#     return {"status": "OK"}

# @app.post(f"/{SERVICE_NAME}", response_model=DatabaseTable)
# async def ttd_endpoint(
#     payload: QueryInterpreterOutputGuardrail,
#     connection_id: str | None = None,          # <-- passed from orchestrator
# ):
#     """
#     Public endpoint forwards everything to ttd.py.
#     """
#     request_id = str(uuid.uuid4())
#     log_prefix = f"[{SERVICE_NAME} API][{request_id}]"
#     logger.info(f"{log_prefix} START | connection_id={connection_id}")

#     try:
#         result = await return_ttd_result(input=payload, connection_id=connection_id)
#         logger.info(f"{log_prefix} SUCCESS | rows={result.row_count}")
#         logger.info(f"{log_prefix} SUCCESS | rows={result}")


#         return result
#     except Exception as exc:
#         error_msg = f"{SERVICE_NAME} API error: {str(exc)}"
#         logger.error(f"{log_prefix} EXCEPTION: {error_msg}", exc_info=True)
#         return DatabaseTable(
#             database=SERVICE_NAME,
#             table=None,
#             csv_path=None,
#             row_count=None,
#             tool=SERVICE_NAME,
#             message=error_msg
#         )




# main.py
import logging
import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config.guardrail import QueryInterpreterOutputGuardrail, DatabaseTable
from app.ttd import return_ttd_result, get_ttd_db  # <-- import the getter so we can preload

SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")  # Generalized via env var

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# logger = logging.getLogger(__name__)
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="BioChirp TTD Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def preload_ttd():
    """
    Optional: preload TTD DB at service startup so the first request
    doesn't pay the cold-start cost.
    """
    try:
        get_ttd_db()
        logger.info("[startup] TTD database preloaded successfully")
    except Exception as e:
        logger.error("[startup] Failed to preload TTD database: %s", e, exc_info=True)


@app.get("/")
def root():
    return {"message": f"{SERVICE_NAME.upper()} service is up"}


@app.get("/health")
async def health():
    return {"status": "OK"}


@app.post(f"/{SERVICE_NAME}", response_model=DatabaseTable)
async def ttd_endpoint(
    payload: QueryInterpreterOutputGuardrail,
    connection_id: str | None = None,          # <-- passed from orchestrator
):
    """
    Public endpoint forwards everything to ttd.py.
    """
    request_id = str(uuid.uuid4())
    log_prefix = f"[{SERVICE_NAME} API][{request_id}]"
    logger.info(f"{log_prefix} START | connection_id={connection_id}")

    try:
        result = await return_ttd_result(input=payload, connection_id=connection_id)
        logger.info(f"{log_prefix} SUCCESS | rows={result.row_count}")
        logger.info(f"{log_prefix} RESULT: {result}")
        return result
    except Exception as exc:
        error_msg = f"{SERVICE_NAME} API error: {str(exc)}"
        logger.error(f"{log_prefix} EXCEPTION: {error_msg}", exc_info=True)
        # Return a graceful DatabaseTable error object instead of raising HTTPException
        return DatabaseTable(
            database=SERVICE_NAME,
            table=None,
            csv_path=None,
            row_count=None,
            tool=SERVICE_NAME,
            message=error_msg,
        )




# # main.py

# from fastapi import FastAPI
# from fastapi.middleware.cors import CORSMiddleware
# import logging
# import uuid
# import os

# from config.guardrail import QueryInterpreterOutputGuardrail, DatabaseTable
# from app.ttd import return_ttd_result

# SERVICE_NAME = os.getenv("SERVICE_NAME", "ttd")

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
# )
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("httpcore").setLevel(logging.WARNING)
# logger = logging.getLogger(__name__)

# app = FastAPI(title="BioChirp TTD Service", version="1.0.0")
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=False,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# @app.get("/")
# def root():
#     return {"message": f"{SERVICE_NAME.upper()} service is up"}

# @app.get("/health")
# async def health():
#     return {"status": "OK"}

# @app.post(f"/{SERVICE_NAME}", response_model=DatabaseTable)
# async def ttd_endpoint(
#     payload: QueryInterpreterOutputGuardrail,
#     connection_id: str | None = None,
# ):
#     request_id = str(uuid.uuid4())
#     log_prefix = f"[{SERVICE_NAME} API][{request_id}]"
#     logger.info(f"{log_prefix} START | connection_id={connection_id}")

#     try:
#         result = await return_ttd_result(input=payload, connection_id=connection_id)
#         logger.info(f"{log_prefix} SUCCESS | rows={result.row_count}")
#         logger.info(f"{log_prefix} SUCCESS | result={result}")
#         return result
#     except Exception as exc:
#         error_msg = f"{SERVICE_NAME} API error: {str(exc)}"
#         logger.error(f"{log_prefix} EXCEPTION: {error_msg}", exc_info=True)
#         return DatabaseTable(
#             database=SERVICE_NAME,
#             table=None,
#             csv_path=None,
#             row_count=None,
#             tool=SERVICE_NAME,
#             message=error_msg
#         )
