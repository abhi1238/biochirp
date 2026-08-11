"""BioChirp MSigDB data tool — schema_kg variant."""
import re
import threading
from typing import Optional

from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_msigdb


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_msigdb_db = \
    setup_service_globals("msigdb", "MSigDB", return_preprocessed_msigdb)


_MSIGDB_CAPABILITIES = (
    "- Gene set / signature catalogue: MSigDB geneset_id, geneset_name, collection, URL, gene_count, organism (geneset_master_table)\n"
    "- Gene membership: which genes belong to each gene set (gene_geneset_association)\n"
    "- Gene registry: distinct gene symbols across all gene sets, per organism (gene_master_table)\n"
    "- Gene set metadata: PMID, GEO ID, contributor, sub-collection, brief/full descriptions (geneset_metadata)\n"
    "- Collections (friendly names, same vocabulary for human and mouse sets): Hallmark, Positional, Curated, Regulatory, Computational, Ontology, Oncogenic, Immunologic, CellType, CellLineage (no legacy codes like H/C1-C9/MH/M1 on disk)\n"
    "- Organisms: Homo sapiens (~34k sets), Mus musculus (~18k sets), Rattus norvegicus (~31 sets)"
)
_MSIGDB_LIMITATIONS = (
    "drug-target associations, drug indications, variant pathogenicity, protein 3D structures, "
    "gene expression levels, protein-protein interactions, pathway reactions or topology, "
    "or general biology knowledge"
)

# Source DB / ontology → geneset_name prefix. In MSigDB the source database is
# encoded as the geneset_name prefix (KEGG_MEDICUS_*, REACTOME_*, GOBP_*, …); it
# is NOT a `collection` value, so "KEGG pathways" / "REACTOME gene sets" / "GO
# biological-process sets" bind no filter under the value-mapper (which extracts
# concrete entity values, not prefix patterns). This hook deterministically maps
# the source-DB keyword in the query to a geneset_name prefix filter, which
# dataframe_filtering substring-matches (geneset_name is in _SUBSTRING_MATCH_COLS).
# Order matters: multi-word phrases before bare tokens.
# Intersection-phrase detector (mirrors _orchestrator.py _INTERSECTION_RX).
# Used to detect "shared between X and Y" intent so organism can be injected.
_MSIGDB_INTERSECTION_RX = re.compile(
    r"\b(both|all of|each of|as well as|simultaneously|at the same time|"
    r"in common|shared by|shared between|common to|in both|overlap between|"
    r"common between|genes shared|shared genes)\b",
    re.I,
)
_MSIGDB_HUMAN_RX = re.compile(r"\b(human|homo sapiens)\b", re.I)
_MSIGDB_MOUSE_RX = re.compile(r"\b(mouse|mus musculus)\b", re.I)

_SOURCE_DB_PREFIXES = [
    ("go biological process", "GOBP_"), ("gobp", "GOBP_"),
    ("go molecular function", "GOMF_"), ("gomf", "GOMF_"),
    ("go cellular component", "GOCC_"), ("gocc", "GOCC_"),
    ("kegg", "KEGG_"), ("reactome", "REACTOME_"), ("biocarta", "BIOCARTA_"),
    ("wikipathways", "WP_"), ("wikipathway", "WP_"),
    ("human phenotype", "HP_"), ("hpo", "HP_"),
    ("microrna", "MIR_"), ("mirna", "MIR_"),
]

# ── Concept-token map (data-driven, like HCDT's synonym map) ─────────────────
# Built lazily from the loaded geneset_master_table parquet on first query.
# Maps {lowercase_query_word: UPPERCASE_TOKEN} where the token is an uppercase
# segment found inside MSigDB geneset_names at frequency 5–300. This range
# captures disease/pathway/condition concepts (LUPUS=30, NOTCH=50,
# INTERFERON=104) while excluding very-rare experiment-specific labels (<5)
# and very-common generic terms (SIGNALING, RESPONSE, REGULATION, >300).
# Source-DB prefixes (GOBP_, REACTOME_, …) are excluded — they are handled
# separately by _SOURCE_DB_PREFIXES above.
_CONCEPT_TOKEN_MAP: Optional[dict] = None
_CONCEPT_TOKEN_LOCK = threading.Lock()

