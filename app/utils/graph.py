
# from pydantic import BaseModel, Field, constr
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
# from pydantic import BaseModel, Extra, Field, constr, model_validator
from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple, Optional
import itertools


def build_table_graph(foreign_keys: Dict[str, List[Tuple[str, str, str, str]]],
                      db_name: str):
    """
    Build undirected adjacency + lookup of FK column pairs for both directions.

    foreign_keys[db_name] is a list of tuples:
      (src_table, src_col, tgt_table, tgt_col)
    """
    edges = defaultdict(set)        # table -> set(neighbors)
    fk_lookup = defaultdict(list)   # (A, B) -> list of (A.col, B.col)

    for src_table, src_col, tgt_table, tgt_col in foreign_keys[db_name]:
        edges[src_table].add(tgt_table)
        edges[tgt_table].add(src_table)

        fk_lookup[(src_table, tgt_table)].append((src_col, tgt_col))
        fk_lookup[(tgt_table, src_table)].append((tgt_col, src_col))

    return edges, fk_lookup
    
    
    


def shortest_path_table(graph: Dict[str, set], start: str, end: str) -> Optional[List[str]]:
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
    




# -------------------------------
# Steiner-like minimal cover with explicit join plan
# -------------------------------
def concept_table_steiner_coverage_with_columns(
    database_schemas_all: Dict[str, Dict[str, List[str]]],
    foreign_keys_all: Dict[str, List[Tuple[str, str, str, str]]],
    db_name: str,
    concepts: List[str],
):
    """
    Find a minimal connected set of tables that covers all requested concept columns.
    Return a plan that includes:
      - FK-valid join order (root-first BFS)
      - parents map (table -> parent table in the spanning tree; None for root)
      - explicit join_pairs for each tree edge with left_on/right_on
      - concept columns per table (informational)
    """
    schema = database_schemas_all[db_name]
    table_graph, fk_lookup = build_table_graph(foreign_keys_all, db_name)

    # Map each concept column to tables containing it
    concept_to_tables = defaultdict(list)
    for table, columns in schema.items():
        for concept in concepts:
            if concept in columns:
                concept_to_tables[concept].append(table)

    missing = [c for c in concepts if not concept_to_tables[c]]
    if missing:
        raise ValueError(f"Concept(s) not found in schema: {missing}")

    # Choose one table per concept, then connect them with minimal cover via paths
    column_choices = [concept_to_tables[c] for c in concepts]
    min_tables_set = None
    best_combo = None
    best_paths = None

    for combo in itertools.product(*column_choices):
        combo = list(combo)
        tables_in_paths = set(combo)
        paths = []
        for t1, t2 in itertools.combinations(combo, 2):
            path = shortest_path_table(table_graph, t1, t2)
            if path is None:
                break
            tables_in_paths.update(path)
            paths.append(path)
        else:
            if (min_tables_set is None) or (len(tables_in_paths) < len(min_tables_set)):
                min_tables_set = set(tables_in_paths)
                best_combo = combo
                best_paths = paths

    if min_tables_set is None:
        raise ValueError("No connected cover found (even with intermediate tables).")

    # Build adjacency restricted to the chosen set + collect join pairs for edges present in those paths
    adj = defaultdict(set)
    join_pairs = {}  # (fqA, fqB) -> {"left_on": [...], "right_on": [...]}

    for path in best_paths:
        for a, b in zip(path, path[1:]):
            if a in min_tables_set and b in min_tables_set:
                adj[a].add(b)
                adj[b].add(a)
                pairs_ab = fk_lookup.get((a, b), [])
                if pairs_ab:
                    join_pairs[(f"{db_name}.{a}", f"{db_name}.{b}")] = {
                        "left_on":  [p[0] for p in pairs_ab],
                        "right_on": [p[1] for p in pairs_ab],
                    }
                pairs_ba = fk_lookup.get((b, a), [])
                if pairs_ba:
                    join_pairs[(f"{db_name}.{b}", f"{db_name}.{a}")] = {
                        "left_on":  [p[0] for p in pairs_ba],
                        "right_on": [p[1] for p in pairs_ba],
                    }

    # Derive a spanning tree (BFS) rooted at a concept table
    root = best_combo[0]
    order: List[str] = []
    parent: Dict[str, Optional[str]] = {root: None}
    seen = set([root])
    q = deque([root])

    while q:
        t = q.popleft()
        if t in min_tables_set:
            order.append(t)
        for nb in sorted(adj[t]):
            if nb not in seen:
                seen.add(nb)
                parent[nb] = t
                q.append(nb)

    fq_order = [f"{db_name}.{t}" for t in order]
    fq_parent = {f"{db_name}.{k}": (None if v is None else f"{db_name}.{v}") for k, v in parent.items() if k in min_tables_set}

    # Concept columns per table (informational)
    table_columns = {}
    for t in min_tables_set:
        fq_t = f"{db_name}.{t}"
        concept_cols = [c for c in concepts if t in concept_to_tables[c]]
        table_columns[fq_t] = {"concept_columns": concept_cols, "join_columns": []}

    return {
        db_name: {
            "tables": fq_order,              # FK-valid order (root first)
            "parents": fq_parent,            # each table's parent in the tree (None for root)
            "table_columns": table_columns,  # info only
            "join_pairs": join_pairs,        # explicit edge-wise join keys
        }
    }


