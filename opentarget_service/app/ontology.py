from typing import Dict, Set, Any, List, Optional
import asyncio
import os
import time
from .client import OTGraphQLClient
from .config import OTClientConfig
import pandas as pd

import logging
# =========================================================
# Logging
# =========================================================
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.ontology")


_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)

_CACHE_TTL_S = int(os.getenv("OT_ONTOLOGY_CACHE_TTL_S", "900"))
_CACHE: Dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = asyncio.Lock()


def _cache_key(prefix: str, term: str) -> str:
    return f"{prefix}:{(term or '').strip().lower()}"


async def _cache_get(key: str) -> Optional[Any]:
    if _CACHE_TTL_S <= 0:
        return None
    async with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            _CACHE.pop(key, None)
            return None
        return value


async def _cache_set(key: str, value: Any) -> None:
    if _CACHE_TTL_S <= 0:
        return
    async with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + _CACHE_TTL_S, value)


async def get_disease_and_descendant_synonyms(disease_name: str):
    cache_key = _cache_key("disease_desc_syn", disease_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    queries = {
        "search": """
        query Search($q: String!) {
          search(
            queryString: $q,
            entityNames: ["disease"],
            page: { index: 0, size: 1 }
          ) {
            hits { id name }
          }
        }
        """,
        "descendants": """
        query GetDesc($id: String!) {
          disease(efoId: $id) {
            descendants
          }
        }
        """,
        "disease_synonyms": """
        query GetDiseaseSynonyms($efoId: String!) {
          disease(efoId: $efoId) {
            name
            synonyms { terms }
          }
        }
        """,
        "metadata": """
        query GetMeta($ids: [String!]!) {
          diseases(efoIds: $ids) {
            name
            synonyms { terms }
          }
        }
        """,
    }

    try:
        search_res = await _ot.run(queries["search"], {"q": disease_name})
        hits = search_res.get("search", {}).get("hits", [])
        if not hits:
            raise ValueError(f"No disease found for {disease_name}")

        root_id = hits[0]["id"]

        disease_res, desc_res = await asyncio.gather(
            _ot.run(queries["disease_synonyms"], {"efoId": root_id}),
            _ot.run(queries["descendants"], {"id": root_id}),
        )

        disease_info = disease_res.get("disease") or {}
        root_synonyms: Set[str] = set()

        if disease_info.get("name"):
            root_synonyms.add(disease_info["name"])

        for group in disease_info.get("synonyms", []) or []:
            root_synonyms.update(group.get("terms", []))

        desc_ids = (
            (desc_res.get("disease") or {}).get("descendants", [])
        ) or []

        if not desc_ids:
            result = {
                "synonyms": sorted(root_synonyms),
                "descendants": [],
                "combined": sorted(root_synonyms),
            }
            await _cache_set(cache_key, result)
            return result

        desc_synonyms: Set[str] = set()
        chunk_size = 500
        sem = asyncio.Semaphore(max(_cfg.metadata_concurrency, 1))

        async def fetch_chunk(chunk: List[str]) -> Dict[str, Any]:
            async with sem:
                return await _ot.run(queries["metadata"], {"ids": chunk})

        tasks = [
            fetch_chunk(desc_ids[i : i + chunk_size])
            for i in range(0, len(desc_ids), chunk_size)
        ]
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                logger.warning(
                    "[ontology] metadata fetch failed for %s: %s",
                    disease_name,
                    res,
                )
                continue
            diseases = res.get("diseases", []) or []
            for d in diseases:
                if not d:
                    continue
                if d.get("name"):
                    desc_synonyms.add(d["name"])
                for group in d.get("synonyms", []) or []:
                    desc_synonyms.update(group.get("terms", []))

        combined = sorted(root_synonyms | desc_synonyms)
        result = {
            "synonyms": sorted(root_synonyms),
            "descendants": sorted(desc_synonyms),
            "combined": combined,
        }
        await _cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.exception("[ontology] disease synonyms failed: %s", e)
        raise RuntimeError(f"OpenTargets failed for {disease_name!r}") from e




async def get_drug_info(drug_query: str) -> Dict[str, Any]:

    # 1. Search for the Drug ID
    search_query = """
    query SearchDrug($q: String!) {
      search(queryString: $q, entityNames: ["drug"], page: {index: 0, size: 1}) {
        hits {
          id
          name
        }
      }
    }
    """

    # 2. Get specific Drug details
    drug_details_query = """
    query GetDrugDetails($drugId: String!) {
      drug(chemblId: $drugId) {
        id
        name
        synonyms
        tradeNames
      }
    }
    """

    try:
        # Step 1: Search for name -> ID
        search_res = await _ot.run(search_query, {"q": drug_query})
        hits = search_res.get("search", {}).get("hits", [])
        if not hits:
            return {"error": f"Drug matching '{drug_query}' not found"}

        drug_id = hits[0]['id']
        
        # Step 2: Fetch detailed properties
        details_res = await _ot.run(drug_details_query, {"drugId": drug_id})
        drug_info = details_res.get("drug", {})

        if not drug_info:
            return {"error": "Drug details could not be retrieved from the database."}

        # Data Cleaning
        trade_names = drug_info.get("tradeNames") or []
        synonyms = drug_info.get("synonyms") or []
        
        # Combined Parameter: Merges both lists and removes duplicates
        combined = sorted(list(set(trade_names + synonyms)))


        logger.info("[ontology] Total entries found for %s: %d", drug_query, len(combined))

        return {
            "name": drug_info.get("name"),
            "id": drug_info.get("id"),
            "trade_names": sorted(list(set(trade_names))),
            "synonyms": sorted(list(set(synonyms))),
            "combined": combined
        }

    except Exception as e:
        return {"error": str(e)}






async def _get_target_synonyms_legacy(target_query: str) -> Dict[str, Any]:
    """Get target/gene synonyms and alternative names.
    
    Args:
        target_query: Target gene symbol or Ensembl ID
        
    Returns:
        Dictionary with keys:
        - name: Approved symbol
        - id: Ensembl ID
        - approved_name: Full approved name
        - synonyms: List of gene synonyms
        - symbol_synonyms: Alternative gene symbols
        - combined: Union of all names
        
    Raises:
        RuntimeError: If API calls fail
    """
    search_query = """
    query SearchTarget($q: String!) {
      search(queryString: $q, entityNames: ["target"], page: {index: 0, size: 1}) {
        hits {
          id
          name
        }
      }
    }
    """

    target_details_query = """
    query GetTargetDetails($targetId: String!) {
      target(ensemblId: $targetId) {
        id
        approvedSymbol
        approvedName
        biotype
        symbolSynonyms
        nameSynonyms
      }
    }
    """

    try:
        # Step 1: Search for target ID
        logger.debug(f"Searching for target: {target_query}")
        search_res = await _ot.run(search_query, {"q": target_query})
        
        hits = search_res.get("search", {}).get("hits", [])
        if not hits:
            logger.warning(f"Target not found: {target_query}")
            return {
                "error": f"Target matching '{target_query}' not found",
                "name": None,
                "id": None,
                "approved_name": None,
                "synonyms": [],
                "symbol_synonyms": [],
                "combined": []
            }

        target_id = hits[0]['id']
        logger.info(f"Resolved {target_query} → {target_id}")
        
        # Step 2: Get target details
        details_res = await _ot.run(target_details_query, {"targetId": target_id})
        
        target_info = details_res.get("target", {})

        if not target_info:
            logger.error("Target details could not be retrieved")
            return {
                "error": "Target details could not be retrieved from the database.",
                "name": None,
                "id": target_id,
                "approved_name": None,
                "synonyms": [],
                "symbol_synonyms": [],
                "combined": []
            }

        # Extract and clean data
        approved_symbol = target_info.get("approvedSymbol")
        approved_name = target_info.get("approvedName")
        symbol_synonyms = target_info.get("symbolSynonyms") or []
        name_synonyms = target_info.get("nameSynonyms") or []
        
        # Combine all names
        all_names: Set[str] = set()
        if approved_symbol:
            all_names.add(approved_symbol)
        if approved_name:
            all_names.add(approved_name)
        all_names.update(symbol_synonyms)
        all_names.update(name_synonyms)
        
        combined = sorted(list(all_names))

        logger.info(f"Found {len(combined)} total synonyms for {target_query}")

        return {
            "name": approved_symbol,
            "id": target_info.get("id"),
            "approved_name": approved_name,
            "synonyms": sorted(list(set(name_synonyms))),
            "symbol_synonyms": sorted(list(set(symbol_synonyms))),
            "combined": combined
        }

    except Exception as e:
        logger.exception(f"Failed to get target info for {target_query}")
        return {
            "error": str(e), 
            "name": None, 
            "id": None, 
            "approved_name": None,
            "synonyms": [], 
            "symbol_synonyms": [],
            "combined": []
        }




async def get_target_description(target_name: str) -> Optional[str]:
    """Fetch the description of a target by its name (symbol or specific name)."""
    cache_key = _cache_key("target_desc", target_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        target_name = (target_name or "").strip()
        if not target_name:
            return None

        search_query = """
        query SearchTarget($queryString: String!) {
          search(queryString: $queryString, entityNames: ["target"]) {
            hits {
              id
              name
              object {
                ... on Target {
                  approvedSymbol
                }
              }
            }
          }
        }
        """

        data = await _ot.run(search_query, {"queryString": target_name})
        hits = data.get("search", {}).get("hits", [])
        if not hits:
            return None

        best_hit = None
        for hit in hits:
            obj = hit.get("object") or {}
            symbol = obj.get("approvedSymbol", "").upper()
            if symbol == target_name.upper():
                best_hit = hit
                break

        if not best_hit:
            best_hit = hits[0]

        target_id = best_hit["id"]

        description_query = """
        query TargetDesc($ensemblId: String!) {
          target(ensemblId: $ensemblId) {
            functionDescriptions
          }
        }
        """

        target_data = (await _ot.run(description_query, {"ensemblId": target_id})).get("target")
        if not target_data:
            return None

        descriptions = target_data.get("functionDescriptions") or []
        description = descriptions[0] if descriptions else None
        if description:
            await _cache_set(cache_key, description)
        return description
    except Exception as e:
        logger.warning("[ontology] target description failed for %s: %s", target_name, e)
        return None



async def get_drug_description(drug_name: str) -> Optional[str]:
    """Fetch the description of a drug by its name."""
    cache_key = _cache_key("drug_desc", drug_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        drug_name = (drug_name or "").strip()
        if not drug_name:
            return None

        search_query = """
        query SearchDrug($queryString: String!) {
          search(queryString: $queryString, entityNames: ["drug"]) {
            hits {
              id
              name
              object {
                ... on Drug {
                  name
                }
              }
            }
          }
        }
        """

        data = await _ot.run(search_query, {"queryString": drug_name})
        hits = data.get("search", {}).get("hits", [])
        if not hits:
            return None

        best_hit = next((h for h in hits if h["name"].upper() == drug_name.upper()), None)
        if not best_hit:
            best_hit = hits[0]

        chembl_id = best_hit["id"]

        description_query = """
        query DrugDesc($chemblId: String!) {
          drug(chemblId: $chemblId) {
            name
            description
            drugType
            maximumClinicalTrialPhase
          }
        }
        """

        drug_data = (await _ot.run(description_query, {"chemblId": chembl_id})).get("drug")
        if not drug_data:
            return None

        desc = drug_data.get("description")
        if desc:
            await _cache_set(cache_key, desc)
        return desc
    except Exception as e:
        logger.warning("[ontology] drug description failed for %s: %s", drug_name, e)
        return None




async def get_disease_description(disease_name: str) -> Optional[str]:
    """Fetch the description of a disease/phenotype by its name."""
    cache_key = _cache_key("disease_desc", disease_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        disease_name = (disease_name or "").strip()
        if not disease_name:
            return None

        search_query = """
        query SearchDisease($queryString: String!) {
          search(queryString: $queryString, entityNames: ["disease"]) {
            hits {
              id
              name
              object {
                ... on Disease {
                  name
                }
              }
            }
          }
        }
        """

        data = await _ot.run(search_query, {"queryString": disease_name})
        hits = data.get("search", {}).get("hits", [])
        if not hits:
            return None

        best_hit = next((h for h in hits if h["name"].upper() == disease_name.upper()), None)
        if not best_hit:
            best_hit = hits[0]

        disease_id = best_hit["id"]

        description_query = """
        query DiseaseDesc($efoId: String!) {
          disease(efoId: $efoId) {
            name
            description
            therapeuticAreas {
              name
            }
          }
        }
        """

        disease_data = (await _ot.run(description_query, {"efoId": disease_id})).get("disease")
        if not disease_data:
            return None

        desc = disease_data.get("description")
        if desc:
            await _cache_set(cache_key, desc)
        return desc
    except Exception as e:
        logger.warning("[ontology] disease description failed for %s: %s", disease_name, e)
        return None





async def get_drug_synonyms(drug_name: str) -> List[str]:
    """Given a drug name, return drug synonyms from Open Targets."""
    cache_key = _cache_key("drug_syn", drug_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    query = """
    query getDrugSynonyms($name: String!) {
      search(queryString: $name, entityNames: ["drug"], page: { index: 0, size: 1 }) {
        hits {
          object {
            ... on Drug {
              name
              synonyms
            }
          }
        }
      }
    }
    """
    data = await _ot.run(query, {"name": drug_name})
    hits = data.get("search", {}).get("hits", [])
    if not hits:
        return []

    synonyms = hits[0].get("object", {}).get("synonyms") or []
    await _cache_set(cache_key, synonyms)
    return synonyms



async def _get_disease_synonyms_legacy(disease_name: str) -> List[str]:
    """
    Given a disease name, return disease synonyms.
    Process: Search -> Get EFO ID -> Fetch Synonyms.
    """
    # --- Step 1: Search to get ID ---
    search_query = """
    query Search($name: String!) {
      search(queryString: $name, entityNames: ["disease"], page: {index: 0, size: 1}) {
        hits {
          id
          name
        }
      }
    }
    """
    
    data = await _ot.run(search_query, {"name": disease_name})
    hits = data.get("search", {}).get("hits", [])
    
    if not hits:
        return []
        
    disease_id = hits[0]["id"]

    # --- Step 2: Fetch Synonyms using ID ---
    details_query = """
    query DiseaseSynonyms($id: String!) {
      disease(efoId: $id) {
        synonyms {
          terms
        }
      }
    }
    """
    
    disease_data = (await _ot.run(details_query, {"id": disease_id})).get("disease", {})
    
    if not disease_data:
        return []

    # 'synonyms' is a list of objects: [{'terms': ['Syn A', 'Syn B']}, {'terms': ['Syn C']}]
    raw_synonyms = disease_data.get("synonyms") or []
    
    flattened_synonyms = []
    for entry in raw_synonyms:
        if "terms" in entry:
            flattened_synonyms.extend(entry["terms"])
            
    return flattened_synonyms


async def get_target_synonyms(target_name: str) -> List[str]:
    """Given a target (gene) name or symbol, return synonyms."""
    cache_key = _cache_key("target_syn", target_name)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    target_name = (target_name or "").strip()
    if not target_name:
        return []

    search_query = """
    query Search($name: String!) {
      search(queryString: $name, entityNames: ["target"], page: {index: 0, size: 5}) {
        hits {
          id
          object {
            ... on Target {
              approvedSymbol
            }
          }
        }
      }
    }
    """

    data = await _ot.run(search_query, {"name": target_name})
    hits = data.get("search", {}).get("hits", [])
    if not hits:
        return []

    target_id = None
    for hit in hits:
        obj = hit.get("object") or {}
        sym = obj.get("approvedSymbol", "")
        if sym.upper() == target_name.upper():
            target_id = hit["id"]
            break

    if not target_id:
        target_id = hits[0]["id"]

    details_query = """
    query TargetSynonyms($id: String!) {
      target(ensemblId: $id) {
        approvedSymbol
        synonyms { label }
        obsoleteSymbols { label }
      }
    }
    """

    target_data = (await _ot.run(details_query, {"id": target_id})).get("target") or {}
    if not target_data:
        return []

    syn_objs = target_data.get("synonyms") or []
    synonyms = [item["label"] for item in syn_objs if "label" in item]

    obs_objs = target_data.get("obsoleteSymbols") or []
    obsolete = [item["label"] for item in obs_objs if "label" in item]

    combined = sorted(set(synonyms + obsolete))
    await _cache_set(cache_key, combined)
    return combined




async def get_gene_pathways_df(gene_name: str) -> pd.DataFrame:
    """
    Given a gene name (symbol), return a DataFrame of pathways
    associated with that gene using Open Targets.

    Columns:
    - gene_symbol
    - ensembl_id
    - pathway_id
    - pathway_name
    - top_level_term
    """

    # --------------------------------------------------
    # 1. Resolve gene symbol -> Ensembl ID
    # --------------------------------------------------
    search_query = """
    query SearchTarget($q: String!) {
      search(
        queryString: $q,
        entityNames: ["target"],
        page: { index: 0, size: 1 }
      ) {
        hits {
          id
          object {
            ... on Target {
              approvedSymbol
            }
          }
        }
      }
    }
    """

    data = await _ot.run(search_query, {"q": gene_name})
    hits = data.get("search", {}).get("hits", [])
    if not hits:
        logger.info("[ontology] No gene found for: %s", gene_name)
        return pd.DataFrame(
            columns=[
                "gene_name",
                "gene_id",
                "pathway_id",
                "pathway_name",
                "top_level_term",
            ]
        )

    ensembl_id = hits[0]["id"]
    approved_symbol = (hits[0].get("object") or {}).get("approvedSymbol")

    # --------------------------------------------------
    # 2. Fetch pathways for the resolved gene
    # --------------------------------------------------
    pathway_query = """
    query Pathways($ensemblId: String!) {
      target(ensemblId: $ensemblId) {
        id
        approvedSymbol
        pathways {
          pathwayId
          pathway
          topLevelTerm
        }
      }
    }
    """

    data = await _ot.run(pathway_query, {"ensemblId": ensembl_id})
    pathways = (data.get("target") or {}).get("pathways", []) or []

    # --------------------------------------------------
    # 3. Build DataFrame (set-like uniqueness)
    # --------------------------------------------------
    rows = []
    seen: Set[str] = set()

    for p in pathways:
        pname = p.get("pathway")
        if not pname or pname in seen:
            continue

        seen.add(pname)
        rows.append(
            {
                "gene_name": approved_symbol,
                "gene_id": ensembl_id,
                "pathway_id": p.get("pathwayId"),
                "pathway_name": pname,
                "top_level_term": p.get("topLevelTerm"),
            }
        )

    df = pd.DataFrame(rows)

    return df
