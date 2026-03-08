
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
            await self._client.close()
    
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
            response = await client.get(url)
            response.raise_for_status()
            
            data = response.json()
            
            # Safer extraction with validation
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
            
            # Filter out empty strings and normalize
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
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        return self._client
    
    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.close()
    
    async def fetch(self, drug_name: str) -> List[str]:
        """
        Fetch drug synonyms from ChEMBL REST API.
        
        Args:
            drug_name: Drug name to search
            
        Returns:
            List of synonyms
        """
        if not drug_name or not drug_name.strip():
            logger.warning("[ChEMBL REST] Empty drug_name provided")
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
            
            result = sorted(synonyms)
            logger.info(f"[ChEMBL REST] Fetched {len(result)} synonyms for '{drug_name}'")
            return result
            
        except httpx.HTTPStatusError as e:
            logger.warning(f"[ChEMBL REST] HTTP {e.response.status_code} for '{drug_name}'")
            return []
        except httpx.TimeoutException:
            logger.warning(f"[ChEMBL REST] Timeout for '{drug_name}'")
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


class DrugSynonymAggregator:
    """
    Central aggregator to fetch drug synonyms from all available sources.
    
    Sources:
      - PubChem (async)
      - ChEMBL REST API (async)
      - ChEMBL Python Client (sync, lazy loaded)
      - DrugNER (sync)
    """

    def __init__(self):
        """Initialize all synonym sources."""
        self.async_sources = {
            "PubChem": PubChemFetcher(),
            "ChEMBL_REST": ChEMBLRestFetcher(),
        }
        
        self.sync_sources = {
            "ChEMBL_Client": ChEMBLClientFetcher(),
            "DrugNER": DrugNERFetcher(),
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
        
        # ---------- Async sources ----------
        async_tasks = {
            name: source.fetch(drug_name)
            for name, source in self.async_sources.items()
        }
        
        if async_tasks:
            async_results = await asyncio.gather(
                *async_tasks.values(),
                return_exceptions=True
            )
            
            for (name, _), result in zip(async_tasks.items(), async_results):
                if isinstance(result, Exception):
                    logger.error(f"[{name}] Error: {result}")
                    results[name] = []
                elif isinstance(result, list):
                    results[name] = result
                else:
                    logger.warning(f"[{name}] Unexpected result type: {type(result)}")
                    results[name] = []
        
        # ---------- Sync sources (run in thread pool) ----------
        # Wrap synchronous calls in asyncio.to_thread()
        sync_tasks = {
            name: asyncio.to_thread(source.fetch, drug_name)
            for name, source in self.sync_sources.items()
        }
        
        if sync_tasks:
            sync_results = await asyncio.gather(
                *sync_tasks.values(),
                return_exceptions=True
            )
            
            for (name, _), result in zip(sync_tasks.items(), sync_results):
                if isinstance(result, Exception):
                    logger.error(f"[{name}] Error: {result}")
                    results[name] = []
                elif isinstance(result, list):
                    results[name] = result
                else:
                    logger.warning(f"[{name}] Unexpected result type: {type(result)}")
                    results[name] = []
        
        return results