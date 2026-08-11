
import os
import logging
import asyncio
from typing import List, Dict, Optional, Set
from urllib.parse import quote
import httpx


from drug_named_entity_recognition import find_drugs

logger = logging.getLogger(__name__)

# Configuration
FETCH_TIMEOUT_SEC = float(os.getenv("DRUG_FETCH_TIMEOUT_SEC", "60"))
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "10"))
# ChEMBL REST is at ebi.ac.uk and frequently returns HTTP 500 — use a short
# per-request timeout so it doesn't block PubChem results.
CHEMBL_REST_TIMEOUT_SEC = float(os.getenv("CHEMBL_REST_TIMEOUT_SEC", "3"))
# Max wall-clock seconds to wait for each sync source (ChEMBL_Client, DrugNER)
# running in a thread. asyncio.to_thread cannot cancel the underlying thread,
# but the gather will at least unblock when this deadline passes.
SYNC_SOURCE_TIMEOUT_SEC = float(os.getenv("SYNC_SOURCE_TIMEOUT_SEC", "8"))

# Circuit breaker for ChEMBL REST: skip the source for CHEMBL_REST_COOLDOWN_SEC
# after CHEMBL_REST_CIRCUIT_THRESHOLD consecutive failures.
import time as _time
_CHEMBL_REST_FAILURE_COUNT: int = 0
_CHEMBL_REST_SKIP_UNTIL: float = 0.0
_CHEMBL_REST_CIRCUIT_THRESHOLD: int = int(os.getenv("CHEMBL_REST_CIRCUIT_THRESHOLD", "3"))
_CHEMBL_REST_COOLDOWN_SEC: float = float(os.getenv("CHEMBL_REST_COOLDOWN_SEC", "300"))

# Lazy load ChEMBL client
_chembl_client_cache = None
_chembl_load_attempted = False


def get_chembl_client():
    """
    Lazy load ChEMBL client with error handling.
    
    Returns:
        ChEMBL client object or None if unavailable
    """
    global _chembl_client_cache, _chembl_load_attempted
    
    # Return cached client if available
    if _chembl_client_cache is not None:
        return _chembl_client_cache
    
    # Don't retry if we already failed
    if _chembl_load_attempted:
        return None
    
    _chembl_load_attempted = True
    
    try:
        logger.info("Loading ChEMBL client...")
        from chembl_webresource_client.new_client import new_client
        _chembl_client_cache = new_client
        logger.info("ChEMBL client loaded successfully")
        return _chembl_client_cache
        
    except Exception as e:
        logger.error(f"Failed to load ChEMBL client: {e}")
        logger.warning(
            "ChEMBL client functionality will be unavailable. "
            "Service will continue with other drug synonym sources. "
            "Check if https://www.ebi.ac.uk/chembl/api is accessible."
        )
        return None


