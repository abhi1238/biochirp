

"""
Production-grade Steiner tree coverage for database query planning.

This module finds minimal connected sets of tables covering requested concept columns,
with proper memory management, caching, and timeout protection.
"""

from typing import Any, Dict, List, Literal, Optional, Tuple, Union, Set
from collections import defaultdict, deque
from functools import lru_cache
import itertools
import logging
import os
import signal
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Configuration
MAX_COMBINATIONS = int(os.getenv("MAX_COMBINATIONS", "10000"))
MAX_TABLES_IN_COVERAGE = int(os.getenv("MAX_TABLES_IN_COVERAGE", "20"))
STEINER_TIMEOUT_SECONDS = int(os.getenv("STEINER_TIMEOUT_SECONDS", "300"))
USE_GREEDY_ALGORITHM = os.getenv("USE_GREEDY_ALGORITHM", "true").lower() == "true"


class SteinerTreeError(Exception):
    """Base exception for Steiner tree operations."""
    pass


class TimeoutError(SteinerTreeError):
    """Operation exceeded time limit."""
    pass


class NoConnectedCoverError(SteinerTreeError):
    """No connected cover found for concepts."""
    pass


@contextmanager
def timeout(seconds: int):
    """
    Context manager for operation timeout.
    
    Args:
        seconds: Maximum execution time
        
    Raises:
        TimeoutError: If operation exceeds time limit
    """
    def timeout_handler(signum, frame):
        raise TimeoutError(f"Operation exceeded {seconds}s timeout")
    
    # Set up signal handler
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def build_table_graph(
    foreign_keys: Dict[str, List[Tuple[str, str, str, str]]],
    db_name: str
) -> Tuple[Dict[str, Set[str]], Dict[Tuple[str, str], List[Tuple[str, str]]]]:
    """
    Build undirected adjacency graph and FK column lookup.

    Args:
        foreign_keys: Dict mapping db_name to list of FK tuples (src_table, src_col, tgt_table, tgt_col)
        db_name: Target database name

    Returns:
        Tuple of:
          - edges: Undirected adjacency (table -> set of neighbor tables)
          - fk_lookup: Dict mapping (tableA, tableB) to list of (colA, colB) pairs
    """
    edges: Dict[str, Set[str]] = defaultdict(set)
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)

    if db_name not in foreign_keys:
        return edges, fk_lookup

    for src_table, src_col, tgt_table, tgt_col in foreign_keys[db_name]:
        # Undirected edges
        edges[src_table].add(tgt_table)
        edges[tgt_table].add(src_table)

        # Bidirectional FK lookup
        fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
        fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

    return edges, fk_lookup


def shortest_path_table(
    graph: Dict[str, Set[str]],
    start: str,
    end: str
) -> Optional[List[str]]:
    """
    Find shortest path between two tables in the graph using BFS.

    Args:
        graph: Adjacency dictionary (table -> set of neighbors)
        start: Start table
        end: End table

    Returns:
        List of tables forming the path, or None if no path exists
    """
    if start == end:
        return [start]

    visited = {start}
    queue = deque([(start, [start])])

    while queue:
        current, path = queue.popleft()
        for neighbor in graph[current]:
            if neighbor == end:
                return path + [end]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return None


class PathCache:
    """Cache for shortest paths to avoid recalculation."""
    
    def __init__(self, graph: Dict[str, Set[str]]):
        self.graph = graph
        self.cache: Dict[Tuple[str, str], Optional[List[str]]] = {}
    
    def get_path(self, t1: str, t2: str) -> Optional[List[str]]:
        """Get cached shortest path between two tables."""
        # Normalize key (undirected)
        key = tuple(sorted([t1, t2]))
        
        if key not in self.cache:
            self.cache[key] = shortest_path_table(self.graph, t1, t2)
            logger.debug(f"Cached path {t1} -> {t2}: {self.cache[key]}")
        
        return self.cache[key]
    
    def clear(self):
        """Clear the cache."""
        self.cache.clear()


