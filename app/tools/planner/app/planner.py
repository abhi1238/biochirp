# planner.py

import logging
from config.schema import database_schemas, primary_keys_by_db, foreign_keys_by_db
from .graph import concept_table_steiner_coverage_with_columns
from config.guardrail import PlanGenerator, FuzzyFilteredOutputs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")


async def generate_plan(input: FuzzyFilteredOutputs, database: str) -> PlanGenerator:
    tool = "planner"

    fo = input.model_dump(exclude_none=True)
    logger.info("[Planner code] Running")
    logger.info(f"[Planner code] Input : {fo}")

    output_columns = [
        k for k, v in fo["value"].items()
        if (v == "requested") or (isinstance(v, list) and len(v) > 0)
    ]

    # OPTIONAL: If you have precomputed per-table row-count stats, pass them here.
    # Format: { "table_name": estimated_rows_int }
    # Example: table_row_estimates = table_row_estimates_by_db[database]
    table_row_estimates = None

    try:
        plan = concept_table_steiner_coverage_with_columns(
            {database: database_schemas[database]},        # current DB only
            {database: foreign_keys_by_db[database]},      # current DB FKs
            database,
            output_columns,
            table_row_estimates=table_row_estimates,       # <-- new hook
        )
        plan = plan[database]
    except Exception as e:
        logger.exception("Failed to build join plan")
        plan = dict()

    return PlanGenerator(database=database, tool=tool, plan=plan)