class PubChemFetcher:
    """Fetches drug synonyms from PubChem."""
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client
    
    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, drug_name: str) -> List[str]:
        """
        Fetch drug synonyms from PubChem.
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            List of synonyms
        """
        if not drug_name or not drug_name.strip():
            logger.warning("[PubChem] Empty drug_name provided")
            return []
        
        # URL encode drug name
        encoded_name = quote(drug_name.strip())
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded_name}/synonyms/JSON"
        
        try:
            client = await self._get_client()
            # Retry up to 2 times on 429/503 (PubChem: 5 req/s limit)
            response = None
            for attempt in range(3):
                response = await client.get(url)
                if response.status_code in (429, 503) and attempt < 2:
                    wait = float(response.headers.get("retry-after", "1"))
                    logger.warning(f"[PubChem] Rate limited ({response.status_code}), retry in {wait:.1f}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue
                break
            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                logger.warning(f"[PubChem] Invalid response type for '{drug_name}'")
                return []

            info_list = data.get("InformationList", {})
            if not isinstance(info_list, dict):
                return []

            info = info_list.get("Information", [])
            if not isinstance(info, list) or not info:
                return []

            first_item = info[0]
            if not isinstance(first_item, dict):
                return []

            synonyms = first_item.get("Synonym", [])
            if not isinstance(synonyms, list):
                return []

            clean_synonyms = [
                syn.strip() for syn in synonyms
                if isinstance(syn, str) and syn.strip()
            ]

            logger.info(f"[PubChem] Fetched {len(clean_synonyms)} synonyms for '{drug_name}'")
            return clean_synonyms

        except httpx.HTTPStatusError as e:
            logger.warning(f"[PubChem] HTTP {e.response.status_code} for '{drug_name}'")
            return []
        except httpx.TimeoutException:
            logger.warning(f"[PubChem] Timeout for '{drug_name}'")
            return []
        except Exception as e:
            logger.exception(f"[PubChem] Failed for '{drug_name}': {e}")
            return []


class ChEMBLRestFetcher:
    """Fetches drug synonyms from ChEMBL REST API."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            # Use dedicated short timeout — EBI frequently returns HTTP 500.
            self._client = httpx.AsyncClient(timeout=CHEMBL_REST_TIMEOUT_SEC)
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, drug_name: str) -> List[str]:
        """
        Fetch drug synonyms from ChEMBL REST API.

        Returns:
            List of synonyms
        """
        global _CHEMBL_REST_FAILURE_COUNT, _CHEMBL_REST_SKIP_UNTIL

        if not drug_name or not drug_name.strip():
            logger.warning("[ChEMBL REST] Empty drug_name provided")
            return []

        # Circuit-breaker check — skip if EBI has been consistently failing.
        if _time.monotonic() < _CHEMBL_REST_SKIP_UNTIL:
            logger.debug("[ChEMBL REST] circuit open — skipping")
            return []

        # URL encode drug name
        encoded_name = quote(drug_name.strip())
        url = f"https://www.ebi.ac.uk/chembl/api/data/molecule?format=json&pref_name__icontains={encoded_name}"

        synonyms: Set[str] = set()

        try:
            client = await self._get_client()
            response = await client.get(url)
            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                logger.warning(f"[ChEMBL REST] Invalid response type for '{drug_name}'")
                return []

            molecules = data.get("molecules", [])
            if not isinstance(molecules, list):
                return []

            for compound in molecules:
                if not isinstance(compound, dict):
                    continue

                # Add preferred name
                name = compound.get("pref_name", "")
                if name and isinstance(name, str):
                    synonyms.add(name.strip())

                # Add molecule synonyms
                mol_synonyms = compound.get("molecule_synonyms", [])
                if isinstance(mol_synonyms, list):
                    for syn in mol_synonyms:
                        if isinstance(syn, dict):
                            syn_val = syn.get("synonym", "")
                            if syn_val and isinstance(syn_val, str):
                                synonyms.add(syn_val.strip())

            # Success — reset circuit breaker counter.
            _CHEMBL_REST_FAILURE_COUNT = 0
            result = sorted(synonyms)
            logger.info(f"[ChEMBL REST] Fetched {len(result)} synonyms for '{drug_name}'")
            return result

        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            _CHEMBL_REST_FAILURE_COUNT += 1
            label = (
                f"HTTP {e.response.status_code}"
                if isinstance(e, httpx.HTTPStatusError)
                else "Timeout"
            )
            logger.warning(f"[ChEMBL REST] {label} for '{drug_name}' (failures={_CHEMBL_REST_FAILURE_COUNT})")
            if _CHEMBL_REST_FAILURE_COUNT >= _CHEMBL_REST_CIRCUIT_THRESHOLD:
                _CHEMBL_REST_SKIP_UNTIL = _time.monotonic() + _CHEMBL_REST_COOLDOWN_SEC
                logger.warning(
                    f"[ChEMBL REST] circuit tripped after {_CHEMBL_REST_FAILURE_COUNT} failures — "
                    f"skipping for {_CHEMBL_REST_COOLDOWN_SEC:.0f}s"
                )
            return []
        except Exception as e:
            logger.exception(f"[ChEMBL REST] Failed for '{drug_name}': {e}")
            return []


class ChEMBLClientFetcher:
    """Fetches drug synonyms using ChEMBL Python client (synchronous)."""
    
    def fetch(self, drug_name: str) -> List[str]:
        """
        Fetch drug synonyms from ChEMBL using Python client.
        
        Note: This is a synchronous method and should be wrapped in
        asyncio.to_thread() when called from async context.
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            List of synonyms (empty if ChEMBL unavailable)
        """
        if not drug_name or not drug_name.strip():
            logger.warning("[ChEMBL Client] Empty drug_name provided")
            return []
        
        # Get client dynamically (lazy loaded)
        chembl_client = get_chembl_client()
        
        if chembl_client is None:
            logger.debug(
                "[ChEMBL Client] Client unavailable, skipping synonym fetch "
                f"for '{drug_name}'"
            )
            return []
        
        synonyms: Set[str] = set()
        
        try:
            molecule_client = chembl_client.molecule
            results = molecule_client.filter(pref_name__iexact=drug_name.strip())
            
            for mol in results:
                if not isinstance(mol, dict):
                    continue
                
                # Add molecule synonyms
                if "molecule_synonyms" in mol:
                    mol_syns = mol["molecule_synonyms"]
                    if isinstance(mol_syns, list):
                        for syn in mol_syns:
                            if isinstance(syn, dict):
                                syn_val = syn.get("synonyms", "")
                                # Filter empty strings
                                if syn_val and isinstance(syn_val, str):
                                    synonyms.add(syn_val.strip())
                
                # Add preferred and molecule names
                pref_name = mol.get("pref_name", "")
                if pref_name and isinstance(pref_name, str):
                    synonyms.add(pref_name.strip())
                
                mol_name = mol.get("molecule_name", "")
                if mol_name and isinstance(mol_name, str):
                    synonyms.add(mol_name.strip())
            
            # Add original query term
            synonyms.add(drug_name.strip())
            
            result = sorted(synonyms)
            logger.info(f"[ChEMBL Client] Fetched {len(result)} synonyms for '{drug_name}'")
            return result
            
        except Exception as e:
            logger.exception(f"[ChEMBL Client] Failed for '{drug_name}': {e}")
            return []


class DrugNERFetcher:
    """Fetches synonyms using DrugNER extraction (synchronous)."""
    
    def fetch(self, drug_name: str) -> List[str]:
        """
        Fetch drug synonyms using DrugNER.
        
        Note: This is a synchronous method and should be wrapped in
        asyncio.to_thread() when called from async context.
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            List of synonyms
        """
        if not drug_name or not drug_name.strip():
            logger.warning("[DrugNER] Empty drug_name provided")
            return []
        
        synonyms: Set[str] = set()
        
        try:
            results = find_drugs([drug_name.strip()])
            
            for item in results:
                # Results format: (drug_info, ...)
                if not item:
                    continue
                
                drug_info = item[0] if isinstance(item, (list, tuple)) else item
                
                if not isinstance(drug_info, dict):
                    continue
                
                # Add drug name
                name = drug_info.get("name", "")
                if name and isinstance(name, str):
                    synonyms.add(name.strip())
                
                # Add synonyms
                syns = drug_info.get("synonyms", [])
                if isinstance(syns, list):
                    for syn in syns:
                        if isinstance(syn, str) and syn.strip():
                            synonyms.add(syn.strip())
            
            result = sorted(synonyms)
            logger.info(f"[DrugNER] Fetched {len(result)} synonyms for '{drug_name}'")
            return result
            
        except Exception as e:
            logger.exception(f"[DrugNER] Failed for '{drug_name}': {e}")
            return []


class RxNormFetcher:
    """Fetches drug synonyms from the NLM RxNorm API.

    Uses two sequential calls:
      1. Resolve the drug name to an RxCUI (with approximate-term fallback).
      2. Fetch related concepts filtered to BN (brand), IN (ingredient) and
         PIN (precise ingredient) — dosage-form strings (SBD/SCD/DF) are
         intentionally excluded to keep results clean synonym strings.

    No API key required; NLM public endpoint.
    """

    _BASE = "https://rxnav.nlm.nih.gov/REST"
    # Only concept types that yield clean synonym strings.
    _USEFUL_TTY = "BN+IN+PIN"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, drug_name: str) -> List[str]:
        if not drug_name or not drug_name.strip():
            return []
        name = drug_name.strip()
        try:
            client = await self._get_client()

            # Step 1 – exact resolve
            r = await client.get(f"{self._BASE}/rxcui.json",
                                 params={"name": name})
            r.raise_for_status()
            rxcuis: List[str] = r.json().get("idGroup", {}).get("rxnormId") or []

            # Step 1b – approximate fallback (handles brand→generic etc.)
            if not rxcuis:
                r2 = await client.get(f"{self._BASE}/approximateTerm.json",
                                      params={"term": name, "maxEntries": "1"})
                r2.raise_for_status()
                candidates = r2.json().get("approximateGroup", {}).get("candidate", [])
                rxcuis = [c["rxcui"] for c in candidates if "rxcui" in c]

            if not rxcuis:
                logger.debug(f"[RxNorm] No RxCUI for '{drug_name}'")
                return []

            # Step 2 – fetch related brand/ingredient names
            r3 = await client.get(
                f"{self._BASE}/rxcui/{rxcuis[0]}/related.json",
                params={"tty": self._USEFUL_TTY},
            )
            r3.raise_for_status()
            names: Set[str] = set()
            for grp in r3.json().get("relatedGroup", {}).get("conceptGroup", []):
                for prop in grp.get("conceptProperties", []):
                    n = prop.get("name", "").strip()
                    if n:
                        names.add(n)

            result = sorted(names)
            logger.info(f"[RxNorm] Fetched {len(result)} synonyms for '{drug_name}'")
            return result

        except httpx.HTTPStatusError as e:
            logger.warning(f"[RxNorm] HTTP {e.response.status_code} for '{drug_name}'")
            return []
        except httpx.TimeoutException:
            logger.warning(f"[RxNorm] Timeout for '{drug_name}'")
            return []
        except Exception as e:
            logger.exception(f"[RxNorm] Failed for '{drug_name}': {e}")
            return []


class OpenTargetsDrugFetcher:
    """Fetches drug synonyms from Open Targets Platform GraphQL API."""

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

    async def _run_graphql(self, client: httpx.AsyncClient, query: str, variables: Dict) -> Dict:
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
                logger.error(f"[OpenTargets Drug] GraphQL error: {e}")
                return {}
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                logger.error(f"[OpenTargets Drug] GraphQL error: {e}")
                return {}
            except Exception as e:
                logger.error(f"[OpenTargets Drug] GraphQL error: {e}")
                return {}
        return {}

    _SALT_SUFFIXES: Set[str] = {
        "hydrochloride", "hcl", "mesylate", "mesilate", "tosylate",
        "sulfate", "sulphate", "phosphate", "tartrate", "acetate",
        "maleate", "fumarate", "citrate", "succinate", "gluconate",
        "malate", "besylate", "bromide", "chloride", "sodium", "potassium",
        "calcium", "magnesium", "zinc", "lysinate", "dl-lysine", "lysine",
        "xr", "er", "sr", "la", "cr",
    }

    _SEARCH_Q = """
        query SearchDrug($q: String!, $index: Int!, $size: Int!) {
            search(queryString: $q, entityNames: ["drug"], page: {index: $index, size: $size}) {
                hits { id name }
            }
        }"""

    _DETAIL_Q = """
        query GetDrugDetails($drugId: String!) {
            drug(chemblId: $drugId) {
                id name synonyms tradeNames
            }
        }"""

    async def _fetch_drug_detail(self, client: httpx.AsyncClient, drug_id: str) -> dict:
        data = await self._run_graphql(client, self._DETAIL_Q, {"drugId": drug_id})
        return data.get("drug") or {}

    async def fetch(self, drug_name: str) -> List[str]:
        if not drug_name or not drug_name.strip():
            return []

        try:
            client = await self._get_client()

            # STEP 1: Search — request enough hits for salt-variant detection
            search_data = await self._run_graphql(
                client, self._SEARCH_Q,
                {"q": drug_name.strip(), "index": 0, "size": 5}
            )
            raw_hits = search_data.get("search", {}).get("hits", [])
            if not raw_hits:
                logger.info(f"[OpenTargets Drug] No results for '{drug_name}'")
                return []

            # Deduplicate by id
            seen_ids: Set[str] = set()
            unique_hits: List[dict] = []
            for h in raw_hits:
                hid = h.get("id") if isinstance(h, dict) else None
                if hid and hid not in seen_ids:
                    seen_ids.add(hid)
                    unique_hits.append(h)

            # Exact case-insensitive match preferred; fall back to top hit
            query_norm   = drug_name.strip().lower()
            exact_hits   = [h for h in unique_hits if (h.get("name") or "").strip().lower() == query_norm]
            selected_hit = exact_hits[0] if exact_hits else unique_hits[0]
            drug_id      = selected_hit["id"]
            logger.debug(f"[OpenTargets Drug] selected={selected_hit.get('name')!r} id={drug_id!r}")

            # STEP 2: Fetch primary entry details
            drug_info = await self._fetch_drug_detail(client, drug_id)
            if not drug_info:
                return []

            canonical_name = (drug_info.get("name") or "").strip()
            trade_names: List[str] = list(drug_info.get("tradeNames") or [])
            synonyms:    List[str] = list(drug_info.get("synonyms")   or [])

            # STEP 3: Merge salt / formulation variants (best-effort, concurrent)
            def _is_salt(hit_name: str) -> bool:
                base = canonical_name.lower().strip()
                hn   = hit_name.lower().strip()
                if not base or not hn.startswith(base + " "):
                    return False
                suffix = hn[len(base):].strip()
                return bool(suffix) and all(t in self._SALT_SUFFIXES for t in suffix.split())

            variant_hits = [
                h for h in unique_hits
                if h["id"] != drug_id and _is_salt((h.get("name") or "").strip())
            ]
            if variant_hits:
                variant_details = await asyncio.gather(
                    *[self._fetch_drug_detail(client, vh["id"]) for vh in variant_hits],
                    return_exceptions=True,
                )
                for vd in variant_details:
                    if isinstance(vd, dict):
                        trade_names.extend(vd.get("tradeNames") or [])
                        synonyms.extend(vd.get("synonyms") or [])

            # Deduplicate case-insensitively, preserving first-seen casing
            seen_lower: Set[str] = set()
            result: List[str] = []
            for term in ([canonical_name] if canonical_name else []) + trade_names + synonyms:
                if not isinstance(term, str) or not term.strip():
                    continue
                key = term.strip().lower()
                if key not in seen_lower:
                    seen_lower.add(key)
                    result.append(term.strip())

            result = sorted(result)
            logger.info(f"[OpenTargets Drug] Found {len(result)} synonyms for '{drug_name}'")
            return result

        except Exception as e:
            logger.exception(f"[OpenTargets Drug] Failed for '{drug_name}': {e}")
            return []


class DrugSynonymAggregator:
    """
    Central aggregator to fetch drug synonyms from all available sources.

    Sources:
      - PubChem (async)
      - RxNorm   (async) — brand/generic/ingredient names, NLM public API
      - ChEMBL REST API (async)
      - OpenTargets (async) — drug synonyms and trade names
      - ChEMBL Python Client (sync, lazy loaded)
      - DrugNER (sync)
    """

    def __init__(self):
        """Initialize all synonym sources."""
        self.async_sources = {
            "PubChem":       PubChemFetcher(),
            "RxNorm":        RxNormFetcher(),
            "ChEMBL_REST":   ChEMBLRestFetcher(),
            "OpenTargets":   OpenTargetsDrugFetcher(),
        }

        self.sync_sources = {
            "ChEMBL_Client": ChEMBLClientFetcher(),
            "DrugNER":       DrugNERFetcher(),
        }
        
        logger.info(
            f"DrugSynonymAggregator initialized with "
            f"{len(self.async_sources)} async sources and "
            f"{len(self.sync_sources)} sync sources"
        )
    
    async def close(self):
        """Close all async sources."""
        for source in self.async_sources.values():
            if hasattr(source, 'close'):
                try:
                    await source.close()
                except Exception as e:
                    logger.debug(f"Error closing source: {e}")

    async def get_all_synonyms(self, drug_name: str) -> Dict[str, object]:
        """
        Get all synonyms for a drug from all sources.
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            Dict with:
              - 'combined_synonyms' (list): All unique synonyms
              - 'synonyms_by_source' (dict): Synonyms grouped by source
        """
        if not drug_name or not drug_name.strip():
            logger.warning("[DrugSynonymAggregator] Empty drug_name provided")
            return {
                "combined_synonyms": [],
                "synonyms_by_source": {}
            }
        
        logger.info(
            f"[DrugSynonymAggregator] Fetching synonyms for '{drug_name}' "
            f"from {len(self.async_sources) + len(self.sync_sources)} sources"
        )
        
        results = {}
        
        try:
            # Add overall timeout
            results = await asyncio.wait_for(
                self._fetch_all_sources(drug_name),
                timeout=FETCH_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[DrugSynonymAggregator] Overall timeout ({FETCH_TIMEOUT_SEC}s) "
                f"for '{drug_name}'"
            )
            # Return partial results if available
        except Exception as e:
            logger.exception(
                f"[DrugSynonymAggregator] Error fetching synonyms for '{drug_name}': {e}"
            )
        
        # Combine all synonyms
        combined: Set[str] = set()
        for source_syns in results.values():
            if isinstance(source_syns, list):
                combined.update(source_syns)
        
        combined_list = sorted(combined)
        
        total_from_sources = sum(len(v) for v in results.values() if isinstance(v, list))
        
        logger.info(
            f"[DrugSynonymAggregator] Found {len(combined_list)} unique synonyms "
            f"for '{drug_name}' from {total_from_sources} total results across "
            f"{len(results)} sources"
        )
        
        return {
            "combined_synonyms": combined_list,
            "synonyms_by_source": results,
        }
    
    async def _fetch_all_sources(self, drug_name: str) -> Dict[str, List[str]]:
        """
        Fetch from all sources (async and sync).
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            Dict mapping source name to list of synonyms
        """
        results = {}
        
        # ---------- Async sources (per-source timeout so one hang doesn't block others) ----------
        async_tasks = {
            name: asyncio.wait_for(source.fetch(drug_name), timeout=HTTP_TIMEOUT_SEC)
            for name, source in self.async_sources.items()
        }

        if async_tasks:
            async_results = await asyncio.gather(
                *async_tasks.values(),
                return_exceptions=True
            )

            for (name, _), result in zip(async_tasks.items(), async_results):
                if isinstance(result, asyncio.TimeoutError):
                    logger.warning(f"[{name}] Async source timeout after {HTTP_TIMEOUT_SEC:.0f}s for '{drug_name}'")
                    results[name] = []
                elif isinstance(result, Exception):
                    logger.error(f"[{name}] Error: {result}")
                    results[name] = []
                elif isinstance(result, list):
                    results[name] = result
                else:
                    logger.warning(f"[{name}] Unexpected result type: {type(result)}")
                    results[name] = []
        
        # ---------- Sync sources (run in thread pool) ----------
        # Each sync source gets an individual timeout so a hanging EBI call
        # (ChEMBL Client) cannot block for the full 60 s FETCH_TIMEOUT_SEC.
        sync_tasks = {
            name: asyncio.wait_for(
                asyncio.to_thread(source.fetch, drug_name),
                timeout=SYNC_SOURCE_TIMEOUT_SEC,
            )
            for name, source in self.sync_sources.items()
        }

        if sync_tasks:
            sync_results = await asyncio.gather(
                *sync_tasks.values(),
                return_exceptions=True
            )

            for (name, _), result in zip(sync_tasks.items(), sync_results):
                if isinstance(result, Exception):
                    if isinstance(result, asyncio.TimeoutError):
                        logger.warning(f"[{name}] Sync source timeout after {SYNC_SOURCE_TIMEOUT_SEC:.0f}s for '{drug_name}'")
                    else:
                        logger.error(f"[{name}] Error: {result}")
                    results[name] = []
                elif isinstance(result, list):
                    results[name] = result
                else:
                    logger.warning(f"[{name}] Unexpected result type: {type(result)}")
                    results[name] = []
        
        return results