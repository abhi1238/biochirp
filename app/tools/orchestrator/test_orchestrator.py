"""Local, Docker-free structure test for the orchestrator tool wrappers.

Verifies the generic_tool base + each *_tool.py build the right backend URL
(host/port from env, DB-agnostic ?database=) and that the package wiring /
relative imports resolve. Does NOT make HTTP calls.

Run:  python app/tools/orchestrator/test_orchestrator.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PKG_PARENT = Path(__file__).resolve().parent          # app/tools/orchestrator
sys.path.insert(0, str(PKG_PARENT))                    # so `app` == orchestrator pkg


def main() -> int:
    # Set backend hosts/ports the way docker-compose would.
    os.environ["SCHEMA_PLANNER_HOST"] = "biochirp_schema_planner_tool"
    os.environ["SCHEMA_PLANNER_PORT"] = "8020"
    os.environ["SCHEMA_MAPPER_HOST"] = "biochirp_schema_mapper_tool"
    os.environ["SCHEMA_MAPPER_PORT"] = "8019"

    from app.planner_tool import PlannerTool
    from app.schema_mapper_tool import SchemaMapperTool
    from app.router_tool import RouterTool

    p = PlannerTool().url("hcdt")
    m = SchemaMapperTool().url("ctd")
    print("planner   :", p)
    print("mapper    :", m)
    print("router    :", RouterTool().model)

    assert p == "http://biochirp_schema_planner_tool:8020/schema_planner?database=hcdt", p
    assert m == "http://biochirp_schema_mapper_tool:8019/schema_mapper?database=ctd", m

    # default host/port fallback when env unset
    os.environ.pop("SCHEMA_PLANNER_HOST", None)
    os.environ.pop("SCHEMA_PLANNER_PORT", None)
    d = PlannerTool().url("hcdt")
    print("planner(default):", d)
    assert d == "http://localhost:8020/schema_planner?database=hcdt", d

    print("\nPASS ✓  orchestrator wrappers build correct backend URLs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
