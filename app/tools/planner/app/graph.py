# # graph.py

# """
# Production-grade Steiner tree coverage for database query planning.

# Updates (high-value, low-risk):
# 1) Cardinality-aware greedy Steiner:
#    - When coverage ties, prefer tables with lower estimated row counts.
#    - Row-count estimates are optional, passed as `table_row_estimates`.
# """

# from typing import Any, Dict, List, Optional, Tuple, Set
# from collections import defaultdict, deque
# import itertools
# import logging
# import os
# import signal
# from contextlib import contextmanager

# logger = logging.getLogger(__name__)

# # Configuration
# MAX_COMBINATIONS = int(os.getenv("MAX_COMBINATIONS", "10000"))
# MAX_TABLES_IN_COVERAGE = int(os.getenv("MAX_TABLES_IN_COVERAGE", "20"))
# STEINER_TIMEOUT_SECONDS = int(os.getenv("STEINER_TIMEOUT_SECONDS", "300"))
# USE_GREEDY_ALGORITHM = os.getenv("USE_GREEDY_ALGORITHM", "true").lower() == "true"


# class SteinerTreeError(Exception):
#     pass


# class TimeoutError(SteinerTreeError):
#     pass


# class NoConnectedCoverError(SteinerTreeError):
#     pass


# @contextmanager
# def timeout(seconds: int):
#     def timeout_handler(signum, frame):
#         raise TimeoutError(f"Operation exceeded {seconds}s timeout")

#     # SIGALRM can be unavailable on some platforms; signal.signal can also
#     # fail outside the main thread. In those cases run without timeout guard.
#     if not hasattr(signal, "SIGALRM"):
#         logger.warning("SIGALRM unavailable; running without timeout protection.")
#         yield
#         return

#     try:
#         old_handler = signal.signal(signal.SIGALRM, timeout_handler)
#     except (ValueError, AttributeError):
#         logger.warning("SIGALRM unavailable in this runtime context; running without timeout protection.")
#         yield
#         return

#     signal.alarm(seconds)
#     try:
#         yield
#     finally:
#         signal.alarm(0)
#         signal.signal(signal.SIGALRM, old_handler)


# def build_table_graph(
#     foreign_keys: Dict[str, List[Tuple[str, str, str, str]]],
#     db_name: str
# ) -> Tuple[Dict[str, Set[str]], Dict[Tuple[str, str], List[Tuple[str, str]]]]:
#     edges: Dict[str, Set[str]] = defaultdict(set)
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

#     if db_name not in foreign_keys:
#         return edges, fk_lookup

#     for src_table, src_col, tgt_table, tgt_col in foreign_keys[db_name]:
#         edges[src_table].add(tgt_table)
#         edges[tgt_table].add(src_table)

#         fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
#         fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

#     return edges, fk_lookup


# def shortest_path_table(graph: Dict[str, Set[str]], start: str, end: str) -> Optional[List[str]]:
#     if start == end:
#         return [start]

#     visited = {start}
#     queue = deque([(start, [start])])

#     while queue:
#         current, path = queue.popleft()
#         for neighbor in graph[current]:
#             if neighbor == end:
#                 return path + [end]
#             if neighbor not in visited:
#                 visited.add(neighbor)
#                 queue.append((neighbor, path + [neighbor]))
#     return None


# class PathCache:
#     def __init__(self, graph: Dict[str, Set[str]]):
#         self.graph = graph
#         self.cache: Dict[Tuple[str, str], Optional[List[str]]] = {}

#     def get_path(self, t1: str, t2: str) -> Optional[List[str]]:
#         key = tuple(sorted([t1, t2]))
#         if key not in self.cache:
#             self.cache[key] = shortest_path_table(self.graph, t1, t2)
#         return self.cache[key]

#     def clear(self):
#         self.cache.clear()


# def find_greedy_steiner_tree(
#     graph: Dict[str, Set[str]],
#     concept_to_tables: Dict[str, List[str]],
#     path_cache: PathCache,
#     table_row_estimates: Optional[Dict[str, int]] = None,
# ) -> Tuple[Set[str], List[str], List[List[str]]]:
#     """
#     Greedy approximation for Steiner tree.

#     Update:
#     - Coverage-first (maximize #concepts covered)
#     - Tie-breaker: prefer smaller estimated row count if provided
#     """
#     logger.info("Using greedy Steiner tree algorithm")

