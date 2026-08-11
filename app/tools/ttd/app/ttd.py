"""BioChirp TTD data tool — schema_kg variant.

Orchestration (LLM router, in-process schema_kg planner, web fallback, on-empty
retry) is provided by the shared `app.per_db_tool.schema_kg_worker`. This module
only injects TTD's identity + capability blurb. The original HTTP-pipeline worker
is preserved at `ttd.py.pre_schema_kg`.
"""
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_ttd


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_ttd_db = \
    setup_service_globals("ttd", "Therapeutic Target Database", return_preprocessed_ttd)


_TTD_CAPABILITIES = (
    "- Drug-target associations (drug names + target gene symbols + mechanism of action)\n"
    "- Drug-disease indications (drug names + disease names + clinical status)\n"
    "- Target-disease associations (target gene symbols + disease names)\n"
    "- Target-pathway associations (KEGG pathways)\n"
    "- Disease biomarkers (biomarker names + disease names)\n"
    "- Compound bioactivity: IC50 / Ki / EC50 values (target_compound_activity)\n"
    "- Drug synonyms, cross-matching IDs (PubChem CID, ChEBI, CAS) and target UniProt xrefs"
)
_TTD_LIMITATIONS = (
    "protein 3D structures, variant pathogenicity, gene expression levels, "
    "protein-protein interactions, adverse events / side effects / toxicity (ADR) data, "
    "gene ontology (GO) terms, pathway enrichment analysis, or general biology knowledge"
)

_TTD_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_ttd_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_TTD_CAPABILITIES,
    limitations=_TTD_LIMITATIONS,
    # Clinical-maturity sort: Approved first, experimental/patented last, discontinued/withdrawn
    # at the bottom. Applied only when `approval_status` is in the query output (drug-disease,
    # target-disease joins) — inert for target-only or compound-activity queries.
    # Secondary: activity_value ascending (lower IC50/Ki/EC50 = tighter binder = more relevant).
    sort_order=[
        {"col": "approval_status",
         "order": [
             # ── Marketed ──────────────────────────────────────────────────
             "Approved",
             "Approved (orphan drug)",
             "Approved in EU",
             "Approved in China",
             "Registered",
             # ── Pre-approval filings ──────────────────────────────────────
             "Preregistration",
             "Application submitted",
             "Approval submitted",
             "BLA submitted",
             "NDA filed",
             # ── Active clinical development ───────────────────────────────
             "Phase 4",
             "Phase 3",
             "Phase 2/3",
             "Phase 2b",
             "Phase 2a",
             "Phase 2",
             "Phase 1b/2a",
             "Phase 1/2a",
             "Phase 1/2",
             "Phase 1b",
             "Phase 1",
             "Phase 0",
             "Clinical trial",
             # ── Early / non-clinical ──────────────────────────────────────
             "Preclinical",
             "IND submitted",
             "Investigative",
             "Patented",
             # ── Terminated / withdrawn (still informative, never surfaced first) ─
             "Terminated",
             "Withdrawn from market",
             "Discontinued in Phase 4",
             "Discontinued in Preregistration",
             "Discontinued in Phase 3",
             "Discontinued in Phase 2/3",
             "Discontinued in Phase 2b",
             "Discontinued in Phase 2a",
             "Discontinued in Phase 2",
             "Discontinued in Phase 1/2",
             "Discontinued in Phase 1",
         ]},
        # Compound-activity queries: tighter binders (lower IC50/Ki/EC50) surface first.
        # Skipped automatically when activity_value is absent from the result.
        {"col": "activity_value", "dir": "asc"},
    ],
)

return_ttd_result = make_schema_kg_handler(_TTD_CONFIG)
