

import logging
import re
import asyncio
from typing import List, Dict, Optional, Set
import aiohttp
import os

# FIX: Add missing pandas import
import pandas as pd

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuration
MAX_PAGES_PER_QUERY = int(os.getenv("MAX_PAGES_PER_QUERY", "100"))
FETCH_TIMEOUT_SEC = float(os.getenv("FETCH_TIMEOUT_SEC", "300"))
REQUEST_DELAY_MS = int(os.getenv("REQUEST_DELAY_MS", "100"))


def normalize(text: str) -> str:
    """Normalize text to alphanumeric lowercase."""
    return re.sub(r"[^a-z0-9]+", "", text.lower()) if text else ""


def expand_variants(keyword: str) -> Set[str]:
    """Expand keyword to include singular/plural variants."""
    if not keyword:
        return set()
    
    keyword = keyword.strip()
    if keyword.endswith("s"):
        return {keyword, keyword.rstrip("s")}
    else:
        return {keyword, keyword + "s"}


class HGNCAPIGeneFamilyFetcher:
    """Fetch gene family members from HGNC REST API."""
    
    BASE_URL = "https://rest.genenames.org/search/"
    HEADERS = {'Accept': 'application/json'}

    def __init__(self, rows_per_page: int = 100):
        self.rows_per_page = min(rows_per_page, 1000)  # Cap at API limit
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def name() -> str:
        return "HGNC"
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch(self, family_name: str) -> List[str]:
        """
        Fetch gene symbols for a gene family from HGNC API.
        
        Args:
            family_name: Gene family name to search
            
        Returns:
            List of gene symbols
        """
        if not family_name or not family_name.strip():
            logger.warning("[HGNC-API] Empty family_name provided")
            return []
        
        all_genes: Set[str] = set()
        variants = expand_variants(family_name)
        
        logger.info(f"[HGNC-API] Searching for '{family_name}' (variants: {variants})")
        
        session = await self._get_session()
        
        for variant in variants:
            query = f'family_name:"{variant}"'
            start = 0
            page_count = 0
            
            # FIX: Add page limit to prevent infinite loops
            while page_count < MAX_PAGES_PER_QUERY:
                page_count += 1
                url = f"{self.BASE_URL}{query}&start={start}&rows={self.rows_per_page}"
                
                try:
                    async with session.get(
                        url,
                        headers=self.HEADERS,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"[HGNC-API] HTTP {resp.status} for '{variant}' "
                                f"(page {page_count})"
                            )
                            break
                        
                        data = await resp.json()
                        
                        # Validate response structure
                        if not isinstance(data, dict):
                            logger.warning(f"[HGNC-API] Invalid response type: {type(data)}")
                            break
                        
                        response = data.get("response", {})
                        if not isinstance(response, dict):
                            logger.warning(f"[HGNC-API] Invalid response structure")
                            break
                        
                        docs = response.get("docs", [])
                        if not docs:
                            logger.debug(f"[HGNC-API] No more results for '{variant}'")
                            break
                        
                        # Extract gene symbols
                        for doc in docs:
                            if isinstance(doc, dict) and 'symbol' in doc:
                                symbol = doc['symbol']
                                if symbol:
                                    all_genes.add(symbol)
                        
                        logger.debug(
                            f"[HGNC-API] Page {page_count} for '{variant}': "
                            f"{len(docs)} docs, {len(all_genes)} total genes"
                        )
                        
                        start += self.rows_per_page
                        
                        # FIX: Add delay to avoid rate limiting
                        if REQUEST_DELAY_MS > 0:
                            await asyncio.sleep(REQUEST_DELAY_MS / 1000)
                        
                except asyncio.TimeoutError:
                    logger.error(f"[HGNC-API] Timeout for '{variant}' (page {page_count})")
                    break
                except Exception as e:
                    logger.exception(f"[HGNC-API] Request failed for '{variant}': {e}")
                    break
        
        result = sorted(all_genes)
        logger.info(f"[HGNC-API] Found {len(result)} genes for '{family_name}'")
        return result


