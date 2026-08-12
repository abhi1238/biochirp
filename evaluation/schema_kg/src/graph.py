"""
SchemaGraph — build a node-link graph from the three input JSON files.

Node types  : DatabaseNode, TableNode, ColumnNode
Edge types  : belongs_to, fk, concept_bridge

Only queryable columns enter the retrieval index; join keys participate only
as FK-edge endpoints and as BFS neighbours during neighbourhood aggregation.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ─── Node dataclasses ────────────────────────────────────────────────────────

@dataclass
class ColumnNode:
    col_id: str          # e.g. "hcdt.drug_master_table.drug_name"
    db: str
    table: str
    column: str
    description: str
    queryable: bool
    concept_type: Optional[str] = None


@dataclass
class TableNode:
    table_id: str        # e.g. "hcdt.drug_master_table"
    db: str
    table: str


@dataclass
class DatabaseNode:
    db: str


# ─── SchemaGraph ─────────────────────────────────────────────────────────────

class SchemaGraph:
    """
    Holds three tiers of nodes (DB / Table / Column) and three edge types
    (belongs_to / fk / concept_bridge).

    adjacency[node_id] = list of (neighbour_id, edge_type)
    """

    def __init__(self) -> None:
        self.db_nodes:     Dict[str, DatabaseNode] = {}
        self.table_nodes:  Dict[str, TableNode]    = {}
        self.col_nodes:    Dict[str, ColumnNode]   = {}
        # undirected adjacency
        self.adjacency:    Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    # ── accessors ────────────────────────────────────────────────────────────

    @property
    def queryable_columns(self) -> List[ColumnNode]:
        return [c for c in self.col_nodes.values() if c.queryable]

    def bfs_neighbours(self, col_id: str, depth: int) -> List[Tuple[str, str]]:
        """Return all (neighbour_id, edge_type) within `depth` hops of col_id."""
        visited: Set[str] = {col_id}
        frontier = [col_id]
        result: List[Tuple[str, str]] = []
        for _ in range(depth):
            next_frontier = []
            for node in frontier:
                for nbr, etype in self.adjacency[node]:
                    if nbr not in visited:
                        visited.add(nbr)
                        next_frontier.append(nbr)
                        result.append((nbr, etype))
            frontier = next_frontier
        return result

    def node_description(self, node_id: str) -> str:
        """Return the text used to embed this node."""
        if node_id in self.col_nodes:
            c = self.col_nodes[node_id]
            return f"{c.column} in {c.table} ({c.db}): {c.description}"
        if node_id in self.table_nodes:
            t = self.table_nodes[node_id]
            cols = [c.description for c in self.col_nodes.values()
                    if c.db == t.db and c.table == t.table]
            return f"{t.table} in {t.db}: " + " | ".join(cols)
        if node_id in self.db_nodes:
            db = self.db_nodes[node_id]
            tables = sorted({t.table for t in self.table_nodes.values()
                             if t.db == db.db})
            return f"{db.db}: " + " | ".join(tables)
        raise KeyError(node_id)

    # ── private helpers ───────────────────────────────────────────────────────

    def _add_edge(self, a: str, b: str, etype: str) -> None:
        self.adjacency[a].append((b, etype))
        self.adjacency[b].append((a, etype))


# ─── build_graph ─────────────────────────────────────────────────────────────

def build_graph(
    schema_path:       Path,
    queryable_path:    Path,
    concept_type_path: Path,
) -> SchemaGraph:
    """
    Build a SchemaGraph from the three input JSON files.

    Steps
    -----
    1. Add DB / Table / Column nodes from schema.json.
    2. Add belongs_to edges (col→table, table→DB).
    3. Add FK edges: non-queryable columns sharing the same name within one DB.
    4. Add concept_bridge edges: queryable columns sharing concept_type across DBs.
    """
    schema       = json.loads(schema_path.read_text())
    queryable    = json.loads(queryable_path.read_text())
    concept_type = json.loads(concept_type_path.read_text())

    # strip comment keys
    queryable    = {k: v for k, v in queryable.items()    if not k.startswith("_")}
    concept_type = {k: v for k, v in concept_type.items() if not k.startswith("_")}

    g = SchemaGraph()

    # ── Step 1: add nodes ────────────────────────────────────────────────────
    for db, tables in schema.items():
        if db.startswith("_"):       # skip comment/metadata keys
            continue
        if db not in g.db_nodes:
            g.db_nodes[db] = DatabaseNode(db=db)

        for table, columns in tables.items():
            if table.startswith("_"):   # skip _disabled_tables / _comment blocks
                continue
            if not isinstance(columns, dict):
                continue
            table_id = f"{db}.{table}"
            if table_id not in g.table_nodes:
                g.table_nodes[table_id] = TableNode(
                    table_id=table_id, db=db, table=table
                )

            for col, desc in columns.items():
                if col.startswith("_"):     # skip inline comment keys
                    continue
                if not isinstance(desc, str):
                    logger.warning("schema.json: %s.%s.%s has non-string description (%s) — skipped",
                                   db, table, col, type(desc).__name__)
                    continue
                col_id = f"{db}.{table}.{col}"
                is_queryable = queryable.get(col_id, False)
                ctype        = concept_type.get(col_id)
                g.col_nodes[col_id] = ColumnNode(
                    col_id=col_id, db=db, table=table, column=col,
                    description=desc, queryable=is_queryable,
                    concept_type=ctype,
                )

    # ── Step 2: belongs_to edges ─────────────────────────────────────────────
    for col_id, col in g.col_nodes.items():
        table_id = f"{col.db}.{col.table}"
        g._add_edge(col_id, table_id, "belongs_to")
    for table_id, tbl in g.table_nodes.items():
        g._add_edge(table_id, tbl.db, "belongs_to")

    # ── Step 3: FK edges (intra-DB, non-queryable pairs) ────────────────────
    # group non-queryable columns by (db, column_name)
    fk_groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for col_id, col in g.col_nodes.items():
        if not col.queryable:
            fk_groups[(col.db, col.column)].append(col_id)

    for (db, col_name), col_ids in fk_groups.items():
        if len(col_ids) >= 2:
            for i, a in enumerate(col_ids):
                for b in col_ids[i + 1:]:
                    g._add_edge(a, b, "fk")
            logger.debug("FK group %s.%s → %d columns", db, col_name, len(col_ids))

    # ── Step 4: concept_bridge edges (cross-DB, queryable pairs) ────────────
    ctype_groups: Dict[str, List[str]] = defaultdict(list)
    for col_id, col in g.col_nodes.items():
        if col.queryable and col.concept_type:
            ctype_groups[col.concept_type].append(col_id)

    bridges = 0
    for ctype, col_ids in ctype_groups.items():
        for i, a in enumerate(col_ids):
            for b in col_ids[i + 1:]:
                if g.col_nodes[a].db != g.col_nodes[b].db:
                    g._add_edge(a, b, "concept_bridge")
                    bridges += 1

    logger.info(
        "Graph built: %d DBs, %d tables, %d columns (%d queryable), "
        "%d FK groups, %d concept_bridge edges",
        len(g.db_nodes), len(g.table_nodes), len(g.col_nodes),
        len(g.queryable_columns), len(fk_groups), bridges,
    )
    return g
