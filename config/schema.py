from typing import Any, Dict, List, Literal, Optional, Tuple, Union

def validate_schema(database_schemas: dict):
    """Validate schema consistency at startup. Fail fast on errors."""
    for db, tables in database_schemas.items():
        for table, cols in tables.items():
            if not cols:
                raise ValueError(f"{db}.{table} has no columns")
            if len(cols) != len(set(cols)):
                raise ValueError(f"{db}.{table} has duplicate columns: {cols}")
            # Master tables must have exactly one _id column
            if table.endswith("_master_table"):
                id_cols = [c for c in cols if c.endswith("_id")]
                if len(id_cols) != 1:
                    raise ValueError(
                        f"{db}.{table} must have exactly one primary key ID, found {id_cols}"
                    )


def _build_id_to_master_table_map(tables: dict) -> dict:
    """Maps id column name -> master table name for a given DB."""
    id_to_master = {}
    for table_name, columns in tables.items():
        if table_name.endswith("_master_table"):
            for col in columns:
                if col.endswith("_id"):
                    id_to_master[col] = table_name
    return id_to_master


def generate_primary_keys(database_schemas: dict) -> dict:
    primary_keys_by_db = {}
    for db_name, tables in database_schemas.items():
        primary_keys_by_db[db_name] = {}
        for table_name, columns in tables.items():
            if table_name.endswith("_master_table"):
                # Use the _id column explicitly, not positional (handles ordering inconsistencies)
                pk = [col for col in columns if col.endswith("_id")]
                primary_keys_by_db[db_name][table_name] = pk
            elif "_association" in table_name:
                pk = [col for col in columns if col.endswith("_id")]
                primary_keys_by_db[db_name][table_name] = pk
    return primary_keys_by_db


def generate_foreign_keys(database_schemas: dict) -> dict:
    foreign_keys_by_db = {}
    for db_name, tables in database_schemas.items():
        fk_list = []
        id_to_master = _build_id_to_master_table_map(tables)

        for table_name, columns in tables.items():
            # Phase-2 expansion tables often don't follow the `_association`
            # naming convention (e.g. ppi_physical, protein_alias, chebi_pathway_reactome)
            # but they still share an `_id` column
            # with a master table and SHOULD participate in joins. Without this
            # the planner's Steiner cover reports disconnected components and
            # the tool returns "Planner failed." Fix 2026-05-13.
            if table_name.endswith("_master_table"):
                continue
            for col in columns:
                if col.endswith("_id") and col in id_to_master:
                    master_table = id_to_master[col]
                    fk_list.append((table_name, col, master_table, col))

        foreign_keys_by_db[db_name] = fk_list
    return foreign_keys_by_db




