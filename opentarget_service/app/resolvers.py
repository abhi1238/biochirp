from typing import List, Dict, Any, Tuple, Set, Optional
from .guard_rail import ResolvedEntity, QueryResolution, CombinedOutput
from .client import OTGraphQLClient, _pick_canonical_hit, _norm_term
from .config import OTClientConfig
from .uvicorn_logger import setup_logger
from agents import Agent, Runner, function_tool
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
import asyncio
import logging
import os

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models

# ── Groq client for NER + pathway classifier (bypasses LiteLLM) ───────────────
_OT_NER_MODEL = os.getenv("OT_NER_MODEL", "")
if not _OT_NER_MODEL:
    logging.warning("OT_NER_MODEL is not set; NER calls will fail at runtime.")
_groq_client_resolver = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=settings.get_groq_key("opentargets"),
)
_groq_ner_model = OpenAIChatCompletionsModel(
    model=_OT_NER_MODEL,
    openai_client=_groq_client_resolver,
)

# =========================================================
# OpenTargets client
# =========================================================
_cfg = OTClientConfig()
_ot = OTGraphQLClient(_cfg)

# =========================================================
# Logging
# =========================================================
# logger = setup_logger("biochirp.opentargets.resolvers")
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.resolver")

# =========================================================
# Exceptions
# =========================================================
class OpenTargetsError(RuntimeError): ...
class OpenTargetsNotFound(OpenTargetsError): ...
class OpenTargetsUpstream(OpenTargetsError): ...


def _normalize_term(t: str) -> str:
    """Normalize Unicode apostrophes/quotes to ASCII so mapIds resolves them."""
    return (t or "").strip().replace('‘', "'").replace('’', "'")



async def resolve_drug_id(drug_name_or_id: str) -> Tuple[str, str]:
    t = _normalize_term(drug_name_or_id)
    if not t:
        raise ValueError("drug_name_or_id is empty")

    if t.upper().startswith("CHEMBL"):
        chembl = t.upper()
        q = "query($id:String!){drug(chemblId:$id){id name}}"
        d = (await _ot.run(q, {"id": chembl})).get("drug") or {}
        if not d:
            raise OpenTargetsNotFound(f"Drug not found: {t}")
        return d.get("id") or chembl, d.get("name") or None

    hit = await _ot.search_first_hit(t, "drug")
    if not hit:
        raise OpenTargetsNotFound(f"Drug not found: {t}")
    return hit["id"], hit.get("name")


async def resolve_target_id(target_symbol_or_id: str) -> Tuple[str, str]:
    t = _normalize_term(target_symbol_or_id)
    if not t:
        raise ValueError("target_symbol_or_id is empty")

    if t.upper().startswith("ENS"):
        tid = t
        q = "query($id:String!){target(ensemblId:$id){id approvedSymbol approvedName}}"
        d = (await _ot.run(q, {"id": tid})).get("target") or {}
        if not d:
            return tid, None
        return d.get("id") or tid, d.get("approvedSymbol") or d.get("approvedName")

    hit = await _ot.search_first_hit(t, "target")
    if not hit:
        raise OpenTargetsNotFound(f"Target not found: {t}")
    return hit["id"], hit.get("name")




async def resolve_disease_id(disease_name_or_id: str) -> Tuple[str, str]:
    t = _normalize_term(disease_name_or_id)
    if not t:
        raise ValueError("disease_name_or_id is empty")

    if t.upper().startswith(("EFO_", "MONDO_")):
        did = t.upper()
        q = "query($id:String!){disease(efoId:$id){id name}}"
        d = (await _ot.run(q, {"id": did})).get("disease") or {}
        if not d:
            # Stale/retired ID — do NOT search for the literal ID string;
            # that returns nothing. Raise so callers can fall back to the
            # surface-form name resolver.
            raise OpenTargetsNotFound(f"Stale or retired disease ID: {did}")
        return d.get("id") or did, d.get("name")

    hit = await _ot.search_first_hit(t, "disease")
    if not hit:
        raise OpenTargetsNotFound(f"Disease not found: {t}")
    return hit["id"], hit.get("name")

