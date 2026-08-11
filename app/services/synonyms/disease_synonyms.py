import os
import sys
import re
import asyncio
import logging
from typing import List, Dict, Optional, Set
from urllib.parse import quote

import httpx

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Configuration
OVERALL_TIMEOUT_SEC = float(os.getenv("DISEASE_FETCH_TIMEOUT_SEC", "60"))
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "15"))
# Per-source cap: EBI OLS makes 2-3 HTTP calls so give it more than one HTTP_TIMEOUT_SEC
DISEASE_SOURCE_TIMEOUT_SEC = float(os.getenv("DISEASE_SOURCE_TIMEOUT_SEC", "20"))
def _norm(text: str) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _acronym(tokens: List[str]) -> str:
    letters = [t[0] for t in tokens if t and t[0].isalpha()]
    return "".join(letters)

def _filter_terms_by_query(terms: Set[str], disease_name: str) -> List[str]:
    if not terms:
        return []
    query_norm = _norm(disease_name)
    query_tokens = query_norm.split()
    must_tokens = [t for t in query_tokens if t]
    acronym = _acronym(query_tokens).lower()
    if not must_tokens and not acronym:
        return sorted(t.strip() for t in terms if isinstance(t, str) and t.strip())

    # If the query itself (single token, ≥3 chars) appears in the returned terms,
    # the source has confirmed the entity identity (e.g. "HCC" listed as synonym
    # of hepatocellular carcinoma, "T2DM" in type-2-diabetes cluster). Trust all
    # its synonyms. Require single-token so multi-word names like "rheumatoid
    # arthritis" don't trigger trust-all on overly broad NLM disease clusters.
    # Require ≥3 chars to avoid 2-letter ambiguous acronyms like "RA".
    if (len(query_tokens) == 1 and len(query_norm) >= 3
            and any(_norm(t) == query_norm for t in terms if isinstance(t, str))):
        return sorted(t.strip() for t in terms if isinstance(t, str) and t.strip())

    # A single short alphanumeric token (e.g. "cml", "t2dm", "hcc") is likely an
    # acronym: also accept terms whose own acronym spells out the query.
    is_acronym_query = (
        len(query_tokens) == 1
        and len(query_norm) <= 6
        and query_norm.isalnum()
    )

    filtered: List[str] = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        tnorm = _norm(term)
        if not tnorm:
            continue
        # Exact match (e.g. "cml" == "cml")
        if tnorm == query_norm or (acronym and tnorm == acronym):
            filtered.append(term.strip())
            continue
        t_tokens_list = tnorm.split()
        t_tokens = set(t_tokens_list)
        # All query tokens must appear in term tokens
        if all(t in t_tokens for t in must_tokens):
            filtered.append(term.strip())
            continue
        if is_acronym_query:
            n = len(query_norm)
            full_acr = _acronym(t_tokens_list).lower()
            # Prefix-acronym only for ≥3-char queries: 2-char like "RA" is too
            # ambiguous — first-two-word initials match many unrelated terms.
            prefix_acr = _acronym(t_tokens_list[:n]).lower() if n >= 3 else ""
            if full_acr == query_norm or prefix_acr == query_norm:
                filtered.append(term.strip())
                continue
            # T2DM-style: alpha letters form acronym, digits must appear in term
            # e.g. "t2dm" → alpha "tdm" + digit "2"; "type 2 diabetes mellitus" → acr "tdm" ✓ and "2" in term ✓
            if not query_norm.isalpha():
                alpha_q = re.sub(r'\d', '', query_norm)
                digit_q = re.sub(r'\D', '', query_norm)
                if alpha_q:
                    fa = _acronym(t_tokens_list).lower()
                    pa = _acronym(t_tokens_list[:len(alpha_q)]).lower()
                    if (fa == alpha_q or pa == alpha_q) and all(d in tnorm for d in digit_q):
                        filtered.append(term.strip())
    return sorted(set(filtered))

