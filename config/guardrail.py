from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, Extra, Field, constr, field_validator, model_validator
import os
import re

# Hard ceiling on any user-supplied query string reaching a BioChirp service.
# Every entrypoint — interpreter, orchestrator, readme, memory — is bounded
# here to prevent a single oversized POST from exhausting memory in the LLM
# pipeline before validation can reject it. 5000 chars is configurable via
# MAX_QUERY_LENGTH so prod can tune the cap in one place.
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "5000"))

# Shared annotated type used by every entrypoint guardrail. Centralised so a
# future change (lowering the cap, adding a regex, normalising whitespace)
# happens in one place. strip_whitespace=False preserves the raw query for
# downstream interpreters that rely on layout (e.g. PubMed-style operators).
BoundedQuery = constr(max_length=MAX_QUERY_LENGTH)


class WebToolInput(BaseModel):
    query: BoundedQuery = Field(..., example="What is drug for tb?")
    # 2026-05-18: `general` short-circuits the biomedical Agent loop and
    # runs a single DDG + LLM call against the OOD prompt — used by per-DB
    # chats and bio_chat when the router classifies a query NON_BIOMEDICAL.
    # Default keeps the existing biomedical fallback behaviour.
    mode: Literal["biomedical", "general"] = Field(default="biomedical")



class WebSnippet(BaseModel):
    """2026-05-17: typed snippet record so WebToolOutput's strict JSON
    schema (used by the Agents-SDK output_type) accepts the list field."""
    title: str = Field(default="", description="Result title")
    url: str = Field(default="", description="Result URL (acts as citation target)")
    body: str = Field(default="", description="Result snippet body")


class WebToolOutput(BaseModel):
    message: str = Field(
        ...,
        description="Search results or error message",
        example="The Taj Mahal is located in Agra, India."
    )
    tool: str = Field(
        default="web",
        description="Tool identifier",
        example="web"
    )
    # 2026-05-17 (Fix 3b): expose the search snippets that the LLM was
    # given so callers can verify every cited URL came from the supplied
    # source set. Typed WebSnippet (not bare dict) so strict JSON schema
    # accepts the field. Populated server-side by run_web_search from a
    # ContextVar — the LLM does NOT need to fill this.
    snippets: list[WebSnippet] = Field(
        default_factory=list,
        description="Source snippets shown to the LLM. Populated server-side.",
    )
    
    @field_validator('message')
    @classmethod
    def clean_message(cls, v: str) -> str:
        """Remove control characters that break JSON parsing."""
        if not v:
            return v
        # Replace newlines with spaces
        v = v.replace('\n', ' ').replace('\r', ' ')
        # Remove any remaining control characters
        v = re.sub(r'[\x00-\x1F\x7F]', ' ', v)
        # Collapse multiple spaces
        v = re.sub(r'\s+', ' ', v)
        return v.strip()

class TavilyInput(BaseModel):
    query: BoundedQuery = Field(..., example="What is drug for tb?")



class TavilyOutput(BaseModel):
    message: str = Field(..., example="Default response from Tavily tool is returned..")
    tool: str = Field(..., example="tavily")

class ReadmeInput(BaseModel):
    query: BoundedQuery = Field(..., example="What I can ask you?")

class ReadmeOutput(BaseModel):
    # Field now contains ONLY the answer
    answer: str = Field(..., example="Default response from README API is returned..")
    tool: str = Field(...,  example="readme")
    message: str =  Field(..., example="Sucessfuly finished readme tool call.")

