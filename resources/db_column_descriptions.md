# Per-Column Schema Descriptions (for embedding-based concept→column matching)

**This file is the source of truth** for per-column natural-language descriptions. [app/utils/column_embeddings.py](../app/utils/column_embeddings.py) parses it at startup, embeds each row with the local `BGE-small-en-v1.5` model, and exposes a `lookup_column(query, db=None) → list[(db, table, column, score)]` function for the planner.

## When this is used

The planner already uses **exact column-name matching** ([app/tools/planner/app/graph.py](../app/tools/planner/app/graph.py) `_map_concepts_to_unique_tables`). That works when the interpreter emits a canonical field whose name matches a schema column. When it doesn't (novel queries, ambiguous concepts, ontology-specific columns), this embedding lookup becomes the fallback: the planner asks "which column in <db> is semantically closest to this concept?" and ranks columns by cosine similarity to the concept's description.

## Format (machine-parsed)

Same parser as `db_field_aliases.md`: `## <db_slug>` headings, markdown tables.

```
## <db_slug>

| Table                    | Column          | Description                                                       |
|--------------------------|-----------------|-------------------------------------------------------------------|
| drug_master_table        | drug_name       | Generic / preferred drug name (lowercase, e.g. "imatinib").       |
| drug_master_table        | cas_number      | CAS registry number for the compound, e.g. "152459-95-5".         |
| ...                      | ...             | ...                                                               |
```