# -------------------- NLMDiseaseFetcher --------------------
class NLMDiseaseFetcher:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, disease_name: str) -> List[str]:
        if not disease_name or not disease_name.strip():
            return []
        query_norm = _norm(disease_name)
        url = "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search"
        params = {
            "terms": disease_name.strip(),
            "sf": "primary_name,synonyms",
            "ef": "synonyms,word_synonyms,primary_name",
            "maxList": 10000
        }
        synonyms: Set[str] = set()
        try:
            client = await self._get_client()
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or len(data) <= 3:
                return []
            entries = data[3]
            if not isinstance(entries, list):
                return []
            for entry in entries:
                if not isinstance(entry, list):
                    continue
                entry_terms: List[str] = []
                if len(entry) > 0 and isinstance(entry[0], str):
                    entry_terms.append(entry[0].strip())
                if len(entry) > 1 and entry[1]:
                    if isinstance(entry[1], list):
                        entry_terms.extend(s.strip() for s in entry[1] if isinstance(s, str) and s.strip())
                    elif isinstance(entry[1], str):
                        entry_terms.append(entry[1].strip())
                if len(entry) > 2 and entry[2]:
                    if isinstance(entry[2], list):
                        entry_terms.extend(s.strip() for s in entry[2] if isinstance(s, str) and s.strip())
                    elif isinstance(entry[2], str):
                        entry_terms.append(entry[2].strip())
                if not any(_norm(t) == query_norm for t in entry_terms):
                    continue
                synonyms.update(entry_terms)
            synonyms.add(disease_name.strip())
            result = sorted({s.strip() for s in synonyms if isinstance(s, str) and s.strip()})
            logger.info(f"[NLM] Fetched {len(result)} synonyms for '{disease_name}'")
            return result
        except Exception as e:
            logger.exception(f"[NLM] Failed for '{disease_name}': {e}")
            return []

# -------------------- Flexible EBI OLS (DOID + MONDO + NCIT + ORDO) --------------------
class OLSDiseaseFetcher:
    SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"

    def __init__(self, ontology: str = "doid"):
        self.ontology = ontology
        self.TERM_URL_TMPL = f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology}/terms/{{encoded_iri}}"

    async def _get_client(self) -> httpx.AsyncClient:
        if not hasattr(self, "_client") or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client

    async def close(self):
        if hasattr(self, "_client") and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, disease_name: str) -> List[str]:
        if not disease_name or not disease_name.strip():
            return []
        query_norm = _norm(disease_name)
        query_tokens = query_norm.split()
        # Short alphanumeric single-token queries (e.g. "cml", "t2dm") may be acronyms.
        is_acronym_query = (
            len(query_tokens) == 1
            and len(query_norm) <= 6
            and query_norm.isalnum()
        )
        try:
            client = await self._get_client()
            resp = await client.get(
                self.SEARCH_URL,
                params={"q": disease_name.strip(), "ontology": self.ontology, "rows": 10}
            )
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            if not docs:
                logger.info(f"[EBI OLS {self.ontology.upper()}] No results")
                return []
            disease_doc = None
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                doc_terms = []
                label = doc.get("label")
                if isinstance(label, str):
                    doc_terms.append(label.strip())
                syns = doc.get("synonym") or doc.get("synonyms") or []
                if isinstance(syns, list):
                    doc_terms.extend(s.strip() for s in syns if isinstance(s, str) and s.strip())
                if any(_norm(t) == query_norm for t in doc_terms):
                    disease_doc = doc
                    break
            # Acronym fallback: EBI OLS search results often omit synonyms at
            # search level; if the query looks like an acronym, also accept a doc
            # whose label's initials spell the query — full ("cml" → "chronic
            # myeloid leukemia" c-m-l) or prefix ("hiv" → first 3 words of
            # "human immunodeficiency virus infectious disease" give h-i-v).
            if disease_doc is None and is_acronym_query:
                n = len(query_norm)
                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    label = doc.get("label")
                    if not isinstance(label, str):
                        continue
                    label_tokens = _norm(label).split()
                    full_acr = _acronym(label_tokens).lower()
                    prefix_acr = _acronym(label_tokens[:n]).lower() if n >= 3 else ""
                    if full_acr == query_norm or prefix_acr == query_norm:
                        disease_doc = doc
                        break
            # Synonym-endpoint fallback: for short queries (e.g. "HCC", "GBM",
            # "T2DM") whose acronym is derived from compound word parts or digits,
            # label-initials matching fails. Fetch each candidate's full term data
            # and check if the query appears in its registered synonyms.
            if disease_doc is None and len(query_norm) <= 6 and query_norm.isalnum() and docs:
                for candidate_doc in docs:
                    if not isinstance(candidate_doc, dict) or not candidate_doc.get("iri"):
                        continue
                    try:
                        chk_iri = candidate_doc["iri"]
                        chk_enc = quote(quote(chk_iri, safe=""), safe="")
                        chk_url = self.TERM_URL_TMPL.format(encoded_iri=chk_enc)
                        chk_resp = await client.get(chk_url)
                        chk_resp.raise_for_status()
                        chk_data = chk_resp.json()
                        if any(_norm(s) == query_norm for s in chk_data.get("synonyms", []) if isinstance(s, str)):
                            disease_doc = candidate_doc
                            logger.debug(f"[EBI OLS {self.ontology.upper()}] Synonym-endpoint fallback matched '{disease_name}' via {candidate_doc.get('label')}")
                            break
                    except Exception:
                        continue
            if not disease_doc or not disease_doc.get("iri"):
                return []
            iri = disease_doc["iri"]
            label = disease_doc.get("label")
            encoded_iri = quote(quote(iri, safe=""), safe="")
            term_url = self.TERM_URL_TMPL.format(encoded_iri=encoded_iri)
            term_resp = await client.get(term_url)
            term_resp.raise_for_status()
            term_data = term_resp.json()
            terms: Set[str] = set()
            if label and isinstance(label, str):
                terms.add(label.strip())
            for syn in term_data.get("synonyms", []):
                if isinstance(syn, str) and syn.strip():
                    terms.add(syn.strip())
            # Descendants (full page)
            links = term_data.get("_links", {})
            children_link = links.get("children", {}).get("href")
            if children_link:
                child_url = children_link + ("?" if "?" not in children_link else "&") + "size=500"
                child_resp = await client.get(child_url)
                child_resp.raise_for_status()
                child_data = child_resp.json()
                for child in child_data.get("_embedded", {}).get("terms", []):
                    if isinstance(child, dict):
                        clabel = child.get("label")
                        if clabel and isinstance(clabel, str):
                            terms.add(clabel.strip())
            result = sorted({t.strip() for t in terms if isinstance(t, str) and t.strip()})
            logger.info(f"[EBI OLS {self.ontology.upper()}] Found {len(result)} terms for '{disease_name}'")
            return result
        except Exception as e:
            logger.exception(f"[EBI OLS {self.ontology.upper()}] Failed for '{disease_name}': {e}")
            return []

