# import asyncio
# import logging
# import httpx
# import requests
# from owlready2 import get_ontology
# from typing import List, Dict, Optional
# from concurrent.futures import ThreadPoolExecutor, as_completed

# # logger = logging.getLogger(__name__)
# logger = logging.getLogger("uvicorn.error")
# ONTO = get_ontology("http://purl.obolibrary.org/obo/doid.owl").load()


# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
# )
# # -------------------- Source 1: NLM Clinical Table --------------------

# class NLMDiseaseFetcher:
#     @staticmethod
#     async def fetch(disease_name: str) -> List[str]:
#         url = "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search"
#         params = {
#             "terms": disease_name,
#             "sf": "primary_name,synonyms",
#             "ef": "synonyms,word_synonyms,primary_name",
#             "maxList": 10000
#         }
#         synonyms_set = set()
#         try:
#             async with httpx.AsyncClient(timeout=10.0) as client:
#                 resp = await client.get(url, params=params)
#                 resp.raise_for_status()
#                 data = resp.json()

#                 if data and len(data) > 3 and data[3]:
#                     for entry in data[3]:
#                         if len(entry) > 0 and entry[0]:
#                             synonyms_set.add(entry[0])
#                         if len(entry) > 1 and entry[1]:
#                             synonyms_set.update(entry[1] if isinstance(entry[1], list) else [entry[1]])
#                         if len(entry) > 2 and entry[2]:
#                             synonyms_set.update(entry[2] if isinstance(entry[2], list) else [entry[2]])
#                 synonyms_set.add(disease_name)
#         except Exception as e:
#             logger.exception(f"[NLM] Failed for '{disease_name}': {e}")
#         return sorted(synonyms_set)


# # -------------------- Source 2: OBO Ontology --------------------

# class OboDiseaseFetcher:
#     @staticmethod
#     async def fetch(disease_name: str) -> List[str]:
#         try:
#             term = ONTO.search_one(label=disease_name)
#             if not term:
#                 return []
#             descs = term.descendants()
#             descs.discard(term)
#             names = sorted(set(d.label[0] for d in descs if getattr(d, "label", None)))
#             return names
#         except Exception as e:
#             logger.exception(f"[OBO] Failed for '{disease_name}': {e}")
#             return []
        
# # -------------------- Source 3: ebi ontologies--------------------


# class OLSDiseaseFetcher:
#     SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"
#     TERM_URL_TMPL = "https://www.ebi.ac.uk/ols4/api/ontologies/doid/terms/{encoded_iri}"

#     @staticmethod
#     async def fetch(disease_name: str) -> List[str]:
#         """
#         Fetch combined synonyms + descendants for a disease from EBI OLS (DOID).
#         Returns an empty list on any error or if nothing is found.
#         """
#         try:
#             async with httpx.AsyncClient(timeout=30.0) as client:

#                 # ---------------- STEP 1: Search disease ----------------
#                 resp = await client.get(
#                     OLSDiseaseFetcher.SEARCH_URL,
#                     params={
#                         "q": disease_name,
#                         "ontology": "doid",
#                         "rows": 1,
#                     },
#                 )
#                 resp.raise_for_status()
#                 data = resp.json()

#                 docs = data.get("response", {}).get("docs", [])
#                 if not docs:
#                     return []

#                 disease_doc = docs[0]
#                 iri = disease_doc.get("iri")
#                 label = disease_doc.get("label")

#                 if not iri:
#                     return []

#                 # ---------------- STEP 2: Fetch term metadata ----------------
#                 encoded_iri = requests.utils.quote(
#                     requests.utils.quote(iri, safe=""), safe=""
#                 )

#                 term_url = OLSDiseaseFetcher.TERM_URL_TMPL.format(
#                     encoded_iri=encoded_iri
#                 )

#                 term_resp = await client.get(term_url)
#                 term_resp.raise_for_status()
#                 term_data = term_resp.json()

#                 terms = set()

#                 # Root label
#                 if label:
#                     terms.add(label)

#                 # Synonyms
#                 for syn in term_data.get("synonyms", []):
#                     if isinstance(syn, str):
#                         terms.add(syn)