Rules:
- `## db_slug` (lowercase) per DB section.
- 3 columns: Table, Column, Description. Position-based (header text doesn't matter).
- Description should be a short prose sentence (~10–30 words). Mention the data type and a representative example value where it helps disambiguation.
- The (db, table, column) tuple is the unique key — the embedding text is the description string.

## How to populate

1. **Seed from schema**: for each DB in [config/schema.py](../config/schema.py), iterate tables and columns.
2. **Write the description**: focus on what the column *means* and how the user would describe it. Avoid jargon that only appears in the column name itself.
3. **Skip pure infrastructure columns** that nobody would query by name: foreign-key IDs that just join (`_id` columns appearing only on association tables for joining; the canonical entity-name columns ARE worth describing).

## Embedding pipeline

At startup of any process that imports `column_embeddings`:

1. Parse this markdown into `(db, table, column, description)` rows.
2. For each row, call `fastembed.TextEmbedding("BAAI/bge-small-en-v1.5").embed([description])`.
3. Stack into a numpy matrix of shape `(N, 384)`.
4. Cache in memory; expose `lookup_column(query: str, db: str | None, k: int = 5)` that returns top-k by cosine similarity.

No Qdrant collection — descriptions plus 384-dim vectors for ~500 columns is ~1.5 MB in memory, far cheaper than an extra Qdrant collection.

---

# Per-DB column tables
## ctd

| Table                              | Column         | Description |
|------------------------------------|----------------|-------------|
| chemical_master_table              | drug_id        | CTD chemical identifier (MeSH-derived, e.g. "D000305" for adenosine). |
| chemical_master_table              | drug_name      | Chemical or drug name in lowercase as indexed by CTD (e.g. "arsenic", "bisphenol a", "acetaminophen"). Users may ask for "drug", "chemical", "compound", "toxicant", or just type the substance name. |
| chemical_master_table              | cas_rn         | CAS Registry Number for the chemical (e.g. "50-78-2"). Users may ask for "CAS", "CAS number", "CAS RN", or "registry number". |
| chemical_master_table              | pubchem_cid    | PubChem Compound ID (integer). Users may ask for "PubChem ID" or "CID". |
| chemical_master_table              | inchikey       | InChIKey structural hash for the chemical (e.g. "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"). Users may ask for "InChIKey" or "structure key". |
| chemical_master_table              | definition     | Free-text MeSH definition / description of the chemical. Users may ask for "definition", "description", or "what is it". |
| chemical_master_table              | synonyms       | Pipe-delimited alternate names, brand names, and trivial synonyms for the chemical. Users may ask for "synonym", "alias", "brand name", or "alternate name". |
| gene_master_table                  | gene_id        | NCBI Entrez gene ID used internally by CTD. |
| gene_master_table                  | gene_symbol    | HGNC short gene symbol (e.g. "TP53", "EGFR"). Users may ask for "gene", "gene symbol", or just type the symbol. |
| disease_master_table               | disease_id     | CTD disease identifier (MeSH-derived, e.g. "D006973" for hypertension). |
| disease_master_table               | disease_name   | Disease name as curated by CTD (MeSH-derived, e.g. "Hypertension", "Liver Cirrhosis"). Users may ask for "disease", "indication", or "condition". |
| pathway_master_table               | pathway_id     | Pathway identifier (KEGG hsa or Reactome stable ID). |
| pathway_master_table               | pathway_name   | Biological pathway name from upstream sources Reactome / KEGG (e.g. "Apoptosis"). Users may ask for "pathway" or "signaling pathway". |
| chemical_gene_association          | interaction_text    | Free-text description of the chemical-gene interaction (e.g. "Aspirin results in decreased expression of TP53 mRNA"). Users may ask for "interaction description" or "what does it do". |
| chemical_gene_association          | chemical_gene_interaction_actions | Controlled-vocab action terms describing how a chemical affects a gene/protein (e.g. "increases^expression", "decreases^activity", "affects^methylation"). Users may ask for "action", "effect", or "interaction action". |
| chemical_gene_association          | organism_id    | NCBI taxon ID for the organism the interaction was observed in. |
| chemical_disease_association       | chem_disease_direct_evidence | Direct-evidence flag — one of "marker/mechanism" (biomarker or mechanism), "therapeutic" (used to treat). Users may ask for "direct evidence", "evidence type", or "therapeutic vs marker". |
| chemical_disease_association       | inference_gene  | Gene used to infer the chemical-disease association (when not direct). |
| chemical_disease_association       | inference_score | Numeric score for inferred chemical-disease links (higher = stronger inference). |
| chemical_disease_association       | omim_ids        | Pipe-delimited OMIM disease identifiers. Users may ask for "OMIM ID" or "OMIM". |
| chemical_pathway_enriched          | p_value           | Enrichment p-value of chemical-pathway association (lower = more significant). Users may ask for "p-value" or "significance". |
| chemical_pathway_enriched          | corrected_p_value | Multiple-testing-corrected (FDR / Bonferroni) p-value. Users may ask for "FDR", "corrected p-value", "adjusted p-value", or "q-value". |
| chemical_pathway_enriched          | target_match      | Number of genes overlapping with the pathway (foreground count). |
| chemical_pathway_enriched          | target_total      | Total number of pathway genes (background count). |
| chemical_phenotype_ixn             | phenotype_id   | GO term ID representing the phenotype/process affected. |
| chemical_phenotype_ixn             | anatomy_terms  | Pipe-delimited anatomy/tissue terms where the phenotype was observed. |
| chemical_phenotype_ixn             | inference_genes | Genes used to infer the chemical-phenotype link. |
| exposure_events_ctd                | stressor_name  | Environmental exposure stressor (e.g. "lead", "PM2.5", "benzene"). Users may ask for "exposure", "stressor", "pollutant", or "environmental agent". |
| exposure_events_ctd                | medium | Exposure route or medium (e.g. "air", "water", "soil", "diet"). Users may ask for "exposure route", "medium", or "how exposed". |
| exposure_events_ctd                | sex            | Sex of the cohort ("male", "female", "both"). |
| exposure_events_ctd                | age            | Age value or range for the cohort. |
| exposure_events_ctd                | age_units      | Units for the age value (e.g. "years", "months"). |
| exposure_events_ctd                | race           | Race / ethnicity of the cohort. |
| exposure_events_ctd                | smoking_status | Smoking status of the cohort. |
| exposure_events_ctd                | study_countries | Geographic country/region where the exposure study was conducted. Users may ask for "country", "geography", or "region". |
| exposure_events_ctd                | anatomy        | Anatomical site of effect. |
| exposure_events_ctd                | outcome_relationship | Outcome relationship qualifier (e.g. "positive", "inverse", "no correlation"). |
| anatomy_master_ctd                 | anatomy_id     | CTD anatomy identifier (UBERON / MeSH derived). |
| anatomy_master_ctd                 | anatomy_name   | Anatomical structure name (e.g. "liver", "brain"). Users may ask for "anatomy", "tissue", or "organ". |
## hcdt

| Table                     | Column            | Description |
|---------------------------|-------------------|-------------|
| drug_master_table         | drug_id           | HCDT drug identifier. |
| drug_master_table         | drug_name         | Drug preferred name in lowercase (e.g. "imatinib", "gefitinib"). Users may ask for "drug", "compound", "medication", or just type the drug name. |
| drug_master_table         | drug_synonyms     | Pipe-delimited alternate / brand / trade names for the drug. Users may ask for "synonym", "alias", "brand name", "trade name", or "alternate name". |
| drug_master_table         | drug_mw           | Molecular weight in Daltons (e.g. 493.6). Users may ask for "MW", "molecular weight", "molecular mass", or just "mass". |
| drug_master_table         | drug_formula      | Molecular formula of the drug (e.g. "C29H31N7O"). Users may ask for "molecular formula", "chemical formula", "empirical formula", or just "formula". |
| drug_master_table         | drug_inchi        | Full InChI structural string. Users may ask for "InChI". |
| drug_master_table         | drug_inchikey     | InChIKey structural hash. Users may ask for "InChIKey" or "structure key". |
| drug_master_table         | drug_smiles_iso   | Isomeric SMILES preserving stereochemistry. Users may ask for "SMILES", "isomeric SMILES", or "structure". |
| drug_master_table         | drug_smiles_canon | Canonical SMILES (stereochemistry-stripped). Users may ask for "canonical SMILES" or "SMILES". |
| drug_master_table         | drug_iupac        | IUPAC systematic chemical name. Users may ask for "IUPAC name" or "systematic name". |
| gene_master_table         | gene_id           | HCDT internal gene identifier. |
| gene_master_table         | gene_symbol       | HGNC short gene symbol (e.g. "EGFR", "BRAF"). Users may ask for "gene", "gene symbol", or "HGNC symbol". |
| gene_master_table         | gene_name         | Full descriptive gene name (e.g. "epidermal growth factor receptor"). Users may ask for "gene name", "full gene name", or "long gene name". |
| disease_master_table      | disease_id        | HCDT disease identifier. |
| disease_master_table      | disease_name      | Disease label as curated by HCDT (e.g. "Non-small cell lung cancer"). Users may ask for "disease", "indication", or "condition". |
| disease_master_table      | icd11             | ICD-11 disease classification code. Users may ask for "ICD-11" or "ICD code". |
| pathway_master_table      | pathway_id        | HCDT pathway identifier. |
| pathway_master_table      | pathway_name      | Pathway name from upstream sources (e.g. KEGG, Reactome, SMPDB). Users may ask for "pathway" or "signaling pathway". |
| pathway_master_table      | datasource        | Upstream source database (e.g. "KEGG", "Reactome", "SMPDB"). |
| drug_gene_association     | datasource        | Upstream contributor for this drug-gene assertion. Users may ask for "source", "data source", or "provenance". |
| drug_gene_association     | uniprot_id        | UniProt accession of the target protein (e.g. "P00533"). Users may ask for "UniProt ID" or "UniProt accession". |
| drug_gene_association     | hgnc_id           | HGNC identifier of the target gene (e.g. "HGNC:3236"). Users may ask for "HGNC ID". |
| drug_gene_association     | ensembl_id        | Ensembl gene identifier (e.g. "ENSG00000146648"). Users may ask for "Ensembl ID" or "Ensembl gene ID". |
| drug_gene_association     | entrez_id         | NCBI Entrez gene ID (e.g. "1956"). Users may ask for "Entrez ID" or "NCBI gene ID". |
| drug_disease_association  | mesh_id           | MeSH descriptor code for the indication (e.g. "D006973"). Users may ask for "MeSH ID". |
| drug_disease_association  | omim_id           | OMIM disease identifier (e.g. "211980"). Users may ask for "OMIM ID" or "OMIM". |
| drug_disease_association  | icd11             | ICD-11 disease code. Users may ask for "ICD-11" or "ICD code". |
| drug_pathway_association  | reactome_id       | Reactome pathway stable ID for the drug's pathway (e.g. "R-HSA-1234"). Users may ask for "Reactome ID". |
| drug_pathway_association  | kegg_hsa_id       | KEGG human pathway identifier (e.g. "hsa04010"). Users may ask for "KEGG ID" or "KEGG pathway". |
| drug_pathway_association  | smpdb_id          | Small Molecule Pathway Database identifier. Users may ask for "SMPDB ID". |
| rna_master_table          | rna_name          | RNA molecule name (e.g. miRNA / lncRNA symbol like "MIR21" or "HOTAIR"). Users may ask for "RNA", "miRNA", or "lncRNA". |
| rna_master_table          | rna_type          | RNA type (e.g. "miRNA", "lncRNA", "snoRNA"). Users may ask for "RNA type". |
| drug_target_negative      | ki_nM             | Inhibition constant Ki in nanomolar — drug-target binding affinity. Users may ask for "Ki", "binding affinity", or "potency". Lower = more potent. |
| drug_target_negative      | ic50_nM           | Half-maximal inhibitory concentration IC50 in nanomolar. Users may ask for "IC50" or "potency". Lower = more potent. |
| drug_target_negative      | kd_nM             | Dissociation constant Kd in nanomolar. Users may ask for "Kd" or "binding constant". Lower = stronger binding. |
| pathway_xref              | xref_source       | External pathway database name (e.g. "Reactome", "KEGG", "SMPDB"). Users may ask for "cross-reference database" or "xref source". |
| pathway_xref              | xref_id           | External pathway identifier in xref_source. Users may ask for "cross-reference ID" or "xref ID". |

## clinvar

| Table                              | Column                  | Description |
|------------------------------------|-------------------------|-------------|
| variant_master_table               | variant_id              | ClinVar variation identifier (VCV or RCV format, e.g. "VCV000012345"). Users may ask for "variant ID", "ClinVar ID", "VCV", or "RCV". |
| variant_master_table               | variant_name            | HGVS short form for the variant (e.g. "NM_007294.4:c.5266dupC"). Users may ask for "variant", "HGVS", or "mutation". |
| variant_master_table               | gene_symbol             | HGNC symbol of the gene harboring the variant (e.g. "BRCA1"). Users may ask for "gene" or "gene symbol". |
| variant_master_table               | variant_type            | Variant type (e.g. "single nucleotide variant", "deletion", "duplication", "indel"). Users may ask for "variant type" or "mutation type". |
| variant_master_table               | clinical_significance   | Aggregated clinical significance ("Pathogenic", "Likely pathogenic", "Benign", "Likely benign", "Uncertain significance", "Conflicting interpretations of pathogenicity"). Users may ask for "clinical significance", "pathogenicity", "pathogenic vs benign", or "ACMG classification". |
| variant_master_table               | review_status           | ClinVar review status (e.g. "criteria provided, single submitter", "reviewed by expert panel", "practice guideline"). Users may ask for "review status" or "evidence stars". |
| gene_master_table                  | gene_id                 | NCBI Entrez gene ID. |
| gene_master_table                  | gene_symbol             | HGNC short gene symbol. |
| variant_disease_association        | disease_id              | Disease identifier linked to the variant (e.g. MedGen CUI). |
| variant_disease_association        | disease_name            | Disease name linked to the variant (e.g. "Hereditary breast and ovarian cancer syndrome"). Users may ask for "disease" or "condition". |
| variant_disease_association        | clinical_significance   | Clinical significance specific to this variant-disease pair. |
| variant_submission_clinvar         | clinical_significance   | Submitter-level clinical significance call (may differ from aggregate). Users may ask for "submitter classification". |
| variant_submission_clinvar         | date_last_evaluated     | Date the submission was last evaluated (YYYY-MM-DD). Users may ask for "date last evaluated" or "evaluation date". |
| variant_submission_clinvar         | submitted_phenotype     | Phenotype as submitted by the submitter. Users may ask for "submitted phenotype". |
| variant_submission_clinvar         | reported_phenotype      | Phenotype as reported in the source publication. |
| variant_submission_clinvar         | review_status           | Submission-level review status. |
| variant_submission_clinvar         | collection_method       | How the case was collected (e.g. "clinical testing", "research", "literature only", "curation"). Users may ask for "collection method" or "evidence source". |
| variant_submission_clinvar         | origin_counts           | Variant origin counts split by germline vs somatic. Users may ask for "germline vs somatic" or "origin counts". |
| variant_submission_clinvar         | submitter               | Submitting organization / laboratory name (e.g. "Invitae", "GeneDx"). Users may ask for "submitter" or "lab". |
| variant_submission_clinvar         | scv                     | SCV submission accession (e.g. "SCV000123456"). Users may ask for "SCV" or "submission accession". |
| variant_submission_clinvar         | somatic_clinical_impact | Somatic-tier clinical impact classification (e.g. "Tier I", "Tier II"). Users may ask for "somatic impact" or "somatic tier". |
| variant_submission_clinvar         | oncogenicity            | Oncogenicity classification ("Oncogenic", "Likely oncogenic", "Benign", "Likely benign", "Uncertain"). Users may ask for "oncogenicity" or "oncogenic". |
| variant_submission_clinvar         | explanation             | Free-text explanation supporting the classification. |
| variant_citation_clinvar           | rsid                    | dbSNP rsID for the variant (e.g. "rs113993960"). Users may ask for "rsID", "rs number", "dbSNP", or "SNP ID". |
| variant_citation_clinvar           | nsv                     | Structural-variant NSV accession. Users may ask for "NSV". |
| variant_citation_clinvar           | citation_source         | Source of the citation (e.g. "PubMed", "DOI", "OMIM"). Users may ask for "citation source". |
| variant_citation_clinvar           | citation_id             | Identifier within the citation source. |
| variant_genomic_coords_clinvar     | assembly                | Genome assembly / build (e.g. "GRCh38", "GRCh37"). Users may ask for "assembly", "genome build", "hg19", or "hg38". |
| variant_genomic_coords_clinvar     | chrom                   | Chromosome name (e.g. "1", "X", "MT"). Users may ask for "chromosome" or "chr". |
| variant_genomic_coords_clinvar     | pos                     | Genomic position (1-based integer). Users may ask for "position" or "coordinate". |
| variant_genomic_coords_clinvar     | ref                     | Reference allele nucleotide sequence. Users may ask for "reference allele" or "ref". |
| variant_genomic_coords_clinvar     | alt                     | Alternate allele nucleotide sequence. Users may ask for "alternate allele" or "alt". |
| variant_genomic_coords_clinvar     | molecular_consequence   | Molecular consequence term (e.g. "missense_variant", "stop_gained", "frameshift_variant", "synonymous_variant"). Users may ask for "consequence", "effect", or "molecular consequence". |
| variant_genomic_coords_clinvar     | variant_class           | Variant class label (e.g. "SNV", "deletion"). |
| variant_genomic_coords_clinvar     | clnsig                  | Raw VCF CLNSIG clinical significance field. |
| variant_genomic_coords_clinvar     | clndn                   | Raw VCF CLNDN disease name field. |

## hpo

| Table                                | Column            | Description |
|--------------------------------------|-------------------|-------------|
| gene_master_table                    | gene_id           | NCBI Entrez gene ID. |
| gene_master_table                    | gene_symbol       | HGNC short gene symbol (e.g. "FBN1", "DMD"). Users may ask for "gene" or "gene symbol". |
| phenotype_master_table               | phenotype_id      | HPO identifier in the format "HP:" + 7 digits (e.g. "HP:0001250" = Seizure). Users may ask for "HPO ID", "HPO term ID", or "phenotype ID". |
| phenotype_master_table               | phenotype_name    | HPO phenotype term name (e.g. "Seizure", "Intellectual disability", "Short stature"). Users may ask for "phenotype", "symptom", "HPO term", "clinical feature", or "sign". |
| disease_master_table                 | disease_id        | Disease identifier (OMIM or Orphanet, e.g. "OMIM:154700", "ORPHA:773"). Users may ask for "OMIM ID", "Orphanet ID", or "disease ID". |
| disease_master_table                 | disease_name      | Disease label (OMIM or Orphanet derived). Users may ask for "disease" or "condition". |
| gene_phenotype_association           | gene_symbol       | HGNC symbol of the gene associated with the phenotype. |
| gene_phenotype_association           | phenotype_name    | HPO phenotype term linked to the gene. |
| gene_phenotype_association           | frequency         | Frequency of the phenotype in disease cases (e.g. "HP:0040281" = Very frequent, or fraction like "12/15"). Users may ask for "frequency" or "how common". |
| phenotype_master_table_hpo        | definition        | Free-text definition of the HPO term. Users may ask for "definition" or "description". |
| phenotype_master_table_hpo        | is_a_parents      | Parent HPO terms in the is_a hierarchy. Users may ask for "parent phenotype", "broader term", or "parent term". |
| phenotype_master_table_hpo        | alt_ids           | Alternate HPO IDs that resolve to this term. Users may ask for "alternate ID" or "alt ID". |
| phenotype_master_table_hpo        | xrefs             | Cross-references to other ontologies (e.g. MeSH, SNOMED, UMLS). Users may ask for "cross-reference", "xref", or "mapping". |
| phenotype_synonym_hpo                | synonym           | Alternate term / synonym for the phenotype. Users may ask for "synonym", "alternate term", or "AKA". |
| gene_disease_association_hpo         | association_type  | Type of gene-disease association (e.g. "MENDELIAN", "POLYGENIC"). Users may ask for "association type". |
| gene_disease_association_hpo         | source            | Curating source / database (e.g. "OMIM", "Orphanet"). Users may ask for "source" or "data source". |
| disease_phenotype_annotation_hpo     | qualifier         | Annotation qualifier (e.g. "NOT" for negation). Users may ask for "qualifier" or "negation". |
| disease_phenotype_annotation_hpo     | evidence          | Evidence code supporting the annotation (e.g. "PCS", "IEA", "TAS"). Users may ask for "evidence code" or "evidence". |
| disease_phenotype_annotation_hpo     | onset             | Age-of-onset HPO term (e.g. "HP:0003577" = Congenital onset, "HP:0003581" = Adult onset). Users may ask for "onset", "age of onset", or "when does it appear". |
| disease_phenotype_annotation_hpo     | frequency         | Frequency term or fraction (e.g. "HP:0040281" = Very frequent). Users may ask for "frequency". |
| disease_phenotype_annotation_hpo     | sex               | Sex specificity of the annotation ("MALE", "FEMALE"). Users may ask for "sex" or "gender-specific". |
| disease_phenotype_annotation_hpo     | modifier          | Clinical modifier HPO term (e.g. severity, laterality). Users may ask for "modifier" or "clinical modifier". |
| disease_phenotype_annotation_hpo     | aspect            | Aspect of annotation ("P"=phenotypic abnormality, "I"=inheritance, "C"=clinical course, "M"=clinical modifier). Users may ask for "aspect" or "annotation type". |
| disease_phenotype_annotation_hpo     | biocuration       | Biocuration provenance string (curator + date). |

## orphanet

| Table                                | Column                  | Description |
|--------------------------------------|-------------------------|-------------|
| disease_master_table                 | disease_id              | Orphanet identifier in the format "ORPHA:" + integer (e.g. "ORPHA:773" = Neurofibromatosis type 1). Users may ask for "Orphanet ID", "ORPHA ID", or "ORPHA number". |
| disease_master_table                 | disease_name            | Orphanet rare-disease name (e.g. "Cystic fibrosis", "Huntington disease"). Users may ask for "disease", "rare disease", "disorder", or just type the name. |
| gene_master_table                    | gene_id                 | Orphanet internal gene ID. |
| gene_master_table                    | gene_name               | HGNC short gene symbol (e.g. "CFTR", "NF1"). Users may ask for "gene" or "gene symbol". |
| gene_master_table                    | ensembl_accession       | Ensembl gene ID (e.g. "ENSG00000001626"). Users may ask for "Ensembl ID". |
| gene_master_table                    | entrez                  | NCBI Entrez gene ID. Users may ask for "Entrez ID" or "NCBI gene ID". |
| gene_disease_association             | gene_name               | Gene symbol associated with the rare disease. |
| gene_disease_association             | association_type        | Orphanet association type (long descriptive text). Canonical values include "Disease-causing germline mutation(s) in", "Disease-causing germline mutation(s) (loss of function) in", "Disease-causing somatic mutation(s) in", "Major susceptibility factor in", "Modifying germline mutation in", "Part of a fusion gene in", "Role in the phenotype of", "Biomarker tested in", "Candidate gene tested in". Users may ask for "association type", "causal", "causative gene", or "disease-causing". |
| disease_phenotype_association        | phenotype_id            | HPO phenotype identifier (e.g. "HP:0001250"). Users may ask for "HPO ID". |
| disease_phenotype_association        | hpo_term                | HPO phenotype term name observed in the disease (e.g. "Seizure"). Users may ask for "phenotype", "symptom", or "clinical feature". |
| disease_phenotype_association        | frequency               | Frequency of the phenotype in the disease (e.g. "Frequent (79-30%)", "Very frequent (99-80%)"). Users may ask for "frequency" or "how common". |
| disease_xref_orphanet                | xref_source             | External database providing the cross-reference (e.g. "OMIM", "ICD-10", "ICD-11", "MeSH", "UMLS", "MedDRA"). Users may ask for "xref database" or "cross-reference source". |
| disease_xref_orphanet                | xref_id                 | External identifier in xref_source (e.g. "219700" in OMIM). Users may ask for "cross-reference ID" or "xref ID". |
| disease_xref_orphanet                | mapping_relation        | Mapping precision ("E"=exact, "NTBT"=narrower term broader term, "BTNT"=broader term narrower term, "ND"=not defined). Users may ask for "mapping relation" or "mapping precision". |
| disease_xref_orphanet                | validation_status       | Curator validation status (e.g. "Validated"). |
| disease_epidemiology_orphanet        | prevalence_type         | Prevalence type ("Point prevalence", "Birth prevalence", "Lifetime prevalence", "Incidence"). Users may ask for "prevalence type". |
| disease_epidemiology_orphanet        | prevalence_qualification | Qualifying description for the prevalence value. |
| disease_epidemiology_orphanet        | prevalence_class        | Prevalence class bucket (e.g. "1-9 / 100 000", "1-5 / 10 000", "<1 / 1 000 000"). Users may ask for "prevalence" or "how common". |
| disease_epidemiology_orphanet        | val_moy                 | Mean prevalence value (numeric). Users may ask for "prevalence mean" or "prevalence value". |
| disease_epidemiology_orphanet        | geographic              | Geographic area for the prevalence estimate (e.g. "Europe", "Worldwide"). Users may ask for "geography", "country", or "region". |
| disease_onset_inheritance_orphanet   | attribute               | Attribute kind ("onset" or "inheritance"). |
| disease_onset_inheritance_orphanet   | value                   | Attribute value (e.g. "Adult", "Childhood", "Autosomal recessive", "X-linked dominant"). Users may ask for "age of onset", "inheritance", or "inheritance pattern". |
| disease_classification_orphanet      | classification          | Classification taxonomy name. |
| disease_classification_orphanet      | disorder_type           | Orphanet disorder type (e.g. "Disease", "Malformation syndrome", "Clinical syndrome"). Users may ask for "disorder type". |
| disease_classification_orphanet      | parent_disease_name     | Parent disease name in the classification tree. Users may ask for "parent disease". |
| disease_natural_history_orphanet     | target_disease_name     | Related disease name (e.g. for sub-types or progression). |
| disease_natural_history_orphanet     | association_type        | Natural-history relation type. |

## reactome

| Table                          | Column              | Description |
|--------------------------------|---------------------|-------------|
| pathway_master_table           | pathway_id          | Reactome stable identifier in the format "R-HSA-" + integer for human (e.g. "R-HSA-109581" = Apoptosis). Users may ask for "Reactome ID", "pathway ID", or "stable ID". |
| pathway_master_table           | pathway_name        | Reactome pathway preferred name (e.g. "Apoptosis", "Signaling by EGFR", "Cell Cycle"). Users may ask for "pathway", "pathway name", "biological pathway", or just type the name. |
| gene_master_table              | gene_id             | NCBI gene ID for the Reactome gene record. |
| gene_master_table              | gene_symbol         | HGNC short gene symbol. Users may ask for "gene" or "gene symbol". |
| gene_pathway_association       | gene_pathway_evidence | Evidence code for the gene-pathway annotation (e.g. "IEA", "TAS"). Users may ask for "evidence" or "evidence code". |
| pathway_hierarchy_reactome     | parent_id           | Parent pathway stable ID (R-HSA-…) — opaque join key only; filter/display via parent_pathway_name. |
| pathway_hierarchy_reactome     | parent_pathway_name         | Human-readable name of the PARENT (super) pathway, denormalized from pathway_master. Filter on this for "sub-pathways / children of X"; return child_pathway_name. |
| pathway_hierarchy_reactome     | child_id            | Child pathway stable ID (R-HSA-…) — opaque join key only; filter/display via child_pathway_name. |
| pathway_hierarchy_reactome     | child_pathway_name          | Human-readable name of the CHILD (sub) pathway, denormalized from pathway_master. Filter on this for "parent / super-pathway of X"; return parent_pathway_name. |
| uniprot_pathway_reactome       | uniprot_accession   | UniProt accession participating in the pathway (e.g. "P04637"). Users may ask for "UniProt ID", "UniProt accession", or "protein ID". |
| uniprot_pathway_reactome       | uniprot_pathway_evidence | Evidence code for the UniProt-pathway annotation (e.g. "IEA", "TAS"). Users may ask for "evidence" or "evidence code". |
| chebi_pathway_reactome         | chebi_id            | ChEBI chemical participating in the pathway (e.g. "CHEBI:15377"). Users may ask for "ChEBI ID" or "chemical entity". |
| chebi_pathway_reactome         | chebi_pathway_evidence | Evidence code for the ChEBI-pathway annotation (e.g. "IEA", "TAS"). Users may ask for "evidence" or "evidence code". |
| ensembl_pathway_reactome       | ensembl_id          | Ensembl identifier participating in the pathway. Mixes ENSG (gene, ~16%), ENST (transcript, ~42%), and ENSP (protein, ~42%) IDs — do NOT assume gene-only semantics. Users may ask for "Ensembl ID", "Ensembl gene", "Ensembl transcript", or "Ensembl protein". |
| ensembl_pathway_reactome       | ensembl_pathway_evidence | Evidence code for the Ensembl-pathway annotation (e.g. "IEA", "TAS"). Users may ask for "evidence" or "evidence code". |
| ncbi_pathway_reactome          | ncbi_gene_id        | NCBI Entrez gene ID participating in the pathway. Users may ask for "Entrez ID" or "NCBI gene ID". |
| ncbi_pathway_reactome          | ncbi_pathway_evidence | Evidence code for the NCBI-pathway annotation (e.g. "IEA", "TAS"). Users may ask for "evidence" or "evidence code". |

## string

| Table                    | Column              | Description |
|--------------------------|---------------------|-------------|
| protein_master_table     | protein_id          | STRING protein identifier (e.g. "9606.ENSP00000275493"). Users may ask for "STRING ID" or "protein ID". |
| protein_master_table     | gene_symbol         | HGNC short gene symbol of the protein (e.g. "EGFR"). Users may ask for "gene", "gene symbol", or "protein name". |
| protein_master_table     | protein_size        | Protein length in amino acids (integer, e.g. 1210 for EGFR). Users may ask for "size", "length", "protein size", or "amino acids". |
| protein_master_table     | annotation          | Free-text functional protein description (e.g. "Receptor tyrosine kinase"). Users may ask for "annotation", "function", or "description". |
| ppi_association          | association_score   | STRING overall functional-association confidence score (integer 700–1000, where 900+ = highest confidence). Users may ask for "combined score", "confidence score", "STRING score", or "interaction confidence". |
| ppi_physical             | physical_score      | STRING physical-interaction-only confidence score derived from binding and co-complex evidence (integer 700–1000). Users may ask for "physical interaction score", "physical PPI score", or "binding evidence". |
| ppi_detailed_channels    | neighborhood        | Per-channel score: genomic neighborhood evidence (0–1000). Users may ask for "neighborhood score" or "genomic neighborhood". |
| ppi_detailed_channels    | fusion              | Per-channel score: gene-fusion evidence (0–1000). Users may ask for "fusion score". |
| ppi_detailed_channels    | cooccurence         | Per-channel score: phylogenetic co-occurrence evidence (0–1000; note: schema spelling). Users may ask for "co-occurrence score" or "phylogenetic profile". |
| ppi_detailed_channels    | coexpression        | Per-channel score: co-expression evidence across organisms (0–1000). Users may ask for "co-expression score". |
| ppi_detailed_channels    | experimental        | Per-channel score: experimental / biochemical evidence (0–1000; from BioGRID, IntAct, etc.). Users may ask for "experimental score" or "experimental evidence". |
| ppi_detailed_channels    | database            | Per-channel score: curated-database evidence (0–1000; from Reactome, KEGG, etc.). Users may ask for "database score" or "curated evidence". |
| ppi_detailed_channels    | textmining          | Per-channel score: text-mining evidence from co-mentions in PubMed (0–1000). Users may ask for "text-mining score" or "literature evidence". |
| ppi_detailed_channels    | channel_combined_score | STRING combined confidence score aggregated across all evidence channels (integer 700–1000). Users may ask for "combined score", "overall STRING score", or "channel combined score". |
| protein_alias            | alias               | Alternate identifier / synonym for the protein (e.g. RefSeq accession, Ensembl ID, gene symbol). Users may ask for "alias", "synonym", or "alternate ID". |
| protein_alias            | source              | Source database for the alias (e.g. "Ensembl_HGNC", "RefSeq", "UniProt"). Users may ask for "alias source" or "source database". |

## uniprot

| Table                          | Column              | Description |
|--------------------------------|---------------------|-------------|
| protein_master_table           | protein_id          | UniProt accession (e.g. "P04637" for human p53, "P00533" for human EGFR). Users may ask for "UniProt ID", "UniProt accession", "accession", or "protein ID". |
| protein_master_table           | entry_name          | UniProt entry name in the format SYMBOL_SPECIES (e.g. "P53_HUMAN", "EGFR_HUMAN"). Users may ask for "entry name" or "UniProt name". |
| protein_master_table           | protein_name        | Full protein recommended name (e.g. "Cellular tumor antigen p53", "Epidermal growth factor receptor"). Users may ask for "protein name", "full name", or "recommended name". |
| protein_master_table           | gene_symbol         | HGNC short gene symbol (e.g. "TP53"). Users may ask for "gene" or "gene symbol". |
| protein_master_table           | ensembl_accession   | Ensembl gene ID cross-reference. Users may ask for "Ensembl ID". |
| protein_master_table           | entrez              | NCBI Entrez gene ID. Users may ask for "Entrez ID" or "NCBI gene ID". |
| protein_master_table           | organism            | Organism scientific name (e.g. "Homo sapiens", "Mus musculus"). Users may ask for "organism" or "species". |
| protein_master_table           | reviewed            | Whether the entry is Swiss-Prot reviewed (boolean; True = Swiss-Prot, False = TrEMBL). Users may ask for "Swiss-Prot reviewed", "reviewed", or "manually curated". |
| gene_protein_association       | gene_symbol         | HGNC symbol linked to the protein. |
| gene_protein_association       | hgnc_id             | HGNC identifier of the gene (e.g. "HGNC:3236"). Users may ask for "HGNC ID". |
| keyword_master_uniprot         | keyword_id          | UniProt keyword identifier (e.g. "KW-0053"). |
| keyword_master_uniprot         | keyword_name        | UniProt keyword (e.g. "Apoptosis", "Kinase", "Phosphoprotein"). Users may ask for "keyword" or "UniProt keyword". |
| keyword_master_uniprot         | category            | Keyword category ("Biological process", "Cellular component", "Molecular function", "Disease", "PTM", "Technical term"). Users may ask for "keyword category". |
| keyword_master_uniprot         | description         | Description of the keyword. |
| keyword_master_uniprot         | synonyms            | Synonyms for the keyword. |
| subcell_location_uniprot       | subcell_id          | UniProt subcellular location identifier (e.g. "SL-0086"). |
| subcell_location_uniprot       | subcell_name        | Subcellular localization term (e.g. "Cytoplasm", "Nucleus", "Plasma membrane", "Mitochondrion"). Users may ask for "subcellular location", "localization", "compartment", or "where is the protein". |
| subcell_location_uniprot       | description         | Description of the subcellular location. |
| subcell_location_uniprot       | keyword             | Associated UniProt keyword for the location. |
| gene_ontology_uniprot          | qualifier           | GO qualifier (e.g. "enables", "involved_in", "located_in", "part_of"). Users may ask for "GO qualifier". |
| gene_ontology_uniprot          | go_id               | Gene Ontology term ID (e.g. "GO:0006915" = apoptotic process). Users may ask for "GO ID", "GO term ID", or "GO accession". |
| gene_ontology_uniprot          | db_reference        | Source database reference for the GO annotation. |
| gene_ontology_uniprot          | evidence            | GO evidence code (e.g. "IDA"=Inferred from Direct Assay, "IEA"=Inferred from Electronic Annotation, "TAS"=Traceable Author Statement, "ISS"=Inferred from Sequence Similarity). Users may ask for "evidence code" or "GO evidence". |
| gene_ontology_uniprot          | aspect              | GO aspect ("P"=Biological Process, "C"=Cellular Component, "F"=Molecular Function). Users may ask for "GO aspect", "GO category", or "biological process/molecular function". |
| variant_disease_uniprot        | ftid                | UniProt variant feature ID (e.g. "VAR_005795"). Users may ask for "feature ID" or "variant feature ID". |
| variant_disease_uniprot        | swiss_prot_change   | Variant nomenclature in Swiss-Prot format (e.g. "p.Arg175His"). Users may ask for "variant nomenclature", "amino acid change", or "protein change". |
| variant_disease_uniprot        | variant_type        | Variant type label. |
| variant_disease_uniprot        | dbsnp               | dbSNP rsID cross-reference (e.g. "rs28934578"). Users may ask for "dbSNP", "rsID", or "rs number". |
| variant_disease_uniprot        | disease_name        | Disease name linked to the variant (e.g. "Li-Fraumeni syndrome"). Users may ask for "disease". |
| id_mapping_uniprot             | db                  | External database name for the cross-reference (e.g. "PDB", "RefSeq", "Ensembl", "GeneID"). Users may ask for "external database" or "cross-reference database". |
| id_mapping_uniprot             | external_id         | External identifier value in db. Users may ask for "external ID" or "cross-reference ID". |
| species_master_uniprot         | taxon_id            | NCBI taxonomy ID (e.g. 9606 for human, 10090 for mouse). Users may ask for "taxon ID", "taxonomy ID", or "NCBI taxon". |
| species_master_uniprot         | species_code        | UniProt 5-letter species mnemonic (e.g. "HUMAN", "MOUSE", "ECOLI"). Users may ask for "species code" or "UniProt mnemonic". |
| species_master_uniprot         | scientific_name     | Scientific species name (e.g. "Homo sapiens"). Users may ask for "scientific name" or "Latin name". |
| species_master_uniprot         | common_name         | Common species name (e.g. "Human", "Mouse"). Users may ask for "common name". |
| species_master_uniprot         | kingdom             | Taxonomic kingdom (e.g. "Eukaryota", "Bacteria", "Archaea", "Viruses"). Users may ask for "kingdom" or "domain of life". |

## msigdb

| Table | Column | Description |
|---|---|---|
| geneset_master_table | geneset_name | MSigDB gene-set / signature name in ALL-CAPS underscore-delimited form (e.g. "HALLMARK_APOPTOSIS", "GOBP_RESPONSE_TO_X_RAY", "REACTOME_CELL_CYCLE"). The source database/ontology is encoded as the prefix before the first underscore (HALLMARK_, GOBP_/GOMF_/GOCC_, REACTOME_, KEGG_, WP_, BIOCARTA_, PID_, HP_, MIR_). Users may ask for "gene set name", "signature", "pathway name", or "gene set". |
| geneset_master_table | collection | MSigDB top-level collection in friendly-name form. Observed values: Hallmark, Positional, Curated, Regulatory, Computational, Ontology, Oncogenic, Immunologic, CellType, CellLineage. Legacy codes (H, C1–C9, MH) do NOT appear on disk. Users may ask for "collection", "MSigDB category", "Hallmark", "Curated", "Ontology", etc. Note: source databases (KEGG, Reactome, GO, BioCarta, WikiPathways) are NOT collection values — they live in the geneset_name prefix and sub_collection. |
| geneset_master_table | gene_count | Number of unique genes in the gene set, stored as a String (e.g. "131"; observed range 5–1998). Users may ask for "gene count", "set size", or "number of genes". Cast to integer before numeric filtering. |
| geneset_master_table | geneset_organism | Organism the gene set was defined for. Values: "Homo sapiens" (~34k sets), "Mus musculus" (~18k sets), "Rattus norvegicus" (~31 sets). Users may ask for "organism", "species", "human gene sets", or "mouse gene sets". |
| gene_master_table | gene_symbol | HGNC (human) or MGI (mouse) gene symbol of a gene indexed in MSigDB (e.g. MYC, EGFR, TP53, KRAS, BRCA1). Users may ask for "gene", "gene symbol", "HGNC symbol", or "member gene". Master catalog of all gene symbols appearing in at least one MSigDB gene set. |
| gene_master_table | gene_organism | Organism for this gene symbol entry. Values: "Homo sapiens", "Mus musculus", "Rattus norvegicus". Use with gene_symbol to disambiguate human vs mouse orthologs. Users may ask for "organism" or "species". |
| geneset_metadata | pmid | PubMed ID of the source publication for the gene set. Users may ask for "PubMed ID", "PMID", or "source publication". NULL for ~67% of gene sets (no associated paper). |
| geneset_metadata | geo_id | GEO dataset accession the gene set was derived from (e.g. "GSE12345"). Users may ask for "GEO ID", "GEO accession", or "GEO dataset". NULL for ~84% of gene sets when not applicable. |
| geneset_metadata | sub_collection | Precise sub-collection / source-DB discriminator within the parent collection. Values include: CGP, CP, CP:BIOCARTA, CP:KEGG_LEGACY, CP:KEGG_MEDICUS, CP:PID, CP:REACTOME, CP:WIKIPATHWAYS, GO:BP, GO:CC, GO:MF, HPO, IMMUNESIGDB, MIR:MIRDB, MIR:MIR_LEGACY, TFT:GTRD, TFT:TFT_LEGACY, VAX, 3CA. Use this for source-DB routing: "KEGG canonical pathways" → sub_collection LIKE "CP:KEGG%"; "GO biological process" → sub_collection="GO:BP"; "Reactome" → "CP:REACTOME". Users may ask for "sub-collection", "KEGG", "Reactome", "GO BP", "WikiPathways", etc. |
| geneset_metadata | brief_description | Short free-text description of the gene set (e.g. "Genes involved in apoptosis"). Users may ask for "description", "brief description", or "what does this gene set represent". |
| geneset_metadata | full_description | Full free-text description of the gene set, providing detailed biological context. Users may ask for "full description" or "detailed description". NULL for ~65% of gene sets; filter with is_not_null() before displaying. |

## ttd

| Table | Column | Description |
|---|---|---|
| biomarker_disease_association | biomarker_id | TTD biomarker identifier; joins to biomarker_master_table for the biomarker name. |
| biomarker_disease_association | disease_id | TTD disease identifier; joins to disease_master_table for the disease name. |
| biomarker_master_table | biomarker_id | TTD biomarker identifier with a "BM" prefix, zero-padded to 6 digits (e.g. "BM000001", "BM000189"). Internal join key, rarely user-facing. |
| biomarker_master_table | biomarker_name | Clinical biomarker label, typically a full protein descriptor (e.g. "Erbb2 tyrosine kinase receptor (HER2)", "Cytochrome P450 2C19 (CYP2C19)", "GTPase KRas (KRAS)", "Proliferation marker protein Ki-67 (MKI67)") or a quantitative clinical measure (e.g. "high on-treatment platelet reactivity (P2Y12 reactivity unit [PRU] value of more than 234)"). Users may ask for "biomarker" or "diagnostic marker". |
| disease_master_table | disease_id | TTD disease identifier — an ICD-11 stem code (e.g. "1A00", "1A03", "BD40", "EB51.0") or an ICD-11 range (e.g. "1A00-1A09", "8A61-8A6Z"); a small number of free-text labels also appear (e.g. "Radiocontrast agent"). |
| disease_master_table | disease_name | Disease name as curated by TTD (e.g. "Cholera", "Type 2 diabetes", "Non-small cell lung cancer"). Users may ask for "disease", "indication", "condition", or "disorder". |
| drug_crossmatching_association | drug_id | TTD-internal drug identifier (D-prefix); joins to drug_master_table. |
| drug_crossmatching_association | formula | Molecular formula of the drug, e.g. "C29H31N7O". Users may ask for "molecular formula", "chemical formula", "empirical formula", or just "formula". |
| drug_crossmatching_association | pubchem_cid | PubChem Compound ID, stored as a string of digits (e.g. "5291" for imatinib). Users may ask for "PubChem ID", "PubChem CID", or "CID". |
| drug_crossmatching_association | pubchem_sid | PubChem Substance ID(s), stored as a semicolon-separated string of digits — usually "; " (semicolon-space) separator, but ~9% of populated rows (792/8,783) use a bare ";" instead, so tokenize with the regex `\s*;\s*` rather than a literal "; " split. Lists are long — the row for drug_id D00ABO (KW-2449, PubChem CID 11427553) starts with three SIDs "16524833", "23572482", "42506629" and continues to 30 SIDs in total. Users may ask for "PubChem SID" or "substance ID". |
| drug_crossmatching_association | drug_name | Drug preferred name as recorded in the crossmatching file. Usually matches drug_master_table.drug_name on the same drug_id, but 24/23,224 joined rows (0.10%) diverge — typically when TTD records a research-code/INN here while drug_master_table records a brand or alternate name (e.g. drug_id D02QFW: crossmatching has "Trofinetide" while master has "Daybue"). Prefer drug_master_table.drug_name when consistency matters; surface this column only when the crossmatching context is the user's intent. |
| drug_crossmatching_association | cas_number | CAS Registry Number. 99.8% of non-null rows (12,141/12,163) carry a literal "CAS " prefix (e.g. "CAS 152459-95-5", "CAS 50-78-2"); the remaining 22 rows store the bare digits-with-dashes form (e.g. "203191-10-0"). Filters should include the "CAS " prefix to match the dominant 99.8% — bare digits will match only those 22 rows. The runtime polars filter at app/utils/dataframe_filtering.py is case-insensitive but does NOT auto-prepend the prefix; the interpreter at resources/prompts/interpreter_db_notes.yaml PREFIX-PRESERVING block handles this for LLM-generated queries. Users may ask for "CAS", "CAS number", "CAS RN", or "registry number". |
| drug_crossmatching_association | chebi_xref | ChEBI identifier cross-reference (e.g. "CHEBI:45783" for imatinib). Users may ask for "ChEBI ID" or "ChEBI". |
| drug_crossmatching_association | superdrug_atc | ATC classification code(s) from SuperDrug (e.g. "L01XE01" for imatinib). Multi-code entries are semicolon-separated, e.g. "D08AX08; V03AB16; V03AZ01". Users may ask for "ATC code" or "ATC classification". |
| drug_crossmatching_association | superdrug_cas | SuperDrug-sourced CAS registry number. 100% of non-null rows (1,211/1,211) carry the literal prefix "cas=" plus a 9-digit zero-padded number without dashes (e.g. "cas=000051412"). Distinct from the canonical cas_number column above. Filters must use this exact shape — passing "51-41-2", "cas=51412", or "000051412" all match ZERO rows (no fallback forms exist on disk). |
| drug_disease_association | drug_id | TTD-internal drug identifier (D-prefix); joins to drug_master_table. |
| drug_disease_association | disease_id | TTD disease identifier (ICD-11 stem code); joins to disease_master_table. |
| drug_disease_association | approval_status | Drug development stage for the (drug, disease) pair. The TTD parquet carries 40 distinct raw strings, which collapse to 38 once case is folded — two casing- duplicate pairs exist on disk ("Clinical Trial"/"Clinical trial" and "Phase 3"/"phase 3"). The runtime filter at app/utils/dataframe_filtering.py lowercases both sides, so the casing dupes are invisible at filter time. Canonical write-form is Title-Case; emit values as listed below.  Values fall into these families: - Approved family: Approved \| Approved in EU \| Approved in China \|   Approved (orphan drug) \| Registered - Submission family: NDA filed \| BLA submitted \| IND submitted \|   Application submitted \| Approval submitted \| Preregistration - Phase family: Phase 0 \| Phase 1 \| Phase 1b \| Phase 1/2 \| Phase 1/2a \|   Phase 1b/2a \| Phase 2 \| Phase 2a \| Phase 2b \| Phase 2/3 \| Phase 3 \|   Phase 4 \| Clinical Trial - Discontinued family: Discontinued in Phase 1 \| Discontinued in Phase 1/2 \|   Discontinued in Phase 2 \| Discontinued in Phase 2a \| Discontinued in Phase 2b \|   Discontinued in Phase 2/3 \| Discontinued in Phase 3 \| Discontinued in Phase 4 \|   Discontinued in Preregistration \| Terminated \| Withdrawn from market - Early family: Investigative \| Preclinical \| Patented  Users may ask for "approval status", "clinical stage", "FDA status", or use family terms like "approved" / "in trials" / "discontinued"; map "FDA-approved" or "marketed" to "Approved". |
| drug_master_table | drug_id | TTD-internal drug identifier with a "D" prefix (e.g. "D00AAN", "D02EZF"). Users may say "TTD drug ID", "TTD identifier", or "drug accession". |
| drug_master_table | drug_name | Preferred drug name as curated by TTD. Casing follows the source compound name — mixed across the corpus (≈7% pure lowercase generic INNs like "imatinib", ≈40% uppercase research codes like "KW-2449", ≈53% mixed casing like "Opterone" or "SMP-797"). Users may ask for "drug", "compound", "medication", "therapy", "drug name", or just type the brand/generic name directly. |
| drug_synonyms_association | drug_id | TTD-internal drug identifier (D-prefix); joins to drug_master_table. |
| drug_synonyms_association | drug_name | Drug name as recorded in the synonyms file (a single drug typically has multiple synonym rows, so drug_name repeats). 30,673 distinct values across 238,077 rows. NOT a clean mirror of drug_master_table.drug_name — 55/24,984 shared drug_ids have set-divergent drug_name (e.g. drug_id D01ZAQ has master "Capivasertib" but synonyms-file "AZD5363"; D93JXG has master "Ngenla" but synonyms "Somatrogon"), typically when one table records the INN while the other records the developmental research code. Drug_id coverage also diverges: 7,676 drug_ids exist only in drug_master_table and 5,729 drug_ids exist only in this synonyms file. Prefer drug_master_table for the canonical primary drug name; use this column when also surfacing the synonym row context. |
| drug_synonyms_association | synonym | Alternate drug name, brand name, trade name, or common synonym. Trade names carry a literal " (TN)" suffix on disk (e.g. "Gleevec (TN)" for imatinib); other entries are bare strings of varied shape — CHEMBL IDs ("CHEMBL400717"), NSC codes ("NSC-297,170"), BRN registry numbers ("BRN 3971661"), CAS-like digit strings, IUPAC partial names, or common names. The runtime filter at app/utils/dataframe_filtering.py applies SUBSTRING matching to this column (as of 2026-05-23, same treatment as variant_name/disease_name) — so a user filter on "Gleevec" correctly matches the "Gleevec (TN)" row, and a filter on "CHEMBL" returns all ChEMBL-ID synonyms. Users may ask for "synonym", "alias", "brand name", "trade name", "alternate name", or "also known as". |
| drug_target_association | drug_id | TTD-internal drug identifier (D-prefix); joins to drug_master_table. |
| drug_target_association | target_id | TTD-internal target identifier (T-prefix); joins to target_master_table. |
| drug_target_association | drug_mechanism_of_action_on_target | Mechanism of action of the drug on its target. The TTD parquet carries 47 distinct raw strings dominated by Title-Case nouns; top 5 by row count: Inhibitor (29,747), Modulator (4,737), Antagonist (3,566), Agonist (2,690), Activator (389). Long-tail values include Binder, Blocker, Chelator, Co-agonist, Cofactor, Degrader, Disrupter, Enhancer, Immunostimulant, Inducer, Intercalator, Inverse agonist, Ligand, Mimetic, Opener, Partial agonist, Potentiator, Reactivator, Regulator, Replacement, Silencer, Stabilizer, Stimulator, Suppressor, Antisense, CAR-T-Cell-Therapy, plus parenthesised refinements like "Blocker (channel blocker)", "Modulator (allosteric modulator)", "Inhibitor (gating inhibitor)", "Regulator (upregulator)", "Binder (minor groove binder)", "Immunomodulator (Immunostimulant)", "Antagonist/GLP1 agonist", "CAR-T-Cell-Therapy(Dual specific)". Casing on disk is mostly Title-Case but a few rows carry typos / lowercase variants ("Agonis" (misspelling), "Stablizer" (misspelling), "antagonist" (lowercase), "inhibitor" (lowercase)). The runtime filter at app/utils/dataframe_filtering.py is case-insensitive so lowercase user input will match Title-Case rows. Users may ask for "mechanism", "MoA", "mechanism of action", "drug action", or "how does the drug work". |
| pathway_master_table | pathway_id | KEGG human-pathway identifier with an "hsa" prefix (e.g. "hsa00010" for Glycolysis/Gluconeogenesis, "hsa04010" for MAPK signaling). |
| pathway_master_table | pathway_name | Biological pathway name from KEGG (e.g. "Glycolysis / Gluconeogenesis", "MAPK signaling pathway", "Apoptosis"). Users may ask for "pathway", "signaling pathway", or "biological pathway". Note TTD pathways are KEGG-only; for Reactome or WikiPathways use the dedicated pathway DBs. |
| target_compound_activity_association | target_id | TTD-internal target identifier (T-prefix); joins to target_master_table. |
| target_compound_activity_association | drug_compound_id | TTD drug or compound identifier (D-prefix for curated drugs, C-prefix for raw screening compounds; C-compounds dominate ~95% of rows). |
| target_compound_activity_association | pubchem_cid | PubChem CID of the screening compound (string of digits). Useful for cross-DB joins to PubChem/ChEMBL/ChEBI compound records. Distinct from the drug-level pubchem_cid in drug_crossmatching_association — this one is the screened compound, which may be a research molecule never promoted to a drug. |
| target_compound_activity_association | gene_symbol | HGNC gene symbol (or non-human mnemonic) of the assayed target. 1,511 distinct non-null values across 829,159 rows; 61,438 rows (7.4%) are null where the upstream literature did not provide a gene mapping. For every target_id shared with target_master_table the SET of gene_symbol values agrees (verified — 0/1,507 target_ids have set-divergent gene_symbols), though row-orderings differ for multi-gene complexes — same per-target SET-identity relationship as target_uniprot_association.gene_symbol. Same human / non-human mnemonic caveats as target_master_table.gene_symbol. |
| target_compound_activity_association | activity_raw | Raw unprocessed activity string from the literature, e.g. "IC50 = 3270000 nM", "IC50 = 21370 nM". Real on-disk values are essentially always "<activity_type> <activity_operator> <activity_value> nM". Users may ask for "raw value" or "literature value". |
| target_compound_activity_association | activity_type | Bioactivity measurement type. Disk has exactly 3 non-null values across 829,159 rows: "IC50", "Ki", "EC50" (plus 2,222 null rows). Note: "Kd" does NOT appear in the TTD corpus despite being a common bioactivity type elsewhere. Users may ask for "activity type", "potency type", or use the assay name directly. |
| target_compound_activity_association | activity_operator | Comparator for the activity value. Disk has 6 non-null values: "<", "<=", "=", ">", ">=", "~" (plus 2,222 null rows). Users may ask for "operator", "comparator", or use phrasings like "less than 10 nM" / "at most 100 nM". |
| target_compound_activity_association | activity_value | Numeric bioactivity value (Float64); interpret in conjunction with activity_unit. Users may ask for "potency", "binding affinity", "activity value", or "IC50 value". Lower values typically mean more potent. |
| target_compound_activity_association | activity_unit | Unit for activity_value. The TTD parquet has exactly ONE non-null value across all 829,159 rows: "nM" (plus 2,222 null rows). Other units (µM, mM, M, pM) do NOT appear in this corpus — the upstream loader has already normalized every potency to nanomolar. Users may type "uM" or "micromolar" in a query but the filter target should be "nM"; for sub-µM thresholds, pass the equivalent nM value (e.g. "1 µM" → activity_value < 1000, activity_unit = "nM"). |
| target_disease_association | target_id | TTD-internal target identifier (T-prefix); joins to target_master_table. |
| target_disease_association | disease_id | TTD disease identifier; joins to disease_master_table to recover the human-readable disease name. |
| target_master_table | target_id | TTD-internal target identifier with a "T" prefix (e.g. "T00032", "T47101"). Users may say "TTD target ID" or "target accession". |
| target_master_table | target_name | Therapeutic target preferred name, usually a protein or receptor (e.g. "Epidermal growth factor receptor", "Beta-2 adrenergic receptor"). Users may ask for "target", "target name", "protein target", or "therapeutic target". |
| target_master_table | gene_symbol | HGNC gene symbol for human targets (e.g. "EGFR", "BRAF", "ADRB2"). Non-human / viral / bacterial / fungal targets carry mnemonic phrases instead (e.g. "HIV rev", "HIV gp120", "Bact pla", "Fung erg6"). Users may ask for "gene", "gene symbol", "HGNC symbol", or just type the symbol. |
| target_pathway_association | target_id | TTD-internal target identifier (T-prefix); joins to target_master_table. |
| target_pathway_association | pathway_id | KEGG pathway identifier (hsa-prefix); joins to pathway_master_table to recover the human-readable pathway name. |
| target_uniprot_association | target_id | TTD-internal target identifier (T-prefix); joins to target_master_table. |
| target_uniprot_association | gene_symbol | HGNC gene symbol (or non-human mnemonic) for the target. The SET of gene_symbol values per target_id is identical to target_master_table (verified — 0 target_ids have set-divergent gene_symbol values), but the two tables produce different cross-product row-orderings for multi-gene target complexes (e.g. target_id T01447 has rows ordered (NAE1, UBA3) in one table and (UBA3, NAE1) in the other; the underlying set {NAE1, UBA3} is shared). Row-by-row equality between the two columns therefore appears to diverge ~14.7% of joined rows even though the per-target sets agree. 629/4,487 rows (14%) are null. Same human / non-human / viral / bacterial mnemonic caveats as target_master_table.gene_symbol (e.g. "HIV rev", "Fung erg6"). For canonical gene_symbol queries prefer target_master_table; use this column when also retrieving uniprot_xref / target_type from the same row. |
| target_uniprot_association | target_name | Therapeutic target preferred name. 4,296 distinct values across 4,487 rows, 0 nulls. For the 3,669 target_ids shared with target_master_table the per-target SET of target_name agrees (verified — 0 set-divergent targets); but this table has BROADER coverage with 629 additional target_ids that exist only in the uniprot-association file (i.e. they have a UniProt mapping but no full target_master_table entry). Prefer target_master_table when also retrieving gene_symbol; use this column when also retrieving uniprot_xref / target_type / the 629 uniprot-only targets. |
| target_uniprot_association | uniprot_xref | UniProt entry name (mnemonic) for the target protein, e.g. "EGFR_HUMAN", "OSTP_HUMAN", "TGFA_HUMAN", "ERG6_PNEC8". NOT the P-prefixed accession number (P00533). Sentinel "NOUNIPROTAC" indicates no UniProt mapping (613/4487 rows ≈ 13.7%). Users may ask for "UniProt ID", "UniProt accession", "UniProt entry name", or "protein ID". |
| target_uniprot_association | target_type | TTD target development class. Exactly 6 distinct values (case-insensitive at filter layer; canonical write-form is Sentence-case as below): "Successful target" (drug-approved), "Clinical trial target", "Literature-reported target", "Preclinical target", "Patented-recorded target", "Discontinued target". Users may ask for "target class", "target type", or "development stage". |
