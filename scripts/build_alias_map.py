#!/usr/bin/env python3
"""Build per-DB alias→canonical maps for entity resolution.

Many DBs ship their OWN alias/xref table (STRING `protein_alias_string`, etc.)
that maps a protein/gene/drug's many names → its record, but the resolver only
queries EXTERNAL KBs (HGNC/UniProt/…) which track canonical gene symbols, not
biochemistry nicknames ("p110α", "zyxin", "junctin"). Those nicknames live ONLY
in the DB's own alias table — and it was never consulted for INPUT resolution.

This builds `resources/values/alias_map_<db>.pkl` = ::

    { canonical_field: { normalized_alias: canonical_value } }

so the resolver (expand_synonyms) can map an unknown term to the DB's canonical
value (e.g. "p110alpha" → "PIK3CA") before DB-overlap filtering.

Generic & schema-driven (no per-DB hardcoding):
  * alias column  = any column typed `xref_id` in concept_type.json
  * canonical col = a column typed as a name concept (CANONICAL_TYPES) in a table
    that shares a join key with the alias table (the FK — found as the common
    column, no FK-string parsing needed)
  * join alias→canonical on that shared key, keyed by the canonical column NAME
    (which matches the resolver's parsed_value field, e.g. "gene_symbol")

Precision controls (drop noise, keep high-value synonyms):
  * drop structural identifiers as alias values (ENSP/ENSG/accessions/RefSeq/
    transcript-suffixed/pure-digit) — see _STRUCTURAL_RX
  * drop ambiguous aliases (one normalized alias → >1 distinct canonical value)
  * drop aliases that are just a case-variant of their own canonical (already
    resolvable by the existing case-insensitive DB-overlap)
  * drop aliases that collide with a DIFFERENT entity's own current canonical
    value (e.g. NCBI Gene's synonym dump lists "HTT" as a historical alias of
    SLC6A4/serotonin-transporter, but "HTT" is ALSO the distinct, current,
    official symbol of the Huntingtin gene — an exact-current-symbol match
    must always take precedence over a cross-entity/cross-species alias, or
    a literal query for "HTT" would silently pull in SLC6A4's rows too).
    General precedence rule: exact canonical match wins over alias-derived
    mapping — applies to any CANONICAL_TYPES field, not just gene_symbol.

Tables are read through each DB's own loader (return_preprocessed_<db>) so the
served column names match schema.json/concept_type.json (the on-disk parquets
may carry pre-rename names like `string_id`).

Usage:
  python scripts/build_alias_map.py            # all DBs with an xref_id column
  python scripts/build_alias_map.py string     # specific DBs only
"""
from __future__ import annotations

import importlib
import json
import pickle
import re
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
SCHEMA_ROOT = ROOT / "evaluation" / "schema_kg" / "inputs"
PKL_DIR = ROOT / "resources" / "values"

# Name-like concept types that an alias can resolve TO (a resolver-queryable
# field). gene_symbol is the STRING case; the rest generalize to other DBs.
CANONICAL_TYPES = {
    "gene_symbol", "gene_name", "drug_name", "disease_name", "protein_name",
    "pathway_name", "rna_name", "phenotype_name", "target_name",
}

# Alias VALUES that are structural identifiers, not human-readable synonyms.
_STRUCTURAL_RX = re.compile(
    r"^(ENS[PGT]\d|CCDS\d|[A-NR-Z]\d{5}|[OPQ]\d[A-Z0-9]{3}\d|N[MPRG]_|X[MP]_|\d+$)"
    r"|[-_]\d{2,}$|\.\d+$",
    re.I,
)


