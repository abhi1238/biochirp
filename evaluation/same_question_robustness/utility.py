from __future__ import annotations

import asyncio
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import aiohttp
import mygene  # pip install mygene
import pandas as pd
import requests

from groq import Groq
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=UserWarning, module="mygene")

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# BUG FIXED: the original file declared OT_GRAPHQL_URL at module level but
# then used the *different* name OT_GQL_URL (never defined) inside
# get_top_opentarget_target_id and resolve_disease_opentarget → NameError at
# runtime.  Single canonical name used throughout.
OT_GRAPHQL_URL: str = "https://api.platform.opentargets.org/api/v4/graphql"
OLS_SEARCH_URL: str = "https://www.ebi.ac.uk/ols4/api/search"
LLAMA_CANONICAL_MODEL: str = "llama-3.3-70b-versatile"
LLAMA_CANONICAL_SOURCE: str = "Llama"

_WS_RE = re.compile(r"\s+")
_ID_COLON_FORM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[A-Za-z0-9._-]+$")


# =============================================================================
# Normalisation helpers
# =============================================================================

def _normalize_gene_term(g: str) -> str:
    """
    Normalise a gene symbol for MyGene queries.

    MyGene's ``symbol`` scope is case-sensitive, so we upper-case to maximise
    hit rate.  The operation is reversible: original casing is preserved in the
    ``gene_name`` column of the returned DataFrame.

    Parameters
    ----------
    g : str  e.g. ``"brca1"``, ``" TP53 "``
    Returns
    -------
    str  e.g. ``"BRCA1"``
    """
    return (g or "").strip().upper()


def _normalize_disease_term(term: str) -> str:
    """
    Normalise a disease name for API queries.

    Steps applied:

    1. Strip and lower-case.
    2. Remove possessive apostrophes  (``"Alzheimer's"`` → ``"alzheimer"``).
    3. Collapse internal whitespace runs to a single space.

    Note: spacing typos such as ``"type2 diabetes"`` are *not* corrected here.
    Add domain-specific pre-correction before calling this function if needed.

    Parameters
    ----------
    term : str  Raw disease name.
    Returns
    -------
    str  Normalised disease name.
    """
    term = (term or "").strip().lower()
    term = re.sub(r"'s?\b", "", term)   # alzheimer's → alzheimer
    term = re.sub(r"\s+", " ", term)    # collapse spaces
    return term


def _normalize_drug_term(name: str) -> str:
    """
    Normalise a drug name for OpenTargets search.

    Parameters
    ----------
    name : str  Raw drug name, e.g. ``"Ibuprofen"``, ``" ASPIRIN "``.
    Returns
    -------
    str  Lower-cased, stripped drug name.

    Notes
    -----
    BUG FIXED: the original implementation called ``.lower()`` and then
    immediately ``.upper()``, so the lower-case step was a no-op and queries
    were sent in ALL-CAPS.  The OpenTargets API returns mixed-case drug names,
    so the subsequent ``_exact_match`` call (which upper-cases both sides)
    worked correctly — but sending ALL-CAPS degraded search ranking.  Now
    returns lower-case, consistent with how the API's search engine tokenises.
    """
    return (name or "").strip().lower()


# =============================================================================
# Shared utilities
# =============================================================================

def _norm_ws(s: str) -> str:
    """Collapse any whitespace run to a single space and strip ends."""
    return _WS_RE.sub(" ", s).strip()


def _to_ot_disease_id_format(disease_id: Optional[str]) -> Optional[str]:
    """
    Convert disease IDs to OpenTargets-style delimiter format.

    OpenTargets typically uses IDs like ``EFO_0000305`` and ``MONDO_0018076``.
    Some paths in this code previously converted these to colon form
    (``EFO:0000305``), which creates avoidable mismatches across models.

    Rules:
    - Keep ``None``/empty values unchanged.
    - If the value already contains ``_``, keep it as-is.
    - If it is a compact ``PREFIX:VALUE`` identifier, convert only the first
      colon to underscore.
    """
    if disease_id is None:
        return None

    value = str(disease_id).strip()
    if not value:
        return None

    if "_" in value:
        return value

    if _ID_COLON_FORM_RE.match(value):
        return value.replace(":", "_", 1)

    return value


def _pick_best_hit(
    term_norm: str,
    hits: list[dict],
    hit_name_key: str = "name",
) -> dict | None:
    """
    Choose the best hit from a list of OpenTargets search results.

    Selection order:

    1. Exact normalised-label match (case-insensitive, whitespace-collapsed).
    2. Highest ``difflib.SequenceMatcher`` ratio as a tiebreaker.
    3. First element as a last resort.

    Parameters
    ----------
    term_norm : str
        Normalised query term (output of ``_normalize_disease_term``).
    hits : list[dict]
        Raw hit dicts from an OpenTargets search response.
    hit_name_key : str
        Key in each hit dict holding the display name (default ``"name"``).

    Returns
    -------
    dict | None
    """
    if not hits:
        return None

    for h in hits:
        hn = _norm_ws(str(h.get(hit_name_key, "")).lower())
        if hn and hn == term_norm:
            return h

    best: dict | None = None
    best_score = -1.0
    for h in hits:
        hn = _norm_ws(str(h.get(hit_name_key, "")).lower())
        if not hn:
            continue
        score = SequenceMatcher(None, term_norm, hn).ratio()
        if score > best_score:
            best = h
            best_score = score

    return best or hits[0]


def _exact_match(
    term_u: str,
    name: Optional[str],
    synonyms: Optional[List[str]],
) -> bool:
    """
    Return ``True`` if *term_u* (upper-cased) matches the entity name or any
    synonym (case-insensitive comparison).

    Parameters
    ----------
    term_u : str           Upper-cased query term.
    name : str | None      Primary API name of the entity.
    synonyms : list | None List of synonym strings; may be ``None``.
    """
    if name and name.upper() == term_u:
        return True
    if synonyms:
        for s in synonyms:
            if s and s.upper() == term_u:
                return True
    return False


def _print_confident_mapping(
    *,
    entity_type: str,
    raw_name: str,
    resolved_id: str,
    source: str,
    canonical_name: Optional[str] = None,
) -> None:
    """
    Print a trace line when a high-confidence mapping is accepted.

    Parameters
    ----------
    entity_type : str
        Entity label, e.g. ``"disease"`` or ``"drug"``.
    raw_name : str
        Original input term.
    resolved_id : str
        Final resolved identifier.
    source : str
        Resolver source label written to the output DataFrame.
    canonical_name : str | None
        Canonical form used for fallback resolution, when applicable.
    """
    raw_clean = str(raw_name).strip()
    if canonical_name and str(canonical_name).strip():
        canonical_clean = str(canonical_name).strip()
        print(
            f"[map][{entity_type}][{source}] "
            f"raw='{raw_clean}' -> canonical='{canonical_clean}' -> id='{resolved_id}'"
        )
        return

    print(
        f"[map][{entity_type}][{source}] "
        f"raw='{raw_clean}' -> id='{resolved_id}'"
    )


# =============================================================================
# ── GENE RESOLVERS ────────────────────────────────────────────────────────────
# =============================================================================

