

# """
# Production-grade Steiner tree coverage for database query planning.

# This module finds minimal connected sets of tables covering requested concept columns,
# with proper memory management, caching, and timeout protection.
# """

# from typing import Any, Dict, List, Literal, Optional, Tuple, Union, Set
# from collections import defaultdict, deque
# from functools import lru_cache
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
#     """Base exception for Steiner tree operations."""
#     pass


# class TimeoutError(SteinerTreeError):
#     """Operation exceeded time limit."""
#     pass


# class NoConnectedCoverError(SteinerTreeError):
#     """No connected cover found for concepts."""
#     pass


# @contextmanager
# def timeout(seconds: int):
#     """
#     Context manager for operation timeout.
    
#     Args:
#         seconds: Maximum execution time
        
#     Raises:
#         TimeoutError: If operation exceeds time limit
#     """
#     def timeout_handler(signum, frame):
#         raise TimeoutError(f"Operation exceeded {seconds}s timeout")
    
#     # Set up signal handler
#     old_handler = signal.signal(signal.SIGALRM, timeout_handler)
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
#     """
#     Build undirected adjacency graph and FK column lookup.

#     Args:
#         foreign_keys: Dict mapping db_name to list of FK tuples (src_table, src_col, tgt_table, tgt_col)
#         db_name: Target database name

#     Returns:
#         Tuple of:
#           - edges: Undirected adjacency (table -> set of neighbor tables)
#           - fk_lookup: Dict mapping (tableA, tableB) to list of (colA, colB) pairs
#     """
#     edges: Dict[str, Set[str]] = defaultdict(set)
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

#     if db_name not in foreign_keys:
#         return edges, fk_lookup

#     for src_table, src_col, tgt_table, tgt_col in foreign_keys[db_name]:
#         # Undirected edges
#         edges[src_table].add(tgt_table)
#         edges[tgt_table].add(src_table)

#         # Bidirectional FK lookup
#         fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
#         fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

#     return edges, fk_lookup


# def shortest_path_table(
#     graph: Dict[str, Set[str]],
#     start: str,
#     end: str
# ) -> Optional[List[str]]:
#     """
#     Find shortest path between two tables in the graph using BFS.

#     Args:
#         graph: Adjacency dictionary (table -> set of neighbors)
#         start: Start table
#         end: End table

#     Returns:
#         List of tables forming the path, or None if no path exists
#     """
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
#     """Cache for shortest paths to avoid recalculation."""
    
#     def __init__(self, graph: Dict[str, Set[str]]):
#         self.graph = graph
#         self.cache: Dict[Tuple[str, str], Optional[List[str]]] = {}
    
#     def get_path(self, t1: str, t2: str) -> Optional[List[str]]:
#         """Get cached shortest path between two tables."""
#         # Normalize key (undirected)
#         key = tuple(sorted([t1, t2]))
        
#         if key not in self.cache:
#             self.cache[key] = shortest_path_table(self.graph, t1, t2)
#             logger.debug(f"Cached path {t1} -> {t2}: {self.cache[key]}")
        
#         return self.cache[key]
    
#     def clear(self):
#         """Clear the cache."""
#         self.cache.clear()


# def find_greedy_steiner_tree(
#     graph: Dict[str, Set[str]],
#     concept_to_tables: Dict[str, List[str]],
#     path_cache: PathCache
# ) -> Tuple[Set[str], List[str], List[List[str]]]:
#     """
#     Greedy approximation for Steiner tree problem.
    
#     Strategy: Choose tables that cover the most concepts first, then connect them
#     with minimal additional tables.
    
#     Args:
#         graph: Table adjacency graph
#         concept_to_tables: Mapping of concepts to tables containing them
#         path_cache: Path cache for efficiency
        
#     Returns:
#         Tuple of (all_tables, concept_tables, connecting_paths)
#     """
#     logger.info("Using greedy Steiner tree algorithm")
    
#     # Count how many concepts each table covers
#     table_concept_count = defaultdict(int)
#     for concept, tables in concept_to_tables.items():
#         for table in tables:
#             table_concept_count[table] += 1
    
#     # Select one table per concept (prefer tables covering most concepts)
#     selected_tables = []
#     covered_concepts = set()
    
#     for concept, tables in concept_to_tables.items():
#         if concept in covered_concepts:
#             continue
        
#         # Choose table with highest concept count
#         best_table = max(tables, key=lambda t: table_concept_count[t])
#         selected_tables.append(best_table)
        
#         # Mark all concepts this table covers
#         for c, ts in concept_to_tables.items():
#             if best_table in ts:
#                 covered_concepts.add(c)
    
#     logger.info(f"Greedy selected {len(selected_tables)} tables for {len(concept_to_tables)} concepts")
    
#     # If only one table, return early
#     if len(selected_tables) == 1:
#         return set(selected_tables), selected_tables, []
    
#     # Find paths connecting selected tables (MST-style)
#     all_tables = set(selected_tables)
#     paths = []
    
#     # Start with first table, add closest tables iteratively
#     connected = {selected_tables[0]}
#     remaining = set(selected_tables[1:])
    
#     while remaining:
#         best_path = None
#         best_length = float('inf')
#         best_from = None
#         best_to = None
        
#         # Find shortest path from any connected table to any remaining table
#         for conn_table in connected:
#             for rem_table in remaining:
#                 path = path_cache.get_path(conn_table, rem_table)
#                 if path and len(path) < best_length:
#                     best_path = path
#                     best_length = len(path)
#                     best_from = conn_table
#                     best_to = rem_table
        
#         if best_path is None:
#             # No path found - disconnected graph
#             logger.warning(f"No path found from {connected} to {remaining}")
#             return None, None, None
        
#         # Add this path
#         paths.append(best_path)
#         all_tables.update(best_path)
#         connected.add(best_to)
#         remaining.remove(best_to)
        
