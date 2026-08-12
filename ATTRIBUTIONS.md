# BioChirp — Data Source Attributions

Canonical attribution list for the 11 upstream databases BioChirp serves. This file mirrors [`config/attributions.py`](config/attributions.py), which is the authoritative, machine-readable source — `DATABASE_ATTRIBUTIONS`'s 11 entries are reproduced verbatim below. Update both together when a database's license or version changes.

## Quick-reference license matrix

| License class | Databases | Use note |
|---|---|---|
| **CC0 1.0** | hcdt, opentargets, reactome | Attribution requested but no restriction |
| **US Public Domain** | clinvar | NIH/NCBI terms apply; no restriction, attribution requested |
| **CC BY 4.0** | msigdb (KEGG MEDICUS subset is CC BY-SA 4.0), orphanet (Orphadata Science products only), string, uniprot | Attribution required |
| **Custom — non-commercial, research/educational** | ctd | Citation and usage notification required |
| **Custom — free with citation, no modification** | hpo | Ontology must not be modified |
| **Free academic use, no formal upstream license tag** | ttd | Verify terms before redistribution |

## Full citations

- **ClinVar** — Landrum MJ et al. ClinVar. Nucleic Acids Res 2020. [US Public Domain (NIH/NCBI terms apply)] https://www.ncbi.nlm.nih.gov/clinvar/
- **CTD (Comparative Toxicogenomics Database)** — Davis AP et al. The Comparative Toxicogenomics Database. Nucleic Acids Res 2023. [Custom CTD terms — non-commercial, research/educational use only, citation + usage notification required] https://ctdbase.org/
- **HCDT 2.0 (Highly Confident Drug-Target database)** — Zhang et al. HCDT 2.0. Sci Data 12, 2025. [CC0 1.0 (per Figshare DOI 10.6084/m9.figshare.28094780.v2)] http://hainmu-biobigdata.com/hcdt2/
- **Human Phenotype Ontology (HPO)** — Koehler S et al. HPO. Nucleic Acids Res 2021. [Custom HPO license — free use with citation; ontology must not be modified] https://hpo.jax.org/
- **MSigDB v2026.1 (Hs+Mm)** — Liberzon A et al. The Molecular Signatures Database (MSigDB) hallmark gene set collection. Cell Syst 2015. [CC BY 4.0 (KEGG MEDICUS subset: CC BY-SA 4.0)] https://www.gsea-msigdb.org/gsea/msigdb/
- **Open Targets Platform 26.03** — Ochoa D et al. Open Targets Platform. Nucleic Acids Res 2023. [CC0 1.0] https://platform.opentargets.org/
- **Orphanet (Orphadata Science products)** — Orphanet, https://www.orpha.net. [CC BY 4.0 (Orphadata Science products only; DTA-only products excluded)] https://www.orpha.net/
- **Reactome v96** — Milacic M et al. Reactome 2024. Nucleic Acids Res 2024. [CC0 1.0] https://reactome.org/
- **STRING v12.0** — Szklarczyk D et al. STRING v12. Nucleic Acids Res 2023. [CC BY 4.0] https://string-db.org/
- **TTD (Therapeutic Target Database)** — Zhou Y et al. TTD. Nucleic Acids Res 2020. [Free academic use; no formal license tag upstream — verify before redistribution] https://ttd.idrblab.cn/
- **UniProt** — UniProt Consortium. UniProt. Nucleic Acids Res 2023. [CC BY 4.0] https://www.uniprot.org/

## How this is used in the running service

Every per-DB chat response can append an attribution footer naming the database(s) consulted, with license and citation, via `config/attributions.py`'s `attribution_footer()`. It's called from [`app/per_db_tool/schema_kg_chat.py`](app/per_db_tool/schema_kg_chat.py), the shared chat router library every one of the 11 database services builds on.

See [`TERMS_OF_SERVICE.md`](TERMS_OF_SERVICE.md) for the license obligations these attributions carry, particularly CTD's non-commercial restriction.
