

# graph.py

"""
Simple, deterministic Steiner coverage for database query planning.

Design:
1) Strict concept -> unique table mapping
2) Steiner tree over those terminal tables
3) Deterministic BFS order + FK-validated join pairs
"""

from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import logging
import os
import signal
from contextlib import contextmanager

import networkx as nx
from networkx.algorithms.approximation import steiner_tree

logger = logging.getLogger(__name__)

# Configuration
MAX_TABLES_IN_COVERAGE = int(os.getenv("MAX_TABLES_IN_COVERAGE", "20"))
STEINER_TIMEOUT_SECONDS = int(os.getenv("STEINER_TIMEOUT_SECONDS", "300"))


class SteinerTreeError(Exception):
    pass


class TimeoutError(SteinerTreeError):
    pass


class NoConnectedCoverError(SteinerTreeError):
    pass


@contextmanager
def timeout(seconds: int):
    def timeout_handler(signum, frame):
        raise TimeoutError(f"Operation exceeded {seconds}s timeout")

    if not hasattr(signal, "SIGALRM"):
        logger.warning("SIGALRM unavailable; running without timeout protection.")
        yield
        return

    try:
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    except (ValueError, AttributeError):
        logger.warning("SIGALRM unavailable in this runtime context; running without timeout protection.")
        yield
        return

    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def build_table_graph(
    foreign_keys: Dict[str, List[Tuple[str, str, str, str]]],
    db_name: str,
    edge_weights: Optional[Dict[Tuple[str, str], float]] = None,
) -> Tuple[nx.Graph, Dict[Tuple[str, str], List[Tuple[str, str]]]]:
    """
    Returns:
      - undirected schema graph G with edge weight
      - directional fk_lookup[(a,b)] = [(a_col,b_col), ...]
    """
    G = nx.Graph()
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

    for src_table, src_col, tgt_table, tgt_col in sorted(foreign_keys.get(db_name, [])):
        w = 1.0
        if edge_weights:
            w = edge_weights.get((src_table, tgt_table), edge_weights.get((tgt_table, src_table), 1.0))

        if G.has_edge(src_table, tgt_table):
            existing = G[src_table][tgt_table].get("weight", 1.0)
            G[src_table][tgt_table]["weight"] = min(existing, w)
        else:
            G.add_edge(src_table, tgt_table, weight=w)

        fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
        fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

    return G, fk_lookup


def _map_concepts_to_unique_tables(
    schema: Dict[str, List[str]],
    concepts: List[str],
) -> Dict[str, str]:
    concept_to_tables: Dict[str, List[str]] = defaultdict(list)

    for table, cols in sorted(schema.items()):
        col_set = set(cols)
        for concept in concepts:
            if concept in col_set:
                concept_to_tables[concept].append(table)

    missing = sorted(c for c in concepts if not concept_to_tables[c])
    if missing:
        raise ValueError(f"Concept(s) not found in any table: {missing}. Please verify column names.")

    result = {}
    for concept in concepts:
        tables = concept_to_tables[concept]
        if len(tables) == 1:
            result[concept] = tables[0]
        else:
            # Resolve ambiguity:
            #   - For *_name columns (user-supplied filter values), prefer association/
            #     relationship tables so a disease_name-only query hits the chem-disease
            #     join (CTD chemical_disease_association, TTD P1-05-Drug_disease,
            #     DrugCentral drug_disease_association) instead of the disease master.
            #   - For *_id columns, prefer master tables as the canonical FK source.
            master_tables = sorted(t for t in tables if t.endswith("_master_table"))
            non_master = sorted(t for t in tables if not t.endswith("_master_table"))
            assoc_tables = [t for t in non_master if "_association" in t]

            if concept.endswith("_name") and (assoc_tables or non_master):
                result[concept] = assoc_tables[0] if assoc_tables else non_master[0]
            elif master_tables:
                result[concept] = master_tables[0]
            else:
                result[concept] = sorted(tables)[0]

            logger.warning(
                "Ambiguous concept '%s' maps to %s; resolved to '%s'",
                concept, sorted(tables), result[concept],
            )

    return result


def _pick_single_fk_pair(
    fk_cols: List[Tuple[str, str]],
    par: str,
    child: str,
) -> Tuple[str, str]:
    unique_pairs = sorted(set(fk_cols))
    if len(unique_pairs) > 1:
        logger.warning(
            "Multiple FK candidates for %s -> %s: %s. Selecting deterministic pair %s.",
            par, child, unique_pairs, unique_pairs[0]
        )
    return unique_pairs[0]




def _build_bfs_order_and_parent(
    steiner_g: nx.Graph,
    root: str,
) -> Tuple[List[str], Dict[str, Optional[str]]]:
    order = [root] + [v for _, v in nx.bfs_edges(steiner_g, root, sort_neighbors=sorted)]
    parent: Dict[str, Optional[str]] = {root: None}
    parent.update(
        {child: par for child, par in nx.bfs_predecessors(steiner_g, root, sort_neighbors=sorted)}
    )
    return order, parent


