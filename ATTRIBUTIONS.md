# BioChirp — Data Source Attributions

Canonical attribution list for the upstream databases BioChirp serves.
This file is intended for inclusion in publications, derivative software,
and downstream documentation. The same content is programmatically
available via `config/attributions.py` and is appended to user-facing
responses on a per-database basis.

Last updated: 2026-05-16 (synced character-for-character against
`config/attributions.py`, which is the authoritative source).

## Quick-reference license matrix

| License class | Databases | Use note |
|---|---|---|
| **Public domain / CC0 1.0** | civic, doid, hcdt, opentargets, reactome, wikipathways | Attribution requested but no restriction |
| **US Government public domain** | clinvar, mesh, pubtator | No restriction; attribution requested |
| **CC BY 4.0** | chebi, mondo, orphanet (Orphadata Science products only), string, uniprot | Attribution required |
| **MIT** | biogrid, dgidb | Attribution required |
| **CC BY-SA 3.0 Unported** | chembl | Share-alike: derivatives must be CC BY-SA |
| **CC BY-SA 4.0** | drugcentral, pharmgkb, trrust | Share-alike: derivatives must be CC BY-SA |
| **CC BY 4.0 (mixed share-alike subset)** | msigdb | CC BY 4.0 overall; KEGG MEDICUS subset is CC BY-SA 4.0 |
| **No formal license; effectively public domain** | hgnc | Attribution requested |
| **Custom — non-commercial, research/educational** | ctd | Citation and usage notification required |
| **Custom — open-with-citation, no modification** | hpo | Ontology must not be modified |
| **Custom — free academic use (no formal upstream tag)** | ttd | Verify before redistribution |
| **Mixed per-resource (academic filter on public tier)** | omnipath | Commercial-source rows filtered via `BIOCHIRP_PUBLIC_TIER=1` |

## Full citations

The per-database citations below are the recommended manuscript form.
Each entry's exact license string is the authoritative version
emitted by `config/attributions.py`.