class HGNCLocalGeneFamilyFetcher:
    """Fetch gene family members from local HGNC file."""
    
    def __init__(self, hgnc_file_path: str):
        self.hgnc_file_path = hgnc_file_path
        self.hgnc_data: Optional[pd.DataFrame] = None
        
        try:
            # FIX: Load in __init__ but make it sync
            self.hgnc_data = pd.read_csv(
                hgnc_file_path,
                sep="\t",
                on_bad_lines="skip",
                dtype=str
            )
            logger.info(f"[HGNC-Local] Loaded {len(self.hgnc_data)} rows from {hgnc_file_path}")
        except Exception as e:
            self.hgnc_data = None
            logger.error(f"[HGNC-Local] Failed to load file: {e}")

    @staticmethod
    def name() -> str:
        return "HGNC-Local"

    def _sync_fetch(self, family_keyword: str) -> List[str]:
        """Synchronous fetch to avoid blocking async event loop."""
        if self.hgnc_data is None:
            return []
        
        try:
            variants = expand_variants(family_keyword)
            
            # Filter rows where gene_family contains any variant
            mask = self.hgnc_data['gene_family'].fillna('').apply(
                lambda x: any(v.lower() in x.lower() for v in variants)
            )
            
            symbols = self.hgnc_data.loc[mask, 'symbol'].dropna().unique()
            return sorted(symbols.tolist())
            
        except Exception as e:
            logger.exception(f"[HGNC-Local] Search failed for '{family_keyword}': {e}")
            return []

    async def fetch(self, family_keyword: str) -> List[str]:
        """
        Fetch gene symbols for a gene family from local file.
        
        Args:
            family_keyword: Gene family keyword to search
            
        Returns:
            List of gene symbols
        """
        if not family_keyword or not family_keyword.strip():
            logger.warning("[HGNC-Local] Empty family_keyword provided")
            return []
        
        logger.info(f"[HGNC-Local] Searching for '{family_keyword}'")
        
        # FIX: Run pandas operations in thread pool to avoid blocking
        result = await asyncio.to_thread(self._sync_fetch, family_keyword)
        
        logger.info(f"[HGNC-Local] Found {len(result)} genes for '{family_keyword}'")
        return result


class MyGeneFamilyFetcher:
    """Fetch gene family members from MyGene.info API."""
    
    BASE_URL = "https://mygene.info/v3/query"

    def __init__(self, species: str = "human", size: int = 1000):
        self.species = species
        self.size = min(size, 1000)  # Cap at API limit
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def name() -> str:
        return "MyGene"
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch(self, keyword: str) -> List[str]:
        """
        Fetch gene symbols matching keyword from MyGene API.
        
        Args:
            keyword: Search keyword
            
        Returns:
            List of gene symbols
        """
        if not keyword or not keyword.strip():
            logger.warning("[MyGene] Empty keyword provided")
            return []
        
        variants = expand_variants(keyword)
        norm_variants = {normalize(v) for v in variants}
        result: Set[str] = set()
        
        logger.info(f"[MyGene] Searching for '{keyword}' (variants: {variants})")
        
        session = await self._get_session()

        for variant in variants:
            offset = 0
            page_count = 0
            
            # FIX: Add page limit
            while page_count < MAX_PAGES_PER_QUERY:
                page_count += 1
                
                try:
                    params = {
                        "q": variant,
                        "species": self.species,
                        "fields": "symbol,name,alias",
                        "size": self.size,
                        "from": offset
                    }
                    
                    async with session.get(
                        self.BASE_URL,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"[MyGene] HTTP {resp.status} for '{variant}' "
                                f"(page {page_count})"
                            )
                            break
                        
                        data = await resp.json()
                        
                        if not isinstance(data, dict):
                            logger.warning(f"[MyGene] Invalid response type")
                            break
                        
                        hits = data.get("hits", [])
                        if not hits:
                            logger.debug(f"[MyGene] No more results for '{variant}'")
                            break
                        
                        for hit in hits:
                            if not isinstance(hit, dict):
                                continue
                            
                            # Get all searchable fields
                            aliases = hit.get("alias", [])
                            if isinstance(aliases, str):
                                aliases = [aliases]
                            elif not isinstance(aliases, list):
                                aliases = []
                            
                            fields = [
                                hit.get("symbol", ""),
                                hit.get("name", "")
                            ] + aliases
                            
                            # Check if any field matches our keyword
                            if any(normalize(f) in norm_variants for f in fields if f):
                                symbol = hit.get("symbol")
                                if symbol:
                                    result.add(symbol)
                        
                        offset += self.size
                        
                        # FIX: Add delay
                        if REQUEST_DELAY_MS > 0:
                            await asyncio.sleep(REQUEST_DELAY_MS / 1000)
                        
                except asyncio.TimeoutError:
                    logger.error(f"[MyGene] Timeout for '{variant}' (page {page_count})")
                    break
                except Exception as e:
                    logger.exception(f"[MyGene] Failed for '{variant}': {e}")
                    break
        
        result_list = sorted(filter(None, result))
        logger.info(f"[MyGene] Found {len(result_list)} genes for '{keyword}'")
        return result_list


