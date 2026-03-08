from typing import Dict, List, Optional, Union, Sequence, Set, Any
import os
import logging
import numpy as np
from kneed import KneeLocator

from .http_client import post_json_with_retries

from dotenv import load_dotenv

# --------------------------------------------------
# Optional dotenv loading (off by default in production)
# --------------------------------------------------
if os.getenv("LOAD_DOTENV", "").lower() in ("1", "true", "yes"):
    load_dotenv(os.getenv("DOTENV_PATH", ".env"))

# --------------------------------------------------
# Logging
# --------------------------------------------------
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.utility_evaluation")

# --------------------------------------------------
# Config
# --------------------------------------------------
OT_GRAPHQL_URL = os.getenv(
    "OT_GRAPHQL_URL", "https://api.platform.opentargets.org/api/v4/graphql"
)
LLM_FILTER_URL = os.getenv(
    "LLM_FILTER_URL", "https://biochirp.iiitd.edu.in/services/llm_filter/api"
)
EXPAND_SYNONYMS_URL = os.getenv(
    "EXPAND_SYNONYMS_URL", "https://biochirp.iiitd.edu.in/services/expand_synonyms/api"
)

DEFAULT_TIMEOUT = float(os.getenv("UTILITY_EVAL_TIMEOUT", "45"))
MAX_RETRIES = int(os.getenv("UTILITY_EVAL_RETRIES", "3"))
BACKOFF = float(os.getenv("UTILITY_EVAL_BACKOFF", "0.5"))
POOL_CONNECTIONS = int(os.getenv("UTILITY_EVAL_POOL_CONNECTIONS", "20"))
POOL_MAXSIZE = int(os.getenv("UTILITY_EVAL_POOL_MAXSIZE", "50"))


async def _post_json(
    url: str,
    payload: dict,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    params: Optional[dict] = None,
) -> dict:
    try:
        resp = await post_json_with_retries(
            url,
            payload,
            params=params,
            timeout=timeout,
            max_retries=MAX_RETRIES,
            backoff_base_s=BACKOFF,
        )
    except Exception as e:
        logger.error("HTTP error url=%s err=%s", url, e)
        raise
    try:
        return resp.json()
    except ValueError as e:
        logger.error("Non-JSON response from %s", url)
        raise RuntimeError(f"Non-JSON response from {url}") from e


