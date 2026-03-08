import os
import sys
import re
import asyncio
import logging
from typing import List, Dict, Optional, Set
from urllib.parse import quote

import httpx
from owlready2 import get_ontology

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
DOID_ONTOLOGY_URL = os.getenv("DOID_ONTOLOGY_URL", "http://purl.obolibrary.org/obo/doid.owl")

_ontology_cache = None

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
    query_tokens = _norm(disease_name).split()
    must_tokens = [t for t in query_tokens if t]
    acronym = _acronym(query_tokens).lower()
    if not must_tokens and not acronym:
        return sorted(t.strip() for t in terms if isinstance(t, str) and t.strip())
    filtered: List[str] = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        tnorm = _norm(term)
        if not tnorm:
            continue
        if tnorm == query_norm or (acronym and tnorm == acronym):
            filtered.append(term.strip())
            continue
        t_tokens = set(tnorm.split())
        if all(t in t_tokens for t in must_tokens):
            filtered.append(term.strip())
    return sorted(set(filtered))

def get_disease_ontology():
    global _ontology_cache
    if _ontology_cache is not None:
        return _ontology_cache
    try:
        logger.info(f"Loading disease ontology from {DOID_ONTOLOGY_URL}...")
        _ontology_cache = get_ontology(DOID_ONTOLOGY_URL).load()
        logger.info("Disease ontology loaded successfully")
        return _ontology_cache
    except Exception as e:
        logger.exception(f"Failed to load disease ontology: {e}")
        raise

try:
    get_disease_ontology()
    logger.info("? DOID ontology preloaded")
except Exception as e:
    logger.warning(f"DOID preload failed: {e}")

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

# -------------------- OBO (DOID owlready2) --------------------
class OboDiseaseFetcher:
    def _sync_fetch(self, disease_name: str) -> List[str]:
        try:
            onto = get_disease_ontology()
            term = onto.search_one(label=disease_name)
            if not term:
                return []
            descs = term.descendants()
            descs.discard(term)
            names = []
            if hasattr(term, "label") and term.label and len(term.label) > 0:
                names.append(term.label[0])
            for d in descs:
                if hasattr(d, "label") and d.label and len(d.label) > 0:
                    names.append(d.label[0])
            result = sorted(set(names))
            logger.info(f"[OBO] Found {len(result)} terms for '{disease_name}'")
            return result
        except Exception as e:
            logger.exception(f"[OBO] Failed for '{disease_name}': {e}")
            return []

    async def fetch(self, disease_name: str) -> List[str]:
        if not disease_name or not disease_name.strip():
            return []
        return await asyncio.to_thread(self._sync_fetch, disease_name.strip())

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
        try:
            resp = await client.post(self.API_URL, json={"query": query, "variables": variables})
            resp.raise_for_status()
            return resp.json().get("data", {})
        except Exception as e:
            logger.error(f"[OpenTargets] GraphQL error: {e}")
            return {}

    async def fetch(self, disease_name: str) -> List[str]:
        if not disease_name or not disease_name.strip():
            return []
        query_norm = _norm(disease_name)
        queries = { ... }  # ? your original 4 GraphQL queries (unchanged ? paste them here exactly as before)
        # (for brevity in this message, the full OpenTargets fetch logic is identical to the previous version you had)
        # ... [full OpenTargets code from earlier messages] ...
        # (it ends with the same result = sorted(...) block)

# -------------------- Aggregator with 7 sources --------------------
class DiseaseSynonymAggregator:
    def __init__(self):
        self.sources = {
            "NLM": NLMDiseaseFetcher(),
            "OBO": OboDiseaseFetcher(),
            "EBI_DOID": OLSDiseaseFetcher(ontology="doid"),
            # "MONDO": OLSDiseaseFetcher(ontology="mondo"),
            # "NCIT": OLSDiseaseFetcher(ontology="ncit"),
            # "ORDO": OLSDiseaseFetcher(ontology="ordo"), 
            # "OpenTargets": OpenTargetsDiseaseFetcher(),
        }

    async def close(self):
        for source in self.sources.values():
            if hasattr(source, 'close'):
                await source.close()

    async def get_all_synonyms(self, disease_name: str) -> Dict[str, object]:
        if not disease_name or not disease_name.strip():
            return {"combined_synonyms": [], "synonyms_by_source": {}, "official_name": disease_name}
        logger.info(f"[Aggregator] Fetching for '{disease_name}'")
        tasks = {name: source.fetch(disease_name) for name, source in self.sources.items()}
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True),
                timeout=OVERALL_TIMEOUT_SEC
            )
            synonyms_by_source = {}
            for (name, _), result in zip(tasks.items(), results):
                if isinstance(result, Exception):
                    logger.error(f"[{name}] Error: {result}")
                    synonyms_by_source[name] = []
                elif isinstance(result, list):
                    synonyms_by_source[name] = result
                else:
                    synonyms_by_source[name] = []
        except asyncio.TimeoutError:
            logger.error(f"[Aggregator] Timeout for '{disease_name}'")
            synonyms_by_source = {name: [] for name in self.sources.keys()}
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

    async def get_synonyms_by_source(self, disease_name: str, source: str) -> List[str]:
        src = self.sources.get(source)
        if not src:
            return []
        terms = await src.fetch(disease_name)
        return _filter_terms_by_query(set(terms), disease_name)