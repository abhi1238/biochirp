import os
import sys
import asyncio
import logging
from typing import List, Dict, Optional, Union, Set
from urllib.parse import quote

import aiohttp
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
                
                for entry in results:
                    if not isinstance(entry, dict):
                        continue
                    
                    genes = entry.get("genes", [])
                    if not isinstance(genes, list):
                        continue
                    
                    for gene in genes:
                        if not isinstance(gene, dict):
                            continue
                        
                        # Get primary gene name
                        gene_name = gene.get("geneName")
                        if isinstance(gene_name, dict):
                            primary = gene_name.get("value")
                            if primary and isinstance(primary, str):
                                synonyms.add(primary.strip())
                        
                        # Get synonyms
                        syns = gene.get("synonyms", [])
                        if isinstance(syns, list):
                            for syn in syns:
                                if isinstance(syn, dict):
                                    val = syn.get("value")
                                    if val and isinstance(val, str):
                                        synonyms.add(val.strip())
            
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
                size=1
            )
            
            if not isinstance(result, dict):
                return []
            
            hits = result.get("hits")
            if not isinstance(hits, list) or not hits:
                return []
            
            hit = hits[0]
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


class GeneSynonymAggregator:
    """
    Aggregates gene synonyms from UniProt, HGNC, and MyGene.info.
    """

    def __init__(self):
        # FIX: Create instances, not classes
        self.sources = {
            "UniProt": UniProtGeneFetcher(),
            "HGNC": HGNCGeneFetcher(),
            "MyGene": MyGeneInfoFetcher()
        }
    
    async def close(self):
        """Close all sources."""
        for source in self.sources.values():
            if hasattr(source, 'close'):
                await source.close()

    async def get_all_synonyms(
        self,
        gene_symbol: str,
        organism_id: Optional[int] = 9606,
        mygene_species: Union[str, int] = "human"
    ) -> Dict[str, object]:
        """
        Fetch gene synonyms from all available sources concurrently.
        
        Args:
            gene_symbol: Gene symbol to search
            organism_id: NCBI taxonomy ID for UniProt (9606 = human)
            mygene_species: Species for MyGene.info
            
        Returns:
            Dict with 'combined_synonyms', 'synonyms_by_source', and 'official_symbol'
        """
        if not gene_symbol or not gene_symbol.strip():
            logger.warning("[GeneSynonymAggregator] Empty gene_symbol provided")
            return {
                "combined_synonyms": [],
                "synonyms_by_source": {},
                "official_symbol": gene_symbol
            }
        
        logger.info(f"[GeneSynonymAggregator] Fetching synonyms for '{gene_symbol}'")
        
        # Prepare tasks
        uniprot_task = self.sources["UniProt"].fetch(
            gene_symbol,
            organism_id=organism_id
        )
        hgnc_task = self.sources["HGNC"].fetch(gene_symbol)
        mygene_task = self.sources["MyGene"].fetch(
            gene_symbol,
            species=mygene_species
        )

        try:
            # FIX: Add overall timeout
            results = await asyncio.wait_for(
                asyncio.gather(
                    uniprot_task,
                    hgnc_task,
                    mygene_task,
                    return_exceptions=True
                ),
                timeout=OVERALL_TIMEOUT_SEC
            )
            
            uniprot_syns, hgnc_syns, mygene_syns = results
            
            # Handle exceptions
            if isinstance(uniprot_syns, Exception):
                logger.error(f"[UniProt] Error: {uniprot_syns}")
                uniprot_syns = []
            
            if isinstance(hgnc_syns, Exception):
                logger.error(f"[HGNC] Error: {hgnc_syns}")
                hgnc_syns = []
            
            if isinstance(mygene_syns, Exception):
                logger.error(f"[MyGene] Error: {mygene_syns}")
                mygene_syns = []
            
        except asyncio.TimeoutError:
            logger.error(
                f"[GeneSynonymAggregator] Overall timeout ({OVERALL_TIMEOUT_SEC}s) "
                f"for '{gene_symbol}'"
            )
            uniprot_syns, hgnc_syns, mygene_syns = [], [], []

        synonyms_by_source = {
            "UniProt": uniprot_syns if isinstance(uniprot_syns, list) else [],
            "HGNC": hgnc_syns if isinstance(hgnc_syns, list) else [],
            "MyGene": mygene_syns if isinstance(mygene_syns, list) else []
        }

        # Combine and deduplicate
        combined = sorted(set().union(*synonyms_by_source.values()))
        
        logger.info(
            f"[GeneSynonymAggregator] Found {len(combined)} unique synonyms "
            f"for '{gene_symbol}'"
        )
        
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": synonyms_by_source,
            "official_symbol": gene_symbol
        }

    async def get_synonyms_by_source(
        self,
        gene_symbol: str,
        source: str
    ) -> List[str]:
        """
        Fetch from a single source.
        
        Args:
            gene_symbol: Gene symbol to search
            source: Source name (UniProt, HGNC, or MyGene)
            
        Returns:
            List of synonyms from that source
        """
        fetcher = self.sources.get(source)
        
        if not fetcher:
            logger.warning(f"[GeneSynonymAggregator] Invalid source: '{source}'")
            return []
        
        # FIX: All our fetchers are async, so just await
        return await fetcher.fetch(gene_symbol)