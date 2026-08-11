import asyncio
import copy
import json
import logging
import os
import re
import sys
from typing import Dict, Any, Optional, Set, List

import httpx

from synonyms.target_family_retriver import TargetMemberAggregator
from synonyms.disease_synonyms import DiseaseSynonymAggregator
from synonyms.drug_synonyms import DrugSynonymAggregator
from synonyms.gene_synonyms import GeneSynonymAggregator
from config.guardrail import Llm_Member_Selector_Input
from utils.concept_values import get_db_concept_values, get_db_alias_map

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# LLM filter configuration (used only for disease_name)
def _build_llm_filter_url() -> str:
    url = os.getenv("LLM_FILTER_URL")
    if url:
        return url
    host = os.getenv("LLM_FILTER_HOST", "biochirp_llm_filter_tool")
    port = os.getenv("LLM_FILTER_PORT", "8017")
    return f"http://{host}:{port}/llm_member_selection_filter"


LLM_FILTER_URL = _build_llm_filter_url()
LLM_FILTER_TIMEOUT = float(os.getenv("LLM_FILTER_TIMEOUT", "30"))
MAX_CONCURRENT_LLM_REQUESTS = int(os.getenv("MAX_CONCURRENT_LLM_REQUESTS", "5"))

# Cache aggregators to avoid recreating them every call
_aggregator_cache: Optional[Dict[str, Any]] = None

# Salt/counterion suffixes that external APIs treat as a distinct compound from the
# free base. When a user queries "imatinib mesylate", PubChem/ChEMBL/OpenTargets
# return the mesylate salt's synonyms but NOT "imatinib" (free base) because they
# are registered as separate chemical entities. Stripping the suffix and expanding
# the base name separately bridges this gap so TTD/HCDT/etc. can match
# drug_name = "Imatinib" against a query for "imatinib mesylate".
_DRUG_SALT_SUFFIXES: frozenset = frozenset({
    "hydrochloride", "hcl", "mesylate", "mesilate", "tosylate",
    "sulfate", "sulphate", "phosphate", "tartrate", "acetate",
    "maleate", "fumarate", "citrate", "succinate", "gluconate",
    "malate", "besylate", "bromide", "chloride",
    "sodium", "potassium", "calcium", "magnesium", "zinc",
    "lysinate", "lysine",
})


def _strip_drug_salt(name: str) -> Optional[str]:
    """Return the free-base name if *name* ends with a known salt suffix, else None.

    "imatinib mesylate" → "imatinib"
    "canagliflozin hydrochloride" → "canagliflozin"
    "imatinib" → None  (no suffix to strip)
    Handles multi-word salts: "erlotinib hydrochloride" → "erlotinib",
    "deferoxamine mesylate" → "deferoxamine".
    """
    parts = name.strip().split()
    if len(parts) >= 2 and parts[-1].lower() in _DRUG_SALT_SUFFIXES:
        return " ".join(parts[:-1])
    return None


def _match_key(value: Any) -> Optional[str]:
    """Canonical key for DB-overlap COMPARISON ONLY (never used as a value).

    Collapses cosmetic surface variation so equivalent names land on one key:
    lowercase -> punctuation/symbols to space -> collapse whitespace -> sort tokens.
    This rescues word-order / punctuation / spacing variants, e.g.
    'tuberculosis, pulmonary' and 'Pulmonary tuberculosis' both -> 'pulmonary tuberculosis',
    and 'Non-Small Cell Lung Cancer' -> 'cancer cell lung non small'.

    It does NOT bridge a different token SET (a missing/extra word, e.g.
    'Mycobacterium tuberculosis infection' vs 'Mycobacterium infection') — that is
    left to the fuzzy/semantic services. Deliberately conservative: NO
    stemming/singularization, which would corrupt names like 'diabetes'/'herpes'.
    """
    norm = safe_normalize(value)
    if norm is None:
        return None
    toks = re.sub(r"[^a-z0-9]+", " ", norm).split()
    return " ".join(sorted(toks)) if toks else None


_EXACT_MATCH_NAMES_FILE = "exact_match_disease_names.json"
_protected_keys_cache: Optional[Set[str]] = None


