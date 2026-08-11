"""Partner-role denormalization for self-referential databases.

Some DBs have a fact table that references the SAME master/dimension table
through TWO foreign keys with different roles — an anchor (query) side and a
partner side. STRING PPI is the canonical case::

    ppi.protein_id          → protein_master_table.protein_id   (query side)
    ppi.protein_partner_id  → protein_master_table.protein_id   (partner side)

The shared schema_planner does a single star-join per dimension table, keyed on
the anchor FK, so only the anchor protein's attributes (e.g. protein_size) reach
the result df — partner-side attributes are invisible. Teaching the planner to
self-join one table twice would touch the shared join path every DB depends on
(high blast radius). Instead we denormalize the partner side at load time: a
left-join of the master's SCALAR attributes onto the partner FK, emitted as
``<prefix><attr>`` columns.

This is:
  * non-breaking — purely additive columns; the planner/join/text2sql code is
    untouched, and DBs that never call this get nothing new;
  * generic — driven by explicit (partner_fk, master_key, attrs) params, no
    per-DB logic inside; any pairwise/self-referential DB (PPI, drug-drug
    interaction, gene-gene) reuses it;
  * cheap — lazy; the join is folded into the existing LazyFrame plan.

Keep ``attrs`` to small scalar columns (size, type, symbol). Do NOT project
large free-text blobs (annotations) — they bloat every fact row.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl

logger = logging.getLogger("uvicorn.error")

# schema.json encodes foreign keys in the column description as
# "... FK → <master_table>.<master_key>" (Unicode arrow; ASCII "->" tolerated).
_FK_RE = re.compile(r"FK\s*[→\-]?>?\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)")
# Role markers that distinguish the partner side of a self-referential edge from
# the anchor side. Conventional across pairwise DBs (partner / _b / _2 / target2).
_PARTNER_HINT = re.compile(r"partner|_b$|_b_|(?<![a-z])b_gene|_2$|2_gene|target2|second", re.I)
# A master attribute longer than this (max chars over the column) is treated as a
# free-text blob and NOT denormalized — keeps fact rows lean (annotations, seqs).
_MAX_SCALAR_LEN = 80


def add_partner_attributes(
    fact: pl.LazyFrame,
    master: pl.LazyFrame,
    *,
    partner_fk: str,
    master_key: str,
    attrs: Sequence[str],
    prefix: str = "partner_",
) -> pl.LazyFrame:
    """Left-join ``master``'s ``attrs`` onto ``fact`` via the partner-side FK.

    Each projected attribute ``a`` lands as ``f"{prefix}{a}"``. The join is a
    left-join on ``fact[partner_fk] == master[master_key]`` after de-duplicating
    ``master`` on its key (so a 1:1 lookup can never explode rows).

    Returns ``fact`` unchanged (logged) when ``partner_fk`` is absent, ``master``
    lacks ``master_key``, or none of ``attrs`` exist / all are already present.
    """
    fact_cols = set(fact.collect_schema().names())
    master_cols = set(master.collect_schema().names())

    if partner_fk not in fact_cols:
        logger.debug("partner_denorm: fact has no '%s'; skipping", partner_fk)
        return fact
    if master_key not in master_cols:
        logger.debug("partner_denorm: master has no '%s'; skipping", master_key)
        return fact

    want = [
        a for a in attrs
        if a in master_cols and f"{prefix}{a}" not in fact_cols
    ]
    if not want:
        logger.debug("partner_denorm: nothing to add for fk=%s", partner_fk)
        return fact

    rename_map = {master_key: partner_fk}
    rename_map.update({a: f"{prefix}{a}" for a in want})
    lookup = (
        master.select([master_key, *want])
        .unique(subset=[master_key])
        .rename(rename_map)
    )
    out = fact.join(lookup, on=partner_fk, how="left")
    logger.info(
        "partner_denorm: added %s via %s->%s",
        [f"{prefix}{a}" for a in want], partner_fk, master_key,
    )
    return out


def _load_schema(db: str, schema: Mapping | None) -> dict:
    """Return {table: {col: description}} for *db* from the passed schema or
    schema_kg/inputs/<db>/schema.json. {} when unreachable."""
    if schema:
        return dict(schema)
    cands = [
        Path("/app/schema_kg/inputs") / db / "schema.json",
        Path(__file__).resolve().parents[2] / "evaluation" / "schema_kg" / "inputs" / db / "schema.json",
    ]
    root = os.getenv("SCHEMA_KG_INPUTS_ROOT")
    if root:
        cands.insert(0, Path(root) / db / "schema.json")
    for p in cands:
        try:
            if p.is_file():
                return json.loads(p.read_text()).get(db, {}) or {}
        except Exception:
            continue
    return {}


def _scalar_attrs(master: pl.LazyFrame, master_key: str, fact_cols: set[str]) -> list[str]:
    """Master columns worth denormalizing onto a partner: not the key, not a
    long free-text blob, and not already represented (by name) in the fact table.
    A master col ``c`` is considered already represented if any fact column name
    contains ``c`` (e.g. master ``gene_symbol`` vs fact ``channel_partner_gene_symbol``)."""
    master_cols = [c for c in master.collect_schema().names() if c != master_key]
    cands = [c for c in master_cols if not any(c in fc for fc in fact_cols)]
    if not cands:
        return []
    # Data-driven blob filter: drop columns whose longest value exceeds the
    # scalar threshold. One cheap aggregation over the (small) master table.
    try:
        lens = master.select(
            [pl.col(c).cast(pl.Utf8).str.len_chars().max().alias(c) for c in cands]
        ).collect().row(0, named=True)
        return [c for c in cands if (lens.get(c) or 0) <= _MAX_SCALAR_LEN]
    except Exception:
        return cands


def auto_add_partner_attributes(
    tables: dict[str, pl.LazyFrame],
    *,
    db: str,
    schema: Mapping | None = None,
    prefix: str = "partner_",
) -> dict[str, pl.LazyFrame]:
    """Auto-detect the "one master table reachable via two FKs (anchor + partner)"
    pattern from schema.json FK metadata and denormalize the partner side.

    For each fact table whose column descriptions declare ≥2 FK columns pointing
    at the SAME master table, where one column is the anchor and another is
    role-marked as the partner (``_PARTNER_HINT``), this left-joins the master's
    scalar attributes onto the partner FK as ``<prefix><attr>`` (via
    :func:`add_partner_attributes`). Attributes are discovered automatically:
    every master column that isn't the key, isn't already represented in the
    fact table, and isn't a long free-text blob.

    Mutates and returns ``tables`` (keys are the loader's table names, typically
    db-suffixed e.g. ``ppi_association_string``). No-op when the FK pattern is
    absent, so it is safe to call from any DB loader.
    """
    sch = _load_schema(db, schema)
    if not sch:
        logger.debug("partner_denorm[auto:%s]: no schema; skipping", db)
        return tables

    # Map each schema table name to the loader's actual dict key (schema names
    # may be bare or db-suffixed; loader keys are usually suffixed).
    def _resolve(tbl: str) -> str | None:
        for k in (tbl, f"{tbl}_{db}", tbl.removesuffix(f"_{db}")):
            if k in tables:
                return k
        return None

    for fact_tbl, colmap in sch.items():
        if not isinstance(colmap, dict):
            continue
        fact_key = _resolve(fact_tbl)
        if fact_key is None:
            continue

        # column -> (master_table, master_key) parsed from FK descriptions
        fks: dict[str, tuple[str, str]] = {}
        for col, desc in colmap.items():
            m = _FK_RE.search(desc or "")
            if m:
                fks[col] = (m.group(1), m.group(2))

        by_master: dict[tuple[str, str], list[str]] = defaultdict(list)
        for col, mtk in fks.items():
            by_master[mtk].append(col)

        for (master_tbl, master_key), fk_cols in by_master.items():
            if len(fk_cols) < 2:
                continue
            partner_cols = [c for c in fk_cols if _PARTNER_HINT.search(c)]
            anchor_cols = [c for c in fk_cols if c not in partner_cols]
            if not partner_cols or not anchor_cols:
                continue  # roles ambiguous — refuse to guess
            master_key_resolved = _resolve(master_tbl)
            if master_key_resolved is None:
                continue
            master_lf = tables[master_key_resolved]
            fact_lf = tables[fact_key]
            fact_cols = set(fact_lf.collect_schema().names())
            attrs = _scalar_attrs(master_lf, master_key, fact_cols)
            if not attrs:
                continue
            for pc in partner_cols:
                fact_lf = add_partner_attributes(
                    fact_lf, master_lf,
                    partner_fk=pc, master_key=master_key, attrs=attrs, prefix=prefix,
                )
            tables[fact_key] = fact_lf
            logger.info(
                "partner_denorm[auto:%s]: %s.%s ← %s%s from %s",
                db, fact_key, partner_cols, prefix, attrs, master_key_resolved,
            )
    return tables


__all__ = ["add_partner_attributes", "auto_add_partner_attributes"]
