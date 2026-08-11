
import os
import logging

def setup_logger(name: str) -> logging.Logger:
    """
    Uvicorn-safe logger.
    - Reuses uvicorn.error handlers
    - No duplicate handlers
    - Honors AXIS_LOG_LEVEL
    """

    level_name = os.getenv("AXIS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Reuse uvicorn's error logger (has handlers + formatting)
    base_logger = logging.getLogger("uvicorn.error")

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # IMPORTANT: propagate so logs go through uvicorn handlers
    logger.propagate = True

    # Do NOT add handlers
    # Do NOT touch root logger

    return logger
