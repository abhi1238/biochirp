# BioChirp — Terms of Service (Public Retrieval Tier)

**Effective date:** 2026-05-14
**Applies to:** The publicly-hosted BioChirp retrieval API and web interface at this host. Local / institutional deployments running `docker compose --profile restricted up` are not covered by this public-tier ToS.

## 1. What BioChirp is

BioChirp is a retrieval-only orchestrator over publicly available
biomedical databases. It does not produce original data of its own; every
response it returns is sourced from one or more upstream databases.
Each upstream source carries its own license, attribution requirement,
and use restriction. **By using this service, you agree to comply with
the terms of every upstream source whose data appears in your response.**

## 2. Data sources and their licenses

The public tier serves data from databases listed in
[`audit_reports/web_availability_audit/database_use_notes.md`](audit_reports/web_availability_audit/database_use_notes.md)
and [`audit_reports/web_availability_audit/citation_requirements.csv`](audit_reports/web_availability_audit/citation_requirements.csv).
Salient categories:

### 2.1 Open / unrestricted (CC0, CC BY 4.0, MIT, US Public Domain)

biogrid, chebi, civic, clinvar, dgidb, doid, hcdt, hgnc, hpo, mesh,
mondo, msigdb, opentargets, orphanet, pubtator, reactome, string, ttd,
uniprot, wikipathways — attribution requested but no commercial or
redistribution restriction.

### 2.2 Share-alike — derivatives must carry the same license

- **ChEMBL** — CC BY-SA 3.0
- **PharmGKB / ClinPGx** — CC BY-SA 4.0
- **TRRUST** — CC BY-SA 4.0

If you redistribute derivatives of these data (including ML models trained
substantially on them, or aggregated data products), your derivative must
be licensed under the same share-alike terms. Mere retrieval and analysis
does not trigger this; redistribution of a derived product does.

### 2.3 Non-commercial — use of CTD is restricted

- **CTD** (Comparative Toxicogenomics Database) — CC BY-NC-SA 3.0

**You may not use BioChirp responses that contain CTD-sourced data for
commercial purposes.** This restriction is imposed by CTD's upstream
license and is transitively binding on you as a downstream user.
Commercial users who require CTD data must obtain a separate license
directly from CTD (https://ctdbase.org/).

### 2.4 OmniPath — academic-only sources filtered (best-effort)

OmniPath aggregates from many sources, a subset of which require a separate
commercial license. The public tier filters rows whose source provenance
indicates a commercial-restricted upstream (KEA, ProtMapper, Phospho.ELM
commercial tier, HPMR, LMPID, and similar). This filter is best-effort and
not a substitute for verifying compliance with each upstream source's
specific terms. Commercial users should query OmniPath directly with their
own license.

## 3. Attribution requirement

If you publish results, methods, code, or derivatives that depend on
BioChirp responses, you must cite both:

1. **BioChirp itself** — see `CITATION.cff` at the repository root.
2. **Each upstream database whose data appears in your derivative output** —
 per the citation list in
 [`audit_reports/web_availability_audit/citation_requirements.csv`](audit_reports/web_availability_audit/citation_requirements.csv).

A minimum-acceptable attribution footer that BioChirp itself emits with
each API response identifies the upstream databases used; this is your
attribution starting point but may need expansion in formal publications.

## 4. Use restrictions

You may not:

- Use BioChirp responses containing CTD data for commercial purposes
 without obtaining a separate CTD license.
- Train, fine-tune, distil, or otherwise update machine-learning model
 weights using responses that contain data. ( is not served
 on this tier, but if you somehow obtain content via this service,
 this restriction applies.)
- Bulk-scrape the public tier to reconstruct upstream databases. Use the
 upstream download endpoints directly; we link to them in each response's
 attribution footer.
- Use this service to generate medical advice, diagnoses, or clinical
 decisions. BioChirp is a research tool only.

## 5. No warranty

This service is provided **as is**, without warranty of any kind. Upstream
databases may contain errors, omissions, or stale records; BioChirp does
not validate, correct, or guarantee the accuracy of any data it relays.
The service operator disclaims liability for any decision made on the
basis of BioChirp output.

## 6. Privacy

The public tier may log queries (minus any personally identifying
information you choose not to provide) for purposes of error analysis,
abuse detection, and capacity planning. No third party receives your
queries except for upstream API providers that BioChirp may consult to
fulfill a request (currently: Open Targets GraphQL API; expected to
expand). Do not send personally identifying patient data or other
sensitive information through the public tier.

## 7. Rate limits

The public tier is subject to per-IP rate limits intended to keep the
service available for all users. Sustained automated traffic should
instead run BioChirp locally; the entire stack is open-source.

## 8. Changes to these terms

These terms may be updated as upstream licenses or data sources change.
The current version is the one at this URL on the date of your query.
Material changes will be noted at the top of this document with a new
effective date. Continued use after a material change constitutes
acceptance of the new terms.

## 9. Contact

Issues, license clarifications, or compliance questions: open an issue
at the BioChirp repository or contact the maintainer listed in
`CITATION.cff`.

---

**Audit reference:** All license determinations above are based on the
2026-05-14 web-availability audit at
[`audit_reports/web_availability_audit/`](audit_reports/web_availability_audit/).
Re-run the audit (`audit_reports/web_availability_audit/web_availability_summary.md`
explains how) to verify current upstream terms before relying on this
document for legal or commercial purposes.
