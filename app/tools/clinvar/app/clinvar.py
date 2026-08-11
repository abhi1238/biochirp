"""BioChirp ClinVar data tool — schema_kg variant.

Orchestration (LLM router, in-process schema_kg planner, web fallback, on-empty
retry) is provided by the shared `app.per_db_tool.schema_kg_worker`. This module
only injects ClinVar's identity + capability blurb. The original HTTP-pipeline
worker is preserved at `clinvar.py.pre_schema_kg`.
"""
import re
from typing import Optional

from config.schema import database_schemas

from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_clinvar


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_clinvar_db = \
    setup_service_globals("clinvar", "ClinVar", return_preprocessed_clinvar)


_CLINVAR_CAPABILITIES = (
    "- Variant clinical significance / pathogenicity (Pathogenic, Likely pathogenic, "
    "Uncertain significance, Likely benign, Benign, Conflicting classifications, Risk factor, Drug response)\n"
    "- Variant-gene associations (HGVS variant names + HGNC gene symbols)\n"
    "- Variant-disease associations (variant + disease/condition name + per-disease clinical significance)\n"
    "- Variant details: variant type (SNV, Indel, Deletion, Duplication) and molecular consequence "
    "(missense, synonymous, stop gained, frameshift)\n"
    "- Review status / star rating (e.g. reviewed by expert panel = 4 stars)\n"
    "- Genomic coordinates (assembly GRCh38/GRCh37, chromosome, position, ref/alt alleles)\n"
    "- Variant cross-references: dbSNP rsID, dbVar nsv, allele ID, SCV submission accessions\n"
    "- Submitter interpretations + supporting literature citations (PubMed PMIDs)"
)
_CLINVAR_LIMITATIONS = (
    "drug-target interactions, drug indications, protein 3D structures, "
    "protein-protein interactions, gene expression levels, pathway membership, "
    "or general biology knowledge"
)

