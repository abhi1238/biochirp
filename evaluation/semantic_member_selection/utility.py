from typing import Dict, List, Optional, Union, Sequence, Set, Any
import requests
import numpy as np
from kneed import KneeLocator
from dotenv import load_dotenv
import requests
import time
import json

load_dotenv("../../.env")


def dedupe_case_insensitive(lists: list[list]) -> list:
    """Merge multiple lists, deduplicate case-insensitively, preserve original casing of first occurrence."""
    seen   = {}   # lowercase -> original casing
    result = []
    for lst in lists:
        for term in lst:
            key = term.strip().lower()
            if key and key not in seen:
                seen[key] = term.strip()
                result.append(term.strip())
    return result






def get_disease_and_descendant_synonyms(disease_name: str, batch_size: int = 50, page_size: int = 10) -> dict:

    GRAPHQL_URL = "https://api.platform.opentargets.org/api/v4/graphql"


    # ── Step 1: Search and pick TOP hit only ──────────────────────────────────
    search_query = """
    query SearchDisease($queryString: String!, $index: Int!, $size: Int!) {
        search(queryString: $queryString, entityNames: ["disease"], page: {index: $index, size: $size}) {
            total
            hits { id name entity description }
        }
    }
    """
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": search_query, "variables": {"queryString": disease_name, "index": 0, "size": page_size}},
        timeout=15
    )
    resp.raise_for_status()
    hits = resp.json()["data"]["search"]["hits"]

    if not hits:
        raise ValueError(f"No disease found for: '{disease_name}'")

    top_hit = hits[0]
    efo_id  = top_hit["id"]
    print(f"[✓] Top match : {top_hit['name']} ({efo_id})")

    # ── Step 2: Fetch synonyms + descendant IDs ───────────────────────────────
    detail_query = """
    query DiseaseDetail($efoId: String!) {
        disease(efoId: $efoId) {
            id
            name
            description
            synonyms { relation terms }
            descendants
            parents { id name }
            children { id name }
        }
    }
    """
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": detail_query, "variables": {"efoId": efo_id}},
        timeout=15
    )
    resp.raise_for_status()
    disease_data   = resp.json()["data"]["disease"]
    descendant_ids = disease_data.get("descendants") or []

    synonyms_by_relation = {}
    all_synonyms_flat    = []
    for entry in (disease_data.get("synonyms") or []):
        relation = entry.get("relation", "unknown")
        terms    = entry.get("terms") or []
        synonyms_by_relation.setdefault(relation, []).extend(terms)
        all_synonyms_flat.extend(terms)

    all_synonyms_flat = list(dict.fromkeys(all_synonyms_flat))

    print(f"[✓] Synonym relations    : {list(synonyms_by_relation.keys())}")
    print(f"[✓] Total unique synonyms: {len(all_synonyms_flat)}")
    print(f"[✓] Descendants found    : {len(descendant_ids)}")

    # ── Step 3: Batch-resolve each descendant's detail ────────────────────────
    descendant_details   = []
    all_descendant_names = []
    all_descendant_syns  = []

    descendant_query = """
    query DescendantInfo($efoId: String!) {
        disease(efoId: $efoId) {
            id
            name
            description
            synonyms { relation terms }
        }
    }
    """
    total_batches = -(-len(descendant_ids) // batch_size)
    for batch_num, i in enumerate(range(0, len(descendant_ids), batch_size), start=1):
        batch = descendant_ids[i : i + batch_size]
        print(f"  Batch {batch_num}/{total_batches} — resolving {len(batch)} descendants...")
        for did in batch:
            try:
                r = requests.post(
                    GRAPHQL_URL,
                    json={"query": descendant_query, "variables": {"efoId": did}},
                    timeout=10
                )
                r.raise_for_status()
                node = r.json()["data"]["disease"]
                if node:
                    node_syns_flat   = []
                    node_syns_by_rel = {}
                    for entry in (node.get("synonyms") or []):
                        node_syns_by_rel.setdefault(entry["relation"], []).extend(entry["terms"])
                        node_syns_flat.extend(entry.get("terms") or [])

                    node["synonyms_by_relation"] = node_syns_by_rel
                    node["synonyms_flat"]        = list(dict.fromkeys(node_syns_flat))
                    del node["synonyms"]

                    descendant_details.append(node)
                    all_descendant_names.append(node["name"])
                    all_descendant_syns.extend(node["synonyms_flat"])
            except Exception as e:
                print(f"  [!] Skipped {did}: {e}")
            time.sleep(0.1)

    all_descendant_syns = list(dict.fromkeys(all_descendant_syns))

    # ── 👈 NEW: Build combined unique list (case-insensitive) ─────────────────
    combined_all_terms = dedupe_case_insensitive([
        [disease_data["name"]],      # root disease name
        all_synonyms_flat,           # root synonyms
        all_descendant_names,        # all descendant names
        all_descendant_syns,         # all descendant synonyms
    ])

    # ── Step 4: Assemble result ───────────────────────────────────────────────
    result = {
        "id":                    disease_data["id"],
        "name":                  disease_data["name"],
        "description":           disease_data["description"],
        "parents":               disease_data.get("parents", []),
        "children":              disease_data.get("children", []),
        "synonyms_by_relation":  synonyms_by_relation,
        "synonyms_flat":         all_synonyms_flat,
        "descendant_ids":        descendant_ids,
        "descendant_names":      all_descendant_names,
        "descendant_synonyms":   all_descendant_syns,
        "descendant_details":    descendant_details,
        "combined":    combined_all_terms,   # 👈 NEW: the unified list
    }

    print(f"\n[✓] Done!")
    print(f"    Root synonyms (flat)      : {len(all_synonyms_flat)}")
    print(f"    Descendant names (flat)   : {len(all_descendant_names)}")
    print(f"    Descendant synonyms (flat): {len(all_descendant_syns)}")
    print(f"    Combined unique terms     : {len(combined_all_terms)}")   # 👈 NEW
    return result


def compute_metrics(y_true, y_pred, universe):

    universe = set(universe)
    y_true = set(y_true) & universe
    y_pred = set(y_pred) & universe

    TP = y_pred & y_true
    FP = y_pred - y_true
    FN = y_true - y_pred
    TN = universe - (y_pred | y_true)

    tp, fp, fn, tn = map(len, (TP, FP, FN, TN))

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall    = tp / (tp + fn) if (tp + fn) else 0.0
    f1        = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    accuracy  = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "specificity": specificity,
    }