def _validate_and_build_join_pairs(
    order: List[str],
    parent: Dict[str, Optional[str]],
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
    schema: Dict[str, List[str]],
    db_name: str,
) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
    join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

    for child in order:
        par = parent.get(child)
        if par is None:
            continue

        fk_cols = fk_lookup.get((par, child), [])
        if not fk_cols:
            raise ValueError(
                f"No foreign key found between parent '{par}' and child '{child}'. "
                "Database schema may be incomplete."
            )

        par_col, child_col = _pick_single_fk_pair(fk_cols, par, child)

        par_cols = set(schema.get(par, []))
        child_cols = set(schema.get(child, []))

        if par_col not in par_cols:
            raise ValueError(f"FK column '{par_col}' not found in table '{par}'. Available: {sorted(par_cols)}")
        if child_col not in child_cols:
            raise ValueError(f"FK column '{child_col}' not found in table '{child}'. Available: {sorted(child_cols)}")

        fq_par = f"{db_name}.{par}"
        fq_child = f"{db_name}.{child}"
        join_pairs[(fq_par, fq_child)] = {
            "left_on": [par_col],
            "right_on": [child_col],
        }

    return join_pairs


def _build_table_columns(
    order: List[str],
    parent: Dict[str, Optional[str]],
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
    concept_to_table: Dict[str, str],
    db_name: str,
) -> Dict[str, Dict[str, List[str]]]:
    table_columns: Dict[str, Dict[str, List[str]]] = {}

    for table in order:
        fq_table = f"{db_name}.{table}"
        concept_cols = sorted(c for c, mapped in concept_to_table.items() if mapped == table)

        join_cols: List[str] = []
        par = parent.get(table)
        if par is not None:
            fk_cols = fk_lookup.get((par, table), [])
            if fk_cols:
                _, child_col = _pick_single_fk_pair(fk_cols, par, table)
                join_cols = [child_col]

        table_columns[fq_table] = {
            "concept_columns": concept_cols,
            "join_columns": join_cols,
        }

    return table_columns


def concept_table_steiner_coverage_with_columns(
    database_schemas_all: Dict[str, Dict[str, List[str]]],
    foreign_keys_all: Dict[str, List[Tuple[str, str, str, str]]],
    db_name: str,
    concepts: List[str],
    edge_weights: Optional[Dict[Tuple[str, str], float]] = None,
) -> Dict[str, Any]:
    if not concepts:
        raise ValueError("concepts list cannot be empty")
    if db_name not in database_schemas_all:
        raise ValueError(
            f"Database '{db_name}' not found in schemas. "
            f"Available: {sorted(database_schemas_all.keys())}"
        )

    schema = database_schemas_all[db_name]
    clean_concepts = sorted(dict.fromkeys(concepts))

    # Step 1: strict concept->table mapping
    concept_to_table = _map_concepts_to_unique_tables(schema, clean_concepts)
    terminal_tables = sorted(set(concept_to_table.values()))

    logger.info("[%s] Concepts=%s, terminals=%s", db_name, clean_concepts, terminal_tables)

    # Single-table shortcut
    if len(terminal_tables) == 1:
        table = terminal_tables[0]
        fq = f"{db_name}.{table}"
        return {
            db_name: {
                "tables": [fq],
                "parents": {fq: None},
                "table_columns": {
                    fq: {
                        "concept_columns": clean_concepts,
                        "join_columns": [],
                    }
                },
                "join_pairs": {},
            }
        }

    # Step 2: graph + Steiner
    G, fk_lookup = build_table_graph(foreign_keys_all, db_name, edge_weights=edge_weights)

    # Ensure terminals exist as nodes even if isolated
    G.add_nodes_from(terminal_tables)

    # Fail fast if terminals are disconnected
    cc = nx.node_connected_component(G, terminal_tables[0])
    if any(t not in cc for t in terminal_tables):
        raise NoConnectedCoverError(
            f"No connected cover found. Terminal tables are in disconnected components: {terminal_tables}"
        )

    with timeout(STEINER_TIMEOUT_SECONDS):
        try:
            st = steiner_tree(G, terminal_tables, weight="weight", method="mehlhorn")
        except nx.NetworkXError as e:
            raise NoConnectedCoverError(
                f"No connected cover found: {e}. Concepts may be in disconnected components."
            ) from e

    steiner_nodes = set(st.nodes())
    if not steiner_nodes or not all(t in steiner_nodes for t in terminal_tables):
        raise NoConnectedCoverError(f"Steiner tree does not span all terminals: {terminal_tables}")
    if not nx.is_connected(st):
        raise NoConnectedCoverError(f"Steiner subgraph is disconnected for terminals: {terminal_tables}")

    if len(steiner_nodes) > MAX_TABLES_IN_COVERAGE:
        logger.warning(
            "Solution requires %s tables, exceeds recommended MAX_TABLES_IN_COVERAGE=%s.",
            len(steiner_nodes), MAX_TABLES_IN_COVERAGE
        )

    # Step 3: deterministic BFS order
    root = terminal_tables[0]
    order, parent = _build_bfs_order_and_parent(st, root)

    if set(order) != steiner_nodes:
        raise NoConnectedCoverError("Failed to build BFS order that covers all Steiner nodes.")

    join_pairs = _validate_and_build_join_pairs(order, parent, fk_lookup, schema, db_name)
    table_columns = _build_table_columns(order, parent, fk_lookup, concept_to_table, db_name)

    fq_order = [f"{db_name}.{t}" for t in order]
    fq_parent = {
        f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}")
        for k, v in parent.items()
    }

    return {
        db_name: {
            "tables": fq_order,
            "parents": fq_parent,
            "table_columns": table_columns,
            "join_pairs": join_pairs,
        }
    }
