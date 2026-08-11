"""
Web-evidence fallback for per-database summarisers.

When the user marks a field as "requested" in `parsed_value` but the resulting
DataFrame projection does not contain that column (because the DB doesn't
hold it, the planner dropped it, or the interpreter couldn't map it), we
call BioChirp's web tool to fetch a focused web answer. The result is
appended to the summariser's input dict under the `web_evidence` key,
which the summariser prompt is told to treat as a citable second source.

This file is intentionally framework-thin so it can be imported from every
per-DB service without dragging extra deps.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Iterable, Optional

# httpx is a transitive dep of the openai SDK that every per-DB service
# already imports, so it is guaranteed to be importable here. We use the
# AsyncClient so fan-out web calls don't burn thread-pool slots.
import httpx

from config import settings

log = logging.getLogger("uvicorn.error")

# How long a web-evidence record stays valid in the cross-DB Redis cache.
# 60 s comfortably covers a multi-DB fan-out (10+ per-DB containers all
# asking for the same identifier within ~10 s) while bounding staleness
# for sequential user queries. Override via WEB_EVIDENCE_CACHE_TTL=0 to
# disable the cache entirely.
WEB_EVIDENCE_CACHE_TTL = int(os.getenv("WEB_EVIDENCE_CACHE_TTL", "60"))


def _cache_key(cleaned_query: str, missing: list[tuple[str, str]]) -> str:
    """Cache key for cross-DB web-evidence dedup.

    Stable across DBs: TTD, CTD, and HCDT all asking "CAS for imatinib"
    produce the same key. `db_display_name` is deliberately excluded from
    the key — the focused-prompt template embeds it as 'context: <DB>' but
    the answer (a literal CAS / UniProt / structural ID) is authoritative
    regardless of which DB asked.

    Uses sha1 of (query, sorted column names) so the key is short, stable
    across process restarts, and safe for Redis (no special chars).
    """
    cols_sig = ",".join(sorted(c for c, _ in missing))
    h = hashlib.sha1(f"{cleaned_query}\x00{cols_sig}".encode("utf-8")).hexdigest()[:16]
    return f"biochirp:web_evidence:{h}"


async def _cache_get(redis_client, key: str) -> Optional[list[dict]]:
    """Best-effort Redis GET. Returns parsed JSON list on hit, None on miss
    or any kind of failure (Redis unreachable, malformed payload, etc.).
    """
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("[web_evidence] cache GET %s failed: %s", key, e)
        return None


async def _cache_set(redis_client, key: str, value: list[dict], ttl: int) -> None:
    """Best-effort Redis SETEX. Swallows every error — the cache is purely
    a performance optimisation, never a correctness dependency."""
    if redis_client is None or ttl <= 0 or not value:
        return
    try:
        await redis_client.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        log.warning("[web_evidence] cache SET %s failed: %s", key, e)


WEB_EVIDENCE_TIMEOUT = float(os.getenv("WEB_EVIDENCE_TIMEOUT", "15"))
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WEB_EVIDENCE_MAX_FIELDS = int(os.getenv("WEB_EVIDENCE_MAX_FIELDS", "2"))

# Snake-case schema column → user-facing label. Only fields where the
# user-facing word does not match the column name need an entry. The label
# is used to build the focused web query.
_FIELD_LABELS: dict[str, str] = {
    # ---- structural identifiers
    "drug_smiles_iso":   "isomeric SMILES",
    "drug_smiles_canon": "canonical SMILES",
    "smiles":            "SMILES",
    "canonical_smiles":  "canonical SMILES",
    "drug_inchi":        "InChI",
    "inchi":             "InChI",
    "standard_inchi":    "InChI",
    "drug_inchikey":     "InChIKey",
    "inchikey":          "InChIKey",
    "inchi_key":         "InChIKey",
    "standard_inchi_key":"InChIKey",
    "drug_iupac":        "IUPAC name",
    "iupac_name":        "IUPAC name",
    "drug_formula":      "molecular formula",
    "full_molformula":   "molecular formula",
    "formula":           "molecular formula",
    "cd_formula":        "molecular formula",
    "drug_mw":           "molecular weight",
    "mol_weight":        "molecular weight",
    "mw_freebase":       "molecular weight",
    "full_mwt":          "molecular weight",
    "cd_molweight":      "molecular weight",
    "mass":              "molecular mass",
    "monoisotopic_mass": "monoisotopic mass",
    "tpsa":              "topological polar surface area (TPSA)",
    "psa":               "topological polar surface area (TPSA)",
    "clogp":             "cLogP",
    "alogp":             "cLogP",
    "protein_sequence":  "protein sequence",
    # ---- cross-reference IDs
    "pubchem_cid":       "PubChem CID",
    "pubchem_sid":       "PubChem SID",
    "cas":               "CAS registry number",
    "cas_number":        "CAS registry number",
    "cas_rn":            "CAS registry number",
    "cas_reg_no":        "CAS registry number",
    "cas_registry_number":"CAS registry number",
    "atc_code":          "ATC code",
    "superdrug_atc":     "ATC code",
    "chebi_id":          "ChEBI ID",
    "chebi_xref":        "ChEBI ID",
    "uniprot_id":        "UniProt accession",
    "uniprot_accession": "UniProt accession",
    "uniprot_xref":      "UniProt accession",
    "hgnc_id":           "HGNC ID",
    "ensembl_id":        "Ensembl ID",
    "ensembl_accession": "Ensembl ID",
    "entrez_id":         "Entrez Gene ID",
    "entrez":            "Entrez Gene ID",
    "ncbi_entrez":       "Entrez Gene ID",
    "ncbi_gene_id":      "Entrez Gene ID",
    "mesh_id":           "MeSH ID",
    "omim_id":           "OMIM ID",
    "omim_ids":          "OMIM ID",
    "icd11":             "ICD-11 code",
    "reactome_id":       "Reactome pathway ID",
    "kegg_hsa_id":       "KEGG pathway ID",
    "smpdb_id":          "SMPDB pathway ID",
    "rsid":              "rsID (dbSNP)",
    "dbsnp":             "rsID (dbSNP)",
    "drugbank_id":       "DrugBank ID",
    "drugbank_accession":"DrugBank accession",
    # ---- quantitative bioactivity
    "ki_nM":             "Ki (nM)",
    "ic50_nM":           "IC50 (nM)",
    "kd_nM":             "Kd (nM)",
    "activity_value":    "activity value",
    "act_value":         "activity value",
    "standard_value":    "standard activity value",
    # ---- mechanism / regulatory
    "moa":               "mechanism of action",
    "mechanism_of_action":"mechanism of action",
    "drug_mechanism_of_action_on_target": "mechanism of action",
    "approval_date":     "approval date",
    "first_approval":    "approval date",
    "black_box_warning": "black-box warning status",
    "withdrawn_flag":    "withdrawal status",
    "prodrug":           "prodrug status",
    "first_in_class":    "first-in-class status",
    "natural_product":   "natural-product status",
    # ---- variant
    "molecular_consequence": "molecular consequence",
    "variant_class":     "variant class",
    "review_status":     "review status",
}


def _is_requested(v: Any) -> bool:
    if v == "requested":
        return True
    if isinstance(v, list) and v == ["requested"]:
        return True
    return False


def _normalise_parsed_value(parsed_value: Any) -> dict[str, Any]:
    """Coerce parsed_value into a plain dict regardless of whether the
    caller passed a pydantic model, a dict, or None."""
    if parsed_value is None:
        return {}
    if isinstance(parsed_value, dict):
        return parsed_value
    try:
        return parsed_value.model_dump(exclude_none=True)
    except Exception:
        try:
            return dict(parsed_value)
        except Exception:
            return {}


def detect_missing_fields(
    parsed_value: Any,
    df_columns: Iterable[str] | None,
    schema_cols: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """Return [(column, user_label), ...] for fields the user requested
    that are NOT present as DataFrame columns. Only fields with a known
    user-facing label are returned (the helper is a no-op for obscure
    columns where a web query wouldn't be meaningful)."""
    pv = _normalise_parsed_value(parsed_value)
    if not pv:
        return []
    df_set = set(df_columns or [])
    schema_set = set(schema_cols or []) if schema_cols else None
    out: list[tuple[str, str]] = []
    for col, val in pv.items():
        if not _is_requested(val):
            continue
        if col in df_set:
            continue
        if schema_set is not None and col not in schema_set:
            # Field isn't even in this DB's schema — interpreter slop, skip.
            continue
        label = _FIELD_LABELS.get(col)
        if label is None:
            continue
        out.append((col, label))
    return out


async def _call_groq(query: str, *, timeout: Optional[float] = None) -> Optional[dict]:
    """Call Groq browser-search directly and return {\"message\": ...}."""
    api_key    = os.getenv("GROQ_API_KEY", "").strip()
    model_name = settings.WEB_MODEL_NAME
    if not api_key:
        return None
    t = timeout or WEB_EVIDENCE_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=t) as c:
            r = await c.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": query}],
                    "tools":       [{"type": "browser_search"}],
                    # "auto" (not "required"): "required" 400s when the model
                    # answers without searching. See web_tool.py for the rationale.
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_completion_tokens": 256,
                    "reasoning_effort": "low",
                },
            )
            r.raise_for_status()
            msg = (r.json()["choices"][0]["message"].get("content") or "").strip()
            return {"message": msg} if msg else None
    except Exception as e:
        log.warning("[web_evidence] Groq call failed (%s): %s", type(e).__name__, e)
        return None