def _load_db_tables(db: str) -> dict:
    """Return {table_name: polars.DataFrame} via the DB's own loader.

    Some loaders (e.g. CTD) stash extra precomputed helper objects (plain
    dicts/sets keyed with a leading underscore, e.g. `_active_drug_ids`)
    alongside the real tables in the same returned mapping. Skip anything
    that isn't a DataFrame/LazyFrame so downstream `.columns` access doesn't
    crash on a non-table value.
    """
    mod = importlib.import_module(f"tools.{db}.app.database_loader")
    fn = getattr(mod, f"return_preprocessed_{db}")
    raw = fn()[db]
    out = {}
    for name, lf in raw.items():
        if isinstance(lf, pl.LazyFrame):
            out[name] = lf.collect()
        elif isinstance(lf, pl.DataFrame):
            out[name] = lf
        # else: helper object (dict/set/etc.) — not a table, skip.
    return out


def _concept_types(db: str) -> dict:
    p = SCHEMA_ROOT / db / "concept_type.json"
    if not p.exists():
        return {}
    return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}


def build_db_alias_map(db: str) -> dict:
    """Return {canonical_field: {norm_alias: canonical_value}} for *db* ({} if N/A)."""
    ctypes = _concept_types(db)
    # alias columns: <db>.<table>.<col> typed alias_id — a narrow concept type
    # reserved for columns in dedicated synonym/alias tables (e.g. protein_alias,
    # gene_synonyms_association) that map an alternate name to a canonical value.
    # xref_id is intentionally NOT used here: it covers non-queryable cross-reference
    # fields (CAS numbers, review_status, description, etc.) that must not feed alias
    # resolution even though they are also non-queryable identifiers.
    alias_cols = [
        tuple(k.split(".")[1:]) for k, v in ctypes.items()
        if v == "alias_id" and len(k.split(".")) == 3
    ]
    if not alias_cols:
        return {}

    tables = _load_db_tables(db)
    out: dict[str, dict] = {}

    for alias_table, alias_col in alias_cols:
        adf = tables.get(alias_table)
        if adf is None or alias_col not in adf.columns:
            print(f"  [skip] {db}.{alias_table}.{alias_col}: table/col not loaded")
            continue

        # canonical = a CANONICAL_TYPES column in a table sharing a join key.
        # Collect ALL candidates then prefer master tables over association tables —
        # e.g. disease_synonyms_association shares disease_id with BOTH
        # disease_pathway_association (gene_symbol column) AND disease_master_table
        # (disease_name column); we must pick the master, not whichever comes first
        # in ctypes iteration order.
        candidates: list[tuple[str, str, str]] = []  # (table, join_key, canonical_col)
        for k, v in ctypes.items():
            if v not in CANONICAL_TYPES:
                continue
            _, ctable, ccol = k.split(".")
            mdf = tables.get(ctable)
            if mdf is None or ccol not in mdf.columns:
                continue
            # Exclude the alias column itself as a join key — prevents joining
            # two synonym tables that happen to share the "synonym" column, and
            # prevents a self-join when the alias col lives in the master table
            # (e.g. cas_rn in chemical_master). Also skip when the first shared
            # column IS the canonical column (jkey == ccol → self-join produces
            # duplicate column names in Polars).
            shared = [c for c in adf.columns if c in mdf.columns and c != alias_col]
            if shared and shared[0] != ccol:
                candidates.append((ctable, shared[0], ccol))
        # Prefer master tables (name contains "master") over association tables.
        candidates.sort(key=lambda t: (0 if "master" in t[0] else 1))
        target = candidates[0] if candidates else None
        if target is None:
            print(f"  [skip] {db}.{alias_table}: no canonical table shares a join key")
            continue
        ctable, jkey, ccol = target

        j = (
            adf.select([jkey, alias_col])
            .join(tables[ctable].select([jkey, ccol]), on=jkey, how="inner")
            .with_columns([
                pl.col(alias_col).str.strip_chars().str.to_lowercase().alias("_na"),
                pl.col(ccol).cast(pl.Utf8).alias("_cn"),
            ])
            .filter(pl.col("_na").str.len_chars() >= 2)
            .filter(~pl.col(alias_col).cast(pl.Utf8)
                    .map_elements(lambda s: bool(_STRUCTURAL_RX.search(s)), return_dtype=pl.Boolean))
        )

        # Restrict to canonicals that appear in at least one non-master, non-alias
        # table (i.e. entities with actual data in the DB). This removes organism-
        # specific variants (MYC.L, SLC9A6B, …) that share a synonym with the human
        # canonical (MYC, SLC9A6) but have no associations in the DB, resolving
        # multi-organism ambiguity without any DB-specific hardcoding.
        active_canonicals: set[str] = set()
        for tname, tdf in tables.items():
            if tname in {alias_table, ctable}:
                continue
            if jkey not in tdf.columns:
                continue
            active_ids = set(tdf[jkey].cast(pl.Utf8).unique().to_list())
            vals = (
                tables[ctable]
                .filter(pl.col(jkey).cast(pl.Utf8).is_in(active_ids))
                .select(ccol)[ccol]
                .cast(pl.Utf8)
                .to_list()
            )
            active_canonicals.update(vals)
        if active_canonicals:
            j = j.filter(pl.col("_cn").is_in(list(active_canonicals)))
            print(f"  active-canonical filter: {len(active_canonicals)} distinct {ccol} with data")

        # unambiguous (1 canonical), and not a mere case-variant of itself
        g = (
            j.group_by("_na")
            .agg(pl.col("_cn").n_unique().alias("_n"), pl.col("_cn").first().alias("_v"))
            .filter(pl.col("_n") == 1)
            .filter(pl.col("_na") != pl.col("_v").str.to_lowercase())
        )

        # Cross-entity collision guard: drop any alias whose normalized form is
        # ALSO — under a completely independent row — some OTHER entity's own
        # current canonical value in the same table (e.g. "htt" is a legitimate
        # historical alias of SLC6A4 in CTD's own synonym dump, but "HTT" is
        # ALSO the distinct current gene_symbol of Huntingtin). Without this
        # guard, a literal query for the foreign entity's own exact symbol
        # would be silently rewritten to the alias's target, pulling in the
        # wrong gene's rows (CFH/FH, HTT/SLC6A4 pattern). Exact-current-symbol
        # match always wins over an alias-derived cross mapping — this is a
        # general precedence rule, not a per-entity exclusion list.
        all_canon_lower = set(
            tables[ctable][ccol]
            .cast(pl.Utf8)
            .drop_nulls()
            .str.strip_chars()
            .str.to_lowercase()
            .to_list()
        )
        before_n = g.height
        g = g.filter(
            (~pl.col("_na").is_in(list(all_canon_lower)))
            | (pl.col("_na") == pl.col("_v").str.to_lowercase())
        )
        dropped_n = before_n - g.height
        if dropped_n:
            print(f"  cross-entity collision guard: dropped {dropped_n} alias(es) "
                  f"that collide with another entity's own current {ccol}")

        amap = dict(zip(g["_na"].to_list(), g["_v"].to_list()))
        out.setdefault(ccol, {}).update(amap)
        print(f"  {db}.{alias_table}.{alias_col} → {ctable}.{ccol}: "
              f"{len(amap)} aliases (from {j.height} joined rows)")

    return out


def main(dbs: list | None = None) -> None:
    if dbs is None:
        dbs = sorted(
            p.name for p in SCHEMA_ROOT.iterdir()
            if p.is_dir() and (p / "concept_type.json").exists()
            and "xref_id" in (p / "concept_type.json").read_text()
        )
    print(f"Building alias maps for: {dbs}\n")
    PKL_DIR.mkdir(parents=True, exist_ok=True)
    for db in dbs:
        print(f"{'='*60}\n{db}")
        try:
            amap = build_db_alias_map(db)
        except Exception as e:
            print(f"  [ERROR] {db}: {e}\n")
            continue
        if not amap:
            print(f"  no alias map (no xref_id/canonical pair)\n")
            continue
        out = PKL_DIR / f"alias_map_{db}.pkl"
        with open(out, "wb") as f:
            pickle.dump(amap, f)
        total = sum(len(v) for v in amap.values())
        print(f"  → {list(amap.keys())} / {total} aliases  →  {out.name}\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args if args else None)