class CommonFields(BaseModel):
    """All biomedical schema fields, used for input, query, and output stages."""
    drug_name: Optional[Union[str, List[str],  None]] = Field(default=None, example="requested")
    target_name: Optional[Union[str, List[str], None]] = Field(default=None)
    gene_name: Optional[Union[str, List[str], None]] = Field(default=None)
    gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)
    disease_name: Optional[Union[str, List[str], None]] = Field(default=None, example=["Fever"])
    pathway_name: Optional[Union[str, List[str], None]] = Field(default=None)
    biomarker_name: Optional[Union[str, List[str], None]] = Field(default=None)
    drug_mechanism_of_action_on_target: Optional[Union[str, List[str], None]] = Field(default=None)
    approval_status: Optional[Union[str, List[str], None]] = Field(default=None)
    variant_name: Optional[Union[str, List[str], None]] = Field(default=None)
    # DB-specific fields for databases with non-standard column names
    mesh_term: Optional[Union[str, List[str], None]] = Field(default=None)
    chemical_name: Optional[Union[str, List[str], None]] = Field(default=None)
    geneset_name: Optional[Union[str, List[str], None]] = Field(default=None)
    tf_name: Optional[Union[str, List[str], None]] = Field(default=None)
    # HGNC-specific filter fields
    locus_type: Optional[Union[str, List[str], None]] = Field(default=None)
    locus_group: Optional[Union[str, List[str], None]] = Field(default=None)
    location: Optional[Union[str, List[str], None]] = Field(default=None)
    prev_symbol: Optional[Union[str, List[str], None]] = Field(default=None)   # hgnc.gene_master_table (pipe-delimited prior gene symbols)
    alias_symbol: Optional[Union[str, List[str], None]] = Field(default=None)  # hgnc.gene_master_table (alternative gene symbols)
    prev_name: Optional[Union[str, List[str], None]] = Field(default=None)     # hgnc.gene_master_table (prior full gene name)
    alias_name: Optional[Union[str, List[str], None]] = Field(default=None)    # hgnc.gene_master_table (alternative full gene name)
    # PPI / interaction output fields
    gene_partner_id: Optional[Union[str, List[str], None]] = Field(default=None)
    protein_partner_id: Optional[Union[str, List[str], None]] = Field(default=None)
    substrate_id: Optional[Union[str, List[str], None]] = Field(default=None)
    # Database-specific concept fields (enable filtering on these columns)
    collection: Optional[Union[str, List[str], None]] = Field(default=None)         # msigdb
    phenotype_name: Optional[Union[str, List[str], None]] = Field(default=None)     # hpo
    # Phenotype-ID lookup (HP:NNNNNNN). Distinct from `phenotype_name`. HPO
    # exposes this on phenotype_master_table; Orphanet uses it in
    # disease_phenotype_association.
    phenotype_id: Optional[Union[str, List[str], None]] = Field(default=None)       # hpo, orphanet (HP:NNNNNNN)
    # Pathway-ID lookup (Reactome R-HSA-NNNNNNN / R-MMU-NNNNNNN; WikiPathways
    # WPNNN — the WikiPathways loader filters by `pathway_id` after
    # renaming the upstream column).
    pathway_id: Optional[Union[str, List[str], None]] = Field(default=None)         # reactome, wikipathways
    uniprot_accession: Optional[Union[str, List[str], None]] = Field(default=None)  # reactome.uniprot_pathway_association
    # DrugCentral stores MoA as free-text in act_table_full_drugcentral.moa.
    # Distinct from TTD's `drug_mechanism_of_action_on_target` (Title-Case enum).
    moa: Optional[Union[str, List[str], None]] = Field(default=None)                 # drugcentral.act_table_full
    # ── TTD v2 fields (2026-05-11) ─────────────────────────────────────────
    synonym: Optional[Union[str, List[str], None]] = Field(default=None)            # ttd.drug_synonyms_association
    uniprot_xref: Optional[Union[str, List[str], None]] = Field(default=None)       # ttd.target_uniprot_association (also HCDT)
    target_type: Optional[Union[str, List[str], None]] = Field(default=None)        # ttd.target_uniprot_association
    activity_type: Optional[Union[str, List[str], None]] = Field(default=None)      # ttd.target_compound_activity_association (IC50/Ki/EC50)
    activity_operator: Optional[Union[str, List[str], None]] = Field(default=None)  # ttd.target_compound_activity_association (=, >, <)
    activity_value: Optional[Union[str, List[str], None]] = Field(default=None)     # ttd.target_compound_activity_association (numeric in nM)
    activity_unit: Optional[Union[str, List[str], None]] = Field(default=None)      # ttd.target_compound_activity_association
    drug_compound_id: Optional[Union[str, List[str], None]] = Field(default=None)   # ttd.target_compound_activity_association
    pubchem_cid: Optional[Union[str, List[str], None]] = Field(default=None)        # ttd.drug_crossmatching_association (also other DBs)
    pubchem_sid: Optional[Union[str, List[str], None]] = Field(default=None)        # ttd.drug_crossmatching_association
    cas_number: Optional[Union[str, List[str], None]] = Field(default=None)         # ttd.drug_crossmatching_association
    cas: Optional[Union[str, List[str], None]] = Field(default=None)                 # cross-DB alias for cas_number (used by DrugCentral); TTD service coerces cas→cas_number
    chebi_xref: Optional[Union[str, List[str], None]] = Field(default=None)         # ttd.drug_crossmatching_association
    superdrug_atc: Optional[Union[str, List[str], None]] = Field(default=None)      # ttd.drug_crossmatching_association
    superdrug_cas: Optional[Union[str, List[str], None]] = Field(default=None)      # ttd.drug_crossmatching_association
    formula: Optional[Union[str, List[str], None]] = Field(default=None)            # ttd.drug_crossmatching_association
    # ── TRRUST v2 fields (2026-05-11) ──────────────────────────────────────
    # Categorical enum: free-form list (Activation / Repression / Unknown) is
    # tiny so the interpreter could in principle be locked to a Literal[],
    # but TRRUST also surfaces casing variants ("Unknown" vs "Other"), so we
    # keep it as a free string and rely on fuzzy/exact match downstream.
    regulation_type: Optional[Union[str, List[str], None]] = Field(default=None)    # trrust.tf_gene_association
    pubmed_ids: Optional[Union[str, List[str], None]] = Field(default=None)         # trrust.tf_gene_association (also CTD, etc.)
    organism: Optional[Union[str, List[str], None]] = Field(default=None)           # trrust.tf_gene_association (Homo sapiens / Mus musculus)
    # ── HCDT v2 RNA fields (2026-05-12) ────────────────────────────────────
    rna_name: Optional[Union[str, List[str], None]] = Field(default=None)           # hcdt.drug_rna_association (RNA symbol / name)
    rna_type: Optional[Union[str, List[str], None]] = Field(default=None)           # hcdt.drug_rna_association (miRNA / lncRNA / etc.)
    # ── UniProt entry fields (2026-05-12) ─────────────────────────────────
    accession: Optional[Union[str, List[str], None]] = Field(default=None)          # uniprot (P04637, …)
    entry_name: Optional[Union[str, List[str], None]] = Field(default=None)         # uniprot (P53_HUMAN, EGFR_HUMAN, …)
    protein_name: Optional[Union[str, List[str], None]] = Field(default=None)       # uniprot (e.g. "Cellular tumor antigen p53")
    # ── Descriptive Qdrant-backed fields (2026-05-12) ──────────────────────
    definition: Optional[Union[str, List[str], None]] = Field(default=None)              # chebi.chemical_master
    annotation: Optional[Union[str, List[str], None]] = Field(default=None)              # string.protein_master
    pharmacological_actions: Optional[Union[str, List[str], None]] = Field(default=None) # mesh (descriptor/disease/chemical master)
    hpo_term: Optional[Union[str, List[str], None]] = Field(default=None)                # orphanet.disease_phenotype_association
    modification: Optional[Union[str, List[str], None]] = Field(default=None)            # omnipath.enz_sub_table (PTM type)
    # OmniPath enzyme-substrate fields (referenced by SHARED enzyme_substrate relation).
    enzyme_genesymbol: Optional[Union[str, List[str], None]] = Field(default=None)       # omnipath.enz_sub_association
    substrate_genesymbol: Optional[Union[str, List[str], None]] = Field(default=None)    # omnipath.enz_sub_association
    # ── STRING PPI table-prefixed gene-symbol columns (2026-06-23) ─────────────
    # STRING denormalises gene symbols into each PPI table with a table prefix
    # (association_/physical_/channel_). The value-mapper picks these per table,
    # but without them as fields ParsedValue (extra="ignore") SILENTLY DROPS them
    # → "No filter terms produced after entity expansion" → web/AI fallback for
    # the core "what interacts with X?" query. Declare them so they survive.
    association_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)          # string.ppi_association
    association_partner_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)  # string.ppi_association
    physical_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)             # string.ppi_physical
    physical_partner_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)     # string.ppi_physical
    channel_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)              # string.ppi_detailed_channels
    channel_partner_gene_symbol: Optional[Union[str, List[str], None]] = Field(default=None)      # string.ppi_detailed_channels
    partner_protein_size: Optional[Union[str, List[str], None]] = Field(default=None)             # string PPI (denormalised partner aa-length)
    # Hierarchy child column — ONLY Reactome's pathway_hierarchy_reactome
    # carries a literal `child_id` column. DOID and MONDO hierarchies store
    # one (disease_id, parent_id) row per edge — children are derived by
    # self-joining on parent_id, NOT by reading a child_id column.
    child_id: Optional[Union[str, List[str], None]] = Field(default=None)                # reactome.pathway_hierarchy_reactome only
    interaction_types: Optional[Union[str, List[str], None]] = Field(default=None)       # dgidb.drug_gene_association
    experiment_type: Optional[Union[str, List[str], None]] = Field(default=None)         # biogrid.ppi_association
    clinical_significance: Optional[Union[str, List[str], None]] = Field(default=None)   # clinvar.variant_master, civic
    review_status: Optional[Union[str, List[str], None]] = Field(default=None)           # clinvar.variant_master
    phenotype_category: Optional[Union[str, List[str], None]] = Field(default=None)      # pharmgkb
    class_name: Optional[Union[str, List[str], None]] = Field(default=None)              # drugcentral.drug_pharma_class
    # ── PubTator3 entity-ID fields (2026-05-12) ────────────────────────────
    # PubTator master tables use ID-only schemas (MeSH for chemicals/diseases,
    # tmVar/rsID for mutations, NCBI taxon for species, Cellosaurus for cell
    # lines). The interpreter typically emits *_name fields; these IDs are
    # used when other DBs (MeSH/ChEBI/MONDO/HGNC) forward resolved IDs into
    # PubTator for the citation lookup.
    chemical_id: Optional[Union[str, List[str], None]] = Field(default=None)        # pubtator, mesh, chebi
    disease_id: Optional[Union[str, List[str], None]] = Field(default=None)         # pubtator, mondo, doid, clinvar, ...
    mutation_id: Optional[Union[str, List[str], None]] = Field(default=None)        # pubtator.mutation_master (rsID / tmVar)
    # ClinVar carries rsIDs in a SEPARATE column from `variant_id` (which on
    # ClinVar holds VCV/RCV accessions). Without this field the SHARED prompt
    # rule "rsNNN on ClinVar → rsid" would be silently dropped by Rule 5.
    rsid: Optional[Union[str, List[str], None]] = Field(default=None)               # clinvar.variant_citation_clinvar
    species_id: Optional[Union[str, List[str], None]] = Field(default=None)         # pubtator.species_master (NCBI taxonomy)
    cellline_id: Optional[Union[str, List[str], None]] = Field(default=None)        # pubtator.cellline_master (Cellosaurus CVCL)
    gene_id: Optional[Union[str, List[str], None]] = Field(default=None)            # pubtator.gene_master (NCBI Entrez), most DBs
    pmid: Optional[Union[str, List[str], None]] = Field(default=None)               # pubtator association tables, ctd, clinvar citations
    mentions: Optional[Union[str, List[str], None]] = Field(default=None)           # pubtator entity tables (free-text surface forms)
    relation_type: Optional[Union[str, List[str], None]] = Field(default=None)      # pubtator.relation (associate / treat / cause / …)
    entity1_type: Optional[Union[str, List[str], None]] = Field(default=None)       # pubtator.relation
    entity1_id: Optional[Union[str, List[str], None]] = Field(default=None)         # pubtator.relation
    entity2_type: Optional[Union[str, List[str], None]] = Field(default=None)       # pubtator.relation
    entity2_id: Optional[Union[str, List[str], None]] = Field(default=None)         # pubtator.relation
    # ── Phase 2 coverage expansion (2026-05-12) ────────────
    # Reactome
    ensembl_id: Optional[Union[str, List[str], None]] = Field(default=None)             # reactome.ensembl_pathway, hpo, hgnc.gene_identifier
    ncbi_gene_id: Optional[Union[str, List[str], None]] = Field(default=None)           # reactome.ncbi_pathway, hpo
    entrez_id: Optional[Union[str, List[str], None]] = Field(default=None)              # hgnc.gene_identifier
    refseq_id: Optional[Union[str, List[str], None]] = Field(default=None)              # hgnc.gene_identifier
    uniprot_id: Optional[Union[str, List[str], None]] = Field(default=None)             # hgnc.gene_identifier, hcdt, pharmgkb
    # ── HCDT logical xref column names (renamed at load time from parquet names) ──
    # HCDT's exec/pipeline layer renames the raw parquet columns to short logical
    # names (uniprot, hgnc, ensembl, entrez).  Because CommonFields uses
    # extra="ignore" these would be silently dropped without explicit declarations.
    # Do NOT remove the *_id counterparts above — other DBs still use those names.
    uniprot: Optional[Union[str, List[str], None]] = Field(default=None)                # hcdt xref (logical rename of uniprot_id column)
    hgnc: Optional[Union[str, List[str], None]] = Field(default=None)                   # hcdt xref (logical rename of hgnc_id column)
    ensembl: Optional[Union[str, List[str], None]] = Field(default=None)                # hcdt xref (logical rename of ensembl_id column)
    entrez: Optional[Union[str, List[str], None]] = Field(default=None)                 # hcdt xref (logical rename of entrez_id column)
    chebi_id: Optional[Union[str, List[str], None]] = Field(default=None)               # reactome.chebi_pathway, chebi
    evidence: Optional[Union[str, List[str], None]] = Field(default=None)               # reactome.gene_pathway_association
    # CIViC
    assertion_id: Optional[Union[str, List[str], None]] = Field(default=None)           # civic.assertion_master
    molecular_profile_id: Optional[Union[str, List[str], None]] = Field(default=None)   # civic.molecular_profile_master
    feature_id: Optional[Union[str, List[str], None]] = Field(default=None)             # civic.feature_master
    variant_group_id: Optional[Union[str, List[str], None]] = Field(default=None)       # civic.variant_group_master
    acmg_codes: Optional[Union[str, List[str], None]] = Field(default=None)             # civic.assertion_master
    amp_category: Optional[Union[str, List[str], None]] = Field(default=None)           # civic.assertion_master
    clingen_codes: Optional[Union[str, List[str], None]] = Field(default=None)          # civic.assertion_master
    regulatory_approval: Optional[Union[str, List[str], None]] = Field(default=None)    # civic.assertion_master
    fda_companion_test: Optional[Union[str, List[str], None]] = Field(default=None)     # civic.assertion_master
    feature_type: Optional[Union[str, List[str], None]] = Field(default=None)           # civic.feature_master (Gene/Fusion/Factor)
    # PharmGKB (snake_case alias for raw upstream columns)
    level_of_evidence: Optional[Union[str, List[str], None]] = Field(default=None)      # pharmgkb.clinical_annotation (1A/1B/2A/2B/3/4)
    specialty_population: Optional[Union[str, List[str], None]] = Field(default=None)   # pharmgkb.clinical_annotation
    source: Optional[Union[str, List[str], None]] = Field(default=None)                 # pharmgkb.drug_label, hpo, omnipath
    testing_level: Optional[Union[str, List[str], None]] = Field(default=None)          # pharmgkb.drug_label
    allele_function: Optional[Union[str, List[str], None]] = Field(default=None)        # pharmgkb.clinical_ann_alleles
    evidence_type: Optional[Union[str, List[str], None]] = Field(default=None)          # pharmgkb.clinical_ann_evidence, civic
    biomarker_flag: Optional[Union[str, List[str], None]] = Field(default=None)         # pharmgkb.drug_label
    # MeSH
    scr_id: Optional[Union[str, List[str], None]] = Field(default=None)                 # mesh.scr_master (Cxxxxxx)
    pharmacological_action_id: Optional[Union[str, List[str], None]] = Field(default=None)  # mesh.pharmacological_action
    pharmacological_action_name: Optional[Union[str, List[str], None]] = Field(default=None) # mesh.pharmacological_action
    substance_id: Optional[Union[str, List[str], None]] = Field(default=None)           # mesh.pharmacological_action
    substance_name: Optional[Union[str, List[str], None]] = Field(default=None)         # mesh.pharmacological_action
    subheading: Optional[Union[str, List[str], None]] = Field(default=None)             # mesh.qualifier_master
    abbreviation: Optional[Union[str, List[str], None]] = Field(default=None)           # mesh.qualifier_master
    mesh_id: Optional[Union[str, List[str], None]] = Field(default=None)                # mesh.descriptor/disease/chemical master (Dxxxxxx)
    semantic_types: Optional[Union[str, List[str], None]] = Field(default=None)         # mesh.descriptor_master
    tree_numbers: Optional[Union[str, List[str], None]] = Field(default=None)           # mesh.descriptor_master / tree_hierarchy
    top_tree_branch: Optional[Union[str, List[str], None]] = Field(default=None)        # mesh.descriptor_master
    parent_tree_number: Optional[Union[str, List[str], None]] = Field(default=None)     # mesh.tree_hierarchy
    scr_name: Optional[Union[str, List[str], None]] = Field(default=None)               # mesh.scr_master
    # UniProt (extended)
    reviewed: Optional[Union[str, List[str], None]] = Field(default=None)               # uniprot.protein_master (true=Swiss-Prot, false=TrEMBL)
    keyword_id: Optional[Union[str, List[str], None]] = Field(default=None)             # uniprot.keyword_master
    keyword_name: Optional[Union[str, List[str], None]] = Field(default=None)           # uniprot.keyword_master
    subcell_id: Optional[Union[str, List[str], None]] = Field(default=None)             # uniprot.subcell_location
    subcell_name: Optional[Union[str, List[str], None]] = Field(default=None)           # uniprot.subcell_location
    go_id: Optional[Union[str, List[str], None]] = Field(default=None)                  # uniprot.gene_ontology
    qualifier: Optional[Union[str, List[str], None]] = Field(default=None)              # uniprot.gene_ontology
    # BioGRID
    ptm_type: Optional[Union[str, List[str], None]] = Field(default=None)               # biogrid.ptm_association
    residue: Optional[Union[str, List[str], None]] = Field(default=None)                # biogrid.ptm_association
    action: Optional[Union[str, List[str], None]] = Field(default=None)                 # biogrid.chemical_interaction
    interaction_type: Optional[Union[str, List[str], None]] = Field(default=None)       # biogrid.chemical_interaction
    identifier_type: Optional[Union[str, List[str], None]] = Field(default=None)        # biogrid.identifier_crosswalk
    chemical_type: Optional[Union[str, List[str], None]] = Field(default=None)          # biogrid.chemical_interaction
    chemical_source: Optional[Union[str, List[str], None]] = Field(default=None)        # biogrid.chemical_interaction
    # ChEBI
    xref_type: Optional[Union[str, List[str], None]] = Field(default=None)              # chebi.chemical_xref (CAS/KEGG/DrugBank/PubChem/...)
    relation_type_id: Optional[Union[str, List[str], None]] = Field(default=None)       # chebi.chemical_relation
    synonym_type: Optional[Union[str, List[str], None]] = Field(default=None)           # chebi.chemical_synonyms (IUPAC NAME / SYNONYM / BRAND NAME / ...)
    # MSigDB
    sub_collection: Optional[Union[str, List[str], None]] = Field(default=None)         # msigdb.geneset_metadata (CGP / CP:KEGG / GO:BP / ...)
    # OmniPath
    category: Optional[Union[str, List[str], None]] = Field(default=None)               # omnipath.intercell (ligand/receptor/...)
    entity_type: Optional[Union[str, List[str], None]] = Field(default=None)            # omnipath.annotations (protein/complex/mirna)
    # HPO
    parent_id: Optional[Union[str, List[str], None]] = Field(default=None)              # hpo/doid/mondo hierarchies
    is_a_parents: Optional[Union[str, List[str], None]] = Field(default=None)           # hpo.phenotype_ontology_master
    alt_ids: Optional[Union[str, List[str], None]] = Field(default=None)                # hpo.phenotype_ontology_master
    xrefs: Optional[Union[str, List[str], None]] = Field(default=None)                  # hpo.phenotype_ontology_master
    association_type: Optional[Union[str, List[str], None]] = Field(default=None)       # hpo.gene_disease_association (MENDELIAN/POLYGENIC/...)
    # DGIdb
    gene_concept_id: Optional[Union[str, List[str], None]] = Field(default=None)        # dgidb.gene_category (hgnc:XXXXX)
    gene_long_name: Optional[Union[str, List[str], None]] = Field(default=None)         # dgidb.gene_category
    category_name: Optional[Union[str, List[str], None]] = Field(default=None)          # dgidb.gene_category (DRUGGABLE GENOME/KINASE/...)
    source_names: Optional[Union[str, List[str], None]] = Field(default=None)           # dgidb.gene_category
    # DOID / MONDO
    subset: Optional[Union[str, List[str], None]] = Field(default=None)                 # doid/mondo subset slim tags
    relation: Optional[Union[str, List[str], None]] = Field(default=None)               # mondo.disease_hierarchy (is_a/part_of)
    # ── Phase 3 coverage expansion (2026-05-12) ────────────────────────────
    # PharmGKB variant + guideline annotation fields
    significance: Optional[Union[str, List[str], None]] = Field(default=None)            # pharmgkb.variant_*_annotation
    phenotype: Optional[Union[str, List[str], None]] = Field(default=None)               # pharmgkb.variant_phenotype_annotation
    assay_type: Optional[Union[str, List[str], None]] = Field(default=None)              # pharmgkb.variant_fa_annotation
    functional_terms: Optional[Union[str, List[str], None]] = Field(default=None)        # pharmgkb.variant_fa_annotation
    cell_type: Optional[Union[str, List[str], None]] = Field(default=None)               # pharmgkb.variant_fa_annotation
    recommendation: Optional[Union[str, List[str], None]] = Field(default=None)          # pharmgkb.guideline_annotation
    cancer_genome: Optional[Union[str, List[str], None]] = Field(default=None)           # pharmgkb.guideline_annotation, drug_label
    # HGNC withdrawn-gene fields
    withdrawn_symbol: Optional[Union[str, List[str], None]] = Field(default=None)        # hgnc.withdrawn_gene
    status: Optional[Union[str, List[str], None]] = Field(default=None)                  # hgnc.withdrawn_gene (Entry/Symbol Withdrawn)


    class Config:
        extra = "ignore"





