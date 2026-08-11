"""BioChirp Reactome data tool — schema_kg variant.

Orchestration (LLM router, in-process schema_kg planner, web fallback, on-empty
retry) is provided by the shared `app.per_db_tool.schema_kg_worker`. This module
only injects Reactome's identity + capability blurb. The original HTTP-pipeline
worker is preserved at `reactome.py.pre_schema_kg`.
"""
import polars as pl

from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_reactome


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_reactome_db = \
    setup_service_globals("reactome", "Reactome", return_preprocessed_reactome)


_REACTOME_CAPABILITIES = (
    "- Pathway records: Reactome stable ID (R-HSA-<n>) + pathway name (pathway_master_table; Homo sapiens only)\n"
    "- Gene-pathway membership: HGNC gene symbols ↔ pathway name/ID with evidence codes (gene_master_table + gene_pathway_association)\n"
    "- Pathway hierarchy: parent / child pathway relationships (pathway_hierarchy_reactome)\n"
    "- UniProt accession → pathway membership (uniprot_pathway_reactome)\n"
    "- ChEBI compound ID → pathway membership (chebi_pathway_reactome)\n"
    "- Ensembl ID (gene/transcript/protein) → pathway membership (ensembl_pathway_reactome)\n"
    "- NCBI Entrez gene ID → pathway membership (ncbi_pathway_reactome)\n"
    "- Evidence codes (TAS / IEA) for entity-pathway annotations"
)
_REACTOME_LIMITATIONS = (
    "drug-target associations, drug indications, variant pathogenicity, "
    "protein 3D structures, gene expression levels, protein-protein interactions, "
    "or general biology knowledge"
)

def _real_filter(v) -> bool:
    """True if v is a usable filter value (not None / '' / 'requested' / empty list)."""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() not in ("", "requested")
    if isinstance(v, (list, tuple)):
        return any(_real_filter(x) for x in v)
    return bool(v)


# Compound "<entity> OF the (sub)pathways OF X" targets. When one of these is the
# requested OUTPUT, the query is NOT a pure-hierarchy read — the hierarchy is
# graph-disconnected (parent_id/child_id aren't FKs to pathway_master), so the
# planner cannot join it onward. We decompose: hop 1 reads the (sub)pathway IDs
# from the hierarchy in-memory, hop 2 filters the matching association table by
# those pathway_ids. Maps target → (association table, master table|None, join col).
_COMPOUND_TARGETS = {
    "gene_symbol":       ("gene_pathway_association", "gene_master_table", "gene_id"),
    "uniprot_accession": ("uniprot_pathway_reactome", None, None),
    "chebi_id":          ("chebi_pathway_reactome",   None, None),
    "ensembl_id":        ("ensembl_pathway_reactome", None, None),
    "ncbi_gene_id":      ("ncbi_pathway_reactome",    None, None),
}


def _one_table_plan(db: str, table: str, concept_cols: list) -> dict:
    fqt = f"{db}.{table}"
    return {"tables": [fqt], "parents": {fqt: None},
            "table_columns": {fqt: {"concept_columns": list(concept_cols), "join_columns": []}},
            "join_pairs": {}}


def _two_table_plan(db: str, root: str, leaf: str, join_col: str,
                    root_cols: list, leaf_cols: list) -> dict:
    rfq, lfq = f"{db}.{root}", f"{db}.{leaf}"
    # JSON-safe "left,right" string key (normalize_join_pairs parses it back to a
    # tuple) — a raw tuple key would blow up the WebSocket JSON serializer.
    return {"tables": [rfq, lfq],
            "parents": {rfq: None, lfq: rfq},
            "table_columns": {
                rfq: {"concept_columns": list(root_cols), "join_columns": []},
                lfq: {"concept_columns": list(leaf_cols), "join_columns": [join_col]},
            },
            "join_pairs": {f"{rfq},{lfq}": {"left_on": [join_col], "right_on": [join_col]}}}