class UniProtFamilyFetcher:
    """Fetch gene family members from UniProt API."""
    
    BASE_URL = "https://rest.uniprot.org/uniprotkb/search"

    def __init__(self, size: int = 500):
        self.size = min(size, 500)  # Cap at API limit
        self._session: Optional[aiohttp.ClientSession] = None

    @staticmethod
    def name() -> str:
        return "UniProt"
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _query(self, session: aiohttp.ClientSession, term: str) -> List[dict]:
        """Query UniProt API with pagination."""
        results = []
        cursor = None
        page_count = 0
        
        # FIX: Add page limit
        while page_count < MAX_PAGES_PER_QUERY:
            page_count += 1
            
            params = {
                "query": f'"{term}" AND organism_id:9606',
                "fields": "accession,gene_names,protein_name,protein_families",
                "format": "json",
                "size": self.size
            }
            if cursor:
                params["cursor"] = cursor
            
            try:
                async with session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[UniProt] HTTP {resp.status} for '{term}' (page {page_count})"
                        )
                        break
                    
                    data = await resp.json()
                    
                    if not isinstance(data, dict):
                        logger.warning(f"[UniProt] Invalid response type")
                        break
                    
                    page_results = data.get("results", [])
                    results.extend(page_results)
                    
                    cursor = data.get("nextCursor")
                    if not cursor:
                        break
                    
                    # FIX: Add delay
                    if REQUEST_DELAY_MS > 0:
                        await asyncio.sleep(REQUEST_DELAY_MS / 1000)
                    
            except asyncio.TimeoutError:
                logger.error(f"[UniProt] Timeout for '{term}' (page {page_count})")
                break
            except Exception as e:
                logger.exception(f"[UniProt] Failed for '{term}': {e}")
                break
        
        return results

    async def fetch(self, keyword: str) -> List[str]:
        """
        Fetch gene symbols matching keyword from UniProt.
        
        Args:
            keyword: Search keyword
            
        Returns:
            List of gene symbols
        """
        if not keyword or not keyword.strip():
            logger.warning("[UniProt] Empty keyword provided")
            return []
        
        variants = expand_variants(keyword)
        norm_variants = {normalize(v) for v in variants}
        result: Set[str] = set()
        
        logger.info(f"[UniProt] Searching for '{keyword}' (variants: {variants})")
        
        session = await self._get_session()

        for variant in variants:
            hits = await self._query(session, variant)
            
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                
                genes = hit.get("genes", [])
                if not genes or not isinstance(genes, list):
                    continue
                
                main_gene = genes[0]
                if not isinstance(main_gene, dict):
                    continue
                
                # Extract main gene name
                main = main_gene.get("geneName", {}).get("value", "") if isinstance(main_gene.get("geneName"), dict) else ""
                
                # Extract alternative names
                alts = []
                for field in ["synonyms", "orderedLocusNames", "orfNames"]:
                    field_data = main_gene.get(field, [])
                    if isinstance(field_data, list):
                        alts.extend(
                            item.get("value", "")
                            for item in field_data
                            if isinstance(item, dict)
                        )
                
                # Extract protein description
                prot_desc = hit.get("proteinDescription", {})
                if isinstance(prot_desc, dict):
                    rec_name = prot_desc.get("recommendedName", {})
                    if isinstance(rec_name, dict):
                        full_name = rec_name.get("fullName", {})
                        if isinstance(full_name, dict):
                            prot = full_name.get("value", "")
                        else:
                            prot = ""
                    else:
                        prot = ""
                else:
                    prot = ""
                
                # Extract protein families
                fams = hit.get("proteinFamilies", [])
                if not isinstance(fams, list):
                    fams = []
                
                # Check if any field matches
                all_fields = [main] + alts + [prot] + fams
                for field in all_fields:
                    if field and normalize(field) in norm_variants:
                        if main:
                            result.add(main)
                        break
        
        result_list = sorted(filter(None, result))
        logger.info(f"[UniProt] Found {len(result_list)} genes for '{keyword}'")
        return result_list