# Generic biological process words that appear frequently in geneset_names but
# are too broad to use as disease/concept filters. Excluded from the map.
_EXCLUDE_GENERIC: frozenset = frozenset({
    "REGULATION", "RESPONSE", "SIGNALING", "PROCESS", "PATHWAY", "ACTIVITY",
    "EXPRESSION", "BINDING", "POSITIVE", "NEGATIVE", "GENE", "CELL", "PROTEIN",
    "GENES", "CELLS", "PROTEINS", "TARGET", "TARGETS", "MEDIATED", "INDUCED",
    "DEPENDENT", "INDEPENDENT", "ASSOCIATED", "RELATED", "ENCODED", "MODIFIED",
    "COMPLEX", "RECEPTOR", "TCELL", "BCELL", "TYPE", "STAGE", "STATE", "LEVEL",
    # Source-DB prefixes (handled by _SOURCE_DB_PREFIXES above)
    "GOBP", "GOMF", "GOCC", "HALLMARK", "REACTOME", "KEGG", "BIOCARTA",
    "WP", "PID", "HP", "MIR", "GSE",
})

# Abbreviation / alias expansions for query terms that don't appear as tokens
# in geneset_names at sufficient frequency (e.g. "SLE" appears only 2× as a
# standalone token, so it needs an alias to "LUPUS" which appears 30×).
# Also covers standard medical abbreviations and brand-name → author-set aliases.
# Keyed by lowercase query word; value is a list of uppercase tokens to inject.
_CONCEPT_ALIASES: dict = {
    # Disease abbreviations
    "sle":          ["LUPUS"],           # systemic lupus erythematosus
    "ra":           ["RHEUMATOID"],      # rheumatoid arthritis
    "ms":           ["SCLEROSIS"],       # multiple sclerosis
    "als":          ["AMYOTROPHIC"],     # amyotrophic lateral sclerosis
    "ad":           ["ALZHEIMER"],       # Alzheimer disease
    "pd":           ["PARKINSON"],       # Parkinson disease
    "hd":           ["HUNTINGTON"],      # Huntington disease
    # Disease names that appear <5× as geneset_name tokens (below dynamic map floor)
    "rheumatoid":   ["RHEUMATOID"],
    "alzheimer":    ["ALZHEIMER"],
    "alzheimers":   ["ALZHEIMER"],
    "parkinson":    ["PARKINSON"],
    "parkinsons":   ["PARKINSON"],
    "huntington":   ["HUNTINGTON"],
    # Pathway/process aliases
    "ire1a":        ["IRE1"],            # IRE1-alpha → IRE1 token
    "ire1α":        ["IRE1"],
    "xbp1s":        ["XBP1"],            # spliced XBP1 → XBP1 token
    # Brand-name → author-set aliases (concept injection; for exact-set precision
    # use term_rewrite in config instead, e.g. mammaprint → VANTVEER_BREAST_CANCER_POOR_PROGNOSIS)
    "oncotype":     ["PAIK"],            # Oncotype DX → Paik 21-gene signature
}


def _build_concept_token_map(data: dict) -> dict:
    """Build {lowercase_word: UPPERCASE_TOKEN} from geneset_names in the parquet.

    Data-driven (mirrors HCDT's _build_syn_map): scans all geneset_name values,
    extracts non-prefix uppercase tokens in the frequency range [5, 300], and
    maps their lowercase form to the canonical uppercase token. Tokens outside
    this range are either too rare (experiment-specific) or too generic to be
    useful as concept substring filters. Source-DB prefix tokens and high-
    frequency generic terms are excluded; they are handled separately.
    """
    import logging
    from collections import Counter

    log = logging.getLogger("uvicorn.error")
    gm = data.get("geneset_master_table_msigdb")
    if gm is None:
        log.warning("[msigdb] concept token map: geneset_master_table not loaded")
        return {}
    try:
        df = gm.collect() if hasattr(gm, "collect") else gm
        names = df["geneset_name"].drop_nulls().to_list()

        token_counts: Counter = Counter()
        for name in names:
            parts = name.split("_")
            if len(parts) < 2:
                continue
            # Skip the first segment (source-DB prefix) — handled by _SOURCE_DB_PREFIXES.
            # Allow alphanumeric tokens (e.g. XBP1=13, IRE1=7, BRCA1, TP53).
            for tok in parts[1:]:
                if len(tok) >= 3 and tok.isupper():
                    token_counts[tok] += 1

        result: dict = {}
        for tok, cnt in token_counts.items():
            if 5 <= cnt <= 300 and tok not in _EXCLUDE_GENERIC:
                result[tok.lower()] = tok   # lowercase key → uppercase canonical token

        log.info("[msigdb] concept token map: %d lookup keys", len(result))
        return result
    except Exception as exc:
        import logging as _l
        _l.getLogger("uvicorn.error").warning(
            "[msigdb] concept token map build failed: %s", exc)
        return {}


