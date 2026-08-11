# planner.py

import logging
from config.schema import database_schemas, foreign_keys_by_db
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

    schema_cols = {c for tbl in database_schemas[database].values() for c in tbl}
    output_columns = [
        k for k, v in fo["value"].items()
        if k in schema_cols and ((v == "requested") or (isinstance(v, list) and len(v) > 0))
    ]

    try:
        plan = concept_table_steiner_coverage_with_columns(
            {database: database_schemas[database]},        # current DB only
            {database: foreign_keys_by_db[database]},      # current DB FKs
            database,
            output_columns,
        )
        plan = plan[database]
    except Exception as e:
        logger.exception("Failed to build join plan")
        plan = dict()

    return PlanGenerator(database=database, tool=tool, plan=plan)