#                 # ---------------- STEP 3: Fetch descendants ----------------
#                 children_link = (
#                     term_data.get("_links", {})
#                     .get("children", {})
#                     .get("href")
#                 )

#                 if children_link:
#                     child_resp = await client.get(children_link)
#                     child_resp.raise_for_status()
#                     child_data = child_resp.json()

#                     for child in (
#                         child_data.get("_embedded", {}).get("terms", [])
#                     ):
#                         label = child.get("label")
#                         if label:
#                             terms.add(label)

#                 return sorted(terms)

#         except Exception as e:
#             logger.exception(f"[OLS] Failed for '{disease_name}': {e}")
#             return []


# class OpenTargetsDiseaseFetcher:
#     API_URL = "https://api.platform.opentargets.org/api/v4/graphql"

#     async def run_graphql(self, client: httpx.AsyncClient, query: str, variables: Dict) -> Dict:
#         try:
#             resp = await client.post(self.API_URL, json={"query": query, "variables": variables})
#             resp.raise_for_status()
#             data = resp.json()
#             return data.get("data", {})
#         except Exception as e:
#             logger.exception(f"[OpenTargets] GraphQL error: {e}")
#             return {}

#     async def fetch_all(self, disease_name: str) -> List[str]:
#         queries = {
#             "search": """
#                 query Search($q: String!) {
#                     search(queryString: $q, entityNames: ["disease"], page: {index: 0, size: 1}) {
#                         hits { id }
#                     }
#                 }""",
#             "descendants": """
#                 query GetDesc($id: String!) {
#                     disease(efoId: $id) { descendants }
#                 }""",
#             "disease_synonyms": """
#                 query GetDiseaseSynonyms($efoId: String!) {
#                     disease(efoId: $efoId) {
#                         name
#                         synonyms { terms }
#                     }
#                 }""",
#             "metadata": """
#                 query GetMeta($ids: [String!]!) {
#                     diseases(efoIds: $ids) {
#                         name
#                         synonyms { terms }
#                     }
#                 }"""
#         }

#         async with httpx.AsyncClient(timeout=15.0) as client:
#             # STEP 1: search disease
#             search_data = await self.run_graphql(
#                 client, queries["search"], {"q": disease_name}
#             )
#             hits = search_data.get("search", {}).get("hits", [])
#             if not hits:
#                 return []

#             root_id = hits[0]["id"]

#             # STEP 2: root synonyms
#             disease_data = await self.run_graphql(
#                 client, queries["disease_synonyms"], {"efoId": root_id}
#             )
#             disease_info = disease_data.get("disease", {})

#             terms = set()
#             if disease_info.get("name"):
#                 terms.add(disease_info["name"])

#             for group in disease_info.get("synonyms", []):
#                 terms.update(group.get("terms", []))

#             # STEP 3: descendants
#             desc_data = await self.run_graphql(
#                 client, queries["descendants"], {"id": root_id}
#             )
#             desc_ids = desc_data.get("disease", {}).get("descendants", [])

#             if not desc_ids:
#                 return sorted(terms)

#             # STEP 4: batch descendant metadata
#             chunk_size = 500
#             for i in range(0, len(desc_ids), chunk_size):
#                 chunk = desc_ids[i:i + chunk_size]
#                 meta_data = await self.run_graphql(
#                     client, queries["metadata"], {"ids": chunk}
#                 )
#                 for d in meta_data.get("diseases", []):
#                     if d.get("name"):
#                         terms.add(d["name"])
#                     for syn in d.get("synonyms", []):
#                         terms.update(syn.get("terms", []))

#             return sorted(terms)


# # -------------------- Aggregator --------------------



# class DiseaseSynonymAggregator:
#     def __init__(self):
#         self.sources = {
#             "NLM": NLMDiseaseFetcher,
#             "OBO": OboDiseaseFetcher,
#             "OpenTargets": OpenTargetsDiseaseFetcher(),
#             "ebi" : OLSDiseaseFetcher
#         }

#     async def get_all_synonyms(self, disease_name: str) -> Dict[str, List[str]]:
#         nlm_task = self.sources["NLM"].fetch(disease_name)
#         obo_task = self.sources["OBO"].fetch(disease_name)
#         ebi_task = self.sources["ebi"].fetch(disease_name)
#         ot_task = self.sources["OpenTargets"].fetch_all(disease_name)