# ======================================================
# EVALUATION HELPERS
# ======================================================
def restrict_to_universe(pred: Set[str], universe: Set[str]) -> Set[str]:
    """
    Restrict predictions to the closed evaluation universe.
    Anything outside is ignored (neither FP nor TP).
    """
    return pred & universe


def llm_member_filter(
    *,
    category: str,
    single_term: str,
    string_list: List[str],
    timeout: int = 45,
) -> List[str]:
    """
    Call BioChirp LLM member selection filter.

    Parameters
    ----------
    category : str
        e.g. "disease_name", "gene_symbol", "drug_name"
    single_term : str
        Anchor term (e.g. "fever")
    string_list : List[str]
        Candidate members to filter
    timeout : int
        Request timeout (seconds)

    Returns
    -------
    List[str]
        LLM-filtered members
    """

    API_BASE = "https://biochirp.iiitd.edu.in/services/llm_filter/api"

    payload = {
        "category": category,
        "single_term": single_term,
        "string_list": string_list,
    }

    url = f"{API_BASE}/llm_member_selection_filter"

    r = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )

    r.raise_for_status()
    data = r.json()

    return data.get("value", [])


def expand_synonyms(
    *,
    disease_names: List[str] = None,
    drug_names: List[str] = None,
    gene_names: List[str] = None,
    database: str = None,
    timeout: int = 45
) -> Dict:
    """
    Call BioChirp Expand Synonyms service.

    Parameters
    ----------
    disease_names : List[str]
        Disease terms to expand (e.g. ["tuberculosis"])
    database : str
        One of {"ttd", "ctd", "hcdt"}. Defaults to "ttd" if None is passed.
    timeout : int
        Request timeout in seconds

    Returns
    -------
    dict
        Raw JSON response from expand_synonyms service
    """

    EXPAND_SYNONYMS_API = "https://biochirp.iiitd.edu.in/services/expand_synonyms/api"


