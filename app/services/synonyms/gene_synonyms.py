import os
import re
import sys
import asyncio
import logging
from typing import List, Dict, Optional, Union, Set
from urllib.parse import quote

import aiohttp
import httpx
import mygene

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# Configuration
OVERALL_TIMEOUT_SEC = float(os.getenv("GENE_FETCH_TIMEOUT_SEC", "60"))
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "12"))
# Per-source cap: NCBIGene makes 2 HTTP calls + retries, give it more room
GENE_SOURCE_TIMEOUT_SEC = float(os.getenv("GENE_SOURCE_TIMEOUT_SEC", "15"))


class UniProtGeneFetcher:
    """Fetch gene synonyms from UniProt."""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
            )
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def fetch(
        self,
        gene_symbol: str,
        organism_id: Optional[int] = 9606
    ) -> List[str]:
        """
        Fetch gene synonyms from UniProt.
        
        Args:
            gene_symbol: Gene symbol to search
            organism_id: NCBI taxonomy ID (9606 = human)
            
        Returns:
            List of gene synonyms
        """
        if not gene_symbol or not gene_symbol.strip():
            logger.warning("[UniProt] Empty gene_symbol provided")
            return []
        
        base_url = "https://rest.uniprot.org/uniprotkb/search"
        
        # FIX: URL encode gene symbol
        encoded_symbol = quote(gene_symbol.strip())
        query = f"gene_exact:{encoded_symbol}"
        
        if organism_id is not None:
            query += f" AND organism_id:{organism_id}"

        params = {
            "query": query,
            "format": "json",
            "fields": "gene_names",
            "size": 100
        }

        synonyms: Set[str] = set()
        
        try:
            session = await self._get_session()
            
            async with session.get(base_url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[UniProt] HTTP {resp.status} for '{gene_symbol}'"
                    )
                    return []
                
                data = await resp.json()
                
                if not isinstance(data, dict):
                    logger.warning("[UniProt] Invalid response type")
                    return []
                
                results = data.get("results", [])
                if not isinstance(results, list):
                    return []
                
                query_upper = gene_symbol.strip().upper()

                # Separate entries where the query is the PRIMARY gene symbol
                # from entries where it only appears as an alias of a different
                # gene (e.g. "HTT" is an alias of SLC6A4).  Prefer primary
                # matches; fall back to alias-only matches when no primary match
                # exists (handles HER2→ERBB2, p53→TP53, etc.).
                primary_genes: list = []
                alias_genes: list = []
                for entry in results:
                    if not isinstance(entry, dict):
                        continue
                    for gene in (entry.get("genes") or []):
                        if not isinstance(gene, dict):
                            continue
                        gn = gene.get("geneName")
                        primary = (gn.get("value", "") if isinstance(gn, dict) else "").strip()
                        if primary.upper() == query_upper:
                            primary_genes.append(gene)
                        else:
                            syn_vals = {
                                s.get("value", "").upper()
                                for s in (gene.get("synonyms") or [])
                                if isinstance(s, dict)
                            }
                            if query_upper in syn_vals:
                                alias_genes.append(gene)

                for gene in (primary_genes if primary_genes else alias_genes):
                    gn = gene.get("geneName")
                    if isinstance(gn, dict) and gn.get("value"):
                        synonyms.add(gn["value"].strip())
                    for syn in (gene.get("synonyms") or []):
                        if isinstance(syn, dict) and syn.get("value"):
                            synonyms.add(syn["value"].strip())
            
            result = sorted(synonyms)
            logger.info(f"[UniProt] Found {len(result)} synonyms for '{gene_symbol}'")
            return result
            
        except aiohttp.ClientError as e:
            logger.error(f"[UniProt] HTTP error for '{gene_symbol}': {e}")
            return []
        except asyncio.TimeoutError:
            logger.error(f"[UniProt] Timeout for '{gene_symbol}'")
            return []
        except Exception as e:
            logger.exception(f"[UniProt] Failed for '{gene_symbol}': {e}")
            return []