# NOTE: OpenTargets is intentionally NOT registered here. The opentarget_service
# is a live GraphQL client (see opentarget_service/app/graphql.py) and does not
# participate in the federated parquet planner. The parquet snapshots under
# database/opentargets/ are reference dumps only. TODO: revisit if/when a
# parquet-backed loader replaces the GraphQL path.
database_schemas = {

    # >>> GENERATED ttd BEGIN — DO NOT EDIT (source: dbs/ttd/schema.yaml; run: python scripts/gen_schema.py --write --db ttd) <<<
    "ttd": {
        "biomarker_disease_association": ['biomarker_id', 'disease_id'],
        "biomarker_master_table": ['biomarker_id', 'biomarker_name'],
        "disease_master_table": ['disease_id', 'disease_name'],
        # Cross-DB identifier bridge for TTD drugs (PubChem / ChEBI / CAS / SuperDrug).
        "drug_crossmatching_association": ['drug_id', 'formula', 'crossmatch_pubchem_cid', 'pubchem_sid', 'cas_number', 'chebi_xref', 'superdrug_atc', 'superdrug_cas'],
        "drug_disease_association": ['drug_id', 'disease_id', 'approval_status'],
        "drug_master_table": ['drug_id', 'drug_name'],
        # Long-form drug synonyms (one row per drug_id × synonym).
        "drug_synonyms_association": ['drug_id', 'synonym'],
        "drug_target_association": ['target_id', 'drug_id', 'drug_mechanism_of_action_on_target'],
        "pathway_master_table": ['pathway_id', 'pathway_name'],
        # Quantitative target-compound bioactivity (IC50 / Ki / EC50, nM). drug_compound_id covers both TTD drug IDs (D-prefix) and raw compound IDs (C-prefix) not in drug_master_table.
        "target_compound_activity_association": ['target_id', 'drug_compound_id', 'compound_activity_pubchem_cid', 'activity_raw', 'activity_type', 'activity_operator', 'activity_value', 'activity_unit'],
        # compound_activity_gene_symbol omitted (exec_schema:false): canonical source is target_master_table.gene_symbol; keeping it in the exec schema too caused a Polars _right join collision on multi-table plans
        "target_disease_association": ['target_id', 'disease_id'],
        "target_master_table": ['target_id', 'target_name', 'gene_symbol'],
        "target_pathway_association": ['target_id', 'pathway_id'],
        # Target ↔ UniProt mapping (bridges TTD targets to UniProt / HGNC / Ensembl downstream).
        "target_uniprot_association": ['target_id', 'uniprot_xref', 'target_type'],
        # uniprot_target_name omitted (exec_schema:false): comes from target_master_table via the target_id FK; duplicating it here caused Polars _right suffix collisions on multi-table plans
        # uniprot_gene_symbol omitted (exec_schema:false): comes from target_master_table via the target_id FK; duplicating it here caused Polars _right suffix collisions on multi-table plans
    },
    # >>> GENERATED ttd END <<<
    # CTD v2 schema — adds DirectEvidence, PubMedIDs, InteractionActions,
    # InferenceScore as filterable/output columns, plus 3 new tables.
    # >>> GENERATED ctd BEGIN — DO NOT EDIT (source: dbs/ctd/schema.yaml; run: python scripts/gen_schema.py --write --db ctd) <<<
    "ctd": {
        "chemical_gene_association": ['drug_id', 'gene_id', 'interaction_text', 'chemical_gene_interaction_actions', 'pubmed_count', 'gene_forms'],
        "gene_pathway_association": ['gene_id', 'pathway_id'],
        "gene_disease_association": ['gene_id', 'disease_id', 'gene_disease_direct_evidence', 'evidence_rank', 'pubmed_count'],
        "chemical_disease_association": ['drug_id', 'disease_id', 'chem_disease_direct_evidence', 'evidence_rank', 'pubmed_count', 'inference_gene', 'inference_score'],
        "disease_pathway_association": ['disease_id', 'pathway_id', 'disease_pathway_inference_gene'],
        "chemical_master_table": ['drug_id', 'drug_name', 'cas_rn', 'pubchem_cid', 'inchikey', 'chemical_definition'],
        # Chemical/drug synonym lookup — one row per drug_id × synonym, derived at load time by exploding chemical_master_table_ctd.chemical_synonyms. Use for exact synonym-to-chemical mapping.
        "chemical_synonyms_association": ['drug_id', 'chemical_synonym'],
        # Gene synonym/alias lookup — one row per gene_id × synonym, derived at load time by exploding gene_master_table_ctd.gene_synonyms (~42% of genes have synonyms). Use for exact alias-to-gene mapping.
        "gene_synonyms_association": ['gene_id', 'gene_synonym'],
        # Disease synonym lookup — one row per disease_id × synonym, derived at load time by exploding disease_master_table_ctd.disease_synonyms (~85% of diseases have synonyms). Use for exact synonym-to-disease mapping.
        "disease_synonyms_association": ['disease_id', 'disease_synonym'],
        "pathway_master_table": ['pathway_id', 'pathway_name'],
        "gene_master_table": ['gene_id', 'gene_symbol', 'gene_name', 'uniprot_ids'],
        "disease_master_table": ['disease_id', 'disease_name', 'disease_definition', 'slim_mappings'],
        "chemical_pathway_enriched": ['drug_id', 'pathway_id', 'p_value', 'corrected_p_value', 'target_match', 'target_total'],
        "chemical_phenotype_ixn": ['drug_id', 'phenotype_id', 'chemical_phenotype_organism_id', 'chemical_phenotype_interaction_text', 'interaction_actions', 'anatomy_terms', 'inference_genes', 'chemical_phenotype_name', 'co_mentioned_terms'],
        "exposure_studies_ctd": ['study_reference', 'study_factors', 'exposure_stressors', 'study_receptors', 'exposure_study_countries', 'mediums', 'exposure_markers', 'diseases', 'phenotypes', 'author_summary'],
        "exposure_events_ctd": ['stressor_name', 'stressor_id', 'disease_id', 'event_phenotype_name', 'phenotype_id', 'phenotype_action_type', 'exposure_marker', 'exposure_marker_id', 'outcome_relationship', 'event_reference', 'sex', 'age', 'age_units', 'race', 'smoking_status', 'medium', 'event_study_countries', 'anatomy', 'enrollment_start_year', 'enrollment_end_year', 'stressor_source_category', 'event_receptors'],
        # Many-to-many cross-reference between exposure studies (by PubMed reference) and the diseases they study; derived from exposure_studies_v2. 2,836 rows.
        "exposure_study_disease_association": ['reference', 'disease_id'],
        # Many-to-many cross-reference between exposure studies (by PubMed reference) and the chemical stressors examined; derived from exposure_studies_v2. 12,003 rows.
        "exposure_study_stressor_association": ['reference', 'drug_id'],
    },
    # >>> GENERATED ctd END <<<

    # HCDT v2 schema — association tables are minimal FK pairs (no denormalised
    # names/xrefs); master tables carry the human-readable names and cross-DB IDs.
    # Source of truth: schema_kg/inputs/hcdt/schema.json
    # >>> GENERATED hcdt BEGIN — DO NOT EDIT (source: dbs/hcdt/schema.yaml; run: python scripts/gen_schema.py --write --db hcdt) <<<
    "hcdt": {
        "drug_master_table": ['drug_id', 'drug_name', 'drug_mw', 'drug_formula', 'drug_inchi', 'drug_smiles_iso', 'drug_smiles_canon', 'drug_inchikey', 'drug_iupac'],
        # Drug synonym lookup — one row per drug_id × synonym, derived at load time by exploding drug_master_table_hcdt.drug_synonyms. Use for exact synonym-to-drug mapping (brand names, trade names, alternate INN forms).
        "drug_synonyms_association": ['drug_id', 'synonym'],
        "drug_gene_association": ['drug_id', 'gene_id', 'source_count', 'ttd_confirmed'],
        "drug_disease_association": ['drug_id', 'disease_id'],
        "drug_pathway_association": ['drug_id', 'pathway_id'],
        "disease_master_table": ['disease_id', 'disease_name', 'icd11', 'omim_xref'],
        "gene_master_table": ['gene_id', 'gene_symbol', 'uniprot', 'hgnc', 'ensembl', 'entrez'],
        "pathway_master_table": ['pathway_id', 'pathway_name', 'kegg_hsa_xref', 'smpdb_xref', 'chebi_xref', 'kegg_xref'],
        "rna_master_table": ['rna_id', 'rna_name', 'rna_type'],
        "drug_rna_association": ['drug_id', 'rna_id'],
        "drug_target_negative": ['drug_id', 'gene_id', 'ki_nm', 'ic50_nm', 'kd_nm'],
        "pathway_gene_association": ['gene_id', 'pathway_id'],
        # synthetic table built at runtime by database_loader by unpivoting pathway_master_table columns {kegg_hsa_xref, smpdb_xref, chebi_xref, kegg_xref} into long form (pathway_id, xref_source, xref_id); not a parquet file on disk (all columns added_at_load); Reactome IDs are the pathway_id itself and are NOT duplicated in this table
        "pathway_xref": ['pathway_id', 'xref_source', 'xref_id'],
    },
    # >>> GENERATED hcdt END <<<

    # >>> GENERATED hpo BEGIN — DO NOT EDIT (source: dbs/hpo/schema.yaml; run: python scripts/gen_schema.py --write --db hpo) <<<
    "hpo": {
        "gene_master_table_hpo": ['gene_id', 'gene_symbol'],
        "disease_master_table_hpo": ['disease_id', 'disease_name'],
        "phenotype_master_table_hpo": ['phenotype_id', 'phenotype_name', 'definition'],
        "gene_phenotype_association_hpo": ['gene_id', 'phenotype_id', 'gene_phenotype_frequency', 'disease_id'],
        "disease_phenotype_association_hpo": ['disease_id', 'phenotype_id'],
        "phenotype_hierarchy_hpo": ['phenotype_id', 'parent_id'],
        "phenotype_synonym_hpo": ['phenotype_id', 'synonym'],
        "phenotype_gene_association_hpo": ['phenotype_id', 'gene_id', 'disease_id'],
        "gene_disease_association_hpo": ['gene_id', 'ncbi_gene_id', 'association_type', 'disease_id', 'source'],
        "disease_phenotype_annotation_hpo": ['disease_id', 'qualifier', 'phenotype_id', 'reference', 'evidence', 'onset', 'disease_phenotype_frequency', 'sex', 'modifier', 'aspect', 'biocuration'],
    },
    # >>> GENERATED hpo END <<<

    # >>> GENERATED clinvar BEGIN — DO NOT EDIT (source: dbs/clinvar/schema.yaml; run: python scripts/gen_schema.py --write --db clinvar) <<<
    "clinvar": {
        "variant_master_table_clinvar": ['variant_id', 'variant_name', 'variant_type', 'aggregate_clinical_significance', 'aggregate_review_status'],
        "gene_master_table_clinvar": ['gene_id', 'gene_symbol'],
        "variant_gene_association_clinvar": ['variant_id', 'gene_id'],
        "variant_disease_association_clinvar": ['variant_id', 'disease_id', 'variant_disease_name', 'disease_clinical_significance'],
        # Phase 3 (2026-05-14): submitter-level interpretations — loaded by the loader but previously unregistered.
        "variant_submission_clinvar": ['variant_id', 'submission_clinical_significance', 'date_last_evaluated', 'description', 'submitted_phenotype', 'reported_phenotype', 'submission_review_status', 'collection_method', 'origin_counts', 'submitter', 'scv', 'submitted_gene_symbol', 'explanation', 'somatic_clinical_impact', 'oncogenicity', 'contributes_to_aggregate'],
        "variant_citation_clinvar": ['variant_id', 'citation_allele_id', 'rsid', 'nsv', 'citation_source', 'citation_id'],
        "variant_genomic_coords_clinvar": ['variant_id', 'genomic_allele_id', 'assembly', 'chrom', 'pos', 'ref', 'alt', 'clnsig', 'clnrevstat', 'clndn', 'clndisdb', 'gene_info', 'molecular_consequence', 'variant_class'],
    },
    # >>> GENERATED clinvar END <<<

    # >>> GENERATED reactome BEGIN — DO NOT EDIT (source: dbs/reactome/schema.yaml; run: python scripts/gen_schema.py --write --db reactome) <<<
    "reactome": {
        "pathway_master_table_reactome": ['pathway_id', 'pathway_name'],
        "gene_master_table_reactome": ['gene_id', 'gene_symbol'],
        "gene_pathway_association_reactome": ['gene_id', 'pathway_id', 'gene_pathway_evidence'],
        "pathway_hierarchy_reactome": ['parent_id', 'parent_pathway_name', 'child_id', 'child_pathway_name'],
        "uniprot_pathway_reactome": ['uniprot_accession', 'pathway_id', 'uniprot_pathway_evidence'],
        "chebi_pathway_reactome": ['chebi_id', 'pathway_id', 'chebi_pathway_evidence'],
        "ensembl_pathway_reactome": ['ensembl_id', 'pathway_id', 'ensembl_pathway_evidence'],
        "ncbi_pathway_reactome": ['ncbi_gene_id', 'pathway_id', 'ncbi_pathway_evidence'],
    },
    # >>> GENERATED reactome END <<<

    "orphanet": {
        "disease_master_table_orphanet": ['disease_id', 'disease_name'],
        "gene_master_table_orphanet": ['gene_id', 'gene_symbol', 'ensembl_accession', 'entrez'],
        "gene_disease_association_orphanet": ['gene_id', 'gene_symbol', 'disease_id', 'gene_disease_association_type'],
        "disease_phenotype_association_orphanet": ['disease_id', 'phenotype_id', 'hpo_term', 'frequency'],
        "disease_xref_orphanet": ['disease_id', 'xref_source', 'xref_id', 'mapping_relation', 'disease_xref_validation_status'],
        "disease_epidemiology_orphanet": ['disease_id', 'source', 'prevalence_type', 'prevalence_qualification', 'prevalence_class', 'val_moy', 'geographic', 'disease_epidemiology_validation_status'],
        "disease_onset_inheritance_orphanet": ['disease_id', 'attribute', 'value'],
        "disease_natural_history_orphanet": ['disease_id', 'target_disease_id', 'target_disease_name', 'disease_history_association_type'],
        "disease_classification_orphanet": ['classification', 'disease_id', 'disorder_type', 'parent_disease_id', 'parent_disease_name'],
    },
    # >>> GENERATED orphanet END <<<

    # >>> GENERATED string BEGIN — DO NOT EDIT (source: dbs/string/schema.yaml; run: python scripts/gen_schema.py --write --db string) <<<
    "string": {
        "protein_master_table": ['protein_id', 'gene_symbol', 'protein_size', 'annotation'],
        "ppi_association": ['protein_id', 'association_gene_symbol', 'protein_partner_id', 'association_partner_gene_symbol', 'association_score', 'association_partner_protein_size'],
        "ppi_physical": ['protein_id', 'physical_gene_symbol', 'protein_partner_id', 'physical_partner_gene_symbol', 'physical_score', 'physical_partner_protein_size'],
        "ppi_detailed_channels": ['protein_id', 'channel_gene_symbol', 'protein_partner_id', 'channel_partner_gene_symbol', 'neighborhood', 'fusion', 'cooccurence', 'coexpression', 'experimental', 'database', 'textmining', 'channel_combined_score', 'channels_partner_protein_size'],
        "protein_alias": ['protein_id', 'alias', 'source'],
    },
    # >>> GENERATED string END <<<

    # >>> GENERATED uniprot BEGIN — DO NOT EDIT (source: dbs/uniprot/schema.yaml; run: python scripts/gen_schema.py --write --db uniprot) <<<
    "uniprot": {
        "protein_master_table": ['protein_id', 'entry_name', 'protein_name', 'gene_symbol', 'ensembl_accession', 'entrez', 'organism', 'reviewed', 'hgnc'],
        "gene_protein_association": ['protein_id', 'ensembl_accession', 'entrez'],
        "keyword_master_uniprot": ['keyword_id', 'keyword_name', 'category', 'keyword_description', 'keyword_synonyms', 'keyword_entry_type', 'keyword_hierarchy'],
        "subcell_location_uniprot": ['subcell_id', 'subcell_name', 'subcell_description', 'subcell_synonyms', 'keyword_id', 'subcell_hierarchy', 'subcell_entry_type'],
        "gene_ontology_uniprot": ['protein_id', 'qualifier', 'go_id', 'db_reference', 'evidence', 'aspect'],
        "variant_disease_uniprot": ['protein_id', 'ftid', 'swiss_prot_change', 'variant_type', 'dbsnp', 'disease_id', 'disease_name'],
        "id_mapping_uniprot": ['protein_id', 'db', 'external_id'],
        # protein→UniProt-keyword membership edge (one row per protein×keyword), parsed from the KW lines of uniprot_sprot_human.dat. Join protein_master_table_uniprot on protein_id, and keyword_master_uniprot on keyword_id for the human-readable keyword_name / category. Answers "what keywords are annotated for protein X".
        "protein_keyword_uniprot": ['protein_id', 'keyword_id'],
        # protein→subcellular-location membership edge (one row per protein×location), parsed from the CC SUBCELLULAR LOCATION lines of uniprot_sprot_human.dat. Join protein_master_table_uniprot on protein_id, and subcell_location_uniprot on subcell_id for the human-readable subcell_name. Answers "what subcellular locations is protein X found in".
        "protein_subcell_uniprot": ['protein_id', 'subcell_id'],
        # Free-text FUNCTION annotation for each Swiss-Prot reviewed human protein, parsed from CC -!- FUNCTION lines in uniprot_sprot_human.dat. One row per protein (first/canonical FUNCTION block). Answers questions about what a protein does, its role in a pathway or process, its mechanism of action as a molecular entity. Join protein_master_table_uniprot on protein_id for gene_symbol / protein_name.
        "protein_function_uniprot": ['protein_id', 'function_text'],
        # Residue-level post-translational modification (PTM) sites parsed from FT MOD_RES feature blocks in uniprot_sprot_human.dat. One row per (protein, position, modification) triple. Covers phosphorylation, methylation, acetylation, ubiquitination, sumoylation, and other curated PTMs. Answers "is protein X phosphorylated?", "what residues of gene Y are modified?", "which kinase phosphorylates protein Z?". Join protein_master_table_uniprot on protein_id.
        "ptm_sites_uniprot": ['protein_id', 'position', 'modification', 'enzymes'],
        # Protein-protein interaction (PPI) data parsed from CC -!- SUBUNIT and CC -!- INTERACTION lines in uniprot_sprot_human.dat. Two interaction_type values: SUBUNIT = free-text curator prose describing subunit composition, complex membership, and binding partners; BINARY = structured binary interaction with experiment count from IntAct. Answers "what are the interaction partners of protein X?", "is protein A in a complex with protein B?", "what proteins does gene Y interact with?", "list the subunits of complex Z". Join protein_master_table_uniprot on protein_id.
        "protein_interaction_uniprot": ['protein_id', 'interaction_type', 'interactor_gene', 'nb_experiments', 'description'],
        "species_master_uniprot": ['taxon_id', 'species_code', 'kingdom', 'scientific_name', 'common_name', 'synonym'],
    },
    # >>> GENERATED uniprot END <<<

    # >>> GENERATED msigdb BEGIN — DO NOT EDIT (source: dbs/msigdb/schema.yaml; run: python scripts/gen_schema.py --write --db msigdb) <<<
    "msigdb": {
        "geneset_master_table": ['geneset_id', 'geneset_name', 'collection', 'url', 'gene_count', 'geneset_organism'],
        "gene_geneset_association": ['geneset_id', 'gene_id'],
        "gene_master_table": ['gene_id', 'gene_symbol', 'gene_organism'],
        "geneset_metadata": ['geneset_id', 'historical_name', 'pmid', 'authors', 'geo_id', 'exact_source', 'external_url', 'chip', 'sub_collection', 'contributor', 'contributor_org', 'brief_description', 'full_description', 'tags'],
    },
    # >>> GENERATED msigdb END <<<

    # NOTE: The Tavily-backed "web" route is intentionally NOT registered here.
    # It is a free-text search tool (route="web" in the interpreter/orchestrator),
    # not a federated tabular source, so it does not participate in the parquet
    # planner. Keeping it out of database_schemas avoids spurious entries in the
    # FK graph, the auto-generated service scaffolds, and per-DB profile builds.
}