def _get_concept_token_map(data: dict) -> dict:
    global _CONCEPT_TOKEN_MAP
    if _CONCEPT_TOKEN_MAP is None:
        with _CONCEPT_TOKEN_LOCK:
            if _CONCEPT_TOKEN_MAP is None:
                _CONCEPT_TOKEN_MAP = _build_concept_token_map(data)
    return _CONCEPT_TOKEN_MAP


def _real_filter_val(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() not in ("", "requested")
    if isinstance(v, (list, tuple)):
        return any(_real_filter_val(x) for x in v)
    return bool(v)


def _strip_sql_wildcards(v):
    """The filter engine substring-matches geneset_name LITERALLY (str.contains,
    literal=True), so a mapper-emitted SQL pattern like 'KEGG_%' matches the literal
    text including '%' → 0 rows. Strip '%' so the bare prefix substring-matches."""
    if isinstance(v, str):
        return v.replace("%", "").strip()
    if isinstance(v, (list, tuple)):
        out = [_strip_sql_wildcards(x) for x in v if x is not None]
        return [x for x in out if x != ""]
    return v


def _msigdb_organism_inject(ctx) -> None:
    """Pre-expand hook: for intersection queries with explicit organism keywords
    ("human", "mouse"), inject geneset_organism + gene_organism into
    ctx.inp["parsed_value"] BEFORE the expand call.

    The LLM mapper reliably extracts the two geneset_names for intersection
    queries but consistently drops the organism qualifier. Injecting here gives
    the expand service the organism context so it can return the correct
    organism's geneset_ids (human vs mouse).  A second reinforcement happens in
    _source_db_prefix_filter (pre_join) for cases where expand ignores the
    organism field and still returns multi-organism candidates.
    """
    q = (getattr(getattr(ctx, "input", None), "cleaned_query", None) or "")
    if not _MSIGDB_INTERSECTION_RX.search(q):
        return
    if _MSIGDB_HUMAN_RX.search(q):
        organism = "Homo sapiens"
    elif _MSIGDB_MOUSE_RX.search(q):
        organism = "Mus musculus"
    else:
        return
    pv = ctx.inp.get("parsed_value") or {}
    if not _real_filter_val(pv.get("geneset_organism")):
        pv["geneset_organism"] = [organism]
    if not _real_filter_val(pv.get("gene_organism")):
        pv["gene_organism"] = [organism]
    ctx.inp["parsed_value"] = pv


def _source_db_prefix_filter(ctx) -> None:
    """Make MSigDB source-DB queries ('KEGG pathways', 'REACTOME gene sets', …) work,
    and inject concept-keyword tokens for disease/pathway concept queries where
    filter_val is entirely empty after expand (e.g. 'IFN signature for SLE patients').

    Layer 1 — Source-DB prefix injection (original behaviour):
      Sources are geneset_name PREFIXES, not `collection` values; the value-mapper
      either binds nothing (→ 'refusing whole-DB dump' → web) or binds a 'KEGG_%'
      SQL pattern whose literal '%' matches 0 rows. Strips '%' wildcards and injects
      the bare prefix (e.g. 'KEGG_') so dataframe_filtering substring-matches.

    Layer 2 — Concept-keyword injection (2026-06-29):
      Fires ONLY when filter_val has NO real values after the source-DB layer —
      i.e. the whole-DB dump guard would have fired with "No filter terms produced".
      Scans the cleaned_query for:
        a) Known abbreviation aliases (_CONCEPT_ALIASES) — handles SLE→LUPUS,
           RA→RHEUMATOID, AD→ALZHEIMER, ire1a→IRE1, etc.
        b) Data-driven concept tokens — lazy-built from the parquet at first query
           (mirrors HCDT's _build_syn_map pattern); maps lowercase query words to
           their uppercase token form as it appears inside geneset_names.
      Injects matching tokens as geneset_name substring filters so that queries like
      "Which gene sets involve Notch signaling?" find KEGG_NOTCH_SIGNALING_PATHWAY,
      WP_NOTCH_SIGNALING_WP61 etc., and "Does an IFN signature exist for SLE?"
      finds HALLMARK_INTERFERON_GAMMA_RESPONSE and HALLMARK_INTERFERON_ALPHA_RESPONSE.

    Layer 3 — Organism reinforcement for intersection queries (original behaviour).
    """
    fv = getattr(ctx, "filter_val", None)
    if not isinstance(fv, dict):
        return

    # ── Layer 1: source-DB prefix ──────────────────────────────────────────
    if "geneset_name" in fv:
        cleaned = _strip_sql_wildcards(fv.get("geneset_name"))
        if cleaned:
            fv["geneset_name"] = cleaned
        else:
            fv.pop("geneset_name", None)

    if not _real_filter_val(fv.get("geneset_name")):
        q_lower = (getattr(getattr(ctx, "input", None), "cleaned_query", None) or "").lower()
        for token, prefix in _SOURCE_DB_PREFIXES:
            if token in q_lower:
                fv["geneset_name"] = [prefix]
                cols = list(getattr(ctx, "out_cols", None) or [])
                if "geneset_name" not in cols:
                    cols.append("geneset_name")
                    ctx.out_cols = cols
                break  # first match wins; most-specific phrases are listed first

    # ── Layer 2: concept-keyword injection ────────────────────────────────
    # Only fires when ALL filter values are empty/sentinel — the query has no
    # anchor entity at all. Injecting concept tokens when other entities are
    # already resolved (e.g. gene_symbol=["BRCA1"]) would incorrectly broaden
    # the result to all genesets mentioning the disease concept.
    if not _real_filter_val(fv.get("geneset_name")) and not any(
        _real_filter_val(v) for v in fv.values()
    ):
        q = (getattr(getattr(ctx, "input", None), "cleaned_query", None) or "").lower()
        token_map = _get_concept_token_map(ctx.data) if ctx.data else {}

        injected: list = []
        seen: set = set()

        def _add(tok: str) -> None:
            if tok and tok not in seen:
                seen.add(tok)
                injected.append(tok)

        # Pass a: abbreviation/alias expansions (abbreviations not in parquet tokens)
        for word in re.findall(r'\b[a-z][a-z0-9α-ω]{1,}\b', q):
            for tok in _CONCEPT_ALIASES.get(word, []):
                _add(tok)

        # Pass b: data-driven token lookup (words ≥3 chars in token map)
        for word in re.findall(r'\b[a-z][a-z0-9]{2,}\b', q):
            tok = token_map.get(word)
            if tok:
                _add(tok)

        if injected:
            fv["geneset_name"] = injected
            cols = list(getattr(ctx, "out_cols", None) or [])
            if "geneset_name" not in cols:
                cols.append("geneset_name")
                ctx.out_cols = cols
            ctx.log.info(
                "[msigdb] concept inject: %d token(s) → geneset_name=%s",
                len(injected), injected,
            )

    # ── Layer 3: organism reinforcement for intersection queries ───────────
    q_full = (getattr(getattr(ctx, "input", None), "cleaned_query", None) or "")
    if _MSIGDB_INTERSECTION_RX.search(q_full):
        if _MSIGDB_HUMAN_RX.search(q_full):
            inj_org = "Homo sapiens"
        elif _MSIGDB_MOUSE_RX.search(q_full):
            inj_org = "Mus musculus"
        else:
            inj_org = None
        if inj_org:
            if not _real_filter_val(fv.get("geneset_organism")):
                fv["geneset_organism"] = [inj_org]
            if not _real_filter_val(fv.get("gene_organism")):
                fv["gene_organism"] = [inj_org]


_MSIGDB_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_msigdb_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_MSIGDB_CAPABILITIES,
    limitations=_MSIGDB_LIMITATIONS,
    pre_expand=_msigdb_organism_inject,
    pre_join=_source_db_prefix_filter,
    # Brand-name → canonical geneset_name rewrites applied to the query BEFORE
    # the schema_mapper sees it (term_rewrite, like CTD's MeSH synonym handling).
    # MammaPrint is the 70-gene van't Veer poor-prognosis signature; the mapper
    # would otherwise copy "MammaPrint" verbatim (Rule 1) and substring-match
    # would find 0 rows (VANTVEER_* names contain no "mammaprint" substring).
    term_rewrite={
        "mammaprint": "VANTVEER_BREAST_CANCER_POOR_PROGNOSIS",
    },
)

return_msigdb_result = make_schema_kg_handler(_MSIGDB_CONFIG)
