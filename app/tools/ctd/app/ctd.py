"""BioChirp CTD data tool — schema_kg variant."""
import re
import threading
from typing import Optional

from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)
from app.per_db_tool._orchestrator import WorkerCtx

from .database_loader import return_preprocessed_ctd

# ---------------------------------------------------------------------------
# CTD drug synonym map — brand name / investigational code → canonical MeSH name
# Built lazily on first request from the loaded parquets; cached module-level.
# Same pattern as HCDT's _build_syn_map: joins chemical_synonyms_association_ctd
# with chemical_master_table_ctd so no internet API is needed.
# ---------------------------------------------------------------------------
_CTD_SYN_MAP: Optional[dict] = None
_CTD_SYN_MAP_LOCK = threading.Lock()

# Signals that a synonym is a chemical string (SMILES/InChI) rather than a
# human-readable name. Kept identical to the HCDT pattern.
_CHEM_NOISE_RE = re.compile(r"[=\[\]#@\\/]|^InChI=", re.ASCII)
_PUNCT_STRIP_RE = re.compile(r"^[^\w-]+|[^\w-]+$")


def _build_ctd_syn_map(data: dict) -> dict:
    """Collect (lowercase synonym → canonical drug_name) from loaded CTD tables.

    ctx.data is always wrapped as {'ctd': {table_name: df, ...}}; unwrap to the
    inner dict so lookups work regardless of the call site.
    """
    import logging
    import polars as pl

    log = logging.getLogger("uvicorn.error")
    # Unwrap outer {db_name: {tables}} wrapper produced by clean_table_dict()
    tables = data.get("ctd") or data
    syn_tbl    = tables.get("chemical_synonyms_association_ctd")
    master_tbl = tables.get("chemical_master_table_ctd")
    if syn_tbl is None or master_tbl is None:
        log.warning("[ctd] drug synonym map skipped — tables missing from loaded data")
        return {}
    try:
        syn_df    = syn_tbl.collect()    if hasattr(syn_tbl,    "collect") else syn_tbl
        master_df = master_tbl.collect() if hasattr(master_tbl, "collect") else master_tbl
        joined = (
            syn_df
            .join(master_df.select(["drug_id", "drug_name"]), on="drug_id", how="left")
            .filter(pl.col("drug_name").is_not_null() & pl.col("chemical_synonym").is_not_null())
            .select(["chemical_synonym", "drug_name"])
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
            if key not in result or len(name) < len(result[key]):
                result[key] = name
        log.info("[ctd] drug synonym map built: %d entries", len(result))
        return result
    except Exception as exc:
        log.warning("[ctd] drug synonym map build failed: %s", exc)
        return {}


def _get_ctd_syn_map(data: dict) -> dict:
    global _CTD_SYN_MAP
    if _CTD_SYN_MAP is None:
        with _CTD_SYN_MAP_LOCK:
            if _CTD_SYN_MAP is None:
                _CTD_SYN_MAP = _build_ctd_syn_map(data)
    return _CTD_SYN_MAP


def _rewrite_synonyms_ctd(query: str, syn_map: dict) -> str:
    """Replace brand/colloquial names with canonical MeSH drug names.

    Tries n-grams longest-first (up to 5 words) so multi-word names like
    'Bacillus Calmette Guerin Vaccine' are matched before single-word fallbacks.
    """
    words = query.split()
    out: list = []
    i = 0
    while i < len(words):
        replaced = False
        for n in range(min(5, len(words) - i), 0, -1):
            raw_slice = words[i : i + n]
            stripped = [_PUNCT_STRIP_RE.sub("", w) for w in raw_slice]
            lookup_key = " ".join(stripped).lower()
            canonical = syn_map.get(lookup_key)
            if canonical and canonical.lower() != lookup_key:
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


async def _ctd_pre_expand(ctx: WorkerCtx) -> None:
    """Rewrite drug brand/colloquial names to canonical MeSH names before routing.

    Fires BEFORE the orchestrator router and schema_mapper see the query, so
    brand names (e.g. 'Tecfidera', 'BCG immunotherapy') are presented as their
    CTD canonical names (e.g. 'Dimethyl Fumarate', 'BCG Vaccine').
    Reads directly from the loaded CTD parquets — no internet API needed.
    """
    query = ctx.inp.get("cleaned_query", "")
    if not query or not ctx.data:
        return
    syn_map = _get_ctd_syn_map(ctx.data)
    if not syn_map:
        return
    rewritten = _rewrite_synonyms_ctd(query, syn_map)
    if rewritten != query:
        ctx.inp["cleaned_query"] = rewritten
        ctx.log.info("[ctd] pre_expand drug synonym rewrite: %r → %r",
                     query[:80], rewritten[:80])


def _ctd_on_schema_map_empty(ctx: WorkerCtx, rephrased_query: str) -> Optional[str]:
    """Second-chance synonym rewrite when schema_mapper returned None.

    The orchestrator LLM may rephrase without resolving a brand name. Try the
    same synonym map on the rephrased query before falling back to the web tool.
    """
    if not ctx.data:
        return None
    syn_map = _get_ctd_syn_map(ctx.data)
    if not syn_map:
        return None
    rewritten = _rewrite_synonyms_ctd(rephrased_query, syn_map)
    return rewritten if rewritten != rephrased_query else None


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_ctd_db = \
    setup_service_globals("ctd", "Comparative Toxicogenomics Database", return_preprocessed_ctd)


# Intent keyword sets for treatment vs causation detection.
_TREATMENT_RE = re.compile(
    r"\btreat(?:s|ed|ing|ment|ments)?\b|"
    r"\btherap(?:y|ies|eutic|eutically|eutics)?\b|"
    r"\bused? (?:to|for)\b|"
    r"\butilize[sd]? for\b|"
    r"\bindicated? for\b|"
    r"\bindications? (?:for|of)\b|"
    r"\bprescribed? for\b|"
    r"\bgiven for\b|"
    r"\badminister(?:ed|ing)? (?:for|to)\b|"
    r"\bdrugs? for\b|"
    r"\bmedications? for\b|"
    r"\bhelp(?:s|ed|ing)? (?:with|treat)\b|"
    r"\balleviat(?:e|es|ed|ing)\b|"
    r"\bcure[sd]?\b|"
    r"\bremedies? for\b",
    re.IGNORECASE,
)
_CAUSATION_RE = re.compile(
    r"\bcaus(?:e|es|ed|ing|ation)?\b|"
    r"\binduc(?:e|es|ed|ing|tion)?\b|"
    r"\btrigger(?:s|ed|ing)?\b|"
    r"\blead(?:s|ing)? to\b|"
    r"\bresult(?:s|ing|ed)? in\b|"
    r"\btoxic(?:ity|ities|ant|ants)?\b|"
    r"\bmarker\b|"
    r"\bmechanism\b|"
    r"\bresponsible for\b|"
    r"\bproduc(?:e|es|ed|ing|tion)?\b",
    re.IGNORECASE,
)
# Production plan uses fully-qualified names: "ctd.chemical_disease_association_ctd"
_CHEM_DIS_TABLE = "chemical_disease_association_ctd"
_EVIDENCE_COL = "chem_disease_direct_evidence"


def _sanitize_ctd_interaction_actions(ctx: WorkerCtx) -> None:
    """Drop a malformed chemical_gene_interaction_actions filter.

    The real column values are always compound `<verb>^<property>` codes
    (e.g. "increases^expression", "affects^binding" — 137 distinct
    combinations). For an ambiguous/generic verb (modulate, affect, alter,
    regulate, ...) the schema_mapper LLM occasionally emits a bare,
    non-compound term (e.g. ["expression"] or ["activity"]) instead of
    "requested". That bare term can never equal/substring-match a real
    "<verb>^<property>" value, so it silently zeroes the whole result —
    even though gene_symbol alone would have returned the correct rows.
    This mirrors the schema_mapper's own retry-diagnosis ("the interaction
    action filter was too restrictive") but fixes it on the FIRST attempt
    instead of relying on a second LLM round-trip to notice.

    Fires for both the schema_mapper's own filter_val AND the shared
    per-DB-tool "literal floor" fallback (_orchestrator.py:141), which
    echoes the same malformed raw term when expand_and_match returns empty.
    """
    key = "chemical_gene_interaction_actions"
    val = ctx.filter_val.get(key)
    if not isinstance(val, list) or not val:
        return
    if any("^" in str(v) for v in val):
        return  # at least one well-formed compound code — trust the mapper
    ctx.log.info(
        "[ctd] dropping malformed %s filter %r (no compound '<verb>^<property>' "
        "code) — falling through to unfiltered gene_symbol match", key, val,
    )
    ctx.filter_val[key] = None
    if key in ctx.out_cols:
        pass  # keep as an output column if already requested — just not a filter


def _ctd_pre_join(ctx: WorkerCtx) -> None:
    """Inject direct_evidence filter based on treatment/causation intent.

    Runs after the schema_mapper sets filter_val. Only fires when:
      - chemical_disease_association_ctd is in the plan tables (checked by suffix)
      - chem_disease_direct_evidence isn't already a real filter (set by mapper)
    Treatment keywords → direct_evidence=['therapeutic']
    Causation keywords → direct_evidence=['marker/mechanism']
    Ambiguous → no filter (both types returned)
    """
    _sanitize_ctd_interaction_actions(ctx)

    plan_tables: list = []
    if isinstance(ctx.plan, dict):
        plan_tables = ctx.plan.get("tables", [])

    # Production plan uses "ctd.chemical_disease_association_ctd" — match by suffix
    if not any(_CHEM_DIS_TABLE in t for t in plan_tables):
        return

    # Always project the evidence column for chem-disease queries so the per-DB
    # sort_order can rank therapeutic > marker/mechanism. It is otherwise dropped
    # from out_cols when no treatment/causation keyword fires (e.g. "indications
    # for Glivec"), leaving the ordering blind. Harmless extra column otherwise.
    for _proj in (_EVIDENCE_COL, "pubmed_count"):
        if _proj not in ctx.out_cols:
            ctx.out_cols = [_proj] + ctx.out_cols

    # If the mapper already set a specific direct_evidence filter, respect it
    existing = ctx.filter_val.get(_EVIDENCE_COL)
    if isinstance(existing, list) and existing:
        return  # mapper already handled it

    # Only inject the direct_evidence filter when there is already a real entity
    # filter in filter_val (disease_name, drug_name, gene_symbol, etc.).
    # Without this guard, adding direct_evidence=['therapeutic'] alone would return
    # ALL therapeutic rows in the entire CTD database — a whole-DB partial dump.
    def _is_real(v) -> bool:
        return isinstance(v, list) and any(
            x and str(x).strip().lower() not in ("", "requested") for x in v
        )

    entity_cols = {k for k, v in ctx.filter_val.items()
                   if k != _EVIDENCE_COL and _is_real(v)}
    if not entity_cols:
        return  # no entity filter set yet — evidence filter alone would dump the DB

    # ctx.inp is always model_dump() dict; get cleaned_query
    _inp = ctx.inp
    if isinstance(_inp, dict):
        query = (_inp.get("cleaned_query") or "").lower()
    else:
        query = (getattr(_inp, "cleaned_query", None) or "").lower()

    if _TREATMENT_RE.search(query):
        ctx.filter_val[_EVIDENCE_COL] = ["therapeutic"]
        if _EVIDENCE_COL not in ctx.out_cols:
            ctx.out_cols = [_EVIDENCE_COL] + ctx.out_cols
    elif _CAUSATION_RE.search(query):
        ctx.filter_val[_EVIDENCE_COL] = ["marker/mechanism"]
        if _EVIDENCE_COL not in ctx.out_cols:
            ctx.out_cols = [_EVIDENCE_COL] + ctx.out_cols
    # else: ambiguous — no filter, return all rows


def _ctd_narrow(ctx: WorkerCtx) -> None:
    """Prune drug_name candidates that have no associations in any CTD table.

    expand_and_match fuzzy search on a user term (e.g. "glibenclamide") can
    return derivative/metabolite names that substring-match (e.g.
    "4-hydroxyglibenclamide", C070073) ahead of the canonical MeSH D-entry
    ("Glyburide", D005905). Those derivatives have 0 rows in every association
    table and silently zero-out query results → LLM fallback.

    This hook checks each resolved drug_name against the precomputed
    _active_drug_ids set (built at load time in database_loader.py). If at
    least one candidate is active, inactive ones are dropped. When ALL
    candidates are inactive (genuinely novel/unmapped drug), the full list is
    kept so the query still runs and gets 0 rows — graceful degradation, not
    silent data loss.

    Runs as the CTD `narrow` hook inside _build_post_expand, before the plan
    is set, so the planner sees the pruned filter_val.
    """
    drug_names = ctx.filter_val.get("drug_name")
    if not isinstance(drug_names, list) or len(drug_names) < 2:
        return  # nothing to prune with a single candidate

    db = ctx.data.get("ctd", {})
    name_to_id: dict = db.get("_drug_name_lower_to_id", {})
    active_ids: set = db.get("_active_drug_ids", set())

    if not name_to_id or not active_ids:
        return  # precomputed lookups absent — skip gracefully

    active = [n for n in drug_names if name_to_id.get(n.lower()) in active_ids]

    if not active or len(active) == len(drug_names):
        return  # nothing to prune, or all active

    pruned = [n for n in drug_names if n not in set(active)]
    ctx.log.info(
        "[ctd] narrow: dropped %d zero-association drug_name candidate(s): %s",
        len(pruned), pruned,
    )
    ctx.filter_val["drug_name"] = active
    if ctx.expand_response and isinstance(ctx.expand_response.get("value"), dict):
        ctx.expand_response["value"]["drug_name"] = active


_CTD_CAPABILITIES = (
    "- Chemical / drug records: names, synonyms, definitions, and IDENTIFIERS — "
    "CAS Registry Number, PubChem CID, InChIKey, MeSH chemical ID\n"
    "- Gene records: official HGNC symbols, full gene names, synonyms, UniProt accessions\n"
    "- Disease records: names, synonyms, definitions, MeSH/OMIM identifiers\n"
    "- Chemical-gene interactions in BOTH directions (which chemicals affect a gene, "
    "AND which genes a chemical affects), including the action — increase/decrease of "
    "expression, activity, methylation, phosphorylation, stability, etc. — and the "
    "gene form involved (protein, mRNA)\n"
    "- Chemical-disease associations (curated marker/mechanism + therapeutic + inferred), both directions\n"
    "- Gene-disease associations, both directions\n"
    "- Chemical-phenotype interactions (GO biological processes) and the anatomical sites where they occur\n"
    "- Gene / disease / chemical pathway associations (KEGG, Reactome) and chemical pathway / GO enrichment\n"
    "- Exposure studies and exposure events: environmental chemical stressors, study "
    "populations, biomarkers, geography, and measured outcomes\n"
    "- Curated toxicogenomic relationships between environmental chemicals, genes, "
    "diseases, pathways and phenotypes"
)
_CTD_LIMITATIONS = (
    "protein 3D structures, quantitative binding-affinity (IC50/Ki/EC50) values, "
    "genetic variant pathogenicity, and protein-protein interaction networks"
)

_CTD_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_ctd_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_CTD_CAPABILITIES,
    limitations=_CTD_LIMITATIONS,
    narrow=_ctd_narrow,
    pre_join=_ctd_pre_join,
    # Curated-evidence ordering: therapeutic associations rank above
    # marker/mechanism, then (within a tier) semantic relevance is preserved.
    # Keys whose column is absent in a given query are skipped automatically.
    sort_order=[
        {"col": "chem_disease_direct_evidence",
         "order": ["therapeutic", "marker/mechanism|therapeutic", "marker/mechanism"]},
        {"col": "gene_disease_direct_evidence",
         "order": ["marker/mechanism|therapeutic", "marker/mechanism"]},
        # NOTE: deliberately NO chem-gene interaction-action key here. Tested 2026-06-27
        # (bortezomib->proteasome): CTD curates the mechanistic target (PSMB5) with only 1
        # co-mention vs 74 for downstream effectors (CASP3) — ordering can't surface a barely-
        # curated target, and a binding-first key would risk regressing the load-bearing
        # pubmed_count ranking that the chem-gene DuckDB view relies on (BPA->ESR1/ESR2/AR).
        # This class is a CTD curation-depth limitation, not an ordering bug.
        {"col": "pubmed_count", "dir": "desc"},   # study depth, within evidence tier
    ],
    pre_expand=_ctd_pre_expand,
    on_schema_map_empty=_ctd_on_schema_map_empty,
    # MeSH disease term rewrites: applied case-insensitively to the query BEFORE
    # the schema_mapper LLM sees it. These cover disease terms that are absent from
    # disease_synonyms_association_ctd_v2.parquet (e.g. hypolipidemia, ph+ all) or
    # map to a different canonical name than the parquet entry (hemangioma subtypes).
    # Drug brand names are handled dynamically via _ctd_pre_expand / _ctd_on_schema_map_empty
    # reading from chemical_synonyms_association_ctd — no hardcoding needed.
    term_rewrite={
        "infantile hemangioma": "Hemangioma",
        "hypolipidemia": "Hypobetalipoproteinemia",
        "low cholesterol": "Hypobetalipoproteinemia",
        "low ldl": "Hypobetalipoproteinemia",
        "ph+ all": "Precursor Cell Lymphoblastic Leukemia-Lymphoma",
        "philadelphia chromosome-positive all": "Precursor Cell Lymphoblastic Leukemia-Lymphoma",
    },
    # schema_mapper's dual-mapper/tiebreaker LLM stage is genuinely nondeterministic
    # (all 3 roles are the same Groq-served model, which doesn't reliably honor
    # temperature=0/seed=42) — confirmed on CTD via repeated identical queries
    # returning thousands of rows on one run and "No rows matched" on the next.
    # Retry rather than accept a false "no data" result. Opt-in only for CTD.
    retry_on_empty_filter_plan=True,
)

return_ctd_result = make_schema_kg_handler(_CTD_CONFIG)
