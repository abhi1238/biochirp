import logging
import re
import aiohttp
import asyncio
from typing import List, Dict

logger = logging.getLogger("TargetMemberFetcher")
logger.setLevel(logging.INFO)

def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower()) if text else ""

def expand_variants(keyword: str) -> set:
    return {keyword, keyword.rstrip("s")} if keyword.endswith("s") else {keyword, keyword + "s"}

class HGNCAPIGeneFamilyFetcher:
    BASE_URL = "https://rest.genenames.org/search/"
    HEADERS = {'Accept': 'application/json'}

    def __init__(self, rows_per_page: int = 100):
        self.rows_per_page = rows_per_page

    @staticmethod
    def name() -> str:
        return "HGNC"

    async def fetch(self, family_name: str) -> List[str]:
        all_genes, start = [], 0
        variants = expand_variants(family_name)
        async with aiohttp.ClientSession() as session:
            for v in variants:
                query = f'family_name:"{v}"'
                while True:
                    url = f"{self.BASE_URL}{query}&start={start}&rows={self.rows_per_page}"
                    try:
                        async with session.get(url, headers=self.HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status != 200:
                                logger.warning(f"[HGNC-API] HTTP {resp.status} for '{v}'")
                                break
                            data = await resp.json()
                            docs = data.get("response", {}).get("docs", [])
                            if not docs:
                                break
                            all_genes.extend(doc['symbol'] for doc in docs if 'symbol' in doc)
                            start += self.rows_per_page
                    except Exception as e:
                        logger.error(f"[HGNC-API] Request failed: {e}")
                        break
        return sorted(set(all_genes))


class HGNCLocalGeneFamilyFetcher:
    def __init__(self, hgnc_file_path: str):
        try:
            self.hgnc_data = pd.read_csv(hgnc_file_path, sep="\t", on_bad_lines="skip", dtype=str)
            logger.info(f"[HGNC-Local] Loaded {len(self.hgnc_data)} rows")
        except Exception as e:
            self.hgnc_data = None
            logger.error(f"[HGNC-Local] Failed to load file: {e}")

    @staticmethod
    def name() -> str:
        return "HGNC"

    async def fetch(self, family_keyword: str) -> List[str]:
        if self.hgnc_data is None:
            return []
        try:
            variants = expand_variants(family_keyword)
            mask = self.hgnc_data['gene_family'].fillna('').apply(lambda x: any(v.lower() in x.lower() for v in variants))
            return sorted(self.hgnc_data.loc[mask, 'symbol'].dropna().unique())
        except Exception as e:
            logger.error(f"[HGNC-Local] Search failed: {e}")
            return []


class MyGeneFamilyFetcher:
    BASE_URL = "https://mygene.info/v3/query"

    def __init__(self, species: str = "human", size: int = 1000):
        self.species = species
        self.size = size

    @staticmethod
    def name() -> str:
        return "MyGene"

    async def fetch(self, keyword: str) -> List[str]:
        variants = expand_variants(keyword)
        norm_variants = {normalize(v) for v in variants}
        result = []

        async with aiohttp.ClientSession() as session:
            for variant in variants:
                offset = 0
                while True:
                    try:
                        params = {
                            "q": variant,
                            "species": self.species,
                            "fields": "symbol,name,alias",
                            "size": self.size,
                            "from": offset
                        }
                        async with session.get(self.BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status != 200:
                                break
                            data = await resp.json()
                            hits = data.get("hits", [])
                            if not hits:
                                break
                            for hit in hits:
                                aliases = hit.get("alias", [])
                                if isinstance(aliases, str):
                                    aliases = [aliases]
                                fields = [hit.get("symbol", ""), hit.get("name", "")] + aliases
                                if any(normalize(f) in norm_variants for f in fields):
                                    result.append(hit.get("symbol", ""))
                            offset += self.size
                    except Exception as e:
                        logger.error(f"[MyGene] Failed for '{variant}': {e}")
                        break
        return sorted(set(filter(None, result)))


class UniProtFamilyFetcher:
    BASE_URL = "https://rest.uniprot.org/uniprotkb/search"

    def __init__(self, size: int = 500):
        self.size = min(size, 500)

    @staticmethod
    def name() -> str:
        return "UniProt"

    async def fetch(self, keyword: str) -> List[str]:
        async def query(session, term: str) -> List[dict]:
            results, cursor = [], None
            while True:
                params = {
                    "query": f'"{term}" AND organism_id:9606',
                    "fields": "accession,gene_names,protein_name,protein_families",
                    "format": "json", "size": self.size
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    async with session.get(self.BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                        results.extend(data.get("results", []))
                        cursor = data.get("nextCursor")
                        if not cursor:
                            break
                except Exception as e:
                    logger.error(f"[UniProt] Failed for '{term}': {e}")
                    break
            return results

        variants = expand_variants(keyword)
        norm_variants = {normalize(v) for v in variants}
        result = []

        async with aiohttp.ClientSession() as session:
            for v in variants:
                hits = await query(session, v)
                for hit in hits:
                    genes = hit.get("genes", [])
                    main = genes[0].get("geneName", {}).get("value", "") if genes else ""
                    alts = [g.get("value", "") for t in ["synonyms", "orderedLocusNames", "orfNames"] for g in genes[0].get(t, [])] if genes else []
                    prot = hit.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                    fams = hit.get("proteinFamilies", [])
                    for f in [main] + alts + [prot] + fams:
                        if normalize(f) in norm_variants:
                            result.append(main)
                            break
        return sorted(set(filter(None, result)))


class TargetMemberAggregator:
    def __init__(self, use_hgnc_api=True, use_hgnc_local=False, hgnc_file_path=None, use_uniprot=True, use_mygene=True):
        self.logger = logger
        self.sources = []
        if use_hgnc_api:
            self.sources.append(HGNCAPIGeneFamilyFetcher())
        if use_hgnc_local and hgnc_file_path:
            self.sources.append(HGNCLocalGeneFamilyFetcher(hgnc_file_path=hgnc_file_path))

    async def get_synonyms_by_source(self, family_name: str) -> Dict[str, List[str]]:
        async def fetch_one(src):
            try:
                return src.name(), await src.fetch(family_name)
            except Exception as e:
                logger.error(f"[{src.name()}] failed: {e}")
                return src.name(), []

        results = await asyncio.gather(*[fetch_one(src) for src in self.sources])
        return dict(results)

    async def get_all_synonyms(self, family_name: str) -> Dict[str, object]:
        by_source = await self.get_synonyms_by_source(family_name)
        combined = sorted(set(x.lower() for v in by_source.values() for x in v))
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": by_source
        }