# -------------------- OpenTargets --------------------
class OpenTargetsDiseaseFetcher:
    API_URL = "https://api.platform.opentargets.org/api/v4/graphql"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def run_graphql(self, client: httpx.AsyncClient, query: str, variables: Dict) -> Dict:
        # Retry transient gateway failures (502/503/504) + timeouts/transport errors:
        # OpenTargets' gateway 502s intermittently and recovers within ~1s, so a single
        # shot silently drops OT synonyms for that run (the source of count flakiness).
        # Retry transient failures: 400 (OT rate-limit burst), 502/503/504, timeouts.
        # 3 attempts, 0.5s→1s backoff.
        for attempt in range(3):
            try:
                resp = await client.post(self.API_URL, json={"query": query, "variables": variables})
                resp.raise_for_status()
                return resp.json().get("data", {})
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                logger.error(f"[OpenTargets] GraphQL error: {e}")
                return {}
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                logger.error(f"[OpenTargets] GraphQL error: {e}")
                return {}
            except Exception as e:
                logger.error(f"[OpenTargets] GraphQL error: {e}")
                return {}
        return {}

    async def fetch(self, disease_name: str) -> List[str]:
        if not disease_name or not disease_name.strip():
            return []
        query_norm = _norm(disease_name)
        queries = {
            "search": """
                query Search($q: String!) {
                    search(queryString: $q, entityNames: ["disease"], page: {index: 0, size: 1}) {
                        hits { id name }
                    }
                }""",
            "descendants": """
                query GetDesc($id: String!) {
                    disease(efoId: $id) { descendants }
                }""",
            "disease_synonyms": """
                query GetDiseaseSynonyms($efoId: String!) {
                    disease(efoId: $efoId) {
                        name
                        synonyms { terms }
                    }
                }""",
            "metadata": """
                query GetMeta($ids: [String!]!) {
                    diseases(efoIds: $ids) {
                        name
                        synonyms { terms }
                    }
                }"""
        }

        try:
            client = await self._get_client()

            # STEP 1: Search disease
            search_data = await self.run_graphql(
                client, queries["search"], {"q": disease_name.strip()}
            )
            hits = search_data.get("search", {}).get("hits", [])
            if not hits:
                logger.info(f"[OpenTargets] No results for '{disease_name}'")
                return []

            # Exact case-insensitive match preferred; fall back to top hit
            valid_hits = [h for h in hits if isinstance(h, dict) and h.get("id")]
            exact_hits = [h for h in valid_hits if (h.get("name") or "").strip().lower() == query_norm]
            top_hit    = exact_hits[0] if exact_hits else (valid_hits[0] if valid_hits else None)
            if not top_hit:
                logger.info(f"[OpenTargets] No usable hit for '{disease_name}'")
                return []
            root_id = top_hit["id"]

            # STEP 2: Root synonyms
            disease_data = await self.run_graphql(
                client, queries["disease_synonyms"], {"efoId": root_id}
            )
            disease_info = disease_data.get("disease", {})
            if not isinstance(disease_info, dict):
                disease_info = {}

            terms: Set[str] = set()
            name = disease_info.get("name")
            if name and isinstance(name, str):
                terms.add(name.strip())
            synonyms = disease_info.get("synonyms", [])
            if isinstance(synonyms, list):
                for group in synonyms:
                    if isinstance(group, dict):
                        group_terms = group.get("terms", [])
                        if isinstance(group_terms, list):
                            terms.update(
                                t.strip() for t in group_terms
                                if isinstance(t, str) and t.strip()
                            )

            # STEP 3: Descendants
            desc_data = await self.run_graphql(
                client, queries["descendants"], {"id": root_id}
            )
            disease_desc = desc_data.get("disease", {})
            desc_ids = disease_desc.get("descendants", []) if isinstance(disease_desc, dict) else []

            if not desc_ids or not isinstance(desc_ids, list):
                result = sorted({t.strip() for t in terms if isinstance(t, str) and t.strip()})
                logger.info(f"[OpenTargets] Found {len(result)} terms for '{disease_name}'")
                return result

            # STEP 4: Batch descendant metadata
            chunk_size = 500
            for i in range(0, len(desc_ids), chunk_size):
                chunk = desc_ids[i:i + chunk_size]
                meta_data = await self.run_graphql(
                    client, queries["metadata"], {"ids": chunk}
                )
                diseases = meta_data.get("diseases", [])
                if not isinstance(diseases, list):
                    continue
                for d in diseases:
                    if not isinstance(d, dict):
                        continue
                    d_name = d.get("name")
                    if d_name and isinstance(d_name, str):
                        terms.add(d_name.strip())
                    d_syns = d.get("synonyms", [])
                    if isinstance(d_syns, list):
                        for syn in d_syns:
                            if isinstance(syn, dict):
                                syn_terms = syn.get("terms", [])
                                if isinstance(syn_terms, list):
                                    terms.update(
                                        t.strip() for t in syn_terms
                                        if isinstance(t, str) and t.strip()
                                    )

            result = sorted({t.strip() for t in terms if isinstance(t, str) and t.strip()})
            logger.info(f"[OpenTargets] Found {len(result)} terms for '{disease_name}'")
            return result

        except Exception as e:
            logger.exception(f"[OpenTargets] Failed for '{disease_name}': {e}")
            return []

