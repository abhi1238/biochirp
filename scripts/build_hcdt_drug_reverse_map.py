#!/usr/bin/env python3
"""Add hcdt.drug_name to synonym_reverse_map.pkl.

Parses HCDT drug_master_table_v2.parquet drug_synonyms (pipe-separated) and
builds {synonym_lower: [canonical_drug_name]} so brand names like "Eliquis"
resolve to the HCDT-canonical INN "Apixaban" even when external KB APIs are down.

Safe to re-run: overwrites only the hcdt.drug_name key in the existing pkl.

Usage:
    python scripts/build_hcdt_drug_reverse_map.py
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "database" / "hcdt" / "drug_master_table_v2.parquet"
PKL_PATH = ROOT / "resources" / "values" / "synonym_reverse_map.pkl"


def build_drug_name_map(parquet_path: Path) -> dict[str, list[str]]:
    df = pl.read_parquet(parquet_path, columns=["drug_name", "drug_synonyms"])

    rev: dict[str, list[str]] = defaultdict(list)
    seen_per_key: dict[str, set[str]] = defaultdict(set)

    for drug_name, drug_synonyms in df.iter_rows():
        if not drug_name:
            continue
        canonical = drug_name.strip()
        if not canonical:
            continue

        if drug_synonyms:
            tokens = [t.strip() for t in drug_synonyms.split("|") if t.strip()]
        else:
            tokens = []

        # Always include the canonical name itself so case variants resolve.
        tokens.append(canonical)

        for token in tokens:
            key = token.lower()
            if canonical not in seen_per_key[key]:
                seen_per_key[key].add(canonical)
                rev[key].append(canonical)

    return dict(rev)


def main() -> None:
    print(f"Reading {PARQUET} ...")
    drug_map = build_drug_name_map(PARQUET)
    print(f"Built hcdt.drug_name map: {len(drug_map):,} synonym entries")

    print(f"Loading {PKL_PATH} ...")
    with open(PKL_PATH, "rb") as f:
        rev_map: dict = pickle.load(f)

    prev_count = len(rev_map.get("hcdt", {}).get("drug_name", {}))
    rev_map.setdefault("hcdt", {})["drug_name"] = drug_map
    new_count = len(rev_map["hcdt"]["drug_name"])
    print(f"hcdt.drug_name: {prev_count:,} → {new_count:,} entries")

    with open(PKL_PATH, "wb") as f:
        pickle.dump(rev_map, f, protocol=4)
    print(f"Saved {PKL_PATH}")

    # Spot-check a few brand names
    checks = [("eliquis", "Apixaban"), ("valtrex", "Valacyclovir"),
               ("viagra", "SILDENAFIL"), ("gleevec", "Imatinib"), ("iressa", "Gefitinib")]
    print("\nSpot-checks:")
    for alias, expected in checks:
        hits = drug_map.get(alias, [])
        ok = any(h.lower() == expected.lower() for h in hits)
        print(f"  {'✓' if ok else '✗'} {alias!r} → {hits[:3]}")


if __name__ == "__main__":
    main()
