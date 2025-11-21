import asyncio
import aiohttp
import mygene
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class UniProtGeneFetcher:
    @staticmethod
    async def fetch(gene_symbol: str, organism_id: int | None = 9606, timeout_sec: int = 12) -> List[str]:
        base_url = "https://rest.uniprot.org/uniprotkb/search"
        query = f"gene_exact:{gene_symbol}"
        if organism_id is not None:
            query += f" AND organism_id:{organism_id}"

        params = {
            "query": query,
            "format": "json",
            "fields": "gene_names",
            "size": 100
        }

        synonyms = set()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
                async with session.get(base_url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"[UniProt] HTTP {resp.status} for {gene_symbol}")
                        return []
                    data = await resp.json()
                    for entry in data.get("results", []):
                        for gene in entry.get("genes", []):
                            primary = (gene.get("geneName") or {}).get("value")
                            if primary:
                                synonyms.add(primary)
                            for syn in gene.get("synonyms", []) or []:
                                val = syn.get("value")
                                if val:
                                    synonyms.add(val)
            logger.info(f"[UniProt] Found {len(synonyms)} synonyms for {gene_symbol}")
        except Exception as e:
            logger.exception(f"[UniProt] Failed for {gene_symbol}: {e}")
        return sorted({s.strip() for s in synonyms if s and s.strip()})


class HGNCGeneFetcher:
    @staticmethod
    async def fetch(gene_symbol: str, timeout_sec: int = 12) -> List[str]:
        url = f"https://rest.genenames.org/fetch/symbol/{gene_symbol}"
        headers = {"Accept": "application/json"}

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"[HGNC] HTTP {resp.status} for {gene_symbol}")
                        return []
                    data = await resp.json()
        except Exception as e:
            logger.exception(f"[HGNC] Failed for {gene_symbol}: {e}")
            return []

        docs = (data.get("response") or {}).get("docs") or []
        if not docs:
            return []

        doc = docs[0]
        synonyms = set()
        for field in ["alias_symbol", "alias_name", "prev_symbol"]:
            values = doc.get(field) or []
            if isinstance(values, list):
                synonyms.update(values)
            elif isinstance(values, str):
                synonyms.add(values)

        symbol = doc.get("symbol")
        if symbol:
            synonyms.add(symbol)

        logger.info(f"[HGNC] Found {len(synonyms)} synonyms for {gene_symbol}")
        return sorted({s.strip() for s in synonyms if s and isinstance(s, str)})


class MyGeneInfoFetcher:
    @staticmethod
    async def fetch(gene_symbol: str, species: str | int = "human") -> List[str]:
        def _query() -> List[str]:
            mg = mygene.MyGeneInfo()
            try:
                result = mg.query(
                    gene_symbol,
                    species=species,
                    fields="symbol,name,alias,other_names",
                    size=1
                )
                hits = result.get("hits") or []
                if not hits:
                    return []
                hit = hits[0]
                synonyms = set()
                for key in ["symbol", "name", "alias", "other_names"]:
                    val = hit.get(key)
                    if isinstance(val, list):
                        synonyms.update(val)
                    elif isinstance(val, str):
                        synonyms.add(val)
                return sorted({s.strip() for s in synonyms if s})
            except Exception as e:
                logger.exception(f"[MyGene.info] Failed for {gene_symbol}: {e}")
                return []

        return await asyncio.to_thread(_query)


class GeneSynonymAggregator:
    """
    Aggregates gene synonyms from UniProt, HGNC, and MyGene.info
    """

    def __init__(self):
        self.sources = {
            "UniProt": UniProtGeneFetcher,
            "HGNC": HGNCGeneFetcher,
            "MyGene": MyGeneInfoFetcher
        }

    async def get_all_synonyms(self, gene_symbol: str,
                                organism_id: int | None = 9606,
                                mygene_species: str | int = "human") -> Dict[str, List[str]]:
        """
        Fetch gene synonyms from all available sources concurrently.
        """
        uniprot_task = self.sources["UniProt"].fetch(gene_symbol, organism_id=organism_id)
        hgnc_task = self.sources["HGNC"].fetch(gene_symbol)
        mygene_task = self.sources["MyGene"].fetch(gene_symbol, species=mygene_species)

        uniprot_syns, hgnc_syns, mygene_syns = await asyncio.gather(uniprot_task, hgnc_task, mygene_task)

        synonyms_by_source = {
            "UniProt": uniprot_syns,
            "HGNC": hgnc_syns,
            "MyGene": mygene_syns
        }

        combined = sorted(set().union(*synonyms_by_source.values()))
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": synonyms_by_source,
            "official_symbol": gene_symbol
        }

    async def get_synonyms_by_source(self, gene_symbol: str, source: str) -> List[str]:
        """
        Fetch from a single source
        """
        fetcher = self.sources.get(source)
        if not fetcher:
            logger.warning(f"[GeneSynonymAggregator] Invalid source: {source}")
            return []

        if asyncio.iscoroutinefunction(fetcher.fetch):
            return await fetcher.fetch(gene_symbol)
        else:
            return fetcher.fetch(gene_symbol)