def find_greedy_steiner_tree(
    graph: Dict[str, Set[str]],
    concept_to_tables: Dict[str, List[str]],
    path_cache: PathCache
) -> Tuple[Set[str], List[str], List[List[str]]]:
    """
    Greedy approximation for Steiner tree problem.
    
    Strategy: Choose tables that cover the most concepts first, then connect them
    with minimal additional tables.
    
    Args:
        graph: Table adjacency graph
        concept_to_tables: Mapping of concepts to tables containing them
        path_cache: Path cache for efficiency
        
    Returns:
        Tuple of (all_tables, concept_tables, connecting_paths)
    """
    logger.info("Using greedy Steiner tree algorithm")
    
    # Count how many concepts each table covers
    table_concept_count = defaultdict(int)
    for concept, tables in concept_to_tables.items():
        for table in tables:
            table_concept_count[table] += 1
    
    # Select one table per concept (prefer tables covering most concepts)
    selected_tables = []
    covered_concepts = set()
    
    for concept, tables in concept_to_tables.items():
        if concept in covered_concepts:
            continue
        
        # Choose table with highest concept count
        best_table = max(tables, key=lambda t: table_concept_count[t])
        selected_tables.append(best_table)
        
        # Mark all concepts this table covers
        for c, ts in concept_to_tables.items():
            if best_table in ts:
                covered_concepts.add(c)
    
    logger.info(f"Greedy selected {len(selected_tables)} tables for {len(concept_to_tables)} concepts")
    
    # If only one table, return early
    if len(selected_tables) == 1:
        return set(selected_tables), selected_tables, []
    
    # Find paths connecting selected tables (MST-style)
    all_tables = set(selected_tables)
    paths = []
    
    # Start with first table, add closest tables iteratively
    connected = {selected_tables[0]}
    remaining = set(selected_tables[1:])
    
    while remaining:
        best_path = None
        best_length = float('inf')
        best_from = None
        best_to = None
        
        # Find shortest path from any connected table to any remaining table
        for conn_table in connected:
            for rem_table in remaining:
                path = path_cache.get_path(conn_table, rem_table)
                if path and len(path) < best_length:
                    best_path = path
                    best_length = len(path)
                    best_from = conn_table
                    best_to = rem_table
        
        if best_path is None:
            # No path found - disconnected graph
            logger.warning(f"No path found from {connected} to {remaining}")
            return None, None, None
        
        # Add this path
        paths.append(best_path)
        all_tables.update(best_path)
        connected.add(best_to)
        remaining.remove(best_to)
        
        logger.debug(f"Connected {best_from} -> {best_to}, path length: {best_length}")
    
    return all_tables, selected_tables, paths


