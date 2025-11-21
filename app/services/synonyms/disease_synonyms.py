

import asyncio
import logging
import httpx
import requests
from owlready2 import get_ontology
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)
ONTO = get_ontology("http://purl.obolibrary.org/obo/doid.owl").load()


# -------------------- Source 1: NLM Clinical Table --------------------

class NLMDiseaseFetcher:
    @staticmethod
    async def fetch(disease_name: str) -> List[str]:
        url = "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search"
        params = {
            "terms": disease_name,
            "sf": "synonyms,word_synonyms,primary_name",
            "ef": "synonyms,word_synonyms,primary_name",
            "maxList": 10000
        }
        synonyms_set = set()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

                if data and len(data) > 3 and data[3]:
                    for entry in data[3]:
                        if len(entry) > 0 and entry[0]:
                            synonyms_set.add(entry[0])
                        if len(entry) > 1 and entry[1]:
                            synonyms_set.update(entry[1] if isinstance(entry[1], list) else [entry[1]])
                        if len(entry) > 2 and entry[2]:
                            synonyms_set.update(entry[2] if isinstance(entry[2], list) else [entry[2]])
                synonyms_set.add(disease_name)
        except Exception as e:
            logger.exception(f"[NLM] Failed for '{disease_name}': {e}")
        return sorted(synonyms_set)


# -------------------- Source 2: OBO Ontology --------------------

class OboDiseaseFetcher:
    @staticmethod
    async def fetch(disease_name: str) -> List[str]:
        try:
            term = ONTO.search_one(label=disease_name)
            if not term:
                return []
            descs = term.descendants()
            descs.discard(term)
            names = sorted(set(d.label[0] for d in descs if getattr(d, "label", None)))
            return names
        except Exception as e:
            logger.exception(f"[OBO] Failed for '{disease_name}': {e}")
            return []


# -------------------- Source 3: OpenTargets GraphQL --------------------

class OpenTargetsDiseaseFetcher:
    API_URL = "https://api.platform.opentargets.org/api/v4/graphql"

    def run_graphql(self, query: str, variables: Dict) -> Dict:
        try:
            resp = requests.post(self.API_URL, json={"query": query, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(data["errors"])
            return data["data"]
        except Exception as e:
            logger.exception(f"[OpenTargets] GraphQL error: {e}")
            return {}

    def find_efo_id(self, name: str) -> Optional[str]:
        query = """
        query Search($q: String!) {
            search(queryString: $q, entityNames: ["disease"], page: {index: 0, size: 1}) {
                hits { id name }
            }
        }
        """
        try:
            hits = self.run_graphql(query, {"q": name})["search"]["hits"]
            return hits[0]["id"] if hits else None
        except Exception:
            return None

    def get_official_disease_name(self, name: str, n: int = 5, min_score: float = 500.0) -> Optional[str]:
        q_template = f"""
        query Search($q: String!) {{
            search(queryString: $q, entityNames: ["disease"], page: {{ index: 0, size: {n} }}) {{
                hits {{ id name entity score }}
            }}
        }}
        """
        suffixes = [
            "", " disease", " diseases", "s", " illness",
            " disorder", " condition", " ailment", " syndrome"
        ]
        candidates = set()
        checked_variants = set()

        forms = {name, name.rstrip("s")}
        variants = []
        for form in forms:
            for suffix in suffixes:
                variant = (form + suffix).strip()
                if variant not in checked_variants:
                    checked_variants.add(variant)
                    variants.append(variant)

        def query_variant(variant: str):
            try:
                hits = self.run_graphql(q_template, {"q": variant})["search"]["hits"]
                return [h for h in hits if h.get("entity") == "disease" and h.get("score", 0) >= min_score]
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(query_variant, v): v for v in variants}
            for future in as_completed(futures):
                for hit in future.result():
                    candidates.add(tuple(hit.items()))

        candidates = [dict(h) for h in candidates]
        if not candidates:
            return None
        best = max(candidates, key=lambda h: h["score"])
        return best.get("name")

    def fetch_synonyms(self, efo_id: str) -> List[str]:
        query = """
        query GetDiseaseSynonyms($efoId: String!) {
            disease(efoId: $efoId) {
                name
                synonyms {
                    terms
                }
            }
        }
        """
        data = self.run_graphql(query, {"efoId": efo_id}).get("disease", {})
        names = set()
        if data.get("name"):
            names.add(data["name"])
        for syn in data.get("synonyms", []):
            names.update(syn.get("terms", []))
        return sorted(names)

    def fetch_descendants(self, efo_id: str) -> List[str]:
        query = """
        query Descendants($id: String!) {
            disease(efoId: $id) {
                descendants
            }
        }
        """
        ids = self.run_graphql(query, {"id": efo_id}).get("disease", {}).get("descendants", [])
        return self.fetch_names_for_ids(ids).values()

    def fetch_names_for_ids(self, efo_ids: List[str]) -> Dict[str, str]:
        query = """
        query Names($ids: [String!]!) {
            diseases(efoIds: $ids) {
                id
                name
            }
        }
        """
        data = self.run_graphql(query, {"ids": efo_ids}).get("diseases", [])
        return {d["id"]: d["name"] for d in data}

    def fetch_all(self, disease_name: str) -> List[str]:
        efo_id = self.find_efo_id(disease_name)
        if not efo_id:
            return []
        synonyms = self.fetch_synonyms(efo_id)
        descendants = self.fetch_descendants(efo_id)
        return sorted(set(synonyms + list(descendants)))


# -------------------- Aggregator --------------------

class DiseaseSynonymAggregator:
    def __init__(self):
        self.sources = {
            "NLM": NLMDiseaseFetcher,
            "OBO": OboDiseaseFetcher,
            "OpenTargets": OpenTargetsDiseaseFetcher()
        }

    async def get_all_synonyms(self, disease_name: str) -> Dict[str, List[str]]:
        nlm_task = self.sources["NLM"].fetch(disease_name)
        obo_task = self.sources["OBO"].fetch(disease_name)
        open_target_syns = self.sources["OpenTargets"].fetch_all(disease_name)

        nlm_syns, obo_syns = await asyncio.gather(nlm_task, obo_task)

        synonyms_by_source = {
            "NLM": nlm_syns,
            "OBO": obo_syns,
            "OpenTargets": open_target_syns,
        }

        combined = sorted(set().union(*synonyms_by_source.values()))
        return {
            "combined_synonyms": combined,
            "synonyms_by_source": synonyms_by_source,
            "official_name": disease_name
        }

    async def get_synonyms_by_source(self, disease_name: str, source: str) -> List[str]:
        src = self.sources.get(source)
        if not src:
            logger.warning(f"Unknown disease synonym source: {source}")
            return []

        if isinstance(src, OpenTargetsDiseaseFetcher):
            return src.fetch_all(disease_name)
        return await src.fetch(disease_name)