_SEARCH_TARGET_QUERY = """
query SearchTarget($q: String!, $size: Int!) {
  search(queryString: $q, entityNames: ["target"], page: { index: 0, size: $size }) {
    hits {
      id
      name
      object {
        ... on Target {
          approvedSymbol
          symbolSynonyms { label }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# MyGene bulk resolver (PRIMARY)
# ---------------------------------------------------------------------------

def get_ensembl_ids_fast(genes: List[str]) -> pd.DataFrame:
    """
    Bulk-resolve gene terms to Ensembl gene IDs using MyGene.info.

    Resolution is intentionally strict for symbol-like inputs to avoid
    off-target alias/name matches (e.g. ``BRAF`` mapping to a non-canonical
    Ensembl ID).

    Parameters
    ----------
    genes : list[str]
        Gene terms in any casing, e.g. ``["BRCA1", "tp53", "interleukin 6"]``.

    Returns
    -------
    pd.DataFrame
        Columns: ``gene_name`` (original) | ``gene_id`` (Ensembl ID or NaN).

    Notes
    -----
    - Pass 1: query ``symbol`` scope only and keep exact symbol matches.
    - Pass 2: for unresolved *non-symbol-like* terms, query ``alias,name``.
    - Unresolved genes are NaN; pass them to
      :func:`get_top_opentarget_target_id`.
    """
    mg = mygene.MyGeneInfo()

    genes_norm = [_normalize_gene_term(g) for g in genes]
    df_in = pd.DataFrame({"gene_name": list(genes), "_gene_norm": genes_norm})

    def _run_querymany(terms: List[str], scopes: str) -> pd.DataFrame:
        """Run querymany safely and return a normalized result frame."""
        if not terms:
            return pd.DataFrame(columns=["query", "symbol", "ensembl.gene", "_score"])

        raw = mg.querymany(
            terms,
            scopes=scopes,
            fields="ensembl.gene,symbol,_score",
            species="human",
            as_dataframe=True,
            returnall=True,
        )
        results: pd.DataFrame = raw["out"] if isinstance(raw, dict) else raw
        if not isinstance(results, pd.DataFrame) or results.empty:
            return pd.DataFrame(columns=["query", "symbol", "ensembl.gene", "_score"])

        df = results.reset_index()
        if "query" not in df.columns:
            # querymany dataframe index is the query term.
            first_col = df.columns[0]
            df = df.rename(columns={first_col: "query"})

        for col in ("symbol", "ensembl.gene", "_score"):
            if col not in df.columns:
                df[col] = None

        return df[["query", "symbol", "ensembl.gene", "_score"]]

    def _extract_ensembl(val) -> Optional[str]:
        """Handle the shapes MyGene uses for the ensembl.gene field."""
        if isinstance(val, list):
            candidates: List[str] = []
            for item in val:
                if isinstance(item, dict):
                    gid = item.get("gene")
                else:
                    gid = item
                if isinstance(gid, str) and gid.strip().upper().startswith("ENSG"):
                    candidates.append(gid.strip().upper())
            if not candidates:
                return None
            # deterministic pick when multiple IDs are provided
            return sorted(set(candidates))[0]
        if isinstance(val, dict):
            gid = val.get("gene")
            if isinstance(gid, str) and gid.strip().upper().startswith("ENSG"):
                return gid.strip().upper()
            return None
        # BUG FIXED: bare pd.isna(val) raises ValueError when val is a list or
        # dict (ambiguous truth value).  Guard with try/except.
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            return None
        if isinstance(val, str) and val.strip().upper().startswith("ENSG"):
            return val.strip().upper()
        return None

    # Pass 1: exact symbol matches only.
    pass1 = _run_querymany(genes_norm, scopes="symbol")
    pass1["_gene_norm"] = pass1["query"].astype(str).str.strip().str.upper()
    pass1["_symbol_norm"] = pass1["symbol"].astype(str).str.strip().str.upper()
    pass1["gene_id"] = pass1["ensembl.gene"].apply(_extract_ensembl)
    pass1 = pass1[
        (pass1["_gene_norm"] == pass1["_symbol_norm"]) &
        pass1["gene_id"].notna()
    ].copy()
    pass1["_score"] = pd.to_numeric(pass1["_score"], errors="coerce").fillna(-1.0)
    pass1 = (
        pass1.sort_values(["_gene_norm", "_score"], ascending=[True, False])
        .drop_duplicates(subset=["_gene_norm"], keep="first")
    )
    df_mapped = pass1[["_gene_norm", "gene_id"]].copy()

    resolved_terms = set(df_mapped["_gene_norm"].tolist())
    unresolved_terms = [t for t in genes_norm if t not in resolved_terms]

    # Pass 2: alias/name fallback only for non-symbol-like terms.
    # Symbol-like terms (e.g. TP53, BRAF, IL28B) stay unresolved here and can
    # be validated by OpenTargets fallback in the orchestrator.
    symbol_like_re = re.compile(r"^[A-Z0-9-]+$")
    pass2_terms = [t for t in unresolved_terms if not symbol_like_re.fullmatch(t)]
    if pass2_terms:
        pass2 = _run_querymany(pass2_terms, scopes="alias,name")
        pass2["_gene_norm"] = pass2["query"].astype(str).str.strip().str.upper()
        pass2["gene_id"] = pass2["ensembl.gene"].apply(_extract_ensembl)
        pass2 = pass2[pass2["gene_id"].notna()].copy()
        pass2["_score"] = pd.to_numeric(pass2["_score"], errors="coerce").fillna(-1.0)
        pass2 = (
            pass2.sort_values(["_gene_norm", "_score"], ascending=[True, False])
            .drop_duplicates(subset=["_gene_norm"], keep="first")
        )
        if not pass2.empty:
            df_mapped = pd.concat(
                [df_mapped, pass2[["_gene_norm", "gene_id"]]],
                ignore_index=True,
            )
            df_mapped = df_mapped.drop_duplicates(subset=["_gene_norm"], keep="first")

    return df_in.merge(df_mapped, on="_gene_norm", how="left")[["gene_name", "gene_id"]]


# ---------------------------------------------------------------------------
# OpenTargets exact-symbol / synonym fallback
# ---------------------------------------------------------------------------

def get_top_opentarget_target_id(
    gene_symbol: str,
    prefer_exact_symbol: bool = True,
    timeout: int = 30,
) -> Optional[str]:
    """
    Resolve a single gene symbol to an Ensembl ID via OpenTargets search.

    With ``prefer_exact_symbol=True`` (recommended):
      Accepts a hit only when the query matches:

      * the approved HGNC symbol  (e.g. ``BAG6``)  **or**
      * any ``symbolSynonym``     (e.g. ``BAT3`` → ``BAG6``)

      Returns ``None`` if neither condition is met — no fuzzy acceptance.

    With ``prefer_exact_symbol=False``:
      Returns the top search hit unconditionally (exploratory use only).

    Parameters
    ----------
    gene_symbol : str          Gene symbol or alias, e.g. ``"BAT3"``.
    prefer_exact_symbol : bool Enforce exact/synonym matching (default ``True``).
    timeout : int              HTTP timeout in seconds.

    Returns
    -------
    str | None  Ensembl gene ID or ``None``.

    Notes
    -----
    BUG FIXED: the original code checked only ``approvedSymbol`` against the
    query.  Old synonyms (BAT3 → BAG6, CTL4 → SLC44A4, IL28B → IFNL3) were
    therefore never matched and the function always returned ``None`` for them.
    Added a second loop over ``symbolSynonyms``.
    """
    symbol = (gene_symbol or "").strip()
    if not symbol:
        return None

    try:
        resp = requests.post(
            OT_GRAPHQL_URL,  # BUG FIXED: was undefined OT_GQL_URL → NameError
            json={"query": _SEARCH_TARGET_QUERY, "variables": {"q": symbol, "size": 20}},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[OpenTargets] request failed for '{symbol}': {exc}")
        return None

    if payload.get("errors"):
        print(f"[OpenTargets] GraphQL errors for '{symbol}': {payload['errors']}")
        return None

    hits = payload.get("data", {}).get("search", {}).get("hits", [])
    if not hits:
        return None

    if not prefer_exact_symbol:
        return hits[0].get("id")

    sym_u = symbol.upper()
    for h in hits:
        obj = h.get("object") or {}

        # (a) approved symbol
        if obj.get("approvedSymbol", "").upper() == sym_u:
            return h["id"]

        # (b) any synonym — KEY FIX
        synonyms = [s["label"].upper() for s in (obj.get("symbolSynonyms") or [])]
        if sym_u in synonyms:
            return h["id"]

    return None


# ---------------------------------------------------------------------------
# Gene orchestrator
# ---------------------------------------------------------------------------

def resolve_ensembl_ids_with_fallback(
    genes: List[str],
    timeout: int = 30,
    use_opentargets_fallback: bool = True,
) -> pd.DataFrame:
    """
    Resolve gene symbols to Ensembl IDs via a two-stage pipeline.

    Stage 1 — MyGene bulk query (fast, handles aliases natively).
    Stage 2 — OpenTargets per-gene search + synonym matching (optional).

    Parameters
    ----------
    genes : list[str]
        Gene symbols in any casing.
    timeout : int
        Per-request HTTP timeout for Stage 2 OpenTargets calls (seconds).
    use_opentargets_fallback : bool
        If ``False``, genes unresolved after MyGene are left as NaN.

    Returns
    -------
    pd.DataFrame
        Columns: ``gene_name`` | ``gene_id`` | ``source``

        ``source`` values: ``"mygene"`` | ``"opentargets"`` | ``None``

    Notes
    -----
    Input typos (e.g. ``"PGFRA"`` for ``"PDGFRA"``) cannot be corrected by
    any resolver and will remain NaN with a printed warning.
    """
    # ── Stage 1: MyGene ──────────────────────────────────────────────────────
    df = get_ensembl_ids_fast(genes).copy()

    # BUG FIXED: original assigned source="mygene" for ALL rows, including
    # unresolved ones, which was misleading.  Only tag rows with an actual ID.
    df["source"] = df["gene_id"].apply(lambda x: "mygene" if pd.notna(x) else None)

    if not df["gene_id"].isna().any():
        return df[["gene_name", "gene_id", "source"]]

    # ── Fallback disabled ────────────────────────────────────────────────────
    if not use_opentargets_fallback:
        missing = df.loc[df["gene_id"].isna(), "gene_name"].tolist()
        print(f"[warn] Unresolved after MyGene (fallback disabled): {missing}")
        # BUG FIXED: original had no `return` here, so the function fell
        # through into Stage 2 regardless of the flag value.
        return df[["gene_name", "gene_id", "source"]]

    # ── Stage 2: OpenTargets per-gene ────────────────────────────────────────
    # BUG FIXED: original iterated over a stale boolean mask captured before
    # any mutations, meaning already-resolved rows could be re-queried if the
    # loop were ever parallelised.  Use a live index expression instead.
    for idx in df.index[df["gene_id"].isna()]:
        gene  = df.at[idx, "gene_name"]
        ot_id = get_top_opentarget_target_id(
            gene_symbol=gene,
            prefer_exact_symbol=True,
            timeout=timeout,
        )

        if ot_id is not None:
            df.at[idx, "gene_id"] = ot_id
            df.at[idx, "source"]  = "opentargets"
        else:
            print(
                f"[warn] Could not resolve '{gene}' via MyGene or OpenTargets. "
                f"Possible typo or ambiguous gene-family prefix."
            )

    return df[["gene_name", "gene_id", "source"]]


# =============================================================================
# ── DISEASE RESOLVERS ─────────────────────────────────────────────────────────
# =============================================================================

_MAP_IDS_QUERY = """
query MapDiseases($terms: [String!]!) {
  mapIds(queryTerms: $terms, entityNames: ["disease"]) {
    mappings {
      term
      hits { id name entity score }
    }
  }
}
"""

_MAP_DRUG_IDS_QUERY = """
query MapDrugs($terms: [String!]!) {
  mapIds(queryTerms: $terms, entityNames: ["drug"]) {
    mappings {
      term
      hits { id name entity score }
    }
  }
}
"""

_DISEASE_SEARCH_QUERY_ASYNC = """
query DiseaseSearch($term: String!, $size: Int!) {
  search(queryString: $term, entityNames: ["disease"], page: {index: 0, size: $size}) {
    hits { id name }
  }
}
"""


# ---------------------------------------------------------------------------
# OpenTargets mapIds bulk resolver (PRIMARY — chunked HTTP requests)
# ---------------------------------------------------------------------------

def resolve_diseases_opentargets_bulk(
    terms: List[str],
    timeout: int = 15,
    strict_score_one: bool = True,
    max_terms_per_request: int = 200,
    max_chars_per_request: int = 10000,
) -> Dict[str, Optional[str]]:
    """
    Bulk-resolve disease names to EFO/MONDO IDs via OpenTargets ``mapIds``.

    Terms are resolved in chunked ``mapIds`` HTTP requests.

    Matching behaviour
    ------------------
    ✅ Case-insensitive           ``"BREAST CANCER"`` == ``"breast cancer"``
    ✅ Synonym-aware              ``"mammary cancer"`` → breast cancer
    ✅ Common abbreviations       ``"T2DM"``, ``"IBD"``
    ✅ Alternate punctuation      ``"Alzheimer's disease"`` → Alzheimer disease
    ❌ Spacing variants           ``"type2 diabetes"`` → no hit
    ❌ Truncations / informal     ``"alzheimers"`` → no hit  (try OLS fallback)

    Ontology preference: EFO_ > MONDO_ > other.

    Parameters
    ----------
    terms : list[str]
        Normalised disease names (output of ``_normalize_disease_term``).
    timeout : int
        HTTP timeout in seconds.
    strict_score_one : bool
        If ``True`` (default), accept only score==1.0 hits (exact/synonym-level
        matches). If ``False``, take top ontology-priority hit regardless of
        score.

    Returns
    -------
    dict[str, str | None]
        ``{"breast cancer": "EFO_0000305", "unknown": None, ...}``
    """
    if not terms:
        return {}

    import json

    query_terms = list(
        dict.fromkeys(
            str(t).strip()
            for t in terms
            if isinstance(t, str) and str(t).strip()
        )
    )
    if not query_terms:
        return {}

    max_terms_per_request = max(int(max_terms_per_request), 1)
    max_chars_per_request = max(int(max_chars_per_request), 256)

    result: Dict[str, Optional[str]] = {t: None for t in query_terms}

    def _chunk_terms(items: List[str]) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 2  # [] brackets
        for term in items:
            term_len = len(json.dumps(term, ensure_ascii=False))
            extra = term_len + (1 if current else 0)
            if current and (
                len(current) >= max_terms_per_request
                or (current_len + extra) > max_chars_per_request
            ):
                chunks.append(current)
                current = [term]
                current_len = 2 + term_len
            else:
                current.append(term)
                current_len += extra
        if current:
            chunks.append(current)
        return chunks

    def _process_chunk(batch_terms: List[str]) -> None:
        if not batch_terms:
            return
        try:
            resp = requests.post(
                OT_GRAPHQL_URL,
                json={"query": _MAP_IDS_QUERY, "variables": {"terms": batch_terms}},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                raise ValueError(f"GraphQL errors: {data['errors']}")

            for mapping in data.get("data", {}).get("mapIds", {}).get("mappings", []):
                term = str(mapping.get("term", "")).strip()
                if not term or term not in result:
                    continue

                hits = mapping.get("hits") or []
                if not hits:
                    continue

                hits_to_use = hits
                if strict_score_one:
                    hits_to_use = [
                        h for h in hits
                        if float(h.get("score", 0.0)) == 1.0
                    ]
                    if not hits_to_use:
                        continue

                efo_hits = [h for h in hits_to_use if str(h.get("id", "")).startswith("EFO_")]
                mondo_hits = [h for h in hits_to_use if str(h.get("id", "")).startswith("MONDO_")]
                best = (efo_hits or mondo_hits or hits_to_use)[0]
                best_id = str(best.get("id", ""))
                if best_id:
                    result[term] = _to_ot_disease_id_format(best_id)

        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            msg = str(exc).lower()
            is_too_large = (
                status == 413
                or "request entity too large" in msg
                or "payload too large" in msg
            )
            if is_too_large and len(batch_terms) > 1:
                mid = len(batch_terms) // 2
                _process_chunk(batch_terms[:mid])
                _process_chunk(batch_terms[mid:])
                return
            print(
                f"[OpenTargets] bulk disease resolve failed for batch(size={len(batch_terms)}): {exc}"
            )

    for chunk in _chunk_terms(query_terms):
        _process_chunk(chunk)

    return result


def resolve_drugs_opentargets_bulk(
    terms: List[str],
    timeout: int = 15,
    strict_score_one: bool = True,
    max_terms_per_request: int = 200,
    max_chars_per_request: int = 10000,
) -> Dict[str, Optional[str]]:
    """
    Bulk-resolve drug names to OpenTargets drug IDs (CHEMBL...) via ``mapIds``.

    Parameters
    ----------
    terms : list[str]
        Drug names in raw or normalized form.
    timeout : int
        HTTP timeout in seconds.
    strict_score_one : bool
        If ``True``, accept only score==1 exact/synonym matches.

    Returns
    -------
    dict[str, str | None]
        Keys are lower/space-normalized input terms.
    """
    if not terms:
        return {}

    query_terms = list(
        dict.fromkeys(
            str(t).strip()
            for t in terms
            if isinstance(t, str) and str(t).strip()
        )
    )
    if not query_terms:
        return {}

    import json

    max_terms_per_request = max(int(max_terms_per_request), 1)
    max_chars_per_request = max(int(max_chars_per_request), 256)

    result: Dict[str, Optional[str]] = {
        _norm_ws(t.lower()): None for t in query_terms
    }

    def _chunk_terms(items: List[str]) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 2  # [] brackets
        for term in items:
            term_len = len(json.dumps(term, ensure_ascii=False))
            extra = term_len + (1 if current else 0)
            if current and (
                len(current) >= max_terms_per_request
                or (current_len + extra) > max_chars_per_request
            ):
                chunks.append(current)
                current = [term]
                current_len = 2 + term_len
            else:
                current.append(term)
                current_len += extra
        if current:
            chunks.append(current)
        return chunks

    def _process_chunk(batch_terms: List[str]) -> None:
        if not batch_terms:
            return
        try:
            resp = requests.post(
                OT_GRAPHQL_URL,
                json={"query": _MAP_DRUG_IDS_QUERY, "variables": {"terms": batch_terms}},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                raise ValueError(f"GraphQL errors: {data['errors']}")

            for mapping in data.get("data", {}).get("mapIds", {}).get("mappings", []):
                term = _norm_ws(str(mapping.get("term", "")).lower())
                if not term or term not in result:
                    continue

                hits = mapping.get("hits") or []
                if not hits:
                    continue

                hits_to_use = hits
                if strict_score_one:
                    hits_to_use = [
                        h for h in hits
                        if float(h.get("score", 0.0)) == 1.0
                    ]
                    if not hits_to_use:
                        continue

                chembl_hits = [
                    h for h in hits_to_use
                    if isinstance(h.get("id"), str) and h["id"].startswith("CHEMBL")
                ]
                best = (chembl_hits or hits_to_use)[0]
                best_id = str(best.get("id", ""))
                if best_id:
                    result[term] = best_id

        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            msg = str(exc).lower()
            is_too_large = (
                status == 413
                or "request entity too large" in msg
                or "payload too large" in msg
            )
            if is_too_large and len(batch_terms) > 1:
                mid = len(batch_terms) // 2
                _process_chunk(batch_terms[:mid])
                _process_chunk(batch_terms[mid:])
                return
            print(
                f"[OpenTargets] bulk drug resolve failed for batch(size={len(batch_terms)}): {exc}"
            )

    for chunk in _chunk_terms(query_terms):
        _process_chunk(chunk)

    return result


# ---------------------------------------------------------------------------
# OLS per-term fallback
# ---------------------------------------------------------------------------

def resolve_disease_ols(term: str, timeout: int = 10) -> Optional[str]:
    """
    Resolve a single disease name to an EFO ID via the OLS4 API.

    Strict exact-match against EFO ``label`` and ``synonym`` fields.
    Intended as a fallback for terms that ``mapIds`` cannot handle.

    Parameters
    ----------
    term : str     Normalised disease name.
    timeout : int  HTTP timeout in seconds.

    Returns
    -------
    str | None  e.g. ``"EFO_0002690"`` or ``None``.
    """
    term_norm = _normalize_disease_term(term)
    if not term_norm:
        return None

    params = {
        "q":           term_norm,
        "ontology":    "efo",
        "queryFields": "label,synonym",
        "exact":       "true",
        "rows":        1,
        "lang":        "en",
    }

    try:
        resp = requests.get(OLS_SEARCH_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        if not docs:
            return None
        return _to_ot_disease_id_format(docs[0]["short_form"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Disease orchestrator
# ---------------------------------------------------------------------------

async def get_disease_ids_fast(
    diseases: List[str],
    use_ols_fallback: bool = False,
    use_groq_canonical_fallback: bool = True,
    use_llama_canonical_fallback: Optional[bool] = None,
    groq_api_key: Optional[str] = None,
    llama_api_key: Optional[str] = None,
) -> pd.DataFrame:
    """
    Resolve disease names to IDs with a strict two-stage pipeline.

    Stage 1 — OpenTargets ``mapIds`` exact matching (score==1 only).
    Stage 2 — Llama canonicalisation for unresolved terms, then OpenTargets
              exact re-check on those canonical names.
    Stage 3 — Optional OLS fallback for any residual misses.

    Parameters
    ----------
    diseases : list[str]
        Disease names in any casing/punctuation.
    use_ols_fallback : bool
        If ``True``, attempt OLS for terms still unresolved after Stages 1+2.
        Default is ``False`` so unresolved values remain unchanged by default.
    use_groq_canonical_fallback : bool
        If ``True`` (default), canonicalise unresolved terms with Llama
        (served through Groq API) before final resolution.
    use_llama_canonical_fallback : bool | None
        Alias for ``use_groq_canonical_fallback``. If provided, takes
        precedence.
    groq_api_key : str | None
        Optional Groq API key for Llama calls. Falls back to
        ``GROQ_API_KEY`` environment variable if not provided.
    llama_api_key : str | None
        Alias of ``groq_api_key`` for clarity. If provided, takes precedence.

    Returns
    -------
    pd.DataFrame
        Columns: ``disease_name`` | ``disease_id`` | ``source``

        ``source`` values:
        ``"OpenTargets"`` | ``"Llama"`` | ``"OLS"`` | ``None``

    Notes
    -----
    """
    diseases_norm = [_normalize_disease_term(d) for d in diseases]
    df = pd.DataFrame({"disease_name": diseases, "_disease_norm": diseases_norm})
    df["disease_id"] = None
    df["source"] = None

    # Stage 1: strict OpenTargets exact/synonym match only.
    ot_exact = resolve_diseases_opentargets_bulk(
        diseases_norm,
        strict_score_one=True,
    )

    for idx, term in df["_disease_norm"].items():
        ot_id = ot_exact.get(term)
        if ot_id:
            ot_id = _to_ot_disease_id_format(ot_id)
            df.at[idx, "disease_id"] = ot_id
            df.at[idx, "source"] = "OpenTargets"
            _print_confident_mapping(
                entity_type="disease",
                raw_name=str(df.at[idx, "disease_name"]),
                resolved_id=str(ot_id),
                source="OpenTargets",
            )

    # Stage 2: unresolved -> Llama canonical label -> OT exact.
    unresolved_mask = df["disease_id"].isna() & df["disease_name"].notna()
    unresolved_names = (
        df.loc[unresolved_mask, "disease_name"]
        .astype(str)
        .str.strip()
        .tolist()
    )
    unresolved_names = list(dict.fromkeys([n for n in unresolved_names if n]))

    use_canonical_fallback = (
        use_groq_canonical_fallback
        if use_llama_canonical_fallback is None
        else use_llama_canonical_fallback
    )

    canonical_map: Dict[str, Optional[str]] = {}
    if use_canonical_fallback and unresolved_names:
        try:
            model_api_key = llama_api_key or groq_api_key
            canonical_input = {name: None for name in unresolved_names}
            canonical_map = await canonicalise_disease_dict(
                canonical_input,
                groq_api_key=model_api_key,
                llama_api_key=model_api_key,
            )
        except Exception as exc:
            print(f"[warn] Llama disease canonicalisation failed: {exc}")
            canonical_map = {}

    canonical_terms = list(
        dict.fromkeys(
            _normalize_disease_term(v)
            for v in canonical_map.values()
            if isinstance(v, str) and v.strip()
        )
    )
    canonical_to_id: Dict[str, Optional[str]] = {}
    if canonical_terms:
        canonical_to_id = resolve_diseases_opentargets_bulk(
            canonical_terms,
            strict_score_one=True,
        )

    for idx, row in df.loc[unresolved_mask].iterrows():
        raw_name = str(row["disease_name"]).strip()
        canonical_name = canonical_map.get(raw_name)
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            continue
        canonical_norm = _normalize_disease_term(canonical_name)
        canonical_id = canonical_to_id.get(canonical_norm)
        if canonical_id:
            canonical_id = _to_ot_disease_id_format(canonical_id)
            df.at[idx, "disease_id"] = canonical_id
            df.at[idx, "source"] = LLAMA_CANONICAL_SOURCE
            _print_confident_mapping(
                entity_type="disease",
                raw_name=raw_name,
                canonical_name=canonical_name,
                resolved_id=str(canonical_id),
                source=LLAMA_CANONICAL_SOURCE,
            )

    # Stage 3 (optional): OLS fallback for still-unresolved rows.
    if use_ols_fallback:
        unresolved_mask = df["disease_id"].isna() & df["_disease_norm"].notna()
        still_missing = (
            df.loc[unresolved_mask, "_disease_norm"]
            .astype(str)
            .str.strip()
            .tolist()
        )
        still_missing = list(dict.fromkeys([n for n in still_missing if n]))

        if still_missing:
            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(resolve_disease_ols, t): t for t in still_missing}
                ols_lookup: Dict[str, Optional[str]] = {}
                for fut in as_completed(futures):
                    term = futures[fut]
                    ols_lookup[term] = fut.result()

            for idx, term in df.loc[unresolved_mask, "_disease_norm"].items():
                ols_id = ols_lookup.get(term)
                if ols_id:
                    ols_id = _to_ot_disease_id_format(ols_id)
                    df.at[idx, "disease_id"] = ols_id
                    df.at[idx, "source"] = "OLS"
                    _print_confident_mapping(
                        entity_type="disease",
                        raw_name=str(df.at[idx, "disease_name"]),
                        resolved_id=str(ols_id),
                        source="OLS",
                    )

    unresolved = df["disease_id"].isna()
    if unresolved.any():
        missing = df.loc[unresolved, "disease_name"].tolist()
        print(f"[warn] Unresolved diseases after OpenTargets/Llama/OLS: {missing}")

    return df[["disease_name", "disease_id", "source"]]


# =============================================================================
# ── DRUG RESOLVERS ────────────────────────────────────────────────────────────
# =============================================================================

_DRUG_SEARCH_QUERY = """
query DrugSearch($term: String!, $size: Int!) {
  search(queryString: $term, entityNames: ["drug"], page: { index: 0, size: $size }) {
    hits { id name }
  }
}
"""

_DRUG_BY_ID_QUERY = """
query DrugById($id: String!) {
  drug(chemblId: $id) {
    id
    name
    synonyms
  }
}
"""


async def _resolve_drug(
    session: aiohttp.ClientSession,
    drug_name: str,
    sem: asyncio.Semaphore,
) -> tuple[str, str | None]:
    """
    Resolve a single drug name to a ChEMBL ID asynchronously.

    Two-stage:

    1. Search for candidate ChEMBL IDs by name.
    2. For each candidate, fetch full metadata and check name/synonyms for
       an exact match.

    Parameters
    ----------
    session : aiohttp.ClientSession  Shared HTTP session.
    drug_name : str                  Raw drug name.
    sem : asyncio.Semaphore          Concurrency limiter.

    Returns
    -------
    tuple[str, str | None]  ``(drug_name, chembl_id_or_None)``
    """
    term_norm = _normalize_drug_term(drug_name)   # lower-case
    term_u    = term_norm.upper()                 # for exact-match comparison only

    search_payload = {
        "query": _DRUG_SEARCH_QUERY,
        "variables": {"term": term_norm, "size": 10},
    }

    async with sem:
        try:
            async with session.post(
                OT_GRAPHQL_URL,
                json=search_payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return drug_name, None

                data = await resp.json()
                hits = data.get("data", {}).get("search", {}).get("hits", [])
                if not hits:
                    return drug_name, None

                for h in hits:
                    chembl_id    = h["id"]
                    meta_payload = {
                        "query": _DRUG_BY_ID_QUERY,
                        "variables": {"id": chembl_id},
                    }

                    async with session.post(
                        OT_GRAPHQL_URL,
                        json=meta_payload,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as meta_resp:
                        if meta_resp.status != 200:
                            continue
                        meta = await meta_resp.json()
                        drug = meta.get("data", {}).get("drug")
                        if not drug:
                            continue
                        if _exact_match(term_u, drug.get("name"), drug.get("synonyms")):
                            return drug_name, chembl_id

                return drug_name, None

        except Exception:
            return drug_name, None


async def get_chembl_ids_fast(drug_list: List[str]) -> pd.DataFrame:
    """
    Resolve drug names to ChEMBL IDs with exact-first + canonical fallback.

    Stage 1 — OpenTargets exact/synonym validation from raw input names.
    Stage 2 — For unresolved names, Llama canonicalisation then OpenTargets
              exact re-check on canonical names.

    Parameters
    ----------
    drug_list : list[str]
        Raw drug names, e.g. ``["ibuprofen", "Aspirin", "METFORMIN"]``.

    Returns
    -------
    pd.DataFrame
        Columns: ``drug_name`` | ``drug_id`` | ``source``
        ``drug_id`` is a ChEMBL ID (e.g. ``"CHEMBL521"``) or ``NaN``.

    Notes
    -----
    - Source values: ``"OpenTargets"`` | ``"Llama"`` | ``None``.
    - Unresolved rows remain unchanged (`drug_id` stays null).
    - Duplicate drug names are preserved row-wise.
    """
    # Keep row order/duplicates from input but resolve IDs via strict OT mapIds.
    df = pd.DataFrame({"drug_name": list(drug_list)})
    df["drug_id"] = None
    df["source"] = None

    stage1_terms = (
        df["drug_name"]
        .dropna()
        .astype(str)
        .str.strip()
        .tolist()
    )
    stage1_terms = list(dict.fromkeys([t for t in stage1_terms if t]))
    stage1_lookup = resolve_drugs_opentargets_bulk(
        stage1_terms,
        strict_score_one=True,
    )

    for idx, raw_name in df["drug_name"].items():
        raw_name = str(raw_name).strip()
        if not raw_name:
            continue
        cid = stage1_lookup.get(_norm_ws(raw_name.lower()))
        if cid:
            df.at[idx, "drug_id"] = cid
            df.at[idx, "source"] = "OpenTargets"

    # Print direct strict OpenTargets matches from Stage 1.
    ot_mask = df["source"].eq("OpenTargets") & df["drug_name"].notna() & df["drug_id"].notna()
    for _, row in df.loc[ot_mask, ["drug_name", "drug_id"]].iterrows():
        _print_confident_mapping(
            entity_type="drug",
            raw_name=str(row["drug_name"]),
            resolved_id=str(row["drug_id"]),
            source="OpenTargets",
        )

    unresolved_mask = df["drug_id"].isna() & df["drug_name"].notna()
    unresolved_names = (
        df.loc[unresolved_mask, "drug_name"]
        .astype(str)
        .str.strip()
        .tolist()
    )
    unresolved_names = list(dict.fromkeys([n for n in unresolved_names if n]))

    if not unresolved_names:
        return df

    # Canonicalise unresolved names via Llama + OT verification.
    try:
        canonical_input = {name: None for name in unresolved_names}
        canonical_map = await canonicalise_drug_dict(canonical_input)
    except Exception as exc:
        print(f"[warn] Llama drug canonicalisation failed: {exc}")
        return df

    canonical_terms = list(
        dict.fromkeys(
            str(v).strip()
            for v in canonical_map.values()
            if isinstance(v, str) and str(v).strip()
        )
    )
    if not canonical_terms:
        return df

    canonical_lookup = resolve_drugs_opentargets_bulk(
        canonical_terms,
        strict_score_one=True,
    )

    # Write back resolved canonical IDs to previously unresolved raw rows.
    for idx, raw_name in df.loc[unresolved_mask, "drug_name"].items():
        raw_name = str(raw_name).strip()
        canonical_name = canonical_map.get(raw_name)
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            continue

        ckey = _norm_ws(canonical_name.lower().strip())
        cid = canonical_lookup.get(ckey)
        if cid:
            df.at[idx, "drug_id"] = cid
            df.at[idx, "source"] = LLAMA_CANONICAL_SOURCE
            _print_confident_mapping(
                entity_type="drug",
                raw_name=raw_name,
                canonical_name=canonical_name,
                resolved_id=str(cid),
                source=LLAMA_CANONICAL_SOURCE,
            )

    return df


# =============================================================================
# ── VALIDATION UTILITY ────────────────────────────────────────────────────────
# =============================================================================

def validate_id_dataframe(
    df: pd.DataFrame,
    *,
    model: str,
    question_key: str,
    run_number: int,
) -> bool:
    """
    Validate that a resolver output DataFrame meets structural contracts.

    Checks
    ------
    1. Object is a ``pd.DataFrame``.
    2. At least one column and one row.
    3. Every column name ends with ``"_id"``.
    4. No NaN values anywhere.

    Parameters
    ----------
    df : pd.DataFrame    Output from any ``get_*_ids_fast`` function.
    model : str          Model label for log messages.
    question_key : str   Experiment identifier.
    run_number : int     Repetition index.

    Returns
    -------
    bool  ``True`` if all checks pass.
    """
    prefix = f"[{model} | {question_key} | run {run_number}]"

    def _fail(msg: str) -> bool:
        full = f"{prefix} {msg}"
        print(full)
        warnings.warn(full)
        return False

    if not isinstance(df, pd.DataFrame):
        return _fail(f"Expected pandas DataFrame, got {type(df).__name__}")

    if df.empty or df.shape[1] == 0:
        return _fail("DataFrame is empty or has no columns")

    bad_cols = [c for c in df.columns if not str(c).endswith("_id")]
    if bad_cols:
        return _fail(f"Invalid columns (must end with '_id'): {bad_cols}")

    if df.isna().any().any():
        nan_locs = (
            df.isna()
            .stack()
            .loc[lambda s: s]
            .index
            .tolist()[:5]
        )
        return _fail(f"NaN values detected (first 5 locations): {nan_locs}")

    return True






def _ot_verify_bulk_disease(
    suggestions: dict[str, Optional[str]],
    timeout: int = 15,
) -> dict[str, Optional[str]]:
    """
    Verify LLM suggestions against OpenTargets mapIds in one HTTP request.

    Only accepts score == 1.0 (exact label or known synonym).
    Prefers EFO_ IDs over MONDO_.

    Parameters
    ----------
    suggestions : dict[original_name, suggested_name_or_None]

    Returns
    -------
    dict[original_name, ot_confirmed_name_or_None]
    """


    import asyncio
    import json
    import logging
    import os
    import time
    from typing import Optional

    import requests

    OT_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"

    unique_terms = list({s for s in suggestions.values() if s is not None})

    if not unique_terms:
        return {k: None for k in suggestions}

    log.info("[OT] Verifying %d unique suggestions...", len(unique_terms))

    _MAP_IDS_QUERY = """
    query MapDiseases($terms: [String!]!) {
    mapIds(queryTerms: $terms, entityNames: ["disease"]) {
        mappings {
        term
        hits { id name score }
        }
    }
    }
    """


    try:
        resp = requests.post(
            OT_GRAPHQL_URL,
            json={"query": _MAP_IDS_QUERY, "variables": {"terms": unique_terms}},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            log.error("[OT] GraphQL errors: %s", data["errors"])
            return {k: None for k in suggestions}

        # Build: suggested_term → OT confirmed name (score == 1 only)
        confirmed: dict[str, str] = {}
        for mapping in data["data"]["mapIds"]["mappings"]:
            hits = [h for h in (mapping.get("hits") or []) if h.get("score", 0) == 1]
            if not hits:
                continue
            efo   = [h for h in hits if h["id"].startswith("EFO_")]
            mondo = [h for h in hits if h["id"].startswith("MONDO_")]
            best  = (efo or mondo or hits)[0]
            confirmed[mapping["term"]] = best["name"]
            log.info("[OT] %r → %r (%s)", mapping["term"], best["name"], best["id"])

        return {
            orig: confirmed.get(sugg) if sugg else None
            for orig, sugg in suggestions.items()
        }

    except Exception as exc:
        log.error("[OT] Request failed: %s", exc)
        return {k: None for k in suggestions}


async def canonicalise_disease_dict(
    disease_dict: dict[str, None],
    groq_api_key: Optional[str] = None,
    llama_api_key: Optional[str] = None,
    *,
    max_batch_chars: int = 6000,
    max_batch_items: int = 80,
    max_concurrency: int = 2,
) -> dict[str, Optional[str]]:
    """
    Canonicalise disease names via Llama LLM + OpenTargets verification.

    Step 1 — Chunked Llama calls: suggest canonical forms for inputs.
    Step 2 — One OT mapIds call: verifies each suggestion (score == 1 only).

    Parameters
    ----------
    disease_dict : dict[str, None]
        Keys are raw disease names, values are None.
        e.g. {'Acid-reflux disorder': None, 'Acute heart failure': None}

    groq_api_key : str | None
        Groq API key used to access Llama. Falls back to GROQ_API_KEY
        environment variable.
    llama_api_key : str | None
        Alias of ``groq_api_key`` for clarity. If provided, takes precedence.
    max_batch_chars : int
        Approximate JSON payload budget per LLM batch.
    max_batch_items : int
        Hard cap on names per LLM batch to prevent oversized JSON replies.
    max_concurrency : int
        Maximum concurrent LLM calls.

    Returns
    -------
    dict[str, str | None]
        Same keys. Value is OT-confirmed canonical name, or None if:
          - Term is a biological process, not a disease
          - LLM was unsure
          - OT could not confirm the suggestion at score == 1

    Example
    -------
    >>> result = asyncio.run(canonicalise_disease_dict({
    ...     'Acute heart failure':                 None,
    ...     'Amyotrophic lateral sclerosis (ALS)': None,
    ...     'Anabolic metabolism':                 None,
    ... }))
    >>> # {'Acute heart failure':                 'acute heart failure',
    >>> #  'Amyotrophic lateral sclerosis (ALS)': 'amyotrophic lateral sclerosis',
    >>> #  'Anabolic metabolism':                 None}
    """


    import asyncio
    import json
    import logging
    import os
    import time
    from typing import Optional

    import requests
    from groq import Groq
    key = llama_api_key or groq_api_key or os.environ.get("GROQ_API_KEY")

    if not key:
        raise EnvironmentError("Set GROQ_API_KEY env var or pass groq_api_key=.")

    if not disease_dict:
        return {}


    _SYSTEM_PROMPT = """
        TASK: DISEASE_CANONICALIZATION_ONLY
        You are a biomedical disease nomenclature expert.

        Given a JSON list of disease names, return a JSON object mapping each name
        to its canonical disease label as used by the Open Targets Platform.

        ABSOLUTE CERTAINTY RULE (CRITICAL)
        ---------------------------------
        You may change a name ONLY IF ALL of the following are true:
        1. The input refers to a disease (not a process, symptom, phenotype, or mechanism).
        2. The canonical label is a widely accepted disease name used verbatim by Open Targets.
        3. The mapping is unambiguous (no competing diseases share this name or abbreviation).
        4. You would make the same mapping in a curated biomedical database without hesitation.

        If ANY condition fails, return null.

        Additional Rules
        ----------------
        - If the input already appears to be a canonical Open Targets disease label, return it unchanged.
        - Return null for biological processes or mechanisms:
        e.g. "Carcinogenesis", "Anabolic metabolism", "Tumorigenesis", "Inflammation"
        - Return null for ambiguous abbreviations unless globally unique (e.g. "ALS" is allowed).
        - Never guess. When in doubt, return null.
        - Return ONLY valid JSON. No markdown. No explanation.

        Example:
        Input:  ["Acute pain", "Anabolic metabolism", "ALS"]
        Output: {
        "Acute pain": "pain",
        "Anabolic metabolism": null,
        "ALS": "amyotrophic lateral sclerosis"
        }
            """


    client = Groq(api_key=key)
    names = list(disease_dict.keys())

    batches = _chunk_list_by_char_budget(
        names,
        max_chars=max_batch_chars,
        max_items=max_batch_items,
    )
    log.info(
        "[LLM] Total %d names split into %d batches (max_batch_chars=%d, max_batch_items=%d, model=%s).",
        len(names),
        len(batches),
        max_batch_chars,
        max_batch_items,
        LLAMA_CANONICAL_MODEL,
    )

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _call_one_batch(batch_names: list[str]) -> dict[str, Optional[str]]:
        log.info("[LLM] Batch size: %d", len(batch_names))
        start = time.perf_counter()
        async with sem:
            reply = await asyncio.to_thread(
                client.chat.completions.create,
                model=LLAMA_CANONICAL_MODEL,
                temperature=0,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(batch_names, ensure_ascii=False)},
                ],
            )

        elapsed = time.perf_counter() - start
        answer = reply.choices[0].message.content.strip()
        log.info("[LLM] Batch done in %.2fs", elapsed)

        # Strip accidental markdown fences
        if answer.startswith("```"):
            parts = answer.split("```")
            answer = parts[1].lstrip("json").strip() if len(parts) > 1 else answer

        try:
            parsed = json.loads(answer)
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("Expected top-level object", answer, 0)
            return parsed
        except json.JSONDecodeError as exc:
            log.error("[LLM] JSON parse failed: %s | raw=%r", exc, answer)
            # Retry with smaller batches when a response is truncated.
            if len(batch_names) > 1:
                mid = len(batch_names) // 2
                left = batch_names[:mid]
                right = batch_names[mid:]
                log.warning(
                    "[LLM] Retrying disease batch by splitting %d -> %d + %d",
                    len(batch_names),
                    len(left),
                    len(right),
                )
                lmap, rmap = await asyncio.gather(
                    _call_one_batch(left),
                    _call_one_batch(right),
                )
                merged = dict(lmap)
                merged.update(rmap)
                return merged
            return {}

    batch_results = await asyncio.gather(*[_call_one_batch(b) for b in batches])

    suggestions: dict[str, Optional[str]] = {}
    for parsed in batch_results:
        suggestions.update(parsed)

    # Ensure every input name is present (model may skip some)
    for name in names:
        if name not in suggestions:
            log.warning("[LLM] Model skipped %r — defaulting to None", name)
            suggestions[name] = None
        else:
            log.info("[LLM] %r → %r", name, suggestions[name])

    # ── Step 2: one OT verification call ─────────────────────────────────────
    result = _ot_verify_bulk_disease(suggestions)

    n = sum(1 for v in result.values() if v is not None)
    log.info("Done: %d / %d confirmed | %d → None", n, len(names), len(names) - n)

    return result


def _ot_verify_bulk_drug(
    suggestions: dict[str, Optional[str]],
    timeout: int = 15,
) -> dict[str, Optional[str]]:
    """
    Verify LLM suggestions against OpenTargets mapIds in one HTTP request.

    Only accepts score == 1.0 (exact label or known synonym).
    Prefers ChEMBL-like IDs (CHEMBL...) if multiple exact hits exist.

    Parameters
    ----------
    suggestions : dict[original_name, suggested_name_or_None]

    Returns
    -------
    dict[original_name, ot_confirmed_name_or_None]
    """

    import asyncio
    import json
    import logging
    import os
    import time
    from typing import Optional

    from groq import Groq

    OT_GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"

    unique_terms = list({s for s in suggestions.values() if isinstance(s, str) and s.strip()})
    if not unique_terms:
        return {k: None for k in suggestions}

    log.info("[OT] Verifying %d unique drug suggestions...", len(unique_terms))

    _MAP_IDS_QUERY = """
    query MapDrugs($terms: [String!]!) {
      mapIds(queryTerms: $terms, entityNames: ["drug"]) {
        mappings {
          term
          hits { id name score }
        }
      }
    }
    """

    try:
        resp = requests.post(
            OT_GRAPHQL_URL,
            json={"query": _MAP_IDS_QUERY, "variables": {"terms": unique_terms}},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            log.error("[OT] GraphQL errors: %s", data["errors"])
            return {k: None for k in suggestions}

        confirmed: dict[str, str] = {}

        for mapping in data["data"]["mapIds"]["mappings"]:
            hits = [h for h in (mapping.get("hits") or []) if float(h.get("score", 0)) == 1.0]
            if not hits:
                continue

            # Prefer CHEMBL IDs if present; else take first exact hit
            chembl = [h for h in hits if isinstance(h.get("id"), str) and h["id"].startswith("CHEMBL")]
            best = (chembl or hits)[0]

            confirmed[mapping["term"]] = best["name"]
            log.info("[OT] %r → %r (%s)", mapping["term"], best["name"], best["id"])

        return {
            orig: confirmed.get(sugg) if sugg else None
            for orig, sugg in suggestions.items()
        }

    except Exception as exc:
        log.error("[OT] Request failed: %s", exc)
        return {k: None for k in suggestions}
    



def _chunk_list_by_char_budget(
    items: list[str],
    max_chars: int,
    max_items: int = 80,
) -> list[list[str]]:
    """
    Split strings into JSON-safe batches by both payload size and item count.

    This guards against oversized LLM prompts and oversized JSON responses.
    """
    import json

    max_chars = max(int(max_chars), 256)
    max_items = max(int(max_items), 1)

    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 2  # [] brackets

    for raw in items:
        s = "" if raw is None else str(raw)

        # Exact contribution once JSON-escaped.
        item_len = len(json.dumps(s, ensure_ascii=False))
        sep_len = 1 if current else 0 
        extra_len = item_len + sep_len

        if current and (
            (current_len + extra_len > max_chars) or (len(current) >= max_items)
        ):
            chunks.append(current)
            current = [s]
            current_len = 2 + item_len
        else:
            current.append(s)
            current_len += extra_len

    if current:
        chunks.append(current)

    return chunks


async def canonicalise_drug_dict(
    drug_dict: dict[str, None],
    groq_api_key: Optional[str] = None,
    llama_api_key: Optional[str] = None,
    *,
    max_batch_chars: int = 6000,    # smaller batch => safer JSON responses
    max_batch_items: int = 80,      # hard cap by item count as well
    max_concurrency: int = 3,       # concurrent Groq calls (be gentle)
) -> dict[str, Optional[str]]:
    """
    Canonicalise drug names via Llama LLM + OpenTargets verification (score==1).

    Fixes long-context errors by batching names into smaller chunks.
    """
    import asyncio
    import json
    import logging
    import os
    import time
    from typing import Optional

    from groq import Groq


    key = llama_api_key or groq_api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise EnvironmentError("Set GROQ_API_KEY env var or pass groq_api_key=.")
    if not drug_dict:
        return {}

    _SYSTEM_PROMPT = """
            TASK: DRUG_CANONICALIZATION_ONLY
            You are a biomedical drug nomenclature expert.

            Given a JSON list of drug names, return a JSON object mapping each name
            to its canonical drug label as used by the Open Targets Platform, or null if unsure.

            ABSOLUTE CERTAINTY RULE (CRITICAL)
            ---------------------------------
            You may change a name ONLY IF ALL of the following are true:
            1. The input refers to a specific drug/compound (not a class, mechanism, procedure).
            2. The canonical label is widely accepted and used by Open Targets.
            3. The mapping is unambiguous (no competing drugs share the label/abbrev).
            4. You would make the same mapping in a curated database without hesitation.

            If ANY condition fails, return null.

            Additional Rules
            ----------------
            - If the input already appears canonical, return it unchanged.
            - Return null for drug classes (e.g., "beta blockers") unless a specific drug is given.
            - Return null for mechanisms/processes (e.g., "angiogenesis inhibition").
            - Never guess. When in doubt, return null.
            - Return ONLY valid JSON. No markdown. No explanation.

            Example:
            Input:  ["Gleevec", "acetylsalicylic acid", "beta blockers"]
            Output: {"Gleevec": "imatinib", "acetylsalicylic acid": "aspirin", "beta blockers": null}
            """.strip()

    client = Groq(api_key=key)
    raw_drug_names = list(drug_dict.keys())

    # ---- Batch names to avoid context-length errors
    batches = _chunk_list_by_char_budget(
        raw_drug_names,
        max_chars=max_batch_chars,
        max_items=max_batch_items,
    )
    log.info(
        "[LLM] Total %d names split into %d batches (max_batch_chars=%d, max_batch_items=%d, model=%s).",
        len(raw_drug_names),
        len(batches),
        max_batch_chars,
        max_batch_items,
        LLAMA_CANONICAL_MODEL,
    )

    sem = asyncio.Semaphore(max_concurrency)

    async def _call_one_batch(batch_names: list[str]) -> dict[str, Optional[str]]:
        log.info("[LLM] Batch size: %d", len(batch_names))
        start = time.perf_counter()
        async with sem:
            reply = await asyncio.to_thread(
                client.chat.completions.create,
                model=LLAMA_CANONICAL_MODEL,
                temperature=0,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(batch_names, ensure_ascii=False)},
                ],
            )

        elapsed = time.perf_counter() - start
        answer = reply.choices[0].message.content.strip()
        log.info("[LLM] Batch done in %.2fs", elapsed)

        # Strip accidental markdown fences
        if answer.startswith("```"):
            parts = answer.split("```")
            answer = parts[1].lstrip("json").strip() if len(parts) > 1 else answer

        try:
            parsed = json.loads(answer)
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("Expected top-level object", answer, 0)
            return parsed
        except json.JSONDecodeError as exc:
            log.error("[LLM] JSON parse failed: %s | raw=%r", exc, answer)
            # Retry with smaller sub-batches when response JSON is truncated.
            if len(batch_names) > 1:
                mid = len(batch_names) // 2
                left = batch_names[:mid]
                right = batch_names[mid:]
                log.warning(
                    "[LLM] Retrying drug batch by splitting %d -> %d + %d",
                    len(batch_names),
                    len(left),
                    len(right),
                )
                lmap, rmap = await asyncio.gather(
                    _call_one_batch(left),
                    _call_one_batch(right),
                )
                merged = dict(lmap)
                merged.update(rmap)
                return merged
            return {}

    # ---- Run Groq over batches
    batch_results = await asyncio.gather(*[_call_one_batch(b) for b in batches])

    # ---- Merge suggestions
    suggestions: dict[str, Optional[str]] = {}
    for parsed in batch_results:
        for k, v in parsed.items():
            suggestions[k] = v

    # Ensure every input name exists (model might skip)
    for name in raw_drug_names:
        if name not in suggestions:
            log.warning("[LLM] Model skipped %r — defaulting to None", name)
            suggestions[name] = None

    # ---- Verify against OpenTargets (your verifier)
    verified = _ot_verify_bulk_drug(suggestions)

    n_ok = sum(1 for v in verified.values() if v is not None)
    log.info("Done: %d / %d confirmed | %d → None", n_ok, len(raw_drug_names), len(raw_drug_names) - n_ok)

    return verified