# http://localhost:8032/expand_synonyms_unrestricted/api
    # # FIX: Fall back to "ttd" if database is None to avoid sending None as query param
    # if database is None:
    #     database = "ttd"

    payload = {
        "cleaned_query": None,
        "status": None,
        "route": None,
        "message": None,
        "parsed_value": {
            "drug_name": drug_names,
            "target_name": None,
            "gene_name": gene_names,
            "disease_name": disease_names,
            "pathway_name": None,
            "biomarker_name": None,
            "drug_mechanism_of_action_on_target": None,
            "approval_status": None
        },
        "tool": None
    }

    url = f"{EXPAND_SYNONYMS_API}/expand_synonyms"
    params = {"database": database}

    response = requests.post(
        url,
        params=params,
        json=payload,
        timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def to_numpy(x):
    # if x is a PyTorch/TF tensor, convert
    try:
        return x.cpu().numpy()
    except AttributeError:
        return np.asarray(x)


def get_drug_info(drug_query, page_size: int = 50, max_pages: int = 100):
    url = "https://api.platform.opentargets.org/api/v4/graphql"

    # 1. Search for the Drug ID
    search_query = """
    query SearchDrug($q: String!, $index: Int!, $size: Int!) {
      search(queryString: $q, entityNames: ["drug"], page: {index: $index, size: $size}) {
        hits {
          id
          name
        }
      }
    }
    """

    # 2. Get specific Drug details
    drug_details_query = """
    query GetDrugDetails($drugId: String!) {
      drug(chemblId: $drugId) {
        id
        name
        synonyms
        tradeNames
      }
    }
    """

    try:
        # Step 1: Search for name -> ID (iterate all pages)
        all_hits = []
        for page_index in range(max_pages):
            search_resp = requests.post(
                url,
                json={
                    'query': search_query,
                    'variables': {'q': drug_query, 'index': page_index, 'size': page_size},
                },
                timeout=30,
            )
            search_resp.raise_for_status()
            search_res = search_resp.json()
            page_hits = search_res.get('data', {}).get('search', {}).get('hits', []) or []

            if not page_hits:
                break

            all_hits.extend(page_hits)
            if len(page_hits) < page_size:
                break

        if not all_hits:
            raise ValueError(f"Drug matching '{drug_query}' not found")

        # Deduplicate by id
        unique_hits = []
        seen_ids = set()
        for hit in all_hits:
            hit_id = hit.get('id')
            if hit_id and hit_id not in seen_ids:
                seen_ids.add(hit_id)
                unique_hits.append(hit)

        query_norm = drug_query.strip().lower()
        exact_hits = [
            h for h in unique_hits
            if (h.get('name') or '').strip().lower() == query_norm
        ]
        selected_hit = exact_hits[0] if exact_hits else unique_hits[0]
        drug_id = selected_hit['id']

        # Step 2: Fetch detailed properties
        details_resp = requests.post(
            url,
            json={'query': drug_details_query, 'variables': {'drugId': drug_id}},
            timeout=30,
        )
        details_resp.raise_for_status()
        details_res = details_resp.json()
        drug_info = details_res.get('data', {}).get('drug', {})

        if not drug_info:
            raise ValueError("Drug details could not be retrieved from the database.")

        # Data Cleaning
        canonical_name = (drug_info.get("name") or "").strip()
        trade_names = drug_info.get("tradeNames") or []
        synonyms = drug_info.get("synonyms") or []

        # Combined Parameter: canonical name + trade names + synonyms
        combined_terms = set(trade_names) | set(synonyms)
        if canonical_name:
            combined_terms.add(canonical_name)
        combined = sorted(combined_terms)

        print(f"Total entry found for {drug_query} : {len(combined)}")

        return {
            "name": drug_info.get("name"),
            "id": drug_info.get("id"),
            "trade_names": sorted(list(set(trade_names))),
            "synonyms": sorted(list(set(synonyms))),
            "combined": combined
        }

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to fetch drug info for '{drug_query}': {e}") from e


def safe_repair_list(s: str) -> list:
    """
    Safely extract string elements from LLM output, handling both single
    and double quotes, even if the output is not properly formatted as a list.

    Uses ast.literal_eval first for robust parsing, falls back to regex.
    Handles apostrophes inside values correctly (e.g. "Parkinson's disease").

    Parameters
    ----------
    s : str
        Raw LLM output containing string elements.

    Returns
    -------
    List[str]
        List of extracted string elements.
    """
    import re
    import ast

    if not s:
        return []

    # Try ast.literal_eval first (most robust for well-formed lists)
    stripped = s.strip()
    if stripped.startswith("["):
        try:
            result = ast.literal_eval(stripped)
            if isinstance(result, list):
                return [str(x) for x in result if x]
        except Exception:
            pass

    # FIX: Use a smarter regex that handles apostrophes inside double-quoted strings
    # and apostrophes inside single-quoted strings separately
    # First try double-quoted items (handles apostrophes inside values)
    double_quoted = re.findall(r'"([^"]*)"', s)
    if double_quoted:
        return double_quoted

    # Fallback: single-quoted items (apostrophes may cause splits — best-effort)
    single_quoted = re.findall(r"'([^']*)'", s)
    return single_quoted


def knee_threshold(sims, S=5, fallback_pct=95):
    sorted_scores = np.sort(sims)[::-1]

    knee = KneeLocator(
        range(len(sorted_scores)),
        sorted_scores,
        curve="convex",
        direction="decreasing",
        S=S,
    )

    if knee.knee is None:
        return np.percentile(sorted_scores, fallback_pct)

    return sorted_scores[knee.knee]


def filter_and_sort_hits(terms, sims, threshold):
    return sorted(
        [(terms[i], float(s)) for i, s in enumerate(sims) if s > threshold],
        key=lambda x: x[1],
        reverse=True
    )


async def return_openai_member(user_prompt, system_prompt, model="gpt-4o-mini"):

    from openai import AsyncOpenAI
    import time

    client = AsyncOpenAI()

    start = time.perf_counter()

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    elapsed = time.perf_counter() - start

    answer = response.choices[0].message.content.strip()

    return {
        "model": model,
        "answer": answer,
        "latency": elapsed,
    }


async def return_grok_member(
    user_prompt: str,
    system_prompt: str,
    model: str = "grok-4-1-fast-non-reasoning-latest",
):
    """
    Async wrapper for Grok (xAI) via OpenAI-compatible SDK.
    The SDK is synchronous — run in a worker thread.

    FIX: Use chat.completions.create (OpenAI-compatible) instead of
    responses.create which does not exist on the xAI client.
    """
    import os
    import time
    import asyncio
    import httpx
    from openai import OpenAI

    grok_client = OpenAI(
        api_key=os.environ.get("GROK_KEY"),
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(3600.0),
    )

    start = time.perf_counter()

    # FIX: Changed from grok_client.responses.create → grok_client.chat.completions.create
    response = await asyncio.to_thread(
        grok_client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    elapsed = time.perf_counter() - start

    # FIX: Use standard chat completions response format
    answer = response.choices[0].message.content.strip()

    return {
        "model": model,
        "answer": answer,
        "latency": elapsed,
    }


async def return_llama_member(
    user_prompt: str,
    system_prompt: str,
    model: str = "llama-3.3-70b-versatile",
):
    """
    Async wrapper for Groq LLaMA models.
    Groq SDK is synchronous — run in thread.
    """
    import os
    import time
    import asyncio
    from groq import Groq

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    start = time.perf_counter()

    reply = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )

    elapsed = time.perf_counter() - start

    answer = reply.choices[0].message.content.strip()

    return {
        "model": model,
        "answer": answer,
        "latency": elapsed,
    }


async def return_gemini_member(
    user_prompt: str,
    system_prompt: str,
    model: str = "gemini-2.5-flash-lite",
):
    """
    Async wrapper for Gemini using google.genai (new SDK).
    Runs the synchronous client call in a thread to avoid blocking.
    """

    from google import genai
    import asyncio
    import time

    client = genai.Client()

    start = time.perf_counter()

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=user_prompt,
        config={
            "system_instruction": system_prompt,
        },
    )

    elapsed = time.perf_counter() - start

    return {
        "model": model,
        "answer": response.text,
        "latency": elapsed,
    }