def find_exhaustive_steiner_tree(
    graph: Dict[str, Set[str]],
    concepts: List[str],
    concept_to_tables: Dict[str, List[str]],
    path_cache: PathCache,
    max_combinations: int
) -> Tuple[Set[str], List[str], List[List[str]]]:
    """
    Exhaustive search for optimal Steiner tree (tries multiple combinations).
    
    Args:
        graph: Table adjacency graph
        concepts: List of concept columns
        concept_to_tables: Mapping of concepts to tables
        path_cache: Path cache for efficiency
        max_combinations: Maximum combinations to try
        
    Returns:
        Tuple of (all_tables, concept_tables, connecting_paths)
    """
    logger.info(f"Using exhaustive search (max {max_combinations} combinations)")
    
    column_choices = [concept_to_tables[c] for c in concepts]
    
    # Estimate total combinations
    total_combos = 1
    for choices in column_choices:
        total_combos *= len(choices)
    
    logger.info(f"Total possible combinations: {total_combos}")
    
    if total_combos > max_combinations:
        logger.warning(
            f"Total combinations ({total_combos}) exceeds limit ({max_combinations}). "
            f"Will try first {max_combinations} only."
        )
    
    min_tables_set: Optional[Set[str]] = None
    best_combo: Optional[List[str]] = None
    best_paths: Optional[List[List[str]]] = None
    
    combinations_tried = 0
    
    for combo_tuple in itertools.product(*column_choices):
        if combinations_tried >= max_combinations:
            logger.warning(f"Reached combination limit of {max_combinations}")
            break
        
        combinations_tried += 1
        
        # Deduplicate while preserving concept coverage tracking
        combo = list(combo_tuple)
        unique_tables = list(set(combo))
        
        # Verify all concepts are covered by unique tables
        covered_concepts = set()
        for i, table in enumerate(combo):
            concept = concepts[i]
            if table in concept_to_tables[concept]:
                covered_concepts.add(concept)
        
        if len(covered_concepts) != len(concepts):
            # This combination doesn't cover all concepts
            continue
        
        if len(unique_tables) == 1:
            # All concepts in same table - optimal!
            min_tables_set = set(unique_tables)
            best_combo = unique_tables
            best_paths = []
            logger.info("Found single-table solution")
            break
        
        # Find paths connecting all tables in combo
        tables_in_paths = set(unique_tables)
        paths: List[List[str]] = []
        all_connected = True
        
        for t1, t2 in itertools.combinations(unique_tables, 2):
            path = path_cache.get_path(t1, t2)
            if path is None:
                # No path exists - this combo won't work
                all_connected = False
                break
            tables_in_paths.update(path)
            paths.append(path)
        
        if not all_connected:
            continue
        
        # Check if this is best so far
        if (min_tables_set is None) or (len(tables_in_paths) < len(min_tables_set)):
            min_tables_set = tables_in_paths
            best_combo = unique_tables
            best_paths = paths
            logger.debug(
                f"New best: {len(tables_in_paths)} tables for "
                f"{len(unique_tables)} concept tables"
            )
    
    logger.info(f"Tried {combinations_tried} combinations")
    
    return min_tables_set, best_combo, best_paths


def build_spanning_tree(
    min_tables_set: Set[str],
    best_combo: List[str],
    best_paths: List[List[str]]
) -> Tuple[List[str], Dict[str, Optional[str]], Dict[str, Set[str]]]:
    """
    Build spanning tree from selected tables and paths.
    
    Args:
        min_tables_set: All tables in the cover
        best_combo: Concept-containing tables
        best_paths: Paths connecting them
        
    Returns:
        Tuple of (join_order, parent_map, adjacency)
    """
    # Build adjacency restricted to chosen tables
    adj: Dict[str, Set[str]] = defaultdict(set)
    
    if best_paths:
        for path in best_paths:
            for a, b in zip(path, path[1:]):
                if a in min_tables_set and b in min_tables_set:
                    adj[a].add(b)
                    adj[b].add(a)
    
    # Build spanning tree via BFS (root = first concept table)
    root = best_combo[0]
    order: List[str] = []
    parent: Dict[str, Optional[str]] = {root: None}
    seen = {root}
    queue = deque([root])
    
    while queue:
        t = queue.popleft()
        order.append(t)
        
        # Sort neighbors for deterministic ordering
        for nb in sorted(adj[t]):
            if nb not in seen:
                seen.add(nb)
                parent[nb] = t
                queue.append(nb)
    
    logger.info(f"Built spanning tree with {len(order)} tables, root: {root}")
    
    return order, parent, adj


