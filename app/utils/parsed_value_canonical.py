"""LLM-agnostic field normalization for parsed_value.

Maps synonym fields (which different LLMs prefer) to canonical names so
downstream routing code can look in ONE place.

Reasoning: gpt-oss:20b puts "EGFR" in `gene_name`; gpt-5.4-nano puts it
in `target_name`. Both are correct, but our routing was tuned for one.
This shim normalizes both to the canonical `gene_symbol` (the short HGNC
code is the canonical join key across every DB in BioChirp after the
2026-05-15 schema migration).
"""
from typing import Any

# Map: source field -> canonical field
ALIAS_MAP: dict[str, str] = {
    # Gene aliases -> gene_symbol (the canonical short HGNC code)
    "gene_name": "gene_symbol",
    "target_name": "gene_symbol",
    # Drug aliases -> drug_name
    "chemical_name": "drug_name",
    "compound_name": "drug_name",
    # Disease aliases -> disease_name
    "phenotype_name": "disease_name",   # CAREFUL: only when DB doesn't have native phenotype_name field
    "condition_name": "disease_name",
    # Variant aliases -> variant_name
    "mutation_name": "variant_name",
    # Protein aliases -> protein_name
    "uniprot_accession": "accession",   # uniprot prefers accession
    "entry_name": "accession",
}

# Some DBs DO have a real distinct field for the alias.
# Examples:
#   - HPO has phenotype_name distinct from disease_name; don't merge.
# Per-DB skip list:
ALIAS_PER_DB_SKIP: dict[str, set[str]] = {
    "hpo": {"phenotype_name"},
    "clinvar": {"variant_name"},  # clinvar has its own variant column; don't fold
    # 2026-06-18: orphanet's gene_name no-collapse entry REMOVED. The v2 parquet
    # dropped gene_name (so it's no longer in database_schemas / the loaded data),
    # so collapsing gene_name → gene_symbol now correctly routes orphanet gene
    # queries to the real gene_symbol column instead of leaving an unsupported
    # gene_name filter that post_expand would drop.
    # TTD's _pre_expand (app/tools/ttd/app/ttd.py) handles the
    # target_name ↔ gene_symbol mirror itself, gated by an HGNC-shape
    # check via _looks_hgnc so family tokens like "tyrosine kinase" /
    # "GPCR" / "kinase" stay in target_name and do NOT bleed into
    # gene_symbol. The unconditional copy here would un-gate that
    # protection and produce zero-row joins on family queries.
    "ttd":         {"target_name"},
}


def canonicalize(parsed_value: dict[str, Any], db: str) -> dict[str, Any]:
    """Return a copy of parsed_value with canonical fields ADDED (not replacing).

    NON-DESTRUCTIVE (fix 2026-05-13):
    The earlier destructive version (null-ing src after copying to canonical)
    broke each DB tool's routing code that reads source fields directly
    (hcdt.py reads gene_symbol; clinvar.py reads variant_name). Pass rate
    regressed from 97.7% → 25.9% in the canonical bench.

    New behavior: COPY src → canonical when canonical is missing, but
    LEAVE src intact so legacy per-DB routing still fires. The canonical
    field becomes additional context, not replacement. Any new LLM-agnostic
    code can read canonical; legacy code keeps reading src; both work.

    Behavior:
      - For each (src, canonical) pair in ALIAS_MAP:
        - If db is in ALIAS_PER_DB_SKIP and src is in skip set: leave both alone.
        - If src has value and canonical is empty: copy src into canonical.
        - If canonical=="requested" and src has real value: replace canonical with src.
        - Otherwise: leave both alone.
      - NEVER null out src — legacy routing depends on it.
    """
    if not isinstance(parsed_value, dict):
        return parsed_value
    pv = dict(parsed_value)  # shallow copy
    skip = ALIAS_PER_DB_SKIP.get(db.lower(), set())
    for src, canonical in ALIAS_MAP.items():
        if src in skip:
            continue
        src_val = pv.get(src)
        canonical_val = pv.get(canonical)
        if src_val is None or src_val == "":
            continue
        # canonical already populated with a real value: leave both alone
        if canonical_val and canonical_val != "requested" and src_val != "requested":
            continue
        # canonical is empty or just "requested" sentinel: populate it with src value
        if canonical_val == "requested" and src_val != "requested":
            pv[canonical] = src_val
        elif not canonical_val:
            pv[canonical] = src_val
        # IMPORTANT: do NOT null pv[src]. Legacy per-DB routing reads it.
    return pv