#         nlm_syns, obo_syns, ot_syns, ebi_syns = await asyncio.gather(
#             nlm_task, obo_task, ot_task, ebi_task
#         )

#         # nlm_syns, obo_syns, ebi_syns = await asyncio.gather(
#         #     nlm_task, obo_task, ebi_task
#         # )

#         logger.info(f"Entry rerived from nlm: {len(nlm_syns)}")
#         logger.info(f"Entry rerived from obo_syns: {len(obo_syns)}")
#         logger.info(f"Entry rerived from ebi: {len(ebi_syns)}")
#         # logger.info(f"Entry rerived from OT: {len(ot_syns)}")

#         synonyms_by_source = {
#             "NLM": nlm_syns,
#             "OBO": obo_syns,
#             "EBI": ebi_syns,
#             "OpenTargets": ot_syns,
#         }


#         logger.info(f"Entry rerived all : {synonyms_by_source}")

#         # logger.info("\n--------------------------------")

#         combined = sorted(set().union(*synonyms_by_source.values()))


#         return {
#             "combined_synonyms": combined,
#             "synonyms_by_source": synonyms_by_source,
#             "official_name": disease_name,
#         }

#     async def get_synonyms_by_source(self, disease_name: str, source: str) -> List[str]:
#         src = self.sources.get(source)
#         if not src:
#             logger.warning(f"Unknown disease synonym source: {source}")
#             return []

#         if isinstance(src, OpenTargetsDiseaseFetcher):
#             return await src.fetch_all(disease_name)

#         return await src.fetch(disease_name)


# import os
# import sys
# import re
# import asyncio
# import logging
# from typing import List, Dict, Optional, Set
# from urllib.parse import quote

# import httpx
# from owlready2 import get_ontology

# # Configure logging
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
#     stream=sys.stdout
# )

# logger = logging.getLogger(__name__)

# # Configuration
# OVERALL_TIMEOUT_SEC = float(os.getenv("DISEASE_FETCH_TIMEOUT_SEC", "60"))
# HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "15"))
# DOID_ONTOLOGY_URL = os.getenv("DOID_ONTOLOGY_URL", "http://purl.obolibrary.org/obo/doid.owl")

# # Lazy load ontology
# _ontology_cache = None


# def _norm(text: str) -> str:
#     """
#     Normalize disease names for exact-match comparisons.
#     Lowercase, strip, remove punctuation, collapse whitespace.
#     """
#     if not text:
#         return ""
#     text = text.strip().lower()
#     text = re.sub(r"[^a-z0-9]+", " ", text)
#     return re.sub(r"\s+", " ", text).strip()


# def _acronym(tokens: List[str]) -> str:
#     """Build acronym from tokens (letters only)."""
#     letters = [t[0] for t in tokens if t and t[0].isalpha()]
#     return "".join(letters)


# def _filter_terms_by_query(terms: Set[str], disease_name: str) -> List[str]:
#     """
#     Filter terms to keep those that retain query-specific modifiers.
#     Keeps:
#       - exact normalized match
#       - acronym match (e.g., TNBC)
#       - terms containing all non-generic, non-numeric tokens
#     """
#     if not terms:
#         return []

#     query_norm = _norm(disease_name)
#     query_tokens = _norm(disease_name).split()
#     must_tokens = [t for t in query_tokens if t]
#     acronym = _acronym(query_tokens).lower()

#     if not must_tokens and not acronym:
#         return sorted(t.strip() for t in terms if isinstance(t, str) and t.strip())

#     filtered: List[str] = []
#     for term in terms:
#         if not isinstance(term, str) or not term.strip():
#             continue
#         tnorm = _norm(term)
#         if not tnorm:
#             continue
#         if tnorm == query_norm:
#             filtered.append(term.strip())
#             continue
#         if acronym and tnorm == acronym:
#             filtered.append(term.strip())
#             continue
#         t_tokens = set(tnorm.split())
#         if all(t in t_tokens for t in must_tokens):
#             filtered.append(term.strip())

