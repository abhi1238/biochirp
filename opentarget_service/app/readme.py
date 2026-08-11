from agents import function_tool

@function_tool(
    strict_mode=False,
    name_override="readme_tool",
    description_override=(
        "Information about BioChirp capabilities and supported queries. "
        "Use when users ask what BioChirp can do or need examples."
    ),
)
def readme_tool() -> str:
    """Return BioChirp capability information."""

    return """
# BioChirp OpenTargets Guide

## Tools

**Disease Tool** - Drugs & targets for a disease + rich disease metadata
Returns:
- Drug list + target list with association scores, per-datatype evidence
  breakdown (genetic_association, somatic_mutation, drugs, literature,
  affected_pathway, animal_model, rna_expression), clinical phase, status
- Description, synonyms, dbXrefs, therapeutic areas
- Ontology: parents, children/subtypes, ancestors, descendants
- Phenotypes (HPO), similar diseases (embedding-based), OTAR projects
- Anatomical locations (direct + indirect UBERON, id + name)
- Literature mention count, CSV download
Example: "Give list of Breast cancer drugs", "What are subtypes of melanoma?"

**Drug Tool** - Indications & targets for a drug + rich drug metadata
Returns:
- Disease list + target list with mechanism of action, clinical phase, status
- Description, synonyms, trade names, drug type, max clinical stage
- Cross references, parent + child molecules
- Drug warnings (black-box, withdrawal, year, country, toxicity class)
- Adverse events (top by logLR, MedDRA codes)
- Indications (id + name + max clinical stage)
- Drug-side pharmacogenomics (PharmGKB: category, evidence level, genotype,
  variant, phenotype, target, is_direct_target)
- Similar drugs (embedding-based), literature mention count, CSV download
Example: "What does aspirin treat?", "What are PGx guidelines for imatinib?"

**Target Tool** - Diseases & drugs for a gene/protein + rich target metadata
Returns:
- Disease list + drug list with association scores, per-datatype breakdown,
  phase, status, action types, mechanism of action
- Synonyms, dbXrefs, protein IDs, genomic location, canonical transcript
- Target class, tractability (per modality), subcellular locations
- Genetic constraint (gnomAD pLI/LOEUF/obs/exp)
- GO biological-process / molecular-function / cellular-component
- Tissue expression (GTEx/HPA: RNA value + zscore, protein level/reliability)
- Safety liabilities (event, direction, dosing, biosamples)
- Mouse phenotypes (IMPC), cancer hallmarks, homologues (orthologs/paralogs)
- Chemical probes (high-quality flag, scores), DepMap essentiality, TEP
- PPI interactions (IntAct/Reactome/STRING — count + top by score)
- Similar targets (embedding-based), literature mention count
- GWAS credible sets colocalising with the target (count + top rows)
- Full transcript list (UniProt ID, AlphaFold ID, canonical flag)
- Protein-coding coordinates (per-residue annotations)
- Pharmacogenomics (target-side: variants/genotypes affecting drug response)
- Pathways (Reactome), CSV download
Example: "Diseases associated with TP53", "Tractability of PIK3CA",
"Tissues expressing EGFR", "GWAS credible sets near KRAS"

**OpenTargets GraphQL Tool** - Passthrough for variants / studies /
credible sets / GO terms / clinical reports / batch IDs / map free-text→IDs /
generic OT search / API release metadata
- `variant` — allele freq, VEP effect, transcript consequences, credible sets,
  pharmacogenomics, evidences, enhancer-to-gene predictions
- `study` — GWAS/eQTL metadata, sample sizes, summary-stats location,
  target/biosample (for eQTL), credible sets, QC values
- `credible_set` / `credible_sets` — fine-mapped GWAS hits
- `clinical_report` / `clinical_reports` — clinical trial reports by NCT ID
- `gene_ontology_terms` — GO labels for given GO IDs
- `targets_batch` / `diseases_batch` / `drugs_batch` — batch ID lookup
- `map_ids` — resolve free-text terms to OT IDs (TP53, aspirin, …)
- `search` / `facets` — cross-entity discovery
- `association_datasources`, `interaction_resources` — list evidence sources
- `meta` — OT release version / build date

**Web Search** - Current info beyond database

## Entities Supported (auto-resolved by the interpreter)

**Diseases:** Medical names → EFO/MONDO IDs
**Drugs:** Generic/brand names → ChEMBL IDs
**Targets:** Gene symbols → Ensembl IDs
**Mechanisms:** inhibitor, antagonist, modulator, agonist, blocker, activator
**Pathways:** PI3K/AKT pathway, MAPK signaling, etc.

## Resolution Methods

**OpenTargets mapping:** Direct match (fast, accurate)
**Web search:** Fallback when not in mapping
You'll see: "matched in OpenTargets" or "found via web search"

## Query Patterns

**Single anchor:** "Melanoma drugs", "What does metformin treat?",
"Diseases associated with TP53"
**Filtered:** "Give drug that target TP53 in breast cancer treatment"
**Metadata drill-down:** "Tractability of PIK3CA",
"Tissues expressing EGFR", "PGx for clopidogrel", "Subtypes of melanoma",
"Mouse phenotypes for BRCA1", "Chemical probes for BRD4"
**Variant / study (use IDs directly):** "Variant 1_55039774_C_T effect",
"Study GCST006907 sample size"
**ID resolution:** "Map TP53, aspirin, breast cancer to OT IDs"

## Output

- Entity IDs with resolution method
- Entity synonyms and descriptions
- Preview (50 rows) + full CSV download
- Smart column filtering based on query
- Rich `metadata` payload with all the per-anchor fields listed above

## Tips

✓ Specific names, standard terms, combine entities ("X for Y")
✓ For variant/study questions, supply IDs directly (rsID, GCST..., NCT...)
✗ Single vague words, colloquial-only terms

## Examples

"What does aspirin treat?"
"What is the target of aspirin?"
"What drugs are used to treat TB?"
"What are the GWAS credible sets near KRAS?"
"What pharmacogenomic variants affect imatinib response?"
"In which tissues is EGFR most highly expressed?"
"What is the tractability assessment of MCL1?"
"""