def validate_and_build_join_pairs(
    order: List[str],
    parent: Dict[str, Optional[str]],
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
    schema: Dict[str, List[str]],
    db_name: str
) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
    """
    Build join pairs from spanning tree with validation.
    
    Args:
        order: Join order (list of tables)
        parent: Parent map from spanning tree
        fk_lookup: FK column lookup
        schema: Database schema
        db_name: Database name
        
    Returns:
        Join pairs dictionary
        
    Raises:
        ValueError: If FK columns missing or invalid
    """
    join_pairs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    
    for child in order:
        par = parent.get(child)
        if par is None:
            continue  # Root has no parent
        
        # Get FK columns for this parent-child edge
        fk_cols = fk_lookup.get((par, child), [])
        
        if not fk_cols:
            raise ValueError(
                f"No foreign key found between parent '{par}' and child '{child}'. "
                f"Database schema may be incomplete or tables are not actually connected."
            )
        
        # Validate that columns exist in tables
        par_cols_in_schema = set(schema.get(par, []))
        child_cols_in_schema = set(schema.get(child, []))
        
        for par_col, child_col in fk_cols:
            if par_col not in par_cols_in_schema:
                raise ValueError(
                    f"FK column '{par_col}' not found in table '{par}'. "
                    f"Available columns: {sorted(par_cols_in_schema)}"
                )
            if child_col not in child_cols_in_schema:
                raise ValueError(
                    f"FK column '{child_col}' not found in table '{child}'. "
                    f"Available columns: {sorted(child_cols_in_schema)}"
                )
        
        fq_par = f"{db_name}.{par}"
        fq_child = f"{db_name}.{child}"
        
        join_pairs[(fq_par, fq_child)] = {
            "left_on": [col[0] for col in fk_cols],
            "right_on": [col[1] for col in fk_cols],
        }
        
        logger.debug(
            f"Join pair: {fq_par} -> {fq_child} on "
            f"{join_pairs[(fq_par, fq_child)]}"
        )
    
    return join_pairs


def build_table_columns(
    order: List[str],
    parent: Dict[str, Optional[str]],
    fk_lookup: Dict[Tuple[str, str], List[Tuple[str, str]]],
    concepts: List[str],
    concept_to_tables: Dict[str, List[str]],
    db_name: str
) -> Dict[str, Dict[str, List[str]]]:
    """
    Build table_columns metadata for all tables in join plan.
    
    Args:
        order: Join order
        parent: Parent map
        fk_lookup: FK lookup
        concepts: Concept columns
        concept_to_tables: Concept to table mapping
        db_name: Database name
        
    Returns:
        Table columns metadata
    """
    table_columns: Dict[str, Dict[str, List[str]]] = {}
    
    for t in order:
        fq_t = f"{db_name}.{t}"
        
        # Concept columns this table contains
        concept_cols = [c for c in concepts if t in concept_to_tables[c]]
        
        # Join columns (FK columns used to connect to parent)
        join_cols: List[str] = []
        if parent.get(t) is not None:
            # Child table - get child side of FK
            par = parent[t]
            fk_cols = fk_lookup.get((par, t), [])
            join_cols = [col[1] for col in fk_cols]  # child columns
        
        table_columns[fq_t] = {
            "concept_columns": concept_cols,
            "join_columns": join_cols
        }
    
    return table_columns