#         logger.debug(f"Connected {best_from} -> {best_to}, path length: {best_length}")
    
#     return all_tables, selected_tables, paths


# def find_exhaustive_steiner_tree(
#     graph: Dict[str, Set[str]],
#     concepts: List[str],
#     concept_to_tables: Dict[str, List[str]],
#     path_cache: PathCache,
#     max_combinations: int
# ) -> Tuple[Set[str], List[str], List[List[str]]]:
#     """
#     Exhaustive search for optimal Steiner tree (tries multiple combinations).
    
#     Args:
#         graph: Table adjacency graph
#         concepts: List of concept columns
#         concept_to_tables: Mapping of concepts to tables
#         path_cache: Path cache for efficiency
#         max_combinations: Maximum combinations to try
        
#     Returns:
#         Tuple of (all_tables, concept_tables, connecting_paths)
#     """
#     logger.info(f"Using exhaustive search (max {max_combinations} combinations)")
    
#     column_choices = [concept_to_tables[c] for c in concepts]
    
#     # Estimate total combinations
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
        
#         # Deduplicate while preserving concept coverage tracking
#         combo = list(combo_tuple)
#         unique_tables = list(set(combo))
        
#         # Verify all concepts are covered by unique tables
#         covered_concepts = set()
#         for i, table in enumerate(combo):
#             concept = concepts[i]
#             if table in concept_to_tables[concept]:
#                 covered_concepts.add(concept)
        
#         if len(covered_concepts) != len(concepts):
#             # This combination doesn't cover all concepts
#             continue
        
#         if len(unique_tables) == 1:
#             # All concepts in same table - optimal!
#             min_tables_set = set(unique_tables)
#             best_combo = unique_tables
#             best_paths = []
#             logger.info("Found single-table solution")
#             break
        
#         # Find paths connecting all tables in combo
#         tables_in_paths = set(unique_tables)
#         paths: List[List[str]] = []
#         all_connected = True
        
#         for t1, t2 in itertools.combinations(unique_tables, 2):
#             path = path_cache.get_path(t1, t2)
#             if path is None:
#                 # No path exists - this combo won't work
#                 all_connected = False
#                 break
#             tables_in_paths.update(path)
#             paths.append(path)
        
#         if not all_connected:
#             continue
        
#         # Check if this is best so far
#         if (min_tables_set is None) or (len(tables_in_paths) < len(min_tables_set)):
#             min_tables_set = tables_in_paths
#             best_combo = unique_tables
#             best_paths = paths
#             logger.debug(
#                 f"New best: {len(tables_in_paths)} tables for "
#                 f"{len(unique_tables)} concept tables"
#             )
    
#     logger.info(f"Tried {combinations_tried} combinations")
    
#     return min_tables_set, best_combo, best_paths


# def build_spanning_tree(
#     min_tables_set: Set[str],
#     best_combo: List[str],
#     best_paths: List[List[str]]
# ) -> Tuple[List[str], Dict[str, Optional[str]], Dict[str, Set[str]]]:
#     """
#     Build spanning tree from selected tables and paths.
    
#     Args:
#         min_tables_set: All tables in the cover
#         best_combo: Concept-containing tables
#         best_paths: Paths connecting them
        
#     Returns:
#         Tuple of (join_order, parent_map, adjacency)
#     """
#     # Build adjacency restricted to chosen tables
#     adj: Dict[str, Set[str]] = defaultdict(set)
    
#     if best_paths:
#         for path in best_paths:
#             for a, b in zip(path, path[1:]):
#                 if a in min_tables_set and b in min_tables_set:
#                     adj[a].add(b)
#                     adj[b].add(a)
    
#     # Build spanning tree via BFS (root = first concept table)
#     root = best_combo[0]
#     order: List[str] = []
#     parent: Dict[str, Optional[str]] = {root: None}
#     seen = {root}
#     queue = deque([root])
    
#     while queue:
#         t = queue.popleft()
#         order.append(t)
        
#         # Sort neighbors for deterministic ordering
#         for nb in sorted(adj[t]):
#             if nb not in seen:
#                 seen.add(nb)
#                 parent[nb] = t
#                 queue.append(nb)
    
#     logger.info(f"Built spanning tree with {len(order)} tables, root: {root}")
    
#     return order, parent, adj


# def validate_and_build_join_pairs(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     schema: Dict[str, List[str]],
#     db_name: str
# ) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
#     """
#     Build join pairs from spanning tree with validation.
    
#     Args:
#         order: Join order (list of tables)
#         parent: Parent map from spanning tree
#         fk_lookup: FK column lookup
#         schema: Database schema
#         db_name: Database name
        
#     Returns:
#         Join pairs dictionary
        
#     Raises:
#         ValueError: If FK columns missing or invalid
#     """
#     join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    
#     for child in order:
#         par = parent.get(child)
#         if par is None:
#             continue  # Root has no parent
        
#         # Get FK columns for this parent-child edge
#         fk_cols = fk_lookup.get((par, child), [])
        
#         if not fk_cols:
#             raise ValueError(
#                 f"No foreign key found between parent '{par}' and child '{child}'. "
#                 f"Database schema may be incomplete or tables are not actually connected."
#             )
        
#         # Validate that columns exist in tables
#         par_cols_in_schema = set(schema.get(par, []))
#         child_cols_in_schema = set(schema.get(child, []))
        
#         for par_col, child_col in fk_cols:
#             if par_col not in par_cols_in_schema:
#                 raise ValueError(
#                     f"FK column '{par_col}' not found in table '{par}'. "
#                     f"Available columns: {sorted(par_cols_in_schema)}"
#                 )
#             if child_col not in child_cols_in_schema:
#                 raise ValueError(
#                     f"FK column '{child_col}' not found in table '{child}'. "
#                     f"Available columns: {sorted(child_cols_in_schema)}"
#                 )
        