#     return sorted(set(filtered))


# def get_disease_ontology():
#     """
#     Lazy load disease ontology with error handling.
    
#     Returns:
#         Loaded ontology object
#     """
#     global _ontology_cache
    
#     if _ontology_cache is not None:
#         return _ontology_cache
    
#     try:
#         logger.info(f"Loading disease ontology from {DOID_ONTOLOGY_URL}...")
#         _ontology_cache = get_ontology(DOID_ONTOLOGY_URL).load()
#         logger.info("Disease ontology loaded successfully")
#         return _ontology_cache
#     except Exception as e:
#         logger.exception(f"Failed to load disease ontology: {e}")
#         raise


# # -------------------- Source 1: NLM Clinical Table --------------------

# class NLMDiseaseFetcher:
#     """Fetch disease synonyms from NLM Clinical Tables."""
    
#     def __init__(self):
#         self._client: Optional[httpx.AsyncClient] = None
    
#     async def _get_client(self) -> httpx.AsyncClient:
#         """Get or create HTTP client."""
#         if self._client is None or self._client.is_closed:
#             self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
#         return self._client
    
#     async def close(self):
#         """Close HTTP client."""
#         if self._client and not self._client.is_closed:
#             await self._client.close()
    
#     async def fetch(self, disease_name: str) -> List[str]:
#         """
#         Fetch disease synonyms from NLM Clinical Tables.
        
#         Args:
#             disease_name: Disease name to search
            
#         Returns:
#             List of synonyms
#         """
#         if not disease_name or not disease_name.strip():
#             logger.warning("[NLM] Empty disease_name provided")
#             return []
        
#         query_norm = _norm(disease_name)
#         url = "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search"
        
#         # FIX: URL encode disease name
#         params = {
#             "terms": disease_name.strip(),
#             "sf": "primary_name,synonyms",
#             "ef": "synonyms,word_synonyms,primary_name",
#             "maxList": 10000
#         }
        
#         synonyms: Set[str] = set()
        
#         try:
#             client = await self._get_client()
#             resp = await client.get(url, params=params)
#             resp.raise_for_status()
            
#             data = resp.json()
            
#             # FIX: Safer nested access with validation
#             if not isinstance(data, list) or len(data) <= 3:
#                 logger.warning("[NLM] Invalid response format")
#                 return []
            
#             entries = data[3]
#             if not isinstance(entries, list):
#                 return []
            
#             for entry in entries:
#                 if not isinstance(entry, list):
#                     continue

#                 entry_terms: List[str] = []
#                 # Primary name (index 0)
#                 if len(entry) > 0 and entry[0] and isinstance(entry[0], str):
#                     entry_terms.append(entry[0].strip())

#                 # Synonyms (index 1)
#                 if len(entry) > 1 and entry[1]:
#                     if isinstance(entry[1], list):
#                         entry_terms.extend(s.strip() for s in entry[1] if isinstance(s, str) and s.strip())
#                     elif isinstance(entry[1], str):
#                         entry_terms.append(entry[1].strip())

#                 # Word synonyms (index 2)
#                 if len(entry) > 2 and entry[2]:
#                     if isinstance(entry[2], list):
#                         entry_terms.extend(s.strip() for s in entry[2] if isinstance(s, str) and s.strip())
#                     elif isinstance(entry[2], str):
#                         entry_terms.append(entry[2].strip())

#                 # Require exact normalized match against any term for this entry
#                 if not any(_norm(t) == query_norm for t in entry_terms):
#                     continue

#                 synonyms.update(entry_terms)
            
#             # Add original query
#             synonyms.add(disease_name.strip())
            
#             result = sorted(
#                 {s.strip() for s in synonyms if isinstance(s, str) and s.strip()}
#             )
#             logger.info(f"[NLM] Fetched {len(result)} synonyms for '{disease_name}'")
#             return result
            
#         except httpx.HTTPError as e:
#             logger.error(f"[NLM] HTTP error for '{disease_name}': {e}")
#             return []
#         except Exception as e:
#             logger.exception(f"[NLM] Failed for '{disease_name}': {e}")
#             return []


# # -------------------- Source 2: OBO Ontology --------------------