def concept_table_steiner_coverage_with_columns(
    database_schemas_all: Dict[str, Dict[str, List[str]]],
    foreign_keys_all: Dict[str, List[Tuple[str, str, str, str]]],
    db_name: str,
    concepts: List[str],
) -> Dict[str, Any]:
    """
    Find minimal connected set of tables covering all requested concept columns.

    This implements a Steiner tree approximation:
      1. Find tables containing each concept
      2. Find minimal set of tables connecting all concepts (including bridge tables)
      3. Build spanning tree with explicit join conditions
      4. Return FK-valid join order

    Args:
        database_schemas_all: Dict mapping db_name to schema (table -> columns)
        foreign_keys_all: Dict mapping db_name to FK list
        db_name: Target database name
        concepts: List of column names to cover

    Returns:
        Dict with structure:
        {
            db_name: {
                "tables": [...],           # Join order (root-first BFS)
                "parents": {...},          # table -> parent in tree (None for root)
                "table_columns": {...},    # table -> {"concept_columns": [...], "join_columns": [...]}
                "join_pairs": {...}        # (tableA, tableB) -> {"left_on": [...], "right_on": [...]}
            }
        }

    Raises:
        ValueError: If concepts not found or no connected cover exists
        TimeoutError: If operation exceeds timeout
        NoConnectedCoverError: If no connected solution exists
    """
    # Validate inputs
    if not concepts:
        raise ValueError("concepts list cannot be empty")

    if db_name not in database_schemas_all:
        raise ValueError(
            f"Database '{db_name}' not found in schemas. "
            f"Available: {list(database_schemas_all.keys())}"
        )

    logger.info(f"Finding Steiner tree coverage for {len(concepts)} concepts in {db_name}")
    
    schema = database_schemas_all[db_name]
    
    # Build graph and lookups
    table_graph, fk_lookup = build_table_graph(foreign_keys_all, db_name)
    
    logger.info(f"Graph has {len(table_graph)} tables with {sum(len(v) for v in table_graph.values())//2} edges")

    # Map each concept to tables containing it
    concept_to_tables: Dict[str, List[str]] = defaultdict(list)
    for table, columns in schema.items():
        for concept in concepts:
            if concept in columns:
                concept_to_tables[concept].append(table)

    # Check for missing concepts
    missing = [c for c in concepts if not concept_to_tables[c]]
    if missing:
        raise ValueError(
            f"Concept(s) not found in any table: {missing}. "
            f"Please verify column names are correct."
        )

    # Log concept distribution
    for concept, tables in concept_to_tables.items():
        logger.debug(f"Concept '{concept}' found in {len(tables)} table(s): {tables}")

    # Handle single-table case (all concepts in one table)
    all_tables = set()
    for tables in concept_to_tables.values():
        all_tables.update(tables)

    if len(all_tables) == 1:
        # All concepts in single table - no joins needed!
        single_table = list(all_tables)[0]
        fq_table = f"{db_name}.{single_table}"
        
        logger.info(f"All concepts found in single table: {single_table}")

        return {
            db_name: {
                "tables": [fq_table],
                "parents": {fq_table: None},
                "table_columns": {
                    fq_table: {
                        "concept_columns": concepts,
                        "join_columns": []
                    }
                },
                "join_pairs": {}
            }
        }

    # Multi-table case - find Steiner tree with timeout protection
    try:
        with timeout(STEINER_TIMEOUT_SECONDS):
            # Initialize path cache
            path_cache = PathCache(table_graph)
            
            # Choose algorithm based on configuration
            if USE_GREEDY_ALGORITHM:
                min_tables_set, best_combo, best_paths = find_greedy_steiner_tree(
                    table_graph, concept_to_tables, path_cache
                )
            else:
                min_tables_set, best_combo, best_paths = find_exhaustive_steiner_tree(
                    table_graph, concepts, concept_to_tables, path_cache, MAX_COMBINATIONS
                )
            
            # Check if solution found
            if min_tables_set is None or best_combo is None:
                raise NoConnectedCoverError(
                    "No connected cover found. Concepts may be in disconnected "
                    "components of the database schema."
                )
            
            # Validate solution size
            if len(min_tables_set) > MAX_TABLES_IN_COVERAGE:
                logger.warning(
                    f"Solution requires {len(min_tables_set)} tables, which exceeds "
                    f"recommended maximum of {MAX_TABLES_IN_COVERAGE}. "
                    f"Query may be slow."
                )
            
            logger.info(
                f"Found cover with {len(min_tables_set)} total tables "
                f"({len(best_combo)} concept tables, "
                f"{len(min_tables_set) - len(best_combo)} bridge tables)"
            )
            
    except TimeoutError:
        logger.error(f"Steiner tree search exceeded {STEINER_TIMEOUT_SECONDS}s timeout")
        raise
    
    # Build spanning tree
    order, parent, adj = build_spanning_tree(min_tables_set, best_combo, best_paths)
    
    # Build join pairs with validation
    join_pairs = validate_and_build_join_pairs(
        order, parent, fk_lookup, schema, db_name
    )
    
    # Build table column metadata
    table_columns = build_table_columns(
        order, parent, fk_lookup, concepts, concept_to_tables, db_name
    )
    
    # Build fully-qualified names
    fq_order = [f"{db_name}.{t}" for t in order]
    fq_parent = {
        f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}")
        for k, v in parent.items()
    }
    
    logger.info(
        f"Successfully built query plan: "
        f"{len(fq_order)} tables, {len(join_pairs)} joins"
    )

    return {
        db_name: {
            "tables": fq_order,
            "parents": fq_parent,
            "table_columns": table_columns,
            "join_pairs": join_pairs,
        }
    }