def _load_protected_keys() -> Set[str]:
    """safe_normalize()'d names that MUST use exact (case-insensitive) matching in the
    DB-overlap filter, bypassing the order/punctuation-insensitive _match_key.

    These are names where word order or +/- sign is semantically load-bearing, so
    loose matching would wrongly merge DISTINCT diseases (e.g. 'Cone-rod dystrophy'
    vs 'Rod-cone dystrophy'; 'T+ B+' / 'T-B+' / 'T-B-' SCID). Seeded from a collision
    audit; extend the JSON data file (no code change needed). Missing file → empty
    set → loose matching for everything (i.e. degrades safely to prior behavior).
    """
    candidates: List[str] = []
    try:
        import synonyms  # namespace package mounted at /app/synonyms in the container
        candidates += [os.path.join(p, _EXACT_MATCH_NAMES_FILE) for p in list(synonyms.__path__)]
    except Exception:
        pass
    candidates.append(os.path.join(os.path.dirname(__file__),
                                   "..", "..", "services", "synonyms", _EXACT_MATCH_NAMES_FILE))
    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"[expand_synonyms] failed to read {path}: {e}")
            continue
        names = data.get("names", []) if isinstance(data, dict) else data
        keys = {k for n in names if (k := safe_normalize(n)) is not None}
        logger.info(f"[expand_synonyms] loaded {len(keys)} exact-match protected names from {path}")
        return keys
    logger.info("[expand_synonyms] no exact_match_disease_names.json found — loose matching for all")
    return set()


def _get_protected_keys() -> Set[str]:
    global _protected_keys_cache
    if _protected_keys_cache is None:
        _protected_keys_cache = _load_protected_keys()
    return _protected_keys_cache


def filter_candidates_by_db(
    db_name: Optional[str],
    field_name: str,
    candidates: List[str]
) -> List[str]:
    """
    Keep candidates that overlap a DB value and return the matched **DB-canonical**
    value(s) — not the synonym. Downstream filtering compares case-insensitively
    against the real column values (``col.str.to_lowercase().is_in(...)``), so a
    cosmetic variant like 'tuberculosis, pulmonary' must be emitted as HCDT's actual
    'Pulmonary tuberculosis' to match.

    Matching is order/punctuation/whitespace-insensitive (see _match_key) EXCEPT for
    names in the protected list (exact_match_disease_names.json), which require exact
    case-insensitive equality — those are names where word order or +/- sign is
    load-bearing and loose matching would merge distinct diseases. Variants differing
    by a missing/extra word are NOT matched here (left to the fuzzy/semantic services).
    """
    if not db_name or not candidates:
        return candidates

    db_choices = get_db_concept_values(db_name.lower()).get(field_name)
    if not db_choices:
        logger.warning(
            f"[expand_synonyms] No DB choices for {db_name}.{field_name}"
        )
        return candidates

    protected = _get_protected_keys()

    # Protected names -> exact (case-insensitive) key; everything else -> loose key.
    exact_map: Dict[str, List[str]] = {}   # safe_normalize(value) -> [db values]
    loose_map: Dict[str, List[str]] = {}   # _match_key(value)     -> [db values]
    for c in db_choices:
        nk = safe_normalize(c)
        if nk is not None and nk in protected:
            exact_map.setdefault(nk, []).append(c)
        else:
            k = _match_key(c)
            if k is not None:
                loose_map.setdefault(k, []).append(c)

    seen: Set[str] = set()
    filtered: List[str] = []
    for cand in candidates:
        nk = safe_normalize(cand)
        if nk is not None:
            for db_val in exact_map.get(nk, ()):        # protected: exact only
                if db_val not in seen:
                    seen.add(db_val)
                    filtered.append(db_val)
        k = _match_key(cand)
        if k is not None:
            for db_val in loose_map.get(k, ()):         # rest: order/punct-insensitive
                if db_val not in seen:
                    seen.add(db_val)
                    filtered.append(db_val)

    logger.info(
        f"[expand_synonyms] DB overlap filter for {db_name}.{field_name}: "
        f"{len(candidates)} → {len(filtered)} (token-normalized, "
        f"{len(exact_map)} exact-protected)"
    )
    return filtered


