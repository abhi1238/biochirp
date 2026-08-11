# BioChirp Database Registry

_Auto-generated. Use this registry when populating `relevant_databases`: pick the database(s) whose **entities**, **relationships**, or **filter fields** best cover the entities and predicates in the query. Do not memorise topic-to-database rules — match the query against what each database actually exposes here._

## ClinVar
- **Version:** ClinVar 2025-05 variant summary (2025-05-06)
- **Entities:** gene, variant
- **Relationships:** variant↔disease, variant↔gene
- **Filterable on:** gene_name, gene_symbol, target_name, variant_name, disease_name, clinical_significance, review_status

## CTD
- **Version:** CTD 2024 Q1 snapshot (2024-01-01)
- **Entities:** chemical, disease, gene, pathway
- **Relationships:** chemical↔disease, chemical↔gene, disease↔pathway, gene↔disease, gene↔pathway
- **Filterable on:** drug_name, disease_name, gene_name, gene_symbol, target_name, pathway_name, phenotype_name, chemical_name

## HCDT
- **Version:** v2 bulk download (Apr 2026 refresh) (2026-04 (raw_v2/), legacy v1 snapshot 2024-03-01)
- **Entities:** disease, drug, gene, pathway, rna
- **Relationships:** drug↔disease, drug↔gene, drug↔pathway, drug↔rna
- **Filterable on:** drug_name, disease_name, target_name, gene_name, gene_symbol, pathway_name, rna_name, rna_type

## HPO
- **Version:** HPO 2025-01 release (2025-01-01)
- **Entities:** disease, gene, phenotype
- **Relationships:** disease↔phenotype, gene↔disease, gene↔phenotype, phenotype↔gene
- **Filterable on:** phenotype_name, disease_name, gene_name, gene_symbol, target_name

## MSigDB
- **Version:** MSigDB (human Hallmark/C1–C9 + mouse MH/M1–M8 gene-set collections)
- **Entities:** gene, gene set (signature)
- **Relationships:** gene↔geneset
- **Filterable on:** geneset_name, gene_symbol, collection, organism

## Orphanet
- **Version:** Orphanet product4 (gene-disease XML) 2025-05 (2025-05-06)
- **Entities:** disease, gene
- **Relationships:** disease↔phenotype, gene↔disease
- **Filterable on:** disease_name, gene_name, gene_symbol, target_name, hpo_term

## Reactome
- **Version:** Reactome v89 (2025-02) (2025-02-01)
- **Entities:** gene, pathway
- **Relationships:** gene↔pathway
- **Filterable on:** pathway_name, gene_name, gene_symbol, target_name

## STRING
- **Version:** STRING v12.0 human PPI (9606) (2023-01-01)
- **Entities:** protein
- **Relationships:** protein↔protein
- **Filterable on:** gene_symbol, gene_name, target_name, annotation

## TTD
- **Version:** TTD 2024 release (March 2024) (2024-03-01)
- **Entities:** biomarker, disease, drug, pathway, target
- **Relationships:** biomarker↔disease, drug↔crossmatching, drug↔disease, drug↔synonyms, drug↔target, target↔compound, target↔disease, target↔pathway, target↔uniprot
- **Filterable on:** drug_name, target_name, gene_name, gene_symbol, disease_name, approval_status, biomarker_name, pathway_name, drug_mechanism_of_action_on_target, mechanism_of_action, synonym, uniprot_xref, target_type, pubchem_cid, pubchem_sid, cas_number, cas, chebi_xref, superdrug_atc, superdrug_cas, formula, activity_type, activity_value, activity_unit, activity_operator, drug_compound_id

## UniProt
- **Version:** UniProt Swiss-Prot human (2025-05) (2025-05-06)
- **Entities:** protein
- **Relationships:** gene↔protein
- **Filterable on:** gene_symbol, gene_name, target_name, accession, entry_name, protein_name, reviewed, organism, keyword_name, subcell_name, go_id, qualifier

## WEB
- **Version:** n/a (n/a)
- **Entities:** (none)
- **Relationships:** (none)
- **Filterable on:** (see schema columns)