# =========================================================
# Deterministic OpenTargets resolver (mapIds ONLY)
# =========================================================
async def open_targets_resolver(term: str) -> ResolvedEntity:
    """
    Deterministically resolve a biomedical surface form using OpenTargets mapIds.
    No LLM. No intent inference. No priority hacks.
    """
    logger.info(f"[open_targets_resolver][Input]: {term}")
    t = _normalize_term(term)

    if not t:
        return ResolvedEntity(
            surface_form=None,
            type=None,
            id=None,
            resolution_method="not_found",
        )

    _mapids_q = """
            query ($terms:[String!]!) {
              mapIds(queryTerms:$terms) {
                mappings {
                  hits {
                    id
                    entity
                    name
                  }
                }
              }
            }
            """
    try:
        try:
            data = await _ot.run(_mapids_q, {"terms": [t]})
        except Exception as first_exc:  # noqa: BLE001
            # One extra retry to ride out a transient OT gateway blip, so a brief
            # upstream failure is NOT silently reported as 'entity not found'
            # (which would wrongly trigger the drug/acronym fallback chains).
            logger.info("[open_targets_resolver] mapIds retry after: %s", first_exc)
            await asyncio.sleep(0.6)
            data = await _ot.run(_mapids_q, {"terms": [t]})

        hits: List[Dict[str, Any]] = []
        for m in (data.get("mapIds") or {}).get("mappings", []):
            hits.extend(m.get("hits") or [])

        if not hits:
            # mapIds missed — try the OT search API (handles aliases, synonyms, legacy names)
            for entity_type in ("target", "disease", "drug"):
                try:
                    hit = await _ot.search_first_hit(t, entity_type)
                    if hit:
                        logger.info(
                            "[open_targets_resolver] search fallback hit %r → %s (%s)",
                            t, hit["id"], entity_type,
                        )
                        return ResolvedEntity(
                            surface_form=t,
                            type=entity_type,
                            id=str(hit["id"]),
                            resolution_method="search_fallback",
                        )
                except Exception:
                    pass
            # Last resort: if term looks like "A/B" (slash-paired genes/diseases),
            # try each part independently and return the first hit.
            if "/" in t:
                for part in t.split("/"):
                    part = part.strip()
                    if not part:
                        continue
                    for entity_type in ("target", "disease", "drug"):
                        try:
                            hit = await _ot.search_first_hit(part, entity_type)
                            if hit:
                                logger.info(
                                    "[open_targets_resolver] slash-split hit %r → %s (%s)",
                                    part, hit["id"], entity_type,
                                )
                                return ResolvedEntity(
                                    surface_form=t,
                                    type=entity_type,
                                    id=str(hit["id"]),
                                    resolution_method="slash_split_fallback",
                                )
                        except Exception:
                            pass
            return ResolvedEntity(
                surface_form=t,
                type=None,
                id=None,
                resolution_method="not_found",
            )

        # OpenTargets mapIds is fuzzy/relevance-ranked and can rank a DIFFERENT entity
        # above the exact queried term (e.g. mapIds("metformin") →
        # [ROSIGLITAZONE, BIGUANIDE, METFORMIN]; mapIds("pembrolizumab") →
        # [TORIPALIMAB, PEMBROLIZUMAB, ...]). Anchor to the literal term: prefer an
        # exact name match, then canonical substring, then positional — the same
        # selector the search path uses. Nothing entity-specific is hardcoded.
        best = _pick_canonical_hit(hits, t) or hits[0]

        # If the term did NOT exact-match the chosen hit's name, disambiguate target
        # candidates by approvedSymbol — mapIds aliases can disagree with HGNC
        # (e.g. "HTT" can rank SLC6A4 before huntingtin if the alias index disagrees).
        if _norm_term(best.get("name") or "") != _norm_term(t):
            target_hits = [h for h in hits if str(h.get("entity", "")).lower() == "target"]
            if len(target_hits) > 1:
                for candidate in target_hits:
                    try:
                        sym_data = await _ot.run(
                            "query($id:String!){target(ensemblId:$id){approvedSymbol}}",
                            {"id": candidate["id"]},
                        )
                        sym = (sym_data.get("target") or {}).get("approvedSymbol", "")
                        if sym and sym.upper() == t.upper():
                            best = candidate
                            logger.info(
                                "[open_targets_resolver] approvedSymbol match: %r → %s",
                                t, candidate["id"],
                            )
                            break
                    except Exception:
                        pass  # verification failure → keep positional best

        return ResolvedEntity(
            surface_form=t,
            type=str(best["entity"]).lower(),
            id=str(best["id"]),
            resolution_method="mapIds",
        )

    except Exception:
        logger.exception("[open_targets_resolver] mapIds failed")
        return ResolvedEntity(
            surface_form=t,
            type=None,
            id=None,
            resolution_method="not_found",
        )