def get_aggregators() -> Dict[str, Any]:
    """
    Lazy load and cache synonym aggregators.
    
    Returns:
        Dict with aggregator instances
    """
    global _aggregator_cache
    
    if _aggregator_cache is not None:
        return _aggregator_cache
    
    logger.info("[expand_synonyms] Initializing synonym aggregators...")
    
    _aggregator_cache = {
        "target": TargetMemberAggregator(),
        "drug": DrugSynonymAggregator(),
        "gene": GeneSynonymAggregator(),
        "disease": DiseaseSynonymAggregator()
    }
    
    logger.info("[expand_synonyms] Aggregators initialized")
    return _aggregator_cache


_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "κ": "kappa", "λ": "lambda",
    "μ": "mu", "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma",
    "τ": "tau", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
}


def _spell_greek(s: str) -> str:
    """Spell out Greek letters so 'p110α' matches alias-table 'p110alpha'."""
    return "".join(_GREEK.get(ch, ch) for ch in s)


def safe_normalize(value: Any) -> Optional[str]:
    """Normalize value to lowercase string for comparison.

    Strips Unicode dashes (en-dash/em-dash → hyphen) and possessive suffixes
    ("Wilson's" → "Wilson") so that common orthographic variants of disease,
    gene, and drug names land on the same canonical key.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            logger.warning(f"Cannot normalize non-string value: {type(value)}")
            return None

    # Normalize Unicode dashes to ASCII hyphen (e.g. "Von Hippel–Lindau" → "Von Hippel-Lindau")
    value = value.replace('–', '-').replace('—', '-')
    # Strip possessives – handles straight (U+0027), curly (U+2018/2019), modifier (U+02BC)
    # "Wilson’s" -> "Wilson",  "Huntington’s" -> "Huntington"
    value = re.sub(u"[\u0027\u2018\u2019\u02bc]s\\b", "", value)
    normalized = value.strip().lower()
    return normalized if normalized else None


async def call_llm_filter_service(
    client: httpx.AsyncClient,
    field_name: str,
    single_term: str,
    matches: List[str]
) -> List[str]:
    """
    Call LLM filter service for a single term with error handling.
    """
    try:
        input_data = Llm_Member_Selector_Input(
            category=field_name,
            single_term=single_term,
            string_list=matches
        ).model_dump()

        response = await client.post(
            LLM_FILTER_URL,
            json=input_data,
            timeout=LLM_FILTER_TIMEOUT
        )
        response.raise_for_status()

        result = response.json()

        if not isinstance(result, dict) or "value" not in result:
            logger.warning(
                f"[expand_synonyms] Invalid LLM filter response for term '{single_term}': {result}"
            )
            return []

        filtered = result["value"]
        if not isinstance(filtered, list):
            logger.warning(
                f"[expand_synonyms] LLM filter returned non-list for term '{single_term}': {type(filtered)}"
            )
            return []

        return filtered

    except httpx.TimeoutException:
        logger.error(f"[expand_synonyms] Timeout calling LLM filter for term '{single_term}'")
        return []
    except httpx.HTTPError as e:
        logger.error(f"[expand_synonyms] HTTP error calling LLM filter for term '{single_term}': {e}")
        return []
    except Exception as e:
        logger.exception(f"[expand_synonyms] Error calling LLM filter for term '{single_term}': {e}")
        return []


async def filter_candidates_with_llm(
    field_name: str,
    user_terms: List[str],
    candidates: List[str]
) -> List[str]:
    """
    Run LLM filter for each user term and return a unique filtered list.
    Falls back to empty list on failure.
    """
    if not user_terms or not candidates:
        return []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_REQUESTS)

    async with httpx.AsyncClient() as client:
        async def filtered_call(term: str) -> List[str]:
            async with semaphore:
                return await call_llm_filter_service(client, field_name, term, candidates)

        try:
            llm_filter_results = await asyncio.gather(
                *[filtered_call(term) for term in user_terms],
                return_exceptions=True
            )
        except Exception as e:
            logger.exception(f"[expand_synonyms] LLM filtering failed for field '{field_name}': {e}")
            return []

    llm_filter_matches: List[str] = []
    error_count = 0

    for i, result in enumerate(llm_filter_results):
        if isinstance(result, Exception):
            error_count += 1
            logger.error(
                f"[expand_synonyms] LLM filter error for term '{user_terms[i]}': {result}"
            )
            continue
        if isinstance(result, list):
            llm_filter_matches.extend(result)
        else:
            logger.warning(
                f"[expand_synonyms] Unexpected LLM filter result type for term '{user_terms[i]}': "
                f"{type(result)}"
            )

    if error_count > 0:
        logger.warning(
            f"[expand_synonyms] {error_count}/{len(user_terms)} LLM filter calls failed "
            f"for field '{field_name}'"
        )

    # Normalize + dedupe
    seen: Set[str] = set()
    filtered_unique: List[str] = []
    for match in llm_filter_matches:
        norm = safe_normalize(match)
        if norm and norm not in seen:
            seen.add(norm)
            filtered_unique.append(norm)

    return filtered_unique


async def synonyms_expander(
    data: Dict[str, Any],
    database: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """
    Expand biomedical terms with their synonyms and aliases.

    Args:
        data: Parsed query values (dict or ParsedValue)
        database: Optional target database for DB-overlap filtering
        raw: If True, return the DB-overlap candidate list for disease_name
             without running the LLM filter. Used by expand_and_match_db's
             unified-LLM path. Only affects the disease_name branch (the only
             branch that currently calls the LLM filter).

    Returns:
        Dict with expanded synonyms for each field
    """
    tool = "expand_synonyms"
    
    logger.info(f"[{tool}] Starting synonym expansion")
    
    # Convert to dict if needed
    try:
        if hasattr(data, 'model_dump'):
            field_outputs = data.model_dump(exclude_none=True)
        else:
            field_outputs = data
    except Exception as e:
        logger.error(f"[{tool}] Failed to convert input to dict: {e}")
        field_outputs = data if isinstance(data, dict) else {}
    
    if not field_outputs:
        logger.warning(f"[{tool}] Empty input data")
        return {}
    
    # db_name = database.strip() if isinstance(database, str) else None
    db_name = database.strip() if isinstance(database, str) and database.strip() else None

    logger.info(f"[{tool}] Input fields: {list(field_outputs.keys())}")
    
    # Deep copy to avoid mutating input
    processed = copy.deepcopy(field_outputs)
    
    # Get aggregators (cached)
    try:
        aggregators = get_aggregators()
    except Exception as e:
        logger.exception(f"[{tool}] Failed to load aggregators: {e}")
        return processed
    
    # Helper to safely expand a list of terms
    async def expand_terms(
        terms: list,
        aggregator: Any,
        category: str
    ) -> Set[str]:
        """Expand a list of terms using an aggregator."""
        all_synonyms = set()
        
        for term in terms:
            if not term:
                continue
            
            try:
                result = await aggregator.get_all_synonyms(term)
                synonyms = result.get("combined_synonyms", [])
                
                # Safely normalize all synonyms
                normalized = [
                    norm for s in synonyms
                    if (norm := safe_normalize(s)) is not None
                ]
                
                all_synonyms.update(normalized)
                
                logger.info(
                    f"[{tool}] {category} '{term}': "
                    f"{len(synonyms)} synonyms → {len(normalized)} normalized"
                )
                
            except Exception as e:
                logger.exception(f"[{tool}] Failed to expand {category} '{term}': {e}")
        
        return all_synonyms
    
    # -------- TARGET → GENE --------
    if "target_name" in field_outputs:
        target_value = field_outputs.get("target_name")
        
        if isinstance(target_value, list) and target_value:
            logger.info(f"[{tool}] Expanding {len(target_value)} targets")
            
            target_synonyms = await expand_terms(
                target_value,
                aggregators["target"],
                "target"
            )
            
            # FIX: Create gene_name if it doesn't exist!
            if "gene_name" not in processed or not isinstance(processed["gene_name"], list):
                processed["gene_name"] = []
            
            # Normalize existing genes
            existing_genes = {
                norm for g in processed["gene_name"]
                if (norm := safe_normalize(g)) is not None
            }
            
            # Combine and sort
            processed["gene_name"] = sorted(existing_genes | target_synonyms)
            
            logger.info(
                f"[{tool}] Target expansion: "
                f"{len(target_synonyms)} synonyms → gene_name has {len(processed['gene_name'])} total"
            )
    
    # -------- DRUG --------
    if "drug_name" in field_outputs:
        drug_value = field_outputs.get("drug_name")
        if isinstance(drug_value, str) and drug_value:
            drug_value = [drug_value]
        if isinstance(drug_value, list) and drug_value:
            logger.info(f"[{tool}] Expanding {len(drug_value)} drugs")
            
            drug_synonyms = await expand_terms(
                drug_value,
                aggregators["drug"],
                "drug"
            )

            # Salt-stripping: when the user queries a salt form (e.g. "imatinib
            # mesylate"), external APIs return the salt's synonyms but not the free
            # base ("imatinib"). Strip known salt suffixes, expand the base name
            # separately, and merge — so DBs that store the free-base INN (TTD,
            # HCDT, …) still get a match.
            base_names = []
            original_norms = {safe_normalize(d) for d in drug_value}
            for d in drug_value:
                base = _strip_drug_salt(d)
                if base and safe_normalize(base) not in original_norms:
                    base_names.append(base)

            if base_names:
                logger.info(f"[{tool}] Salt-stripped base names to expand: {base_names}")
                base_synonyms = await expand_terms(
                    base_names,
                    aggregators["drug"],
                    "drug-base"
                )
                base_normalized = {
                    norm for b in base_names
                    if (norm := safe_normalize(b)) is not None
                }
                drug_synonyms |= base_synonyms | base_normalized

            # Normalize original drugs
            original_drugs = {
                norm for d in drug_value
                if (norm := safe_normalize(d)) is not None
            }

            # DB-native drug alias resolution: before/alongside external APIs,
            # check the DB's own synonym table (alias_map_<db>.pkl, "drug_name"
            # key). Resolves investigational codes (DX-88 → Plasma kallikrein,
            # RTA-408 → Omaveloxolone) and brand names (Viagra → SILDENAFIL)
            # that external APIs don't know. Values may be a list of canonical
            # forms (e.g. free-base + salt) — all are added for max recall.
            alias_drug_canon: set = set()
            if db_name:
                _damap = get_db_alias_map(db_name).get("drug_name", {})
                if _damap:
                    for d in drug_value:
                        nk = safe_normalize(d)
                        if not nk:
                            continue
                        canon = _damap.get(nk)
                        if canon:
                            if isinstance(canon, list):
                                alias_drug_canon.update(c.lower() for c in canon)
                            else:
                                alias_drug_canon.add(canon.lower())
                    if alias_drug_canon:
                        logger.info(
                            f"[{tool}] drug_name alias-map resolved "
                            f"{len(alias_drug_canon)} canonical name(s) from DB alias table"
                        )

            # Combine and sort
            expanded_drugs = sorted(original_drugs | drug_synonyms | alias_drug_canon)

            # Filter to terms the DB actually stores (same as disease path). Skip
            # the filter when it returns empty — that means the drug is unknown to
            # this DB and all candidates should be forwarded so downstream services
            # can report the gap rather than silently return nothing.
            if db_name:
                db_filtered = filter_candidates_by_db(db_name, "drug_name", expanded_drugs)
                processed["drug_name"] = db_filtered if db_filtered else expanded_drugs
            else:
                processed["drug_name"] = expanded_drugs

            logger.info(
                f"[{tool}] Drug expansion: "
                f"{len(drug_synonyms)} synonyms → drug_name has {len(processed['drug_name'])} total"
            )
    
    # -------- GENE --------
    if "gene_name" in field_outputs:
        gene_value = field_outputs.get("gene_name")
        if isinstance(gene_value, str) and gene_value:
            gene_value = [gene_value]
        if isinstance(gene_value, list) and gene_value:
            logger.info(f"[{tool}] Expanding {len(gene_value)} genes")
            
            gene_synonyms = await expand_terms(
                gene_value,
                aggregators["gene"],
                "gene"
            )
            
            # Normalize original genes
            original_genes = {
                norm for g in gene_value
                if (norm := safe_normalize(g)) is not None
            }
            
            # Combine and sort (merge with any existing from target expansion)
            existing = set(processed.get("gene_name", []))
            processed["gene_name"] = sorted(original_genes | gene_synonyms | existing)
            
            logger.info(
                f"[{tool}] Gene expansion: "
                f"{len(gene_synonyms)} synonyms → gene_name has {len(processed['gene_name'])} total"
            )
    
    # -------- GENE SYMBOLS (gene_symbol / enzyme_genesymbol / substrate_genesymbol) --------
    # All are canonical identifiers, but databases may store aliases
    # (e.g. EGFR → ERBB1, HER1). Use GeneSynonymAggregator for all.
    # STRING uses table-prefixed variants (association_gene_symbol, physical_gene_symbol,
    # channel_gene_symbol) — include them so alias_map_string.pkl + runtime fetchers fire.
    for _gsym_field in (
        "gene_symbol", "enzyme_genesymbol", "substrate_genesymbol",
        "association_gene_symbol", "physical_gene_symbol", "channel_gene_symbol",
    ):
        if _gsym_field not in field_outputs:
            continue
        gsym_value = field_outputs.get(_gsym_field)
        if isinstance(gsym_value, str) and gsym_value:
            gsym_value = [gsym_value]
        if not isinstance(gsym_value, list) or not gsym_value:
            continue
        logger.info(f"[{tool}] Expanding {len(gsym_value)} {_gsym_field} symbols")

        gsym_synonyms = await expand_terms(
            gsym_value,
            aggregators["gene"],
            _gsym_field
        )

        original_gsyms = {
            norm for g in gsym_value
            if (norm := safe_normalize(g)) is not None
        }

        # DB-native alias resolution: external KBs track gene symbols, not protein
        # nicknames/full names ("p110α", "calcium-sensing receptor", "junctin").
        # Those live in the DB's own alias table (alias_map_<db>.pkl); map the
        # input term → DB-canonical symbol (e.g. "p110alpha" → "PIK3CA"). Keyed
        # by the canonical field name (matches this loop's field). No-op if absent.
        alias_canon: Set[str] = set()
        if db_name:
            # alias_map_<db>.pkl is always keyed by "gene_symbol" regardless of
            # the column's table-prefixed name (association_gene_symbol, etc.).
            # Fall back to "gene_symbol" key when the prefixed name is absent.
            _alias_key = _gsym_field if _gsym_field in get_db_alias_map(db_name) else "gene_symbol"
            amap = get_db_alias_map(db_name).get(_alias_key, {})
            if amap:
                for g in gsym_value:
                    nk = safe_normalize(g)
                    if not nk:
                        continue
                    # try the term as-is and with Greek letters spelled out, since
                    # alias tables store "p110alpha" while users type "p110α".
                    canon = amap.get(nk) or amap.get(_spell_greek(nk))
                    if canon:
                        alias_canon.add(canon.lower())
                if alias_canon:
                    logger.info(
                        f"[{tool}] {_gsym_field} alias-map resolved "
                        f"{len(alias_canon)} canonical symbol(s) from DB alias table"
                    )

        existing = set(processed.get(_gsym_field, []))
        processed[_gsym_field] = sorted(original_gsyms | gsym_synonyms | existing | alias_canon)

        logger.info(
            f"[{tool}] {_gsym_field} expansion: "
            f"{len(gsym_synonyms)} synonyms → {_gsym_field} has {len(processed[_gsym_field])} total"
        )

    # -------- DISEASE --------
    if "disease_name" in field_outputs:
        disease_value = field_outputs.get("disease_name")

        if isinstance(disease_value, list) and disease_value:
            logger.info(f"[{tool}] Expanding {len(disease_value)} diseases")

            disease_synonyms = await expand_terms(
                disease_value,
                aggregators["disease"],
                "disease"
            )

            original_diseases = {
                norm for d in disease_value
                if (norm := safe_normalize(d)) is not None
            }

            # DB-native disease alias resolution: informal names ("breast cancer",
            # "alzheimer's disease") → DB-canonical names ("Breast Neoplasms",
            # "Alzheimer Disease"). Mirrors the gene_symbol alias-map logic above.
            alias_disease_canon: Set[str] = set()
            if db_name:
                amap = get_db_alias_map(db_name).get("disease_name", {})
                if amap:
                    for d in disease_value:
                        nk = safe_normalize(d)
                        if not nk:
                            continue
                        canon = amap.get(nk)
                        if canon:
                            alias_disease_canon.add(canon.lower())
                    if alias_disease_canon:
                        logger.info(
                            f"[{tool}] disease_name alias-map resolved "
                            f"{len(alias_disease_canon)} canonical name(s) from DB alias table"
                        )

            candidate_diseases = sorted(original_diseases | disease_synonyms | alias_disease_canon)

            # NEW: if database is None -> return full aggregate directly
            if db_name is None:
                processed["disease_name"] = candidate_diseases
                logger.info(
                    f"[{tool}] Disease expansion (no database): "
                    f"returning {len(processed['disease_name'])} aggregated candidates"
                )
            else:
                # Keep existing behavior when database is provided
                db_filtered_candidates = filter_candidates_by_db(
                    db_name,
                    "disease_name",
                    candidate_diseases
                )
                logger.info(
                    f"[{tool}] Disease candidates after DB overlap: "
                    f"{len(db_filtered_candidates)}"
                )

                user_terms = [
                    d.strip() for d in disease_value
                    if isinstance(d, str) and d.strip()
                ]

                llm_filtered = []
                if raw:
                    # Caller will run the unified LLM filter on the union of
                    # fuzzy + semantic + synonyms candidates. Return DB-overlap
                    # candidates as-is.
                    processed["disease_name"] = sorted(set(db_filtered_candidates))
                    logger.info(
                        f"[{tool}][RAW] Disease: returning {len(processed['disease_name'])} "
                        f"DB-overlap candidates (LLM skipped)"
                    )
                else:
                    if db_filtered_candidates:
                        llm_filtered = await filter_candidates_with_llm(
                            field_name="disease_name",
                            user_terms=user_terms,
                            candidates=db_filtered_candidates
                        )
                    else:
                        logger.info(f"[{tool}] Disease LLM filter skipped: no DB-overlap candidates")

                    if llm_filtered:
                        processed["disease_name"] = sorted(set(llm_filtered))
                        logger.info(
                            f"[{tool}] Disease expansion + LLM filter: "
                            f"{len(db_filtered_candidates)} candidates → {len(processed['disease_name'])} final"
                        )
                    elif db_filtered_candidates:
                        # LLM returned nothing but DB overlap had candidates — keep originals
                        processed["disease_name"] = sorted(original_diseases)
                        logger.info(
                            f"[{tool}] Disease expansion: LLM returned no matches; "
                            f"keeping {len(processed['disease_name'])} original terms only"
                        )
                    else:
                        # DB overlap found 0 matches — ontology names don't match DB style
                        # (e.g. MONDO/DOID names vs CTD MeSH-style). Pass all aggregated
                        # synonyms through so downstream fuzzy matching can still work.
                        processed["disease_name"] = candidate_diseases if candidate_diseases else sorted(original_diseases)
                        logger.info(
                            f"[{tool}] Disease expansion: DB overlap empty; passing "
                            f"{len(processed['disease_name'])} aggregated synonyms to downstream"
                        )

    # -------- PHENOTYPE --------
    # phenotype_name appears in HPO, orphanet, and CTD phenotype_ixn tables.
    # No external synonym API exists for phenotype terms; rely entirely on the
    # DB-local concept_values pool. If the pool returns nothing (unknown DB or
    # missing concept key) pass the original terms through unchanged so downstream
    # fuzzy/semantic services can still attempt a match.
    if "phenotype_name" in field_outputs:
        phenotype_value = field_outputs.get("phenotype_name")
        if isinstance(phenotype_value, str) and phenotype_value:
            phenotype_value = [phenotype_value]
        if isinstance(phenotype_value, list) and phenotype_value:
            logger.info(f"[{tool}] Expanding {len(phenotype_value)} phenotype_name terms")

            original_phenotypes = {
                norm for p in phenotype_value
                if (norm := safe_normalize(p)) is not None
            }

            if db_name:
                db_filtered = filter_candidates_by_db(db_name, "phenotype_name", list(original_phenotypes))
                processed["phenotype_name"] = sorted(db_filtered) if db_filtered else sorted(original_phenotypes)
            else:
                processed["phenotype_name"] = sorted(original_phenotypes)

            logger.info(
                f"[{tool}] phenotype_name expansion: "
                f"{len(original_phenotypes)} originals → {len(processed['phenotype_name'])} final"
            )

    # -------- PATHWAY / GENESET --------
    # pathway_name (reactome) and geneset_name (msigdb) have no external synonym
    # APIs — rely on DB-local concept_values pool for canonical name matching.
    # Pass through unchanged if the pool is empty (safe fallback for unknown DBs).
    for _path_field in ("pathway_name", "geneset_name"):
        if _path_field not in field_outputs:
            continue
        path_value = field_outputs.get(_path_field)
        if isinstance(path_value, str) and path_value:
            path_value = [path_value]
        if not isinstance(path_value, list) or not path_value:
            continue

        logger.info(f"[{tool}] Expanding {len(path_value)} {_path_field} terms")

        original_paths = {
            norm for p in path_value
            if (norm := safe_normalize(p)) is not None
        }

        if db_name:
            db_filtered = filter_candidates_by_db(db_name, _path_field, list(original_paths))
            processed[_path_field] = sorted(db_filtered) if db_filtered else sorted(original_paths)
        else:
            processed[_path_field] = sorted(original_paths)

        logger.info(
            f"[{tool}] {_path_field} expansion: "
            f"{len(original_paths)} originals → {len(processed[_path_field])} final"
        )

    # -------- PROTEIN --------
    # protein_name appears in uniprot. Proteins are best bridged to DB values via
    # their encoding gene symbols. Delegate to GeneSynonymAggregator (same as
    # gene_symbol) to pull NCBI/HGNC aliases, then apply the DB alias-map and
    # concept-values filter. Falls back to the original term if nothing resolves.
    if "protein_name" in field_outputs:
        protein_value = field_outputs.get("protein_name")
        if isinstance(protein_value, str) and protein_value:
            protein_value = [protein_value]
        if isinstance(protein_value, list) and protein_value:
            logger.info(f"[{tool}] Expanding {len(protein_value)} protein_name terms")

            protein_synonyms = await expand_terms(
                protein_value,
                aggregators["gene"],  # gene aggregator bridges protein↔gene-symbol
                "protein_name"
            )

            original_proteins = {
                norm for p in protein_value
                if (norm := safe_normalize(p)) is not None
            }

            # DB-native alias resolution (same pattern as gene_symbol above)
            alias_protein_canon: Set[str] = set()
            if db_name:
                amap = get_db_alias_map(db_name).get("protein_name", {})
                if amap:
                    for p in protein_value:
                        nk = safe_normalize(p)
                        if not nk:
                            continue
                        canon = amap.get(nk)
                        if canon:
                            if isinstance(canon, list):
                                alias_protein_canon.update(c.lower() for c in canon)
                            else:
                                alias_protein_canon.add(canon.lower())
                    if alias_protein_canon:
                        logger.info(
                            f"[{tool}] protein_name alias-map resolved "
                            f"{len(alias_protein_canon)} canonical name(s) from DB alias table"
                        )

            candidate_proteins = sorted(original_proteins | protein_synonyms | alias_protein_canon)

            if db_name:
                db_filtered = filter_candidates_by_db(db_name, "protein_name", candidate_proteins)
                processed["protein_name"] = db_filtered if db_filtered else candidate_proteins
            else:
                processed["protein_name"] = candidate_proteins

            logger.info(
                f"[{tool}] protein_name expansion: "
                f"{len(protein_synonyms)} synonyms → protein_name has {len(processed['protein_name'])} total"
            )

    # -------- VARIANT --------
    # variant_name (clinvar) carries rsIDs, HGVS expressions, and ClinVar
    # allele identifiers. Synonym expansion must NOT be applied — injecting
    # false synonyms into structured identifiers would corrupt filtering.
    # Normalize to lowercase and pass through unchanged.
    if "variant_name" in field_outputs:
        variant_value = field_outputs.get("variant_name")
        if isinstance(variant_value, str) and variant_value:
            variant_value = [variant_value]
        if isinstance(variant_value, list) and variant_value:
            logger.info(f"[{tool}] Passing through {len(variant_value)} variant_name terms (no expansion)")

            normalized_variants = sorted({
                norm for v in variant_value
                if (norm := safe_normalize(v)) is not None
            })
            processed["variant_name"] = normalized_variants if normalized_variants else sorted(
                v for v in variant_value if v
            )

            logger.info(
                f"[{tool}] variant_name: {len(processed['variant_name'])} terms passed through"
            )

    # -------- FINAL OUTPUT --------
    output_data = {
        k: (
            v if v == "requested"  # Keep "requested" as-is
            else sorted(v) if isinstance(v, list) and v  # Sort non-empty lists
            else None  # Everything else becomes None
        )
        for k, v in processed.items()
    }
    
    # Log summary
    total_terms = sum(
        len(v) if isinstance(v, list) else 0
        for v in output_data.values()
    )
    
    logger.info(
        f"[{tool}] Finished. "
        f"Output has {len(output_data)} fields with {total_terms} total terms"
    )
    
    return output_data
