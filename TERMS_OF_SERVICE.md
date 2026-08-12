# BioChirp — Terms of Service (Public Retrieval Tier)

**Applies to:** The publicly-hosted BioChirp retrieval API and web interface at [biochirp.iiitd.edu.in](https://biochirp.iiitd.edu.in). If you're running your own self-hosted instance from this repository instead, these terms don't automatically apply to your deployment — you're bound by the same upstream database licenses regardless (§2), but the operational terms below (§5–§7) are about *this* hosted instance specifically.

## 1. What BioChirp is

BioChirp is a retrieval-only orchestrator over 11 curated biomedical databases. It does not produce original data of its own; every response it returns is sourced from one or more upstream databases. Each upstream source carries its own license, attribution requirement, and use restriction. **By using this service, you agree to comply with the terms of every upstream source whose data appears in your response.**

## 2. Data sources and their licenses

The full, authoritative license list is [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md), generated from [`config/attributions.py`](config/attributions.py). Summary by category:

### 2.1 Open / unrestricted (CC0, CC BY 4.0, US Public Domain)

ClinVar, HCDT, MSigDB (excluding its KEGG MEDICUS subset), Open Targets, Orphanet, Reactome, STRING, UniProt — attribution requested but no commercial or redistribution restriction.

### 2.2 Non-commercial — CTD

**CTD** (Comparative Toxicogenomics Database) is licensed for non-commercial, research/educational use only, with citation and usage notification required. **You may not use BioChirp responses that contain CTD-sourced data for commercial purposes.** This restriction is imposed by CTD's upstream license and is transitively binding on you as a downstream user. Commercial users who require CTD data must obtain a separate license directly from CTD (https://ctdbase.org/).

### 2.3 Custom terms — HPO and TTD

- **HPO** (Human Phenotype Ontology) — free use with citation; the ontology itself must not be modified.
- **TTD** (Therapeutic Target Database) — free for academic use; TTD has no formal license tag upstream, so verify current terms before redistributing TTD-sourced data.

## 3. Attribution requirement

If you publish results, methods, code, or derivatives that depend on BioChirp responses, you must cite both:

1. **BioChirp itself** — see [`CITATION.cff`](CITATION.cff) at the repository root.
2. **Each upstream database whose data appears in your derivative output** — per [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md).

The attribution footer BioChirp itself can emit with a chat response (via `config/attributions.py`'s `attribution_footer()`) identifies the upstream database(s) used — this is your attribution starting point but may need expansion in formal publications.

## 4. Use restrictions

You may not:

- Use BioChirp responses containing CTD data for commercial purposes without obtaining a separate CTD license (§2.2).
- Bulk-scrape the public tier to reconstruct upstream databases. Use the upstream download endpoints directly — see [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md) for each source's URL.
- Use this service to generate medical advice, diagnoses, or clinical decisions. BioChirp is a research tool only.

## 5. No warranty

This service is provided **as is**, without warranty of any kind. Upstream databases may contain errors, omissions, or stale records; BioChirp does not validate, correct, or guarantee the accuracy of any data it relays. The service operator disclaims liability for any decision made on the basis of BioChirp output.

## 6. Privacy

The hosted instance may log queries for purposes of error analysis, abuse detection, and capacity planning. No third party receives your queries except for upstream API providers that BioChirp may consult to fulfill a request (currently: the Open Targets GraphQL API). Do not send personally identifying patient data or other sensitive information through this service.

## 7. Rate limits

The hosted instance is subject to per-IP rate limits intended to keep the service available for all users — see [`deploy/SECURITY.md`](deploy/SECURITY.md). Sustained automated traffic should instead run BioChirp locally; the entire stack is open-source.

## 8. Changes to these terms

These terms may be updated as upstream licenses or data sources change. The current version is the one at this URL on the date of your query.

## 9. Contact

Issues, license clarifications, or compliance questions: open an issue at the BioChirp repository or contact the maintainer listed in [`CITATION.cff`](CITATION.cff).