with open("/app/resources/prompts/opentarget_entity_extractor.md", "r", encoding="utf-8") as f:
    prompt_md_entity_extractor = f.read()



PATHWAY_MECHANISM_CLASSIFIER = """You are a biomedical term classifier. Classify the given terms into exactly ONE category.

OUTPUT ONLY ONE OF THESE THREE EXACT STRINGS (no quotes, no explanation, no extra text):
mechanism_of_action
pathway_name
null

CLASSIFICATION RULES:

mechanism_of_action = How a drug/molecule acts
Examples: inhibitor, activator, antagonist, agonist, blocker, modulator, inducer, suppressor

pathway_name = Named biological/signaling pathway
Examples: MAPK pathway, PI3K/AKT pathway, apoptosis pathway, Wnt signaling, glycolysis

null = Anything else or unclear

CRITICAL OUTPUT REQUIREMENTS:
- Return ONLY the category string
- NO explanations
- NO punctuation marks (no periods, commas, quotes)
- NO preambles like "The category is" or "This is"
- NO additional words
- NO markdown formatting
- NO newlines before or after

EXAMPLES:

Input: kinase inhibitor
Output: mechanism_of_action

Input: mTOR pathway
Output: pathway_name

Input: aspirin
Output: null

Input: EGFR antagonist
Output: mechanism_of_action

Input: unknown term XYZ123
Output: null
"""


agent_pathway_mechanism_classifier = Agent(
    name="pathway_mechanism_classifier",
    model=_groq_ner_model,
    instructions=PATHWAY_MECHANISM_CLASSIFIER,
    output_type=str,
    tools=[],
)


# if not prompt_md:
#     message = "Summarization skipped (no prompt loaded)"
# else:


