

import logging
import asyncio
import httpx
from typing import List, Dict
# from chembl_webresource_client.new_client import new_client
from drug_named_entity_recognition import find_drugs
logger = logging.getLogger(__name__)


class PubChemFetcher:
    """Fetches drug synonyms from PubChem."""
    @staticmethod
    async def fetch(drug_name: str) -> List[str]:
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{drug_name}/synonyms/JSON"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                info = data.get("InformationList", {}).get("Information", [])
                synonyms = info[0].get("Synonym", []) if info else []
                logger.info(f"[PubChem] Fetched {len(synonyms)} synonyms for '{drug_name}'")
                return synonyms
        except Exception as e:
            logger.exception(f"[PubChem] Failed for '{drug_name}': {e}")
            return []


class ChEMBLRestFetcher:
    """Fetches drug synonyms from ChEMBL REST API."""
    @staticmethod
    async def fetch(drug_name: str) -> List[str]:
        url = f"https://www.ebi.ac.uk/chembl/api/data/molecule?format=json&pref_name__icontains={drug_name}"
        synonyms = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                for compound in data.get("molecules", []):
                    name = compound.get("pref_name", "")
                    if name:
                        synonyms.add(name.strip())
                    for syn in compound.get("molecule_synonyms", []):
                        syn_val = syn.get("synonym", "")
                        if syn_val:
                            synonyms.add(syn_val.strip())
            logger.info(f"[ChEMBL REST] Fetched {len(synonyms)} synonyms for '{drug_name}'")
            return sorted(synonyms)
        except Exception as e:
            logger.exception(f"[ChEMBL REST] Failed for '{drug_name}': {e}")
            return []


# class ChEMBLClientFetcher:
#     """Fetches drug synonyms using ChEMBL Python client."""
#     @staticmethod
#     def fetch(drug_name: str) -> List[str]:
#         synonyms = set()
#         try:
#             molecule_client = new_client.molecule
#             results = molecule_client.filter(pref_name__iexact=drug_name)
#             for mol in results:
#                 if "molecule_synonyms" in mol:
#                     for syn in mol["molecule_synonyms"]:
#                         synonyms.add(syn.get("synonyms", ""))
#                 synonyms.update(filter(None, [mol.get("pref_name"), mol.get("molecule_name")]))
#             synonyms.add(drug_name)
#             logger.info(f"[ChEMBL Client] Fetched {len(synonyms)} synonyms for '{drug_name}'")
#             return sorted(synonyms)
#         except Exception as e:
#             logger.exception(f"[ChEMBL Client] Failed for '{drug_name}': {e}")
#             return []


class DrugNERFetcher:
    """Fetches synonyms using DrugNER extraction."""
    @staticmethod
    def fetch(drug_name: str) -> List[str]:
        synonyms = set()
        try:
            results = find_drugs([drug_name])
            for drug_info, *_ in results:
                name = drug_info.get("name", "")
                if name:
                    synonyms.add(name.strip())
                for syn in drug_info.get("synonyms", []):
                    if isinstance(syn, str) and syn.strip():
                        synonyms.add(syn.strip())
            logger.info(f"[DrugNER] Fetched {len(synonyms)} synonyms for '{drug_name}'")
            return sorted(synonyms)
        except Exception as e:
            logger.exception(f"[DrugNER] Failed for '{drug_name}': {e}")
            return []


class DrugSynonymAggregator:
    """
    Central aggregator to fetch drug synonyms from all available sources.
    """

    def __init__(self):
        self.sources = {
            "PubChem": PubChemFetcher,
            # "ChEMBL_REST": ChEMBLRestFetcher,
            "ChEMBL_Client": ChEMBLClientFetcher,
            "DrugNER": DrugNERFetcher,
        }

    async def get_all_synonyms(self, drug_name: str) -> Dict[str, List[str]]:
        # Async tasks
        pubchem_task = self.sources["PubChem"].fetch(drug_name)
        chembl_rest_task = self.sources["ChEMBL_REST"].fetch(drug_name)
        pubchem_syns, chembl_rest_syns = await asyncio.gather(pubchem_task, chembl_rest_task)

        # Sync tasks
        chembl_client_syns = self.sources["ChEMBL_Client"].fetch(drug_name)
        drugner_syns = self.sources["DrugNER"].fetch(drug_name)

        all_synonyms = {
            "PubChem": pubchem_syns,
            "ChEMBL_REST": chembl_rest_syns,
            "ChEMBL_Client": chembl_client_syns,
            "DrugNER": drugner_syns,
        }

        combined = set()
        for source, syns in all_synonyms.items():
            combined.update(syns)

        return {
            "combined_synonyms": sorted(combined),
            "synonyms_by_source": all_synonyms,
        }
