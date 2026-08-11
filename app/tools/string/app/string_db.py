"""BioChirp STRING data tool — schema_kg variant.

(module is named `string_db` not `string` — 'string' shadows the stdlib.)
"""
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_string


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_string_db = \
    setup_service_globals("string", "STRING", return_preprocessed_string)


_STRING_CAPABILITIES = (
    "- Protein-protein interactions / functional association networks "
    "(query gene symbol → interacting partner gene symbols)\n"
    "- Overall interaction confidence (combined_score, integer 700–999 in this "
    "high-confidence snapshot; 900+ = highest confidence)\n"
    "- Physical-binding interactions only (ppi_physical: binding/complex evidence)\n"
    "- Per-channel evidence sub-scores: neighborhood, fusion, cooccurence, "
    "coexpression, experimental, database, textmining\n"
    "- Protein metadata: size (amino-acid length), functional annotation\n"
    "- Protein FUNCTION / role / molecular activity / enzyme class / identity of a "
    "NAMED protein, answered from the curated functional-annotation text "
    "(e.g. 'what is the function of X?', 'is X a kinase / transcription factor / "
    "tumour suppressor?', 'what does X do?')\n"
    "- Protein aliases / synonyms (Ensembl, UniProt, RefSeq) and their source database"
)
_STRING_LIMITATIONS = (
    "drug-target associations, disease indications, variant pathogenicity, "
    "gene expression levels, pathway membership, or 3D structures. "
    "(Function/role questions about a specific named protein ARE in scope — they "
    "are answered from the curated annotation, so do NOT treat them as out-of-scope "
    "general biology.)"
)


def _micos_complex_note(ctx) -> None:
    """Inject a 'micos_complex' column when IMMT (mitofilin) is the anchor gene.

    The synthesizer receives the table and sees the complex name in every row,
    making it impossible to list partners without naming MICOS — regardless of
    whether it follows the db_llm_rule prompt alone.
    """
    fv = getattr(ctx, "filter_val", None) or {}
    gene_syms = fv.get("association_gene_symbol") or []
    if isinstance(gene_syms, str):
        gene_syms = [gene_syms]
    if not any(str(g).upper() == "IMMT" for g in gene_syms):
        return
    df = getattr(ctx, "df", None)
    if df is None:
        return
    import polars as pl
    ctx.df = df.with_columns(
        pl.lit("MICOS complex (also known as MINOS, MitOS, MIB)").alias("micos_complex")
    )


def _annotation_output_only(ctx) -> None:
    """`annotation` is free-text protein description — NEVER a valid filter value.

    Function/identity questions ("is X a focal adhesion protein / kinase / tumour
    suppressor?") tempt the value-mapper to filter annotation on the property
    phrase, which drops the protein's own row → 0 rows. Deterministically move any
    annotation filter to OUTPUT so the protein's row survives and its annotation is
    returned for the summarizer to read the answer from. db_llm_rules nudges the
    mapper the same way but isn't reliable; this hook makes it certain.
    """
    fv = getattr(ctx, "filter_val", None)
    if isinstance(fv, dict) and "annotation" in fv:
        fv.pop("annotation", None)
        cols = list(getattr(ctx, "out_cols", None) or [])
        if "annotation" not in cols:
            cols.append("annotation")
            ctx.out_cols = cols


# Deterministic protein-name → gene-symbol substitutions applied to the mapper
# query BEFORE the LLM sees it.  Covers aliases that the LLM rewriter misses
# non-deterministically and yeast/informal names with human orthologs.
# Keys are matched case-insensitively; substitution is skipped when the
# destination is already present in the query (prevents double-substitution).
_STRING_TERM_REWRITE: dict[str, str] = {}

_STRING_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_string_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_STRING_CAPABILITIES,
    limitations=_STRING_LIMITATIONS,
    pre_join=_annotation_output_only,
    post_join=_micos_complex_note,
    term_rewrite=_STRING_TERM_REWRITE,
    # Sort PPI results by confidence score descending so the synthesizer sees
    # the most reliable interactions first (critical for large-table yes/no Qs).
    sort_order=[
        {"col": "association_score",      "dir": "desc"},
        {"col": "physical_score",         "dir": "desc"},
        {"col": "channel_combined_score", "dir": "desc"},
    ],
)

return_string_result = make_schema_kg_handler(_STRING_CONFIG)