class HGNCGeneFetcher:
    """Fetch gene synonyms from HGNC."""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
            )
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def fetch(self, gene_symbol: str) -> List[str]:
        """
        Fetch gene synonyms from HGNC.
        
        Args:
            gene_symbol: Gene symbol to search
            
        Returns:
            List of gene synonyms
        """
        if not gene_symbol or not gene_symbol.strip():
            logger.warning("[HGNC] Empty gene_symbol provided")
            return []
        
        # FIX: URL encode gene symbol
        encoded_symbol = quote(gene_symbol.strip())
        url = f"https://rest.genenames.org/fetch/symbol/{encoded_symbol}"
        headers = {"Accept": "application/json"}

        try:
            session = await self._get_session()
            
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"[HGNC] HTTP {resp.status} for '{gene_symbol}'")
                    return []
                
                data = await resp.json()
                
        except aiohttp.ClientError as e:
            logger.error(f"[HGNC] HTTP error for '{gene_symbol}': {e}")
            return []
        except asyncio.TimeoutError:
            logger.error(f"[HGNC] Timeout for '{gene_symbol}'")
            return []
        except Exception as e:
            logger.exception(f"[HGNC] Failed for '{gene_symbol}': {e}")
            return []

        # Parse response
        if not isinstance(data, dict):
            logger.warning("[HGNC] Invalid response type")
            return []
        
        response = data.get("response")
        if not isinstance(response, dict):
            return []
        
        docs = response.get("docs")
        if not isinstance(docs, list) or not docs:
            logger.info(f"[HGNC] No results for '{gene_symbol}'")
            return []

        doc = docs[0]
        if not isinstance(doc, dict):
            return []
        
        synonyms: Set[str] = set()
        
        # Extract synonyms from various fields
        for field in ["alias_symbol", "alias_name", "prev_symbol"]:
            values = doc.get(field)
            
            if isinstance(values, list):
                for val in values:
                    if isinstance(val, str) and val.strip():
                        synonyms.add(val.strip())
            elif isinstance(values, str) and values.strip():
                synonyms.add(values.strip())

        # Add official symbol
        symbol = doc.get("symbol")
        if symbol and isinstance(symbol, str):
            synonyms.add(symbol.strip())

        result = sorted(synonyms)
        logger.info(f"[HGNC] Found {len(result)} synonyms for '{gene_symbol}'")
        return result

    async def lookup_hgnc_id(self, symbol: str) -> Optional[str]:
        """Return the HGNC ID (e.g. 'HGNC:4851') if *symbol* is a current
        HGNC-approved primary symbol, else None.

        Uses the same /fetch/symbol/<sym> endpoint used by fetch() — primary
        symbols only, aliases return no docs.
        """
        if not symbol or not symbol.strip():
            return None
        encoded = quote(symbol.strip().upper())
        url = f"https://rest.genenames.org/fetch/symbol/{encoded}"
        headers = {"Accept": "application/json"}
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        docs = data.get("response", {}).get("docs", [])
        if not docs or not isinstance(docs[0], dict):
            return None
        return docs[0].get("hgnc_id") or None


class MyGeneInfoFetcher:
    """Fetch gene synonyms from MyGene.info (synchronous library)."""
    
    def _sync_query(
        self,
        gene_symbol: str,
        species: Union[str, int]
    ) -> List[str]:
        """Synchronous query to MyGene.info."""
        mg = mygene.MyGeneInfo()
        
        try:
            result = mg.query(
                gene_symbol,
                species=species,
                fields="symbol,name,alias,other_names",
                size=5,
            )

            if not isinstance(result, dict):
                return []

            hits = result.get("hits")
            if not isinstance(hits, list) or not hits:
                return []

            # Prefer the hit whose primary symbol exactly matches the query
            # (guards against e.g. "HTT" ranking SLC6A4/"5-HTT" first).
            query_upper = gene_symbol.strip().upper()
            hit = next(
                (h for h in hits if isinstance(h, dict) and h.get("symbol", "").upper() == query_upper),
                hits[0],
            )
            if not isinstance(hit, dict):
                return []

            synonyms: Set[str] = set()

            for key in ["symbol", "name", "alias", "other_names"]:
                val = hit.get(key)
                
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str) and item.strip():
                            synonyms.add(item.strip())
                elif isinstance(val, str) and val.strip():
                    synonyms.add(val.strip())
            
            return sorted(synonyms)
            
        except Exception as e:
            logger.exception(f"[MyGene.info] Failed for '{gene_symbol}': {e}")
            return []
    
    async def fetch(
        self,
        gene_symbol: str,
        species: Union[str, int] = "human"
    ) -> List[str]:
        """
        Fetch gene synonyms from MyGene.info.
        
        Args:
            gene_symbol: Gene symbol to search
            species: Species name or NCBI taxonomy ID
            
        Returns:
            List of gene synonyms
        """
        if not gene_symbol or not gene_symbol.strip():
            logger.warning("[MyGene.info] Empty gene_symbol provided")
            return []
        
        logger.info(f"[MyGene.info] Fetching synonyms for '{gene_symbol}'")
        
        # Run synchronous library call in thread pool
        result = await asyncio.to_thread(
            self._sync_query,
            gene_symbol.strip(),
            species
        )
        
        logger.info(f"[MyGene.info] Found {len(result)} synonyms for '{gene_symbol}'")
        return result