#         fq_par = f"{db_name}.{par}"
#         fq_child = f"{db_name}.{child}"
        
#         join_pairs[(fq_par, fq_child)] = {
#             "left_on": [col[0] for col in fk_cols],
#             "right_on": [col[1] for col in fk_cols],
#         }
        
#         logger.debug(
#             f"Join pair: {fq_par} -> {fq_child} on "
#             f"{join_pairs[(fq_par, fq_child)]}"
#         )
    
#     return join_pairs


# def build_table_columns(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     concepts: List[str],
#     concept_to_tables: Dict[str, List[str]],
#     db_name: str
# ) -> Dict[str, Dict[str, List[str]]]:
#     """
#     Build table_columns metadata for all tables in join plan.
    
#     Args:
#         order: Join order
#         parent: Parent map
#         fk_lookup: FK lookup
#         concepts: Concept columns
#         concept_to_tables: Concept to table mapping
#         db_name: Database name
        
#     Returns:
#         Table columns metadata
#     """
#     table_columns: Dict[str, Dict[str, List[str]]] = {}
    
#     for t in order:
#         fq_t = f"{db_name}.{t}"
        
#         # Concept columns this table contains
#         concept_cols = [c for c in concepts if t in concept_to_tables[c]]
        
#         # Join columns (FK columns used to connect to parent)
#         join_cols: List[str] = []
#         if parent.get(t) is not None:
#             # Child table - get child side of FK
#             par = parent[t]
#             fk_cols = fk_lookup.get((par, t), [])
#             join_cols = [col[1] for col in fk_cols]  # child columns
        
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
# ) -> Dict[str, Any]:
#     """
#     Find minimal connected set of tables covering all requested concept columns.

#     This implements a Steiner tree approximation:
#       1. Find tables containing each concept
#       2. Find minimal set of tables connecting all concepts (including bridge tables)
#       3. Build spanning tree with explicit join conditions
#       4. Return FK-valid join order

#     Args:
#         database_schemas_all: Dict mapping db_name to schema (table -> columns)
#         foreign_keys_all: Dict mapping db_name to FK list
#         db_name: Target database name
#         concepts: List of column names to cover

#     Returns:
#         Dict with structure:
#         {
#             db_name: {
#                 "tables": [...],           # Join order (root-first BFS)
#                 "parents": {...},          # table -> parent in tree (None for root)
#                 "table_columns": {...},    # table -> {"concept_columns": [...], "join_columns": [...]}
#                 "join_pairs": {...}        # (tableA, tableB) -> {"left_on": [...], "right_on": [...]}
#             }
#         }

#     Raises:
#         ValueError: If concepts not found or no connected cover exists
#         TimeoutError: If operation exceeds timeout
#         NoConnectedCoverError: If no connected solution exists
#     """
#     # Validate inputs
#     if not concepts:
#         raise ValueError("concepts list cannot be empty")

#     if db_name not in database_schemas_all:
#         raise ValueError(
#             f"Database '{db_name}' not found in schemas. "
#             f"Available: {list(database_schemas_all.keys())}"
#         )

#     logger.info(f"Finding Steiner tree coverage for {len(concepts)} concepts in {db_name}")
    
#     schema = database_schemas_all[db_name]
    
#     # Build graph and lookups
#     table_graph, fk_lookup = build_table_graph(foreign_keys_all, db_name)
    
#     logger.info(f"Graph has {len(table_graph)} tables with {sum(len(v) for v in table_graph.values())//2} edges")

#     # Map each concept to tables containing it
#     concept_to_tables: Dict[str, List[str]] = defaultdict(list)
#     for table, columns in schema.items():
#         for concept in concepts:
#             if concept in columns:
#                 concept_to_tables[concept].append(table)

#     # Check for missing concepts
#     missing = [c for c in concepts if not concept_to_tables[c]]
#     if missing:
#         raise ValueError(
#             f"Concept(s) not found in any table: {missing}. "
#             f"Please verify column names are correct."
#         )

#     # Log concept distribution
#     for concept, tables in concept_to_tables.items():
#         logger.debug(f"Concept '{concept}' found in {len(tables)} table(s): {tables}")

#     # Handle single-table case (all concepts in one table)
#     all_tables = set()
#     for tables in concept_to_tables.values():
#         all_tables.update(tables)

#     if len(all_tables) == 1:
#         # All concepts in single table - no joins needed!
#         single_table = list(all_tables)[0]
#         fq_table = f"{db_name}.{single_table}"
        
#         logger.info(f"All concepts found in single table: {single_table}")

#         return {
#             db_name: {
#                 "tables": [fq_table],
#                 "parents": {fq_table: None},
#                 "table_columns": {
#                     fq_table: {
#                         "concept_columns": concepts,
#                         "join_columns": []
#                     }
#                 },
#                 "join_pairs": {}
#             }
#         }

#     # Multi-table case - find Steiner tree with timeout protection
#     try:
#         with timeout(STEINER_TIMEOUT_SECONDS):
#             # Initialize path cache
#             path_cache = PathCache(table_graph)
            
#             # Choose algorithm based on configuration
#             if USE_GREEDY_ALGORITHM:
#                 min_tables_set, best_combo, best_paths = find_greedy_steiner_tree(
#                     table_graph, concept_to_tables, path_cache
#                 )
#             else:
#                 min_tables_set, best_combo, best_paths = find_exhaustive_steiner_tree(
#                     table_graph, concepts, concept_to_tables, path_cache, MAX_COMBINATIONS
#                 )
            
#             # Check if solution found
#             if min_tables_set is None or best_combo is None:
#                 raise NoConnectedCoverError(
#                     "No connected cover found. Concepts may be in disconnected "
#                     "components of the database schema."
#                 )
            