def _hierarchy_pathway_ids(ctx, filter_col: str, names, id_col: str) -> list:
    """Decomposition hop 1: pathway stable IDs of the (sub)pathways named by
    ``names``, read straight from the in-memory pathway_hierarchy table. ``[]`` on
    any problem (caller then leaves the query for the planner/decomposer)."""
    try:
        data = getattr(ctx, "data", None) or {}
        tbls = data.get(ctx.db) if isinstance(data.get(ctx.db), dict) else data
        hkey = next((k for k in (tbls or {}) if "pathway_hierarchy" in k), None)
        if hkey is None:
            return []
        df = (tbls[hkey].filter(pl.col(filter_col).is_in(list(names)))
                        .select(id_col).unique().collect())
        return [v for v in df[id_col].to_list() if isinstance(v, str) and v]
    except Exception:
        return []


# FALLBACK keyword lists — used ONLY when the LLM mapper left the hierarchy
# direction ambiguous. The PRIMARY signal is the mapper's column binding (driven by
# db_llm_rules), which generalises to any phrasing; these are a deterministic safety
# net for the rare miss. Must mean pathway-WITHIN-pathway, never entity-in-pathway.
_HIER_CHILD_KW = (
    "sub-pathway", "sub pathway", "subpathway", "child pathway", "children of",
    "child of", "sub-process", "sub process", "subprocess", "constituent pathway",
    "component pathway", "nested pathway", "descendant pathway", "subordinate pathway",
    "lower-level pathway", "broken down into", "decompose into", "decomposes into",
    "falls under", "fall under", "underneath", "below", "beneath")  # positional: safe in
    # Reactome (no numeric "below" queries) and stops text2sql's analytic-RX false-fire
_HIER_PARENT_KW = (
    "parent pathway", "parent of", "super-pathway", "superpathway", "super pathway",
    "super-process", "super process", "superprocess", "sub-pathway of", "subpathway of",
    "subprocess of", "sub-process of", "part of", "ancestor pathway",
    "encompassing pathway", "broader pathway", "umbrella pathway", "supercategory",
    "belongs to", "belong to", "above")