# class OboDiseaseFetcher:
#     """Fetch disease synonyms from OBO ontology (synchronous)."""
    
#     def _sync_fetch(self, disease_name: str) -> List[str]:
#         """
#         Synchronous fetch from ontology.
        
#         Note: owlready2 is synchronous, so this runs in thread pool.
#         """
#         try:
#             onto = get_disease_ontology()
            
#             term = onto.search_one(label=disease_name)
#             if not term:
#                 logger.info(f"[OBO] No term found for '{disease_name}'")
#                 return []
            
#             descs = term.descendants()
#             descs.discard(term)
            
#             names = sorted(
#                 set(
#                     d.label[0] for d in descs
#                     if hasattr(d, "label") and d.label
#                 )
#             )
            
#             logger.info(f"[OBO] Found {len(names)} descendants for '{disease_name}'")
#             return names
            
#         except Exception as e:
#             logger.exception(f"[OBO] Failed for '{disease_name}': {e}")
#             return []
    
#     async def fetch(self, disease_name: str) -> List[str]:
#         """
#         Fetch disease descendants from OBO ontology.
        
#         Args:
#             disease_name: Disease name to search
            
#         Returns:
#             List of descendant disease names
#         """
#         if not disease_name or not disease_name.strip():
#             logger.warning("[OBO] Empty disease_name provided")
#             return []
        
#         # FIX: Run synchronous operations in thread pool
#         return await asyncio.to_thread(self._sync_fetch, disease_name.strip())


# # -------------------- Source 3: EBI Ontology Lookup Service --------------------

# class OLSDiseaseFetcher:
#     """Fetch disease synonyms from EBI OLS."""
    
#     SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"
#     TERM_URL_TMPL = "https://www.ebi.ac.uk/ols4/api/ontologies/doid/terms/{encoded_iri}"
    
#     def __init__(self):
#         self._client: Optional[httpx.AsyncClient] = None
    
#     async def _get_client(self) -> httpx.AsyncClient:
#         """Get or create HTTP client."""
#         if self._client is None or self._client.is_closed:
#             self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
#         return self._client
    
#     async def close(self):
#         """Close HTTP client."""
#         if self._client and not self._client.is_closed:
#             await self._client.close()

#     async def fetch(self, disease_name: str) -> List[str]:
#         """
#         Fetch disease synonyms and descendants from EBI OLS.
        
#         Args:
#             disease_name: Disease name to search
            
#         Returns:
#             List of synonyms and descendant names
#         """
#         if not disease_name or not disease_name.strip():
#             logger.warning("[EBI OLS] Empty disease_name provided")
#             return []
        
#         query_norm = _norm(disease_name)
#         try:
#             client = await self._get_client()

#             # STEP 1: Search disease
#             resp = await client.get(
#                 self.SEARCH_URL,
#                 params={
#                     "q": disease_name.strip(),
#                     "ontology": "doid",
#                     "rows": 10,
#                 },
#             )
#             resp.raise_for_status()
#             data = resp.json()

#             if not isinstance(data, dict):
#                 return []
            
#             response = data.get("response", {})
#             if not isinstance(response, dict):
#                 return []
            
#             docs = response.get("docs", [])
#             if not isinstance(docs, list) or not docs:
#                 logger.info(f"[EBI OLS] No results for '{disease_name}'")
#                 return []

#             # Prefer exact normalized match on label/synonyms; otherwise abort
#             disease_doc = None
#             for doc in docs:
#                 if not isinstance(doc, dict):
#                     continue
#                 label = doc.get("label")
#                 doc_terms: List[str] = []
#                 if isinstance(label, str) and label.strip():
#                     doc_terms.append(label.strip())
#                 syns = doc.get("synonym") or doc.get("synonyms") or []
#                 if isinstance(syns, list):
#                     doc_terms.extend(s.strip() for s in syns if isinstance(s, str) and s.strip())
#                 elif isinstance(syns, str) and syns.strip():
#                     doc_terms.append(syns.strip())
#                 if any(_norm(t) == query_norm for t in doc_terms):
#                     disease_doc = doc
#                     break

#             if disease_doc is None:
#                 logger.info(f"[EBI OLS] No exact match for '{disease_name}'")
#                 return []