#     # Count how many concepts each table covers
#     table_concept_count = defaultdict(int)
#     for concept, tables in concept_to_tables.items():
#         for table in tables:
#             table_concept_count[table] += 1

#     def score_table(t: str) -> Tuple[int, int]:
#         # primary: maximize concept coverage
#         cover = table_concept_count[t]
#         # secondary: minimize estimated rows (smaller is better)
#         if table_row_estimates is None:
#             # no stats -> neutral tie-break
#             return (cover, 0)
#         rows = table_row_estimates.get(t)
#         # unknown rows treated as very large (penalize)
#         if rows is None:
#             rows = 10**18
#         # since we use max(), invert sign so smaller rows => larger score
#         return (cover, -int(rows))

#     selected_tables: List[str] = []
#     covered_concepts = set()

#     for concept, tables in concept_to_tables.items():
#         if concept in covered_concepts:
#             continue

#         best_table = max(tables, key=score_table)
#         selected_tables.append(best_table)

#         for c, ts in concept_to_tables.items():
#             if best_table in ts:
#                 covered_concepts.add(c)

#     logger.info(f"Greedy selected {len(selected_tables)} tables for {len(concept_to_tables)} concepts")

#     if len(selected_tables) == 1:
#         return set(selected_tables), selected_tables, []

#     all_tables = set(selected_tables)
#     paths: List[List[str]] = []

#     connected = {selected_tables[0]}
#     remaining = set(selected_tables[1:])

#     while remaining:
#         best_path = None
#         best_length = float("inf")
#         best_to = None

#         for conn_table in connected:
#             for rem_table in remaining:
#                 path = path_cache.get_path(conn_table, rem_table)
#                 if path and len(path) < best_length:
#                     best_path = path
#                     best_length = len(path)
#                     best_to = rem_table

#         if best_path is None:
#             logger.warning(f"No path found from {connected} to {remaining}")
#             return None, None, None  # type: ignore

#         paths.append(best_path)
#         all_tables.update(best_path)
#         connected.add(best_to)
#         remaining.remove(best_to)

#     return all_tables, selected_tables, paths


# def find_exhaustive_steiner_tree(
#     graph: Dict[str, Set[str]],
#     concepts: List[str],
#     concept_to_tables: Dict[str, List[str]],
#     path_cache: PathCache,
#     max_combinations: int
# ) -> Tuple[Set[str], List[str], List[List[str]]]:
#     logger.info(f"Using exhaustive search (max {max_combinations} combinations)")

#     column_choices = [concept_to_tables[c] for c in concepts]

#     total_combos = 1
#     for choices in column_choices:
#         total_combos *= len(choices)

#     logger.info(f"Total possible combinations: {total_combos}")

#     if total_combos > max_combinations:
#         logger.warning(
#             f"Total combinations ({total_combos}) exceeds limit ({max_combinations}). "
#             f"Will try first {max_combinations} only."
#         )

#     min_tables_set: Optional[Set[str]] = None
#     best_combo: Optional[List[str]] = None
#     best_paths: Optional[List[List[str]]] = None

#     combinations_tried = 0

#     for combo_tuple in itertools.product(*column_choices):
#         if combinations_tried >= max_combinations:
#             logger.warning(f"Reached combination limit of {max_combinations}")
#             break

#         combinations_tried += 1
#         combo = list(combo_tuple)
#         unique_tables = list(set(combo))

#         covered_concepts = set()
#         for i, table in enumerate(combo):
#             concept = concepts[i]
#             if table in concept_to_tables[concept]:
#                 covered_concepts.add(concept)

#         if len(covered_concepts) != len(concepts):
#             continue

#         if len(unique_tables) == 1:
#             min_tables_set = set(unique_tables)
#             best_combo = unique_tables
#             best_paths = []
#             logger.info("Found single-table solution")
#             break

#         tables_in_paths = set(unique_tables)
#         paths: List[List[str]] = []
#         all_connected = True

#         for t1, t2 in itertools.combinations(unique_tables, 2):
#             path = path_cache.get_path(t1, t2)
#             if path is None:
#                 all_connected = False
#                 break
#             tables_in_paths.update(path)
#             paths.append(path)

#         if not all_connected:
#             continue

#         if (min_tables_set is None) or (len(tables_in_paths) < len(min_tables_set)):
#             min_tables_set = tables_in_paths
#             best_combo = unique_tables
#             best_paths = paths

#     logger.info(f"Tried {combinations_tried} combinations")
#     return min_tables_set, best_combo, best_paths  # type: ignore


