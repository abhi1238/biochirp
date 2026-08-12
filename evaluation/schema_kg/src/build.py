"""
Entry point: build the Schema KG for any BioChirp database and index it in Qdrant.

Usage (from repo root):
    python -m schema_kg.src.build --db hcdt
    python -m schema_kg.src.build --db ttd
    python -m schema_kg.src.build --inputs schema_kg/inputs/hcdt
    python -m schema_kg.src.build --host bioc_qdrant --port 6333
    python -m schema_kg.src.build --dry-run   # graph + embeddings only, no Qdrant upsert

After a successful build, verify with:
    python -m schema_kg.src.build --db hcdt --query "which genes does imatinib target"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# allow `python -m schema_kg.src.build` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from schema_kg.src.graph  import build_graph
from schema_kg.src.embed  import compute_embeddings
from schema_kg.src.index  import upsert_columns
from schema_kg.src.query  import query_time, format_plan
from schema_kg.src.config import QDRANT_HOST, QDRANT_PORT, QDRANT_COLLECTION, collection_for_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("schema_kg.build")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build Schema KG for any BioChirp database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m schema_kg.src.build --db hcdt\n"
            "  python -m schema_kg.src.build --db ttd --dry-run\n"
            "  python -m schema_kg.src.build --inputs schema_kg/inputs/string\n"
        ),
    )
    p.add_argument(
        "--db", default=None,
        help=(
            "Database name (e.g. 'hcdt', 'ttd', 'string'). "
            "Auto-derives --inputs and --collection. "
            "Overridden by explicit --inputs / --collection."
        ),
    )
    p.add_argument(
        "--inputs", default=None,
        help="Directory containing schema.json, queryable.json, concept_type.json "
             "(default: schema_kg/inputs/<db> when --db is given)",
    )
    p.add_argument("--host",       default=QDRANT_HOST)
    p.add_argument("--port",       default=QDRANT_PORT, type=int)
    p.add_argument("--collection", default=None,
                   help="Qdrant collection name (default: schema_kg_<db> when --db is given)")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Build graph + embeddings but skip Qdrant upsert",
    )
    p.add_argument(
        "--graph-only", action="store_true",
        help="Build and print the graph only; skip embedding and Qdrant (fast sanity-check)",
    )
    p.add_argument(
        "--query", default=None,
        help="After building, run this example query and print the retrieval plan",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # ── Resolve inputs and collection from --db shorthand ────────────────────
    db_name = args.db
    if db_name and args.inputs is None:
        args.inputs = f"schema_kg/inputs/{db_name}"
    elif args.inputs is None:
        args.inputs = "schema_kg/inputs/hcdt"
        db_name = db_name or "hcdt"

    if args.collection is None:
        if db_name:
            args.collection = collection_for_db(db_name)
        else:
            # Infer DB name from the inputs path leaf
            db_name = Path(args.inputs).name
            args.collection = collection_for_db(db_name)

    logger.info("DB=%s  inputs=%s  collection=%s", db_name, args.inputs, args.collection)

    inputs = Path(args.inputs)
    schema_path       = inputs / "schema.json"
    queryable_path    = inputs / "queryable.json"
    concept_type_path = inputs / "concept_type.json"

    for p in (schema_path, queryable_path, concept_type_path):
        if not p.exists():
            logger.error("Missing input file: %s", p)
            sys.exit(1)

    # ── 1. Build graph ───────────────────────────────────────────────────────
    logger.info("=== Step 1: Build graph ===")
    graph = build_graph(schema_path, queryable_path, concept_type_path)

    logger.info(
        "Graph summary — DBs: %d  Tables: %d  Columns: %d  Queryable: %d",
        len(graph.db_nodes), len(graph.table_nodes),
        len(graph.col_nodes), len(graph.queryable_columns),
    )

    # print FK groups
    fk_edges = [(a, b) for a, nbrs in graph.adjacency.items()
                 for b, et in nbrs if et == "fk" and a < b]
    logger.info("FK edges: %d", len(fk_edges))

    cb_edges = [(a, b) for a, nbrs in graph.adjacency.items()
                for b, et in nbrs if et == "concept_bridge" and a < b]
    logger.info("Concept-bridge edges: %d (0 for single-DB)", len(cb_edges))

    if args.graph_only:
        logger.info("--graph-only: stopping after graph construction.")
        return

    # ── 2. Compute embeddings ────────────────────────────────────────────────
    logger.info("=== Step 2: Compute SapBERT neighbourhood-aggregated embeddings ===")
    embeddings = compute_embeddings(graph)
    logger.info("Embedding dim: %d  Queryable columns embedded: %d",
                next(iter(embeddings.values())).shape[0], len(embeddings))

    if args.dry_run:
        logger.info("--dry-run: skipping Qdrant upsert")
    else:
        # ── 3. Upsert to Qdrant ──────────────────────────────────────────────
        logger.info("=== Step 3: Upsert to Qdrant [%s:%d / %s] ===",
                    args.host, args.port, args.collection)
        upsert_columns(
            graph=graph, embeddings=embeddings,
            host=args.host, port=args.port, collection=args.collection,
        )
        logger.info("Build complete.")

    # ── 4. Optional demo query ───────────────────────────────────────────────
    if args.query:
        if args.dry_run:
            logger.warning("--dry-run: cannot run query (no Qdrant data). Skipping.")
        else:
            logger.info("=== Demo query: %r ===", args.query)
            plan = query_time(
                user_query=args.query,
                graph=graph,
                host=args.host, port=args.port, collection=args.collection,
            )
            print("\n" + format_plan(plan))


if __name__ == "__main__":
    main()
