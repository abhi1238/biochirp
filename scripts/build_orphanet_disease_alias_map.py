#!/usr/bin/env python3
"""Build orphanet.disease_name synonym→canonical map in synonym_reverse_map.pkl.

Parses en_product1.xml <SynonymList><Synonym> entries and builds
{synonym_lower: [canonical_disease_name]} so alternate names like
"Marchesani syndrome" resolve to "Weill-Marchesani syndrome".

Safe to re-run: overwrites only the orphanet.disease_name key in the existing pkl.

Usage:
    python scripts/build_orphanet_disease_alias_map.py
"""
from __future__ import annotations

import pickle
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XML_PATH = ROOT / "database" / "orphanet" / "raw" / "en_product1.xml"
PKL_PATH = ROOT / "resources" / "values" / "synonym_reverse_map.pkl"


def build_disease_alias_map(xml_path: Path) -> dict[str, list[str]]:
    rev: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    # Only clear Disorder elements AFTER processing — clearing sub-elements early
    # wipes their text before the Disorder end-event fires (iterparse pitfall).
    for _, dis in ET.iterparse(xml_path, events=("end",)):
        if dis.tag != "Disorder":
            continue

        # Canonical name — Orphanet uses <Name lang='en'>text</Name>
        canonical = ""
        for n in dis.findall("Name"):
            txt = (n.text or "").strip()
            if txt:
                canonical = txt
                break
        if not canonical:
            dis.clear()
            continue

        tokens = [canonical]

        # Collect synonyms from <SynonymList><Synonym lang='en'>
        syn_list = dis.find("SynonymList")
        if syn_list is not None:
            for syn in syn_list.findall("Synonym"):
                txt = (syn.text or "").strip()
                if txt:
                    tokens.append(txt)

        for token in tokens:
            key = token.lower()
            if canonical not in seen[key]:
                seen[key].add(canonical)
                rev[key].append(canonical)

        dis.clear()

    return dict(rev)


def main() -> None:
    if not XML_PATH.exists():
        print(f"ERROR: {XML_PATH} not found — download en_product1.xml from orphadata.org first")
        sys.exit(1)

    print(f"Parsing {XML_PATH} ...")
    disease_map = build_disease_alias_map(XML_PATH)
    print(f"Built orphanet.disease_name map: {len(disease_map):,} synonym entries")

    # Load existing pkl and update only the orphanet.disease_name key
    if PKL_PATH.exists():
        with open(PKL_PATH, "rb") as f:
            store = pickle.load(f)
    else:
        store = {}

    store.setdefault("orphanet", {})["disease_name"] = disease_map

    with open(PKL_PATH, "wb") as f:
        pickle.dump(store, f, protocol=4)
    print(f"Saved → {PKL_PATH}")
    print("Restart biochirp_expand_and_match_db_tool to activate.")


if __name__ == "__main__":
    main()