#             # Validate solution size
#             if len(min_tables_set) > MAX_TABLES_IN_COVERAGE:
#                 logger.warning(
#                     f"Solution requires {len(min_tables_set)} tables, which exceeds "
#                     f"recommended maximum of {MAX_TABLES_IN_COVERAGE}. "
#                     f"Query may be slow."
#                 )
            
#             logger.info(
#                 f"Found cover with {len(min_tables_set)} total tables "
#                 f"({len(best_combo)} concept tables, "
#                 f"{len(min_tables_set) - len(best_combo)} bridge tables)"
#             )
            
#     except TimeoutError:
#         logger.error(f"Steiner tree search exceeded {STEINER_TIMEOUT_SECONDS}s timeout")
#         raise
    
#     # Build spanning tree
#     order, parent, adj = build_spanning_tree(min_tables_set, best_combo, best_paths)
    
#     # Build join pairs with validation
#     join_pairs = validate_and_build_join_pairs(
#         order, parent, fk_lookup, schema, db_name
#     )
    
#     # Build table column metadata
#     table_columns = build_table_columns(
#         order, parent, fk_lookup, concepts, concept_to_tables, db_name
#     )
    
#     # Build fully-qualified names
#     fq_order = [f"{db_name}.{t}" for t in order]
#     fq_parent = {
#         f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}")
#         for k, v in parent.items()
#     }
    
#     logger.info(
#         f"Successfully built query plan: "
#         f"{len(fq_order)} tables, {len(join_pairs)} joins"
#     )

#     return {
#         db_name: {
#             "tables": fq_order,
#             "parents": fq_parent,
#             "table_columns": table_columns,
#             "join_pairs": join_pairs,
#         }
#     }

# vs


# """
# Production-grade Steiner tree coverage for database query planning.

# This module finds minimal connected sets of tables covering requested concept columns,
# using NetworkX's Steiner tree approximation (Mehlhorn algorithm) for optimal connectivity.

# Design decisions:
#   - Table selection: When a concept exists in multiple tables, we evaluate
#     combinations using Steiner tree cost (edge count + bridge penalty),
#     considering graph distance — not just concept frequency.
#   - Table connection: Delegated to NetworkX's steiner_tree (Mehlhorn),
#     which gives a (2 - 2/l) approximation guarantee.
#   - All graph operations use NetworkX standard library (no hand-rolled BFS/MST).
#   - Optional edge weights supported for future cost-based planning
#     (e.g., cardinality, join explosion risk).
# """

# from typing import Any, Dict, List, Optional, Tuple, Set
# from collections import defaultdict
# import itertools
# import logging
# import os
# import signal
# from contextlib import contextmanager

# import networkx as nx
# from networkx.algorithms.approximation import steiner_tree

# logger = logging.getLogger(__name__)

# # Configuration
# MAX_COMBINATIONS = int(os.getenv("MAX_COMBINATIONS", "10000"))
# MAX_TABLES_IN_COVERAGE = int(os.getenv("MAX_TABLES_IN_COVERAGE", "20"))
# STEINER_TIMEOUT_SECONDS = int(os.getenv("STEINER_TIMEOUT_SECONDS", "300"))

# # Scoring weights for terminal selection
# # cost = alpha * num_edges + beta * num_bridge_tables
# ALPHA_EDGE_WEIGHT = float(os.getenv("ALPHA_EDGE_WEIGHT", "1.0"))
# BETA_BRIDGE_PENALTY = float(os.getenv("BETA_BRIDGE_PENALTY", "0.5"))


# class SteinerTreeError(Exception):
#     """Base exception for Steiner tree operations."""
#     pass


# class SteinerTimeoutError(SteinerTreeError):
#     """Operation exceeded time limit."""
#     pass


# class NoConnectedCoverError(SteinerTreeError):
#     """No connected cover found for concepts."""
#     pass


# @contextmanager
# def timeout(seconds: int):
#     """
#     Context manager for operation timeout.

#     Uses SIGALRM on Unix. On Windows or non-main threads, runs without
#     timeout protection and logs a warning.

#     Args:
#         seconds: Maximum execution time

#     Raises:
#         SteinerTimeoutError: If operation exceeds time limit
#     """
#     def timeout_handler(signum, frame):
#         raise SteinerTimeoutError(f"Operation exceeded {seconds}s timeout")

#     has_alarm = hasattr(signal, 'SIGALRM')

#     if not has_alarm:
#         logger.warning(
#             "SIGALRM not available (Windows or non-main thread). "
#             "Running without timeout protection."
#         )
#         yield
#         return

#     old_handler = signal.signal(signal.SIGALRM, timeout_handler)
#     signal.alarm(seconds)
#     try:
#         yield
#     finally:
#         signal.alarm(0)
#         signal.signal(signal.SIGALRM, old_handler)


# def build_table_graph(
#     foreign_keys: Dict[str, List[Tuple[str, str, str, str]]],
#     db_name: str,
#     edge_weights: Optional[Dict[Tuple[str, str], float]] = None,
# ) -> Tuple[nx.Graph, Dict[Tuple[str, str], List[Tuple[str, str]]]]:
#     """
#     Build undirected NetworkX graph and FK column lookup.

#     Args:
#         foreign_keys: Dict mapping db_name to list of FK tuples
#                       (src_table, src_col, tgt_table, tgt_col)
#         db_name: Target database name
#         edge_weights: Optional dict mapping (tableA, tableB) to edge weight.
#                       Defaults to 1.0 for all edges if not provided.

#     Returns:
#         Tuple of:
#           - G: NetworkX undirected graph with 'weight' attribute on edges
#           - fk_lookup: Dict mapping (tableA, tableB) to list of (colA, colB) pairs
#     """
#     G = nx.Graph()
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

#     if db_name not in foreign_keys:
#         return G, fk_lookup