# -------------------- Aggregator with 7 sources --------------------
class DiseaseSynonymAggregator:
    def __init__(self):
        self.sources = {
            "NLM": NLMDiseaseFetcher(),
            "EBI_DOID": OLSDiseaseFetcher(ontology="doid"),
            "MONDO": OLSDiseaseFetcher(ontology="mondo"),
            # "NCIT": OLSDiseaseFetcher(ontology="ncit"),
            # "ORDO": OLSDiseaseFetcher(ontology="ordo"),
            "OpenTargets": OpenTargetsDiseaseFetcher(),
        }

    async def close(self):
        for source in self.sources.values():
            if hasattr(source, 'close'):
                await source.close()

    async def get_all_synonyms(self, disease_name: str) -> Dict[str, object]:
        if not disease_name or not disease_name.strip():
            return {"combined_synonyms": [], "synonyms_by_source": {}, "official_name": disease_name}
        logger.info(f"[Aggregator] Fetching for '{disease_name}'")
        # Wrap each source individually so a single slow source does not block others.
        tasks = {
            name: asyncio.wait_for(source.fetch(disease_name), timeout=DISEASE_SOURCE_TIMEOUT_SEC)
            for name, source in self.sources.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        synonyms_by_source = {}
        for (name, _), result in zip(tasks.items(), results):
            if isinstance(result, asyncio.TimeoutError):
                logger.warning(f"[{name}] Source timeout after {DISEASE_SOURCE_TIMEOUT_SEC:.0f}s for '{disease_name}'")
                synonyms_by_source[name] = []
            elif isinstance(result, Exception):
                logger.error(f"[{name}] Error: {result}")
                synonyms_by_source[name] = []
            elif isinstance(result, list):
                synonyms_by_source[name] = result
            else:
                synonyms_by_source[name] = []
        # Smart filtering
        for src in list(synonyms_by_source.keys()):
            if synonyms_by_source[src]:
                synonyms_by_source[src] = _filter_terms_by_query(set(synonyms_by_source[src]), disease_name)
        combined = sorted({s for lst in synonyms_by_source.values() for s in lst})
        logger.info(f"[Aggregator] Final: {len(combined)} unique synonyms for '{disease_name}'")
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": synonyms_by_source,
            "official_name": disease_name,
        }