async def fetch_web_evidence(
    parsed_value: Any,
    df_columns: Iterable[str] | None,
    cleaned_query: str | None,
    db_display_name: str,
    schema_cols: Iterable[str] | None = None,
    *,
    max_fields: int = WEB_EVIDENCE_MAX_FIELDS,
    web_url: str = "",        # unused — kept for call-site compatibility
    timeout: float = WEB_EVIDENCE_TIMEOUT,
    client: Optional[httpx.AsyncClient] = None,  # unused — kept for call-site compatibility
    redis_client: Any = None,
    cache_ttl: int = WEB_EVIDENCE_CACHE_TTL,
) -> Optional[list[dict]]:
    """For every field the user requested but the DataFrame projection
    does not include, ask the BioChirp web tool a focused question and
    return a list of evidence records. Returns None when no fallback was
    needed (so the caller can simply check `if web_evidence:`).

    Each record has the shape:
        {
            "source": "web",
            "field": <schema_col>,
            "label": <user-facing label>,
            "query": <focused query string sent to the web tool>,
            "message": <web tool response>,
        }

    Connection reuse: if `client` is None we build (and close) a one-shot
    AsyncClient — old behaviour, preserved for every existing caller. When
    `client` is provided, we reuse it without closing — the caller owns the
    client lifecycle. `_finalize.py` passes the process-wide singleton from
    `app.per_db_tool._httpx_client.get_httpx_client()`, which amortises the
    TCP+TLS handshake across the typical 3-5 missing fields per query and
    across the 10+ DBs that fan out in a multi-DB user request.
    """
    missing = detect_missing_fields(parsed_value, df_columns, schema_cols)
    if not missing:
        return None
    missing = missing[:max_fields]  # cap fan-out to limit cost/latency

    q = (cleaned_query or "").strip()

    # Cross-DB dedup: in a multi-DB fan-out (budgeted_query, MCP), TTD, CTD,
    # and HCDT all ask the web for the same identifier (e.g. cas_number for
    # imatinib) within the same user request. Redis is the only shared
    # surface across the per-DB containers, so the cache is keyed on
    # (cleaned_query, missing_columns) and ignores db_display_name.
    cache_key = _cache_key(q, missing) if q else None
    if cache_key and cache_ttl > 0:
        cached = await _cache_get(redis_client, cache_key)
        if cached is not None:
            log.info(
                "[web_evidence] cache HIT key=%s db=%s fields=%s",
                cache_key, db_display_name, [c for c, _ in missing],
            )
            return cached or None

    async def one(col: str, label: str) -> Optional[dict]:
        focused = (
            f"What is the {label} for the entity in this user query "
            f"(context: {db_display_name})? User query: {q!r}. "
            f"Reply with the verbatim {label} and cite the source URL."
        )
        resp = await _call_groq(focused, timeout=timeout)
        if not resp:
            return None
        msg = resp.get("message") or ""
        if not msg:
            return None
        return {
            "source": "web",
            "field": col,
            "label": label,
            "query": focused,
            "message": msg,
        }

    results = await asyncio.gather(
        *(one(col, lbl) for col, lbl in missing),
        return_exceptions=False,
    )

    cleaned = [r for r in results if r]
    # Populate cache on the miss path so the next DB in the fan-out skips
    # its own web round-trip. Empty results aren't cached (don't pin
    # transient failures).
    if cache_key and cache_ttl > 0 and cleaned:
        await _cache_set(redis_client, cache_key, cleaned, cache_ttl)
    return cleaned or None