class NCBIGeneFetcher:
    """Fetch gene aliases from NCBI Gene (Entrez esearch + esummary)."""

    _BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    _API_KEY = os.getenv("NCBI_API_KEY", "")

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_json(self, session: aiohttp.ClientSession, url: str, params: dict) -> tuple:
        """GET with up to 2 retries on 429/503 (NCBI hard rate limit: 3 req/s no key)."""
        for attempt in range(3):
            async with session.get(url, params=params) as r:
                if r.status in (429, 503) and attempt < 2:
                    wait = float(r.headers.get("Retry-After", "1"))
                    logger.warning(f"[NCBIGene] Rate limited ({r.status}), retry in {wait:.1f}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                return r.status, data
        return 503, {}

    async def fetch(self, gene_symbol: str) -> List[str]:
        if not gene_symbol or not gene_symbol.strip():
            return []
        name = gene_symbol.strip()
        base_params: Dict = {"api_key": self._API_KEY} if self._API_KEY else {}
        try:
            session = await self._get_session()
            # Step 1 – resolve gene ID for human; fetch up to 5 candidates
            # because a symbol may appear as an alias of another gene (e.g.
            # "HTT" is listed as an alias of SLC6A4 and NCBI ranks that first).
            search_params = {
                **base_params,
                "db": "gene",
                "term": f"{name}[sym] AND Homo sapiens[orgn]",
                "retmax": "5",
                "retmode": "json",
            }
            status, data = await self._get_json(session, f"{self._BASE}/esearch.fcgi", search_params)
            if status != 200:
                logger.warning(f"[NCBIGene] esearch HTTP {status} for '{gene_symbol}'")
                return []
            ids = data.get("esearchresult", {}).get("idlist", [])
            if not ids:
                logger.info(f"[NCBIGene] No gene ID found for '{gene_symbol}'")
                return []
            # Step 2 – fetch summaries for all candidates, pick the one whose
            # primary symbol matches the query (avoid alias-only hits).
            summary_params = {**base_params, "db": "gene", "id": ",".join(ids), "retmode": "json"}
            status, sdata = await self._get_json(session, f"{self._BASE}/esummary.fcgi", summary_params)
            if status != 200:
                logger.warning(f"[NCBIGene] esummary HTTP {status} for '{gene_symbol}'")
                return []
            result_map = (sdata or {}).get("result", {})
            # Prefer exact primary-symbol match; fall back to first candidate.
            chosen_id = next(
                (gid for gid in ids if result_map.get(gid, {}).get("name", "").upper() == name.upper()),
                ids[0],
            )
            doc = result_map.get(chosen_id, {})
            if not doc:
                return []
            synonyms: Set[str] = set()
            for alias in (doc.get("otheraliases") or "").split(","):
                if alias.strip():
                    synonyms.add(alias.strip())
            for desig in (doc.get("otherdesignations") or "").split("|"):
                if desig.strip():
                    synonyms.add(desig.strip())
            result = sorted(synonyms)
            logger.info(f"[NCBIGene] Found {len(result)} synonyms for '{gene_symbol}'")
            return result
        except asyncio.TimeoutError:
            logger.error(f"[NCBIGene] Timeout for '{gene_symbol}'")
            return []
        except Exception as e:
            logger.exception(f"[NCBIGene] Failed for '{gene_symbol}': {e}")
            return []


class OpenTargetsGeneFetcher:
    """Fetches gene/target synonyms from Open Targets Platform GraphQL API."""

    API_URL = "https://api.platform.opentargets.org/api/v4/graphql"

    def __init__(self):
        self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _run_graphql(self, client: httpx.AsyncClient, query: str, variables: dict) -> dict:
        # Retry transient gateway failures (502/503/504) + timeouts/transport errors;
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
                logger.error(f"[OpenTargets Gene] GraphQL error: {e}")
                return {}
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                logger.error(f"[OpenTargets Gene] GraphQL error: {e}")
                return {}
            except Exception as e:
                logger.error(f"[OpenTargets Gene] GraphQL error: {e}")
                return {}
        return {}

    # Single query — synonym data comes back inline via the Target fragment,
    # same approach as shared_synonym_utility.get_synonyms_by_symbol().
    _SEARCH_Q = """
        query searchGene($queryString: String!, $index: Int!, $size: Int!) {
            search(queryString: $queryString, entityNames: ["target"],
                   page: {index: $index, size: $size}) {
                hits {
                    id entity
                    object {
                        ... on Target {
                            approvedSymbol approvedName
                            symbolSynonyms { label }
                            nameSynonyms   { label }
                        }
                    }
                }
            }
        }"""

    async def fetch(self, gene_symbol: str) -> List[str]:
        if not gene_symbol or not gene_symbol.strip():
            return []

        try:
            client = await self._get_client()

            search_data = await self._run_graphql(
                client, self._SEARCH_Q,
                {"queryString": gene_symbol.strip(), "index": 0, "size": 3}
            )
            raw_hits = search_data.get("search", {}).get("hits", [])
            if not raw_hits:
                logger.info(f"[OpenTargets Gene] No results for '{gene_symbol}'")
                return []

            # Deduplicate by id
            seen_ids: Set[str] = set()
            unique_hits: List[dict] = []
            for h in raw_hits:
                hid = h.get("id") if isinstance(h, dict) else None
                if hid and hid not in seen_ids:
                    seen_ids.add(hid)
                    unique_hits.append(h)

            # Exact case-insensitive match on approvedSymbol preferred; fall back to top hit
            query_norm = gene_symbol.strip().lower()
            exact_hits = [
                h for h in unique_hits
                if (h.get("object", {}).get("approvedSymbol") or "").strip().lower() == query_norm
            ]
            best_hit = exact_hits[0] if exact_hits else unique_hits[0]
            target   = best_hit.get("object") or {}

            approved_symbol = (target.get("approvedSymbol") or "").strip()
            approved_name   = (target.get("approvedName")   or "").strip()
            symbol_syns = [s["label"] for s in target.get("symbolSynonyms", []) if isinstance(s, dict) and s.get("label")]
            name_syns   = [n["label"] for n in target.get("nameSynonyms",   []) if isinstance(n, dict) and n.get("label")]

            # Deduplicate case-insensitively, preserving first-seen casing
            seen_lower: Set[str] = set()
            result: List[str] = []
            for term in ([approved_symbol] if approved_symbol else []) + \
                        ([approved_name]   if approved_name   else []) + \
                        symbol_syns + name_syns:
                key = term.strip().lower()
                if key and key not in seen_lower:
                    seen_lower.add(key)
                    result.append(term.strip())

            result = sorted(result)
            logger.info(f"[OpenTargets Gene] Found {len(result)} synonyms for '{gene_symbol}'")
            return result

        except Exception as e:
            logger.exception(f"[OpenTargets Gene] Failed for '{gene_symbol}': {e}")
            return []


# Matches terms that look like HGNC-approved gene symbols (e.g. SLC6A4, BRAF, TP53).
# Used in the post-union cross-gene filter to identify candidates for HGNC ID lookup.
_HGNC_SYM_RE = re.compile(r"^[A-Z][A-Z0-9\-]{1,9}$")

_gene_synonym_cache: Dict[str, Dict] = {}


class GeneSynonymAggregator:
    """Aggregates gene synonyms from UniProt, HGNC, MyGene.info, NCBI Gene, and OpenTargets."""

    def __init__(self):
        self.sources = {
            "UniProt":       UniProtGeneFetcher(),
            "HGNC":          HGNCGeneFetcher(),
            "MyGene":        MyGeneInfoFetcher(),
            "NCBIGene":      NCBIGeneFetcher(),
            "OpenTargets":   OpenTargetsGeneFetcher(),
        }

    async def close(self):
        for source in self.sources.values():
            if hasattr(source, "close"):
                await source.close()

    async def get_all_synonyms(
        self,
        gene_symbol: str,
        organism_id: Optional[int] = 9606,
        mygene_species: Union[str, int] = "human",
    ) -> Dict[str, object]:
        if not gene_symbol or not gene_symbol.strip():
            return {"combined_synonyms": [], "synonyms_by_source": {}, "official_symbol": gene_symbol}

        cache_key = gene_symbol.strip().upper()
        if cache_key in _gene_synonym_cache:
            logger.info(f"[GeneSynonymAggregator] Cache hit for '{gene_symbol}'")
            return _gene_synonym_cache[cache_key]

        logger.info(f"[GeneSynonymAggregator] Fetching synonyms for '{gene_symbol}'")

        # Wrap each source individually so a single slow source does not block others.
        tasks = {
            "UniProt":     asyncio.wait_for(self.sources["UniProt"].fetch(gene_symbol, organism_id=organism_id), timeout=GENE_SOURCE_TIMEOUT_SEC),
            "HGNC":        asyncio.wait_for(self.sources["HGNC"].fetch(gene_symbol),                             timeout=GENE_SOURCE_TIMEOUT_SEC),
            "MyGene":      asyncio.wait_for(self.sources["MyGene"].fetch(gene_symbol, species=mygene_species),    timeout=GENE_SOURCE_TIMEOUT_SEC),
            "NCBIGene":    asyncio.wait_for(self.sources["NCBIGene"].fetch(gene_symbol),                         timeout=GENE_SOURCE_TIMEOUT_SEC),
            "OpenTargets": asyncio.wait_for(self.sources["OpenTargets"].fetch(gene_symbol),                      timeout=GENE_SOURCE_TIMEOUT_SEC),
        }

        raw = await asyncio.gather(*tasks.values(), return_exceptions=True)

        synonyms_by_source: Dict[str, List[str]] = {}
        for name, result in zip(tasks.keys(), raw):
            if isinstance(result, asyncio.TimeoutError):
                logger.warning(f"[{name}] Source timeout after {GENE_SOURCE_TIMEOUT_SEC:.0f}s for '{gene_symbol}'")
                synonyms_by_source[name] = []
            elif isinstance(result, Exception):
                logger.error(f"[{name}] Error: {result}")
                synonyms_by_source[name] = []
            else:
                synonyms_by_source[name] = result if isinstance(result, list) else []

        combined = sorted(set().union(*synonyms_by_source.values()))
        logger.info(f"[GeneSynonymAggregator] {len(combined)} unique synonyms for '{gene_symbol}'")

        # ── Post-union cross-gene symbol filter ──────────────────────────────
        # Some databases (NCBI Gene, MyGene) store cross-gene aliases in a
        # target gene's "otheraliases" / "otherdesignations" fields.  After the
        # union step these stray aliases — which may include the canonical
        # primary HGNC symbol of a *different* gene (e.g. "SLC6A4" leaking
        # into HTT's synonym set via the "5HTT" alias chain) — cause spurious
        # DB matches that return completely wrong rows.
        #
        # Fix: look up the query gene's HGNC ID; then for every term in
        # combined that looks like a primary HGNC symbol (all-caps, 2–10 chars),
        # confirm its HGNC ID in parallel.  Any term whose HGNC ID differs from
        # the query gene's HGNC ID is a foreign canonical symbol and is removed.
        #
        # Design notes:
        #   • Only primary symbols are checked (/fetch/symbol/ returns nothing
        #     for aliases/prev_symbols), so legitimate aliases like "5HTT",
        #     "SERT", or "HD" are always kept.
        #   • When the query is not a current HGNC primary symbol (e.g. an alias
        #     like "HER2") query_hgnc_id is None and the filter is skipped
        #     entirely — safe fallback, no false removals.
        #   • Checks run in parallel with 5 s per-call timeout; slow HGNC
        #     responses are treated as "unknown" (term is kept).
        hgnc_fetcher = self.sources["HGNC"]
        query_upper = gene_symbol.strip().upper()
        try:
            query_hgnc_id: Optional[str] = await asyncio.wait_for(
                hgnc_fetcher.lookup_hgnc_id(query_upper), timeout=5.0
            )
        except Exception:
            query_hgnc_id = None

        if query_hgnc_id:
            candidates = [
                t for t in combined
                if t.upper() != query_upper and _HGNC_SYM_RE.match(t.upper())
            ]
            if candidates:
                hgnc_id_results = await asyncio.gather(
                    *[
                        asyncio.wait_for(hgnc_fetcher.lookup_hgnc_id(t), timeout=5.0)
                        for t in candidates
                    ],
                    return_exceptions=True,
                )
                foreign: Set[str] = {
                    candidates[i].upper()
                    for i, hid in enumerate(hgnc_id_results)
                    if isinstance(hid, str) and hid != query_hgnc_id
                }
                if foreign:
                    logger.info(
                        f"[GeneSynonymAggregator] Removed foreign-gene symbols "
                        f"for '{gene_symbol}': {foreign}"
                    )
                    combined = sorted(t for t in combined if t.upper() not in foreign)
        # ─────────────────────────────────────────────────────────────────────

        result = {
            "combined_synonyms": combined,
            "synonyms_by_source": synonyms_by_source,
            "official_symbol": gene_symbol,
        }
        _gene_synonym_cache[cache_key] = result
        return result
