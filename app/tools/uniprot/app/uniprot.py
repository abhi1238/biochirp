"""BioChirp UniProt data tool — schema_kg variant.

Orchestration (LLM router, in-process schema_kg planner, web fallback, on-empty
retry) is provided by the shared `app.per_db_tool.schema_kg_worker`. This module
only injects UniProt's identity + capability blurb. The original HTTP-pipeline
worker is preserved at `uniprot.py.pre_schema_kg`.
"""
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_uniprot


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_uniprot_db = \
    setup_service_globals("uniprot", "UniProt", return_preprocessed_uniprot)


_UNIPROT_CAPABILITIES = (
    "- Protein identity: UniProt accession, entry name (mnemonic), full protein name, "
    "gene symbol, organism (Swiss-Prot reviewed human proteins; 20,431 entries)\n"
    "- Cross-reference IDs: Ensembl gene ID, NCBI Entrez ID, HGNC ID, plus an id_mapping "
    "table to RefSeq, GeneID, HGNC, Ensembl, UniProtKB-ID and other external databases "
    "(note: PDB cross-references are NOT present in this snapshot)\n"
    "- Gene Ontology annotations per protein: GO ID, aspect (F/P/C — molecular function, "
    "biological process, cellular component), qualifier, evidence code\n"
    "- Functional keywords (UniProt controlled vocabulary: keyword name, category, "
    "hierarchy, GO cross-references)\n"
    "- Subcellular locations (controlled vocabulary: location name, hierarchy, GO links)\n"
    "- Natural variant–disease associations: protein change (HGVS p. notation), variant "
    "clinical significance (pathogenic/benign/uncertain), dbSNP rsID, associated disease name\n"
    "- Protein function annotations: free-text CC FUNCTION descriptions per protein "
    "(17k proteins; use for 'what does protein X do?' or 'function of gene Y')\n"
    "- Post-translational modification (PTM) sites: modification type (e.g. Phosphoserine), "
    "sequence position, modifying enzyme gene symbol (where curated)\n"
    "- Protein-protein interactions: SUBUNIT (free-text complex/binding descriptions from "
    "CC SUBUNIT) and BINARY (structured IntAct interactions with experiment counts), "
    "with interacting partner gene symbol\n"
    "- Species master table: species code, NCBI taxon ID, scientific + common names\n"
    "NOT available in this snapshot: drug-target cross-references (use OpenTargets or "
    "ChEMBL), PDB structure cross-references, amino-acid sequences"
)
_UNIPROT_LIMITATIONS = (
    "protein 3D structures or coordinates (including PDB cross-references), amino-acid "
    "sequences, gene expression levels, kinase-substrate or E3 ligase-substrate "
    "relationship tables, drug-target associations (use OpenTargets/ChEMBL), or general "
    "biology knowledge"
)

_UNIPROT_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_uniprot_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_UNIPROT_CAPABILITIES,
    limitations=_UNIPROT_LIMITATIONS,
    # UniProt PTM enzyme column stores abbreviations ("PKA", "CaMK2") not full names.
    term_rewrite={"Protein kinase A": "PKA"},
)

return_uniprot_result = make_schema_kg_handler(_UNIPROT_CONFIG)