async def _post_graphql(
    query: str, variables: dict, *, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    data = await _post_json(
        OT_GRAPHQL_URL, {"query": query, "variables": variables}, timeout=timeout
    )
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL response is not a JSON object")
    if data.get("errors"):
        raise RuntimeError(f"GraphQL error: {data.get('errors')}")
    return data.get("data", {})

async def get_disease_and_descendant_synonyms(disease_name: str):
    if not disease_name or not str(disease_name).strip():
        raise ValueError("disease_name is empty")

    queries = {
        "search": """
        query Search($q: String!) {
          search(
            queryString: $q,
            entityNames: ["disease"],
            page: { index: 0, size: 1 }
          ) {
            hits { id name }
          }
        }
        """,

        "descendants": """
        query GetDesc($id: String!) {
          disease(efoId: $id) {
            descendants
          }
        }
        """,

        "disease_synonyms": """
        query GetDiseaseSynonyms($efoId: String!) {
          disease(efoId: $efoId) {
            name
            synonyms { terms }
          }
        }
        """,

        "metadata": """
        query GetMeta($ids: [String!]!) {
          diseases(efoIds: $ids) {
            name
            synonyms { terms }
          }
        }
        """
    }

    try:
        # --------------------------------------------------
        # STEP 1: Resolve disease name ? EFO ID
        # --------------------------------------------------
        search_res = await _post_graphql(queries["search"], {"q": disease_name})
        hits = search_res.get("search", {}).get("hits", [])
        if not hits:
            raise ValueError(f"No disease found for {disease_name}")

        root_id = hits[0]["id"]

        # --------------------------------------------------
        # STEP 2: Root disease name + synonyms
        # --------------------------------------------------
        disease_res = await _post_graphql(queries["disease_synonyms"], {"efoId": root_id})
        disease_info = disease_res.get("disease") or {}

        root_synonyms = set()

        if disease_info.get("name"):
            root_synonyms.add(disease_info["name"])

        for group in disease_info.get("synonyms", []) or []:
            root_synonyms.update(group.get("terms", []))

        # --------------------------------------------------
        # STEP 3: Fetch descendant EFO IDs
        # --------------------------------------------------
        desc_res = await _post_graphql(queries["descendants"], {"id": root_id})
        desc_ids = (desc_res.get("disease", {}).get("descendants", [])) or []

        # If no descendants, combined = root only
        if not desc_ids:
            combined = sorted(root_synonyms)
            return {
                "synonyms": sorted(root_synonyms),
                "descendants": [],
                "combined": combined,
            }

        # --------------------------------------------------
        # STEP 4: Resolve descendant names + synonyms (batched)
        # --------------------------------------------------
        desc_synonyms = set()
        chunk_size = 500

        for i in range(0, len(desc_ids), chunk_size):
            chunk = desc_ids[i : i + chunk_size]

            meta_res = await _post_graphql(queries["metadata"], {"ids": chunk})
            diseases = meta_res.get("diseases", []) or []

            for d in diseases:
                if not d:
                    continue

                if d.get("name"):
                    desc_synonyms.add(d["name"])

                for group in d.get("synonyms", []) or []:
                    desc_synonyms.update(group.get("terms", []))

        # --------------------------------------------------
        # STEP 5: Explicit UNION (root ? descendants)
        # --------------------------------------------------
        combined = sorted(root_synonyms | desc_synonyms)

        logger.info(
            "Disease '%s' combined synonyms: %d", disease_name, len(combined)
        )

        return {
            "synonyms": sorted(root_synonyms),
            "descendants": sorted(desc_synonyms),
            "combined": combined,
            "descendant_ids": desc_ids,
        }

    except Exception as e:
        logger.exception("OpenTargets failed for disease=%r", disease_name)
        raise RuntimeError(f"OpenTargets failed for {disease_name!r}") from e



# def safe_div(n, d):
#     return n / d if d != 0 else 0.0




def compute_metrics(y_true, y_pred, universe):
    y_true = set(y_true or [])
    y_pred = set(y_pred or [])
    universe = set(universe or [])

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

    # Cohen?s kappa
    total = tp + fp + fn + tn
    if total:
        po = accuracy
        pe = (((tp + fp) * (tp + fn)) + ((fn + tn) * (fp + tn))) / (total ** 2)
        kappa = (po - pe) / (1 - pe) if (1 - pe) else 0.0
    else:
        kappa = 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "specificity": specificity,
        "kappa": kappa,
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


async def llm_member_filter(
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

    payload = {
        "category": category,
        "single_term": single_term,
        "string_list": string_list,
    }
    if not string_list:
        return []

    url = f"{LLM_FILTER_URL}/llm_member_selection_filter"
    try:
        data = await _post_json(url, payload, timeout=timeout)
        value = data.get("value", [])
        if not isinstance(value, list):
            logger.warning("LLM filter returned non-list value")
            return []
        return value
    except Exception as e:
        logger.exception("LLM filter failed")
        return []



async def expand_synonyms(
    *,
    disease_names: List[str] = None,
    drug_names: List[str] = None,
    gene_names: List[str] = None,
    database: str = "ttd",
    timeout: int = 45
) -> Dict:
    """
    Call BioChirp Expand Synonyms service.

    Parameters
    ----------
    disease_names : List[str]
        Disease terms to expand (e.g. ["tuberculosis"])
    database : str
        One of {"ttd", "ctd", "hcdt"}
    timeout : int
        Request timeout in seconds

    Returns
    -------
    dict
        Raw JSON response from expand_synonyms service
    """

    disease_names = disease_names or []
    drug_names = drug_names or []
    gene_names = gene_names or []
    if database not in {"ttd", "ctd", "hcdt"}:
        raise ValueError(f"Invalid database: {database}")
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

    url = f"{EXPAND_SYNONYMS_URL}/expand_synonyms"
    params = {"database": database}

    return await _post_json(url, payload, params=params, timeout=timeout)



def to_numpy(x):
    # if x is a PyTorch/TF tensor, convert
    try:
        # PyTorch or TF
        return x.cpu().numpy()
    except AttributeError:
        # if x is already numpy
        return np.asarray(x)




async def get_drug_info(drug_query):
    if not drug_query or not str(drug_query).strip():
        return {"error": "drug_query is empty"}
    # 1. Search for the Drug ID
    search_query = """
    query SearchDrug($q: String!) {
      search(queryString: $q, entityNames: ["drug"], page: {index: 0, size: 1}) {
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
        # Step 1: Search for name -> ID
        search_res = await _post_graphql(search_query, {"q": drug_query})
        hits = search_res.get("search", {}).get("hits", [])
        if not hits:
            return {"error": f"Drug matching '{drug_query}' not found"}

        drug_id = hits[0]['id']
        
        # Step 2: Fetch detailed properties
        details_res = await _post_graphql(drug_details_query, {"drugId": drug_id})
        drug_info = details_res.get("drug", {})

        if not drug_info:
            return {"error": "Drug details could not be retrieved from the database."}

        # Data Cleaning
        trade_names = drug_info.get("tradeNames") or []
        synonyms = drug_info.get("synonyms") or []
        
        # Combined Parameter: Merges both lists and removes duplicates
        combined = sorted(list(set(trade_names + synonyms)))


        logger.info("Drug '%s' combined synonyms: %d", drug_query, len(combined))

        return {
            "name": drug_info.get("name"),
            "id": drug_info.get("id"),
            "trade_names": sorted(list(set(trade_names))),
            "synonyms": sorted(list(set(synonyms))),
            "combined": combined
        }

    except Exception as e:
        logger.exception("Failed to fetch drug info for %r", drug_query)
        return {"error": str(e)}




def safe_repair_list(s: str) -> list:
    """
    Safely extract string elements from LLM output, handling both single
    and double quotes, even if the output is not properly formatted as a list.

    Many LLM outputs may return member lists as plain text, or in a 
    poorly formatted pseudo-list. This function extracts all quoted items 
    to produce a clean Python list of strings.

    Parameters
    ----------
    s : str
        Raw LLM output containing string elements, possibly using 
        single or double quotes.

    Returns
    -------
    List[str]
        List of extracted string elements.

    Example
    -------
    >>> safe_repair_list('"apple", "banana", "cherry"')
    ['apple', 'banana', 'cherry']
    >>> safe_repair_list("'apple', 'banana', 'cherry'")
    ['apple', 'banana', 'cherry']
    >>> safe_repair_list('"apple", \'banana\', "cherry"')
    ['apple', 'banana', 'cherry']
    """
    import re
    # Match anything inside single or double quotes
    matches = re.findall(r'["\']([^"\']*)["\']', s)
    return matches


def knee_threshold(sims, S=5.0, fallback_pct=95):
    arr = np.asarray(sims, dtype=float)
    if arr.size == 0:
        return 0.0
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0
    sorted_scores = np.sort(arr)[::-1]

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
    if terms is None or sims is None:
        return []
    n = min(len(terms), len(sims))
    if n == 0:
        return []
    if threshold is None:
        threshold = 0.0
    return sorted(
        [(terms[i], float(sims[i])) for i in range(n) if sims[i] > threshold],
        key=lambda x: x[1],
        reverse=True,
    )




async def return_openai_member(user_prompt, system_prompt, model="gpt-4o-mini"):

    from openai import OpenAI, AsyncOpenAI
    import time

    client = AsyncOpenAI()

    start = time.perf_counter()

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        answer = response.choices[0].message.content.strip().lower()
        elapsed = time.perf_counter() - start
        return {"model": model, "answer": answer, "latency": elapsed}
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.exception("OpenAI member call failed")
        return {"model": model, "answer": None, "latency": elapsed, "error": str(e)}


async def return_grok_member(
    user_prompt: str,
    system_prompt: str,
    model: str = "grok-4-1-fast-non-reasoning-latest",
):
    """
    Async wrapper for Grok (xAI) via OpenAI-compatible SDK.
    The SDK is synchronous ? run in a worker thread.
    """
    import os
    import time
    import asyncio
    import httpx
    from openai import OpenAI

    grok_key = os.environ.get("GROK_KEY")
    if not grok_key:
        return {"model": model, "answer": None, "latency": 0.0, "error": "GROK_KEY not set"}

    grok_client = OpenAI(
        api_key=grok_key,
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(3600.0),
    )

    start = time.perf_counter()
    try:
        response = await asyncio.to_thread(
            grok_client.responses.create,
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            store=False,
        )
        answer = response.output_text.strip()
        elapsed = time.perf_counter() - start
        return {"model": model, "answer": answer, "latency": elapsed}
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.exception("Grok member call failed")
        return {"model": model, "answer": None, "latency": elapsed, "error": str(e)}




async def return_llama_member(
    user_prompt: str,
    system_prompt: str,
    model: str = "llama-3.3-70b-versatile",
):
    """
    Async wrapper for Groq LLaMA models.
    Groq SDK is synchronous ? run in thread.
    """
    import os
    import time
    import asyncio
    from groq import Groq

    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        return {"model": model, "answer": None, "latency": 0.0, "error": "GROQ_API_KEY not set"}

    groq_client = Groq(api_key=groq_key)

    start = time.perf_counter()
    try:
        reply = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        answer = reply.choices[0].message.content.strip()
        elapsed = time.perf_counter() - start
        return {"model": model, "answer": answer, "latency": elapsed}
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.exception("Groq member call failed")
        return {"model": model, "answer": None, "latency": elapsed, "error": str(e)}





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

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=user_prompt,
            config={"system_instruction": system_prompt},
        )
        elapsed = time.perf_counter() - start
        return {"model": model, "answer": response.text, "latency": elapsed}
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.exception("Gemini member call failed")
        return {"model": model, "answer": None, "latency": elapsed, "error": str(e)}



async def get_synonyms_by_symbol(gene_symbol):
    if not gene_symbol or not str(gene_symbol).strip():
        return "gene_symbol is empty"
    # GraphQL query that searches for a 'target' entity by name
    query = """
    query searchGene($queryString: String!) {
      search(queryString: $queryString, entityNames: ["target"]) {
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
    
    variables = {"queryString": gene_symbol}
    
    try:
        data = await _post_graphql(query, variables)
        
        hits = data.get("search", {}).get("hits", [])
        
        if not hits:
            return f"No target found for search term: {gene_symbol}"
        
        # Take the top hit (the most relevant match)
        target = hits[0]['object']
        
        symbol_syns = [s['label'] for s in target.get('symbolSynonyms', [])]
        name_syns = [n['label'] for n in target.get('nameSynonyms', [])]


        symbol_syns = list({x.lower(): x for x in symbol_syns}.values())
        name_syns = list({x.lower(): x for x in name_syns}.values())

        logger.info(
            "Target '%s' combined synonyms: %d",
            gene_symbol,
            len(sorted(list(set(symbol_syns + name_syns)))),
        )
        
        return {
            "ensembl_id": hits[0]['id'],
            "approved_symbol": target.get("approvedSymbol"),
            "approved_name": target.get("approvedName"),
            "combined": sorted(list(set(symbol_syns + name_syns)))
        }

    except Exception as e:
        logger.exception("Failed to fetch target synonyms for %r", gene_symbol)
        return f"Error: {str(e)}"