validate_schema(database_schemas)

primary_keys_by_db = generate_primary_keys(database_schemas)
foreign_keys_by_db = generate_foreign_keys(database_schemas)


# Decoration tables — joined as LEFT (not INNER) so missing entries do not
# drop base rows. Use for identifier-bridge / cross-ID tables where ~20% of
# entities legitimately have no entry. Example: TTD's drug_crossmatching_-
# association holds small-molecule cross-IDs (PubChem, ChEBI, CAS, formula).
# 7,017/32,660 TTD drugs (21.5%) — all biologics: monoclonal antibodies,
# CAR-T constructs, vaccines, cell therapies — have no row in this table.
# Inner-joining it silently dropped them from every query that requested
# the cross-ID output columns (bevacizumab/pembrolizumab/nivolumab → 0
# diseases, CD19 → 0 drugs in same_question_robustness 2026-05-23).
# Decoration tables are also excluded from root-table candidacy so the
# join chain starts from a canonical table (drug_master / target_master)
# and the decoration is LEFT-attached at the end.
database_decoration_tables: dict[str, set[str]] = {
    # 2026-05-23 PM (post-fix round 4): added target_uniprot_association as
    # decoration so the planner anchors on target_master_table when a query
    # filters on gene_symbol. Per the TTD manifest, target_uniprot_association
    # carries a gene_symbol "mirror" column whose per-target SET agrees with
    # target_master_table — but the cross-product row-orderings differ, so
    # picking it as root would silently re-order multi-gene complexes. Keep
    # it decoration-only: LEFT-attached after target_master is the root.
    # >>> GENERATED ttd DECOR BEGIN — DO NOT EDIT (source: dbs/ttd/schema.yaml) <<<
    "ttd": {"drug_crossmatching_association", "target_uniprot_association"},
    # >>> GENERATED ttd DECOR END <<<
}

