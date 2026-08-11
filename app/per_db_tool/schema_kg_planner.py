"""Shared, DB-agnostic in-process Schema-KG planner.

One `SchemaKgPlanner` instance per database. Each instance loads
`schema_kg/inputs/<db>/` (schema.json, queryable.json, concept_type.json,
schema_rules.json), computes column embeddings with the biochirp-bge model, and
builds an in-memory Qdrant collection ONCE (warm at container boot, or lazily on
first query).

`plan_query_pruned(question)` runs:

  dual-expand (LLM ×2) → ANN (Qdrant in-memory) → dual-filter → dual-value-map
  → build_pruned_subgraph → filter_plan / output_plan

Returns a plan dict, or None when 0 ANN hits (non-biomedical / DB-irrelevant).

This module was generalized out of the HCDT-specific
`app/tools/hcdt/app/schema_kg_planner.py` so any DB with a populated
`schema_kg/inputs/<db>/` directory can use the same in-process pipeline with
zero copied code — only `get_planner("<db>")` differs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

import networkx as nx

logger = logging.getLogger("uvicorn.error")

# Inside Docker: ./schema_kg/ is mounted at /app/schema_kg/.
# On the host:   schema_kg/ sits at <repo_root>/schema_kg/.
_INPUTS_ROOT = Path("/app/schema_kg/inputs")
if not _INPUTS_ROOT.exists():
    # Host fallback: repo_root/schema_kg/inputs (this file lives at
    # <repo_root>/app/per_db_tool/schema_kg_planner.py → parents[2] == repo_root)
    _host_root = Path(__file__).resolve().parents[2] / "evaluation" / "schema_kg" / "inputs"
    if _host_root.exists():
        _INPUTS_ROOT = _host_root


# Non-queryable columns that are FREE-TEXT / descriptive payload, never join
# keys. The FK graph (see _build_pruned_subgraph) treats any non-queryable
# column shared by ≥2 tables as a join edge; without this guard a descriptive
# column present in several master tables forges a BOGUS direct edge. e.g. CTD's
# `synonyms` lives in disease_master, gene_master AND chemical_master, so the
# Steiner tree joined disease_master↔gene_master on `synonyms` (1 hop) instead of
# routing through gene_disease_association (2 hops) — collapsing "genes for breast
# cancer" to a keyless join that returns 0 rows. Real join keys are scalar IDs
# (*_id) and are unaffected. Matched case-insensitively by bare column name.
#
# hgnc_id / uniprot_id / ensembl_id / entrez_id / drug_name are the SAME
# false-edge failure mode, just for xref/name columns instead of free text:
# HCDT's schema.yaml denormalizes gene xrefs (hgnc_id/uniprot_id/ensembl_id/
# entrez_id) into drug_gene_association_hcdt and drug_target_negative_hcdt,
# and denormalizes drug_name into 5 association tables including
# drug_target_negative_hcdt (all exec_schema:false — display-only copies; the
# real FKs are gene_id and drug_id). Because these columns are non-queryable
# and present in ≥2 association tables, the FK-group loop below wrongly forged
# a direct edge between them and silently overwrote the correct gene_id/
# drug_id join key in `full_jk` (last-write-wins) with e.g. "hgnc_id" or
# "drug_name" — which don't survive database_loader.py's gene_master_table
# rename (hgnc_id→hgnc) or aren't unique-per-row, so
# join_and_filter_database's validate_join_columns raised "Join column
# 'hgnc_id'/'drug_name' not found" on EGFR/ALK/FLT3-style gene-targeted
# queries. Excluding them here forces the Steiner tree back onto the real
# gene_id / drug_id edges (via gene_master_table_hcdt / drug_master_table_hcdt).
_NON_FK_DESCRIPTIVE_COLS: frozenset = frozenset({
    "synonyms", "definition", "description", "name", "gene_name", "drug_definition",
    "tree_numbers", "parent_ids", "slim_mappings", "gene_forms",
    "alt_gene_ids", "alt_disease_ids", "alt_drug_ids", "alt_ids",
    "uniprot_ids", "interaction_text", "comment", "comments", "notes",
    "hgnc_id", "uniprot_id", "ensembl_id", "entrez_id", "drug_name",
})

# The FK-group loop below only treats a shared column as a join edge when it is
# NON-queryable ("real join keys are internal-only *_id columns never surfaced
# to users" — see the FK-group comment above). HCDT's schema.yaml marks
# drug_target_negative_hcdt.drug_id queryable:true (deliberately, so users can
# look up a compound by its raw PubChem CID — ~48% of this table's drug_id
# values are orphan CIDs absent from drug_master_table_hcdt), but drug_id is
# STILL the real FK into drug_master_table_hcdt for the ~52% that DO resolve.
# Because `not col_node.queryable` excludes it, drug_id silently drops out of
# fk_col_to_tbls, so drug_master_table_hcdt <-> drug_target_negative_hcdt has NO
# direct edge. The Steiner tree then routes through drug_gene_association_hcdt
# instead — but that hop only shares gene_id with drug_target_negative_hcdt, so
# the join drops the drug identity constraint entirely: "gilteritinib IC50"
# expands to gilteritinib's ~128 confirmed target genes (via
# drug_gene_association) and then pulls IC50 rows for ANY drug tested against
# those genes — wrong data (IC50 values attributed to gilteritinib that belong
# to unrelated compounds) and an expensive multi-hundred-row explosion that
# contributes to client-side timeouts. Force these known-true FK columns back
# into FK consideration regardless of their queryable flag so the Steiner tree
# can use the direct, semantically-correct drug_id edge instead.
_FORCE_FK_COLS: frozenset = frozenset({"drug_id"})


# ── Enumerate-output guard ──────────────────────────────────────────────────────
# DB-AGNOSTIC. "How many GENES are associated with cystic fibrosis?" must mark the
# gene concept column as a requested OUTPUT; the value-mapper LLM does this most of
# the time but intermittently drops the output entity on "how many X for Y" / "which
# X for Y" phrasings, leaving a filter-only parsed_value (→ the planner pulls only
# the filter table; the answer is wrong). This deterministic guard restores the
# missing output by matching the question's head noun to a queryable concept column
# via concept_type — NO per-DB hardcoding. Tightly gated: fires only when the
# question is enumerate-intent AND the mapper produced ZERO requested outputs (an
# unambiguous miss). Never overrides an existing output decision.
_ENUM_RX = re.compile(
    r"^\s*(?:how many|how much|which|what(?:\s+are|\s+is)?|list|name|show(?:\s+me)?|"
    r"give(?:\s+me)?|find|count(?:\s+the)?(?:\s+number\s+of)?|number\s+of)\s+"
    r"(?:all\s+|the\s+|any\s+|distinct\s+|different\s+|unique\s+|total\s+|specific\s+)*"
    r"(?P<noun>[a-zA-Z][a-zA-Z-]*)",
    re.I,
)


def _singularize(word: str) -> str:
    w = word.lower().strip()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 1 and w.endswith("s"):
        return w[:-1]
    return w


def _ensure_enumerated_output(parsed_value: dict, question: str, kept: list,
                              graph, db: str) -> dict:
    """Promote the head-noun's concept column to 'requested' when an enumerate
    question produced no output column. See module note above. Returns
    parsed_value unchanged unless it can confidently fill the gap."""
    if any(v == "requested" for v in (parsed_value or {}).values()):
        return parsed_value
    m = _ENUM_RX.match(question or "")
    if not m:
        return parsed_value
    head = _singularize(m.group("noun"))
    if len(head) < 3:
        return parsed_value
    # ANN-ranked `kept` first, then any queryable column of this db (the mapper
    # sometimes drops the entity so thoroughly ANN never surfaces it). Match the
    # head noun against concept_type / column name; skip id/xref concepts (the user
    # enumerating "genes" wants the symbol/name, not the accession).
    kept_ids = [cid for cid, _ in kept]
    kept_set = set(kept_ids)
    ordered = kept_ids + [cid for cid, cn in graph.col_nodes.items()
                          if cn.db == db and cn.queryable and cid not in kept_set]
    for col_id in ordered:
        cn = graph.col_nodes.get(col_id)
        if cn is None or cn.db != db or not cn.queryable or cn.column in parsed_value:
            continue
        ctype = (cn.concept_type or "").lower()
        if "xref" in ctype or ctype.endswith("_id"):
            continue
        if head in ctype or head in cn.column.lower():
            parsed_value[cn.column] = "requested"
            logger.info("[schema_kg_planner:%s] enumerate-output guard: promoted %r "
                        "to 'requested' for %r", db, cn.column, (question or "")[:80])
            return parsed_value
    return parsed_value


# ── Steiner tree ────────────────────────────────────────────────────────────────

def _steiner_tree_with_jk(needed_tables: set, G: nx.Graph, join_key: dict) -> tuple:
    """Greedy nearest-fragment Steiner tree, deterministic and FK-identity-aware.

    Tie-break history (2026-08-01): when a table is EQUIDISTANT from multiple
    already-connected fragments via unrelated FK families (e.g. TTD's
    target_master_table_ttd is 2 hops from both drug_master_table_ttd — via
    drug_target_association_ttd, the correct "drug binds this gene" edge — AND
    disease_master_table_ttd — via target_disease_association_ttd, an unrelated
    "gene implicated in this disease" edge with no notion of drug at all), the
    previous version broke ties with whichever candidate Python's `set`
    iteration order surfaced first — a function of hash-seed, not schema
    semantics. That let the disease-mediated bridge win, silently dropping
    drug_target_association_ttd from the plan and turning "drugs targeting
    PIK3CA for breast cancer" into "every target implicated in any of
    alpelisib's diseases" (707→761 unrelated rows).

    Fix: iterate candidates in a fixed (sorted) order, and among equal-length
    paths prefer the one that REUSES an FK column already active in the tree
    (i.e. keeps threading the same identity — e.g. drug_id — that seeded the
    query) over one that introduces a fresh, unrelated FK family. Only when
    both path length and reuse are tied does table-name order (now sorted,
    not hash-dependent) decide — fully deterministic regardless of
    PYTHONHASHSEED.
    """
    if not needed_tables:
        return set(), []
    connected  = {next(iter(needed_tables))}
    remaining  = set(needed_tables) - connected
    join_steps: list = []
    seen_edges: set  = set()
    used_join_cols: set = set()

    def _path_join_cols(path: list) -> list:
        cols = []
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            cols.append(join_key.get((a, b), join_key.get((b, a), "?")))
        return cols

    while remaining:
        best_path, best_target, best_score = None, None, None
        for target in sorted(remaining):
            for src in sorted(connected):
                try:
                    path = nx.shortest_path(G, src, target)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                reuse = sum(1 for c in _path_join_cols(path) if c in used_join_cols)
                # Lower is better: shortest path first, then most FK-identity
                # reuse, then alphabetical (src, target) as the final,
                # fully-deterministic tie-break.
                score = (len(path), -reuse, src, target)
                if best_score is None or score < best_score:
                    best_score, best_path, best_target = score, path, target
        if best_path is None:
            break
        for i in range(len(best_path) - 1):
            a, b  = best_path[i], best_path[i + 1]
            ekey  = tuple(sorted([a, b]))
            if ekey not in seen_edges:
                seen_edges.add(ekey)
                jc = join_key.get((a, b), "?")
                join_steps.append((a, b, jc))
                used_join_cols.add(jc)
            connected.add(b)
        remaining.discard(best_target)
    return connected, join_steps


# ── Pruned subgraph ──────────────────────────────────────────────────────────────

def _build_pruned_subgraph(kept: list, parsed_value: dict, graph, db: str) -> dict:
    """Build a query-specific FK subgraph.

    needed_tables is derived from parsed_value (not raw ANN hits) so columns
    the value mapper ignores don't pull in spurious tables.
    """
    # FK groups: non-queryable columns appearing in ≥2 tables. Computed FIRST so
    # each table's FK signature is available to collapse redundant parallel tables.
    fk_col_to_tbls: dict = defaultdict(set)
    for col_id, col_node in graph.col_nodes.items():
        col_lower = col_node.column.lower()
        if (col_node.db == db
                and (not col_node.queryable or col_lower in _FORCE_FK_COLS)
                and col_lower not in _NON_FK_DESCRIPTIVE_COLS):
            fk_col_to_tbls[col_node.column].add(col_node.table)
    fk_groups = {col: sorted(tbls) for col, tbls in fk_col_to_tbls.items() if len(tbls) >= 2}
    fk_per_tbl: dict = defaultdict(set)
    for fk_col, tables in fk_groups.items():
        for t in tables:
            fk_per_tbl[t].add(fk_col)

    # For each filtered/requested concept column, pick a SINGLE canonical table.
    # Many DBs (TTD, CTD, UniProt, STRING, …) denormalise concept columns —
    # e.g. `gene_symbol` / `drug_name` are mirrored into several association
    # tables for convenience. Adding ALL of them to needed_tables makes the
    # Steiner tree stitch every mirror together, exploding the join
    # (TTD "drugs target EGFR" → 10.8M rows). Prefer a `*_master_table` when one
    # holds the column; otherwise prefer a queryable occurrence; else fall back
    # to all. Normalised DBs (HCDT) are unaffected — their column lives in one
    # master table either way.
    needed_tables: set = set()
    # OUTPUT-only columns that are mirrored across multiple NON-master tables are
    # DEFERRED — see the "outputs project, they don't drive topology" pass after
    # the Steiner tree below. {col_name: [candidate_tables]}.
    deferred_outputs: dict = {}
    for col_name, val in parsed_value.items():
        is_filter = isinstance(val, list)
        is_output = (val == "requested")
        if not (is_filter or is_output):
            continue
        cand = [cn for cid, cn in graph.col_nodes.items()
                if cn.db == db and cn.column == col_name]
        if not cand:
            continue
        # Match "_master_table" as a SUBSTRING — some DBs' schema_kg table names
        # carry a `_<db>` suffix (e.g. disease_master_table_ctd), so endswith()
        # would miss them and fail to collapse denormalized mirrors.
        masters = [cn.table for cn in cand if "_master_table" in cn.table]
        if masters:
            needed_tables.update(masters)
            continue
        queryable = [cn.table for cn in cand if getattr(cn, "queryable", False)]
        cand_tables = sorted(set(queryable or [cn.table for cn in cand]))
        # A FILTER column genuinely constrains the join → it must seed
        # needed_tables. An OUTPUT-only ('requested') column merely needs to be
        # PROJECTED; when it is mirrored across several non-master tables, adding
        # them all makes the Steiner tree stitch in a spurious (often huge) mirror
        # — e.g. CTD `interaction_actions` lives in BOTH chemical_gene_association
        # AND chemical_phenotype_ixn (161k rows); the latter exploded the join.
        # Defer such columns and attach them post-Steiner to a table the plan
        # already includes. Generic; no per-DB/per-column hardcoding.
        if is_output and len(cand_tables) > 1:
            deferred_outputs[col_name] = cand_tables
        else:
            needed_tables.update(cand_tables)

    if not needed_tables:
        return {"needed_tables": set(), "plan_tables": set(), "join_path": [],
                "table_cols": {}, "pruned_G": nx.Graph(), "join_key": {}}

    # Collapse PARALLEL redundant tables: non-master needed tables that share the
    # IDENTICAL FK-key signature are alternative views of the SAME relationship
    # (e.g. STRING's ppi_association / ppi_physical / ppi_detailed_channels are all
    # keyed on {protein_id, protein_partner_id}). Joining several of them collides
    # on the shared key ("protein_partner_id_right already exists"). Keep only the
    # one covering the most requested concept columns; drop the rest.
    def _concept_coverage(t: str) -> int:
        return sum(1 for c in parsed_value if f"{db}.{t}.{c}" in graph.col_nodes)
    by_sig: dict = defaultdict(list)
    for t in needed_tables:
        if "_master_table" in t:
            continue
        sig = frozenset(fk_per_tbl.get(t, ()))
        if sig:
            by_sig[sig].append(t)
    for sig, tbls in by_sig.items():
        if len(tbls) < 2:
            continue
        keep = max(tbls, key=lambda t: (_concept_coverage(t), t))
        for t in tbls:
            if t != keep:
                needed_tables.discard(t)
        logger.info("[schema_kg_planner:%s] collapsed parallel tables %s → kept %s",
                    db, sorted(tbls), keep)

    # Full FK graph for Steiner
    all_tables = {cn.table for cn in graph.col_nodes.values() if cn.db == db}
    full_G  = nx.Graph()
    full_jk: dict = {}
    full_G.add_nodes_from(all_tables)
    for fk_col, tables in fk_groups.items():
        for i, ta in enumerate(tables):
            for tb in tables[i + 1:]:
                if not full_G.has_edge(ta, tb):
                    full_G.add_edge(ta, tb, join_col=fk_col)
                full_jk[(ta, tb)] = fk_col
                full_jk[(tb, ta)] = fk_col

    plan_tables, join_path = _steiner_tree_with_jk(needed_tables, full_G, full_jk)

    # ── Outputs project, they don't drive topology ────────────────────────────
    # Re-attach DEFERRED output-only columns (mirrored across non-master tables).
    # If the Steiner tree already includes a table carrying the column (very
    # common — the column is often mirrored INTO the join's bridge table), do
    # nothing: assemble_pruned_plan's output_plan projects it from there for
    # free, and we avoid pulling in the spurious mirror. Only when NO plan table
    # carries it do we add the single occurrence with the shortest join path into
    # the current plan, then re-run Steiner. Generic; no per-DB hardcoding.
    if deferred_outputs:
        _added = False
        for col_name, cand_tables in deferred_outputs.items():
            if any(f"{db}.{t}.{col_name}" in graph.col_nodes for t in plan_tables):
                continue  # already projectable from a plan table — nothing to add
            best_t, best_len = None, None
            for t in cand_tables:
                for pt in plan_tables:
                    try:
                        d = len(nx.shortest_path(full_G, pt, t))
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    if best_len is None or d < best_len or (d == best_len and t < best_t):
                        best_t, best_len = t, d
            chosen = best_t or cand_tables[0]
            needed_tables.add(chosen)
            _added = True
            logger.info("[schema_kg_planner:%s] deferred output %r not covered by "
                        "plan — added nearest table %s", db, col_name, chosen)
        if _added:
            plan_tables, join_path = _steiner_tree_with_jk(needed_tables, full_G, full_jk)

    # Relevant columns per table
    table_cols: dict = {}
    for table in plan_tables:
        cols: set = set(fk_per_tbl.get(table, set()))
        if table in needed_tables:
            for col_name in parsed_value:
                if f"{db}.{table}.{col_name}" in graph.col_nodes:
                    cols.add(col_name)
            for col_id, _ in kept:
                parts = col_id.split(".")
                if parts[0] == db and parts[1] == table:
                    cols.add(parts[2])
        table_cols[table] = cols

    pruned_G  = nx.Graph()
    pruned_jk: dict = {}
    pruned_G.add_nodes_from(plan_tables)
    for fk_col, tables in fk_groups.items():
        in_plan = [t for t in tables if t in plan_tables]
        for i, ta in enumerate(in_plan):
            for tb in in_plan[i + 1:]:
                if not pruned_G.has_edge(ta, tb):
                    pruned_G.add_edge(ta, tb, join_col=fk_col)
                pruned_jk[(ta, tb)] = fk_col
                pruned_jk[(tb, ta)] = fk_col

    return {
        "needed_tables": needed_tables,
        "plan_tables":   plan_tables,
        "join_path":     join_path,
        "table_cols":    table_cols,
        "pruned_G":      pruned_G,
        "join_key":      pruned_jk,
    }


# ── Production plan converters ────────────────────────────────────────────────────

def to_production_plan(pruned_plan: dict, db: str) -> dict:
    """Convert plan_query_pruned() result to join_and_filter_database() format.

    Returns {tables, parents, table_columns, join_pairs}.
    """
    plan_tables = pruned_plan["plan_tables"]
    join_path   = pruned_plan["join_path"]
    filter_plan = pruned_plan.get("filter_plan", {})
    output_plan = pruned_plan.get("output_plan", {})

    appears_as_child = {b for (a, b, _jk) in join_path}
    root_candidates  = plan_tables - appears_as_child
    if not root_candidates:
        root = next(iter(plan_tables))
    else:
        root = max(root_candidates, key=lambda t: len(filter_plan.get(t, {})))

    bfs_order     = [root]
    parents_short = {root: None}
    for a, b, _jk in join_path:
        if b not in parents_short:
            bfs_order.append(b)
            parents_short[b] = a
        if a not in parents_short:
            bfs_order.insert(1, a)
            parents_short[a] = root

    fq_tables = [f"{db}.{t}" for t in bfs_order]
    parents   = {
        f"{db}.{t}": (None if p is None else f"{db}.{p}")
        for t, p in parents_short.items()
    }

    child_join_col = {b: jk for (a, b, jk) in join_path}
    table_columns: dict = {}
    for table in bfs_order:
        fq_table = f"{db}.{table}"
        concept  = sorted(
            set(filter_plan.get(table, {}).keys()) |
            set(output_plan.get(table, []))
        )
        join_col = child_join_col.get(table)
        table_columns[fq_table] = {
            "concept_columns": concept,
            "join_columns":    [join_col] if join_col else [],
        }

    join_pairs = {
        (f"{db}.{a}", f"{db}.{b}"): {"left_on": [jk], "right_on": [jk]}
        for (a, b, jk) in join_path
    }

    return {
        "tables":        fq_tables,
        "parents":       parents,
        "table_columns": table_columns,
        "join_pairs":    join_pairs,
    }


def _resolve_mutual_exclusions(parsed_value: dict, question: str, rules: Optional[dict]) -> dict:
    """Deterministic post-LLM guard: resolve conflicting 'requested' columns."""
    if not rules:
        return parsed_value
    exclusions = rules.get("mutual_exclusion_rules", [])
    if not exclusions:
        return parsed_value

    pv = dict(parsed_value)
    q_lower = question.lower()

    for rule in exclusions:
        cols = rule.get("columns", [])
        requested = [c for c in cols if pv.get(c) == "requested"]
        if len(requested) < 2:
            continue

        scores = {}
        for col in requested:
            kw_key = f"{col}_keywords"
            keywords = rule.get(kw_key, [])
            scores[col] = sum(1 for kw in keywords if kw in q_lower)

        best_score = max(scores.values())
        if best_score == 0:
            winner = next(c for c in cols if c in requested)
        else:
            winner = max(scores, key=lambda c: scores[c])

        for col in requested:
            if col != winner:
                del pv[col]
                logger.info(
                    "[schema_kg_planner] mutual_exclusion: dropped '%s' in favour of '%s' "
                    "(question=%r)", col, winner, question[:80]
                )

    return pv


def assemble_pruned_plan(kept: list, parsed_value: dict, graph, db: str, *,
                         question: str = "", clean_query: str = "",
                         mapper_agreement: bool = True,
                         orchestrator_used: bool = False,
                         rules: Optional[dict] = None) -> dict:
    """Deterministic prune + Steiner step: turn (kept columns, parsed_value)
    into a join plan. Pure networkx/graph work — NO LLM, NO embeddings.

    This is the body of the standalone schema_planner tool. `plan_query_pruned`
    calls it right after the LLM retrieval stage; the schema_planner HTTP service
    calls it directly on the `kept` / `parsed_value` supplied by the upstream
    retrieval+mapper tools. Both share this one implementation.
    """
    parsed_value = _resolve_mutual_exclusions(parsed_value, question, rules)
    parsed_value = _ensure_enumerated_output(parsed_value, question, kept, graph, db)

    sg = _build_pruned_subgraph(kept, parsed_value, graph, db=db)

    filter_plan: dict = {}
    output_plan: dict = {}
    for col, val in parsed_value.items():
        for table in sg["plan_tables"]:
            if f"{db}.{table}.{col}" not in graph.col_nodes:
                continue
            if isinstance(val, list):
                filter_plan.setdefault(table, {})[col] = val
            elif val == "requested":
                output_plan.setdefault(table, []).append(col)

    return {
        "question":          question,
        "clean_query":       clean_query,
        "parsed_value":      parsed_value,
        "needed_tables":     sg["needed_tables"],
        "plan_tables":       sg["plan_tables"],
        "join_path":         sg["join_path"],
        "table_cols":        sg["table_cols"],
        "filter_plan":       filter_plan,
        "output_plan":       output_plan,
        "mapper_agreement":  mapper_agreement,
        "orchestrator_used": orchestrator_used,
    }


# ── Shared-map loader ────────────────────────────────────────────────────────────────

def _load_schema_rules(rules_path: Path) -> dict:
    """Load schema_rules.json and merge shared_maps.json as the base layer.

    shared_maps.json lives one level above the per-DB inputs dir
    (schema_kg/inputs/shared_maps.json).  Per-DB map entries extend the shared
    ones — the same key in the per-DB file adds entries on top of the shared set
    rather than replacing it entirely, so a DB can add trade names without
    re-declaring the common ones.
    """
    rules: dict = {}
    shared_path = rules_path.parent.parent / "shared_maps.json"
    if shared_path.exists():
        with open(shared_path) as f:
            shared = json.load(f)
        for key, val in shared.items():
            if not key.startswith("_"):
                rules[key] = dict(val) if isinstance(val, dict) else val
    if rules_path.exists():
        with open(rules_path) as f:
            db_rules = json.load(f)
        for key, val in db_rules.items():
            if key in rules and isinstance(rules[key], dict) and isinstance(val, dict):
                rules[key] = {**rules[key], **val}  # shared base, DB extends
            else:
                rules[key] = val
    return rules


# ── Lean graph-only loader (no embeddings / Qdrant / torch) ───────────────────────

def build_schema_graph(db: str, inputs_root: Path = _INPUTS_ROOT) -> tuple:
    """Build ONLY the schema FK graph + schema_rules for `db`.

    Lean counterpart to ``SchemaKgPlanner._do_load`` — it skips the bge model,
    embedding computation and the in-memory Qdrant index, so the standalone
    schema_planner tool (pure graph work) stays torch-free. Returns
    ``(graph, rules_dict)``.
    """
    from schema_kg.src.graph import build_graph

    inputs = Path(inputs_root) / db
    graph = build_graph(
        inputs / "schema.json",
        inputs / "queryable.json",
        inputs / "concept_type.json",
    )
    rules_path = inputs / "schema_rules.json"
    rules = _load_schema_rules(rules_path)
    return graph, rules


_graphs: dict = {}
_graph_lock = threading.Lock()


def get_graph(db: str) -> tuple:
    """Return the cached ``(graph, rules)`` for `db`, building once on first use."""
    g = _graphs.get(db)
    if g is None:
        with _graph_lock:
            g = _graphs.get(db)
            if g is None:
                g = build_schema_graph(db)
                _graphs[db] = g
    return g


# ── STRING ppi_physical → ppi_association override ───────────────────────────────

_ASSOC_RE = re.compile(
    r"\b(interact|interaction|link|associated\s+with|partner|co.operat|work\s+together"
    r"|co[\s\-]?local\w*|regulat\w+\s+protein\s+of|inhibitor\s+of|inhibitory\s+protein"
    r"|component\w*\s+of|subunit\w*\s+of|member\w*\s+of|form\w*\s+the|contain\w*"
    r"|participate\w*\s+in|belong\w*\s+to|part\s+of|protein\s+complex)\b",
    re.IGNORECASE,
)
_PHYS_RE = re.compile(
    r"\b(physically|direct\s+interaction|direct\s+binding|co.complex|structural\s+interaction"
    r"|physically\s+bind|binding\s+partner)\b",
    re.IGNORECASE,
)

_PHYS_COL_MAP = {
    "physical_gene_symbol":         "association_gene_symbol",
    "physical_partner_gene_symbol": "association_partner_gene_symbol",
    "physical_score":               "association_score",
}

# Common protein names → canonical STRING gene symbols.  Mirrors the term_rewrite
# rules in db_llm_rules.yaml for STRING.  Keys are lowercase for case-insensitive lookup.
_STRING_ALIAS_PATCH: dict[str, str] = {
    "phospholamban": "PLN",
    "triadin": "TRDN",
    "serca": "ATP2A2",
    "serca2": "ATP2A2",
    "serca1": "ATP2A1",
    "serca3": "ATP2A3",
    "histidine-rich calcium-binding protein": "HRC",
    "histidine-rich ca-binding protein": "HRC",
    "hrc protein": "HRC",
    "p110α": "PIK3CA", "p110alpha": "PIK3CA",
    "p85α": "PIK3R1", "p85alpha": "PIK3R1",
    "p53": "TP53",
    "pd-1": "PDCD1",
    "c-met": "MET",
    "her2": "ERBB2", "erbb2": "ERBB2",
    "ikkα": "CHUK", "ikkalpha": "CHUK",
    "ikkβ": "IKBKB", "ikkbeta": "IKBKB",
    "ikkγ": "IKBKG", "ikk-gamma": "IKBKG",
    "mtor": "MTOR",
    "star-pap": "TUT1",
}


def _resolve_gene_aliases(genes: list[str]) -> list[str]:
    """Replace known common names with canonical STRING gene symbols (case-insensitive)."""
    resolved: list[str] = []
    for g in genes:
        if isinstance(g, str):
            canonical = _STRING_ALIAS_PATCH.get(g.lower())
            resolved.append(canonical if canonical else g)
        else:
            resolved.append(g)
    return resolved


def _extract_genes_from_question(question: str) -> list[str]:
    """Scan question for known common protein names; return their canonical gene symbols."""
    q_lower = question.lower()
    found: list[str] = []
    seen: set[str] = set()
    for alias, sym in _STRING_ALIAS_PATCH.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', q_lower) and sym not in seen:
            found.append(sym)
            seen.add(sym)
    return found


# Uppercase stop-tokens that appear in biomedical questions but are NOT gene symbols.
_UPPERCASE_STOP: frozenset[str] = frozenset({
    "A", "I", "AN", "AS", "AT", "BY", "DO", "IN", "IS", "IT", "OF", "ON",
    "OR", "TO", "UP", "VS", "THE", "AND", "ARE", "FOR", "NOT", "HAS", "ITS",
    "CAN", "DID", "DNA", "RNA", "PCR", "NMR", "MRI", "CAT", "SDS", "PAGE",
    "MHC", "LPS", "IgG", "IgE", "IgA", "IgM", "ATP", "ADP", "GTP", "GDP",
    "CTP", "UTP", "NAD", "FAD", "HSP", "ECM", "TCR", "BCR", "PRR", "PAR",
    "YES", "NO",
})

_UPPERCASE_SYM_RE = re.compile(r'\b([A-Z][A-Z0-9\-]{1,9})\b')

_UNKNOWN_MEMBER_RE = re.compile(
    r"\b(third|3rd|additional|another|other|unknown|remaining|missing|"
    r"identify|which\s+(?:is\s+)?(?:the\s+)?(?:third|other|additional|"
    r"unknown|new|missing))\b",
    re.I,
)


def _extract_uppercase_syms_from_question(question: str, exclude: set[str]) -> list[str]:
    """Return all-uppercase tokens from question that look like gene symbols.

    Used as a fallback when term_rewrite has already canonicalized names in the query
    text (e.g. 'phospholamban'→'PLN') so alias-based scanning finds nothing.
    Filters out common non-gene uppercase tokens and any already in `exclude`.
    """
    found: list[str] = []
    seen: set[str] = set(exclude)
    for m in _UPPERCASE_SYM_RE.finditer(question):
        tok = m.group(1)
        if tok in _UPPERCASE_STOP or tok in seen:
            continue
        # Must be fully uppercase (no lowercase chars)
        if tok != tok.upper():
            continue
        seen.add(tok)
        found.append(tok)
    return found


def _string_physical_to_association_override(question: str, plan: dict) -> dict:
    """If STRING chose ppi_physical for a bare 'interact' question, swap to association.

    The LLM filter often misroutes functional interaction queries (e.g. 'Do LSM4 and
    LSM6 interact with SMN?') to ppi_physical because RNA-binding proteins are
    conceptually associated with complexes/physical interaction.  ppi_association is
    bidirectionally expanded in-memory and captures co-IP (experimental channel) edges
    at score≥700, so functional queries always find the right rows there.
    """
    if "ppi_physical_string" not in (plan.get("plan_tables") or []):
        return plan
    if not _ASSOC_RE.search(question) or _PHYS_RE.search(question):
        return plan

    import copy
    plan = copy.deepcopy(plan)
    # Rename the table key everywhere it appears
    for key in ("plan_tables", "needed_tables"):
        lst = plan.get(key) or []
        plan[key] = [("ppi_association_string" if t == "ppi_physical_string" else t)
                     for t in lst]
    # Rename columns in table_cols
    if "table_cols" in plan:
        tc = plan["table_cols"]
        if "ppi_physical_string" in tc:
            tc["ppi_association_string"] = [
                _PHYS_COL_MAP.get(c, c) for c in tc.pop("ppi_physical_string")
            ]
    # Rename columns in filter_plan and output_plan (values may be dict OR list)
    for pkey in ("filter_plan", "output_plan"):
        pdata = plan.get(pkey) or {}
        if "ppi_physical_string" in pdata:
            rows = pdata.pop("ppi_physical_string")
            if isinstance(rows, dict):
                remapped = {_PHYS_COL_MAP.get(c, c): v for c, v in rows.items()}
            else:
                remapped = [_PHYS_COL_MAP.get(c, c) for c in rows]
            pdata["ppi_association_string"] = remapped
    # Rename join_path table references
    if plan.get("join_path"):
        plan["join_path"] = [
            [("ppi_association_string" if t == "ppi_physical_string" else t) for t in step]
            for step in plan["join_path"]
        ]
    # CRITICAL: also rename columns in parsed_value so that the expand step (which
    # runs on plan["parsed_value"]) uses association column names.  Without this,
    # ctx.filter_val ends up with physical_gene_symbol keys that don't exist in
    # ppi_association_string, so fast_filter_dataframe silently skips the filter
    # and either returns all rows (0% reduction) or the wrong column is requested.
    if "parsed_value" in plan:
        pv = plan["parsed_value"]
        plan["parsed_value"] = {_PHYS_COL_MAP.get(k, k): v for k, v in pv.items()}
    logger.info("[schema_kg_planner:string] overrode ppi_physical→ppi_association "
                "(bare 'interact' question without physical qualifier)")
    return plan


_PPI_SCORE_COL = {
    "ppi_association_string":       "association_score",
    "ppi_physical_string":          "physical_score",
    "ppi_detailed_channels_string": "channel_combined_score",
}


def _tc_add(plan: dict, table: str, col: str) -> None:
    """Add col to plan['table_cols'][table], handling both list and set representations."""
    tc = plan.setdefault("table_cols", {}).get(table)
    if isinstance(tc, set):
        tc.add(col)
    elif isinstance(tc, list):
        if col not in tc:
            tc.append(col)
    else:
        plan["table_cols"][table] = {col}


def _string_genesym_to_ppi_override(question: str, plan: dict) -> dict:
    """Ensure yes/no PPI questions get edge data with a score, not just master-table rows.

    Four failure modes fixed here:
    (A) plan_tables = {protein_master_table_string} only — mapper chose gene_symbol
        instead of an association column.  Redirect to ppi_association_string.
    (B) plan_tables = {ppi_association_string} but parsed_value has only
        association_gene_symbol with 2+ genes and NO partner filter.  This yields a
        wide result (all partners of A OR B) with no score column.  Add
        association_partner_gene_symbol as a cross-filter to narrow to the A↔B edge.
    (C) Any plan using a single PPI table without a score output column — add the
        table-specific score column as 'requested' so the synthesizer can confirm
        the interaction numerically.
    (D) plan_tables = {ppi_association_string, protein_master_table_string} — mapper
        split the two query genes across tables (one in association_gene_symbol, the
        other in gene_symbol on protein_master).  The join produces ~400 rows whose
        BGE row-relevance scoring exceeds the 180 s execute timeout.  Redirect to a
        pure ppi_association query with both genes cross-filtered.
    """
    if not _ASSOC_RE.search(question) or _PHYS_RE.search(question):
        return plan

    plan_tables = plan.get("plan_tables") or set()
    pv = plan.get("parsed_value") or {}

    # Case (A): only protein_master_table — redirect to ppi_association
    if set(plan_tables) == {"protein_master_table_string"}:
        genes = pv.get("gene_symbol") or []
        if not isinstance(genes, list):
            genes = []
        # Resolve any common names (e.g. 'phospholamban' → 'PLN')
        genes = _resolve_gene_aliases(genes)
        # Deduplicate (alias resolution can produce ['ATP2A2','ATP2A2'] when value_mapper
        # builds pv1_valid + valid_genes with the same uppercase symbol twice).
        _seen_dedup: set = set()
        _deduped: list = []
        for _g in genes:
            _k = _g.upper() if isinstance(_g, str) else str(_g)
            if _k not in _seen_dedup:
                _seen_dedup.add(_k)
                _deduped.append(_g)
        genes = _deduped
        if len(genes) < 2:
            # Supplement with known aliases found verbatim in the question
            q_genes = _extract_genes_from_question(question)
            combined_seen: set = {g.upper() for g in genes}
            for g in q_genes:
                if g.upper() not in combined_seen:
                    genes.append(g)
                    combined_seen.add(g.upper())
        if len(genes) < 2:
            # Fallback: term_rewrite may have already canonicalized names in the question
            # (e.g. 'phospholamban'→'PLN'), so alias scan found nothing.  Scan for
            # all-uppercase tokens that look like gene symbols.
            combined_seen2: set = {g.upper() for g in genes}
            for g in _extract_uppercase_syms_from_question(question, combined_seen2):
                genes.append(g)
                combined_seen2.add(g.upper())
        if len(genes) < 1:
            return plan
        import copy
        plan = copy.deepcopy(plan)
        plan["plan_tables"]   = {"ppi_association_string"}
        plan["needed_tables"] = {"ppi_association_string"}
        plan["join_path"]     = []
        if len(genes) == 1:
            # Single gene (A1): "which proteins form/are members of X?" — return all
            # partners so the synthesizer can identify the relevant complex subunits.
            plan["table_cols"] = {"ppi_association_string": {"association_gene_symbol",
                                                              "association_partner_gene_symbol",
                                                              "association_score"}}
            plan["filter_plan"]  = {"ppi_association_string": {
                "association_gene_symbol": genes,
            }}
            plan["output_plan"]  = {"ppi_association_string": ["association_partner_gene_symbol",
                                                                "association_score"]}
            plan["parsed_value"] = {
                "association_gene_symbol":         genes,
                "association_partner_gene_symbol": "requested",
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(A1): protein_master→ppi_association "
                        "single-gene=%s", genes)
        else:
            # Detect "which is the third/additional/unknown member?" questions.
            # Cross-filtering known genes against each other would never surface the
            # unknown member.  Use ALL known genes as an OR-filter on the anchor column
            # so the synthesizer receives partners of every known complex member and
            # can identify the unrecognised subunit among them.
            # (Anchoring on genes[:1] alone fails when the first gene is a downstream
            # CONTEXT gene, e.g. MTOR in "TSC1-TSC2 complex upstream of mTOR".)
            if _UNKNOWN_MEMBER_RE.search(question):
                # Filter out context genes mentioned after "upstream of / downstream of /
                # regulated by / regulated / dependent on" so the anchor focuses on the
                # COMPLEX genes rather than the pathway-context gene.
                # Pattern: "upstream of GENEX" → GENEX is context, not a complex member.
                _CTX_RE = re.compile(
                    r'\b(?:upstream|downstream|above|below|activat(?:es?|ed\s+by)|'
                    r'inhibit(?:es?|ed\s+by)|regulated(?:\s+by)?|suppressed(?:\s+by)?|'
                    r'dependent(?:\s+on)?|targets?|phosphorylat(?:es?|ed\s+by))\s+(?:of\s+)?'
                    r'([A-Z][A-Z0-9]{1,9})\b',
                    re.I,
                )
                ctx_genes = {m.group(1).upper() for m in _CTX_RE.finditer(question)}
                complex_genes = [g for g in genes if g.upper() not in ctx_genes]
                anchor = complex_genes if complex_genes else genes
                # OR-filter: association_gene_symbol IN anchor
                plan["table_cols"] = {"ppi_association_string": {"association_gene_symbol",
                                                                  "association_partner_gene_symbol",
                                                                  "association_score"}}
                plan["filter_plan"]  = {"ppi_association_string": {
                    "association_gene_symbol": anchor,
                }}
                plan["output_plan"]  = {"ppi_association_string": ["association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"]}
                plan["parsed_value"] = {
                    "association_gene_symbol":         anchor,
                    "association_partner_gene_symbol": "requested",
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(A-unknown): unknown-member "
                            "anchors=%s all-partners", anchor)
            else:
                # Two or more genes (A): cross-filter to find the interaction edge.
                plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"}}
                plan["filter_plan"]   = {"ppi_association_string": {
                    "association_gene_symbol":         genes,
                    "association_partner_gene_symbol": genes,
                }}
                plan["output_plan"]   = {"ppi_association_string": ["association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"]}
                plan["parsed_value"]  = {
                    "association_gene_symbol":         genes,
                    "association_partner_gene_symbol": genes,
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(A): protein_master→ppi_association "
                            "genes=%s", genes)
        return plan

    # Case (B): ppi_association only, 2+ genes in gene_symbol col, no partner filter
    if set(plan_tables) == {"ppi_association_string"}:
        genes = pv.get("association_gene_symbol")
        # has_partner=True only when mapper provided a CONCRETE list of partner genes
        # as a filter value.  "requested" / "(output)" / None are output-only markers
        # (the mapper is asking for the partner column but not filtering on it) and
        # must NOT block Case B / B-unknown.
        _partner_val = pv.get("association_partner_gene_symbol")
        has_partner = isinstance(_partner_val, list) and len(_partner_val) > 0
        # Case (B0): single-gene mapped directly to ppi_association (skips protein_master).
        # The mapper omits association_partner_gene_symbol from output_plan, returning
        # only association_score — the partner column is invisible and list questions
        # (EGFR ligands, Cdc48 partners) fail.  Inject partner into output here.
        if isinstance(genes, list) and len(genes) == 1 and not has_partner:
            import copy
            plan = copy.deepcopy(plan)
            op = plan.setdefault("output_plan", {})
            ppi_op = op.get("ppi_association_string") or []
            if isinstance(ppi_op, list) and "association_partner_gene_symbol" not in ppi_op:
                op["ppi_association_string"] = ["association_partner_gene_symbol"] + ppi_op
            _tc_add(plan, "ppi_association_string", "association_partner_gene_symbol")
            plan.setdefault("parsed_value", {}).setdefault(
                "association_partner_gene_symbol", "requested")
            logger.info("[schema_kg_planner:string] ppi_override(B0): single-gene ppi, "
                        "injected partner_gene_symbol into output gene=%s", genes)
        # B-unknown-single: 1 anchor gene + concrete partner + unknown-member question.
        # The partner is a CONTEXT gene (e.g. "upstream of MTOR" in TSC questions),
        # not the unknown subunit. Clear the partner filter; return all anchor partners.
        if isinstance(genes, list) and len(genes) == 1 and has_partner and _UNKNOWN_MEMBER_RE.search(question):
            import copy
            plan = copy.deepcopy(plan)
            plan["table_cols"]  = {"ppi_association_string": {"association_gene_symbol",
                                                               "association_partner_gene_symbol",
                                                               "association_score"}}
            plan["filter_plan"] = {"ppi_association_string": {
                "association_gene_symbol": genes,
            }}
            plan["output_plan"] = {"ppi_association_string": ["association_gene_symbol",
                                                               "association_partner_gene_symbol",
                                                               "association_score"]}
            plan["parsed_value"] = {
                "association_gene_symbol":         genes,
                "association_partner_gene_symbol": "requested",
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(B-unknown-single): "
                        "unknown-member single-anchor=%s cleared context-partner=%s",
                        genes, _partner_val)
            return plan
        if isinstance(genes, list) and len(genes) >= 2:
            import copy
            # B-unknown: "which is the third/unknown member?" — overrides ALL other
            # Case B logic (including cross-filter when mapper set has_partner=True).
            # Cross-filtering known genes against each other would never surface the
            # unknown member; query ALL known genes as anchors (union) and return all
            # their partners so the synthesizer can identify the missing subunit.
            if _UNKNOWN_MEMBER_RE.search(question):
                plan = copy.deepcopy(plan)
                plan["table_cols"]   = {"ppi_association_string": {"association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"}}
                plan["filter_plan"]  = {"ppi_association_string": {
                    "association_gene_symbol": genes,
                }}
                plan["output_plan"]  = {"ppi_association_string": ["association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"]}
                plan["parsed_value"] = {
                    "association_gene_symbol":         genes,
                    "association_partner_gene_symbol": "requested",
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(B-unknown): unknown-member "
                            "anchors=%s all-partners", genes)
                return plan
        if isinstance(genes, list) and len(genes) >= 2 and not has_partner:

            # Three-entity detection: "Do A and B interact with C?" pattern.
            # If a third distinct gene C exists in the question (beyond the genes
            # the mapper extracted), set partner=[C] so we get A↔C and B↔C rows.
            # Otherwise fall back to the original two-entity A↔B cross-filter.
            known: set[str] = {g.upper() for g in genes if isinstance(g, str)}
            third_genes: list = []
            for _g in _extract_genes_from_question(question):
                if _g.upper() not in known:
                    third_genes.append(_g)
                    known.add(_g.upper())
            if not third_genes:
                for _g in _extract_uppercase_syms_from_question(question, known):
                    if _g.upper() not in known:
                        third_genes.append(_g)
                        known.add(_g.upper())
            plan = copy.deepcopy(plan)
            partner_val = third_genes if third_genes else genes
            plan["parsed_value"]["association_partner_gene_symbol"] = partner_val
            fp = plan.setdefault("filter_plan", {}).setdefault("ppi_association_string", {})
            if isinstance(fp, dict):
                fp["association_partner_gene_symbol"] = partner_val
            _tc_add(plan, "ppi_association_string", "association_partner_gene_symbol")
            if third_genes:
                logger.info("[schema_kg_planner:string] ppi_override(B3): three-entity partner=%s "
                            "genes=%s", third_genes, genes)
            else:
                logger.info("[schema_kg_planner:string] ppi_override(B): added partner filter "
                            "genes=%s", genes)
            pv = plan["parsed_value"]  # refresh reference after deepcopy

    # Case (D): ppi_association + protein_master join — mapper split the two genes
    # across tables. BGE scoring on the ~400-row join result exceeds the 180 s timeout.
    # Collect all gene names from any gene/symbol field and redirect to a pure
    # ppi_association cross-filter (same logic as Case A).
    if ({"ppi_association_string", "protein_master_table_string"}.issubset(set(plan_tables))
            and len(set(plan_tables) - _PPI_SCORE_COL.keys()) > 0):
        pv = plan.get("parsed_value") or {}
        gene_pools: list[list] = []
        for fld in ("association_gene_symbol", "association_partner_gene_symbol", "gene_symbol"):
            val = pv.get(fld)
            if isinstance(val, list) and val:
                gene_pools.append(val)
        # Flatten unique gene names across all pools
        seen: set = set()
        genes: list = []
        for pool in gene_pools:
            for g in pool:
                gl = g.lower() if isinstance(g, str) else g
                if gl not in seen:
                    seen.add(gl)
                    genes.append(g)
        # D-unknown: "third member" question — supplement gene list from question text,
        # filter out context genes (upstream of X), anchor on the complex genes only.
        if _UNKNOWN_MEMBER_RE.search(question):
            import copy
            _seen_d = {g.upper() for g in genes}
            for _g in _extract_genes_from_question(question):
                if _g.upper() not in _seen_d:
                    genes.append(_g); _seen_d.add(_g.upper())
            for _g in _extract_uppercase_syms_from_question(question, _seen_d):
                genes.append(_g); _seen_d.add(_g.upper())
            _CTX_RE_D = re.compile(
                r'\b(?:upstream|downstream|above|below|activat(?:es?|ed\s+by)|'
                r'inhibit(?:es?|ed\s+by)|regulated(?:\s+by)?|suppressed(?:\s+by)?|'
                r'dependent(?:\s+on)?|targets?|phosphorylat(?:es?|ed\s+by))\s+(?:of\s+)?'
                r'([A-Z][A-Z0-9]{1,9})\b', re.I,
            )
            ctx_genes_d = {m.group(1).upper() for m in _CTX_RE_D.finditer(question)}
            complex_genes_d = [g for g in genes if g.upper() not in ctx_genes_d]
            anchor = complex_genes_d if complex_genes_d else genes
            if anchor:
                plan = copy.deepcopy(plan)
                plan["plan_tables"]   = {"ppi_association_string"}
                plan["needed_tables"] = {"ppi_association_string"}
                plan["join_path"]     = []
                plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                    "association_partner_gene_symbol",
                                                                    "association_score"}}
                plan["filter_plan"]   = {"ppi_association_string": {
                    "association_gene_symbol": anchor,
                }}
                plan["output_plan"]   = {"ppi_association_string": ["association_gene_symbol",
                                                                     "association_partner_gene_symbol",
                                                                     "association_score"]}
                plan["parsed_value"]  = {
                    "association_gene_symbol":         anchor,
                    "association_partner_gene_symbol": "requested",
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(D-unknown): "
                            "anchors=%s ctx_excluded=%s", anchor, sorted(ctx_genes_d))
                return plan
        import copy
        if len(genes) >= 2:
            plan = copy.deepcopy(plan)
            plan["plan_tables"]   = {"ppi_association_string"}
            plan["needed_tables"] = {"ppi_association_string"}
            plan["join_path"]     = []
            plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"}}
            plan["filter_plan"]   = {"ppi_association_string": {
                "association_gene_symbol":         genes,
                "association_partner_gene_symbol": genes,
            }}
            plan["output_plan"]   = {"ppi_association_string": ["association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"]}
            plan["parsed_value"]  = {
                "association_gene_symbol":         genes,
                "association_partner_gene_symbol": genes,
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(D2): ppi+master→ppi_association "
                        "genes=%s", genes)
            return plan
        elif len(genes) == 1:
            # Only one protein known — return all its partners so synthesizer can find the named partner.
            plan = copy.deepcopy(plan)
            plan["plan_tables"]   = {"ppi_association_string"}
            plan["needed_tables"] = {"ppi_association_string"}
            plan["join_path"]     = []
            plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"}}
            plan["filter_plan"]   = {"ppi_association_string": {
                "association_gene_symbol": genes,
            }}
            plan["output_plan"]   = {"ppi_association_string": ["association_partner_gene_symbol",
                                                                 "association_score"]}
            plan["parsed_value"]  = {
                "association_gene_symbol":         genes,
                "association_partner_gene_symbol": "requested",
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(D1): ppi+master→ppi_association "
                        "single-gene=%s", genes)
            return plan

    # Case (E): plan uses ppi_detailed_channels or other non-association PPI table
    # (possibly combined with protein_master) — mapper routed co-localization /
    # co-occupancy questions to the channels table instead of ppi_association.
    # Collect gene names from all gene-like fields and redirect to ppi_association.
    _NON_ASSOC_PPI = {"ppi_detailed_channels_string", "ppi_physical_string"}
    if _NON_ASSOC_PPI & set(plan_tables):
        pv = plan.get("parsed_value") or {}
        fp = plan.get("filter_plan") or {}
        gene_pools: list = []
        for fld in ("gene_symbol", "channel_gene_symbol", "physical_gene_symbol",
                    "association_gene_symbol", "physical_partner_gene_symbol",
                    "channel_partner_gene_symbol"):
            val = pv.get(fld)
            if isinstance(val, list) and val:
                gene_pools.append(val)
        for table_fp in fp.values():
            if isinstance(table_fp, dict):
                for fld in ("gene_symbol", "channel_gene_symbol"):
                    val = table_fp.get(fld)
                    if isinstance(val, list) and val:
                        gene_pools.append(val)
        _COMPLEX_WORDS = {"complex", "pathway", "signaling", "signalling", "receptor"}
        seen_e: set = set()
        genes_e: list = []
        for pool in gene_pools:
            for g in pool:
                if not isinstance(g, str):
                    continue
                gl = g.lower()
                # Skip values that look like complex/pathway names rather than gene symbols
                if any(w in gl for w in _COMPLEX_WORDS) and " " in gl:
                    continue
                if gl not in seen_e:
                    seen_e.add(gl)
                    genes_e.append(g)
        if len(genes_e) >= 2:
            import copy
            plan = copy.deepcopy(plan)
            plan["plan_tables"]   = {"ppi_association_string"}
            plan["needed_tables"] = {"ppi_association_string"}
            plan["join_path"]     = []
            plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"}}
            plan["filter_plan"]   = {"ppi_association_string": {
                "association_gene_symbol":         genes_e,
                "association_partner_gene_symbol": genes_e,
            }}
            plan["output_plan"]   = {"ppi_association_string": ["association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"]}
            plan["parsed_value"]  = {
                "association_gene_symbol":         genes_e,
                "association_partner_gene_symbol": genes_e,
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(E2): channels+master→ppi_association "
                        "genes=%s", genes_e)
            return plan
        elif len(genes_e) == 1:
            # Only one valid gene (e.g. 'cohesin complex' was filtered out) — return all
            # partners so the synthesizer can identify matching complex subunits.
            import copy
            plan = copy.deepcopy(plan)
            plan["plan_tables"]   = {"ppi_association_string"}
            plan["needed_tables"] = {"ppi_association_string"}
            plan["join_path"]     = []
            plan["table_cols"]    = {"ppi_association_string": {"association_gene_symbol",
                                                                "association_partner_gene_symbol",
                                                                "association_score"}}
            plan["filter_plan"]   = {"ppi_association_string": {
                "association_gene_symbol": genes_e,
            }}
            plan["output_plan"]   = {"ppi_association_string": ["association_partner_gene_symbol",
                                                                 "association_score"]}
            plan["parsed_value"]  = {
                "association_gene_symbol":         genes_e,
                "association_partner_gene_symbol": "requested",
                "association_score":               "requested",
            }
            logger.info("[schema_kg_planner:string] ppi_override(E1): channels+master→ppi_association "
                        "single-gene=%s", genes_e)
            return plan

    # Case (F): ppi_association plan, but either (a) filter values use common protein names
    # (e.g. 'phospholamban', 'SERCA') that must be resolved to gene symbols, or (b) the
    # parsed_value still uses protein_master column names (gene_symbol/annotation) even
    # though plan_tables already points at ppi_association — the mapper returned an
    # inconsistent plan.  Both sub-cases are fixed here.
    if "ppi_association_string" in set(plan_tables):
        import copy as _copy
        pv_f = plan.get("parsed_value") or {}
        fp_f = plan.get("filter_plan", {}).get("ppi_association_string", {})
        changed_f = False

        # Sub-case (F1): pv uses protein_master columns (gene_symbol / annotation) but
        # plan_tables says ppi_association.  Remap gene_symbol → association columns and
        # extract any second gene from the question via alias scan.
        if "gene_symbol" in pv_f and "association_gene_symbol" not in pv_f:
            raw_genes = pv_f.get("gene_symbol") or []
            if not isinstance(raw_genes, list):
                raw_genes = []
            raw_genes = _resolve_gene_aliases(raw_genes)
            # Also scan question text for additional genes
            q_extra = _extract_genes_from_question(question)
            seen_f1: set = {g.upper() for g in raw_genes}
            for _g in q_extra:
                if _g.upper() not in seen_f1:
                    raw_genes.append(_g)
                    seen_f1.add(_g.upper())
            if len(raw_genes) >= 2:
                plan = _copy.deepcopy(plan)
                plan["plan_tables"]   = {"ppi_association_string"}
                plan["needed_tables"] = {"ppi_association_string"}
                plan["join_path"]     = []
                plan["table_cols"]    = {"ppi_association_string": {
                    "association_gene_symbol", "association_partner_gene_symbol",
                    "association_score"}}
                plan["filter_plan"]   = {"ppi_association_string": {
                    "association_gene_symbol":         raw_genes,
                    "association_partner_gene_symbol": raw_genes,
                }}
                plan["output_plan"]   = {"ppi_association_string": ["association_score"]}
                plan["parsed_value"]  = {
                    "association_gene_symbol":         raw_genes,
                    "association_partner_gene_symbol": raw_genes,
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(F1): mixed pv→ppi_association "
                            "genes=%s", raw_genes)
                changed_f = True
            elif len(raw_genes) == 1:
                plan = _copy.deepcopy(plan)
                plan["plan_tables"]   = {"ppi_association_string"}
                plan["needed_tables"] = {"ppi_association_string"}
                plan["join_path"]     = []
                plan["table_cols"]    = {"ppi_association_string": {
                    "association_gene_symbol", "association_partner_gene_symbol",
                    "association_score"}}
                plan["filter_plan"]   = {"ppi_association_string": {
                    "association_gene_symbol": raw_genes,
                }}
                plan["output_plan"]   = {"ppi_association_string": [
                    "association_partner_gene_symbol", "association_score"]}
                plan["parsed_value"]  = {
                    "association_gene_symbol":         raw_genes,
                    "association_partner_gene_symbol": "requested",
                    "association_score":               "requested",
                }
                logger.info("[schema_kg_planner:string] ppi_override(F1-single): mixed pv→ppi_association "
                            "single-gene=%s", raw_genes)
                changed_f = True

        # Sub-case (F2): association columns present but values use common names.
        if not changed_f:
            pv_f = plan.get("parsed_value") or {}
            fp_f = plan.get("filter_plan", {}).get("ppi_association_string", {})
            for _col in ("association_gene_symbol", "association_partner_gene_symbol"):
                for _src in (pv_f, fp_f):
                    if isinstance(_src.get(_col), list):
                        _resolved = _resolve_gene_aliases(_src[_col])
                        if _resolved != _src[_col]:
                            if not changed_f:
                                plan = _copy.deepcopy(plan)
                                pv_f = plan.get("parsed_value") or {}
                                fp_f = plan.get("filter_plan", {}).get("ppi_association_string", {})
                            pv_f[_col] = _resolved
                            if isinstance(fp_f, dict):
                                fp_f[_col] = _resolved
                            changed_f = True
            if changed_f:
                logger.info("[schema_kg_planner:string] ppi_override(F2): alias-resolved ppi filters "
                            "gene=%s partner=%s",
                            pv_f.get("association_gene_symbol"),
                            pv_f.get("association_partner_gene_symbol"))
        pv = plan.get("parsed_value") or {}

    # Case (C): any single-PPI-table plan — ensure score column is requested
    ppi_only = {t for t in set(plan_tables) if t in _PPI_SCORE_COL}
    if ppi_only and set(plan_tables) == ppi_only:
        for table in ppi_only:
            score_col = _PPI_SCORE_COL[table]
            if pv.get(score_col) != "requested":
                import copy
                plan = copy.deepcopy(plan)
                plan["parsed_value"][score_col] = "requested"
                plan.setdefault("output_plan", {}).setdefault(table, [])
                if score_col not in plan["output_plan"][table]:
                    plan["output_plan"][table].append(score_col)
                _tc_add(plan, table, score_col)
                logger.info("[schema_kg_planner:string] ppi_override(C): added %s output "
                            "to %s", score_col, table)
                break  # only one PPI table in this branch

    return plan


# ── Per-DB planner instance ──────────────────────────────────────────────────────

class SchemaKgPlanner:
    """In-process Schema-KG planner for a single database.

    Thread-safe lazy load: the graph + embeddings + in-memory Qdrant collection
    are built once on first use (or via `warm()` at boot).
    """

    def __init__(self, db: str, inputs_root: Path = _INPUTS_ROOT) -> None:
        self.db = db
        self.inputs = Path(inputs_root) / db
        self.collection = f"{db}_schema_kg"
        self._graph = None
        self._qdrant = None
        self._rules: dict = {}
        self._loaded = False
        self._lock = threading.Lock()

    # -- loading --------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._do_load()
            self._loaded = True

    def _do_load(self) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qmodels
        from schema_kg.src.graph import build_graph
        from schema_kg.src.embed import compute_embeddings

        logger.info("[schema_kg_planner:%s] Loading schema graph from %s",
                    self.db, self.inputs)
        self._graph = build_graph(
            self.inputs / "schema.json",
            self.inputs / "queryable.json",
            self.inputs / "concept_type.json",
        )

        rules_path = self.inputs / "schema_rules.json"
        self._rules = _load_schema_rules(rules_path)
        if (self.inputs / "schema_rules.json").exists():
            logger.info("[schema_kg_planner:%s] Loaded schema_rules.json (+shared_maps)",
                        self.db)
        else:
            logger.warning("[schema_kg_planner:%s] schema_rules.json not found — "
                           "running with shared_maps only", self.db)

        logger.info("[schema_kg_planner:%s] Computing column embeddings …", self.db)
        emb = compute_embeddings(self._graph)
        dim = next(iter(emb.values())).shape[0]

        self._qdrant = QdrantClient(":memory:")
        self._qdrant.create_collection(
            self.collection,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )
        self._qdrant.upsert(
            self.collection,
            [
                qmodels.PointStruct(id=i, vector=v.tolist(), payload={"col_id": cid})
                for i, (cid, v) in enumerate(emb.items())
            ],
        )
        logger.info("[schema_kg_planner:%s] Ready — %d columns in Qdrant (dim=%d)",
                    self.db, len(emb), dim)

    # -- planning -------------------------------------------------------------

    def plan_query_pruned(self, question: str,
                          col_selection_note: str = "",
                          mapper_note: str = "",
                          tiebreaker_note: str = "") -> Optional[dict]:
        """Run the full schema_kg pipeline on a question (synchronous).

        col_selection_note / mapper_note / tiebreaker_note are optional per-DB
        LLM rules (from resources/prompts/db_llm_rules.yaml), each appended to a
        distinct LLM in this pipeline:
          col_selection → query_expander (column-selection LLM)
          mapper        → value_mapper mapper_1/mapper_2 (entity-extraction LLM)
          tiebreaker    → value_mapper orchestrator (dual-mapper DISAGREEMENT
                          RESOLVER — the tie-breaker between mapper_1 and mapper_2)
        Carried via a per-request SHALLOW COPY of self._rules (the loaded
        schema_rules dict is never mutated). Empty → byte-identical prompts.

        Returns a plan dict, or None when 0 ANN hits (DB-irrelevant query).
        """
        self._ensure_loaded()

        from schema_kg.src.hybrid_retrieval import retrieve_columns

        eff_rules = self._rules
        _cs = col_selection_note.strip() if col_selection_note else ""
        _mp = mapper_note.strip() if mapper_note else ""
        _tb = tiebreaker_note.strip() if tiebreaker_note else ""
        if _cs or _mp or _tb:
            eff_rules = {**(self._rules or {})}
            if _cs:
                eff_rules["_col_selection_note"] = _cs
            if _mp:
                eff_rules["_mapper_note"] = _mp
            if _tb:
                eff_rules["_tiebreaker_note"] = _tb

        kept, meta = retrieve_columns(
            question, self._qdrant, self.collection, self._graph,
            with_mapping=True, rules=eff_rules,
        )
        if not kept:
            logger.info("[schema_kg_planner:%s] 0 ANN hits for %r",
                        self.db, question[:80])
            return None

        parsed_value = meta.get("parsed_value") or {}
        plan = assemble_pruned_plan(
            kept, parsed_value, self._graph, self.db,
            question=question,
            clean_query=meta.get("clean_query_1", ""),
            mapper_agreement=meta.get("mapper_agreement", True),
            orchestrator_used=meta.get("orchestrator_used", False),
            rules=self._rules,
        )
        # STRING-specific: when the LLM filter chose ppi_physical but the question
        # uses bare "interact" / "interaction" / "link" without physical qualifiers,
        # swap the plan to ppi_association.  The LLM filter often misroutes functional
        # interaction questions (e.g. "Do LSM4 and LSM6 interact with SMN?") to
        # ppi_physical because it associates RNA-binding proteins with complexes.
        # ppi_association is bidirectionally expanded in-memory and captures co-IP
        # evidence (experimental score in ppi_detailed_channels), so functional
        # interaction edges (score≥700) are always there even if physical score < 700.
        if plan and self.db == "string":
            plan = _string_physical_to_association_override(question, plan)
            plan = _string_genesym_to_ppi_override(question, plan)
        return plan

    def to_production_plan(self, pruned_plan: dict) -> dict:
        return to_production_plan(pruned_plan, db=self.db)

    async def warm(self) -> None:
        """Fire-and-forget: load model + build Qdrant index before first query."""
        try:
            await asyncio.to_thread(self._ensure_loaded)
            logger.info("[schema_kg_planner:%s] Pre-warm complete", self.db)
        except Exception as exc:
            logger.warning("[schema_kg_planner:%s] Pre-warm failed "
                           "(queries will lazy-load): %s", self.db, exc)


# ── Registry ──────────────────────────────────────────────────────────────────────

_planners: dict[str, SchemaKgPlanner] = {}
_registry_lock = threading.Lock()


def get_planner(db: str) -> SchemaKgPlanner:
    """Return the cached SchemaKgPlanner for `db`, creating it if needed."""
    p = _planners.get(db)
    if p is None:
        with _registry_lock:
            p = _planners.get(db)
            if p is None:
                p = SchemaKgPlanner(db)
                _planners[db] = p
    return p


__all__ = [
    "SchemaKgPlanner", "get_planner",
    "to_production_plan",
    "assemble_pruned_plan", "build_schema_graph", "get_graph",
]