# DISEASE-NAMED direction detection ("which gene causes X", "genetic basis of X").
# The col_selection/mapper/tiebreaker prose in db_llm_rules.yaml tells the schema_mapper
# LLM to anchor on variant_disease_name and request gene_symbol as output in this
# direction, but LLM compliance with that instruction is inconsistent (works for some
# phrasings, silently reverts to the gene-anchor default for structurally identical ones).
# This hook is a deterministic backstop: regex-detect the question's WH-target
# (gene/protein, not disease) and force the correct table topology every time,
# independent of what the mapper LLM decided.
_GENE_TARGET_RE = re.compile(
    r"\b(?:which|what)\b(?:\s+\S+){0,3}?\s+(?:gene|genes|protein|proteins)\b",
    re.IGNORECASE,
)
_CAUSE_VERB_RE = re.compile(
    r"\b(?:causes?|caused\s+by|underlies?|underlying|implicated\s+in|associated\s+with|"
    r"responsible\s+for|linked\s+to|mutated\s+in|involved\s+in|leads?\s+to)\s+"
    r"(?P<disease>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)
_BASIS_RE = re.compile(
    r"\b(?:genetic|molecular)\s+basis\s+of\s+(?P<disease>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)
_CAUSE_OF_RE = re.compile(
    r"\bcause\s+of\s+(?P<disease>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)

# Guard against phrases that describe a VARIANT/MUTATION rather than name a disease
# (e.g. "cause of a STAG3 truncating variant" — asking what disease a variant causes,
# not naming a disease at all). Real variant_disease_name values in ClinVar are
# MedGen/OMIM condition names and never contain these words, so this is a safe,
# structural (not per-question) filter rather than a gene-name blocklist.
_NOT_A_DISEASE_RE = re.compile(
    r"\b(?:variants?|mutations?|alleles?|polymorphisms?|genotypes?)\b",
    re.IGNORECASE,
)


def _extract_reverse_direction_disease(q: str) -> str | None:
    """Return the disease/condition phrase when `q` asks for the causal GENE
    (disease-named direction), else None. Only fires on question *structure*
    (which noun is the WH-target) — never on a specific disease name, so it
    generalizes to any disease phrasing rather than hardcoding per-question fixes.
    """
    disease = None
    if _GENE_TARGET_RE.search(q):
        m = _CAUSE_VERB_RE.search(q)
        if m:
            disease = m.group("disease").strip(" .?")
    else:
        m = _BASIS_RE.search(q) or _CAUSE_OF_RE.search(q)
        if m:
            disease = m.group("disease").strip(" .?")
    if not disease or len(disease) < 3:
        return None
    if _NOT_A_DISEASE_RE.search(disease):
        return None
    return disease


_VDA = "variant_disease_association_clinvar"
_VGA = "variant_gene_association_clinvar"
_GMT = "gene_master_table_clinvar"
_VGC = "variant_genomic_coords_clinvar"
_VS = "variant_submission_clinvar"
_VMT = "variant_master_table_clinvar"

# PharmVar/CPIC star-allele nomenclature (GENE*N, e.g. "CYP2C19*2") is a
# generic syntactic pattern — not a fixed gene list. ClinVar has no star-
# allele cross-reference anywhere in its schema: variant_name stores ONLY
# HGVS c./p. notation (confirmed by direct data check: CYP2C19*2/*3 exist in
# the data as "c.681G>A (p.Pro227=)" / "c.636G>A (p.Trp212Ter)", never as
# "*2"/"*3"). Left unguarded, the mapper silently drops the *N suffix and
# resolves only the bare gene, so the join returns every variant of that gene
# and a downstream LLM can assert a specific functional/directional claim
# ("no effect") from that irrelevant, unfiltered data — observed live for
# "What is the effect of the alleles CYP2C19*2 and CYP2C19*3 on CYP2C19
# function?", which answered the opposite of the correct (loss-of-function)
# conclusion. Detect the syntax and force a literal search on the exact
# "GENE*N" string instead: ClinVar's HGVS-only naming guarantees this misses
# cleanly, so the query correctly falls through to the web-search fallback
# (already verified to answer this class of question correctly) instead of
# guessing from unfiltered gene-wide data.
_STAR_ALLELE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,15})\*(\d{1,3})\b")


def _force_variant_name_miss(ctx, sk_plan, terms: list[str], reason: str) -> None:
    """Force a deterministic table miss by searching variant_name (HGVS c./p.
    notation only) for literal text that structurally can never appear there
    — free-text disease names, star-allele syntax, etc. Lets the query fall
    through to the existing (already-verified) LLM/web fallback honestly,
    instead of a downstream LLM asserting a specific claim from irrelevant or
    misleading table data.
    """
    ctx.log.info("[clinvar] %s — forcing literal variant_name miss on %r", reason, terms)
    sk_plan["plan_tables"] = {_VMT}
    sk_plan["join_path"] = []
    op = sk_plan.setdefault("output_plan", {})
    op.clear()
    op[_VMT] = ["variant_name"]
    fp = sk_plan.setdefault("filter_plan", {})
    fp.clear()
    fp[_VMT] = {"variant_name": terms}
    pv = sk_plan.setdefault("parsed_value", {})
    pv.clear()
    pv["variant_name"] = terms
    ctx.filter_val["variant_name"] = terms
    ctx.filter_val["gene_symbol"] = None


def _guard_star_allele_terms(ctx, sk_plan) -> bool:
    q = (getattr(ctx.input, "cleaned_query", "") or "")
    terms = [f"{m.group(1)}*{m.group(2)}" for m in _STAR_ALLELE_RE.finditer(q)]
    if not terms:
        return False
    _force_variant_name_miss(
        ctx, sk_plan, terms,
        "star-allele syntax detected — ClinVar has no PharmVar cross-reference "
        "for this nomenclature (variant_name is HGVS-only); web-search fallback "
        "can answer instead of guessing from unfiltered gene-wide data",
    )
    return True


# "Most common/frequent/primary/leading/main cause of DISEASE" asks for an
# EPIDEMIOLOGICAL/population-prevalence fact. ClinVar's row counts reflect
# submission/testing volume, not clinical prevalence — confirmed by direct
# data check: for "common variable immunodeficiency", TNFSF12 has 266 ClinVar
# entries vs. NFKB1's 7, yet NFKB1 is the epidemiologically correct "most
# common monogenic cause" per the literature. No column in ClinVar's schema
# encodes population prevalence, so no ranking of ClinVar's own rows —
# relevance-score or raw row-count — can ever answer this question type
# correctly. Force a literal miss (the disease-name phrase can never appear
# in variant_name's HGVS notation) so the query falls through to the LLM/web
# fallback's medical-literature knowledge instead of asserting whichever gene
# happens to have the most ClinVar submissions.
_SUPERLATIVE_CAUSE_RE = re.compile(
    r"\b(?:most\s+common|most\s+frequent|primary|leading|main)\b(?:\s+\S+){0,4}?"
    r"\s+cause[s]?\s+of\s+(?P<disease>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)


def _guard_superlative_cause(ctx, sk_plan) -> bool:
    q = (getattr(ctx.input, "cleaned_query", "") or "")
    m = _SUPERLATIVE_CAUSE_RE.search(q)
    if not m:
        return False
    disease = m.group("disease").strip(" .?")
    if not disease:
        return False
    _force_variant_name_miss(
        ctx, sk_plan, [disease],
        "superlative-cause question detected — ClinVar row counts reflect "
        "submission volume, not population prevalence",
    )
    return True

# Pharmacogenomic drug-named direction ("which gene is required for the
# function of DRUG", "which gene metabolizes DRUG"). ClinVar has no drug_name
# column anywhere (its own db_llm_rules explicitly say so), but
# variant_submission_clinvar's free-text submitted_phenotype/reported_phenotype
# fields routinely name the drug directly (e.g. "clopidogrel response",
# "warfarin sensitivity") on rows whose variant is classified clinical_
# significance="drug response". This resolves drug -> gene by searching
# ClinVar's OWN curated text — no drug/gene mapping table of any kind.
_PHARMA_GENE_RE = re.compile(
    r"\b(?:which|what)\b(?:\s+\S+){0,4}?\s+gene[s]?\b.*?"
    r"\b(?:required|needed|responsible|necessary)\s+for\b(?:\s+\S+){0,4}?"
    r"\s+(?:function|metaboli[sz]ation|metaboli[sz]m|activation|response|efficacy)\s+of\s+"
    r"(?P<drug>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)
_PHARMA_METABOLIZES_RE = re.compile(
    r"\b(?:which|what)\b(?:\s+\S+){0,4}?\s+gene[s]?\b.*?"
    r"\bmetaboli[sz]es?\s+(?P<drug>.+?)\s*[\?\.]*\s*$",
    re.IGNORECASE,
)


def _extract_pharmacogene_drug(q: str) -> Optional[str]:
    m = _PHARMA_GENE_RE.search(q) or _PHARMA_METABOLIZES_RE.search(q)
    if not m:
        return None
    drug = m.group("drug").strip(" .?")
    return drug if len(drug) >= 3 else None


async def _pharmacogenomic_lookup(ctx, sk_plan) -> None:
    q = (getattr(ctx.input, "cleaned_query", "") or "").strip()
    if not q:
        return
    # A gene already named/resolved means this isn't the drug->gene direction.
    existing_gene = ctx.filter_val.get("gene_symbol")
    if isinstance(existing_gene, list) and existing_gene:
        return
    drug = _extract_pharmacogene_drug(q)
    if not drug:
        return

    data = (ctx.data or {}).get("clinvar") or {}
    vs_df, vga_df, gmt_df = data.get(_VS), data.get(_VGA), data.get(_GMT)
    if vs_df is None or vga_df is None or gmt_df is None:
        return

    try:
        import polars as pl

        def _lazy(df):
            return df if isinstance(df, pl.LazyFrame) else df.lazy()

        drug_lc = drug.lower()
        matching_variant_ids = (
            _lazy(vs_df)
            .filter(
                pl.col("submitted_phenotype").str.to_lowercase().str.contains(drug_lc, literal=True).fill_null(False)
                | pl.col("reported_phenotype").str.to_lowercase().str.contains(drug_lc, literal=True).fill_null(False)
            )
            .select("variant_id").unique()
            .collect()["variant_id"].to_list()
        )
        if not matching_variant_ids:
            return
        gene_ids = (
            _lazy(vga_df)
            .filter(pl.col("variant_id").is_in(matching_variant_ids))
            .select("gene_id").unique()
            .collect()["gene_id"].to_list()
        )
        if not gene_ids:
            return
        genes = sorted(set(
            _lazy(gmt_df)
            .filter(pl.col("gene_id").is_in(gene_ids))
            .select("gene_symbol").unique()
            .collect()["gene_symbol"].to_list()
        ))
        if not genes:
            return
    except Exception as e:
        ctx.log.warning("[clinvar] pharmacogenomic lookup failed: %s", e)
        return

    ctx.log.info(
        "[clinvar] pharmacogenomic drug-named direction override: drug=%r -> genes=%r",
        drug, genes,
    )
    # The gene(s) are already fully resolved from ClinVar's own data — no need
    # to re-join through variant_submission_clinvar downstream, just filter
    # gene_master_table_clinvar directly to them.
    sk_plan["plan_tables"] = {_GMT}
    sk_plan["join_path"] = []
    op = sk_plan.setdefault("output_plan", {})
    op.clear()
    op[_GMT] = ["gene_symbol"]
    fp = sk_plan.setdefault("filter_plan", {})
    fp.clear()
    fp[_GMT] = {"gene_symbol": genes}
    pv = sk_plan.setdefault("parsed_value", {})
    pv.clear()
    pv["gene_symbol"] = genes
    ctx.filter_val["gene_symbol"] = genes


_mc_vocab_cache: dict[str, str] | None = None

# Every variant-level ClinVar association table is keyed on variant_id and
# joins to the variant hub (variant_gene_association); gene_master is the only
# gene_id-keyed table and reaches the hub on gene_id. This mirrors the physical
# FK layout of the parquet set (verified: VGA has variant_id+gene_id, VDA/VGC/
# VS/VMT all carry variant_id) — it is the schema's own structure, not a
# per-entity or per-question mapping.
_VARIANT_KEYED_TABLES = frozenset({_VDA, _VGC, _VS, _VMT})


def _repair_join_connectivity(ctx, sk_plan) -> None:
    """Re-stitch any plan table a hook left orphaned after pruning another table.

    When a hook drops a table from the plan (e.g. `_normalize_molecular_
    consequence` removing variant_genomic_coords once its filter proves
    unmatchable) it also drops every join_path edge touching that table. If a
    surviving table was connected to the tree ONLY through the pruned one, it is
    left in `plan_tables` with no remaining join edge — so
    join_and_filter_database finds no join_pairs for it and, under
    STRICT_JOIN_MODE, raises MissingJoinError → 0 rows → LLM/web fallback even
    though the data is present (this is exactly the STAG3 "truncating variant"
    failure: variant_disease_association was reachable only via the pruned
    variant_genomic_coords table).

    Reconnect each orphan using ClinVar's fixed FK keys: variant_id to the
    variant hub for variant-keyed tables, gene_id for gene_master. No new tables
    are added and no per-entity logic is involved — this only restores a join
    edge the pruning removed. No-op when the tree is already fully connected.
    """
    plan_tables = set(sk_plan.get("plan_tables") or set())
    if len(plan_tables) < 2:
        return
    join_path = list(sk_plan.get("join_path") or [])
    connected = {t for edge in join_path for t in edge[:2]}
    orphans = plan_tables - connected
    if not orphans:
        return
    hub = _VGA if _VGA in plan_tables else None
    for t in sorted(orphans):
        if t in _VARIANT_KEYED_TABLES and hub:
            join_path.append((hub, t, "variant_id"))
        elif t == _GMT and hub:
            join_path.append((hub, _GMT, "gene_id"))
        elif t == _VGA:
            # The hub itself is orphaned — attach it to gene_master (gene_id) or
            # any surviving variant-keyed table (variant_id) so it rejoins.
            if _GMT in plan_tables:
                join_path.append((_GMT, _VGA, "gene_id"))
            else:
                sibling = next((s for s in plan_tables
                                if s in _VARIANT_KEYED_TABLES), None)
                if sibling:
                    join_path.append((sibling, _VGA, "variant_id"))
                else:
                    continue
        else:
            continue
        ctx.log.info("[clinvar] re-stitched orphaned table %r into join_path "
                     "after a table prune (FK-based connectivity repair)", t)
    sk_plan["join_path"] = join_path


def _molecular_consequence_vocab(ctx) -> dict[str, str]:
    """Map of normalized-term -> raw-stored-term for every distinct
    molecular_consequence value ClinVar actually has (e.g. "missense variant" ->
    "missense_variant"), read live from the data — not a hardcoded list — so
    this stays correct if the underlying snapshot ever changes. Cached at
    module scope; the vocabulary is static for a given data load.
    """
    global _mc_vocab_cache
    if _mc_vocab_cache is not None:
        return _mc_vocab_cache
    vocab: dict[str, str] = {}
    try:
        data = (ctx.data or {}).get("clinvar") or {}
        df = data.get(_VGC)
        if df is None:
            ctx.log.warning(
                "[clinvar] mc_vocab: %r not in ctx.data['clinvar'] (keys=%r)",
                _VGC, list(data.keys()) if hasattr(data, "keys") else type(data),
            )
        elif "molecular_consequence" not in df.columns:
            ctx.log.warning(
                "[clinvar] mc_vocab: no molecular_consequence column in %r (cols=%r)",
                _VGC, df.columns,
            )
        else:
            import polars as pl
            _lazy_df = df if isinstance(df, pl.LazyFrame) else df.lazy()
            raw = (
                _lazy_df.select("molecular_consequence").drop_nulls().unique()
                .collect()["molecular_consequence"].to_list()
            )
            for cell in raw:
                for part in str(cell).split(","):
                    part = part.strip()
                    if "|" in part:
                        part = part.split("|", 1)[1]
                    part = part.strip()
                    if part:
                        vocab.setdefault(part.lower().replace("_", " "), part)
            ctx.log.info("[clinvar] mc_vocab: built %d terms from live data", len(vocab))
    except Exception as e:
        ctx.log.warning("[clinvar] mc_vocab: build failed: %r", e)
    _mc_vocab_cache = vocab
    return vocab


def _normalize_molecular_consequence(ctx, sk_plan) -> None:
    """molecular_consequence stores narrow Sequence-Ontology consequence terms
    with underscores (missense_variant, frameshift_variant, nonsense,
    synonymous_variant, ...). Two distinct problems show up here, both from the
    same root cause — the shared literal-floor fallback in
    app/per_db_tool/_orchestrator.py floors ANY unresolved term to raw
    natural-language text, with no notion of this column's controlled vocabulary:

    1. UNMATCHABLE: functional-impact language ("loss-of-function",
       "gain-of-function") is a different vocabulary entirely — a mechanistic
       classification spanning several SO terms, never a literal stored value.
       It can never match; floor-filtering on it zeroes the whole join
       (variant_genomic_coords_clinvar goes 100% -> 0 rows) instead of just
       omitting an unmatchable filter.
    2. MISPHRASED: real SO concepts phrased naturally ("missense variants",
       plural, space-separated) don't literally substring-match the stored
       underscore/singular form ("missense_variant") even though the concept
       IS real ClinVar data.

    Check each candidate against the REAL vocabulary read from the live data
    (no hardcoded phrase list) — rewrite it to the exact stored form if it
    resolves (fixes case 2), or drop the filter (and the now-pointless table)
    if nothing resolves (fixes case 1) — instead of silently zeroing everything.
    """
    mc_vals = ctx.filter_val.get("molecular_consequence")
    if not isinstance(mc_vals, list) or not mc_vals:
        return
    vocab = _molecular_consequence_vocab(ctx)
    if not vocab:
        return  # couldn't build a vocabulary — don't guess, leave the plan alone

    def _match(term: str) -> Optional[str]:
        t = term.lower().replace("_", " ").replace("-", " ").strip()
        t_sing = t[:-1] if t.endswith("s") and not t.endswith("ss") else t
        for norm, raw in vocab.items():
            if t in norm or norm in t or t_sing in norm or norm in t_sing:
                return raw
        return None

    resolved = [m for m in (_match(v) for v in mc_vals) if m]
    resolved = list(dict.fromkeys(resolved))  # de-dupe, keep order

    if not resolved:
        ctx.log.info(
            "[clinvar] dropping unmatchable molecular_consequence filter %r — not "
            "a Sequence-Ontology consequence term (functional-impact language, "
            "not a literal ClinVar value)", mc_vals,
        )
        ctx.filter_val.pop("molecular_consequence", None)
        fp = sk_plan.get("filter_plan") or {}
        fp.get(_VGC, {}).pop("molecular_consequence", None)
        pv = sk_plan.get("parsed_value") or {}
        pv.pop("molecular_consequence", None)

        op = sk_plan.get("output_plan") or {}
        table_still_needed = bool(fp.get(_VGC)) or bool(op.get(_VGC))
        if not table_still_needed and _VGC in (sk_plan.get("plan_tables") or set()):
            sk_plan["plan_tables"] = {t for t in sk_plan["plan_tables"] if t != _VGC}
            sk_plan["join_path"] = [
                edge for edge in (sk_plan.get("join_path") or [])
                if _VGC not in edge[:2]
            ]
        return

    if resolved != mc_vals:
        ctx.log.info(
            "[clinvar] normalizing molecular_consequence filter %r -> %r "
            "(matched live SO vocabulary)", mc_vals, resolved,
        )
        ctx.filter_val["molecular_consequence"] = resolved
        fp = sk_plan.get("filter_plan") or {}
        if "molecular_consequence" in (fp.get(_VGC) or {}):
            fp[_VGC]["molecular_consequence"] = resolved
        pv = sk_plan.get("parsed_value") or {}
        if "molecular_consequence" in pv:
            pv["molecular_consequence"] = resolved


_DISEASE_WORD_SPLIT_RE = re.compile(r"\s+")


_VDA_TOTAL_ROWS_CACHE: Optional[int] = None

# A relaxed candidate that matches more than this fraction of ALL
# variant_disease_name rows is almost certainly a generic English word
# ("syndrome", "disease", "deficiency", ...) rather than a specific disease
# name — accepting it would flood the answer with unrelated diseases (e.g.
# "restless leg syndrome" relaxing all the way down to bare "syndrome", which
# alone matches ~23% of the table). This is a genericity ceiling, not a
# per-word/per-entity rule — it applies uniformly to whatever candidate the
# word-dropping loop produces.
_RELAX_MAX_MATCH_FRACTION = 0.02


def _vda_total_rows(ctx) -> int:
    global _VDA_TOTAL_ROWS_CACHE
    if _VDA_TOTAL_ROWS_CACHE is not None:
        return _VDA_TOTAL_ROWS_CACHE
    try:
        import polars as pl
        data = (ctx.data or {}).get("clinvar") or {}
        vda_df = data.get(_VDA)
        if vda_df is None:
            return -1
        lz = vda_df if isinstance(vda_df, pl.LazyFrame) else vda_df.lazy()
        total = int(lz.select(pl.len()).collect().item())
        _VDA_TOTAL_ROWS_CACHE = total
        return total
    except Exception as e:
        ctx.log.warning("[clinvar] vda total-rows check failed: %s", e)
        return -1


def _disease_name_row_count(ctx, term: str) -> int:
    """Live count of variant_disease_name rows matching `term`, using the same
    word-boundary + singular/plural matching semantics as the generic
    substring filter in dataframe_filtering.py (so this pre-check agrees with
    what the actual join/filter stage will do).
    """
    try:
        import polars as pl
        from utils.dataframe_filtering import _plural_variant
        data = (ctx.data or {}).get("clinvar") or {}
        vda_df = data.get(_VDA)
        if vda_df is None:
            return -1  # unknown — caller should not act on this
        lz = vda_df if isinstance(vda_df, pl.LazyFrame) else vda_df.lazy()
        term_lc = term.lower()
        patterns = [term_lc]
        plural = _plural_variant(term_lc)
        if plural and plural != term_lc:
            patterns.append(plural)
        col = pl.col("variant_disease_name").str.to_lowercase()
        masks = [col.str.contains(r"\b" + re.escape(p) + r"\b", literal=False) for p in patterns]
        mask = masks[0]
        for m in masks[1:]:
            mask = mask | m
        return lz.filter(mask).select(pl.len()).collect().item()
    except Exception as e:
        ctx.log.warning("[clinvar] disease-name row-count pre-check failed: %s", e)
        return -1


def _relax_disease_name_term(ctx, term: str) -> Optional[str]:
    """If `term` (as extracted/expanded upstream) has zero live matches against
    variant_disease_name, progressively drop the LEADING word and re-check —
    English qualifying/descriptive words (severity, age-of-onset, population
    descriptors, ...) conventionally precede the head noun of a disease name,
    so the trailing words are the part most likely to match ClinVar's stored
    name verbatim. Returns the longest (most specific) relaxation that gets a
    nonzero, non-generic match, or None if no relaxation (including the
    original) qualifies. Purely data-driven — no fixed list of qualifier
    words, and no fixed list of "too generic" words (see
    _RELAX_MAX_MATCH_FRACTION).
    """
    words = _DISEASE_WORD_SPLIT_RE.split(term.strip())
    if len(words) < 2:
        return None
    n = _disease_name_row_count(ctx, term)
    if n < 0:
        return None  # can't verify — leave untouched
    if n > 0:
        return None  # original already matches — nothing to relax
    total = _vda_total_rows(ctx)
    ceiling = total * _RELAX_MAX_MATCH_FRACTION if total > 0 else None
    for i in range(1, len(words)):
        candidate = " ".join(words[i:])
        n = _disease_name_row_count(ctx, candidate)
        if n <= 0:
            continue
        if ceiling is not None and n > ceiling:
            ctx.log.info(
                "[clinvar] relaxed disease-name candidate %r matched %d rows "
                "(> %.1f%% of table) — too generic, skipping", candidate, n,
                _RELAX_MAX_MATCH_FRACTION * 100,
            )
            continue
        return candidate
    return None


def _relax_unmatched_disease_terms(ctx, sk_plan) -> None:
    """Apply `_relax_disease_name_term` to any variant_disease_name filter
    value that the upstream expand/mapper/override stages handed off as a
    literal phrase with zero live matches (e.g. a term-extraction step kept a
    leading qualifier word — "pediatric gliomas" — that never appears in
    ClinVar's own disease-name text, even though the disease itself, "glioma"
    / "gliomas", is well represented).
    """
    fp = sk_plan.get("filter_plan") or {}
    vals = (fp.get(_VDA) or {}).get("variant_disease_name")
    if not isinstance(vals, list) or not vals:
        return
    changed = False
    new_vals = []
    for v in vals:
        if not isinstance(v, str) or not v.strip():
            new_vals.append(v)
            continue
        relaxed = _relax_disease_name_term(ctx, v)
        if relaxed:
            ctx.log.info(
                "[clinvar] variant_disease_name term %r had zero live matches "
                "— relaxed to %r", v, relaxed,
            )
            new_vals.append(relaxed)
            changed = True
        else:
            new_vals.append(v)
    if not changed:
        return
    fp[_VDA]["variant_disease_name"] = new_vals
    pv = sk_plan.get("parsed_value") or {}
    if isinstance(pv.get("variant_disease_name"), list):
        pv["variant_disease_name"] = new_vals
    ctx.filter_val["variant_disease_name"] = new_vals


def _ensure_readable_disease_name(ctx, sk_plan) -> None:
    """variant_disease_association_clinvar carries BOTH an opaque disease_id
    and its human-readable sibling variant_disease_name (see
    config/schema.py: this is the only ClinVar table with both). The mapper
    sometimes requests disease_id alone as the enumerated output, which
    renders as unreadable "CLINVAR_DIS:xxxx" hashes even though the readable
    name is one column away in the same already-joined table. This pairing
    is derived from the schema (disease_id has exactly one readable sibling
    in this table) — not a per-question or per-disease rule, so it applies
    uniformly regardless of what the query is actually about.
    """
    op = sk_plan.get("output_plan") or {}
    cols = op.get(_VDA)
    if not cols or "disease_id" not in cols or "variant_disease_name" in cols:
        return
    ctx.log.info(
        "[clinvar] output_plan requested disease_id without its readable "
        "sibling variant_disease_name for %s — adding it", _VDA,
    )
    op[_VDA] = list(cols) + ["variant_disease_name"]
    pv = sk_plan.get("parsed_value") or {}
    if not pv.get("variant_disease_name"):
        pv["variant_disease_name"] = "requested"


_CLINVAR_SCHEMA_COLS = {c for tbl in database_schemas["clinvar"].values() for c in tbl}


def _sync_out_cols(ctx, sk_plan) -> None:
    """Providing a custom `narrow` hook opts ClinVar out of the shared generic
    out_cols-from-plan computation in schema_kg_worker.py's `_post_expand`
    (that logic only runs in the `elif` branch when no per-DB narrow hook is
    set — see the `if cfg.narrow is not None / elif sk_plan...` there). Without
    this, `ctx.out_cols` is left at whatever stale/default value it had before
    `narrow` ran, ignoring any output_plan edits this hook (or the mapper)
    made. Re-derive it the same way the generic path does: output_plan
    columns first (what the user asked for), then filter_plan columns not
    already covered — restricted to real ClinVar schema columns.
    """
    out_cols_set: set = set()
    for cols in (sk_plan.get("output_plan") or {}).values():
        out_cols_set.update(cols)
    for col, val in (sk_plan.get("parsed_value") or {}).items():
        if val == "requested":
            out_cols_set.add(col)
    filter_cols_set: set = set()
    for cols in (sk_plan.get("filter_plan") or {}).values():
        filter_cols_set.update(cols.keys())
    out_cols_set &= _CLINVAR_SCHEMA_COLS
    filter_cols_set &= _CLINVAR_SCHEMA_COLS
    if not (out_cols_set or filter_cols_set):
        return
    filter_only = filter_cols_set - out_cols_set
    ctx.out_cols = sorted(out_cols_set) + sorted(filter_only)


async def _clinvar_narrow(ctx) -> None:
    sk_plan = (ctx.extras or {}).get("sk_plan")
    if not sk_plan:
        return
    try:
        await _clinvar_narrow_inner(ctx, sk_plan)
    finally:
        # Runs regardless of which branch above fired or returned early, so it
        # catches the FINAL variant_disease_name value no matter which path
        # produced it (plain mapper filter, literal floor, or an override
        # above).
        _relax_unmatched_disease_terms(ctx, sk_plan)
        # Repair join connectivity BEFORE re-deriving out_cols: any hook above
        # (e.g. molecular_consequence pruning) may have orphaned a plan table by
        # dropping the only join edge that reached it. Restore FK-based edges so
        # the executor never errors to 0 rows on a still-present, joinable table.
        _repair_join_connectivity(ctx, sk_plan)
        # Providing this hook opts out of the shared generic out_cols
        # computation (see _sync_out_cols docstring) — always re-derive it
        # from whatever the final sk_plan looks like, regardless of which
        # branch above fired or returned early.
        _sync_out_cols(ctx, sk_plan)


async def _clinvar_narrow_inner(ctx, sk_plan) -> None:
    _normalize_molecular_consequence(ctx, sk_plan)
    _ensure_readable_disease_name(ctx, sk_plan)

    q = (getattr(ctx.input, "cleaned_query", "") or "").strip()
    if not q:
        return

    if _guard_star_allele_terms(ctx, sk_plan):
        return  # star-allele guard fired — forced literal miss, done

    if _guard_superlative_cause(ctx, sk_plan):
        return  # superlative-cause guard fired — forced literal miss, done

    await _pharmacogenomic_lookup(ctx, sk_plan)
    if ctx.filter_val.get("gene_symbol"):
        return  # pharmacogenomic override fired — done

    disease = _extract_reverse_direction_disease(q)
    if not disease:
        return

    # If the mapper already resolved a real gene_symbol filter value, a gene WAS
    # named explicitly (e.g. "which mutations of phospholamban gene cause X" —
    # "gene" trips the WH-target regex, but the gene is already known and the
    # real ask is variant-level detail, not the gene). Forcing gene_symbol=
    # requested here would wipe the output plan down to the already-known gene
    # and throw away what's actually being asked. Back off in that case.
    existing_gene = ctx.filter_val.get("gene_symbol")
    if isinstance(existing_gene, list) and existing_gene:
        ctx.log.info(
            "[clinvar] disease-named override skipped — gene_symbol already "
            "resolved to %r, this is gene-named direction", existing_gene,
        )
        return

    ctx.log.info(
        "[clinvar] disease-named direction override: forcing gene_symbol=requested, "
        "variant_disease_name filter=%r", disease,
    )

    # Reuse the mapper's own fuzzy-resolved disease value if it already extracted
    # one (even under the wrong table topology) — it went through the same
    # ANN/value-mapper matching as any other filter, so it's more precise than
    # the raw regex capture. Fall back to the raw phrase otherwise; the join
    # stage's own semantic matching on variant_disease_name handles raw text fine.
    resolved = ctx.filter_val.get("variant_disease_name")
    dvals = resolved if resolved else [disease]
    if isinstance(dvals, list):
        dvals = dvals[:1]
    else:
        dvals = [dvals]

    sk_plan["plan_tables"] = {_VDA, _VGA, _GMT}
    sk_plan["join_path"] = [(_VDA, _VGA, "variant_id"), (_VGA, _GMT, "gene_id")]
    op = sk_plan.setdefault("output_plan", {})
    op.clear()
    op[_GMT] = ["gene_symbol"]
    fp = sk_plan.setdefault("filter_plan", {})
    fp.clear()
    fp[_VDA] = {"variant_disease_name": dvals}
    pv = sk_plan.setdefault("parsed_value", {})
    pv.clear()
    pv["variant_disease_name"] = dvals
    pv["gene_symbol"] = "requested"
    ctx.filter_val["variant_disease_name"] = dvals


_CLINVAR_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_clinvar_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_CLINVAR_CAPABILITIES,
    limitations=_CLINVAR_LIMITATIONS,
    narrow=_clinvar_narrow,
)

return_clinvar_result = make_schema_kg_handler(_CLINVAR_CONFIG)