#     for src_table, src_col, tgt_table, tgt_col in foreign_keys[db_name]:
#         # Edge weight: use provided weight, or default to 1.0
#         weight = 1.0
#         if edge_weights:
#             weight = edge_weights.get(
#                 (src_table, tgt_table),
#                 edge_weights.get((tgt_table, src_table), 1.0)
#             )

#         # Add edge (or update if already exists with higher weight)
#         if G.has_edge(src_table, tgt_table):
#             existing = G[src_table][tgt_table].get("weight", 1.0)
#             G[src_table][tgt_table]["weight"] = min(existing, weight)
#         else:
#             G.add_edge(src_table, tgt_table, weight=weight)

#         # Bidirectional FK lookup
#         fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
#         fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

#     return G, fk_lookup


# def _score_steiner_tree(
#     steiner_g: nx.Graph,
#     terminal_set: Set[str],
#     alpha: float = ALPHA_EDGE_WEIGHT,
#     beta: float = BETA_BRIDGE_PENALTY,
# ) -> float:
#     """
#     Score a Steiner tree for terminal selection.

#     Score = alpha * total_edge_weight + beta * num_bridge_tables

#     Lower is better. Bridge tables are non-terminal nodes added to connect
#     terminals — penalizing them discourages solutions that route through
#     many intermediate tables.

#     Args:
#         steiner_g: Steiner tree subgraph
#         terminal_set: Set of terminal node names
#         alpha: Weight for edge cost
#         beta: Penalty per bridge (non-terminal) table

#     Returns:
#         Score (lower is better)
#     """
#     total_edge_weight = sum(
#         d.get("weight", 1.0) for _, _, d in steiner_g.edges(data=True)
#     )
#     bridge_count = len(set(steiner_g.nodes()) - terminal_set)
#     return alpha * total_edge_weight + beta * bridge_count


# def _select_terminal_tables(
#     G: nx.Graph,
#     concept_to_tables: Dict[str, List[str]],
#     max_combinations: int,
# ) -> List[str]:
#     """
#     Select one table per concept, minimizing total Steiner tree cost.

#     For concepts that map to a single table, the choice is fixed.
#     For concepts with multiple candidate tables, we try all combinations
#     (up to max_combinations) and pick the one with the best Steiner score.

#     This solves the Phase 1 limitation of greedy-by-concept-count:
#     table selection now considers graph distance and bridge penalty.

#     Args:
#         G: Table graph
#         concept_to_tables: Mapping of concepts to candidate tables
#         max_combinations: Maximum combinations to evaluate

#     Returns:
#         List of selected terminal tables (deduplicated)

#     Raises:
#         NoConnectedCoverError: If no valid combination connects all terminals
#     """
#     # Sort concepts for deterministic ordering
#     concepts = sorted(concept_to_tables.keys())

#     # Separate fixed (single-table) and variable (multi-table) concepts
#     fixed_tables: Dict[str, str] = {}
#     variable_concepts: List[str] = []

#     for concept in concepts:
#         tables = sorted(concept_to_tables[concept])  # deterministic ordering
#         if len(tables) == 1:
#             fixed_tables[concept] = tables[0]
#         else:
#             variable_concepts.append(concept)

#     # If no variable concepts, selection is trivial
#     if not variable_concepts:
#         return sorted(set(fixed_tables.values()))

#     # Build sorted candidate lists for variable concepts
#     variable_choices = [sorted(concept_to_tables[c]) for c in variable_concepts]

#     total_combos = 1
#     for choices in variable_choices:
#         total_combos *= len(choices)

#     capped = total_combos > max_combinations
#     if capped:
#         logger.warning(
#             f"Total combinations ({total_combos}) exceeds limit ({max_combinations}). "
#             f"Evaluating first {max_combinations} only — result may be suboptimal."
#         )

#     fixed_table_set = set(fixed_tables.values())
#     best_terminals: Optional[List[str]] = None
#     best_score = float("inf")
#     tried = 0

#     for combo in itertools.product(*variable_choices):
#         if tried >= max_combinations:
#             break
#         tried += 1

#         all_terminals_set = fixed_table_set | set(combo)
#         all_terminals = sorted(all_terminals_set)  # deterministic

#         # All terminals must exist in graph
#         if not all(t in G for t in all_terminals):
#             continue

#         if len(all_terminals) == 1:
#             return all_terminals

#         try:
#             st = steiner_tree(G, all_terminals, weight="weight", method="mehlhorn")
#             # Validate: steiner_tree returns empty graph for disconnected inputs
#             if not st.nodes() or not all(t in st.nodes() for t in all_terminals):
#                 continue
#             score = _score_steiner_tree(st, all_terminals_set)
#             if score < best_score:
#                 best_score = score
#                 best_terminals = all_terminals
#         except (nx.NetworkXError, KeyError):
#             continue

#     if best_terminals is None:
#         # No valid combination found — this is a real problem, not a silent fallback
#         all_candidates = fixed_table_set.copy()
#         for c in variable_concepts:
#             all_candidates.update(concept_to_tables[c])

#         raise NoConnectedCoverError(
#             f"No valid terminal combination connects all required tables. "
#             f"Tried {tried} combinations. "
#             f"Candidate tables: {sorted(all_candidates)}. "
#             f"Check that the schema graph is connected."
#         )

#     logger.info(
#         f"Selected {len(best_terminals)} terminal tables from "
#         f"{tried} combinations (score={best_score:.2f})"
#     )
#     return best_terminals


# def _build_join_order_and_parents(
#     steiner_g: nx.Graph,
#     root: str,
# ) -> Tuple[List[str], Dict[str, Optional[str]]]:
#     """
#     Build BFS join order and parent map from Steiner tree subgraph.

#     Uses NetworkX's bfs_edges and bfs_predecessors for correctness.

#     Args:
#         steiner_g: Steiner tree subgraph
#         root: Root table for BFS traversal