class TargetMemberAggregator:
    """
    Aggregate gene family members from multiple sources.
    """
    
    def __init__(
        self,
        use_hgnc_api: bool = True,
        use_hgnc_local: bool = False,
        hgnc_file_path: Optional[str] = None,
        use_uniprot: bool = False,  # FIX: Actually use this parameter
        use_mygene: bool = False     # FIX: Actually use this parameter
    ):
        self.logger = logger
        self.sources = []
        
        if use_hgnc_api:
            self.sources.append(HGNCAPIGeneFamilyFetcher())
            logger.info("Enabled HGNC API fetcher")
        
        if use_hgnc_local:
            if hgnc_file_path:
                self.sources.append(HGNCLocalGeneFamilyFetcher(hgnc_file_path=hgnc_file_path))
                logger.info(f"Enabled HGNC Local fetcher: {hgnc_file_path}")
            else:
                logger.warning("HGNC Local requested but no file path provided")
        
        # FIX: Actually use the parameters
        if use_uniprot:
            self.sources.append(UniProtFamilyFetcher())
            logger.info("Enabled UniProt fetcher")
        
        if use_mygene:
            self.sources.append(MyGeneFamilyFetcher())
            logger.info("Enabled MyGene fetcher")
        
        if not self.sources:
            logger.warning("No sources enabled in TargetMemberAggregator")
    
    async def close(self):
        """Close all sources."""
        for source in self.sources:
            if hasattr(source, 'close'):
                await source.close()

    async def get_synonyms_by_source(self, family_name: str) -> Dict[str, List[str]]:
        """
        Fetch synonyms from all enabled sources.
        
        Args:
            family_name: Gene family name to search
            
        Returns:
            Dict mapping source name to list of gene symbols
        """
        async def fetch_one(src):
            try:
                name = src.name()
                results = await src.fetch(family_name)
                return name, results
            except Exception as e:
                logger.exception(f"[{src.name()}] Failed for '{family_name}': {e}")
                return src.name(), []

        # FIX: Add overall timeout
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[fetch_one(src) for src in self.sources]),
                timeout=FETCH_TIMEOUT_SEC
            )
            return dict(results)
        except asyncio.TimeoutError:
            logger.error(
                f"Overall fetch timeout ({FETCH_TIMEOUT_SEC}s) for '{family_name}'"
            )
            return {src.name(): [] for src in self.sources}

    async def get_all_synonyms(self, family_name: str) -> Dict[str, object]:
        """
        Get all synonyms for a gene family from all sources.
        
        Args:
            family_name: Gene family name to search
            
        Returns:
            Dict with 'combined_synonyms' (list) and 'synonyms_by_source' (dict)
        """
        if not family_name or not family_name.strip():
            logger.warning("Empty family_name provided to get_all_synonyms")
            return {
                "combined_synonyms": [],
                "synonyms_by_source": {}
            }
        
        logger.info(f"Fetching synonyms for '{family_name}' from {len(self.sources)} sources")
        
        by_source = await self.get_synonyms_by_source(family_name)
        
        # Combine and normalize
        combined = sorted(set(
            x.lower()
            for synonyms in by_source.values()
            for x in synonyms
            if x  # Filter out empty strings
        ))
        
        logger.info(
            f"Found {len(combined)} unique synonyms for '{family_name}' "
            f"from {sum(len(v) for v in by_source.values())} total results"
        )
        
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": by_source
        }