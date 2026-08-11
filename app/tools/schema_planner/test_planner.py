"""Local, Docker-free test of the schema_planner deterministic core.

Loads schema_kg_planner.py directly (bypassing the heavy package __init__ so it
runs with only networkx installed), builds the HCDT FK graph, and exercises
assemble_pruned_plan + to_production_plan on a realistic (kept, parsed_value)
fixture — the exact payload the schema_planner HTTP tool receives.

Run:  python app/tools/schema_planner/test_planner.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # repo root
sys.path.insert(0, str(ROOT))                 # so `import schema_kg.src.graph` works


def _load_planner_module():
    spec = importlib.util.spec_from_file_location(
        "skp_under_test", ROOT / "app" / "per_db_tool" / "schema_kg_planner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    skp = _load_planner_module()

    db = "hcdt"
    graph, rules = skp.build_schema_graph(db)
    print(f"[graph] {db}: {len(graph.col_nodes)} columns, "
          f"{len(graph.table_nodes)} tables, rules={'yes' if rules else 'no'}")

    # Realistic value-mapper output: "diseases treated by drugs targeting EGFR"
    parsed_value = {"gene_symbol": ["EGFR"], "disease_name": "requested",
                    "drug_name": "requested"}
    # kept columns the upstream filter would surface (col_id, score)
    kept = [
        (f"{db}.drug_gene_association.gene_symbol", 0.92),
        (f"{db}.drug_disease_association.disease_name", 0.88),
        (f"{db}.drug_master_table.drug_name", 0.85),
    ]

    pruned = skp.assemble_pruned_plan(kept, parsed_value, graph, db,
                                      question="diseases treated by drugs targeting EGFR")
    prod = skp.to_production_plan(pruned, db)

    print(f"[prune] needed_tables = {sorted(pruned['needed_tables'])}")
    print(f"[prune] plan_tables   = {sorted(pruned['plan_tables'])}")
    print(f"[prune] join_path     = {pruned['join_path']}")
    print(f"[prune] filter_plan   = {pruned['filter_plan']}")
    print(f"[prune] output_plan   = {pruned['output_plan']}")
    print(f"[prod]  tables        = {prod['tables']}")
    print(f"[prod]  parents       = {prod['parents']}")
    print(f"[prod]  join_pairs    = {prod['join_pairs']}")

    # Assertions: the deterministic planner must connect the filtered gene to the
    # requested disease/drug via a non-empty join plan.
    assert pruned["plan_tables"], "plan_tables empty — planner failed to connect tables"
    assert prod["tables"], "production plan has no tables"
    assert pruned["filter_plan"], "filter_plan empty — gene filter was dropped"
    print("\nPASS ✓  schema_planner core produced a valid join plan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