#     Returns:
#         Tuple of:
#           - order: BFS join order (root first)
#           - parent: Dict mapping table -> parent table (None for root)
#     """
#     order = [root] + [v for _, v in nx.bfs_edges(steiner_g, root)]

#     parent: Dict[str, Optional[str]] = {root: None}
#     parent.update(
#         {child: par for child, par in nx.bfs_predecessors(steiner_g, root)}
#     )

#     return order, parent


# def _validate_and_build_join_pairs(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     schema: Dict[str, List[str]],
#     db_name: str,
# ) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
#     """
#     Build join pairs from spanning tree with FK validation.

#     Args:
#         order: Join order (list of tables)
#         parent: Parent map from spanning tree
#         fk_lookup: FK column lookup
#         schema: Database schema
#         db_name: Database name

#     Returns:
#         Join pairs dictionary

#     Raises:
#         ValueError: If FK columns missing or invalid
#     """
#     join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

#     for child in order:
#         par = parent.get(child)
#         if par is None:
#             continue

#         fk_cols = fk_lookup.get((par, child), [])

#         if not fk_cols:
#             raise ValueError(
#                 f"No foreign key found between parent '{par}' and child '{child}'. "
#                 f"Database schema may be incomplete."
#             )

#         par_cols_in_schema = set(schema.get(par, []))
#         child_cols_in_schema = set(schema.get(child, []))

#         for par_col, child_col in fk_cols:
#             if par_col not in par_cols_in_schema:
#                 raise ValueError(
#                     f"FK column '{par_col}' not found in table '{par}'. "
#                     f"Available: {sorted(par_cols_in_schema)}"
#                 )
#             if child_col not in child_cols_in_schema:
#                 raise ValueError(
#                     f"FK column '{child_col}' not found in table '{child}'. "
#                     f"Available: {sorted(child_cols_in_schema)}"
#                 )

#         fq_par = f"{db_name}.{par}"
#         fq_child = f"{db_name}.{child}"

#         join_pairs[(fq_par, fq_child)] = {
#             "left_on": [col[0] for col in fk_cols],
#             "right_on": [col[1] for col in fk_cols],
#         }

#     return join_pairs


# def _build_table_columns(
#     order: List[str],
#     parent: Dict[str, Optional[str]],
#     fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
#     concepts: List[str],
#     concept_to_tables: Dict[str, List[str]],
#     db_name: str,
# ) -> Dict[str, Dict[str, List[str]]]:
#     """
#     Build table_columns metadata for all tables in join plan.

#     Args:
#         order: Join order
#         parent: Parent map
#         fk_lookup: FK lookup
#         concepts: Concept columns
#         concept_to_tables: Concept to table mapping
#         db_name: Database name

#     Returns:
#         Table columns metadata
#     """
#     table_columns: Dict[str, Dict[str, List[str]]] = {}

#     for t in order:
#         fq_t = f"{db_name}.{t}"

#         concept_cols = sorted(c for c in concepts if t in concept_to_tables[c])

#         join_cols: List[str] = []
#         par = parent.get(t)
#         if par is not None:
#             fk_cols = fk_lookup.get((par, t), [])
#             join_cols = [col[1] for col in fk_cols]

#         table_columns[fq_t] = {
#             "concept_columns": concept_cols,
#             "join_columns": join_cols,
#         }

#     return table_columns


# def concept_table_steiner_coverage_with_columns(
#     database_schemas_all: Dict[str, Dict[str, List[str]]],
#     foreign_keys_all: Dict[str, List[Tuple[str, str, str, str]]],
#     db_name: str,
#     concepts: List[str],
#     edge_weights: Optional[Dict[Tuple[str, str], float]] = None,
# ) -> Dict[str, Any]:
#     """
#     Find minimal connected set of tables covering all requested concept columns.

#     Algorithm:
#       1. Map each concept to candidate tables
#       2. Select optimal terminal tables (graph-distance-aware, scored by
#          edge cost + bridge penalty)
#       3. Compute Steiner tree connecting all terminals (NetworkX Mehlhorn)
#       4. Build BFS spanning tree with explicit join conditions
#       5. Return FK-valid join order

#     Args:
#         database_schemas_all: Dict mapping db_name to schema (table -> columns)
#         foreign_keys_all: Dict mapping db_name to FK list
#         db_name: Target database name
#         concepts: List of column names to cover
#         edge_weights: Optional dict mapping (tableA, tableB) to edge weight.
#                       Use for cost-based planning (cardinality, join cost, etc.)

#     Returns:
#         Dict with structure::

#             {
#                 db_name: {
#                     "tables": [...],
#                     "parents": {...},
#                     "table_columns": {...},
#                     "join_pairs": {...}
#                 }
#             }

#     Raises:
#         ValueError: If concepts not found or inputs invalid
#         SteinerTimeoutError: If operation exceeds timeout
#         NoConnectedCoverError: If no connected solution exists
#     """
#     if not concepts:
#         raise ValueError("concepts list cannot be empty")

#     if db_name not in database_schemas_all:
#         raise ValueError(
#             f"Database '{db_name}' not found in schemas. "
#             f"Available: {sorted(database_schemas_all.keys())}"
#         )

#     logger.info(f"Finding Steiner tree coverage for {len(concepts)} concepts in {db_name}")

#     schema = database_schemas_all[db_name]

#     # Build graph and FK lookup
#     table_graph, fk_lookup = build_table_graph(foreign_keys_all, db_name, edge_weights)

#     logger.info(
#         f"Graph: {table_graph.number_of_nodes()} tables, "
#         f"{table_graph.number_of_edges()} edges"
#     )

#     # Map each concept to tables containing it (sorted for determinism)
#     concept_to_tables: Dict[str, List[str]] = defaultdict(list)
#     for table in sorted(schema.keys()):
#         for concept in concepts:
#             if concept in schema[table]:
#                 concept_to_tables[concept].append(table)