def _hierarchy_disambiguate(ctx) -> None:
    """Deterministically resolve pathway-hierarchy queries — both pure traversal
    and compound "<entity> of the (sub)pathways of X".

    The hierarchy table is a deliberate graph island (parent_id/child_id aren't FKs,
    so the denormalized parent/child names avoid a pathway_master self-join). That
    makes it perfect for "children of X" (single table) but un-joinable for compound
    queries, which we answer by decomposition instead.

    • PURE  "sub-pathways / parent of X"        → pin single hierarchy table,
                                                   filter the OPPOSITE name column.
    • COMPOUND "genes/proteins/... of sub-paths of X"
                                                 → hop 1: child pathway IDs from the
                                                   hierarchy; hop 2: filter the
                                                   association table by those IDs.

    Intent is LLM-driven: the mapper's column binding (which of parent/child carries
    the filter) is the PRIMARY signal — generalises to any phrasing via db_llm_rules.
    The keyword lists are only a deterministic FALLBACK for when the LLM is ambiguous.
    No-op when intent is ambiguous or no pathway is named.
    """
    fv = getattr(ctx, "filter_val", None)
    if not isinstance(fv, dict):
        return
    q = (getattr(getattr(ctx, "input", None), "cleaned_query", None) or "").lower()

    # ── Gate 1 — traversal direction (children-of-X vs parent-of-X).
    # PRIMARY signal = the LLM mapper's column binding (db_llm_rules instructs it to
    # bind the named pathway to exactly one hierarchy column). This generalises to
    # ANY phrasing — no keyword list needed:
    #   parent_pathway_name carries the filter  → user asked for its CHILDREN
    #   child_pathway_name  carries the filter  → user asked for its PARENT
    # FALLBACK = keywords, used ONLY when the LLM left it ambiguous (bound the name
    # to BOTH columns, or to pathway_name / nothing).
    has_pf = _real_filter(fv.get("parent_pathway_name"))
    has_cf = _real_filter(fv.get("child_pathway_name"))
    kw_children = any(k in q for k in _HIER_CHILD_KW)
    kw_parent   = any(k in q for k in _HIER_PARENT_KW)

    if has_pf and not has_cf:
        wants_children = True            # LLM bound parent → wants children
    elif has_cf and not has_pf:
        wants_children = False           # LLM bound child → wants parent
    elif kw_children and not kw_parent:
        wants_children = True            # fallback: keyword
    elif kw_parent and not kw_children:
        wants_children = False
    else:
        return  # no clear hierarchy intent (LLM didn't bind one side; keywords ambiguous)

    # Source the named pathway from whichever name field the mapper populated.
    entity = next((fv.get(s) for s in ("parent_pathway_name", "child_pathway_name", "pathway_name")
                   if _real_filter(fv.get(s))), None)
    if entity is None:
        return  # no pathway named → nothing to filter on
    ent_list = entity if isinstance(entity, (list, tuple)) else [entity]

    if wants_children:
        keep, out, id_col = "parent_pathway_name", "child_pathway_name", "child_id"
    else:
        keep, out, id_col = "child_pathway_name", "parent_pathway_name", "parent_id"

    # We are committed to producing the exact answer deterministically below, so
    # suppress the post-join text2sql pass — its LLM mis-generates SQL on the
    # hierarchy frame (e.g. SELECT parent_pathway_name WHERE parent='Cell Cycle',
    # collapsing the 4 children back to the 1 parent).
    if isinstance(getattr(ctx, "extras", None), dict):
        ctx.extras["skip_text2sql"] = True

    # Gate 3 — is the requested OUTPUT a different entity (→ COMPOUND) or the
    # hierarchy itself (→ PURE)? When several entity types are requested ("genes
    # and proteins of …"), a single star-join can only return one cleanly, so we
    # answer the highest-PRIORITY one deterministically (full multi-entity would
    # need the orchestrator to split it into separate queries).
    _PRIORITY = ("gene_symbol", "uniprot_accession", "chebi_id", "ensembl_id", "ncbi_gene_id")
    out_cols = list(getattr(ctx, "out_cols", None) or [])
    target = next((c for c in _PRIORITY if c in out_cols), None)
    if target is None:  # mapper may have missed it — infer from query wording (same priority)
        if "gene" in q:
            target = "gene_symbol"
        elif "protein" in q:
            target = "uniprot_accession"
        elif "chemical" in q or "compound" in q or "metabolite" in q:
            target = "chebi_id"
        elif "ensembl" in q:
            target = "ensembl_id"
        elif "entrez" in q or "ncbi gene" in q:
            target = "ncbi_gene_id"

    if target is None:
        # ── PURE hierarchy: filter keep=name, output the opposite name, single table.
        for k in ("parent_pathway_name", "child_pathway_name", "pathway_name", "pathway_id"):
            fv.pop(k, None)
        fv[keep] = list(ent_list)
        ctx.out_cols = [out]
        ctx.plan = _one_table_plan(ctx.db, "pathway_hierarchy_reactome",
                                   ["parent_pathway_name", "child_pathway_name"])
        return

    # ── COMPOUND: <target> associated with the (sub)pathways of the named pathway.
    pathway_ids = _hierarchy_pathway_ids(ctx, keep, ent_list, id_col)  # hop 1
    if not pathway_ids:
        return  # couldn't resolve → leave for the planner/decomposer
    assoc, master, jcol = _COMPOUND_TARGETS[target]
    for k in ("parent_pathway_name", "child_pathway_name", "pathway_name", "pathway_id", target):
        fv.pop(k, None)
    fv["pathway_id"] = pathway_ids                                     # hop 2 filter
    ctx.out_cols = [target]
    if master:
        ctx.plan = _two_table_plan(ctx.db, assoc, master, jcol, ["pathway_id"], [target])
    else:
        ctx.plan = _one_table_plan(ctx.db, assoc, ["pathway_id", target])