def get_synonyms_by_symbol(gene_symbol, page_size: int = 50, max_pages: int = 100):
    url = "https://api.platform.opentargets.org/api/v4/graphql"

    query = """
    query searchGene($queryString: String!, $index: Int!, $size: Int!) {
      search(queryString: $queryString, entityNames: ["target"], page: {index: $index, size: $size}) {
        hits {
          id
          entity
          object {
            ... on Target {
              approvedSymbol
              approvedName
              symbolSynonyms { label }
              nameSynonyms { label }
            }
          }
        }
      }
    }
    """

    try:
        all_hits = []
        for page_index in range(max_pages):
            response = requests.post(
                url,
                json={
                    'query': query,
                    'variables': {
                        "queryString": gene_symbol,
                        "index": page_index,
                        "size": page_size,
                    },
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            page_hits = data.get("data", {}).get("search", {}).get("hits", []) or []

            if not page_hits:
                break

            all_hits.extend(page_hits)
            if len(page_hits) < page_size:
                break

        if not all_hits:
            raise ValueError(f"No target found for search term: {gene_symbol}")

        query_norm = gene_symbol.strip().lower()
        exact_hits = [
            h for h in all_hits
            if (h.get("object", {}).get("approvedSymbol") or "").strip().lower() == query_norm
        ]
        best_hit = exact_hits[0] if exact_hits else all_hits[0]

        target = best_hit.get('object') or {}

        symbol_syns = [s['label'] for s in target.get('symbolSynonyms', [])]
        name_syns = [n['label'] for n in target.get('nameSynonyms', [])]

        # FIX: Use list(dict.fromkeys(...)) for proper order-preserving dedup
        symbol_syns = list(dict.fromkeys(symbol_syns))
        name_syns = list(dict.fromkeys(name_syns))

        combined = sorted(list(set(symbol_syns + name_syns)))
        print(f"The number of entry for {gene_symbol} is {len(combined)}")

        return {
            "ensembl_id": best_hit.get('id'),
            "approved_symbol": target.get("approvedSymbol"),
            "approved_name": target.get("approvedName"),
            "combined": combined
        }

    except Exception as e:
        raise ValueError(f"Failed to fetch synonyms for '{gene_symbol}': {e}") from e