#     # Ensure concept tables exist as graph nodes (isolated tables with no FK)
#     for tables in concept_to_tables.values():
#         for t in tables:
#             if t not in table_graph:
#                 table_graph.add_node(t)

#     # Check for missing concepts
#     missing = sorted(c for c in concepts if not concept_to_tables[c])
#     if missing:
#         raise ValueError(
#             f"Concept(s) not found in any table: {missing}. "
#             f"Please verify column names are correct."
#         )

#     for concept in sorted(concept_to_tables):
#         tables = concept_to_tables[concept]
#         logger.debug(f"Concept '{concept}' in {len(tables)} table(s): {tables}")

#     # Handle single-table case: any single table covers all concepts
#     for table in sorted(schema.keys()):
#         table_cols = set(schema[table])
#         if all(c in table_cols for c in concepts):
#             fq_table = f"{db_name}.{table}"
#             logger.info(f"All concepts in single table: {table}")
#             return {
#                 db_name: {
#                     "tables": [fq_table],
#                     "parents": {fq_table: None},
#                     "table_columns": {
#                         fq_table: {
#                             "concept_columns": sorted(concepts),
#                             "join_columns": [],
#                         }
#                     },
#                     "join_pairs": {},
#                 }
#             }

#     # Multi-table case — find Steiner tree with timeout protection
#     try:
#         with timeout(STEINER_TIMEOUT_SECONDS):
#             # Step 1: Select terminal tables (graph-distance-aware)
#             terminal_tables = _select_terminal_tables(
#                 table_graph, concept_to_tables, MAX_COMBINATIONS
#             )

#             logger.info(f"Terminal tables: {terminal_tables}")

#             # Step 2: Compute Steiner tree via NetworkX
#             if len(terminal_tables) == 1:
#                 steiner_g = table_graph.subgraph(terminal_tables).copy()
#             else:
#                 try:
#                     steiner_g = steiner_tree(
#                         table_graph, terminal_tables,
#                         weight="weight", method="mehlhorn"
#                     )
#                 except nx.NetworkXError as e:
#                     raise NoConnectedCoverError(
#                         f"No connected cover found: {e}. "
#                         f"Concepts may be in disconnected components."
#                     )

#             steiner_nodes = set(steiner_g.nodes())

#             # Validate: steiner_tree may return empty graph for disconnected inputs
#             if not steiner_nodes or not all(t in steiner_nodes for t in terminal_tables):
#                 raise NoConnectedCoverError(
#                     f"Steiner tree does not span all terminals. "
#                     f"Terminal tables {terminal_tables} may be in disconnected "
#                     f"components of the schema graph."
#                 )

#             if len(steiner_nodes) > MAX_TABLES_IN_COVERAGE:
#                 logger.warning(
#                     f"Solution requires {len(steiner_nodes)} tables, "
#                     f"exceeds recommended maximum of {MAX_TABLES_IN_COVERAGE}."
#                 )

#             terminal_set = set(terminal_tables)
#             bridge_count = len(steiner_nodes - terminal_set)
#             logger.info(
#                 f"Cover: {len(steiner_nodes)} tables "
#                 f"({len(terminal_tables)} terminal, {bridge_count} bridge)"
#             )

#     except SteinerTimeoutError:
#         logger.error(f"Steiner tree search exceeded {STEINER_TIMEOUT_SECONDS}s timeout")
#         raise

#     # Step 3: Build BFS join order (root = first terminal, deterministic)
#     root = terminal_tables[0]
#     order, parent = _build_join_order_and_parents(steiner_g, root)

#     # Step 4: Build join pairs with FK validation
#     join_pairs = _validate_and_build_join_pairs(
#         order, parent, fk_lookup, schema, db_name
#     )

#     # Step 5: Build table column metadata
#     table_columns = _build_table_columns(
#         order, parent, fk_lookup, concepts, concept_to_tables, db_name
#     )

#     # Fully-qualified names
#     fq_order = [f"{db_name}.{t}" for t in order]
#     fq_parent = {
#         f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}")
#         for k, v in parent.items()
#     }

#     logger.info(f"Query plan: {len(fq_order)} tables, {len(join_pairs)} joins")

#     return {
#         db_name: {
#             "tables": fq_order,
#             "parents": fq_parent,
#             "table_columns": table_columns,
#             "join_pairs": join_pairs,
#         }
#     }


"""
Simple, deterministic Steiner coverage for database query planning.

Assumption (enforced):
- Each user-query concept column maps to exactly one table.
- If a concept appears in multiple tables, this is treated as schema ambiguity.
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
    """Base exception for Steiner operations."""
    pass


class SteinerTimeoutError(SteinerTreeError):
    """Operation exceeded time limit."""
    pass


class NoConnectedCoverError(SteinerTreeError):
    """No connected cover found for required concepts."""
    pass


@contextmanager
def timeout(seconds: int):
    """Timeout guard using SIGALRM on Unix; no-op fallback otherwise."""
    def timeout_handler(signum, frame):
        raise SteinerTimeoutError(f"Operation exceeded {seconds}s timeout")

    has_alarm = hasattr(signal, "SIGALRM")
    if not has_alarm:
        logger.warning("SIGALRM unavailable; running without timeout protection.")
        yield
        return

    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
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
    Build undirected schema graph and FK lookup.
    Returns:
      - G: undirected graph with edge "weight"
      - fk_lookup[(a,b)] -> list[(a_col,b_col)] (directional)
    """
    G = nx.Graph()
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

    for src_table, src_col, tgt_table, tgt_col in sorted(foreign_keys.get(db_name, [])):
        w = 1.0
        if edge_weights:
            w = edge_weights.get((src_table, tgt_table), edge_weights.get((tgt_table, src_table), 1.0))

        if G.has_edge(src_table, tgt_table):
            current = G[src_table][tgt_table].get("weight", 1.0)
            G[src_table][tgt_table]["weight"] = min(current, w)
        else:
            G.add_edge(src_table, tgt_table, weight=w)

        fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
        fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

    return G, fk_lookup


