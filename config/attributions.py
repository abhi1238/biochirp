"""Per-database attribution and citation registry for BioChirp.

Central source of truth for the license, citation, and upstream URL of every
database BioChirp serves. Used by chat-service pipelines to append an
attribution footer to user-facing responses, and by the public-tier service
to honour upstream attribution requirements (CC BY, CC BY-SA, CC BY-NC-SA).

Data here is derived from `audit_reports/web_availability_audit/citation_requirements.csv`
and `database/<db>/MANIFEST.json`. Update both this module and the audit
report together when a database's license or version changes.

Public surface:
    DATABASE_ATTRIBUTIONS  — dict[str, Attribution] keyed by lowercase DB name
    get_attribution(db)    — case-insensitive lookup; returns None if unknown
    attribution_footer(dbs, format="markdown", include_license=True)
                           — render an attribution block for a response
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Attribution:
    full_name: str
    license: str
    citation: str  # short author-year style
    url: str


# Canonical registry. Keys lowercase to match `database/<db>/` folder names.
# License strings should be the SPDX-style label users will see.
DATABASE_ATTRIBUTIONS: dict[str, Attribution] = {
    "clinvar": Attribution(
        "ClinVar", "US Public Domain (NIH/NCBI terms apply)",
        "Landrum MJ et al. ClinVar. Nucleic Acids Res 2020.",
        "https://www.ncbi.nlm.nih.gov/clinvar/",
    ),
    "ctd": Attribution(
        "CTD (Comparative Toxicogenomics Database)", "Custom CTD terms - non-commercial, research/educational use only, citation + usage notification required",
        "Davis AP et al. The Comparative Toxicogenomics Database. Nucleic Acids Res 2023.",
        "https://ctdbase.org/",
    ),
    "hcdt": Attribution(
        "HCDT 2.0 (Highly Confident Drug-Target database)", "CC0 1.0 (per Figshare DOI 10.6084/m9.figshare.28094780.v2)",
        "Zhang et al. HCDT 2.0. Sci Data 12, 2025.",
        "http://hainmu-biobigdata.com/hcdt2/",
    ),
    "hpo": Attribution(
        "Human Phenotype Ontology (HPO)", "Custom HPO license - free use with citation; ontology must not be modified",
        "Koehler S et al. HPO. Nucleic Acids Res 2021.",
        "https://hpo.jax.org/",
    ),
    "msigdb": Attribution(
        "MSigDB v2026.1 (Hs + Mm)", "CC BY 4.0 (KEGG MEDICUS subset: CC BY-SA 4.0)",
        "Liberzon A et al. The Molecular Signatures Database (MSigDB) hallmark gene set collection. Cell Syst. 2015.",
        "https://www.gsea-msigdb.org/gsea/msigdb/",
    ),
    "opentargets": Attribution(
        "Open Targets Platform 26.03", "CC0 1.0",
        "Ochoa D et al. Open Targets Platform. Nucleic Acids Res 2023.",
        "https://platform.opentargets.org/",
    ),
    "orphanet": Attribution(
        "Orphanet (Orphadata Science products)", "CC BY 4.0 (Orphadata Science products only; DTA-only products excluded)",
        "Orphanet. https://www.orpha.net.",
        "https://www.orpha.net/",
    ),
    "reactome": Attribution(
        "Reactome v96", "CC0 1.0",
        "Milacic M et al. Reactome 2024. Nucleic Acids Res 2024.",
        "https://reactome.org/",
    ),
    "string": Attribution(
        "STRING v12.0", "CC BY 4.0",
        "Szklarczyk D et al. STRING v12. Nucleic Acids Res 2023.",
        "https://string-db.org/",
    ),
    "ttd": Attribution(
        "TTD (Therapeutic Target Database)", "Free academic use; no formal license tag upstream - verify before redistribution",
        "Zhou Y et al. TTD. Nucleic Acids Res 2020.",
        "https://ttd.idrblab.cn/",
    ),
    "uniprot": Attribution(
        "UniProt", "CC BY 4.0",
        "UniProt Consortium. UniProt. Nucleic Acids Res 2023.",
        "https://www.uniprot.org/",
    ),
}


def get_attribution(db: str) -> Optional[Attribution]:
    """Case-insensitive lookup. Returns None for unknown databases."""
    if not db:
        return None
    return DATABASE_ATTRIBUTIONS.get(db.strip().lower())


def attribution_footer(
    dbs: Iterable[str],
    *,
    format: str = "markdown",
    include_license: bool = True,
) -> str:
    """Render a per-response attribution block for one or more databases.

    Args:
        dbs: database names (case-insensitive) actually consulted in this response.
        format: "markdown" (default) or "text".
        include_license: include the license summary alongside the citation.

    Returns:
        A short multi-line string suitable for appending to a user-facing
        response. Returns empty string when none of `dbs` is known.
    """
    seen: list[Attribution] = []
    seen_names: set[str] = set()
    for db in dbs:
        a = get_attribution(db)
        if a is None or a.full_name in seen_names:
            continue
        seen.append(a)
        seen_names.add(a.full_name)
    if not seen:
        return ""

    if format == "text":
        lines = ["Data sources used in this response:"]
        for a in seen:
            piece = f"- {a.full_name}: {a.citation} ({a.url})"
            if include_license:
                piece += f" [{a.license}]"
            lines.append(piece)
        lines.append("See TERMS_OF_SERVICE.md for full license terms.")
        return "\n".join(lines)

    # markdown (default)
    lines = ["", "---", "**Data sources used in this response:**", ""]
    for a in seen:
        piece = f"- **{a.full_name}** — {a.citation} ([source]({a.url}))"
        if include_license:
            piece += f" — *{a.license}*"
        lines.append(piece)
    lines.append("")
    lines.append("See [TERMS_OF_SERVICE.md](TERMS_OF_SERVICE.md) for full license terms.")
    return "\n".join(lines)
