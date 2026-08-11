"""BioChirp HCDT data tool — query logic (schema_kg variant).

Orchestration (LLM router, schema_kg planner, web fallback, on-empty retry) is
provided by the shared `app.per_db_tool.schema_kg_worker`. This module carries
only HCDT-specific data + hooks injected via `SchemaKgConfig`.
"""
import re
import threading
from typing import Optional

import polars as pl

from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_hcdt

# ---------------------------------------------------------------------------
# HCDT synonym map — trade name / investigational code → canonical INN
# Built lazily on first request from the loaded parquets; cached module-level.
# Keys are lowercase synonyms (filtered to human-readable, 4-80 chars).
# Values are canonical drug_name strings from drug_master_table.
# ---------------------------------------------------------------------------
_SYN_MAP: Optional[dict] = None
_SYN_MAP_LOCK = threading.Lock()

# Patterns that indicate a chemical string (SMILES/InChI) rather than a
# human-readable trade name. We intentionally keep round-bracket suffixes like
# "(TM)", "(R)", "(HCl)" which are legitimate parts of brand names.
# Signals: double/triple bonds (=,#), SMILES atom brackets ([Na+]), stereochemistry
# (@, \, /), or the InChI= literal prefix. A single plain "(" is not enough.
_CHEM_NOISE_RE = re.compile(r"[=\[\]#@\\/]|^InChI=", re.ASCII)

# Punctuation to strip from query-word edges before synonym dict lookup so
# "Herceptin?" and "(Velcade)" both match their canonical entries.
_PUNCT_STRIP_RE = re.compile(r"^[^\w-]+|[^\w-]+$")


def _build_syn_map(data: dict) -> dict:
    """Collect (lowercase synonym → canonical drug_name) from loaded HCDT tables.

    Filters out SMILES/InChI strings and very short/long tokens so the map
    stays compact and avoids spurious substring matches on chemical notation.
    """
    import logging
    import polars as pl

    log = logging.getLogger("uvicorn.error")
    # Unwrap outer {db_name: {tables}} wrapper produced by clean_table_dict()
    tables = data.get("hcdt") or data
    syn_tbl    = tables.get("drug_synonyms_association_hcdt")
    master_tbl = tables.get("drug_master_table_hcdt")
    if syn_tbl is None or master_tbl is None:
        log.warning("[hcdt] synonym map skipped — tables missing from loaded data")
        return {}
    try:
        syn_df    = syn_tbl.collect()    if hasattr(syn_tbl,    "collect") else syn_tbl
        master_df = master_tbl.collect() if hasattr(master_tbl, "collect") else master_tbl
        joined = (
            syn_df
            .join(master_df.select(["drug_id", "drug_name"]), on="drug_id", how="left")
            .filter(pl.col("drug_name").is_not_null() & pl.col("synonym").is_not_null())
            .select(["synonym", "drug_name"])
            .unique()
        )
        result: dict = {}
        for syn, name in joined.iter_rows():
            syn  = (syn  or "").strip()
            name = (name or "").strip()
            if not syn or not name:
                continue
            if len(syn) < 4 or len(syn) > 80:
                continue
            if _CHEM_NOISE_RE.search(syn):
                continue
            key = syn.lower()
            # Prefer shorter canonical name when two synonyms collide on the
            # same lowercase key (edge case from PubChem dedup artefacts).
            if key not in result or len(name) < len(result[key]):
                result[key] = name
        log.info("[hcdt] synonym map built: %d entries", len(result))
        return result
    except Exception as exc:
        log.warning("[hcdt] synonym map build failed: %s", exc)
        return {}


def _get_syn_map(data: dict) -> dict:
    global _SYN_MAP
    if _SYN_MAP is None:
        with _SYN_MAP_LOCK:
            if _SYN_MAP is None:
                _SYN_MAP = _build_syn_map(data)
    return _SYN_MAP