class ParsedValue(CommonFields):
    """Fields extracted from user query after NER/LLM parsing."""
    class Config:
        extra = "ignore"
ParsedValue.model_rebuild()


class QueryInterpreterOutputGuardrail(BaseModel):
    """LLM-powered query interpreter output."""
    # cleaned_query is the LLM-rewritten user query that downstream services
    # use as a search/embedding input. Bounded to MAX_QUERY_LENGTH so an LLM
    # hallucinating a long restatement (or a prompt-injection attempt) cannot
    # propagate to PubMed/embedding calls.
    cleaned_query: Optional[constr(max_length=MAX_QUERY_LENGTH)] = Field(
        default=None, example="What is the drug for fever?"
    )
    status: Optional[str] = Field(default=None, example="valid")
    route: Optional[str] = Field(default=None, example="biochirp")
    message: Optional[str] = Field(default=None, example="Your question is clear. BioChirp will answer using its workflow.")
    relevant_databases: Optional[List[Literal[
        "TTD", "CTD", "HCDT", "ClinVar",
        "HPO", "Orphanet",
        "Reactome", "STRING", "UniProt",
        "OpenTargets",
        "Web",
    ]]] = Field(default=None, example=["TTD"])
    dropped_constraints: Optional[List[str]] = Field(default=None, example=["SMILES string"])
    parsed_value: ParsedValue = Field(default_factory=ParsedValue)
    tool: Optional[str] = Field(default=None, example="interpreter")
    skip_summary: bool = Field(default=False, description="If True, DB services skip the LLM summarizer and return raw row count only.")

    class Config:
        extra = "ignore"  # was "forbid" — accept flat tool-call JSON from LLMs

    @model_validator(mode='before')
    @classmethod
    def coerce_flat_input(cls, data):
        """Lift flat CommonFields from an LLM tool-call JSON into parsed_value.

        Qwen3/Ollama sometimes generates a flat dict instead of nesting fields
        under parsed_value. This validator detects that and wraps them correctly.
        """
        if not isinstance(data, dict):
            return data
        if 'parsed_value' not in data or data.get('parsed_value') is None:
            common_field_names = set(CommonFields.model_fields.keys())
            flat_vals = {k: v for k, v in data.items() if k in common_field_names and v is not None}
            if flat_vals:
                data = {**data, 'parsed_value': flat_vals}
        return data



