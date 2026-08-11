# BioChirp Qdrant Embedding Column Plan
## Summary
- DBs: 25  |  Tables: 240  |  Embed columns: 525  |  Corrections: 54

## Disagreements Resolved (Generator → Verifier)
- **[biogrid]** `identifier_crosswalk_biogrid.identifier_value` (SKIP→EMBED): DISAGREE: Generator skips this but it contains gene symbols (A1BG, ALK), official symbols, and synonyms (HYST2477, A1B). Confirmed in data: OFFICIAL SYMBOL rows hold searchable gene names, SYNONYM row
- **[chembl]** `uniprot_xwalk_chembl.target_name` (EMBED→SKIP): DISAGREE: Generator proposes embed=true with sample 'Glutathione S-transferase Mu 3'. But actual column content is CHEMBL IDs (CHEMBL2242, CHEMBL2243, CHEMBL2244) — opaque identifiers. The protein nam
- **[chembl]** `uniprot_xwalk_chembl.chembl_target_id` (SKIP→EMBED): DISAGREE: Generator omits this column from both EMBED and SKIP. Actual data shows it contains the protein names ('Glutathione S-transferase Mu 3', 'Fatty-acid amide hydrolase 1') — exactly the values 
- **[civic]** `assertion_master_table_civic.assertion_description` (SKIP→EMBED): DISAGREE: Generator omits this column from both EMBED and SKIP. Actual data shows rich long-form clinical text (e.g. 'HER2 amplification defines a clinically relevant subtype of breast cancer...'). Th
- **[drugcentral]** `structures_drugcentral.status` (EMBED→SKIP): DISAGREE. Generator marks EMBED but the manifest explicitly states these codes ('ONP', 'OFM', 'OFP') are 'undocumented in this dump and not user-facing'. Only ~240 rows have recognizable values ('Appr
- **[hcdt]** `drug_master_table_v2.drug_iupac` (EMBED→SKIP): DISAGREE. Generator marks EMBED but IUPAC systematic names like '3-[4-chloro-5-methoxy-2-(7-piperazin-1-ylimidazo[1,2-a]pyridin-2-yl)phenoxy]-N,N-dimethylpropan-1-amine' are machine-generated structur
- **[mesh]** `tree_hierarchy_mesh.mesh_term` (EMBED→SKIP): DISAGREE — should SKIP. tree_hierarchy has 64,883 rows for 30,954 unique mesh_id values (multiple rows per term, one per tree position). The same mesh_term values are already embedded in descriptor_ma
- **[msigdb]** `geneset_metadata_msigdb.collection` (EMBED→SKIP): should SKIP. Correction (2026-06-23): the earlier rationale (that this column holds opaque LEGACY CODES like C1/C2/H) was WRONG — on disk it holds the same 10 friendly names as the master (Hallmark, Curated, Ontology, …; preprocess translates every code). It is a denormalized, queryable=false mirror of geneset_master_table.collection, so it still does not need its own embeddings — filter collection via the master table.
- **[omnipath]** `annotations_omnipath.genesymbol` (EMBED→SKIP): Gene symbols (A1BG, A1CF, A2M) are short identifiers handled by expand_and_match_db, not semantic vector search. The ingest_all_db_new_fields.py explicitly omits gene symbol columns from Qdrant ingest
- **[omnipath]** `annotations_omnipath.entity_type` (EMBED→SKIP): Low-cardinality closed-vocabulary type tag (protein/complex/mirna/small_molecule ~4 values). Not useful for semantic search; users filter on this, they don't query it semantically. Should be SKIP.
- **[omnipath]** `complex_omnipath.components_genesymbols` (EMBED→SKIP): Pipe/underscore-joined gene symbol strings (NFYA_NFYB_NFYC, DEPTOR_EEF1A1_MLST8_MTOR_PRR5_RICTOR) are structured identifier blobs, not free-text queries. The ingest_all_db_new_fields.py comment at lin
- **[omnipath]** `enz_sub_table_omnipath.enzyme_genesymbol` (EMBED→SKIP): Gene symbols (LCK, SRC, FYN) are short identifiers handled by expand_and_match_db, not semantic search. The project's ingestion scripts explicitly skip gene symbol columns. Should be SKIP.
- **[omnipath]** `enz_sub_table_omnipath.substrate_genesymbol` (EMBED→SKIP): Gene symbols (SOCS3, TERT, FYB1) are short identifiers handled by expand_and_match_db, not semantic search. Should be SKIP.
- **[omnipath]** `interaction_table_omnipath.source_genesymbol` (EMBED→SKIP): Gene symbols (CALM3, CALM1, CALM2) are short identifiers handled by expand_and_match_db. The project's ingestion scripts omit gene symbol columns from Qdrant. Should be SKIP.
- **[omnipath]** `interaction_table_omnipath.target_genesymbol` (EMBED→SKIP): Gene symbols (TRPC1, HOMER1, EGFR) are short identifiers handled by expand_and_match_db. Should be SKIP.
- **[omnipath]** `interaction_table_omnipath.dorothea_level` (EMBED→SKIP): DoRothEA confidence level is a 1-letter code (A, B, C, D) — a low-cardinality ordinal classifier, not free-text biomedical vocabulary. Users filter on it, not query it semantically. Should be SKIP.
- **[omnipath]** `intercell_omnipath.genesymbol` (EMBED→SKIP): Gene symbols (ZDHHC13, SMIM9, GAPT) are short identifiers handled by expand_and_match_db, not semantic search. Should be SKIP.
- **[omnipath]** `protein_master_table_omnipath.gene_symbol` (EMBED→SKIP): Gene symbols (CALM3, CALM1, CALM2) are short identifiers handled by expand_and_match_db. The ingest_all_db_new_fields.py omits gene symbol columns from Qdrant ingestion for OmniPath. Should be SKIP.
- **[omnipath]** `tf_regulon_table_omnipath.source_genesymbol` (EMBED→SKIP): Gene symbols (MYC, SPI1, JUN_JUND) are identifiers handled by expand_and_match_db. Should be SKIP.
- **[omnipath]** `tf_regulon_table_omnipath.target_genesymbol` (EMBED→SKIP): Gene symbols (TERT, BGLAP, JUN) are identifiers handled by expand_and_match_db. Should be SKIP.
- **[omnipath]** `tf_regulon_table_omnipath.dorothea_level` (EMBED→SKIP): DoRothEA confidence level is a 1-letter code (A, B, C, D) — a low-cardinality ordinal classifier, not free-text. Users filter on it rather than query it semantically. Should be SKIP.
- **[orphanet]** `gene_disease_association_orphanet.gene_symbol` (EMBED→SKIP): Gene symbols (KIF7, CWC27, AGA) are short identifiers handled by expand_and_match_db, not semantic vector search. The ingest_all_db_new_fields.py does not embed gene symbols for Orphanet. Should be SK
- **[orphanet]** `gene_master_table_orphanet.gene_id` (EMBED→SKIP): gene_id in this table is actually the HGNC gene symbol (KIF7, CWC27, AGA) per the manifest, equivalent to gene_symbol — handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `clinical_annotation_pharmgkb.Gene` (EMBED→SKIP): Gene symbols (RGS4, SLCO1B1, UGT1A9) are short identifiers handled by expand_and_match_db. The pharmgkb ingest script embeds drug_name, not gene symbols. Should be SKIP.
- **[pharmgkb]** `clinical_annotation_pharmgkb.Level of Evidence` (EMBED→SKIP): Evidence level codes (1A, 1B, 2A) are low-cardinality ordinal identifiers, not free-text biomedical vocabulary. Users filter on these, they do not query them semantically. Should be SKIP.
- **[pharmgkb]** `clinical_annotation_pharmgkb.Specialty Population` (EMBED→SKIP): Per manifest, ~88% of values are null and the only non-null value is 'Pediatric'. This is effectively a binary flag masquerading as a column, with near-zero semantic search value. Should be SKIP.
- **[pharmgkb]** `drug_label_pharmgkb.Genes` (EMBED→SKIP): Gene symbols (CYP2C9, SMN2) and semicolon-joined gene symbol lists are identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `gene_master_table_pharmgkb.gene_name` (EMBED→SKIP): The database_loader.py renames gene_name to gene_symbol at load time (line 16: rename gene_name to gene_symbol). These are HGNC gene symbols (NUP58, IGLC1, OR4S1) handled by expand_and_match_db. Shoul
- **[pharmgkb]** `guideline_annotation_pharmgkb.genes` (EMBED→SKIP): Gene symbols (MTHFR, ABCG2, DPYD) and semicolon-joined gene lists are identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `variant_drug_annotation_pharmgkb.gene` (EMBED→SKIP): Gene symbols (CYP3A4, DPP4, FAIM2) are short identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `variant_drug_annotation_pharmgkb.significance` (EMBED→SKIP): Significance flag (yes, no, not stated) is a ternary categorical filter variable, not meaningful free-text for semantic search. Should be SKIP.
- **[pharmgkb]** `variant_drug_annotation_pharmgkb.specialty_population` (EMBED→SKIP): Per manifest, only non-null value is 'Pediatric' (~12% of rows). This is effectively a binary flag with near-zero semantic search value. Should be SKIP.
- **[pharmgkb]** `variant_drug_association_pharmgkb.evidence_level` (EMBED→SKIP): Evidence level codes (1A, 1B, 2A) are low-cardinality ordinal identifiers, not free-text biomedical vocabulary. Users filter on these, not query semantically. Should be SKIP.
- **[pharmgkb]** `variant_fa_annotation_pharmgkb.gene` (EMBED→SKIP): Gene symbols (CYP2C19, CYP2B6, CYP2C9) are identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `variant_fa_annotation_pharmgkb.significance` (EMBED→SKIP): Significance flag (yes, no, not stated) is a ternary categorical filter, not meaningful free-text for semantic search. Should be SKIP.
- **[pharmgkb]** `variant_fa_annotation_pharmgkb.specialty_population` (EMBED→SKIP): Only non-null value is 'Pediatric' — effectively a binary flag with near-zero semantic search value. Should be SKIP.
- **[pharmgkb]** `variant_fa_annotation_pharmgkb.gene_gene_product` (EMBED→SKIP): Gene/gene-product symbols (MIR2052, CYP1B1, PAH) are identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `variant_phenotype_annotation_pharmgkb.gene` (EMBED→SKIP): Gene symbols (HLA-B, CYP2A6, UGT1A1) are identifiers handled by expand_and_match_db. Should be SKIP.
- **[pharmgkb]** `variant_phenotype_annotation_pharmgkb.significance` (EMBED→SKIP): Significance flag (yes, no, not stated) is a ternary categorical filter, not free-text for semantic search. Should be SKIP.
- **[pharmgkb]** `variant_phenotype_annotation_pharmgkb.specialty_population` (EMBED→SKIP): Only non-null value is 'Pediatric' — effectively a binary flag with near-zero semantic search value. Should be SKIP.
- **[pubtator]** `cellline_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table mentions at per-PMID granularity (8M+ rows). The ingest_all_db_new_fields.py only embeds master table mentions, not association table mentions. Association tables are lazy-scanned pe
- **[pubtator]** `chemical_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table mentions at per-PMID granularity (135M+ rows). The ingest script only embeds master table mentions, not association table mentions — too large, lazy-scanned. Should be SKIP.
- **[pubtator]** `disease_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table mentions at per-PMID granularity (166M+ rows). Too large for Qdrant ingestion; lazy-scanned. Should be SKIP.
- **[pubtator]** `gene_master_table_pubtator.mentions` (EMBED→SKIP): Gene mention strings (6.7M rows) are explicitly excluded by ingest_all_db_new_fields.py line 191 comment: 'gene_master_table.mentions (6.7M rows) — gene SYMBOLS; handled by expand_and_match_db.' Shoul
- **[pubtator]** `gene_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table gene mentions at per-PMID granularity — gene symbols handled by expand_and_match_db, and association tables are lazy-scanned not loaded. Should be SKIP.
- **[pubtator]** `mutation_master_table_pubtator.mentions` (EMBED→SKIP): Mutation mention strings (E123A, R155C, H168P) are explicitly deferred by ingest_all_db_new_fields.py line 190 comment: 'mutation_master_table.mentions (4.8M rows) — deferred; usually rsID-like, less 
- **[pubtator]** `mutation_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table mutation mentions at per-PMID granularity (8M rows). Not in ingest script; lazy-scanned. Should be SKIP.
- **[pubtator]** `relation_pubtator.entity1_type` (EMBED→SKIP): Entity type (Chemical, Gene, Disease) is a low-cardinality closed-vocabulary type classifier (6 values). Users filter on it rather than querying semantically. Should be SKIP.
- **[pubtator]** `relation_pubtator.entity2_type` (EMBED→SKIP): Entity type (Disease, Chemical, Gene) is a low-cardinality closed-vocabulary type classifier. Users filter on it, not query semantically. Should be SKIP.
- **[pubtator]** `species_pubmed_association_pubtator.mentions` (EMBED→SKIP): Association table species mentions at per-PMID granularity (48M rows). Not in ingest script; lazy-scanned. Should be SKIP.
- **[reactome]** `gene_master_table_reactome.gene_name` (EMBED→SKIP): The database_loader.py renames gene_name to gene_symbol at load time (line 8). These are HGNC gene symbols (A1BG, NAT2, ADA) handled by expand_and_match_db. The ingest_all_db_new_fields.py comment at 
- **[reactome]** `pathway_master_table_reactome.species` (EMBED→SKIP): Species is always 'Homo sapiens' in this snapshot (single-value column per manifest). Embedding a constant value provides zero discriminative power for semantic search. Should be SKIP.
- **[uniprot]** `gene_ontology_uniprot.evidence` (EMBED→SKIP): Values are GO evidence code abbreviations: 'IBA', 'IPI', 'IEA', 'IDA', 'TAS', etc. These are opaque acronyms that general users would not type. Users say 'experimental evidence' not 'IDA'. The column 
- **[uniprot]** `variant_disease_uniprot.variant_type` (EMBED→SKIP): Values are ACMG pathogenicity abbreviations: 'LP/P' (Likely Pathogenic/Pathogenic), 'LB/B' (Likely Benign/Benign), 'US' (Uncertain Significance). These are opaque shorthand codes users do not type — u

## Per-DB Embedding Plan

### BIOGRID (6 tables, 25 cols)

**`chemical_interaction_biogrid`**
- `gene_symbol` → `NISCH`, `IDH3G`, `CTSS`, `BCR`
  *Human-readable gene symbol used for semantic lookup of gene-chemical interactions.*
- `action` → `inhibitor`, `activator`, `agonist`, `suppressor`
  *Categorical biomedical action label describing the chemical's effect on the gene target.*
- `interaction_type` → `target`, `recruited deubiquitinase`, `autophagy-targeting protein`, `recruited E3 ligase`
  *Categorical biomedical label describing the type of gene-chemical interaction.*
- `chemical_name` → `Lepirudin`, `Cetuximab`, `Dyclonine`, `Triazolam`
  *Human-readable drug/chemical name, primary lookup field for chemical entities.*
- `chemical_synonyms` → `Lepirudin recombinant|Hirudin variant-1`, `Ctuximab|Cetuximab|Cetuximabum`, `5-(2-Chlorophenyl)-7-nitro-1H-benzo[e][1,4]diazepin-2-one|CLONAZEPAM`, `BDBM50144214`
  *Pipe-delimited synonyms and aliases of the chemical, valuable for synonym-based semantic search.*
- `chemical_type` → `small molecule`, `biologic`, `antibody`, `polypeptidic`
  *Categorical label for the type of chemical entity with biomedical meaning.*
- `method` → `PROTAC`, `Molecular Glue`, `LYTAC`, `AbTAC`
  *Categorical biomedical method name describing the targeting strategy used.*

**`gene_master_table_biogrid`**
- `gene_symbol` → `MAP2K4`, `MYPN`, `ACVR1`, `GATA2`
  *Human-readable gene symbol, primary lookup field for gene entities in BioGRID.*

**`identifier_crosswalk_biogrid`**
- `identifier_value` ⚠️CORRECTED → `NP_001032897`, `NM_001198764`, `Q9TQK3`, `P78539-2`
  *CORRECTED by verifier: DISAGREE: Generator skips this but it contains gene symbols (A1BG, ALK), offi*
- `identifier_type` → `BIOGRID`, `OFFICIAL SYMBOL`, `SYNONYM`, `ENSEMBL RNA`
  *Categorical label naming the type of external identifier, useful for filtering by identifier system.*
- `organism_official_name` → `Homo sapiens`
  *Human-readable organism name, useful for species-level semantic filtering.*

**`ppi_association_biogrid`**
- `gene_a` → `MAP2K4`, `MYPN`, `ACVR1`, `GATA2`
  *Human-readable gene symbol for the first interactor in the protein-protein interaction.*
- `gene_b` → `FLNC`, `ACTN2`, `FNTA`, `PML`
  *Human-readable gene symbol for the second interactor in the protein-protein interaction.*
- `experiment_type` → `Two-hybrid`, `Affinity Capture-MS`, `Affinity Capture-Luminescence`, `Co-crystal Structure`
  *Human-readable experimental method label with biomedical meaning for retrieval.*
- `interaction_class` → `physical`, `genetic`
  *Short categorical label distinguishing physical vs genetic interactions.*
- `throughput` → `Low Throughput`, `High Throughput`, `High Throughput|Low Throughput`
  *Categorical evidence throughput label with biomedical meaning.*

**`ptm_association_biogrid`**
- `gene_symbol` → `YWHAG`, `CDKN1C`, `RNF139`, `STRADA`
  *Human-readable gene symbol for the PTM substrate gene.*
- `ptm_type` → `Phosphorylation`, `Ubiquitination`, `Sumoylation`, `Neddylation`
  *Human-readable post-translational modification type label with biomedical meaning.*
- `source` → `BIOGRID`, `BIOGRID: UbiGRID Project`
  *Source database label that identifies the curation provenance and has meaningful categorical distinc*

**`ptm_relationships_biogrid`**
- `official_symbol` → `MEC1`, `TEL1`, `PTK1`, `VHS1`
  *Human-readable gene/protein symbol for the PTM modifier enzyme.*
- `synonymns` → `ESR1|RAD31|SAD3|protein kinase MEC1`, `DNA-binding protein kinase TEL1|L000002281`, `protein kinase FRK1`, `casein kinase 2 regulatory subunit CKB2|L000000345`
  *Pipe-delimited gene aliases and descriptions useful for synonym-based semantic retrieval.*
- `relationship` → `kinase`, `phosphatase`, `-`
  *Categorical label describing the enzymatic relationship of the modifier to the PTM.*
- `identity` → `catalytic`, `regulatory`, `PTM`
  *Categorical label describing the functional role identity in the PTM relationship.*
- `organism_name` → `Homo sapiens`, `Mus musculus`, `Saccharomyces cerevisiae (S288c)`, `Schizosaccharomyces pombe (972h)`
  *Human-readable organism name, useful for species-level semantic filtering.*
- `source_database` → `PhosphoGRID`, `BIOGRID`
  *Categorical source database label with distinct meaningful values for filtering.*

### CHEBI (4 tables, 7 cols)

**`chemical_master_table_chebi`**
- `chemical_name` → `(+)-Atherospermoline`, `(-)-medicarpin`, `Vismione D`, `ribostamycin sulfate`
  *Human-readable chemical name used as the primary label for semantic lookup.*
- `definition` → `The (−)-enantiomer of medicarpin.`, `A furanochromone that is furo[3,2-g]chromen-5-one substituted at positions 4 and 7 by methoxy and methyl groups.`, `An aminoglycoside sulfate salt resulting from the reaction of ribostamycin with sulfuric acid.`, `A monocarboxylic acid comprising 1,8-naphthyridin-4-one used in treatment of urinary-tract infections.`
  *Short free-text definition describing the chemical's structure, class, and biomedical context.*

**`chemical_synonyms_chebi`**
- `name` → `(+)-3-Carene`, `(S)-(+)-3-carene`, `BRAND NAME example`, `Withaferin A`
  *Synonym or alias name (INN, brand name, IUPAC name, common synonym) enabling synonym-based semantic *
- `type` → `SYNONYM`, `IUPAC NAME`, `INN`, `BRAND NAME`
  *Categorical label with biomedical meaning distinguishing synonym types (INN, brand name, IUPAC, etc.*

**`chemical_origin_chebi`**
- `species_text` → `Homo sapiens`, `Mus musculus`, `Rubia yunnanensis`, `Withania somnifera`
  *Human-readable organism name enabling semantic search by biological source species.*
- `component_text` → `urine`, `root`, `stem`, `Urine`
  *Human-readable anatomical/tissue source label (e.g. root, urine, stem) useful for biomedical semanti*

**`compound_structure_chebi`**
- `iupac_name` → `1alpha,6alpha-car-3-ene`, `7betaH-cadina-1,3,5-trien-2-ol`, `(1R,4S)-2,2-dimethyl-3-methylidenebicyclo[2.2.1]heptane`, `(3R)-3,7-dimethylocta-1,6-dien-3-ol`
  *Human-readable IUPAC systematic name useful for chemical name-based semantic retrieval.*

### CHEMBL (44 tables, 96 cols)

**`activity_properties_chembl`**
- `type` → `DATASET`, `ACTIVITY_TEST`, `TISSUE`, `ROUTE`
  *Human-readable property type labels like DATASET, TISSUE, ROUTE, DOSE — useful for semantic search.*
- `text_value` → `Hematology`, `RBC (Erythrocytes)`, `Blood`, `Gavage`
  *Short free-text categorical values describing assay context.*
- `standard_type` → `IC50`, `EC50`, `GI50`, `Ki`
  *Standardized measurement type labels like IC50, EC50, Ki with biomedical meaning.*

**`activity_stds_lookup_chembl`**
- `standard_type` → `CC50`, `EC50`, `GI50`, `IC50`
  *Standardized bioactivity measurement type names (CC50, EC50, IC50, Ki) with biomedical meaning.*
- `definition` → `Concentration required for 50% cytotoxicity`, `Effective concentration for 50% response`, `Concentration required for 50% growth inhibition`, `Inhibition constant`
  *Short free-text definitions describing what each measurement type means.*

**`assay_classification_chembl`**
- `l1` → `ALIMENTARY TRACT AND METABOLISM`, `CARDIOVASCULAR SYSTEM`, `NERVOUS SYSTEM`, `ANTINEOPLASTIC AND IMMUNOMODULATING AGENTS`
  *Top-level assay classification therapeutic area name.*
- `l2` → `Anti-Obesity Activity`, `Experimental Diabetes Mellitus`, `Analgesic Activity`, `Antihypertensive Activity`
  *Second-level assay classification category name.*
- `l3` → `Computer-Assisted Measurement`, `Food Consumption in Rats Anorexic Models`, `General Anti-Obesity activity`, `Alloxan Induced Diabetes`
  *Third-level assay classification specific assay name — most specific human-readable category.*
- `class_type` → `In vivo efficacy`, `In vitro efficacy`, `In vivo toxicity`
  *Categorical label describing experiment class with biomedical meaning.*

**`assay_master_table_chembl`**
- `description` → `Binding affinity against A2 adenosine receptor`, `In vitro cell cytotoxicity against tumor cells`, `Cytotoxic Activity was evaluated in cancer cell lines`, `Inhibitory activity against tumor cells`
  *Free-text assay description — the main human-readable content for semantic search.*
- `assay_type` → `B`, `F`, `A`, `T`
  *Short categorical code for assay type (B=Binding, F=Functional) with biomedical meaning.*

**`assay_parameters_chembl`**
- `type` → `assay_method`, `data_collection_rate`, `fitting_model`, `fitting_parameter_offset`
  *Parameter type label describing the assay condition — semantically useful.*
- `text_value` → `SPR`, `1:1 + mass transport`, `constant`, `global`
  *Short free-text value describing the parameter setting.*

**`atc_classification_chembl`**
- `who_name` → `fluoride`, `olaflur`, `sodium monofluorophosphate`, `sodium fluoride`
  *WHO INN drug name — human-readable drug name for semantic search.*
- `level1_description` → `ALIMENTARY TRACT AND METABOLISM`, `BLOOD AND BLOOD FORMING ORGANS`, `CARDIOVASCULAR SYSTEM`, `DERMATOLOGICALS`
  *Top-level ATC therapeutic area description.*
- `level2_description` → `STOMATOLOGICAL PREPARATIONS`, `ANTITHROMBOTIC AGENTS`, `ANTIHYPERTENSIVES`, `EMOLLIENTS AND PROTECTIVES`
  *Second-level ATC pharmacological category description.*
- `level3_description` → `STOMATOLOGICAL PREPARATIONS`, `ANTIINFECTIVES AND ANTISEPTICS FOR LOCAL ORAL TREATMENT`, `CARIES PROPHYLACTIC AGENTS`
  *Third-level ATC chemical/pharmacological group description.*
- `level4_description` → `Caries prophylactic agents`, `Antiinfectives and antiseptics for local oral treatment`, `BCR-ABL tyrosine kinase inhibitors`, `Phosphatidylinositol-3-kinase inhibitors`
  *Most specific ATC subgroup description.*

**`binding_sites_chembl`**
- `site_name` → `UDP-glucuronosyltransferase 1-1, Glucuronosyl transferase domain`, `Mitogen-activated protein kinase 14, Protein kinase domain`, `Inosine-5'-monophosphate dehydrogenase 2`, `Dopamine D1 receptor, 7tm_1 domain`
  *Human-readable name of the protein binding site — key for semantic search of binding/structural quer*

**`bioactivity_table_chembl`**
- `standard_type` → `IC50`, `EC50`, `Ki`, `GI50`
  *Standardized bioactivity measurement type (IC50, EC50, Ki) — human-readable enum.*

**`bio_component_sequences_chembl`**
- `description` → `Disulfide bridges`, `Light chain`, `Heavy chain`, `Sequence`
  *Short human-readable description of the biotherapeutic component.*
- `component_type` → `Protein`, `DNA`, `RNA`
  *Categorical type label for the biologic component.*
- `organism` → `Homo sapiens`, `Mus musculus`, `Rattus norvegicus`
  *Human-readable organism name.*

**`biotherapeutics_chembl`**
- `description` → `monoclonal antibody`, `fusion protein`, `peptide hormone`
  *Short free-text description of the biotherapeutic — when not null.*

**`cell_dictionary_chembl`**
- `cell_name` → `DC3F`, `P3HR-1`, `UCLA P-3`, `UMSCC22B`
  *Cell line name — key human-readable identifier for semantic search.*
- `cell_description` → `DC3F`, `P3HR-1`, `UCLA P-3`, `UMSCC22B`
  *Short description of the cell line.*
- `cell_source_tissue` → `Lung`, `Lyphoma`, `Lung Adenocarcinoma`, `Carcinoma`
  *Human-readable tissue of origin for the cell line.*
- `cell_source_organism` → `Homo sapiens`, `Mus musculus`, `Cricetulus griseus`
  *Organism name from which the cell line was derived.*

**`chembl_id_lookup_chembl`**
- `entity_type` → `COMPOUND`, `ASSAY`, `TARGET`, `DOCUMENT`
  *Categorical label for entity type — useful for filtering by type semantically.*

**`compound_records_chembl`**
- `compound_name` → `Compound X`, `Compound V`, `Compound IX`, `Aspirin`
  *Human-readable compound name as recorded in the source — primary search target.*

**`compound_structural_alerts_chembl`**
- `alert_name` → `R1 Reactive alkyl halides`, `R2 Acid halides`, `R3 Carbazides`, `R4 Sulphate esters`
  *Human-readable structural alert name describing the toxicophore.*
- `alert_set_name` → `Glaxo`, `Dundee`, `BMS`, `PAINS`
  *Name of the alert set from which the alert originates.*

**`confidence_score_lookup_chembl`**
- `description` → `Default value - Target unknown or has yet to be assigned`, `Target assigned is non-molecular target type`, `Multiple homologous protein targets assigned`, `Direct protein complex subunit assigned`
  *Full text description of the confidence score — explains target assignment quality.*
- `target_mapping` → `Unassigned`, `Non-molecular`, `Subcellular fraction`, `Molecular (non-protein)`
  *Short human-readable target mapping label — categorical with biomedical meaning.*

**`data_validity_lookup_chembl`**
- `data_validity_comment` → `Author confirmed error`, `Manually validated`, `Non standard unit for type`, `Outside typical range`
  *Short human-readable label for data validity category.*
- `description` → `Error in publication - Author confirmed`, `Data have been checked against the original publication`, `Units for this activity type are non-standard`, `Values for this activity type appear to be outside the typical range`
  *Longer explanation of the data validity comment.*

**`docs_chembl`**
- `title` → `Ezetimibe: a review of its use in the management of hypercholesterolaemia`, `Structure-activity relationships of adenosine A1 receptor ligands`, `Novel kinase inhibitors for cancer treatment`
  *Publication title — most semantically rich field for literature search.*
- `journal` → `J Med Chem`, `Eur J Med Chem`, `Bioorg Med Chem Lett`, `Nature`
  *Journal name — useful for source filtering.*
- `doc_type` → `PUBLICATION`, `PATENT`, `BOOK`, `DATASET`
  *Type of document (PUBLICATION, PATENT, etc.) with categorical meaning.*

**`domains_chembl`**
- `domain_name` → `7tm_1`, `7tm_2`, `7tm_3`, `AAA`
  *Human-readable domain name (Pfam family abbreviation) — useful for protein domain semantic search.*
- `domain_type` → `Pfam-A`, `Pfam-B`, `MEROPS`
  *Domain database type label.*

**`drug_atc_association_chembl`**
- `atc_who_name` → `asciminib`, `pexidartinib`, `midostaurin`, `erdafitinib`
  *WHO INN drug name — the primary human-readable drug name for semantic lookup.*
- `atc_level1_description` → `ANTINEOPLASTIC AND IMMUNOMODULATING AGENTS`, `DERMATOLOGICALS`, `CARDIOVASCULAR SYSTEM`, `ALIMENTARY TRACT AND METABOLISM`
  *Top-level ATC therapeutic category description.*
- `atc_level4_description` → `BCR-ABL tyrosine kinase inhibitors`, `Other protein kinase inhibitors`, `Fibroblast growth factor receptor inhibitors`, `Mitogen-activated protein kinase inhibitors`
  *Most specific ATC subgroup description — drug mechanism class.*

**`drug_indication_association_chembl`**
- `mesh_heading` → `Scleroderma, Diffuse`, `Arthritis, Rheumatoid`, `Myocardial Infarction`, `Prostatic Neoplasms`
  *MeSH disease term — human-readable indication name for semantic search.*
- `efo_term` → `diffuse scleroderma`, `rheumatoid arthritis`, `myocardial infarction`, `prostate carcinoma`
  *EFO disease term — human-readable synonym for the indication.*

**`drug_mechanism_table_chembl`**
- `mechanism_of_action` → `Carbonic anhydrase VII inhibitor`, `Carbonic anhydrase I inhibitor`, `Cytochrome b inhibitor`, `Muscarinic acetylcholine receptor M2 antagonist`
  *Full text description of drug mechanism — the primary human-readable MoA string for semantic search.*
- `action_type` → `INHIBITOR`, `ANTAGONIST`, `AGONIST`, `ACTIVATOR`
  *Action type categorical label with biomedical meaning.*
- `target_name` → `Carbonic anhydrase 7`, `Carbonic anhydrase 1`, `Cytochrome b`, `Muscarinic acetylcholine receptor M2`
  *Human-readable target protein name.*

**`drug_metadata_master_table_chembl`**
- `drug_name` → `aspirin`, `imatinib`, `bevacizumab`, `trastuzumab`
  *Human-readable drug name — primary semantic search field.*
- `molecule_type` → `Small molecule`, `Antibody`, `Protein`, `Oligonucleotide`
  *Categorical molecular type label with biomedical meaning.*
- `structure_type` → `MOL`, `SEQ`, `NONE`, `BOTH`
  *Structure type label indicating compound representation.*
- `usan_stem` → `-mab`, `-nib`, `-stat`, `-lukast`
  *USAN stem indicating drug class by naming convention.*

**`drug_synonym_association_chembl`**
- `synonym` → `Ro-481220`, `Ro-151310`, `Ro-147437`, `Ro-194603`
  *Drug synonym or alias — key for synonym-based semantic search.*
- `syn_type` → `RESEARCH_CODE`, `INN`, `USAN`, `BAN`
  *Synonym type label classifying the synonym class.*

**`drug_warning_table_chembl`**
- `warning_type` → `Black Box Warning`, `Withdrawn`, `Restricted`
  *Type of drug warning — categorical biomedical label.*
- `warning_class` → `hepatotoxicity`, `metabolic toxicity`, `immune system toxicity`, `carcinogenicity`
  *Drug warning class describing the type of toxicity concern.*
- `warning_description` → `Increased risk of serious cardiovascular events`, `Associated with severe liver damage`
  *Free text description of the warning when available.*
- `warning_efo_term` → `hepatocellular carcinoma`, `drug-induced liver injury`, `cardiotoxicity`
  *EFO disease term linked to the warning.*

**`formulations_chembl`**
- `ingredient` → `TAZAROTENE`, `MINOXIDIL`, `CALCIPOTRIENE`, `FLUTICASONE PROPIONATE`
  *Drug ingredient name — human-readable drug name in formulation context.*

**`indication_refs_chembl`**
- `mesh_heading` → `Hyperkalemia`, `Pulmonary Disease, Chronic Obstructive`, `Emphysema`, `Tobacco Use Disorder`
  *MeSH disease heading — human-readable indication name.*
- `efo_term` → `Hyperkalemia`, `chronic obstructive pulmonary disease`, `emphysema`, `nicotine dependence`
  *EFO term — human-readable disease synonym.*

**`mechanism_refs_chembl`**
- `mechanism_of_action` → `Carbonic anhydrase VII inhibitor`, `Carbonic anhydrase I inhibitor`, `Muscarinic acetylcholine receptor M2 antagonist`
  *Full text mechanism of action string — useful for semantic MoA lookup.*
- `action_type` → `INHIBITOR`, `ANTAGONIST`, `AGONIST`, `ACTIVATOR`
  *Action type categorical label.*

**`metabolism_chembl`**
- `enzyme_name` → `CYP3A4`, `CYP2D6`, `CYP1A2`, `UGT1A1`
  *Human-readable enzyme name responsible for metabolism.*
- `met_conversion` → `O-dealkylation`, `N-dealkylation`, `hydroxylation`, `glucuronidation`
  *Short label describing the metabolic transformation.*
- `organism` → `Homo sapiens`, `Rattus norvegicus`, `Mus musculus`
  *Human-readable organism name for the metabolism experiment.*

**`organism_class_chembl`**
- `l1` → `Eukaryotes`, `Bacteria`, `Fungi`, `Viruses`
  *Top-level organism kingdom classification name.*
- `l2` → `Mammalia`, `Gram-Negative`, `Ascomycota`, `Kinetoplastida`
  *Second-level organism class name.*
- `l3` → `Rodentia`, `Primates`, `Acinetobacter`, `Saccharomycetales`
  *Third-level organism order/genus name.*

**`protein_classification_full_chembl`**
- `pref_name` → `Protein class`, `Enzyme`, `Adhesion`, `Secreted protein`
  *Preferred human-readable protein class name.*
- `short_name` → `Protein class`, `Enzyme`, `Adhesion`, `Secreted`
  *Short human-readable protein class label.*
- `definition` → `Root of the ChEMBL protein family classification`, `Biological molecules that possess catalytic activity`, `Surface ligands, usually glycoproteins`, `A rather large group of enzymes`
  *Free-text definition of the protein class.*

**`protein_class_synonyms_chembl`**
- `protein_class_synonym` → `Enzyme`, `Enzymes`, `Biocatalysts`, `Oxidoreductases`
  *Synonym for the protein class — includes INN names, UMLS terms, useful for semantic search.*

**`relationship_type_chembl`**
- `relationship_desc` → `Direct protein target assigned`, `Homologous protein target assigned`, `Molecular target other than protein`, `Non-molecular target assigned`
  *Human-readable description of the relationship type between assay and target.*

**`source_chembl`**
- `src_description` → `Undefined`, `Scientific Literature`, `GSK Malaria Screening`, `Novartis Malaria Screening`
  *Full description of the data source — human-readable name for semantic search.*
- `src_short_name` → `UNDEFINED`, `LITERATURE`, `GSK_TCMDC`, `NOVARTIS`
  *Short name for the data source — human-readable abbreviation.*

**`structural_alerts_chembl`**
- `alert_name` → `R1 Reactive alkyl halides`, `R2 Acid halides`, `R3 Carbazides`, `R4 Sulphate esters`
  *Human-readable structural alert name describing the toxic substructure.*

**`structural_alert_sets_chembl`**
- `set_name` → `Glaxo`, `Dundee`, `BMS`, `PAINS`
  *Human-readable name of the structural alert filter set.*

**`target_master_table_chembl`**
- `pref_name` → `Maltase-glucoamylase`, `ATP-binding cassette sub-family C member 9`, `cGMP-specific 3',5'-cyclic phosphodiesterase`, `Voltage-dependent T-type calcium channel alpha-1H subunit`
  *Preferred human-readable target protein name — primary field for target semantic search.*
- `target_type` → `SINGLE PROTEIN`, `PROTEIN COMPLEX`, `PROTEIN FAMILY`, `ORGANISM`
  *Target type categorical label with biomedical meaning.*
- `organism` → `Homo sapiens`, `Rattus norvegicus`, `Mus musculus`, `Plasmodium falciparum`
  *Human-readable organism name.*

**`target_protein_class_association_chembl`**
- `protein_class_name` → `Hydrolase`, `ABCC subfamily`, `Phosphodiesterase 5A`, `Voltage-gated calcium channel`
  *Human-readable protein class name associated with the target.*

**`target_relations_chembl`**
- `relationship` → `SUBSET OF`, `SUPERSET OF`, `EQUIVALENT TO`
  *Human-readable relationship type label between targets.*

**`target_sequence_association_chembl`**
- `component_description` → `Maltase-glucoamylase`, `ATP-binding cassette sub-family C member 9`, `cGMP-specific 3',5'-cyclic phosphodiesterase`, `Dihydrofolate reductase`
  *Human-readable description of the protein component.*
- `component_organism` → `Homo sapiens`, `Rattus norvegicus`, `Mus musculus`
  *Human-readable organism name.*

**`target_type_chembl`**
- `target_type` → `3D CELL CULTURE`, `ADMET`, `CELL-LINE`, `CHIMERIC PROTEIN`
  *Human-readable target type label — categorical with biomedical meaning.*
- `target_desc` → `Target is a 3D cell culture model`, `Target is not applicable for activity measurements`, `Target is a specific cell-line`, `Target is a fusion of two different proteins`
  *Full text description of the target type.*

**`tissue_dictionary_chembl`**
- `pref_name` → `Uterine cervix`, `Nose`, `Islets of langerhans`, `Pituitary gland`
  *Human-readable tissue name — primary field for tissue semantic search.*

**`uniprot_xwalk_chembl`**
- `target_type` → `SINGLE PROTEIN`, `PROTEIN COMPLEX`, `PROTEIN FAMILY`
  *Target type categorical label.*
- `chembl_target_id` ⚠️CORRECTED → `CHEMBL2242`, `CHEMBL2243`, `CHEMBL2244`
  *CORRECTED by verifier: DISAGREE: Generator omits this column from both EMBED and SKIP. Actual data s*

**`usan_stems_chembl`**
- `stem` → `-ac`, `-actant`, `-adenant`, `-adol`
  *USAN stem suffix/prefix — meaningful drug class identifier.*
- `annotation` → `anti-inflammatory agents (acetic acid derivatives)`, `pulmonary surfactants`, `adenosine receptor antagonists`, `analgesics (mixed opiate receptor agonist/antagonists)`
  *Full text annotation describing what drug class the stem represents.*
- `subgroup` → `-zolac`, `-tolac`, `-profac`
  *Subgroup label for the stem — further classifies the drug class.*

**`warning_refs_chembl`**
- `warning_type` → `Black Box Warning`, `Withdrawn`, `Restricted`
  *Type of drug warning categorical label.*
- `warning_class` → `hepatotoxicity`, `metabolic toxicity`, `immune system toxicity`, `carcinogenicity`
  *Warning class describing the toxicity type.*

### CIVIC (9 tables, 30 cols)

**`assertion_master_table_civic`**
- `molecular_profile` → `ERBB2 Amplification`, `v::ALK Fusion`, `EGFR L858R`, `BRAF V600E`
  *Human-readable gene variant/fusion profile names used for biomedical semantic search.*
- `disease` → `Her2-receptor Positive Breast Cancer`, `Lung Non-small Cell Carcinoma`, `Von Hippel-Lindau Disease`, `Melanoma`
  *Human-readable disease names useful for disease-centric semantic queries.*
- `therapies` → `Alectinib`, `Erlotinib`, `Trastuzumab`, `Trametinib,Dabrafenib`
  *Drug/therapy names that enable semantic drug-centric queries.*
- `assertion_type` → `Predictive`, `Prognostic`, `Diagnostic`, `Predisposing`
  *Categorical biomedical label describing the type of clinical assertion.*
- `significance` → `Positive`, `Sensitivity/Response`, `Oncogenic`, `Likely Oncogenic`
  *Clinical significance labels with biomedical meaning useful for semantic filtering.*
- `amp_category` → `Tier I - Level A`, `Tier II - Level C`
  *AMP/ASCO/CAP tier classification with clinical significance for variant interpretation queries.*
- `nccn_guideline` → `Melanoma`, `Acute Myeloid Leukemia`, `Non-Small Cell Lung Cancer`, `Breast Cancer`
  *Human-readable NCCN cancer disease guideline names useful for guideline-based queries.*
- `assertion_summary` → `EWSR1::WT1 is diagnostic for desmoplastic small round cell tumor`, `BCR::ABL1 fusion is a diagnostic criterion for Chronic Myeloid Leukemia (CML).`, `EML4::NTRK3 is classified as an oncogenic NTRK fusion.`, `HRAS G13R (NM_005343.4:c.37G>C) is oncogenic`
  *Short free-text clinical summary of the assertion, highly informative for semantic search.*
- `phenotypes` → `Juvenile onset,Young adult onset`, `Adult onset,Acute myeloid leukemia,Myelodysplasia`, `Renal cyst,Abnormality of the pancreas,Renal cell carcinoma,Clear cell renal cell carcinoma`, `Pediatric onset,Juvenile onset,Adult onset`
  *Human-readable HPO phenotype terms useful for phenotype-based semantic queries.*

**`disease_master_table_civic`**
- `disease_name` → `Lymphoid Leukemia`, `Gastrointestinal Stromal Tumor`, `Acute Myeloid Leukemia`, `Chronic Myeloid Leukemia`
  *Human-readable disease name; primary semantic search target for disease queries.*

**`drug_master_table_civic`**
- `drug_name` → `4-pyrimidinediamine`, `7-Ethyl-10-Hydroxycamptothecin`, `9F7-F11`, `A66`
  *Human-readable drug/therapy name; essential for drug-based semantic queries.*

**`feature_master_table_civic`**
- `name` → `ALK`, `AKT1`, `ARAF`, `ABL1`
  *Gene symbol or fusion name; primary human-readable identifier for gene/fusion features.*
- `feature_aliases` → `FKH1,FKHR,FOXO1A,FOXO1`, `ECYT4,HIF2A,HLF,MOP2,PASD2,bHLHe73,EPAS1`, `LKB1,PJS,hLKB1,STK11`, `dMMR, Microsatellite Instability`
  *Human-readable gene aliases and synonyms enabling alias-based semantic queries.*
- `description` → `AKT1, also referred to as protein kinase B, is a known oncogene. AKT activation relies on the PI3K pathway...`, `BRCA2 mutations in the germline have become a hallmark for hereditary breast and ovarian cancers...`, `Tumor mutational burden (TMB) refers to the total number of genomic alterations in a tumor cell...`, `ERCC2 functions as a DNA repair gene involved in separating the double helix via 5-3 helicase activity...`
  *Free-text biological description of the gene/feature; rich content for semantic retrieval.*
- `feature_type` → `Gene`, `Fusion`, `Factor`, `Region`
  *Categorical biomedical label describing the feature type (Gene, Fusion, Factor, Region).*
- `five_prime_gene_name` → `SLC4A4`, `CBFA2T3`, `TRPS1`, `MEF2D`
  *Gene symbol of the 5-prime fusion partner; human-readable and semantically meaningful.*
- `three_prime_gene_name` → `EGFR`, `PBX1`, `TACC3`, `TFCP2`
  *Gene symbol of the 3-prime fusion partner; human-readable and semantically meaningful.*

**`gene_master_table_civic`**
- `gene_name` → `ALK`, `AKT1`, `ARAF`, `ABL1`
  *Human-readable gene symbol; primary target for gene-centric semantic queries.*

**`molecular_profile_master_table_civic`**
- `name` → `BCR::ABL1 Fusion`, `AKT1 E17K`, `EML4::ALK Fusion`, `ALK F1174L`
  *Human-readable molecular profile name (gene variant or fusion); primary semantic search target.*
- `summary` → `The BCR-ABL fusion protein, commonly seen in CML, is the product of a translocation...`, `AKT1 E17K is a recurrent mutation that results in activation of the protein...`, `The EML4-ALK fusion variant 1 is seen in non-small cell lung cancer...`, `BRAF V600E has been shown to be a recurrent mutation in multiple cancer types...`
  *Free-text biological summary of the molecular profile variant; rich content for semantic retrieval.*
- `aliases` → `BCR-ABL, BCR-ABL1, T(9;22)(Q34;Q11)`, `GLU17LYS, RS34409589`, `EML4-ALK`, `PHE1174LEU, RS863225281`
  *Human-readable molecular profile aliases and synonyms enabling alias-based semantic lookup.*

**`variant_evidence_association_civic`**
- `drug_name` → `Amivantamab,Chemotherapy`, `Imatinib,Regorafenib Anhydrous,Sunitinib,Ponatinib`, `Tretinoin`, `Paclitaxel,Carboplatin`
  *Human-readable drug name associated with variant evidence; useful for drug-variant queries.*
- `evidence_type` → `Predictive`, `Diagnostic`, `Prognostic`, `Functional`
  *Categorical biomedical label describing the type of clinical evidence.*
- `evidence_direction` → `Supports`, `Does Not Support`
  *Categorical label indicating direction of evidence (Supports / Does Not Support).*
- `evidence_level` → `A`, `B`, `C`, `D`
  *Evidence strength tier (A-E) with clear biomedical meaning for evidence quality queries.*
- `significance` → `Predisposition`, `Oncogenicity`, `Negative`, `Dominant Negative`
  *Clinical significance label relevant to therapeutic or biological interpretation.*

**`variant_group_master_table_civic`**
- `variant_group` → `Imatinib Resistance`, `KIT Exon 17`, `ALK Crizotinib Resistance`, `KIT Exon 11`
  *Human-readable variant group name describing a functionally related set of variants.*
- `description` → `While imatinib has shown to be effective in treating CML...`, `The ALK oncogene has long been associated with resistance mutations to crizotinib...`, `While BRAF V600E is nearly ubiquitous, other BRAF V600 variants also occur...`, `RET activation is a common oncogenic driver...`
  *Free-text description of the variant group with biological context; rich for semantic retrieval.*

**`variant_master_table_civic`**
- `variant_name` → `T(9;22)(Q34;Q11),BCR-ABL1,BCR-ABL`, `THR334ILE,RS121913459`, `E274K,RS121913448`, `GLU17LYS,RS34409589`
  *Human-readable variant name including HGVS-like notation and aliases; key for variant queries.*
- `feature_id` → `BCR::ABL1`, `ABL1`, `AKT1`, `EML4::ALK`
  *Contains gene symbol / fusion name (e.g., BCR::ABL1, ALK) in this table, not a numeric ID.*

### CLINVAR (5 tables, 21 cols)

**`gene_master_table_clinvar`**
- `gene_name` → `AP5Z1`, `ZNF592`, `FOXRED1`, `NUBPL`
  *Human-readable HGNC gene symbols used for semantic lookup by gene name.*

**`variant_disease_association_clinvar`**
- `disease_name` → `Hereditary spastic paraplegia`, `Galloway-Mowat syndrome 1`, `Leigh syndrome`, `Noonan syndrome 5`
  *Free-text disease names used for semantic disease lookup.*
- `clinical_significance` → `Pathogenic`, `Likely pathogenic`, `Uncertain significance`, `Benign`
  *Categorical clinical pathogenicity labels with biomedical meaning, important for filtering by clinic*

**`variant_genomic_coords_clinvar`**
- `clnsig` → `Pathogenic`, `Uncertain_significance`, `Likely_benign`, `Benign`
  *VCF-format clinical significance enum strings with biomedical meaning for pathogenicity queries.*
- `clndn` → `Leigh_syndrome`, `Familial_thoracic_aortic_aneurysm_and_aortic_dissection|Loeys-Dietz_syndrome_2`, `Citrullinemia|Citrullinemia_type_I`, `Cardiac_arrhythmia|Long_QT_syndrome_3`
  *Disease names associated with variants — free-text disease identifiers valuable for semantic disease*
- `molecular_consequence` → `SO:0001583|missense_variant`, `SO:0001587|nonsense`, `SO:0001627|intron_variant`, `SO:0001589|frameshift_variant`
  *SO term pipe-delimited labels like missense_variant, frameshift_variant — biomedical categorical ann*
- `variant_class` → `Deletion`, `Indel`, `single_nucleotide_variant`, `Duplication`
  *Human-readable variant type categories useful for semantic search by variant class.*

**`variant_master_table_clinvar`**
- `variant_name` → `NM_014855.3(AP5Z1):c.80_83del`, `NM_000410.4(HFE):c.845G>A (p.Cys282Tyr)`, `NM_017547.4(FOXRED1):c.694C>T`, `NM_000314.8(PTEN):c.543G>C (p.Leu181=)`
  *HGVS notation variant name combining transcript, gene, and amino-acid change — human-readable and us*
- `gene_symbol` → `AP5Z1`, `ZNF592`, `FOXRED1`, `NUBPL`
  *HGNC gene symbol — standard human-readable gene name for semantic gene lookup.*
- `variant_type` → `Indel`, `Deletion`, `single nucleotide variant`, `Duplication`
  *Human-readable variant type categories useful for filtering and semantic search.*
- `clinical_significance` → `Pathogenic`, `Likely pathogenic`, `Uncertain significance`, `Benign`
  *Pathogenicity classification labels with strong biomedical meaning for clinical variant queries.*
- `review_status` → `criteria provided, multiple submitters, no conflicts`, `reviewed by expert panel`, `no assertion criteria provided`, `criteria provided, single submitter`
  *Evidence review level labels (e.g. reviewed by expert panel) relevant for filtering by evidence qual*

**`variant_submission_clinvar`**
- `clinical_significance` → `Pathogenic`, `Likely pathogenic`, `Uncertain significance`, `Likely benign`
  *Submitter-reported pathogenicity classification — biomedical categorical label.*
- `description` → `This variant is expected to result in loss of function`, `This change likely results in a truncated protein`, `Variant summary: FOXRED1 c.694C>T`, `Frameshift variant predicted to cause NMD`
  *Free-text variant interpretation notes from submitters — short prose useful for semantic search.*
- `submitted_phenotype` → `Skraban-Deardorff syndrome`, `mitochondrial cardiomyopathy`, `IMMUNODEFICIENCY 36 WITH LYMPHOPROLIFERATION`, `BCAT1-related condition`
  *Human-readable disease/phenotype names as submitted — important for disease-based semantic lookup.*
- `review_status` → `criteria provided, single submitter`, `no assertion criteria provided`, `reviewed by expert panel`, `practice guideline`
  *Evidence review level labels meaningful for evidence quality queries.*
- `collection_method` → `clinical testing`, `research`, `case-control`, `reference population`
  *Human-readable evidence collection method labels with biomedical meaning.*
- `submitted_gene_symbol` → `AP5Z1`, `FOXRED1`, `HFE`, `BRCA1`
  *Gene symbol as submitted — human-readable gene name for semantic gene search.*
- `explanation` → `One submitter classified as Pathogenic based on functional evidence`, `Conflicting interpretations from different laboratories`
  *Free-text conflict explanation notes — prose annotation useful for semantic search.*
- `somatic_clinical_impact` → `Tier I - Strong`, `Tier II - Potential`, `Tier III - Unknown`, `Tier IV - Benign/Likely benign`
  *Somatic clinical tier classification labels with biomedical meaning for oncology queries.*
- `oncogenicity` → `Oncogenic`, `Likely oncogenic`, `Uncertain significance`, `Likely benign`
  *Oncogenicity classification labels — categorical biomedical enum relevant for cancer variant queries*

### CTD (21 tables, 60 cols)

**`anatomy_master_v2`**
- `anatomy_name` → `3T3 Cells`, `Abdomen`, `Abdominal Cavity`, `Abdominal Fat`
  *Human-readable anatomy/cell-line name used for semantic search.*
- `definition` → `Cell lines whose original growth…`, `That portion of the body that…`, `The region in the abdomen extending…`, `Fatty tissue in the region of…`
  *Short free-text biomedical definition of the anatomy term.*
- `synonyms` → `3T3 Cell|Cell, 3T3|Cells, 3T3`, `Abdomens`, `Abdominal Cavities|Cavitas abd…`, `Abdominal Adipose Tissue|Abdom…`
  *Pipe-delimited human-readable synonym labels for the anatomy term.*

**`chem_gene_ixn_types_v2`**
- `name` → `abundance`, `chemical synthesis`, `ethylation`, `export`
  *Human-readable interaction type label (e.g. 'abundance', 'hydroxylation').*
- `definition` → `The abundance of a chemical…`, `A biochemical event resulting…`, `The addition of an ethyl group…`, `The movement of a molecule out…`
  *Short free-text definition of the interaction type.*

**`chemical_disease_association_v2`**
- `drug_name` → `06-Paris-LA-66 protocol`, `10,10-bis(4-pyridylmethyl)-9…`, `10,11-dihydro-10-hydroxycarbam…`, `10-hydroxycamptothecin`
  *Human-readable chemical/drug name.*
- `disease_name` → `Precursor Cell Lymphoblastic Lymphoma`, `Hyperkinesis`, `Seizures`, `Epilepsy`
  *Human-readable MeSH disease name.*
- `direct_evidence` → `therapeutic`, `marker/mechanism`, `null`
  *Categorical label describing evidence type (e.g. therapeutic, marker/mechanism).*
- `inference_gene` → `TP53`, `BRCA1`, `EGFR`, `TNF`
  *Gene symbol(s) used as evidence for inference — human-readable.*

**`chemical_gene_interaction_v2`**
- `drug_name` → `10074-G5`, `arsenic`, `cisplatin`, `bisphenol A`
  *Human-readable chemical/drug name.*
- `gene_name` → `AR`, `EPHB2`, `MAX`, `TP53`
  *Human-readable gene symbol.*
- `organism` → `Heterotremata`, `Neocaridina davidi`, `Cairina moschata`, `Tenebrio molitor`
  *Human-readable organism name (includes non-human species relevant for cross-species queries).*
- `interaction_text` → `10074-G5 affects the reaction…`, `10074-G5 inhibits the reaction…`, `10074-G5 results in decreased…`
  *Free-text sentence describing the chemical-gene interaction from literature.*
- `interaction_actions` → `affects^reaction|increases^expression`, `decreases^reaction|increases^expression`, `affects^binding|affects^folding`, `decreases^expression`
  *Structured action type labels describing molecular effects (e.g. increases^expression).*
- `gene_forms` → `gene|mRNA`, `protein`, `gene|protein`, `mutant form|protein`
  *Biomedically meaningful labels for the gene form involved (protein, mRNA, 3' UTR).*

**`chemical_go_enriched_v2`**
- `drug_name` → `10074-G5`, `arsenic`, `cisplatin`, `dexamethasone`
  *Human-readable chemical/drug name.*
- `go_ontology` → `Biological Process`, `Molecular Function`, `Cellular Component`
  *GO ontology branch label (Biological Process, Molecular Function, Cellular Component).*
- `go_term_name` → `positive regulation of miRNA metabolic process`, `cis-regulatory region sequence-specific DNA binding`, `E-box binding`, `RNA polymerase II cis-regulatory region…`
  *Human-readable GO term name describing enriched process/function.*

**`chemical_master_table`**
- `drug_name` → `bevonium`, `insulin, neutral`, `N-acetylglucosaminylasparagine`, `N-acetyl-L-arginine`
  *Human-readable chemical/drug name used for entity lookup.*

**`chemical_master_v2`**
- `drug_name` → `irsogladine`, `CTAP octapeptide`, `3,3-bis(3-fluorophenyl)propylamine`, `perfluorodecyl bromide`
  *Human-readable chemical/drug name.*
- `definition` → `null`, `A naturally occurring compound…`, `A phosphodiesterase inhibitor…`
  *Free-text biomedical description of the chemical.*
- `synonyms` → `2,4-diamino-6-(2,5-dichlorophe…`, `1-Tca-CTAP|CTAP (somatostatin…`, `NPS 846|NPS846`, `OB-BP1 protein, human|sialic a…`
  *Pipe-delimited human-readable chemical synonym names.*

**`chemical_pathway_enriched_v2`**
- `drug_name` → `10074-G5`, `arsenic`, `cisplatin`, `dexamethasone`
  *Human-readable chemical/drug name.*
- `pathway_name` → `Cyclin A:Cdk2-associated events…`, `Cyclin E associated events dur…`, `G1/S Transition`, `Mitotic G1-G1/S phases`
  *Human-readable biological pathway name.*

**`chemical_phenotype_ixn_v2`**
- `drug_name` → `10074-G5`, `arsenic`, `cisplatin`, `lead`
  *Human-readable chemical/drug name.*
- `phenotype_name` → `ATP biosynthetic process`, `cellular lipid biosynthetic process`, `myeloid cell differentiation`, `positive regulation of cholesterol metabolic process`
  *Human-readable GO phenotype/biological process name.*
- `organism` → `Homo sapiens`, `Mus musculus`, `Rattus norvegicus`
  *Human-readable organism name.*
- `interaction_actions` → `decreases^phenotype`, `increases^phenotype`, `affects^phenotype`
  *Structured direction label for phenotype effect (increases/decreases/affects).*

**`disease_go_bp_v2`**
- `disease_name` → `Abruptio Placentae`, `Neural Tube Defects`, `Hypoxia`, `Liver Cirrhosis, Experimental`
  *Human-readable MeSH disease name.*
- `go_term_name` → `10-formyltetrahydrofolate biosynthetic process`, `10-formyltetrahydrofolate catabolic process`, `1,2-diacyl-sn-glycero-3-phosphocholine…`, `positive regulation of cholesterol…`
  *Human-readable GO biological process term.*

**`disease_go_cc_v2`**
- `disease_name` → `Autistic Disorder`, `Carcinoma, Renal Cell`, `Miller-McKusick-Malvaux-Syndrome`, `Three M Syndrome 2`
  *Human-readable MeSH disease name.*
- `go_term_name` → `3M complex`, `3-methylcrotonoyl-CoA carboxylase complex`, `1-alkyl-2-acetylglycerophosphocholine…`
  *Human-readable GO cellular component term.*

**`disease_go_mf_v2`**
- `disease_name` → `Acute Kidney Injury`, `Drug-Related Side Effects and Adverse Reactions`, `Heart Failure`, `Myocardial Ischemia`
  *Human-readable MeSH disease name.*
- `go_term_name` → `10-hydroxy-9-(phosphonooxy)octadecanoate…`, `11-beta-hydroxysteroid dehydrogenase activity`, `1-alkyl-2-acetylglycerophosphocholine esterase…`
  *Human-readable GO molecular function term.*

**`disease_master_table`**
- `disease_name` → `familial gynecomastia, due to…`, `Jalili syndrome`, `Typical Teratoid Rhabdoid Tumor`, `Spheroid body myopathy`
  *Human-readable MeSH disease name used for entity lookup.*

**`exposure_events_v2`**
- `stressor_name` → `alpha-Linolenic Acid`, `chrysene`, `Silver`, `1,1,1-trichloroethane`
  *Human-readable chemical stressor name.*
- `stressor_source_category` → `Commercial product|Dietary|Environmental`, `Commercial product|Environmental|Residential`, `Commercial product|Medicinal`, `Dietary|Environmental|Occupational|Residential`
  *Categorical label for the exposure source (e.g. Commercial product, Dietary, Environmental).*
- `stressor_source_details` → `indoor dust`, `air pollution from electroplating factory`, `second hand smoke in universities`, `fuel oil and coal combustion from petrochemical complex`
  *Free-text description of the specific exposure source.*
- `disease_name` → `Hypoplastic Left Heart Syndrome`, `Carcinoma, Squamous Cell`, `Child Development Disorders, Pervasive`, `Gastrointestinal Diseases`
  *Human-readable MeSH disease outcome name.*
- `phenotype_name` → `cellular response to chemical stimulus`, `positive regulation of cholesterol metabolic process`, `testosterone biosynthetic process`, `fatty acid metabolic process`
  *Human-readable GO phenotype/process outcome name.*
- `outcome_relationship` → `positive correlation`, `negative correlation`, `no correlation`, `prediction/hypothesis`
  *Categorical label for direction of relationship (positive/negative correlation).*
- `phenotype_action_type` → `increased`, `decreased`, `abnormal`
  *Categorical direction label for phenotype outcome (increased/decreased/abnormal).*
- `medium` → `salad dressing`, `e-cigarette, aerosol`, `subcutaneous fat, abdominal`, `chest`
  *Free-text label for the biological/environmental medium of exposure.*
- `anatomy` → `Blood Cells|Telomere|Umbilical Cord`, `Brain|Gray Matter|Rhombencephalon`, `Blood|Erythrocytes`, `Hand`
  *Pipe-delimited human-readable anatomy terms associated with the outcome.*
- `study_countries` → `Hungary`, `Tunisia`, `Ethiopia`, `Estonia|Finland|Iceland|Latvia`
  *Human-readable country names for geographic filtering.*
- `methods` → `graphite furnace atomic absorption spectrometry`, `liquid chromatography-tandem mass spectrometry`, `air monitoring|conditional logistic regression analysis`
  *Free-text experimental method description.*
- `exposure_event_marker` → `2,3,4,4'5-pentachlorobiphenyl`, `protocatechuic acid`, `MUC1`, `Cholesterol, LDL`
  *Human-readable biomarker name for the exposure measurement.*

**`exposure_studies_v2`**
- `author_summary` → `These results suggest that exposure…`, `These results support the hypothesis…`, `Hair samples of school children…`, `The current data confirm past…`
  *Free-text author-written study summary — rich semantic content.*
- `diseases` → `Agricultural Workers' Diseases`, `Carcinoma, Squamous Cell`, `Acute Disease|Burns, Chemical|Esophageal Neoplasms`
  *Pipe-delimited human-readable disease names associated with the study.*
- `phenotypes` → `interleukin-4 production`, `cellular response to chemical stimulus`
  *Pipe-delimited human-readable GO phenotype names from the study.*

**`gene_master_table`**
- `gene_name` → `A1BG`, `A2M`, `NAT1`, `NAT2`
  *Human-readable gene symbol used for entity lookup.*

**`genes_pathways_v2`**
- `gene_name` → `A1BG`, `A1CF`, `A2M`, `AADAC`
  *Human-readable gene symbol.*
- `pathway_name` → `Hemostasis`, `Immune System`, `Innate Immune System`, `Neutrophil degranulation`
  *Human-readable biological pathway name.*

**`pathway_master_table`**
- `pathway_name` → `Interleukin-6 signaling`, `Apoptosis`, `Hemostasis`, `Intrinsic Pathway for Apoptosis`
  *Human-readable biological pathway name used for entity lookup.*

**`phenotype_disease_bp_v2`**
- `go_term_name` → `10-formyltetrahydrofolate biosynthetic process`, `10-formyltetrahydrofolate catabolic process`, `1,2-diacyl-sn-glycero-3-phosphocholine biosynthesis`
  *Human-readable GO biological process term.*
- `disease_name` → `Abruptio Placentae`, `Neural Tube Defects`, `Chemical and Drug Induced Liver Injury`, `Hypoxia`
  *Human-readable MeSH disease name.*

**`phenotype_disease_cc_v2`**
- `go_term_name` → `3M complex`, `3-methylcrotonoyl-CoA carboxylase complex`, `1-alkyl-2-acetylglycerophosphocholine esterase complex`
  *Human-readable GO cellular component term.*
- `disease_name` → `Autistic Disorder`, `Carcinoma, Renal Cell`, `Lissencephaly`, `Three M Syndrome 2`
  *Human-readable MeSH disease name.*

**`phenotype_disease_mf_v2`**
- `go_term_name` → `10-hydroxy-9-(phosphonooxy)octadecanoate phosphatase`, `11-beta-hydroxysteroid dehydrogenase activity`, `1-alkyl-2-acetylglycerophosphocholine esterase activity`
  *Human-readable GO molecular function term.*
- `disease_name` → `Acute Kidney Injury`, `Autism Spectrum Disorder`, `Drug-Related Side Effects and Adverse Reactions`, `Heart Failure`
  *Human-readable MeSH disease name.*

### DGIDB (4 tables, 6 cols)

**`drug_gene_association_dgidb`**
- `interaction_types` → `inhibitor`, `agonist`, `activator`, `blocker`
  *Biomedically meaningful categorical label describing the drug-gene interaction mechanism (e.g. inhib*

**`drug_master_table_dgidb`**
- `drug_name` → `RACLOPRIDE`, `WITHAFERIN A`, `ANGIOTENSIN II`, `NERATINIB`
  *Human-readable drug name used for semantic search and drug lookup.*

**`gene_category_dgidb`**
- `gene_name` → `OR4C3`, `FANCC`, `FPR2`, `SEPTIN5`
  *Human-readable gene symbol used for gene-level semantic search.*
- `gene_long_name` → `olfactory receptor family 4 subfamily C member 3`, `FA complementation group C`, `formyl peptide receptor 2`, `septin 5`
  *Full descriptive gene name enabling free-text search by gene function or protein name.*
- `category_name` → `ENZYME`, `G PROTEIN COUPLED RECEPTOR`, `KINASE`, `TYROSINE KINASE`
  *Biomedically meaningful gene/protein category label (e.g. KINASE, ION CHANNEL) enabling category-bas*

**`gene_master_table_dgidb`**
- `gene_name` → `CYP2D6`, `PPARG`, `ATAD5`, `RGS4`
  *Human-readable gene symbol (HGNC format) enabling gene-level semantic search.*

### DOID (2 tables, 2 cols)

**`disease_master_table_doid`**
- `disease_name` → `angiosarcoma`, `disease of metabolism`, `shrimp allergy`, `eosinophilic esophagitis`
  *Human-readable disease name; primary semantic search target for disease lookup.*

**`disease_synonym_doid`**
- `synonym` → `hemangiosarcoma`, `surfer's eye`, `metabolic disease`, `acetylsalicylic acid allergy`
  *Alternate human-readable names/aliases for diseases; essential for synonym-aware semantic retrieval.*

### DRUGCENTRAL (47 tables, 84 cols)

**`action_type_drugcentral`**
- `action_type` → `ALLOSTERIC MODULATOR`, `GATING INHIBITOR`, `MEMBRANE PERMEABILIZER`, `DNA STRAND BREAK`
  *Human-readable drug action type label used for semantic lookup.*
- `description` → `Allosteric modulator is a substance that binds to a site other than the active site`, `Inhibit the opening or closing of ion channels`, `Permeabilization of the cell membrane`, `A DNA Strand Break involves one or both strands`
  *Short free-text description of the action type mechanism.*

**`act_table_full_drugcentral`**
- `target_name` → `Canalicular multispecific organic anion transporter`, `Motilin receptor`, `Taste receptor type 2 member 4`, `Penicillin-binding protein`
  *Human-readable protein/target name for semantic search.*
- `action_type` → `AGONIST`, `INHIBITOR`, `\N`
  *Biomedically meaningful action type enum (AGONIST, INHIBITOR, etc.).*
- `organism` → `Homo sapiens`, `Pseudomonas aeruginosa`, `Escherichia coli`, `Rattus norvegicus`
  *Human-readable organism name for filtering by species.*

**`approval`**
- `approval_type` → `FDA`, `PMDA`, `EMA`, `Health Canada`
  *Regulatory agency name with biomedical meaning (FDA, EMA, PMDA).*

**`approval_type_drugcentral`**
- `descr` → `FDA`, `EMA`, `Health Canada`, `UK Medicines and Healthcare Products Regulatory Agency`
  *Full name of regulatory agency; human-readable label.*

**`atc_hierarchy`**
- `l1_name` → `ALIMENTARY TRACT AND METABOLISM`, `NERVOUS SYSTEM`, `BLOOD AND BLOOD FORMING ORGANS`, `ANTINEOPLASTIC AND IMMUNOMODULATING AGENTS`
  *Top-level ATC pharmacological category name.*
- `l2_name` → `STOMATOLOGICAL PREPARATIONS`, `ANTIFIBRINOLYTICS`, `ANTIMETABOLITES`, `OTHER ALIMENTARY TRACT AND METABOLISM`
  *Second-level ATC pharmacological category name.*
- `l3_name` → `STOMATOLOGICAL PREPARATIONS`, `ANTIFIBRINOLYTICS`, `ANTIPSORATIC FOR TOPICAL USE`, `ANESTHETICS, LOCAL`
  *Third-level ATC therapeutic category name.*
- `l4_name` → `Caries prophylactic agents`, `Amino acids`, `Pyrimidine analogues`, `Amides`
  *Fourth-level ATC chemical subgroup name.*
- `chemical_substance` → `sodium fluoride`, `sodium monofluorophosphate`, `olaflur`, `aminomethylbenzoic acid`
  *Human-readable drug chemical substance name at the ATC leaf level.*

**`attr_type_drugcentral`**
- `name` → `CHEBI definition`, `Human and mouse protein kinase subfamilies`, `IUPHAR_TARGET_TYPE`, `IUPHAR_TARGET_FAMILY`
  *Human-readable attribute type name with biomedical meaning.*

**`data_source_drugcentral`**
- `source_name` → `KEGG DRUG`, `BINDINGDB`, `NDFRT`, `EXPERT CURATOR`
  *Human-readable name of data source database.*

**`ddi_drugcentral`**
- `drug_class1` → `Monoamine Oxidase Inhibitors`, `Alpha/Beta Agonists`, `Amphetamines`, `Inhalational Anesthetics`
  *Drug class name (first interacting class); human-readable biomedical label.*
- `drug_class2` → `Alpha/Beta Agonists`, `Amphetamines`, `Inhalational Anesthetics`, `buspirone`
  *Drug class name (second interacting class); human-readable biomedical label.*
- `ddi_risk` → `Potentially significant`, `Contraindicated`, `Avoid combination`, `Significant`
  *Risk level label with clinical meaning (Contraindicated, Potentially significant).*
- `description` → `MAO Inhibitors may enhance the sympathomimetic effect`, `fatal/non-fatal reactions`, `hypertension may occur`, `life-threatening hypertensive crisis`
  *Short free-text description of the drug-drug interaction effect.*

**`ddi_risk_drugcentral`**
- `risk` → `Potentially significant`, `Contraindicated`, `Avoid combination`, `Significant`
  *Human-readable DDI risk level label.*

**`disease_master_table`**
- `disease_name` → `Triple negative breast neoplasm`, `Metastatic non-small cell lung carcinoma`, `Medullary thyroid carcinoma`, `Gastrointestinal stromal tumor`
  *Human-readable disease/indication name for semantic lookup.*

**`doid_drugcentral`**
- `label` → `acanthosis nigricans`, `retroperitoneal sarcoma`, `anthracosis`, `esophageal diverticulosis`
  *Human-readable Disease Ontology disease label.*

**`drug_atc`**
- `l1_name` → `NERVOUS SYSTEM`, `BLOOD AND BLOOD FORMING ORGANS`, `ALIMENTARY TRACT AND METABOLISM`, `ANTINEOPLASTIC AND IMMUNOMODULATING AGENTS`
  *Top-level ATC category name; human-readable therapeutic class.*
- `l2_name` → `ANTIFIBRINOLYTICS`, `ANTIMETABOLITES`, `ANTIPSORATIC FOR SYSTEMIC USE`
  *Second-level ATC category name.*
- `l3_name` → `ANESTHETICS, LOCAL`, `ANTIFIBRINOLYTICS`, `ANTIPSORATIC FOR TOPICAL USE`
  *Third-level ATC category name.*
- `l4_name` → `Amides`, `Amino acids`, `Pyrimidine analogues`, `Psoralens for topical use`
  *Fourth-level ATC chemical subgroup name.*
- `chemical_substance` → `levobupivacaine`, `aminomethylbenzoic acid`, `sodium phenylbutyrate`, `azacitidine`
  *Human-readable drug name at ATC leaf level.*

**`drug_class_drugcentral`**
- `name` → `Monoamine Oxidase Inhibitors`, `Alpha/Beta Agonists`, `Amphetamines`, `Inhalational Anesthetics`
  *Human-readable drug class name for semantic search.*

**`drug_disease_association`**
- `disease_name` → `Triple negative breast neoplasm`, `Metastatic non-small cell lung carcinoma`, `Medullary thyroid carcinoma`, `Gastrointestinal stromal tumor`
  *Human-readable disease indication name.*
- `relationship` → `indication`, `contraindication`
  *Relationship type with biomedical meaning (indication, contraindication).*

**`drug_master_table`**
- `drug_name` → `capmatinib`, `selpercatinib`, `ripretinib`, `molnupiravir`
  *Primary human-readable drug name; key for semantic drug lookup.*

**`drug_pharma_class`**
- `class_name` → `Analgesics`, `Analgesics, Non-Narcotic`, `Anti-Inflammatory Agents`, `Anti-Inflammatory Agents, Non-Steroidal`
  *Human-readable pharmacological class name (MeSH PA class).*

**`drug_synonyms`**
- `synonym` → `sacituzumab`, `sacituzumab govitecan`, `sacituzumab govitecan-hziy`, `trodelvy`
  *Drug synonym/alias name; critical for synonym-based semantic retrieval.*

**`drug_target_activity`**
- `gene_symbol` → `ABCC2`, `MLNR`, `TAS2R4`, `EGFR`
  *Gene/protein symbol; primary biomedical entity for target search.*
- `target_name` → `Canalicular multispecific organic anion transporter`, `Motilin receptor`, `Taste receptor type 2 member 4`, `Epidermal growth factor receptor`
  *Human-readable target protein name.*
- `action_type` → `AGONIST`, `INHIBITOR`, `ANTAGONIST`
  *Drug action type with biomedical meaning (AGONIST, INHIBITOR).*
- `organism` → `Homo sapiens`, `Rattus norvegicus`, `Escherichia coli`
  *Human-readable organism name.*

**`faers_drugcentral`**
- `meddra_name` → `5'nucleotidase increased`, `5-alpha-reductase deficiency`, `Abdominal adhesions`, `Abdominal distension`
  *MedDRA adverse event term; human-readable clinical label.*

**`faers_female_drugcentral`**
- `meddra_name` → `Abdominal adhesions`, `Abdominal distension`, `Amyotrophic lateral sclerosis`
  *MedDRA adverse event term for female FAERS subset.*

**`faers_ger_drugcentral`**
- `meddra_name` → `Amyotrophic lateral sclerosis`, `Amyotrophy`, `Abdominal adhesions`
  *MedDRA adverse event term for geriatric FAERS subset.*

**`faers_male_drugcentral`**
- `meddra_name` → `5-alpha-reductase deficiency`, `Abdominal adhesions`
  *MedDRA adverse event term for male FAERS subset.*

**`faers_ped_drugcentral`**
- `meddra_name` → `Abdominal distension`, `Accidental overdose`, `Agitation neonatal`, `Aspartate aminotransferase increased`
  *MedDRA adverse event term for pediatric FAERS subset.*

**`id_type_drugcentral`**
- `description` → `Veterans Health Administration National Drug File`, `National Drug File`, `FDB MedKnowledge (formerly NDDF)`, `RxNorm Vocabulary`
  *Full human-readable name of the identifier database.*

**`inn_stem_drugcentral`**
- `stem` → `-azolam`, `-tamab`, `-vimab`, `-virtide`
  *INN drug name stem; biomedical naming convention element.*
- `definition` → `diazepam derivatives`, `antitumor, monoclonal antibodies`, `antiviral, monoclonal antibodies`, `antiviral, peptides and glycoproteins`
  *Human-readable description of the drug class the stem represents.*

**`label_drugcentral`**
- `title` → `GLYBURIDE AND METFORMIN HYDROCHLORIDE`, `Baycadron`, `Benztropine Mesylate Injection`, `Antizol`
  *Drug label title (brand or generic name); human-readable drug name.*
- `category` → `HUMAN PRESCRIPTION DRUG LABEL`, `Human Prescription Drug Label`, `HUMAN OTC DRUG LABEL`
  *Drug label category indicating intended population/prescription type.*

**`ob_exclusivity_code_drugcentral`**
- `description` → `COMPETITIVE GENERIC THERAPY`, `NEW DOSING SCHEDULE`, `GENERATING ANTIBIOTIC INCENTIVES NOW`, `NEW INDICATION`
  *Human-readable description of the FDA exclusivity code type.*

**`ob_product_drugcentral`**
- `ingredient` → `ALLOPURINOL`, `CALCIUM CHLORIDE; DEXTROSE; MAGNESIUM CHLORIDE`, `levobupivacaine`, `fluorouracil`
  *Active ingredient name; human-readable drug name.*
- `trade_name` → `ALLOPURINOL`, `INPERSOL-LC/LM W/ DEXTROSE 1.5%`, `INPERSOL-LC/LM W/ DEXTROSE 2.5%`
  *Brand/trade name of the drug product.*
- `dose_form` → `TABLET`, `SOLUTION`, `INJECTION`, `CAPSULE`
  *Dosage form label with clinical meaning.*
- `route` → `ORAL`, `INTRAPERITONEAL`, `PARENTERAL`, `INTRAVENOUS`
  *Administration route with clinical meaning.*

**`omop_relationship_drugcentral`**
- `relationship_name` → `indication`, `contraindication`, `off-label use`
  *OMOP relationship type with biomedical meaning (indication, contraindication).*
- `concept_name` → `Triple negative breast neoplasm`, `Metastatic non-small cell lung carcinoma`, `Medullary thyroid carcinoma`, `Gastrointestinal stromal tumor`
  *Human-readable OMOP concept (disease/condition) name.*
- `snomed_full_name` → `Triple negative breast neoplasm`, `Metastatic non-small cell lung carcinoma`, `Gastrointestinal stromal tumor`, `Positron emission tomography`
  *SNOMED CT full disease name; human-readable clinical concept.*

**`parentmol_drugcentral`**
- `name` → `temsavir`, `dexmethylphenidate`, `monomethyl fumarate`, `melengestrol`
  *Human-readable parent molecule drug name.*

**`pdb_drugcentral`**
- `title` → `Crystal structure of...`, `X-ray structure of...`
  *Human-readable PDB entry title describing the structure.*
- `exp_method` → `X-RAY DIFFRACTION`, `ELECTRON MICROSCOPY`, `NMR`
  *Experimental method label with scientific meaning.*

**`product`**
- `product_name` → `GLYBURIDE AND METFORMIN HYDROCHLORIDE`, `Baycadron`, `Benztropine Mesylate Injection`, `ALLOPURINOL`
  *Human-readable drug product name.*
- `form` → `TABLET`, `ELIXIR`, `INJECTION`, `SOLUTION`
  *Dosage form with clinical meaning.*
- `route` → `ORAL`, `PARENTERAL`, `INTRAVENOUS`, `TOPICAL`
  *Administration route with clinical meaning.*

**`property_type_drugcentral`**
- `name` → `Bioavailability`, `Biopharmaceutical Drug Disposition Classification System`, `Clearance`, `Fraction excreted unchanged in urine`
  *Human-readable pharmacokinetic/pharmacodynamic property name.*
- `category` → `ADME`, `Pharmacology`
  *Property category label (ADME, Pharmacology); biomedically meaningful.*

**`protein_type_drugcentral`**
- `type` → `SINGLE PROTEIN`, `PROTEIN FAMILY`, `PROTEIN COMPLEX`, `PROTEIN COMPLEX GROUP`
  *Human-readable protein complex type label.*

**`reference_drugcentral`**
- `title` → `Crystal structures of...`, `Pharmacokinetics of...`, `Clinical trial of...`
  *Human-readable publication title for semantic search.*

**`ref_type_drugcentral`**
- `type` → `DRUG LABEL`, `PATENT`, `CLINICAL TRIAL`, `JOURNAL ARTICLE`
  *Human-readable reference type label.*

**`section_drugcentral`**
- `title` → `PRECAUTIONS SECTION`, `SPL PATIENT PACKAGE INSERT SECTION`, `SPL MEDGUIDE SECTION`, `CLINICAL STUDIES SECTION`
  *Drug label section title (PRECAUTIONS, CLINICAL PHARMACOLOGY, etc.); human-readable clinical label.*

**`structures_drugcentral`**
- `name` → `capmatinib`, `selpercatinib`, `ripretinib`, `molnupiravir`
  *Primary human-readable drug name.*
- `stem` → `-tinib`, `-mab`, `-vir`
  *INN drug name stem; biomedically meaningful nomenclature element.*

**`structure_type_drugcentral`**
- `type` → `ANTIBODY-DRUG CONJUGATE`, `ORGANIC`, `RADIOPHARMACEUTICAL`, `MONOCLONAL ANTIBODY`
  *Human-readable drug structure type label (ORGANIC, MONOCLONAL ANTIBODY, etc.).*

**`synonyms_drugcentral`**
- `name` → `sacituzumab`, `sacituzumab govitecan`, `sacituzumab govitecan-hziy`, `trodelvy`
  *Drug synonym/alias; essential for synonym-based semantic retrieval.*

**`target_class_drugcentral`**
- `l1` → `Parasite`, `Drug`, `Glycoprotein`, `Tumour-associated antigen`
  *Human-readable top-level target class label.*

**`target_component_drugcentral`**
- `name` → `ATP-binding cassette subfamily C member 8`, `Transient receptor potential cation channel subfamily M member 4`, `6-hydroxymethyl-7,8-dihydropterin pyrophosphokinase`, `DNA gyrase subunit B`
  *Human-readable protein target name.*
- `gene` → `Abcc8`, `Trpm4`, `PPPK-DHPS`, `gyrB`
  *Gene symbol; primary biomedical entity identifier for target lookup.*
- `organism` → `Rattus norvegicus`, `Plasmodium berghei`, `Mycolicibacterium smegmatis`, `Lactococcus lactis`
  *Human-readable organism name.*

**`target_dictionary_drugcentral`**
- `name` → `Sur1-Trpm4; Sulfonylurea receptor`, `Tyrosinase`, `Eyes absent homolog 2`, `Butyrophilin subfamily 3 member`
  *Human-readable target protein name.*
- `target_class` → `Ion channel`, `Unclassified`, `Enzyme`, `Antibody`
  *Human-readable target class label (Ion channel, GPCR, Enzyme, etc.).*
- `protein_type` → `PROTEIN COMPLEX GROUP`, `SINGLE PROTEIN`, `PROTEIN FAMILY`, `PROTEIN COMPLEX`
  *Human-readable protein assembly type label.*

**`target_go_drugcentral`**
- `term` → `host cell endoplasmic reticulum`, `secalciferol 1-monooxygenase activity`, `1-alpha,25-dihydroxyvitamin D3 25-hydroxylase activity`, `ERBB3:ERBB2 complex`
  *Gene Ontology term label; human-readable biological function/process/component.*

**`target_keyword_drugcentral`**
- `keyword` → `2Fe-2S`, `3D-structure`, `3Fe-4S`, `4Fe-4S`
  *UniProt keyword label; human-readable biological function annotation.*
- `descr` → `Protein which contains at least one 2Fe-2S iron-sulfur cluster`, `Protein, or part of a protein, whose 3D structure has been resolved`, `Protein involved in the synthesis of abscisic acid`
  *Short description of the keyword's biomedical meaning.*
- `category` → `Ligand`, `Technical term`, `Biological process`, `Disease`
  *Keyword category label (Ligand, Technical term, Biological process).*

**`target_master_table`**
- `gene_id` → `ABCC2`, `MLNR`, `TAS2R4`, `EGFR`
  *Gene symbol used as identifier; suitable for symbol-based semantic lookup.*
- `gene_name` → `Canalicular multispecific organic anion transporter`, `Motilin receptor`, `Taste receptor type 2 member 4`, `Epidermal growth factor receptor`
  *Human-readable protein/gene full name.*

**`ob_patent_use_code_drugcentral`**
- `description` → `PREVENTION OF PREGNANCY`, `TREATMENT OR PROPHYLAXIS OF ANGINA`, `TREATMENT OF HYPERTENSION`, `PROVIDING PREVENTION AND TREATMENT`
  *Human-readable clinical use description for FDA patent use codes.*

### HCDT (15 tables, 19 cols)

**`disease_master_table`**
- `disease_name` → `Vibrio cholerae infection`, `Bacterial infection`, `Inflammation`, `Bacillary dysentery`
  *Human-readable disease name suitable for semantic search.*

**`disease_master_table_v2`**
- `disease_name` → `Familial hyperinsulinemic hypoglycemia`, `Frontotemporal Dementia`, `Mitochondrial Myopathies`, `Huntington disease`
  *Human-readable disease name suitable for semantic search.*

**`drug_disease_association_v2`**
- `drug_name` → `Cyclophosphamide`, `Methotrexate`, `Mercaptopurine`, `Clofarabine`
  *Human-readable drug name for semantic drug lookup.*
- `disease_name` → `B-cell acute lymphoblastic leukemia`, `Alzheimer's disease`, `Huntington disease`, `Trypanosomiasis`
  *Human-readable disease name suitable for semantic search.*

**`drug_gene_association_v2`**
- `drug_name` → `Amitriptyline`, `Clomipramine`, `Desipramine`, `Zonalon`
  *Human-readable drug name for semantic lookup.*

**`drug_master_table`**
- `drug_name` → `Acetylcarnitine`, `Dinitrochlorobenzene`, `9-Ethyladenine`, `Chloroacetaldehyde`
  *Human-readable drug/compound name for semantic search.*

**`drug_master_table_v2`**
- `drug_name` → `1-Benzyl-2,3-dioxoindole-5-sulfonamide`, `8-fluoro-4-[(3R)-3-(methylamino)pyrrolidin-1-yl]-[1]benzothiolo[3,2-d]pyrimidin-2-amine`, `Cyclophosphamide`, `Methotrexate`
  *Human-readable chemical/drug name for semantic search.*

**`drug_pathway_association_v2`**
- `drug_name` → `orlistat`, `Cyclophosphamide`, `Methotrexate`, `Imatinib`
  *Human-readable drug name for semantic lookup.*
- `pathway_name` → `Melanogenesis`, `Ubiquitin mediated proteolysis`, `Alzheimer's disease`, `Wnt signaling pathway`
  *Human-readable biological pathway name for semantic search.*

**`drug_rna_association_v2`**
- `drug_name` → `Framycetin`, `(2S)-2-amino-5-guanidinopentanoate`, `Myricetin`, `N,N'-[acridine-3,6-Diylbis(1h-1,2,3-Triazole-1,4-Diylbenzene-3,1-Diyl)]bis[3-(Diethylamino)propanamide]`
  *Human-readable drug/compound name for semantic lookup.*
- `rna_type` → `RNA`, `circRNA`, `miRNA`, `lncRNA`
  *Categorical RNA type label with biomedical meaning.*

**`drug_target_negative_v2`**
- `drug_name` → `Rifampin`, `Sgc-aak1-1N`, `2-Amino-cyclohex-3-enecarboxylic acid`, `3-(Aminomethyl)-2,6-difluorophenol`
  *Human-readable drug name for semantic lookup of negative interactions.*

**`gene_master_table`**
- `gene_name` → `A1BG`, `A2M`, `NAT1`, `NAT2`
  *Gene symbol (HGNC-style) used as the primary human-readable gene name.*

**`gene_master_table_v2`**
- `gene_symbol` → `PSMD8`, `HAL`, `SLC22A11`, `ARID5B`
  *HGNC gene symbol, the canonical human-readable gene identifier.*

**`pathway_gene_association_v2`**
- `pathway_name` → `Mitochondrial protein import`, `Regulation of expression of SLITs and ROBOs`, `RNA Polymerase II Transcription`, `Signaling Pathways`
  *Human-readable biological pathway name for semantic search.*

**`pathway_master_table`**
- `pathway_name` → `Pentose and glucuronate interconversions`, `Fatty acid degradation`, `Ubiquinone and other terpenoid-quinone biosynthesis`, `Steroid hormone biosynthesis`
  *Human-readable biological pathway name for semantic search.*

**`pathway_master_table_v2`**
- `pathway_name` → `Aflatoxin activation and detoxification`, `Oxidative phosphorylation`, `PI3K Cascade`, `Defective ADA disrupts adenosine deamination`
  *Human-readable biological pathway name for semantic search.*

**`rna_master_table_v2`**
- `rna_name` → `RP11-1149O23.4`, `LINC00163`, `RP11-78F17.1`, `miR-504`
  *Human-readable RNA gene name (lncRNA/miRNA symbol) for semantic lookup.*
- `rna_type` → `lncRNA`, `miRNA`, `circRNA`, `RNA`
  *Categorical RNA biotype label with biomedical meaning (lncRNA/miRNA/circRNA/RNA).*

### HGNC (2 tables, 9 cols)

**`gene_master_table_hgnc`**
- `gene_symbol` → `A1BG`, `A1BG-AS1`, `A1CF`, `A2M`
  *Human-readable approved HGNC gene symbol — primary lookup term for semantic search.*
- `gene_name` → `alpha-1-B glycoprotein`, `A1BG antisense RNA 1`, `APOBEC1 complementation factor`, `alpha-2-macroglobulin`
  *Full human-readable gene name — essential for free-text queries about gene function.*
- `locus_group` → `protein-coding gene`, `non-coding RNA`, `pseudogene`, `other`
  *Categorical biomedical label describing the broad gene class — useful for semantic filtering.*
- `locus_type` → `gene with protein product`, `RNA, long non-coding`, `RNA, micro`, `pseudogene`
  *More specific gene locus type label with biomedical meaning — good for semantic queries on gene type*
- `alias_symbol` → `FLJ23569`, `ACF|ASP|ACF64|ACF65|APOBEC1CF`, `FWP007|S863-7|CPAMD5`, `FLJ25179|p170`
  *Pipe-delimited list of alias gene symbols — critical for synonym-based semantic lookup.*
- `prev_symbol` → `NCRNA00181|A1BGAS|A1BG-AS`, `CPAMD9`, `A3GALT2P`, `P1`
  *Previous official gene symbols — needed to resolve legacy names in semantic search.*
- `alias_name` → `iGb3 synthase|isoglobotriaosylceramide synthase`, `Gb3 synthase|CD77 synthase|globotriaosylceramide synthase`, `aladin|Allgrove, triple-A|adracalin`, `acyl-CoA synthetase family member 1`
  *Human-readable alias full names for the gene — valuable synonym text for semantic retrieval.*
- `prev_name` → `non-protein coding RNA 181|A1BG antisense RNA (non-protein coding)`, `A2M antisense RNA 1 (non-protein coding)|A2M antisense RNA 1`, `C3 and PZP-like, alpha-2-macroglobulin domain containing 9`, `A2ML1 antisense RNA 1 (non-protein coding)`
  *Previous full gene names — needed to match historical terminology in biomedical text.*

**`withdrawn_gene_hgnc`**
- `withdrawn_symbol` → `A12M1`, `A12M2`, `A12M3`, `A12M4`
  *Former gene symbol that was withdrawn — needed to resolve obsolete identifiers in semantic search.*

### HPO (10 tables, 8 col-entries)

**`disease_master_table_hpo`**
- `disease_name` → `Developmental and epileptic encephalopathy 96`, `Pseudohyperkalemia, familial, 2, due to red cell leak`, `White-Kernohan syndrome`, `Short QT syndrome 2`
  *Human-readable disease name suitable for semantic search.*

**`disease_phenotype_annotation_hpo`**
- `disease_name` → `Developmental and epileptic encephalopathy 96`, `Pseudohyperkalemia, familial, 2, due to red cell leak`, `White-Kernohan syndrome`, `Short QT syndrome 2`
  *Human-readable disease name for semantic search.*

**`gene_disease_association_hpo`**
- `gene_symbol` → `CARD9`, `TBC1D7`, `IFT81`, `LZTR1`
  *Human-readable gene symbol for semantic search.*
- `association_type` → `MENDELIAN`, `POLYGENIC`, `UNKNOWN`
  *Categorical biomedical label describing the gene-disease relationship type.*

**`gene_master_table_hpo`**
- `gene_symbol` → `NAT2`, `AARS1`, `ABAT`, `ABCA1`
  *Human-readable gene symbol (HGNC-style) suitable for semantic search.*

<!-- 2026-06-24 HPO normalization: gene_symbol / phenotype_name were removed from the
     association tables (gene_phenotype_association_hpo, phenotype_gene_association_hpo) and
     phenotype_master_table_hpo was dropped. Those values are now sourced solely from the
     master tables (gene_master_table_hpo.gene_symbol, phenotype_master_table_hpo.phenotype_name),
     so the per-association embed entries below were removed to avoid duplicate harvesting. -->

**`phenotype_master_table_hpo`**
- `phenotype_name` → `All`, `Abnormality of body height`, `Multicystic kidney dysplasia`, `Mode of inheritance`
  *Human-readable HPO phenotype name suitable for semantic search.*
- `definition` → `Deviation from the norm of height with respect to that which is expected according to age and gender norms.`, `Multicystic dysplasia of the kidney is characterized by multiple cysts of varying size in the kidney and the absence of a normal pelvicaliceal system.`, `The pattern in which a particular genetic trait or disorder is passed from one generation to the next.`, `A mode of inheritance that is observed for traits related to a gene encoded on one of the autosomes in which a trait manifests in heterozygotes.`
  *Short free-text definition of the phenotype term, high semantic value.*

**`phenotype_synonym_hpo`**
- `synonym` → `Abnormality of body height`, `Multicystic dysplastic kidney`, `Multicystic kidneys`, `Multicystic renal dysplasia`
  *Human-readable synonym for the HPO phenotype term, valuable for semantic search.*

### MESH (6 tables, 12 cols)

**`chemical_master_table_mesh`**
- `mesh_term` → `Calcimycin`, `Temefos`, `Abrin`, `Receptors, CCR6`
  *Human-readable MeSH chemical/compound name — primary semantic search target.*
- `pharmacological_actions` → `Anti-Bacterial Agents|Calcium Ionophores`, `Insecticides`, `Adjuvants, Immunologic|Angiogenesis Inhibitors`, `Adrenergic beta-Antagonists|Antihypertensive Agents`
  *Human-readable pipe-delimited pharmacological action names with strong biomedical meaning for semant*

**`descriptor_master_table_mesh`**
- `mesh_term` → `Calcimycin`, `Abattoirs`, `Abbreviations as Topic`, `Abdomen`
  *Human-readable MeSH descriptor name spanning all categories — primary semantic search target.*
- `pharmacological_actions` → `Anti-Bacterial Agents|Calcium Ionophores`, `Insecticides`, `Diuretics`, `Anthelmintics`
  *Human-readable pharmacological action names with biomedical meaning for semantic search.*

**`disease_master_table_mesh`**
- `mesh_term` → `Abdomen, Acute`, `Abdominal Injuries`, `Abdominal Neoplasms`, `Abetalipoproteinemia`
  *Human-readable MeSH disease name — primary semantic search target for disease queries.*

**`pharmacological_action_mesh`**
- `pharmacological_action_name` → `Abortifacient Agents`, `Narcotic Antagonists`, `Mineralocorticoids`, `Antispermatogenic Agents`
  *Human-readable pharmacological action category name — useful for drug mechanism semantic search.*
- `substance_name` → `alatriopril`, `Z13752A`, `dinoprost tromethamine`, `sulprostone`
  *Human-readable chemical or substance name — useful for drug/compound semantic search.*

**`qualifier_master_table_mesh`**
- `subheading` → `abnormalities`, `administration & dosage`, `adverse effects`, `analogs & derivatives`
  *Human-readable MeSH subheading qualifier label — useful for semantic search over annotation categori*
- `scope_note` → `Used with organs for congenital defects producing changes in the morphology of the organ.`, `Used with drugs for dosage forms, routes of administration, frequency and duration of administration.`, `Used with drugs, chemicals, or biological agents in accepted dosage when intended for diagnostic, therapeutic, prophylactic, or anesthetic purposes.`, `Used with drugs and chemicals for substances that share the same parent molecule or have similar electronic structure.`
  *Short free-text definition of the qualifier's usage — useful for understanding and semantic matching*

**`scr_master_table_mesh`**
- `name` → `bevonium`, `insulin, neutral`, `N-acetylglucosaminylasparagine`, `N-acetyl-L-arginine`
  *Human-readable supplementary concept name (chemical, drug, or disease) — primary semantic search tar*
- `pharmacological_actions` → `D000894-Anti-Inflammatory Agents, Non-Steroidal`, `D007004-Hypoglycemic Agents`, `D000276-Adjuvants, Immunologic|D000863-Antacids|D000894-Anti-Inflammatory Agents, Non-Steroidal|D000897-Anti-Ulcer Agents`, `D000970-Antineoplastic Agents|D007202-Indicators and Reagents|D011838-Radiation-Sensitizing Agents`
  *Pipe-delimited MeSH ID + human-readable action name pairs — the action names carry strong biomedical*
- `note` → `structure given in first source`, `a neutral, buffered solution of pork insulin`, `RN given refers to parent cpd; presence in urine characteristic of aspartylglucosaminuria`, `has effect on convulsive seizures`
  *Short free-text annotation describing the substance's biochemical role, source, or properties — usef*

### MONDO (2 tables, 2 cols)

**`disease_master_table_mondo`**
- `disease_name` → `adrenocortical insufficiency`, `nocturnal enuresis`, `infantile liver failure`, `inflammatory linear verrucous epidermal nevus`
  *Human-readable disease name — primary label for semantic disease search.*

**`disease_synonym_mondo`**
- `synonym` → `Mobius syndrome`, `pulmonary artery stenosis, branch (not PPS)`, `papillary breast carcinoma`, `ABCC9 familial atrial fibrillation`
  *Human-readable disease synonym/alias — essential for matching alternate disease names in semantic se*

### MSIGDB (4 tables, 9 queryable cols)

> **RUNTIME EMBEDDING APPROACH — DIFFERS FROM OTHER DBS**
>
> MSigDB does NOT use a persistent external Qdrant collection for schema-column
> ANN search. The BGE/SapBERT pipeline described in the TTD template does NOT
> apply here. Instead:
>
> 1. **In-memory Qdrant (schema-column ANN):** `app/per_db_tool/schema_kg_planner.py`
>    builds a `QdrantClient(":memory:")` collection at container boot (or lazily
>    on first query). It loads `evaluation/schema_kg/inputs/msigdb/schema.json`,
>    `queryable.json`, and `concept_type.json`, computes column embeddings via
>    `schema_kg.src.embed.compute_embeddings` using the **biochirp-bge** model,
>    and upserts all column vectors into the in-memory collection. This index is
>    rebuilt fresh on every container restart — it is NOT persisted to disk or to
>    the shared Qdrant service.
>
> 2. **Persistent Qdrant — fewshot bank only:** The shared Qdrant service
>    (`:6333`) holds the `fewshot_bank` collection. MSigDB few-shot examples are
>    stored there as points with `payload.db = "msigdb"` and retrieved at query
>    time by `evaluation/schema_kg/src/fewshot_bank.py`. The embedding model for
>    the fewshot bank is **SapBERT-from-PubMedBERT-fulltext-mean-token** (768-dim,
>    cosine). Ingestion is done by
>    `evaluation/schema_kg/scripts/fewshot_ingest.py` run offline.
>
> 3. **concept_values_msigdb.pkl:** User-queryable column values (gene symbols,
>    collection names, sub_collection codes, brief descriptions, etc.) are
>    pre-extracted from parquets by `scripts/build_concept_values.py` and written
>    to `resources/values/concept_values_msigdb.pkl`. This pickle is the source
>    for the value-mapper's candidate lookup at query time. It covers 9 queryable
>    fields: `gene_symbol`, `gene_organism`, `geneset_name`, `collection`,
>    `gene_count`, `geneset_organism`, `pmid`, `geo_id`, `sub_collection`,
>    `brief_description`, `full_description`.
>
> **Schema files used:** `evaluation/schema_kg/inputs/msigdb/` — `schema.json`,
> `queryable.json`, `concept_type.json`, `schema_rules.json`, `parquet_map.json`,
> `parquet_col_map.json`, `questions.json`.

**`gene_geneset_association_msigdb`** — FK-only join table; NO user-queryable columns embedded

This table contains only two columns: `geneset_id` (FK to
`geneset_master_table_msigdb`) and `gene_id` (FK to `gene_master_table_msigdb`,
composite `<gene_symbol>|<organism>`). Both are opaque join keys. Gene symbol,
collection, and organism are NOT stored in this table — they join from their
master tables. Nothing to embed here.

**`gene_master_table_msigdb`** (2 queryable cols)

- `gene_symbol` → `MYC`, `EGFR`, `TP53`, `KRAS`, `BRCA1`
  *HGNC (human) or MGI (mouse) gene symbol. Primary lookup field for gene entities.
  85,460 unique values. Handled via concept_values_msigdb.pkl + expand_and_match_db.*
- `gene_organism` → `Homo sapiens`, `Mus musculus`, `Rattus norvegicus`
  *Organism for this gene entry. 3 unique values. Species filter.*

**`geneset_master_table_msigdb`** (4 queryable cols)

- `geneset_name` → `HALLMARK_APOPTOSIS`, `GOBP_RESPONSE_TO_X_RAY`, `REACTOME_ACTIVATION_OF_NMDA_RECEPTORS_AND_POSTSYNAPTIC_EVENTS`, `WP_GLYCOLYSIS_IN_SENESCENCE`
  *Gene set / signature name. The source database / ontology is encoded as the
  NAME PREFIX (HALLMARK_, GOBP_, REACTOME_, KEGG_, WP_, etc.). 39,897 unique
  values. Primary semantic search target.*
- `collection` → `Hallmark`, `Positional`, `Curated`, `Regulatory`, `Computational`, `Ontology`, `Oncogenic`, `Immunologic`, `CellType`, `CellLineage`
  *MSigDB collection in friendly-name form. 10 unique values. NOTE: source DBs
  (KEGG, REACTOME, GO, BIOCARTA, WikiPathways) are NOT collection values — they
  live in geneset_name prefixes and sub_collection. Route source-DB queries to
  geneset_name (LIKE prefix) or sub_collection, NEVER to collection.*
- `gene_count` → `5`, `10`, `100`, `131`, `1998`
  *Number of unique genes in the set (String type). 1,377 unique values.*
- `geneset_organism` → `Homo sapiens`, `Mus musculus`, `Rattus norvegicus`
  *Organism the gene set was defined for. 3 unique values.*

**`geneset_metadata_msigdb`** (3 queryable cols)

- `sub_collection` → `CGP`, `CP:REACTOME`, `CP:BIOCARTA`, `CP:KEGG_MEDICUS`, `CP:WIKIPATHWAYS`, `GO:BP`, `GO:CC`, `GO:MF`, `HPO`, `IMMUNESIGDB`, `MIR:MIRDB`, `TFT:GTRD`, `3CA`, `VAX`
  *Sub-collection — the precise source DB / category within the parent collection.
  24 unique values. This is the exact discriminator for source-DB queries:
  'KEGG canonical pathways' → sub_collection LIKE 'CP:KEGG%'; 'GO biological
  process' → 'GO:BP'; 'Reactome' → 'CP:REACTOME'. Lives only in this metadata
  table (normalized out of geneset_master_table_msigdb).*
- `brief_description` → `Genes down-regulated in AtT20 cells after LIF treatment`, `Major ELAVL4 associated mRNA targets`, `Genes up-regulated in hepatocellular carcinoma`
  *Short free-text description of the gene set. 38,624 unique values. Highly
  informative for semantic search.*
- `full_description` → (long free-text, ~8,387 non-null values)
  *Full free-text description of the gene set. NULL for ~34.3k of 52,429 sets.
  Useful for deep semantic matching when populated.*

**Columns NOT queryable / SKIP for direct embedding:**

- `geneset_metadata_msigdb.collection` — SKIP (see Disagreements Resolved above):
  denormalized mirror of `geneset_master_table_msigdb.collection`; filter via
  the master table.
- `gene_geneset_association_msigdb.gene_symbol`, `.collection`, `.organism` —
  these columns do NOT EXIST in the 3NF schema (removed in 2026-06-24 MSigDB
  normalization). The association table is a pure FK edge table.
- `geneset_metadata_msigdb.pmid`, `.geo_id` — included in concept_values_msigdb.pkl
  (1,649 and 863 unique values respectively) for value-mapper lookup but are
  opaque identifiers, not free-text semantic search targets.

### OMNIPATH (4 tables, 7 cols)

**`annotations_omnipath`**
- `source` → `HGNC`, `DisGeNet`, `CancerGeneCensus`, `Guide2Pharma`
  *Database/resource name with biomedical meaning (e.g. HGNC, DisGeNet, CancerGeneCensus) useful for so*
- `label` → `mainclass`, `location`, `disease`, `pathway_category`
  *Short annotation key with biomedical meaning (e.g. mainclass, location, disease, pathway_category).*

**`complex_omnipath`**
- `name` → `NFY`, `mTORC2`, `mTORC1`, `SCF-betaTRCP`
  *Human-readable protein complex name (e.g. mTORC1, NFY, SMAD2/SMAD4) ideal for semantic search.*

**`enz_sub_table_omnipath`**
- `modification` → `phosphorylation`, `ubiquitination`, `acetylation`, `methylation`
  *Categorical PTM type with rich biomedical meaning (phosphorylation, ubiquitination, acetylation, etc*

**`intercell_omnipath`**
- `category` → `receptor`, `ligand`, `cytokine`, `transmembrane`
  *Fine-grained intercellular role category with rich biomedical meaning (receptor, ligand, cytokine, e*
- `parent` → `receptor`, `ligand`, `transmembrane`, `secreted`
  *Broader parent category label (receptor, ligand, transporter, ecm, secreted, etc.) useful for high-l*
- `aspect` → `locational`, `functional`
  *Functional vs locational annotation aspect — short categorical label with biomedical meaning.*

### ORPHANET (9 tables, 19 cols)

**`disease_classification_orphanet`**
- `classification` → `Orphanet classification of rare cardiac diseases`, `Orphanet classification of rare neurological diseases`, `Orphanet classification of rare genetic diseases`, `Orphanet classification of rare gastroenterological diseases`
  *Human-readable Orphanet classification tree name (e.g., rare cardiac vs neurological diseases); usef*
- `disease_name` → `Rare cardiac disease`, `Rare cardiomyopathy`, `Naxos disease`, `Inherited arrhythmogenic cardiomyopathy`
  *Human-readable rare disease name; primary search target for disease lookup.*
- `disorder_type` → `Disease`, `Clinical group`, `Category`, `Clinical syndrome`
  *Categorical biomedical label describing the nature of the disorder (Disease, Syndrome, Category, etc*
- `parent_disease_name` → `Rare cardiac disease`, `Rare cardiomyopathy`, `Inherited arrhythmogenic cardiomyopathy`, `Inherited isolated arrhythmogenic cardiomyopathy`
  *Human-readable name of the parent disease in the classification hierarchy; useful for hierarchy-awar*

**`disease_epidemiology_orphanet`**
- `disease_name` → `Multiple epiphyseal dysplasia-myopathy syndrome`, `Alexander disease`, `Alpha-mannosidosis`, `Aspartylglucosaminuria`
  *Human-readable rare disease name; primary search target.*
- `prevalence_type` → `Point prevalence`, `Annual incidence`, `Cases/families`, `Lifetime Prevalence`
  *Biomedical categorical label describing the type of prevalence estimate.*
- `prevalence_class` → `<1 / 1 000 000`, `1-9 / 1 000 000`, `1-9 / 100 000`, `1-5 / 10 000`
  *Categorical prevalence range string meaningful for clinical rarity queries.*
- `geographic` → `Worldwide`, `Japan`, `Malta`, `Cameroon`
  *Human-readable country or region name indicating where the prevalence was measured.*

**`disease_master_table_orphanet`**
- `disease_name` → `Multiple epiphyseal dysplasia-myopathy syndrome`, `Brachydactyly-short stature-retinitis pigmentosa syndrome`, `Aspartylglucosaminuria`, `Multiple sulfatase deficiency`
  *Human-readable rare disease name; primary lookup target for the master entity table.*

**`disease_natural_history_orphanet`**
- `disease_name` → `Multiple epiphyseal dysplasia-myopathy syndrome`, `Alexander disease`, `Alpha-mannosidosis`, `Brachydactyly-short stature-retinitis pigmentosa syndrome`
  *Human-readable source disease name.*
- `target_disease_name` → `Rare bone disease`, `Rare neurologic disease`, `Rare inborn errors of metabolism`, `Rare cardiac disease`
  *Human-readable name of the broader disease category this disease is linked to.*

**`disease_onset_inheritance_orphanet`**
- `disease_name` → `Multiple epiphyseal dysplasia-myopathy syndrome`, `Alexander disease`, `Alpha-mannosidosis`, `Aspartylglucosaminuria`
  *Human-readable rare disease name.*
- `value` → `Infancy`, `Neonatal`, `Autosomal recessive`, `Childhood`
  *Biomedically meaningful categorical string for onset age or inheritance pattern; useful for phenotyp*

**`disease_phenotype_association_orphanet`**
- `hpo_term` → `Macrocephaly`, `Intellectual disability`, `Seizure`, `Spasticity`
  *Human-readable HPO phenotype term name; core target for phenotype similarity search.*
- `frequency` → `Very frequent (99-80%)`, `Frequent (79-30%)`, `Occasional (29-5%)`, `Obligate (100%)`
  *Categorical frequency label (Very frequent / Frequent / Occasional / etc.) with clinical meaning for*

**`disease_xref_orphanet`**
- `disease_name` → `Multiple epiphyseal dysplasia-myopathy syndrome`, `Alpha-mannosidosis`, `Aspartylglucosaminuria`, `Beta-mannosidosis`
  *Human-readable rare disease name.*

**`gene_disease_association_orphanet`**
- `gene_name` → `kinesin family member 7`, `CWC27 spliceosome associated cyclophilin`, `aspartylglucosaminidase`, `sulfatase modifying factor 1`
  *Full human-readable gene name; enriches semantic matching beyond the short symbol.*
- `association_type` → `Disease-causing germline mutation(s) (loss of function) in`, `Disease-causing germline mutation(s) (gain of function) in`, `Major susceptibility factor in`, `Disease-causing germline mutation(s) in`
  *Biomedically meaningful categorical label describing how the gene causes or relates to the disease.*

**`gene_master_table_orphanet`**
- `gene_name` → `kinesin family member 7`, `CWC27 spliceosome associated cyclophilin`, `aspartylglucosaminidase`, `sulfatase modifying factor 1`
  *Full human-readable gene name; primary semantic text for gene search.*

### PHARMGKB (12 tables, 43 cols)

**`clinical_ann_alleles_pharmgkb`**
- `Annotation Text` → `Patients with the GT genotype and chronic hepatitis C may have an increased likelihood of sustained virological response when treated with peginterferon alfa-2b and ribavirin as compared to patients with the GG genotype.`, `Patients with the rs9923231 TT genotype may have an increased risk of bleeding when treated with warfarin as compared to the CC genotypes.`, `Patients with the CT genotype may have a decreased risk for flucloxacillin-induced liver injury as compared to patients with the CC genotype.`, `Patients carrying the CYP2D6*4 allele may be at a decreased risk of developing opioid dependence as a result of taking oxycodone.`
  *Rich free-text clinical sentences describing genotype-drug-outcome relationships — high semantic val*
- `Allele Function` → `No function`, `Normal function`, `Unknown function`, `Uncertain function`
  *Categorical biomedical label describing functional consequence of an allele — meaningful for semanti*

**`clinical_ann_evidence_pharmgkb`**
- `Evidence Type` → `Variant Phenotype Annotation`, `Variant Drug Annotation`, `Guideline Annotation`, `Label Annotation`
  *Categorical label describing the type of pharmacogenomics evidence — biomedically meaningful enum.*
- `Summary` → `CYP2D6 normal metabolizer is associated with decreased number of failed medication trails when treated with citalopram, clomipramine, duloxetine, escitalopram, fluoxetine in people with Obsessive-Compulsive Disorder.`, `CYP2C9 *1/*3 is associated with increased toxicity when exposed to phenytoin.`, `Genotype CT is associated with increased response to irbesartan in people with Hypertension as compared to genotype CC.`, `Genotype CC is associated with decreased severity of Anemia when treated with docetaxel in people with Nasopharyngeal Neoplasms.`
  *Free-text sentence summarising a pharmacogenomics finding — high semantic search value.*

**`clinical_ann_history_pharmgkb`**
- `Comment` → `Updated OMB race to appropriate terminology`, `CA score added as part of scoring revision`, `Updated OMB race to appropriate terminology`, `CA score added as part of scoring revision`
  *Short free-text describing what changed in the annotation — useful for audit/provenance search.*

**`clinical_annotation_pharmgkb`**
- `Phenotype Category` → `Efficacy`, `Toxicity`, `Dosage`, `Metabolism/PK`
  *Categorical label describing the class of pharmacogenomic phenotype — biomedically meaningful.*
- `Drug(s)` → `warfarin`, `exemestane`, `lopinavir`, `paliperidone`
  *Drug name(s) — core search entity for pharmacogenomics queries.*
- `Phenotype(s)` → `Multiple Sclerosis`, `Thrombocytopenia`, `Cough`, `Breast Neoplasms`
  *Human-readable disease/phenotype names — key semantic search target.*

**`drug_gene_association_pharmgkb`**
- `association` → `associated`, `ambiguous`, `not associated`
  *Categorical biomedical label describing the nature of the drug-gene relationship.*
- `evidence` → `ClinicalAnnotation,VariantAnnotation`, `ClinicalAnnotation,GuidelineAnnotation,LabelAnnotation,MultilinkAnnotation,VariantAnnotation`, `GuidelineAnnotation,MultilinkAnnotation,VariantAnnotation`, `LabelAnnotation,VariantAnnotation`
  *Comma-separated list of evidence type labels — biomedically informative categorical annotation.*

**`drug_label_pharmgkb`**
- `Name` → `Annotation of FDA Label for duvelisib`, `Annotation of HCSC Label for raltegravir and UGT1A1`, `Annotation of EMA Label for nateglinide`, `Annotation of CPIC Guideline for warfarin and CYP2C9`
  *Human-readable label annotation name describing drug-gene relationship and source — searchable.*
- `Source` → `FDA`, `EMA`, `HCSC`, `Swissmedic`
  *Regulatory/guideline source organization name — useful categorical label.*
- `Biomarker Flag` → `On FDA Biomarker List`, `Formerly on FDA Biomarker List`
  *Categorical biomarker status label with clinical regulatory meaning.*
- `Testing Level` → `Testing Required`, `Testing Recommended`, `Actionable PGx`, `Informative PGx`
  *Categorical clinical PGx testing recommendation — biomedically meaningful enum.*
- `Chemicals` → `nateglinide`, `glycerol phenylbutyrate`, `nusinersen`, `ospemifene`
  *Human-readable drug/chemical names — core search entity.*

**`drug_master_table_pharmgkb`**
- `drug_name` → `nabilone`, `alfacalcidol`, `calcitriol`, `ombitasvir`
  *Human-readable drug name — primary search entity.*

**`guideline_annotation_pharmgkb`**
- `name` → `Annotation of CPIC Guideline for pravastatin and SLCO1B1`, `Annotation of DPWG Guideline for venlafaxine and CYP2D6`, `Annotation of CPIC Guideline for abacavir and HLA-B`, `Annotation of DPWG Guideline for fluvastatin and SLCO1B1`
  *Human-readable guideline annotation title naming drug, gene, and issuing body — highly searchable.*
- `source` → `CPIC`, `DPWG`, `AIOM`, `RNPGx`
  *Guideline-issuing organization name — meaningful categorical label.*
- `drugs` → `aripiprazole`, `mirtazapine`, `fluoxetine`, `phenprocoumon`
  *Human-readable drug name(s) — core PGx search entity.*
- `summary` → `The CPIC Dosing Guideline recommends increasing starting daily dose and monitoring efficacy in CYP2C19 ultrarapid metabolizer.`, `Select an alternative drug or reduce the initial dose of azathioprine for patients that are NUDT15 poor metabolizers.`, `Select an alternative drug or reduce the initial dose of mercaptopurine for patients that are TPMT poor metabolizers.`, `Alternate non-codeine analgesics are recommended for CYP2D6 ultrarapid and poor metabolizers.`
  *HTML-formatted free-text guideline summary — rich semantic content for search despite markup.*

**`variant_drug_annotation_pharmgkb`**
- `drug_s` → `nifedipine`, `sitagliptin`, `warfarin`, `heroin`
  *Human-readable drug name(s) — core pharmacogenomics search entity.*
- `phenotype_category` → `Efficacy`, `Toxicity`, `Metabolism/PK`, `Dosage`
  *Categorical biomedical label for the type of pharmacogenomic phenotype.*
- `sentence` → `Genotype GG is not associated with metabolism of tacrolimus in children with Kidney Transplantation.`, `Genotype AG is associated with decreased response to alprazolam in men with Alcoholism and Anxiety Disorders.`, `Allele A is associated with increased discontinuation of tamoxifen in women with Breast Neoplasms.`, `Allele T is not associated with response to clomipramine in people with Obsessive-Compulsive Disorder.`
  *Structured natural-language sentence summarizing the variant-drug association — highest semantic val*
- `metabolizer_types` → `poor metabolizer`, `normal metabolizer`, `ultrarapid metabolizer`, `intermediate metabolizer`
  *Categorical PGx metabolizer phenotype label — biomedically meaningful and searchable.*
- `direction_of_effect` → `increased`, `decreased`
  *Categorical label indicating direction of pharmacogenomic effect.*
- `pd_pk_terms` → `steady-state concentration of`, `resistance to`, `clinical benefit to`, `dose-adjusted trough concentrations of`
  *Short pharmacodynamic/pharmacokinetic descriptor phrases — meaningful biomedical text.*
- `population_phenotypes_or_diseases` → `Other:Spondylitis, Ankylosing`, `Disease:Epilepsy`, `Disease:Liver transplantation`, `Other:ICU patients`
  *Disease/phenotype names for the study population — human-readable biomedical labels.*

**`variant_drug_association_pharmgkb`**
- `phenotype_category` → `Efficacy`, `Toxicity`, `Dosage`, `Metabolism/PK`
  *Categorical label for pharmacogenomic phenotype class — biomedically meaningful.*

**`variant_fa_annotation_pharmgkb`**
- `drug_s` → `normeperidine`, `bupropion`, `warfarin`, `ranitidine`
  *Human-readable drug/chemical name(s) — core pharmacogenomics search entity.*
- `phenotype_category` → `Efficacy`, `Toxicity`, `Other`, `Metabolism/PK`
  *Categorical biomedical label for the type of pharmacogenomic phenotype.*
- `sentence` → `Genotype AA is associated with decreased activity of DPYD as compared to genotype CC.`, `Allele T is associated with decreased catalytic activity of CYP4F2 when assayed with arachidonic acid.`, `CYP2C9 *60 is associated with decreased enzyme activity of CYP2C9 when assayed with warfarin.`, `CYP2C9 *45 is associated with decreased clearance of phenytoin as compared to CYP2C9 *1.`
  *Structured natural-language sentence summarizing the functional assay association — high semantic va*
- `assay_type` → `in liver microsomes`, `Expressed protein`, `qRT_PCR`, `PBMCs from healthy controls`
  *Short description of the functional assay type — biomedically informative label.*
- `metabolizer_types` → `poor metabolizer`, `normal metabolizer`, `rapid acetylator`, `slow acetylator`
  *PGx metabolizer phenotype label.*
- `direction_of_effect` → `increased`, `decreased`
  *Categorical effect direction label.*
- `functional_terms` → `glucuronidation of`, `affinity to`, `steady-state level of`, `half-life of`
  *Short pharmacological/biochemical descriptor phrases — meaningful biomedical text.*
- `cell_type` → `in liver microsomes`, `in xenopus oocytes`, `High-grade osteosarcoma cells`, `in CEPH cell lines`
  *Human-readable cell type or biological system used in assay — meaningful biomedical label.*

**`variant_phenotype_annotation_pharmgkb`**
- `drug_s` → `lamotrigine`, `nicotine`, `sacituzumab govitecan`, `loperamide`
  *Human-readable drug name(s) — core pharmacogenomics search entity.*
- `phenotype_category` → `Efficacy`, `Toxicity`, `Other`, `Dosage`
  *Categorical biomedical label for pharmacogenomic phenotype class.*
- `sentence` → `CYP2C19 *2/*2 + *2/*1 are associated with increased risk of treatment emergent mania when treated with amitriptyline in people with Bipolar Disorder.`, `Genotype TT is associated with increased risk of aspirin-intolerant asthma when exposed to aspirin in people with Asthma.`, `Allele A is not associated with trough concentrations of tacrolimus in people with Kidney Transplantation.`, `Allele G is not associated with overall survival (OS) time when treated with gefitinib in people with Adenocarcinoma.`
  *Structured natural-language sentence summarizing a variant-phenotype association — highest semantic *
- `metabolizer_types` → `poor metabolizer`, `normal metabolizer`, `ultrarapid metabolizer`, `intermediate metabolizer`
  *PGx metabolizer phenotype label.*
- `direction_of_effect` → `increased`, `decreased`
  *Categorical effect direction label.*
- `phenotype` → `Side Effect:Hyperbilirubinemia`, `Efficacy: rate of high on-treatment platelet reactivity (HTPR) at 1 month of treatment`, `Efficacy:Recurrence free survival`, `Side Effect:Somnolence`
  *Human-readable phenotype/outcome description — key biomedical semantic content for search.*
- `population_phenotypes_or_diseases` → `Disease:Epilepsy`, `Disease:Ulcerative Colitis, Disease:Crohn Disease`, `Disease:Carcinoma, Squamous Cell`, `Other:Pancreatic Neoplasms, Other:Metastatic neoplasm`
  *Disease/phenotype names for the study population — human-readable biomedical labels.*

### PUBTATOR (5 tables, 5 cols)

**`cellline_master_table_pubtator`**
- `mentions` → `BC1`, `Huh-28`, `BZR 736`, `AU565`
  *Human-readable cell line names/aliases (pipe-delimited synonyms) useful for semantic lookup.*

**`chemical_master_table_pubtator`**
- `mentions` → `sodium hydroxide|NaOH`, `cholesteryl ester|CE`, `leflunomide`, `Amodiaquine`
  *Pipe-delimited chemical names and synonyms (e.g. 'sodium hydroxide|NaOH') — core human-readable labe*

**`disease_master_table_pubtator`**
- `mentions` → `CRS|Congenital Rubella Syndrome`, `pulmonary dysmaturation syndrome`, `Lyme disease`, `tobacco and alcohol use disorders|nicotine dependence`
  *Pipe-delimited disease names and synonyms (e.g. 'Lyme disease', 'Congenital Rubella Syndrome|CRS') —*

**`relation_pubtator`**
- `relation_type` → `associate`, `treat`, `inhibit`, `stimulate`
  *Categorical biomedical relation labels (e.g. 'treat', 'inhibit', 'associate') — meaningful enum stri*

**`species_master_table_pubtator`**
- `mentions` → `Picea mariana`, `Trichoderma reesei`, `Lactococcus lactis`, `Rous Sarcoma Virus`
  *Human-readable organism names (e.g. 'Homo sapiens', 'Saccharomyces cerevisiae') — useful for semanti*

### REACTOME (5 tables, 5 cols)

**`chebi_pathway_reactome`**
- `pathway_name` → `Disease`, `Infectious disease`, `Action of antimicrobials and antimicrobial resistance`, `Biosynthesis of DPA-derived SPMs`
  *Human-readable biological pathway name — ideal for semantic search.*

**`ensembl_pathway_reactome`**
- `pathway_name` → `Synthesis of dolichyl-phosphate mannose`, `Defective DPM1 causes DPM1-CDG`, `FCGR3A-mediated IL10 synthesis`, `trans-Golgi Network Vesicle Budding`
  *Human-readable biological pathway name — ideal for semantic search.*

**`ncbi_pathway_reactome`**
- `pathway_name` → `Platelet degranulation`, `Neutrophil degranulation`, `Acetylation`, `Paracetamol ADME`
  *Human-readable biological pathway name — ideal for semantic search.*

**`pathway_master_table_reactome`**
- `pathway_name` → `2-LTR circle formation`, `3-Methylcrotonyl-CoA carboxylase deficiency`, `5-Phosphoribose 1-diphosphate biosynthesis`, `ABC transporter disorders`
  *Human-readable biological pathway name — ideal for semantic search.*

**`uniprot_pathway_reactome`**
- `pathway_name` → `Hemostasis`, `Adaptive Immune System`, `Disease`, `Complement cascade`
  *Human-readable biological pathway name — ideal for semantic search.*

### STRING (1 tables, 2 cols)

**`protein_master_table_string`**
- `gene_symbol` → `ARF5`, `M6PR`, `FKBP4`, `CYP26B1`
  *Human-readable HGNC gene symbol — primary query term for semantic protein/gene lookup.*
- `annotation` → `ADP-ribosylation factor 5; GTP-binding protein involved in protein trafficking; may modulate vesicle budding and uncoating within the Golgi apparatus.`, `Cation-dependent mannose-6-phosphate receptor; Transport of phosphorylated lysosomal enzymes from the Golgi complex and the cell surface to lysosomes.`, `Peptidyl-prolyl cis-trans isomerase FKBP4; Immunophilin protein with PPIase and co-chaperone activities.`, `Cytochrome P450 26B1; Involved in the metabolism of retinoic acid (RA), rendering this classical morphogen inactive through oxidation.`
  *Free-text protein description including function, mechanism, and family — rich semantic content for *

### TRRUST (3 tables, 4 cols)

**`gene_master_table_trrust`**
- `gene_symbol` → `A2M`, `ABCA1`, `ABCB1`, `SMARCB1`
  *Human-readable HGNC gene symbols used for semantic lookup of target genes.*

**`tf_gene_association_trrust`**
- `regulation_type` → `Activation`, `Repression`, `Unknown`
  *Biomedically meaningful categorical label describing TF regulatory action on target gene.*
- `organism` → `Homo sapiens`, `Mus musculus`
  *Human-readable organism name useful for filtering queries by species.*

**`tf_master_table_trrust`**
- `tf_name` → `AATF`, `ABL1`, `AES`, `AHR`
  *Human-readable transcription factor gene symbols used for semantic lookup.*

### TTD (11 tables, 15 cols)

**`biomarker_master_table_ttd`**
- `biomarker_name` → `Cytochrome P450 2C19 (CYP2C19)`, `GTPase KRas (KRAS)`, `Erbb2 tyrosine kinase receptor (HER2)`, `Proliferation marker protein Ki-67 (MKI67)`
  *Human-readable biomarker names including protein names and gene symbols — ideal for semantic search.*

**`disease_master_table_ttd`**
- `disease_name` → `Cholera`, `Bacillary dysentery`, `Escherichia coli intestinal infection`, `Clostridium difficile enterocolitis`
  *Human-readable disease names — ideal for semantic disease search.*

**`drug_crossmatching_association_ttd`**
- `drug_name` → `8-O-(4-chlorobenzenesulfonyl)manzamine F`, `KW-2449`, `ND1251`, `Hydroxyprogesterone`
  *Human-readable drug/compound names — useful for semantic drug lookup.*

**`drug_master_table_ttd`**
- `drug_name` → `8-O-(4-chlorobenzenesulfonyl)manzamine F`, `KW-2449`, `Opterone`, `ND1251`
  *Human-readable drug names — primary lookup field for semantic drug search.*

**`drug_synonyms_association_ttd`**
- `drug_name` → `8-O-(4-chlorobenzenesulfonyl)manzamine F`, `3-[1-ethyl-2-(3-hydroxyphenyl)butyl]phenol`, `KW-2449`, `Opterone`
  *Canonical drug name — human-readable text for semantic matching.*
- `synonym` → `Metahexestrol`, `Metahexes trol`, `3,3'-Hexestrol`, `meso-3,4-Bis(3'-hydroxyphenyl)hexane`
  *Drug synonyms and aliases — some are human-readable alternative names and trade names worth embeddin*

**`P1-05-Drug_disease`**
- `approval_status` → `Approved`, `Phase 3`, `Phase 2`, `Phase 1`
  *Clinical approval status labels — biomedically meaningful categorical strings suitable for semantic *

**`P1-07-Drug-TargetMapping`**
- `drug_mechanism_of_action_on_target` → `Modulator`, `Agonist`, `Inhibitor`, `Activator`
  *Human-readable mechanism-of-action labels — biomedically meaningful categorical strings for semantic*

**`pathway_master_table_ttd`**
- `pathway_name` → `Glycolysis / Gluconeogenesis`, `Citrate cycle (TCA cycle)`, `Pentose phosphate pathway`, `Pentose and glucuronate interconversions`
  *Human-readable KEGG pathway names — ideal for semantic pathway search.*

**`target_compound_activity_association_ttd`**
- `gene_symbol` → `PON1`, `ALOX5`, `TGFA`, `CTGF`
  *Human gene symbols — human-readable identifiers useful for semantic gene lookup.*

**`target_master_table_ttd`**
- `target_name` → `Osteopontin (SPP1)`, `Transforming growth factor alpha (TGFA)`, `CTGF messenger RNA (CTGF mRNA)`, `Arachidonate 5-lipoxygenase (5-LOX)`
  *Human-readable protein/target names including common names and gene symbols — primary field for sema*
- `gene_symbol` → `SPP1`, `TGFA`, `CTGF`, `ALOX5`
  *Human gene symbols — human-readable identifiers useful for semantic gene lookup.*

**`target_uniprot_association_ttd`**
- `target_name` → `Osteopontin (SPP1)`, `Transforming growth factor alpha (TGFA)`, `Fungal Sterol 24-C-methyltransferase (Fung erg6)`, `CTGF messenger RNA (CTGF mRNA)`
  *Human-readable protein/target names — primary field for semantic target search.*
- `target_type` → `Literature-reported target`, `Clinical trial target`, `Successful target`, `Preclinical target`
  *Categorical target classification labels with biomedical meaning — useful for semantic filtering by *
- `gene_symbol` → `SPP1`, `TGFA`, `CTGF`, `CDC42BPA`
  *Human gene symbols — human-readable identifiers useful for semantic gene lookup.*

### UNIPROT (8 tables, 20 cols)

**`gene_ontology_uniprot`**
- `gene_symbol` → `NUDT4B`, `YWHAB`, `YWHAE`, `SFN`
  *Human-readable gene symbol used for semantic search.*
- `qualifier` → `enables`, `located_in`, `involved_in`, `part_of`
  *Categorical GO relation label with biomedical meaning (enables, involved_in, located_in, etc.).*

**`gene_protein_xwalk_uniprot`**
- `gene_symbol` → `YWHAB`, `YWHAE`, `YWHAH`, `YWHAG`
  *Human-readable gene symbol enabling semantic gene lookup.*

**`id_mapping_uniprot`**
- `db` → `UniProtKB-ID`, `Gene_Name`, `RefSeq`, `RefSeq_NT`
  *Database source name label (Gene_Name, RefSeq, Ensembl, UniProtKB-ID) — categorical string useful fo*

**`keyword_master_uniprot`**
- `keyword_name` → `2Fe-2S`, `3D-structure`, `Abscisic acid biosynthesis`, `Acetoin catabolism`
  *Human-readable UniProt keyword label (e.g. disease, process, function terms).*
- `category` → `Biological process`, `Disease`, `Molecular function`, `Cellular component`
  *Top-level keyword category label with biomedical meaning (Biological process, Disease, Molecular fun*
- `description` → `Protein which contains at least one 2Fe-2S iron-sulfur cluster.`, `Protein whose three-dimensional structure has been resolved experimentally.`, `Protein which contains at least one 3Fe-4S iron-sulfur cluster.`, `Protein involved in the synthesis of abscisic acid.`
  *Short free-text definition of the keyword — rich biomedical content for semantic retrieval.*
- `synonyms` → `[2Fe-2S] cluster; [Fe2S2] cluster; 2 iron, 2 sulfur cluster binding`, `ABA anabolism; ABA biosynthesis; ABA formation`, `3-hydroxy-2-butanone anabolism; Acetoin anabolism`, `ABA mediated signaling; ABA signaling pathway`
  *Synonym strings for the keyword — expands semantic coverage.*

**`protein_master_table_uniprot`**
- `protein_name` → `14-3-3 protein beta/alpha`, `14-3-3 protein epsilon`, `14-3-3 protein eta`, `14-3-3 protein gamma`
  *Full human-readable protein name — primary semantic search target.*
- `gene_symbol` → `YWHAB`, `YWHAE`, `YWHAH`, `YWHAG`
  *Human-readable gene symbol — key biomedical search term.*
- `organism` → `Homo sapiens (Human)`
  *Human-readable organism name (e.g. Homo sapiens (Human)) — useful for species-aware semantic queries*

**`species_master_uniprot`**
- `scientific_name` → `Aedes albopictus densovirus (isolate Boublik/1994)`, `Adeno-associated virus 2`, `Abaeis boisduvaliana`, `Homo sapiens`
  *Full scientific species name — human-readable and useful for organism-aware semantic search.*
- `common_name` → `AalDNV`, `AAV-2`, `Boisduval's yellow butterfly`, `Millipede`
  *Common organism name (Human, Mouse, Rat, etc.) — important alias for semantic species search.*
- `synonym` → `Eurema boisduvaliana`, `Homo sapiens`, `Mus musculus domesticus`, `Canis familiaris`
  *Alternative species name — expands semantic coverage for organism matching.*

**`subcell_location_uniprot`**
- `subcell_name` → `A band`, `Acidocalcisome`, `Acidocalcisome lumen`, `Acidocalcisome membrane`
  *Human-readable subcellular compartment name — key biomedical term for localization queries.*
- `description` → `The appearance of the striated muscle sarcomere under polarized light.`, `The acidocalcisome is an electron-dense organelle.`, `The acrosome is a large lysosome-related organelle.`, `The membrane of the acrosome.`
  *Free-text definition of the subcellular location — rich content for semantic retrieval.*
- `synonyms` → `A-band; A line.`, `Acidocalcisomal lumen.`, `Acrosomal vesicle.`, `Acrosomal inner membrane; IAM.`
  *Alternative names for the subcellular location — expands semantic coverage.*
- `keyword` → `Nucleus`, `Membrane`, `Cytoplasm`, `Mitochondrion`
  *Associated UniProt keyword label — human-readable biomedical term.*

**`variant_disease_uniprot`**
- `gene_symbol` → `A1BG`, `A1CF`, `A2M`, `AAAS`
  *Human-readable gene symbol associated with the variant — primary search term.*
- `disease_name` → `Achalasia-addisonianism-alacrima syndrome (AAAS) [MIM:231550]`, `Charcot-Marie-Tooth disease, axonal, type 2N (CMT2N) [MIM:613287]`, `Developmental and epileptic encephalopathy 29 (DEE29) [MIM:616339]`, `Breast cancer [MIM:114480]`
  *Full human-readable disease name including MIM number — key biomedical entity for semantic search.*

### WIKIPATHWAYS (2 tables, 7 cols)

**`pathway_master_table_wikipathways`**
- `pathway_name` → `Glutathione metabolism`, `Alanine and aspartate metabolism`, `Translation factors`, `Electron transport chain OXPHOS system in mitochondria`
  *Human-readable biological pathway names directly useful for semantic search.*

**`pathway_interaction_wikipathways`**
- `pathway_name` → `10q11.21q11.23 copy number variation syndrome`, `Glutathione metabolism`, `Alanine and aspartate metabolism`, `Translation factors`
  *Human-readable biological pathway names useful for semantic search.*
- `source_label` → `ERCC6`, `CHAT`, `TMEM273`, `PCNA`
  *Human-readable gene/protein/metabolite names for the interaction source node.*
- `source_type` → `GeneProduct`, `Protein`, `Metabolite`, `Complex`
  *Categorical biomedical entity type labels (e.g. GeneProduct, Metabolite) with semantic meaning.*
- `target_label` → `RIF1`, `PARG`, `EIF4ENIF1`, `Apoptosis`
  *Human-readable gene/protein/process names for the interaction target node.*
- `target_type` → `GeneProduct`, `Protein`, `Metabolite`, `Complex`
  *Categorical biomedical entity type labels for the target node with semantic meaning.*
- `interaction_type` → `mim-binding`, `mim-catalysis`, `mim-stimulation`, `mim-inhibition`
  *Controlled vocabulary interaction type labels (mim-binding, mim-inhibition, mim-stimulation) with bi*