- **BioGRID** — Oughtred R et al. The BioGRID interaction database: 2021 update. Nucleic Acids Res 2021. [MIT] https://thebiogrid.org/
- **ChEBI (Chemical Entities of Biological Interest)** — Hastings J et al. ChEBI in 2016. Nucleic Acids Res 2016. [CC BY 4.0] https://www.ebi.ac.uk/chebi/
- **ChEMBL v36** — Zdrazil B et al. The ChEMBL Database in 2023. Nucleic Acids Res 2024. [CC BY-SA 3.0 Unported] https://www.ebi.ac.uk/chembl/
- **CIViC (Clinical Interpretation of Variants in Cancer)** — Griffith M et al. CIViC. Nat Genet 2017. [CC0 1.0] https://civicdb.org/
- **ClinVar** — Landrum MJ et al. ClinVar. Nucleic Acids Res 2020. [US Public Domain (NIH/NCBI terms apply)] https://www.ncbi.nlm.nih.gov/clinvar/
- **CTD (Comparative Toxicogenomics Database)** — Davis AP et al. The Comparative Toxicogenomics Database. Nucleic Acids Res 2023. [Custom CTD terms — non-commercial, research/educational use only, citation + usage notification required] https://ctdbase.org/
- **DGIdb (Drug-Gene Interaction Database)** — Cannon M et al. DGIdb 5.0. Nucleic Acids Res 2024. [MIT (per dgidb-v5 repository; data freely redistributable with citation)] https://dgidb.org/
- **Disease Ontology (DOID)** — Schriml LM et al. Disease Ontology. Nucleic Acids Res 2022. [CC0 1.0] https://disease-ontology.org/
- **DrugCentral** — Avram S et al. DrugCentral 2023. Nucleic Acids Res 2023. [CC BY-SA 4.0] https://drugcentral.org/
- **HCDT 2.0 (Highly Confident Drug-Target database)** — Zhang et al. HCDT 2.0. Sci Data 12, 2025. [CC0 1.0 (per Figshare DOI 10.6084/m9.figshare.28094780.v2)] http://hainmu-biobigdata.com/hcdt2/
- **HGNC (HUGO Gene Nomenclature Committee)** — Tweedie S et al. HGNC. Nucleic Acids Res 2021. [No restrictions on use (effectively public domain; attribution requested)] https://www.genenames.org/
- **Human Phenotype Ontology (HPO)** — Koehler S et al. HPO. Nucleic Acids Res 2021. [Custom HPO license — free use with citation; ontology must not be modified] https://hpo.jax.org/
- **MeSH (Medical Subject Headings)** — U.S. National Library of Medicine. MeSH 2024. [US Public Domain (US Government work; attribution to NLM requested)] https://www.nlm.nih.gov/mesh/
- **Mondo Disease Ontology** — Vasilevsky NA et al. Mondo. medRxiv 2022. [CC BY 4.0] https://mondo.monarchinitiative.org/
- **MSigDB v2026.1 (Hs+Mm)** — Liberzon A et al. MSigDB. Cell Syst 2015. [CC BY 4.0 (KEGG MEDICUS subset: CC BY-SA 4.0)] https://www.gsea-msigdb.org/gsea/msigdb
- **OmniPath (academic-source filter applied on public tier)** — Turei D et al. OmniPath. Mol Syst Biol 2016. [Mixed per-resource licenses (no unified license); commercial-use filter applied on public tier] https://omnipathdb.org/
- **Open Targets Platform 26.03** — Ochoa D et al. Open Targets Platform. Nucleic Acids Res 2023. [CC0 1.0] https://platform.opentargets.org/
- **Orphanet (Orphadata Science products)** — Orphanet, https://www.orpha.net. [CC BY 4.0 (Orphadata Science products only; DTA-only products excluded)] https://www.orpha.net/
- **PharmGKB / ClinPGx (2026-05-05 snapshot)** — Whirl-Carrillo M et al. PharmGKB. Clin Pharmacol Ther 2021. [CC BY-SA 4.0] https://www.clinpgx.org/
- **PubTator3** — Wei C-H et al. PubTator3. Nucleic Acids Res 2024. [US Public Domain (NCBI/NLM terms; attribution requested)] https://www.ncbi.nlm.nih.gov/research/pubtator3/
- **Reactome v96** — Milacic M et al. Reactome 2024. Nucleic Acids Res 2024. [CC0 1.0] https://reactome.org/
- **STRING v12.0** — Szklarczyk D et al. STRING v12. Nucleic Acids Res 2023. [CC BY 4.0] https://string-db.org/
- **TRRUST v2** — Han H et al. TRRUST v2. Nucleic Acids Res 2018. [CC BY-SA 4.0] https://www.grnpedia.org/trrust/
- **TTD (Therapeutic Target Database)** — Zhou Y et al. TTD. Nucleic Acids Res 2020. [Free academic use; no formal license tag upstream — verify before redistribution] https://ttd.idrblab.cn/
- **UniProt** — UniProt Consortium. UniProt. Nucleic Acids Res 2023. [CC BY 4.0] https://www.uniprot.org/
- **WikiPathways** — Martens M et al. WikiPathways. Nucleic Acids Res 2021. [CC0 1.0] https://www.wikipathways.org/

## How this is used in the running service

- Every BioChirp chat-service response template (one per database) appends an attribution footer naming the databases it consulted, with license and citation, via `config/attributions.py`. The shared wiring lives in [app/per_db_chat/_pipeline.py](app/per_db_chat/_pipeline.py).
- The public-tier deployment also serves [TERMS_OF_SERVICE.md](TERMS_OF_SERVICE.md), which transitively binds users to upstream non-commercial / share-alike clauses.
- `BIOCHIRP_PUBLIC_TIER=1` activates the OmniPath license filter at load time (see [app/tools/omnipath/app/database_loader.py](app/tools/omnipath/app/database_loader.py)).
