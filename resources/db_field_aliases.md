# Per-Database Field Alias Map

**This file is the source of truth.** [app/utils/parsed_value_canonical.py](../app/utils/parsed_value_canonical.py) parses it at module import time and applies the aliases additively after the cross-DB synonym normalisation.

## Purpose

The interpreter emits **canonical** field names (`disease_name`, `gene_symbol`, `drug_name`, …) that the LLM has been taught are the universal vocabulary. But each underlying database stores those concepts under its own column names. Without translation, a perfectly valid interpreter output gets rejected by a DB whose schema doesn't use the canonical word — that's why `Which drugs are approved for hypertension?` made ChEMBL fail with `Filter keys {'disease_name'} have NO overlap with DB schema columns`.

This map fixes that **additively**: when the interpreter emits `disease_name="hypertension"` and the target DB is ChEMBL, the canonicaliser also populates `mesh_heading="hypertension"`. The original `disease_name` key is **never removed** — any legacy per-DB routing that reads it continues to work.

## Format (machine-parsed)

```
## <db_slug>                                    # lowercase, matches db arg to canonicalize()

| Interpreter field | DB column     | Origin                          | Rationale         |
|-------------------|---------------|---------------------------------|-------------------|
| disease_name      | mesh_heading  | drug_indication.mesh_heading    | <why this maps>   |
```

Rules the parser enforces:
- Lowercase `## db_slug` heading per database.
- Markdown table inside that section with **at least** the first two columns. Header text doesn't have to be exactly `Interpreter field` / `DB column` — the parser uses column position 0 → interpreter field, position 1 → DB column.
- The header row + the `|---|` separator row are ignored. All remaining rows are data.
- `Origin` and `Rationale` columns are documentation-only; not read by code.
- Comments and prose between tables are ignored by the parser.
- Entries with an empty interpreter field or DB column are silently skipped.
- DB sections without a markdown table are documentation-only (no aliases applied).

## Semantics (what the parser does at runtime)

For each `(interpreter_field, db_column)` row in the active DB's table:

1. If `parsed_value[interpreter_field]` has a real value (not `None`, not the `"requested"` output-marker sentinel) **and** `parsed_value[db_column]` is missing or empty:
   → copy `parsed_value[interpreter_field]` into `parsed_value[db_column]`.
2. If `parsed_value[db_column]` is already populated with a real value:
   → leave both alone (downstream code may have set it explicitly).
3. The source key (`interpreter_field`) is **never removed**.

Order of passes: cross-DB synonyms (`ALIAS_MAP` in parsed_value_canonical.py) → per-DB aliases (this file).

## Audit summary (2026-05-17, updated 2026-06-18)

Active DBs: TTD, CTD, HCDT, ChEMBL, ClinVar, HPO, Orphanet, Reactome, STRING, UniProt, OpenTargets.

Of the universal canonical fields (`disease_name`, `gene_symbol`, `drug_name`, `pathway_name`, `phenotype_name`, `variant_name`, `mesh_term`, `protein_name`, `accession`):

- **Direct match (no alias needed)** — the DB's own column name IS the canonical name: TTD, ClinVar, HPO, Orphanet, Reactome, STRING, UniProt.

- **Handled by per-DB shims in the tool itself** — these tools have their own `normalize_*_parsed_value()` function that does field renaming + smart list merging + unsupported-field stripping that goes beyond what this markdown can express. The shims are documented below for completeness but live in code: CTD, HCDT.

- **Handled by this markdown** — the DB stores the concept under a column whose name doesn't match the canonical field, AND a simple additive copy is sufficient: ChEMBL (for `disease_name`→`mesh_heading`).

---

# Per-DB tables

## ttd

Therapeutic Target Database. Schema columns match canonical interpreter fields directly: `drug_name`, `gene_symbol`, `target_name`, `disease_name`, `pathway_name`, `biomarker_name`, `approval_status`, `activity_type`/`activity_value`/`activity_unit`/`activity_operator`, `cas_number`, `pubchem_cid`, `chebi_xref`, `uniprot_xref`, `formula`, `synonym`, `drug_mechanism_of_action_on_target`, `target_type`. **No aliases needed.**

## ctd

Comparative Toxicogenomics Database. Handled by `normalize_ctd_parsed_value()` in [app/tools/ctd/app/ctd.py](../app/tools/ctd/app/ctd.py) because the rules need smart list merging beyond what this markdown can express. The shim does:

- `gene_name` → merge into `gene_symbol` (CTD's loader renames upstream `GeneSymbol` → `gene_symbol`)
- `target_name` → merge into `gene_symbol`
- `chemical_name` → merge into `drug_name` (CTD's loader renames upstream `ChemicalName` → `drug_name`)
- If a gene filter is present but no drug projection: default `drug_name = "requested"` so the chemical column is returned

Markdown-level entries are intentionally omitted to avoid duplicate writes.

## hcdt

High-Confidence Drug-Target. Handled by `normalize_hcdt_parsed_value()` in [app/tools/hcdt/app/hcdt.py](../app/tools/hcdt/app/hcdt.py):

- `gene_name` → merge into `gene_symbol`
- `target_name` → merge into `gene_symbol`
- Strips fields equal to the literal string `"HCDT"`/`"in HCDT"`/`"from HCDT"` (DB-name leakage from `cleaned_query`)

Markdown-level entries omitted; logic lives in code.

## chembl

ChEMBL is target/compound/bioactivity-centric. Drug–disease links live on the `drug_indication` table keyed by MeSH-controlled vocabulary, not free-text disease names. Without translation, a query like *"drugs for hypertension"* fails the schema-overlap guard with `Filter keys {'disease_name'} have NO overlap`.

| Interpreter field | DB column     | Origin                                          | Rationale |
|-------------------|---------------|-------------------------------------------------|-----------|
| disease_name      | mesh_heading  | `drug_indication_association.mesh_heading`      | Indications in ChEMBL are MeSH-coded; the disease name string lives in `mesh_heading`. |
| disease_id        | mesh_id       | `drug_indication_association.mesh_id`           | MeSH descriptor code (e.g. `D006973` for Hypertension). |

## clinvar

ClinVar variant clinical significance. Columns: `variant_name`, `gene_symbol`, `disease_name`, `variant_type`, `clinical_significance`, `review_status`, `rsid`, `assembly`, `chrom`, `pos`, `ref`, `alt`, `molecular_consequence`, `variant_class`, `submitter`, `date_last_evaluated`, `somatic_clinical_impact`, `oncogenicity`. `variant_name` is in the cross-DB skip list (ClinVar has its own native variant column) so the universal-layer collapse doesn't fire. **No further aliases needed.**

## hpo

Human Phenotype Ontology. Columns: `phenotype_name` (HP terms), `disease_name` (OMIM/Orphanet disease names — distinct from phenotype), `gene_symbol`, `frequency`, `onset`, `sex`, `modifier`, `qualifier`, `evidence`. `phenotype_name` is in the cross-DB skip list so it isn't collapsed into `disease_name`. **No aliases needed.**

## orphanet

Orphanet rare disease catalogue. Columns: `disease_name`, `disease_id`, `gene_name` (HGNC short symbol — e.g. `CFTR`, `NF1`, `TP53` — NOT the long descriptive form; verified against loader + `db_column_descriptions.md` + `interpreter_db_notes.yaml` orphanet block 2026-05-23 audit), `ensembl_accession`, `entrez`, `association_type`, `phenotype_id`, `hpo_term`, `frequency`, `xref_source`, `xref_id`, `prevalence_*`. `gene_name` is in the cross-DB skip list to keep it distinct from the universal `gene_symbol` collapse — Orphanet's worker (`normalize_orphanet_parsed_value` / `_pre_expand`) remaps `gene_symbol → gene_name` internally. **No markdown aliases needed.**

## reactome

Reactome pathway database. Columns: `pathway_name`, `pathway_id`, `gene_symbol`, `uniprot_accession`, `chebi_id`, `ensembl_id`, `ncbi_gene_id`, `gene_pathway_evidence`, `uniprot_pathway_evidence`, `chebi_pathway_evidence`, `ensembl_pathway_evidence`, `ncbi_pathway_evidence`, `parent_id`, `child_id`. All canonical (Reactome's interpreter notes mention `gene_name` as a hint but the schema column is `gene_symbol` which the cross-DB layer already produces). **No aliases needed.**

## string

STRING protein-protein interactions. Columns: `gene_symbol`, `protein_partner_id`, `combined_score`, `protein_size`, `annotation`, `neighborhood`, `fusion`, `cooccurence`, `coexpression`, `experimental`, `database`, `textmining`, `alias`, `source`. All canonical. **No aliases needed.**

## ttd
(see top section)

## uniprot

UniProt protein knowledge base. Columns: `accession` (e.g. `P04637`), `entry_name` (e.g. `P53_HUMAN`), `protein_name`, `gene_symbol`, `ensembl_accession`, `entrez`, `organism`, `keyword_name`, `go_id`, `aspect`, `subcell_name`, `variant_type`, `dbsnp`, `swiss_prot_change`, `disease_name`, `disease_id`. The cross-DB layer maps `uniprot_accession` and `entry_name` → `accession`. All canonical at the per-DB layer. **No aliases needed.**

---

# How to add a new entry

1. Confirm the failure: query a per-DB tool with a canonical interpreter field that DB rejects with `Filter keys {…} have NO overlap`.
2. Find the DB's actual column for that concept (read [config/schema.py](../config/schema.py) or inspect the parquet schema).
3. Add a `(interpreter_field, db_column)` row in the relevant section above; fill `Origin` and `Rationale`.
4. No code change needed. Restart the affected per-DB tool to pick up the markdown:
   ```
   docker restart biochirp_<db>_tool
   ```
   (The markdown is bind-mounted into every per-DB tool + chat container at `/app/resources/db_field_aliases.md`.)

## mirbase

| Interpreter field | DB column | Origin | Rationale |
|---|---|---|---|
| accession | mirbase_accession | mirna_master.mirbase_accession | auto-derived from schema |
| gene_symbol | target_gene_symbol | mirna_target_association.target_gene_symbol | auto-derived from schema |