#             if not isinstance(disease_doc, dict):
#                 return []
            
#             iri = disease_doc.get("iri")
#             label = disease_doc.get("label")

#             if not iri:
#                 return []

#             # STEP 2: Fetch term metadata
#             # FIX: Use urllib.parse.quote instead of requests.utils.quote
#             encoded_iri = quote(quote(iri, safe=""), safe="")
#             term_url = self.TERM_URL_TMPL.format(encoded_iri=encoded_iri)

#             term_resp = await client.get(term_url)
#             term_resp.raise_for_status()
#             term_data = term_resp.json()

#             if not isinstance(term_data, dict):
#                 return []

#             terms: Set[str] = set()

#             # Root label
#             if label and isinstance(label, str):
#                 terms.add(label.strip())

#             # Synonyms
#             synonyms = term_data.get("synonyms", [])
#             if isinstance(synonyms, list):
#                 for syn in synonyms:
#                     if isinstance(syn, str) and syn.strip():
#                         terms.add(syn.strip())

#             # STEP 3: Fetch descendants
#             links = term_data.get("_links", {})
#             if isinstance(links, dict):
#                 children = links.get("children", {})
#                 if isinstance(children, dict):
#                     children_link = children.get("href")

#                     if children_link:
#                         child_resp = await client.get(children_link)
#                         child_resp.raise_for_status()
#                         child_data = child_resp.json()

#                         if isinstance(child_data, dict):
#                             embedded = child_data.get("_embedded", {})
#                             if isinstance(embedded, dict):
#                                 child_terms = embedded.get("terms", [])
#                                 if isinstance(child_terms, list):
#                                     for child in child_terms:
#                                         if isinstance(child, dict):
#                                             child_label = child.get("label")
#                                             if child_label and isinstance(child_label, str):
#                                                 terms.add(child_label.strip())

#             result = sorted(
#                 {t.strip() for t in terms if isinstance(t, str) and t.strip()}
#             )
#             logger.info(f"[EBI OLS] Found {len(result)} terms for '{disease_name}'")
#             return result

#         except httpx.HTTPError as e:
#             logger.error(f"[EBI OLS] HTTP error for '{disease_name}': {e}")
#             return []
#         except Exception as e:
#             logger.exception(f"[EBI OLS] Failed for '{disease_name}': {e}")
#             return []


# # -------------------- Source 4: Open Targets --------------------

# class OpenTargetsDiseaseFetcher:
#     """Fetch disease synonyms from Open Targets Platform."""
    
#     API_URL = "https://api.platform.opentargets.org/api/v4/graphql"
    
#     def __init__(self):
#         self._client: Optional[httpx.AsyncClient] = None
    
#     async def _get_client(self) -> httpx.AsyncClient:
#         """Get or create HTTP client."""
#         if self._client is None or self._client.is_closed:
#             self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
#         return self._client
    
#     async def close(self):
#         """Close HTTP client."""
#         if self._client and not self._client.is_closed:
#             await self._client.close()

#     async def run_graphql(
#         self,
#         client: httpx.AsyncClient,
#         query: str,
#         variables: Dict
#     ) -> Dict:
#         """Execute GraphQL query."""
#         try:
#             resp = await client.post(
#                 self.API_URL,
#                 json={"query": query, "variables": variables}
#             )
#             resp.raise_for_status()
#             data = resp.json()
            
#             if not isinstance(data, dict):
#                 return {}
            
#             return data.get("data", {})
            
#         except httpx.HTTPError as e:
#             logger.error(f"[OpenTargets] HTTP error: {e}")
#             return {}
#         except Exception as e:
#             logger.exception(f"[OpenTargets] GraphQL error: {e}")
#             return {}

#     async def fetch(self, disease_name: str) -> List[str]:
#         """
#         Fetch disease synonyms and descendants from Open Targets.
        
#         Args:
#             disease_name: Disease name to search
            
#         Returns:
#             List of synonyms and descendant names
#         """
#         if not disease_name or not disease_name.strip():
#             logger.warning("[OpenTargets] Empty disease_name provided")
#             return []
        
