# ctd_tool.py
from agents import function_tool
from config.guardrail import QueryInterpreterOutputGuardrail, DatabaseTable
import requests
import uuid
import logging
import os
import sys

SERVICE_NAME = "ctd"

# log = logging.getLogger("uvicorn.error")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

log = logging.getLogger("uvicorn.error")
# log.setLevel(logging.INFO)  # Or logging.DEBUG for more details

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# log.addHandler(stdout_handler)

# # Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)


# f"http://biochirp_ctd_tool:8016/{tool}"
API_URL = f"http://biochirp_{SERVICE_NAME}_tool:8016/{SERVICE_NAME}"
TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))

@function_tool(
    name_override=SERVICE_NAME,
    description_override=f"Query {SERVICE_NAME.upper()}  returns preview + full CSV path via WS."
)
async def ctd(
    input: QueryInterpreterOutputGuardrail,
    connection_id: str | None = None,
) -> DatabaseTable:
    rid = str(uuid.uuid4())
    log_prefix = f"[{SERVICE_NAME} tool][{rid}]"
    log.info(f"{log_prefix} Calling API conn={connection_id}")

    # Build URL with connection_id if provided
    url = API_URL
    if connection_id:
        url = f"{API_URL}?connection_id={connection_id}"

    try:
        r = requests.post(
            url,
            json=input.model_dump(),
            timeout=TIMEOUT
        )
        r.raise_for_status()
        log.info(f"{log_prefix} Finished Successfully")
        return DatabaseTable(**r.json())
    except Exception as e:
        error_msg = f"{SERVICE_NAME.upper()} error: {str(e)}"
        log.error(f"{log_prefix} Error: {error_msg}", exc_info=True)

        return DatabaseTable(
            database=SERVICE_NAME,
            table=None,  # <-- 50-row preview for UI if success
            csv_path=None,  # <-- full file path if success
            row_count=None,
            tool=SERVICE_NAME,
            message=error_msg,
        )