def _rewrite_synonyms(query: str, syn_map: dict) -> str:
    """Replace trade names / investigational codes with canonical INN names.

    Tries n-grams longest-first (up to 5 words) so multi-word brand names
    (e.g. "RTA 408") are matched before single-word fallbacks.
    Strips leading/trailing punctuation from each word before the dict lookup
    so "Herceptin?" and "(Velcade)" still match their entries.
    Preserves the surrounding punctuation in the output token.
    Skips replacements where the canonical name equals the stripped token.
    """
    words = query.split()
    out: list = []
    i = 0
    while i < len(words):
        replaced = False
        for n in range(min(5, len(words) - i), 0, -1):
            # Strip edge punctuation from each word in the n-gram for lookup,
            # but retain the raw words for prefix/suffix reconstruction.
            raw_slice = words[i : i + n]
            stripped = [_PUNCT_STRIP_RE.sub("", w) for w in raw_slice]
            lookup_key = " ".join(stripped).lower()
            canonical = syn_map.get(lookup_key)
            if canonical and canonical.lower() != lookup_key:
                # Carry over leading punctuation of the first word and trailing
                # punctuation of the last word so sentence structure is preserved.
                prefix = raw_slice[0][: len(raw_slice[0]) - len(raw_slice[0].lstrip("(\"'"))]
                suffix = raw_slice[-1][len(raw_slice[-1].rstrip(")?!.,;:'\"")) :]
                out.append(f"{prefix}{canonical}{suffix}")
                i += n
                replaced = True
                break
        if not replaced:
            out.append(words[i])
            i += 1
    return " ".join(out)


async def _hcdt_pre_expand(ctx) -> None:
    """Rewrite trade names / investigational codes in cleaned_query to canonical INN.

    Fires inside the intercept BEFORE the router LLM and schema_mapper see the
    query. Ensures that trade-name queries (e.g. "Herceptin", "Forxiga",
    "RTA 408") are presented to schema_mapper with their canonical INN so the
    ANN match succeeds where it would otherwise return None.
    """
    query = ctx.inp.get("cleaned_query", "")
    if not query or not ctx.data:
        return
    syn_map = _get_syn_map(ctx.data)
    if not syn_map:
        return
    rewritten = _rewrite_synonyms(query, syn_map)
    if rewritten != query:
        ctx.inp["cleaned_query"] = rewritten
        ctx.log.info("[hcdt] pre_expand synonym rewrite: %r → %r",
                     query[:80], rewritten[:80])


def _hcdt_on_schema_map_empty(ctx, rephrased_query: str) -> Optional[str]:
    """Second-chance synonym rewrite when schema_mapper returned None.

    The router LLM may have rephrased a trade name in a way pre_expand did not
    see (e.g. kept "Velcade" after rephrasing). Try the same synonym map on the
    rephrased_query; return the rewritten form so the intercept can retry
    schema_mapper once before falling back to the web tool.
    """
    if not ctx.data:
        return None
    syn_map = _get_syn_map(ctx.data)
    if not syn_map:
        return None
    rewritten = _rewrite_synonyms(rephrased_query, syn_map)
    return rewritten if rewritten != rephrased_query else None


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_hcdt_db = \
    setup_service_globals("hcdt", "High-Confidence Drug-Target database", return_preprocessed_hcdt)