#         query_norm = _norm(disease_name)
#         queries = {
#             "search": """
#                 query Search($q: String!) {
#                     search(queryString: $q, entityNames: ["disease"], page: {index: 0, size: 1}) {
#                         hits { id name }
#                     }
#                 }""",
#             "descendants": """
#                 query GetDesc($id: String!) {
#                     disease(efoId: $id) { descendants }
#                 }""",
#             "disease_synonyms": """
#                 query GetDiseaseSynonyms($efoId: String!) {
#                     disease(efoId: $efoId) {
#                         name
#                         synonyms { terms }
#                     }
#                 }""",
#             "metadata": """
#                 query GetMeta($ids: [String!]!) {
#                     diseases(efoIds: $ids) {
#                         name
#                         synonyms { terms }
#                     }
#                 }"""
#         }

#         try:
#             client = await self._get_client()

#             # STEP 1: Search disease
#             search_data = await self.run_graphql(
#                 client, queries["search"], {"q": disease_name.strip()}
#             )
            
#             hits = search_data.get("search", {}).get("hits", [])
#             if not hits:
#                 logger.info(f"[OpenTargets] No results for '{disease_name}'")
#                 return []

#             # Prefer exact normalized match on name; otherwise abort
#             root_id = None
#             for hit in hits:
#                 if not isinstance(hit, dict):
#                     continue
#                 name = hit.get("name")
#                 if isinstance(name, str) and _norm(name) == query_norm:
#                     root_id = hit.get("id")
#                     break
#             if not root_id:
#                 logger.info(f"[OpenTargets] No exact match for '{disease_name}'")
#                 return []

#             # STEP 2: Root synonyms
#             disease_data = await self.run_graphql(
#                 client, queries["disease_synonyms"], {"efoId": root_id}
#             )
            
#             disease_info = disease_data.get("disease", {})
#             if not isinstance(disease_info, dict):
#                 disease_info = {}

#             terms: Set[str] = set()
            
#             name = disease_info.get("name")
#             if name and isinstance(name, str):
#                 terms.add(name.strip())

#             synonyms = disease_info.get("synonyms", [])
#             if isinstance(synonyms, list):
#                 for group in synonyms:
#                     if isinstance(group, dict):
#                         group_terms = group.get("terms", [])
#                         if isinstance(group_terms, list):
#                             terms.update(
#                                 t.strip() for t in group_terms
#                                 if isinstance(t, str) and t.strip()
#                             )

#             # STEP 3: Descendants
#             desc_data = await self.run_graphql(
#                 client, queries["descendants"], {"id": root_id}
#             )
            
#             disease_desc = desc_data.get("disease", {})
#             if isinstance(disease_desc, dict):
#                 desc_ids = disease_desc.get("descendants", [])
#             else:
#                 desc_ids = []

#             if not desc_ids or not isinstance(desc_ids, list):
#                 result = sorted(
#                     {t.strip() for t in terms if isinstance(t, str) and t.strip()}
#                 )
#                 logger.info(f"[OpenTargets] Found {len(result)} terms for '{disease_name}'")
#                 return result

#             # STEP 4: Batch descendant metadata
#             chunk_size = 500
#             for i in range(0, len(desc_ids), chunk_size):
#                 chunk = desc_ids[i:i + chunk_size]
                
#                 meta_data = await self.run_graphql(
#                     client, queries["metadata"], {"ids": chunk}
#                 )
                
#                 diseases = meta_data.get("diseases", [])
#                 if not isinstance(diseases, list):
#                     continue
                
#                 for d in diseases:
#                     if not isinstance(d, dict):
#                         continue
                    
#                     d_name = d.get("name")
#                     if d_name and isinstance(d_name, str):
#                         terms.add(d_name.strip())
                    
#                     d_syns = d.get("synonyms", [])
#                     if isinstance(d_syns, list):
#                         for syn in d_syns:
#                             if isinstance(syn, dict):
#                                 syn_terms = syn.get("terms", [])
#                                 if isinstance(syn_terms, list):
#                                     terms.update(
#                                         t.strip() for t in syn_terms
#                                         if isinstance(t, str) and t.strip()
#                                     )

