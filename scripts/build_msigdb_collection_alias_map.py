#!/usr/bin/env python3
"""Build MSigDB collection_alias_map from geneset_master_table parquet.

The parquet stores 10 canonical friendly-name collection values:
  Hallmark, Positional, Curated, Regulatory, Computational, Ontology,
  Oncogenic, Immunologic, CellType, CellLineage

This script produces a map of all aliases → canonical collection value
that are DERIVABLE from parquet data (i.e. the canonical values themselves
in different case/whitespace normalisations).

Aliases that are NOT in the parquet (legacy MSigDB codes H, C1-C9, MH,
natural-language synonyms like "canonical pathways", "gene ontology", etc.)
are explicitly NOT included — those are external knowledge, not MSigDB data.

Usage:
  python scripts/build_msigdb_collection_alias_map.py
  python scripts/build_msigdb_collection_alias_map.py --print   # print only, no update
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl
import yaml

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "database" / "msigdb" / "geneset_master_table_msigdb_v2.parquet"
SCHEMA_YAML = ROOT / "dbs" / "msigdb" / "schema.yaml"


def build_collection_alias_map(parquet_path: Path) -> dict[str, str]:
    """Read canonical collection values from parquet and return alias → canonical map.

    Only data-backed aliases are included:
    - Identity: canonical → canonical (e.g. Hallmark → Hallmark)
    - Lowercase: lowercase canonical → canonical (e.g. hallmark → Hallmark)
    - Lowercased split-word variants for CamelCase names
      (e.g. cell type → CellType, cell lineage → CellLineage)

    Entries where key == value (pure identity) are omitted as trivially useless.
    """
    df = pl.read_parquet(parquet_path)
    if "collection" not in df.columns:
        raise ValueError(f"'collection' column not found in {parquet_path}")

    canonical_values: list[str] = sorted(df["collection"].drop_nulls().unique().to_list())
    print(f"Canonical collection values from parquet ({len(canonical_values)}):")
    for v in canonical_values:
        print(f"  {v!r}")

    alias_map: dict[str, str] = {}
    for canonical in canonical_values:
        # Lowercase variant (e.g. "curated" → "Curated")
        lc = canonical.lower()
        if lc != canonical:
            alias_map[lc] = canonical

        # Split CamelCase into space-separated lowercase
        # CellType → "cell type", CellLineage → "cell lineage"
        import re
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", canonical)
        space_lc = words.lower()
        if space_lc != canonical and space_lc != lc and space_lc not in alias_map:
            alias_map[space_lc] = canonical

    print(f"\nDerived {len(alias_map)} data-backed aliases (identity aliases excluded):")
    for k, v in sorted(alias_map.items()):
        print(f"  {k!r} → {v!r}")
    return alias_map


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print the alias map without updating schema.yaml")
    args = parser.parse_args()

    alias_map = build_collection_alias_map(PARQUET)

    if args.print_only:
        print("\n(--print mode: schema.yaml not updated)")
        return

    # Load schema.yaml and replace the collection_alias_map block
    schema_text = SCHEMA_YAML.read_text()
    data = yaml.safe_load(schema_text)

    rules = data.get("rules", {})
    if alias_map:
        rules["collection_alias_map"] = alias_map
    elif "collection_alias_map" in rules:
        del rules["collection_alias_map"]

    # Remove gene_alias_map (not MSigDB-specific data)
    if "gene_alias_map" in rules:
        del rules["gene_alias_map"]
        print("\nRemoved gene_alias_map (generic biomedical knowledge, not MSigDB parquet-backed)")

    print(f"\nschema.yaml NOT rewritten by this script — run gen_schema.py --write --db msigdb to propagate.")
    print("Update dbs/msigdb/schema.yaml manually or adapt this script to write YAML.")


if __name__ == "__main__":
    main()
