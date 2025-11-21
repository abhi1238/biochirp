
import logging
from config.schema import database_schemas, primary_keys_by_db, foreign_keys_by_db
from .graph import shortest_path_table, build_table_graph, concept_table_steiner_coverage_with_columns
from config.guardrail import ParsedValue, PlanGenerator, FuzzyFilteredOutputs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

async def generate_plan(input: FuzzyFilteredOutputs, database: str) -> PlanGenerator:

    database = database

    tool = "planner"

    fo = input.model_dump(exclude_none=True)

    logger.info("[Planner code] Running")

    logger.info(f"[Planner code] Input : {fo}")

    output_columns = [
        k for k, v in fo["value"].items()
        if (v == "requested") or (isinstance(v, list) and len(v) > 0)
    ]

    # db = "ttd"
    # Validate requested columns exist somewhere in the schema
    # db_schema_cols = set()
    # for tbl_cols in database_schemas[database].values():
    #     db_schema_cols.update(tbl_cols)
    # missing_columns = [col for col in output_columns if col not in db_schema_cols]
    # logger.exception(f"Failed to build join plan: {missing_columns}")

    # Build plan (FK-valid order + explicit join key pairs)
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
        plan= dict()
        # return AllDBTableResultsTTD(ttd=f"Planning error: {e}")

    return PlanGenerator(database= database, tool=tool, plan=plan)
        