# def build_spanning_tree(
#     min_tables_set: Set[str],
#     best_combo: List[str],
#     best_paths: List[List[str]]
# ) -> Tuple[List[str], Dict[str, Optional[str]], Dict[str, Set[str]]]:
#     adj: Dict[str, Set[str]] = defaultdict(set)

#     if best_paths:
#         for path in best_paths:
#             for a, b in zip(path, path[1:]):
#                 if a in min_tables_set and b in min_tables_set:
#                     adj[a].add(b)
#                     adj[b].add(a)

#     root = best_combo[0]
#     order: List[str] = []
#     parent: Dict[str, Optional[str]] = {root: None}
#     seen = {root}
#     queue = deque([root])

#     while queue:
#         t = queue.popleft()
#         order.append(t)
#         for nb in sorted(adj[t]):
#             if nb not in seen:
#                 seen.add(nb)
#                 parent[nb] = t
#                 queue.append(nb)

#     logger.info(f"Built spanning tree with {len(order)} tables, root: {root}")
#     return order, parent, adj


# def _pick_single_fk_pair(
#     fk_cols: List[Tuple[str, str]],
#     par: str,
#     child: str,
# ) -> Tuple[str, str]:
#     """
#     Pick exactly one FK pair for an edge.

#     If multiple candidate pairs exist between the same table pair, combining all
#     of them into a single composite join can be incorrect (alternative FKs).
#     Use a deterministic single-pair choice instead.
#     """
#     unique_pairs = sorted(set(fk_cols))
#     if len(unique_pairs) > 1:
#         logger.warning(
#             "Multiple FK candidates for %s -> %s: %s. Selecting deterministic pair %s.",
#             par,
#             child,
#             unique_pairs,
#             unique_pairs[0],
#         )
#     return unique_pairs[0]


# def validate_and_build_join_pairs(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     schema: Dict[str, List[str]],
#     db_name: str
# ) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
#     join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

#     for child in order:
#         par = parent.get(child)
#         if par is None:
#             continue

#         fk_cols = fk_lookup.get((par, child), [])
#         if not fk_cols:
#             raise ValueError(
#                 f"No foreign key found between parent '{par}' and child '{child}'. "
#                 f"Database schema may be incomplete or tables are not actually connected."
#             )

#         par_col, child_col = _pick_single_fk_pair(fk_cols, par, child)
#         par_cols_in_schema = set(schema.get(par, []))
#         child_cols_in_schema = set(schema.get(child, []))

#         if par_col not in par_cols_in_schema:
#             raise ValueError(
#                 f"FK column '{par_col}' not found in table '{par}'. "
#                 f"Available columns: {sorted(par_cols_in_schema)}"
#             )
#         if child_col not in child_cols_in_schema:
#             raise ValueError(
#                 f"FK column '{child_col}' not found in table '{child}'. "
#                 f"Available columns: {sorted(child_cols_in_schema)}"
#             )

#         fq_par = f"{db_name}.{par}"
#         fq_child = f"{db_name}.{child}"

#         join_pairs[(fq_par, fq_child)] = {
#             "left_on": [par_col],
#             "right_on": [child_col],
#         }

#     return join_pairs


# def build_table_columns(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     concepts: List[str],
#     concept_to_tables: Dict[str, List[str]],
#     db_name: str
# ) -> Dict[str, Dict[str, List[str]]]:
#     table_columns: Dict[str, Dict[str, List[str]]] = {}

#     for t in order:
#         fq_t = f"{db_name}.{t}"
#         concept_cols = [c for c in concepts if t in concept_to_tables[c]]

#         join_cols: List[str] = []
#         if parent.get(t) is not None:
#             par = parent[t]
#             fk_cols = fk_lookup.get((par, t), [])
#             if fk_cols:
#                 _, child_col = _pick_single_fk_pair(fk_cols, par, t)
#                 join_cols = [child_col]

#         table_columns[fq_t] = {
#             "concept_columns": concept_cols,
#             "join_columns": join_cols
#         }

#     return table_columns


# def concept_table_steiner_coverage_with_columns(
#     database_schemas_all: Dict[str, Dict[str, List[str]]],
#     foreign_keys_all: Dict[str, List[Tuple[str, str, str, str]]],
#     db_name: str,
#     concepts: List[str],
#     table_row_estimates: Optional[Dict[str, int]] = None,  # <-- new hook
# ) -> Dict[str, Any]:
#     if not concepts:
#         raise ValueError("concepts list cannot be empty")