#             result = sorted(
#                 {t.strip() for t in terms if isinstance(t, str) and t.strip()}
#             )
#             logger.info(f"[OpenTargets] Found {len(result)} terms for '{disease_name}'")
#             return result
            
#         except Exception as e:
#             logger.exception(f"[OpenTargets] Failed for '{disease_name}': {e}")
#             return []


# # -------------------- Aggregator --------------------

# class DiseaseSynonymAggregator:
#     """
#     Aggregate disease synonyms from multiple sources.
#     """
    
#     def __init__(self):
#         # FIX: Create instances consistently
#         self.sources = {
#             "NLM": NLMDiseaseFetcher(),
#             "OBO": OboDiseaseFetcher(),
#             "EBI": OLSDiseaseFetcher(),
#             # "OpenTargets": OpenTargetsDiseaseFetcher(),
#         }
    
#     async def close(self):
#         """Close all sources."""
#         for source in self.sources.values():
#             if hasattr(source, 'close'):
#                 await source.close()

#     async def get_all_synonyms(self, disease_name: str) -> Dict[str, object]:
#         """
#         Fetch disease synonyms from all sources.
        
#         Args:
#             disease_name: Disease name to search
            
#         Returns:
#             Dict with 'combined_synonyms', 'synonyms_by_source', and 'official_name'
#         """
#         if not disease_name or not disease_name.strip():
#             logger.warning("[DiseaseSynonymAggregator] Empty disease_name provided")
#             return {
#                 "combined_synonyms": [],
#                 "synonyms_by_source": {},
#                 "official_name": disease_name
#             }
        
#         logger.info(f"[DiseaseSynonymAggregator] Fetching synonyms for '{disease_name}'")
        
#         # Prepare tasks
#         tasks = {
#             name: source.fetch(disease_name)
#             for name, source in self.sources.items()
#         }

#         try:
#             # FIX: Add overall timeout
#             results = await asyncio.wait_for(
#                 asyncio.gather(*tasks.values(), return_exceptions=True),
#                 timeout=OVERALL_TIMEOUT_SEC
#             )
            
#             # Map results back to source names
#             synonyms_by_source = {}
#             for (name, _), result in zip(tasks.items(), results):
#                 if isinstance(result, Exception):
#                     logger.error(f"[{name}] Error: {result}")
#                     synonyms_by_source[name] = []
#                 elif isinstance(result, list):
#                     synonyms_by_source[name] = result
#                     # FIX: Fix typo
#                     logger.info(f"[{name}] Retrieved {len(result)} entries")
#                 else:
#                     logger.warning(f"[{name}] Unexpected result type: {type(result)}")
#                     synonyms_by_source[name] = []
            
#         except asyncio.TimeoutError:
#             logger.error(
#                 f"[DiseaseSynonymAggregator] Overall timeout ({OVERALL_TIMEOUT_SEC}s) "
#                 f"for '{disease_name}'"
#             )
#             synonyms_by_source = {name: [] for name in self.sources.keys()}

#         # Combine all synonyms
#         combined = sorted(set().union(*synonyms_by_source.values()))
        
#         logger.info(
#             f"[DiseaseSynonymAggregator] Found {len(combined)} unique synonyms "
#             f"for '{disease_name}'"
#         )

#         return {
#             "combined_synonyms": combined,
#             "synonyms_by_source": synonyms_by_source,
#             "official_name": disease_name,
#         }

#     async def get_synonyms_by_source(
#         self,
#         disease_name: str,
#         source: str
#     ) -> List[str]:
#         """
#         Fetch from a single source.
        
#         Args:
#             disease_name: Disease name to search
#             source: Source name (NLM, OBO, EBI, or OpenTargets)
            
#         Returns:
#             List of synonyms from that source
#         """
#         src = self.sources.get(source)
        
#         if not src:
#             logger.warning(f"[DiseaseSynonymAggregator] Unknown source: '{source}'")
#             return []

#         return await src.fetch(disease_name)



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
            "MONDO": OLSDiseaseFetcher(ontology="mondo"),
            "NCIT": OLSDiseaseFetcher(ontology="ncit"),
            "ORDO": OLSDiseaseFetcher(ontology="ordo"), 
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