def _map_concepts_to_unique_tables(
    schema: Dict[str, List[str]],
    concepts: List[str],
) -> Dict[str, str]:
    """
    Enforce 1-to-1 concept->table mapping.
    Raises ValueError for missing or ambiguous concepts.
    """
    concept_to_tables: Dict[str, List[str]] = defaultdict(list)

    for table, cols in sorted(schema.items()):
        col_set = set(cols)
        for c in concepts:
            if c in col_set:
                concept_to_tables[c].append(table)

    missing = sorted(c for c in concepts if not concept_to_tables[c])
    if missing:
        raise ValueError(
            f"Concept(s) not found in any table: {missing}. Please verify column names."
        )

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


def _build_bfs_order_and_parent(
    steiner_g: nx.Graph,
    root: str,
) -> Tuple[List[str], Dict[str, Optional[str]]]:
    """Deterministic BFS using NetworkX traversal with sorted neighbors."""
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
    """Build FK join pairs and validate columns exist in schema."""
    join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

    for child in order:
        par = parent.get(child)
        if par is None:
            continue

        fk_cols = sorted(fk_lookup.get((par, child), []))
        if not fk_cols:
            raise ValueError(
                f"No foreign key found between parent '{par}' and child '{child}'. "
                "Database schema may be incomplete."
            )

        par_cols = set(schema.get(par, []))
        child_cols = set(schema.get(child, []))

        for par_col, child_col in fk_cols:
            if par_col not in par_cols:
                raise ValueError(
                    f"FK column '{par_col}' not found in table '{par}'. Available: {sorted(par_cols)}"
                )
            if child_col not in child_cols:
                raise ValueError(
                    f"FK column '{child_col}' not found in table '{child}'. Available: {sorted(child_cols)}"
                )

        fq_par = f"{db_name}.{par}"
        fq_child = f"{db_name}.{child}"
        join_pairs[(fq_par, fq_child)] = {
            "left_on": [a for a, _ in fk_cols],
            "right_on": [b for _, b in fk_cols],
        }

    return join_pairs


def _build_table_columns(
    order: List[str],
    parent: Dict[str, Optional[str]],
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
    concept_to_table: Dict[str, str],
    db_name: str,
) -> Dict[str, Dict[str, List[str]]]:
    """Build per-table concept_columns and join_columns metadata."""
    table_columns: Dict[str, Dict[str, List[str]]] = {}

    for t in order:
        fq_t = f"{db_name}.{t}"
        concept_cols = sorted(c for c, mapped in concept_to_table.items() if mapped == t)

        join_cols: List[str] = []
        par = parent.get(t)
        if par is not None:
            # Use NetworkX-selected tree edge direction + FK lookup.
            child_side_cols = [child_col for _, child_col in sorted(fk_lookup.get((par, t), []))]
            # Stable dedup
            join_cols = list(dict.fromkeys(child_side_cols))

        table_columns[fq_t] = {
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
    """
    Simple planner:
      1) Strict concept->table mapping (unique only)
      2) Steiner tree on those terminals
      3) Deterministic BFS order + FK-validated join pairs
    """
    if not concepts:
        raise ValueError("concepts list cannot be empty")
    if db_name not in database_schemas_all:
        raise ValueError(
            f"Database '{db_name}' not found in schemas. "
            f"Available: {sorted(database_schemas_all.keys())}"
        )

    schema = database_schemas_all[db_name]
    clean_concepts = sorted(dict.fromkeys(concepts))

    # Step 1: strict mapping
    concept_to_table = _map_concepts_to_unique_tables(schema, clean_concepts)
    terminal_tables = sorted(set(concept_to_table.values()))

    logger.info("[%s] Concepts=%s, terminals=%s", db_name, clean_concepts, terminal_tables)

    # Single-table shortcut
    if len(terminal_tables) == 1:
        t = terminal_tables[0]
        fq = f"{db_name}.{t}"
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

    # Ensure isolated terminal tables still exist as nodes
    G.add_nodes_from(terminal_tables)

    # Optional early connectivity check on terminal-induced subproblem
    if not nx.is_connected(G.subgraph(nx.node_connected_component(G, terminal_tables[0]) | set())):
        # This line intentionally left simple; full check done below after steiner.
        pass

    with timeout(STEINER_TIMEOUT_SECONDS):
        try:
            st = steiner_tree(G, terminal_tables, weight="weight", method="mehlhorn")
        except nx.NetworkXError as e:
            raise NoConnectedCoverError(
                f"No connected cover found: {e}. Concepts may be in disconnected components."
            ) from e

    steiner_nodes = set(st.nodes())
    if not steiner_nodes or not all(t in steiner_nodes for t in terminal_tables):
        raise NoConnectedCoverError(
            f"Steiner tree does not span all terminals: {terminal_tables}"
        )
    if not nx.is_connected(st):
        raise NoConnectedCoverError(
            f"Steiner subgraph is disconnected for terminals: {terminal_tables}"
        )

    if len(steiner_nodes) > MAX_TABLES_IN_COVERAGE:
        logger.warning(
            "Solution requires %s tables, exceeds recommended MAX_TABLES_IN_COVERAGE=%s.",
            len(steiner_nodes),
            MAX_TABLES_IN_COVERAGE,
        )

    # Step 3: deterministic BFS order from deterministic root
    root = terminal_tables[0]
    order, parent = _build_bfs_order_and_parent(st, root)

    if set(order) != steiner_nodes:
        raise NoConnectedCoverError(
            "Failed to build BFS order that covers all Steiner nodes."
        )

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
