"""BioChirp HPO data tool — schema_kg variant.

Orchestration (LLM router, in-process schema_kg planner, web fallback, on-empty
retry) is provided by the shared `app.per_db_tool.schema_kg_worker`. This module
only injects HPO's identity + capability blurb.
"""
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_hpo


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_hpo_db = \
    setup_service_globals("hpo", "HPO", return_preprocessed_hpo)


_HPO_CAPABILITIES = (
    "- Gene-phenotype associations (gene symbols + HPO phenotype term names)\n"
    "- Disease-phenotype associations (disease names + HPO phenotype term names)\n"
    "- Gene-disease associations (gene symbols + disease names + association type: "
    "Mendelian / polygenic)\n"
    "- Phenotype-gene lookups with NCBI Entrez gene IDs\n"
    "- HPO ontology terms: definitions, term hierarchy (parent/child), synonyms\n"
    "- Detailed disease-phenotype annotations: frequency, onset, evidence code, "
    "aspect (phenotypic abnormality / clinical course / inheritance), sex, modifier, "
    "literature references"
)
_HPO_LIMITATIONS = (
    "drug-target associations, drug indications, protein 3D structures, "
    "variant pathogenicity, gene expression levels, protein-protein interactions, "
    "or general biology knowledge"
)

# Disease eponyms that differ from canonical HPO names.  The mapper LLM copies
# entity values verbatim from the query text (Rule 1), so eponym → canonical
# translation must happen in the query text before the mapper sees it.
# Keys are plain strings (re.sub uses re.IGNORECASE via the worker loop).
# Also rewrites question phrasings that confuse ANN/col_selection routing:
#   "amino acid implicated in X" → ANN returns disease_master only; rewrite to
#   "phenotype associated with X" so ANN returns phenotype columns too.
#   "phenotypes associated with heterozygous mutations of the GENE gene" → ANN
#   returns gene_phenotype columns; rewrite so col_selection picks gene_disease.
_HPO_TERM_REWRITE = {
    "Ambras syndrome": "Hypertrichosis universalis congenita Ambras type",
    "Doose syndrome": "Epilepsy with myoclonic-atonic seizures",
    "Moschcowitz syndrome": "Thrombotic thrombocytopenic purpura",
    "Heerfordt syndrome": "Heerfordt syndrome (uveoparotid fever)",
    "Allgrove syndrome": "Triple A syndrome",
    "Gardner-Diamond syndrome": "Autoerythrocyte sensitization syndrome",
    "Aagenaes syndrome": "Cholestasis-Lymphedema syndrome",
    "de Morsier syndrome": "Septo-optic dysplasia spectrum",
    "gluten allergy": "Celiac disease",
    "gluten intolerance": "Celiac disease",
    # "amino acid X implicated in disease Y" → phenotype query against disease_phenotype_annotation_hpo
    "amino acid in implicated in": "phenotype associated with",
    "amino acid is implicated in": "phenotype associated with",
    "amino acid implicated in": "phenotype associated with",
    # "phenotypes associated with heterozygous mutations" → disease query via gene_disease_association_hpo
    "phenotypes are associated with heterozygous mutations of the": "diseases are caused by mutations in the",
    "phenotypes associated with heterozygous mutations of the": "diseases caused by mutations in the",
}

_HPO_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_hpo_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_HPO_CAPABILITIES,
    limitations=_HPO_LIMITATIONS,
    term_rewrite=_HPO_TERM_REWRITE,
    # Router LLM nondeterministically skips query_db for well-curated named
    # diseases/genes (confirmed via BioASQ eval — same bug class as CTD's
    # 2026-06-23 decline fix). Opt-in only for HPO; every other DB is unaffected.
    force_query_db_first=True,
    # schema_mapper's dual-mapper/tiebreaker LLM stage is genuinely nondeterministic
    # (all 3 roles are the same Groq-served model, which doesn't reliably honor
    # temperature=0/seed=42) and occasionally returns an empty filter_plan for a
    # query that plainly names a disease/gene. Retry rather than accept a false
    # "no data" result. Opt-in only for HPO; every other DB is unaffected.
    retry_on_empty_filter_plan=True,
)

return_hpo_result = make_schema_kg_handler(_HPO_CONFIG)