class OutputFields(CommonFields):
    """Filtered/matched fields after fuzzy/semantic DB matching."""
    class Config:
        extra = Extra.forbid
OutputFields.model_rebuild()


class FuzzyFilteredOutputs(BaseModel):
    database: str = Field(..., example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="fuzzy")

FuzzyFilteredOutputs.model_rebuild()



class SimilarityFilteredOutputs(BaseModel):
    database: str = Field(..., example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="fuzzy")

SimilarityFilteredOutputs.model_rebuild()




class PlanGenerator(BaseModel):
    """Dict of OutputFields for each database."""
    database: str = Field(default=None, example="ttd")
    plan: Any
    tool: str = Field(default=None, example="planner")

PlanGenerator.model_rebuild()


class DatabaseTable(BaseModel):
    database: str
    table: Optional[List[dict]] = None
    csv_path: Optional[str] = None
    row_count: Optional[int] = None
    tool: str
    message: Optional[str] = None
    db_version: Optional[str] = None   # snapshot version from SOURCE.md
    db_snapshot_date: Optional[str] = None  # ISO date of data snapshot
    # Per-table filter trace surfaced from join_and_filter_database so the
    # chat UI can show "<table> N→M rows" for each filter step. Each element:
    # {column, input_values, rows_before, rows_after}.
    filter_trace: Optional[List[dict]] = None
    # Full entity mapping from expand_and_match_db: {"drug_name": "requested",
    # "disease_name": ["tuberculosis", ...]}. Used by HCDT chat to show the
    # "Interpreted as:" interpretation section in the tool card.
    filter_val: Optional[dict] = None



class ExpandSynonymsOutput(BaseModel):
    database: Optional[str] = Field(None, example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="expand_synonyms")

ExpandSynonymsOutput.model_rebuild()




class ExpandMemberOutput(BaseModel):
    database: str = Field(None, example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="expand_and_match_db")
    message: Optional[str] = None
    errors: Optional[dict] = None

ExpandMemberOutput.model_rebuild()


class Llm_Member_Selector_Input(BaseModel):
    # value: Optional[OutputFields] = None
    # value: List[Any] = Field(default_factory=list)
    category: str= Field(..., example="disease_name")

    single_term: str = Field(..., example="fever")
    string_list : List[str] = Field(..., example=["fever","cancer"])


Llm_Member_Selector_Input.model_rebuild()