_HCDT_CAPABILITIES = (
    "- Drug-gene target associations (drug names + HGNC gene symbols)\n"
    "- Gene cross-reference identifiers (gene_master_table): UniProt accession, HGNC ID,\n"
    "  Ensembl gene ID, NCBI Entrez ID for a gene.\n"
    "  (query: 'What is the UniProt / HGNC / Ensembl / Entrez ID of [gene]?' MUST be\n"
    "  routed to query_db — these are curated columns, NOT general knowledge.)\n"
    "- Drug-disease indications (drug names + disease names)\n"
    "- Drug-pathway associations (drug names + KEGG pathway names)\n"
    "- Binding affinities: IC50 / Ki / Kd in nM (drug_target_negative table)\n"
    "- Drug-RNA interactions (drug names + RNA names + RNA types)\n"
    "- Drug physicochemical / structural properties (drug_master_table): molecular\n"
    "  weight (drug_mw), molecular formula, InChI, InChIKey, isomeric & canonical\n"
    "  SMILES, IUPAC name.\n"
    "  (query: 'What is the molecular weight / formula / SMILES / InChIKey / IUPAC\n"
    "  name of [drug]?' MUST be routed to query_db — these are curated columns, NOT\n"
    "  general knowledge.)\n"
    "- Drug trade names, brand names, and synonyms (drug_synonyms_association table):\n"
    "  curated per-drug synonym list from PubChem (e.g. Viagra for sildenafil, Gleevec\n"
    "  for imatinib, Tarceva for erlotinib).\n"
    "  (query: 'What is the trade/brand name of [drug]?' or 'What are the synonyms of\n"
    "  [drug]?' MUST be routed to query_db — these are curated DB values, NOT general\n"
    "  knowledge.)\n"
    "- Drug cross-matching data (PubChem CID, ATC codes)\n"
    "- Disease-gene associations: which genes are targeted by drugs used for a disease\n"
    "  (query: 'Which genes are associated with [disease/symptom]?')\n"
    "- Gene-disease associations: which diseases are treated by drugs targeting a gene\n"
    "  (query: 'Which diseases are associated with [gene]?')\n"
    "- Gene-pathway associations: which pathways does a gene participate in\n"
    "  (query: 'Which pathways is [gene] involved in?')\n"
    "- Symptoms and syndromes: 'fever', 'pain', 'inflammation', 'hypertension',\n"
    "  'pyrexia', 'nausea', 'fatigue' are valid disease-name filter values in HCDT.\n"
    "  'Which genes are associated with fever?' MUST be routed to query_db — fever\n"
    "  is a queryable disease name, NOT general biology knowledge.\n"
    "- Indirect / multi-hop links between any of the above entities resolved by\n"
    "  joining through bridging drug/gene tables"
)
_HCDT_LIMITATIONS = (
    "mechanism-of-action text, protein structures, clinical trial data, variant "
    "pathogenicity, gene expression levels, protein-protein interactions, or "
    "general biology knowledge"
)

def _hcdt_negative_binding_note(ctx) -> None:
    """Tag ic50_nm/ki_nm/kd_nm rows with their true provenance.

    drug_target_negative_hcdt is HCDT's ONLY table with binding-affinity
    numbers, and it is a curated negative/counter-screen assay dataset (per
    database_loader.py and the upstream HCDT source), not general potency
    data — there is no separate "positive"/primary-assay affinity table in
    this DB to cross-check against. Without a label, a plain ic50_nm value
    reads as "the" IC50 and can be off by orders of magnitude from a drug's
    real clinical potency (observed: osimertinib vs EGFR — 480,000 nM here
    vs. the drug's actual low-nanomolar potency reported elsewhere).
    """
    df = getattr(ctx, "df", None)
    if df is None or df.is_empty():
        return
    if not any(c in df.columns for c in ("ic50_nm", "ki_nm", "kd_nm")):
        return
    ctx.df = df.with_columns(
        pl.lit(
            "measured in a negative/counter-screen assay context — HCDT has no "
            "separate primary/high-affinity binding table for comparison"
        ).alias("binding_context")
    )


_HCDT_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_hcdt_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_HCDT_CAPABILITIES,
    limitations=_HCDT_LIMITATIONS,
    pre_expand=_hcdt_pre_expand,
    on_schema_map_empty=_hcdt_on_schema_map_empty,
    post_join=_hcdt_negative_binding_note,
)

return_hcdt_result = make_schema_kg_handler(_HCDT_CONFIG)