async def call_grok(user_prompt: str, model: str | None = None, reasoning_effort: str = "low") -> CombinedOutput:
    import json
    from pydantic import ValidationError

    _model = model or _OT_NER_MODEL

    res = await _groq_client_resolver.chat.completions.create(
        model=_model,
        messages=[
            {"role": "system", "content": prompt_md_entity_extractor},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        reasoning_effort=reasoning_effort,
        response_format={"type": "json_object"},
    )

    _cached = getattr(getattr(res.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    logger.info("[NER cache] prompt_tokens=%s cached=%s", getattr(res.usage, "prompt_tokens", "?"), _cached)

    try:
        json_str = res.choices[0].message.content.strip()

        if json_str.startswith("```json"):
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif json_str.startswith("```"):
            json_str = json_str.split("```")[1].split("```")[0].strip()

        data = json.loads(json_str)
        _valid_rt = {"drug", "target", "disease", "mechanism_of_action", "pathway"}
        if isinstance(data.get("requested_types"), list):
            data["requested_types"] = [t for t in data["requested_types"] if t in _valid_rt]
        return CombinedOutput(**data)

    except (json.JSONDecodeError, ValidationError) as e:
        logger.error(f"[NER] Failed to parse output: {e}")
        logger.error(f"[NER] Raw output: {res.choices[0].message.content}")
        return CombinedOutput(entities=[], requested_types=[])



# ─── Per-connection intent cache ─────────────────────────────────────────────
# Populated by set_query_intent_hints() (called from main.py before the
# orchestrator runs) using the FULL user_input.  The interpreter reads this
# to override the NER model when the orchestrator passes only the entity name.
#
# Both caches are keyed by connection_id and are normally evicted by
# evict_connection() from the WS handler's finally-block.  They are also
# capped FIFO (OrderedDict) as a safety net so that HTTP-shim connection ids
# (which have no WS lifecycle) and any missed disconnects can't grow unbounded.
from collections import OrderedDict as _OrderedDict

_CONN_CACHE_MAX = int(os.getenv("OT_CONN_CACHE_MAX", "1000"))
_CONN_INTENT_HINTS: "_OrderedDict[str, list[str]]" = _OrderedDict()

# Stores requested_output ("drug"|"disease"|"target"|"pathway"|"any"|None) keyed
# by connection_id.  Set by interpreter() and read by utility_drug/target/disease.
# This is NOT included in the interpreter's returned QueryResolution JSON to
# prevent the orchestrator from trying to pass it back to interpreter (which
# rejects unknown fields and causes retry loops).
_CONN_REQUESTED_OUTPUT: "_OrderedDict[str, Optional[str]]" = _OrderedDict()


def _cap_conn_cache(cache: "_OrderedDict") -> None:
    """FIFO-evict the oldest entries until *cache* is within _CONN_CACHE_MAX."""
    while len(cache) > _CONN_CACHE_MAX:
        cache.popitem(last=False)


def evict_connection(connection_id: str) -> None:
    """Drop both per-connection caches for *connection_id* (called on WS close)."""
    if not connection_id:
        return
    _CONN_INTENT_HINTS.pop(connection_id, None)
    _CONN_REQUESTED_OUTPUT.pop(connection_id, None)


def get_requested_output(connection_id: str) -> Optional[str]:
    """Return the cached requested_output for a connection."""
    return _CONN_REQUESTED_OUTPUT.get(connection_id or "")

# ─── Output-type keyword sets (replaces regex patterns) ──────────────────────
# Simple word-presence check — the NER model is the primary authority for intent;
# these keywords are a lightweight fallback for when the orchestrator passes only
# the entity name to interpreter instead of the full question.
_TARGET_KEYWORDS = frozenset([
    "gene", "genes", "protein", "proteins", "target", "targets",
])
_PATHWAY_KEYWORDS = frozenset([
    "pathway", "pathways",
])
_DISEASE_KEYWORDS = frozenset([
    "disease", "diseases", "condition", "conditions",
    "disorder", "disorders", "indication", "indications",
])
_EXPRESSION_KEYWORDS = frozenset([
    # "expression" alone is intentionally excluded — too ambiguous with
    # "rna expression evidence" in disease-association queries.
    # "expressed" (past-tense verb about tissue location) is safe.
    "expressed",
    "tissue", "tissues", "organ", "organs",
    "synthesize", "synthesise", "synthesizes", "synthesises",
])
_GWAS_KEYWORDS = frozenset([
    "gwas", "credible", "snp", "locus", "loci",
])
# NOTE: there is intentionally NO _DRUG_KEYWORDS hint set. A drug-output hint
# would have to fire on tokens like "drug"/"drugs"/"approved", but those appear
# constantly in TARGET/DISEASE queries ("drug targets", "approved targets",
# "drug resistant epilepsy"). Materialising an implicit drug entity there pushes
# "drug" into present_types, which suppresses the correct gene/disease output
# (see utility_disease.requested_target_only and utility_target gates). Drug-name
# resolution for novel biologics is handled upstream by the brand→INN normaliser
# in _REWRITE_SYSTEM plus mapIds — not by an intent hint.


def get_query_intent_hints(connection_id: str) -> list:
    """Return cached intent hints for a connection (empty list if none)."""
    return _CONN_INTENT_HINTS.get(connection_id or "", [])


def set_query_intent_hints(connection_id: str, user_input: str) -> None:
    """Called by main.py WS handler before the orchestrator runs.
    Detects output-type intent from keyword presence in the full user question
    and caches it so the interpreter can apply it even when the orchestrator
    passes only the entity name.  Uses simple keyword sets — the NER model
    remains the primary intent authority; these hints are a fallback only.
    """
    tokens = set(user_input.lower().split())
    hints: list[str] = []
    if tokens & _TARGET_KEYWORDS:
        hints.append("target")
    if tokens & _PATHWAY_KEYWORDS:
        hints.append("pathway")
    if tokens & _DISEASE_KEYWORDS:
        hints.append("disease")
    if tokens & _EXPRESSION_KEYWORDS:
        hints.append("expression")
    if tokens & _GWAS_KEYWORDS:
        hints.append("gwas")
    _CONN_INTENT_HINTS[connection_id] = hints
    _cap_conn_cache(_CONN_INTENT_HINTS)
    if hints:
        logger.info(
            f"[intent_hints] conn={connection_id} → {hints} (from '{user_input[:60]}')"
        )

# =========================================================
# Query normaliser — runs before NER to strip parenthetical aliases,
# convert trade names → INN, and collapse noisy surface forms.
# Uses the model set by OT_REWRITE_MODEL env var at temperature=0.
# =========================================================
_OT_REWRITE_MODEL = os.getenv("OT_REWRITE_MODEL", "")
if not _OT_REWRITE_MODEL:
    logging.warning("OT_REWRITE_MODEL is not set; query rewrite calls will fail at runtime.")

_REWRITE_SYSTEM = (
    "You are a biomedical query normaliser. Your ONLY task is to substitute "
    "brand/trade drug names with their INN (international nonproprietary) generic name.\n\n"
    "STRICT RULES — violating any rule is a critical error:\n"
    "1. BRAND→INN ONLY: Replace a brand/trade drug name with its INN ONLY when "
    "you are certain the term is a brand name (e.g. Gleevec→imatinib, "
    "Herceptin→trastuzumab, Keytruda→pembrolizumab, Humira→adalimumab, "
    "Imfinzi→durvalumab, Opdivo→nivolumab, Yervoy→ipilimumab, "
    "Skyrizi→risankizumab, Bimzelx→bimekizumab, Tremfya→guselkumab, "
    "OXLUMO→lumasiran, BIVV001→efanesoctocog alfa, Sunlenca→lenacapavir, "
    "Veopoz→pozelimab, Enjaymo→sutimlimab, Uplizna→inebilizumab).\n"
    "2. INN NAMES ARE ALREADY CORRECT: If the drug name is already an INN generic "
    "name (e.g. abatacept, trastuzumab, pembrolizumab, metformin), do NOT rewrite it.\n"
    "3. NEVER change gene names, disease names, pathway names, or any non-drug term.\n"
    "4. NEVER add qualifiers, adjectives, synonyms, context, or related terms "
    "(do NOT expand 'hepatitis C' to 'hepatitis C virus', do NOT add 'mutation', "
    "'receptor', 'inhibitor', 'pathway', or any other word).\n"
    "5. NEVER change the question intent — do NOT convert a pathway question into a "
    "disease question or any other change that alters what is being asked.\n"
    "6. Remove parenthetical aliases only (e.g. 'imatinib (Gleevec)' → 'imatinib').\n"
    "7. When in doubt, return the query UNCHANGED.\n\n"
    "Do NOT answer the question. Output ONLY the rewritten query string."
)

async def _rewrite_query(query: str) -> str:
    """Normalise query to standard biomedical terms before NER."""
    try:
        res = await _groq_client_resolver.chat.completions.create(
            model=_OT_REWRITE_MODEL,
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": query},
            ],
            temperature=0,
            # 512 (was 150): OT_REWRITE_MODEL is a reasoning model (gpt-oss) whose
            # reasoning tokens (~141) count against the completion budget. At 150 the
            # reasoning starved the visible output, truncating the query mid-string
            # (finish_reason='length') and silently dropping trailing entities
            # (e.g. "Which enzyme is targeted by the drug Imetelstat?" → "Which").
            max_tokens=512,
        )
        choice = res.choices[0]
        _rc = getattr(getattr(res.usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        logger.info("[rewrite cache] prompt_tokens=%s cached=%s", getattr(res.usage, "prompt_tokens", "?"), _rc)
        rewritten = (choice.message.content or "").strip()
        # Safety: keep the ORIGINAL query if the rewrite is empty, runaway-long, was
        # cut off by the token budget (finish_reason='length'), or shrank
        # substantially. The normaliser only strips parentheticals, so a big shrink
        # or a length-cutoff means a dropped entity — never trust it.
        if (
            not rewritten
            or choice.finish_reason == "length"
            or len(rewritten) > 3 * len(query)
            or len(rewritten) < 0.6 * len(query)
        ):
            return query
        return rewritten
    except Exception as e:
        logger.warning("[rewrite_query] failed (%s); using original query", e)
        return query


# =========================================================
# Combined NER + intent (LLM)
# =========================================================
async def combined_ner_and_types(query: str) -> CombinedOutput:
    logger.info(f"[combined_ner_and_types][INPUT]: {query}")

    try:
        # First attempt: fast path (low reasoning budget).
        # At temperature=0 a second identical call returns the same empty
        # output, so on failure we escalate reasoning_effort to "medium"
        # which gives the model enough budget to handle short acronyms like TNBC.
        out = await call_grok(query)
        if not out.entities:
            logger.warning("[COMBINED] NER returned empty entities; retrying with reasoning_effort=medium")
            try:
                out_retry = await call_grok(query, reasoning_effort="medium")
                if out_retry.entities:
                    out = out_retry
                # If retry also returns empty but didn't throw, keep original out
                # (preserves requested_types even when entities=[])
            except Exception as e_retry:
                logger.warning("[COMBINED] Retry with medium effort failed (%s); keeping original", e_retry)

        # Save pre-guardrail entities so interpreter can try them via mapIds
        # when all pass the string-match guardrail fails (e.g. NER expands
        # "TNBC" → "triple-negative breast cancer" which isn't in the query).
        raw_entities: List[str] = [e.strip() for e in out.entities if isinstance(e, str) and e.strip()]

        # HARD GUARDRAILS - work with lists, then create new object
        clean_entities: List[str] = []
        for e in raw_entities:
            if e.lower() in query.lower():
                clean_entities.append(e)

        # Filter requested types
        clean_requested_types = [
            t for t in out.requested_types
            if t in {"drug", "target", "disease", "mechanism_of_action", "pathway"}
        ]

        # Note: keyword-based type overrides are applied at the interpreter level
        # via _CONN_INTENT_HINTS (populated from the FULL user question in main.py),
        # NOT here — the LLM only receives the entity name from the orchestrator,
        # not the full question, so pattern-matching here would be unreliable.

        # Create NEW Pydantic object with cleaned data; include raw for fallback
        cleaned_output = CombinedOutput(
            entities=clean_entities,
            entities_raw=raw_entities,
            requested_types=clean_requested_types,
        )

        logger.info(
            "[COMBINED TOOL] entities=%s; entities_raw=%s; requested_types=%s",
            cleaned_output.entities,
            cleaned_output.entities_raw,
            cleaned_output.requested_types,
        )

        # Return the new Pydantic object
        return cleaned_output

    except Exception:
        logger.exception("[COMBINED] failed")
        return CombinedOutput(entities=[], entities_raw=[], requested_types=[])
# # =========================================================
# # Combined NER + intent (LLM)
# # =========================================================



def is_explicit_entity(e) -> bool:
    """Entity explicitly grounded from user text."""
    return (
        e.surface_form is not None
        and e.type is not None
        and e.id not in (None, "requested")
        and e.resolution_method != "implicit_request"
    )



# =========================================================
# MAIN ORCHESTRATOR TOOL (FIXED)
# =========================================================
@function_tool(
    strict_mode=False,
    name_override="interpreter",
    description_override=(
        "Resolve explicit biomedical entities via OpenTargets mapIds "
        "and ALWAYS materialize requested entity types. "
        "Pass the COMPLETE verbatim user question as user_query — never extract entities. "
        "Also pass connection_id so keyword-detected intent hints can be applied."
    ),
)
async def interpreter(user_query: str, connection_id: Optional[str] = None) -> QueryResolution:
    uq = (user_query or "").strip()
    logger.info(f"[interpreter][INPUT]: {uq}")

    # Normalise before NER: brand names → INN, strip parenthetical aliases.
    # Uses the same model as NER (_OT_NER_MODEL / gpt-oss-120b) so it has
    # biomedical knowledge to avoid rewriting INN names, disease names, or
    # gene symbols that are already in canonical form.
    uq_norm = await _rewrite_query(uq)
    if uq_norm != uq:
        logger.info("[interpreter] Query normalised: %r → %r", uq, uq_norm)
    uq = uq_norm

    combined = await combined_ner_and_types(uq)
    paraphrased_query = uq

    terms = combined.entities
    requested_types = list(combined.requested_types)

    # Apply per-connection intent hints (detected from the FULL user question
    # in main.py before the orchestrator ran — immune to orchestrator stripping).
    cached_hints = _CONN_INTENT_HINTS.get(connection_id or "", [])
    for h in cached_hints:
        if h not in requested_types:
            requested_types.append(h)
            logger.info(f"[interpreter] Applied cached hint '{h}' from conn={connection_id}")

    resolved: List[ResolvedEntity] = []

    # 1. Resolve explicit entities (ONLY what appears in text)
    for t in terms:
        resolved_entity = await open_targets_resolver(t)
        if resolved_entity.id:
            resolved.append(resolved_entity)
        else:
            # mapIds didn't resolve the term — try drug-name search as fallback
            # ONLY when drug intent is present; otherwise symptom/disease terms
            # like "fever" resolve to spurious CHEMBL compounds via search.
            drug_resolved = False
            if "drug" in requested_types:
                try:
                    drug_id, drug_name = await resolve_drug_id(t)
                    resolved.append(ResolvedEntity(
                        surface_form=t,
                        type="drug",
                        id=drug_id,
                        resolution_method="search",
                    ))
                    drug_resolved = True
                    logger.info(f"[interpreter] Drug fallback resolved '{t}' → {drug_id}")
                except Exception:
                    pass

            if not drug_resolved:
                result_tmp_unresolved = await Runner.run(agent_pathway_mechanism_classifier, t)
                message_tmp_unresolved = result_tmp_unresolved.final_output or ""
                logger.info(f"[result_tmp_unresolved output]: {message_tmp_unresolved}")

                if message_tmp_unresolved in ["pathway_name", "mechanism_of_action"]:
                    resolved.append(ResolvedEntity(
                        surface_form=t,
                        type=message_tmp_unresolved,
                        id=None,
                        resolution_method="Web")
                    )

    # 1b. Fallback for guardrail-stripped entities (e.g. NER expands "TNBC" →
    #     "triple-negative breast cancer" which fails the substring check).
    #     Try each pre-guardrail entity via mapIds; accept if it resolves and
    #     no explicit entity was already found from that NER output.
    if not any(is_explicit_entity(e) for e in resolved):
        already_tried = set(terms)
        for raw_t in combined.entities_raw:
            if raw_t in already_tried:
                continue
            already_tried.add(raw_t)
            try:
                raw_resolved = await open_targets_resolver(raw_t)
                if raw_resolved.id:
                    resolved.append(raw_resolved)
                    logger.info(
                        "[interpreter] Pre-guardrail fallback: '%s' → %s (%s)",
                        raw_t, raw_resolved.id, raw_resolved.type,
                    )
            except Exception:
                pass

    # 1c. Last-resort fallback: NER returned empty AND entities_raw is also empty
    #     (retry at reasoning_effort=medium still failed).  Extract uppercase
    #     tokens from the raw query (e.g. "TNBC", "ALK", "NO") and try mapIds
    #     on each.  mapIds is the filter — non-entities resolve to id=None and
    #     are skipped, so no hardcoded stopword list is needed.
    if not any(is_explicit_entity(e) for e in resolved) and not combined.entities_raw and uq:
        import re as _re_fallback
        acronyms = _re_fallback.findall(r'\b[A-Z][A-Z0-9]{1,5}\b', uq)
        already_tried_c = set(terms) | set(combined.entities_raw)
        for acr in acronyms:
            if acr in already_tried_c:
                continue
            already_tried_c.add(acr)
            try:
                acr_resolved = await open_targets_resolver(acr)
                if acr_resolved.id:
                    resolved.append(acr_resolved)
                    logger.info(
                        "[interpreter] Acronym fallback: '%s' → %s (%s)",
                        acr, acr_resolved.id, acr_resolved.type,
                    )
            except Exception:
                pass

    # 1d. Disease-phrase fallback: NER empty, intent hints include 'target'
    #     (i.e. user asks "which genes are associated with X?").
    #     Extract the anchor phrase after common prepositions and try OT disease
    #     search — e.g. "fever", "pyrexia" → HP_0001945.
    if not any(is_explicit_entity(e) for e in resolved) and "target" in requested_types and uq:
        import re as _re_dpf
        _prep_pattern = _re_dpf.compile(
            r'\b(?:associated with|found in|linked to|caused by|related to|in)\s+'
            r'([A-Za-z][A-Za-z0-9 \-/]+?)(?:\s*\(|\?|,|$)',
            _re_dpf.IGNORECASE,
        )
        _dpf_tried: set = set(terms) | set(combined.entities_raw)
        for m in _prep_pattern.finditer(uq):
            phrase = m.group(1).strip()
            if not phrase or phrase.lower() in _dpf_tried:
                continue
            _dpf_tried.add(phrase.lower())
            try:
                hit = await _ot.search_first_hit(phrase, "disease")
                if hit and hit.get("id"):
                    resolved.append(ResolvedEntity(
                        surface_form=phrase,
                        type="disease",
                        id=hit["id"],
                        resolution_method="search",
                    ))
                    logger.info(
                        "[interpreter] Disease-phrase fallback: '%s' → %s",
                        phrase, hit["id"],
                    )
            except Exception:
                pass

    # 2. ALWAYS materialize intent (NO suppression logic)
    for rtype in requested_types:
        resolved.append(
            ResolvedEntity(
                surface_form=None,
                type=rtype,
                id="requested",
                resolution_method="implicit_request",
            )
        )

    # Build resolution message dynamically from structured fields — no LLM call needed
    explicit_found = [e for e in resolved if is_explicit_entity(e)]
    parts = [
        f"{e.type} '{e.surface_form}' → {e.id}"
        for e in explicit_found
        if e.surface_form and e.id
    ]
    unresolved_count = sum(1 for e in resolved if e.resolution_method == "not_found")
    if unresolved_count:
        parts.append(f"{unresolved_count} term(s) unresolved")
    implicit_types = [e.type for e in resolved if e.resolution_method == "implicit_request" and e.type]
    if implicit_types:
        parts.append(f"requested types: {', '.join(implicit_types)}")
    message = "; ".join(parts) if parts else f"Resolved {len(resolved)} entities."


    explicit_entities = [e for e in resolved if is_explicit_entity(e)]

    explicit_types = {e.type for e in explicit_entities}







    # Derive requested_output from requested_types so tools don't lose intent
    # even when the orchestrator strips "requested" entities from its tool call.
    _type_to_output = {
        "drug": "drug",
        "disease": "disease",
        "target": "target",
        "pathway": "pathway",
        "mechanism_of_action": "pathway",
    }
    _output_set = {_type_to_output[t] for t in requested_types if t in _type_to_output}
    if len(_output_set) == 1:
        requested_output = next(iter(_output_set))
    elif _output_set:
        requested_output = "any"
    else:
        requested_output = None

    # Pick the anchor sub-tool. An explicit entity is the strongest signal
    # (e.g. "targets of <drug>" anchors on the drug tool). When no explicit
    # entity grounds a category, fall back to the requested_output so that
    # target-of-drug / drug-for-disease intent still routes correctly instead
    # of dropping to the web path.
    if "target" in explicit_types:
        look_up_category = "target"
    elif "drug" in explicit_types:
        look_up_category = "drug"
    elif "disease" in explicit_types:
        look_up_category = "disease"
    elif requested_output in ("drug", "disease", "target"):
        look_up_category = requested_output
    else:
        look_up_category = "web"

    # Cache requested_output by connection_id so tools can look it up even when
    # the orchestrator constructs its own QueryResolution (omitting implicit entities).
    # We do NOT include requested_output in the returned JSON — doing so caused
    # Mistral Small 4 to retry interpreter with `requested_output` as a parameter
    # (invalid), producing 3 useless interpreter loops before calling the tool.
    if connection_id:
        _CONN_REQUESTED_OUTPUT[connection_id] = requested_output
        _cap_conn_cache(_CONN_REQUESTED_OUTPUT)
        logger.info(f"[interpreter] cached requested_output={requested_output!r} for conn={connection_id}")

    out = QueryResolution(
        query=uq,
        resolved_entities=resolved,
        message=message,
        tool="interpreter",
        paraphrased_query=paraphrased_query or "",
        look_up_category=look_up_category,
        # requested_output intentionally omitted from JSON to prevent orchestrator confusion
    )

    logger.info(f"[interpreter] output: \n {out}")
    # Return a JSON string so the agents SDK passes proper JSON to the model
    # (str(Pydantic model) gives Python repr format, not JSON, which causes
    # the LLM to pass a non-parseable string to downstream tools).
    return out.model_dump_json()