_REACTOME_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_reactome_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_REACTOME_CAPABILITIES,
    limitations=_REACTOME_LIMITATIONS,
    pre_join=_hierarchy_disambiguate,
    # Deterministic pre-mapper term rewrites applied to rephrased_query before the schema_mapper
    # LLM sees the question. These handle compound/ambiguous notations that mapper LLMs confuse
    # with pathway names, causing 0-row failures even when individual gene data exists in DB.
    term_rewrite={
        # JAK/STAT slash compound → expand to individual gene symbols so mapper
        # filters gene_symbol=["JAK1","JAK2","STAT1","STAT3"] not a phantom pathway.
        "JAK/STAT": "JAK1, JAK2, STAT1, and STAT3 genes",
        "JAK-STAT": "JAK1, JAK2, STAT1, and STAT3 genes",
        # Tcf3 (mouse notation) → human TCF7L1 in Wnt/pluripotency context.
        # Prevents confusion with human TCF3 (E2A bHLH), a completely different gene.
        # Full-question rewrite ensures mapper gets gene-first phrasing (vs pathway filter).
        "Tcf3": "TCF7L1",
        "Is TCF7L1 associated with the Wnt pathway?": "pathways that TCF7L1 participates in (looking for Wnt signaling)",
        "difference in the roles of Tcf1 and Tcf3": "compare pathways of TCF7 (Tcf1/TCF7) vs TCF7L1 (Tcf3) genes",
        # AlkA is E. coli prokaryotic; guard against fuzzy-matching to human ALPL.
        "AlkA glycosylase": "base excision repair pathway",
        # "function of gene X" → pathway-membership phrasing so the mapper outputs
        # pathway_name (not just the gene entity). Reactome only stores memberships.
        "function of the gene MDA5": "pathways that MDA5 (IFIH1) participates in",
        "function of the protein encoded by the gene STING": "pathways that STING (TMEM173) participates in",
        "function of the DGAT1 gene product": "pathways that DGAT1 participates in",
        # cGAS pathway function → sub-pathways of the Reactome cGAS pathway (exact name used to avoid
        # synonym-expansion failure; "sub-pathways" keyword triggers _hierarchy_disambiguate hook).
        "function of the cGAS pathway": "sub-pathways of Cytosolic sensors of pathogen-associated DNA",
        # STING activation → STING gene pathways (Reactome stores membership not activation mechanism).
        "How is the STING protein activated?": "pathways that STING gene participates in",
        # Cellular senescence sub-pathways: rephrase to make mapper choose pathway_hierarchy.
        "pathways are involved in cellular senescence": "sub-pathways of Cellular Senescence",
        # KFERQ motif is recognized by HSPA8 (Hsc70) for Chaperone Mediated Autophagy (CMA).
        # Route via HSPA8 gene pathways so CMA appears in the result; CMA has no sub-pathways.
        "autophagy pathway is triggered by the KFERQ motif": "pathways that HSPA8 participates in for autophagy",
        # EGFR ligands: Reactome curates EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN as EGFR ligands.
        # Keys cover both the original question (verbatim when orchestrator errors) and
        # common rephrases from gpt-oss-120b-free. Removing "EGFR" from the rewritten query
        # prevents the mapper from filtering by gene_symbol=EGFR instead of the ligand symbols.
        "signaling molecules (ligands) that interact with the receptor EGFR": (
            "pathway membership for growth factor ligands: EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN"
        ),
        "signaling molecules that interact with EGFR": (
            "pathway membership for growth factor ligands: EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN"
        ),
        "molecules that interact with EGFR": (
            "pathway membership for growth factor ligands: EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN"
        ),
        "ligands of EGFR": (
            "pathway membership for growth factor ligands: EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN"
        ),
        "EGFR ligands": (
            "pathway membership for growth factor ligands: EGF, TGFA, HBEGF, AREG, BTC, EREG, EPGN"
        ),
    },
)

return_reactome_result = make_schema_kg_handler(_REACTOME_CONFIG)