#     if db_name not in database_schemas_all:
#         raise ValueError(
#             f"Database '{db_name}' not found in schemas. "
#             f"Available: {list(database_schemas_all.keys())}"
#         )

#     logger.info(f"Finding Steiner tree coverage for {len(concepts)} concepts in {db_name}")

#     schema = database_schemas_all[db_name]
#     table_graph, fk_lookup = build_table_graph(foreign_keys_all, db_name)

#     # Map each concept to tables containing it
#     concept_to_tables: Dict[str, List[str]] = defaultdict(list)
#     for table, columns in schema.items():
#         for concept in concepts:
#             if concept in columns:
#                 concept_to_tables[concept].append(table)

#     missing = [c for c in concepts if not concept_to_tables[c]]
#     if missing:
#         raise ValueError(
#             f"Concept(s) not found in any table: {missing}. "
#             f"Please verify column names are correct."
#         )

#     # Single-table cover fast path
#     all_tables = set()
#     for tables in concept_to_tables.values():
#         all_tables.update(tables)

#     if len(all_tables) == 1:
#         single_table = list(all_tables)[0]
#         fq_table = f"{db_name}.{single_table}"
#         return {
#             db_name: {
#                 "tables": [fq_table],
#                 "parents": {fq_table: None},
#                 "table_columns": {
#                     fq_table: {"concept_columns": concepts, "join_columns": []}
#                 },
#                 "join_pairs": {}
#             }
#         }

#     try:
#         with timeout(STEINER_TIMEOUT_SECONDS):
#             path_cache = PathCache(table_graph)

#             if USE_GREEDY_ALGORITHM:
#                 min_tables_set, best_combo, best_paths = find_greedy_steiner_tree(
#                     table_graph,
#                     concept_to_tables,
#                     path_cache,
#                     table_row_estimates=table_row_estimates,
#                 )
#             else:
#                 min_tables_set, best_combo, best_paths = find_exhaustive_steiner_tree(
#                     table_graph, concepts, concept_to_tables, path_cache, MAX_COMBINATIONS
#                 )

#             if min_tables_set is None or best_combo is None:
#                 raise NoConnectedCoverError(
#                     "No connected cover found. Concepts may be in disconnected "
#                     "components of the database schema."
#                 )

#             if len(min_tables_set) > MAX_TABLES_IN_COVERAGE:
#                 logger.warning(
#                     f"Solution requires {len(min_tables_set)} tables, which exceeds "
#                     f"recommended maximum of {MAX_TABLES_IN_COVERAGE}. Query may be slow."
#                 )

#     except TimeoutError:
#         logger.error(f"Steiner tree search exceeded {STEINER_TIMEOUT_SECONDS}s timeout")
#         raise

#     order, parent, _adj = build_spanning_tree(min_tables_set, best_combo, best_paths)
#     join_pairs = validate_and_build_join_pairs(order, parent, fk_lookup, schema, db_name)
#     table_columns = build_table_columns(order, parent, fk_lookup, concepts, concept_to_tables, db_name)

#     fq_order = [f"{db_name}.{t}" for t in order]
#     fq_parent = {f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}") for k, v in parent.items()}

#     return {
#         db_name: {
#             "tables": fq_order,
#             "parents": fq_parent,
#             "table_columns": table_columns,
#             "join_pairs": join_pairs,
#         }
#     }



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

    ambiguous = {
        c: sorted(set(ts))
        for c, ts in concept_to_tables.items()
        if len(set(ts)) > 1
    }
    if ambiguous:
        raise ValueError(
            "Ambiguous concept->table mapping detected. "
            "Each terminal concept must map to exactly one table. "
            f"Ambiguous: {ambiguous}"
        )

    return {c: concept_to_tables[c][0] for c in concepts}


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
    table_row_estimates: Optional[Dict[str, int]] = None,  # kept for compatibility
    edge_weights: Optional[Dict[Tuple[str, str], float]] = None,
) -> Dict[str, Any]:
    if not concepts:
        raise ValueError("concepts list cannot be empty")
    if db_name not in database_schemas_all:
        raise ValueError(
            f"Database '{db_name}' not found in schemas. "
            f"Available: {sorted(database_schemas_all.keys())}"
        )

    # Not used in this simplified planner, kept to avoid breaking callers.
    _ = table_row_estimates

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
