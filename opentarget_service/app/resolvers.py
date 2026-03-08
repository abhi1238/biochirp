import pandas as pd
from typing import List, Dict, Any, Tuple, Set, Optional
from .guard_rail import ResolvedEntity, QueryResolution, CombinedOutput
from .client import OTGraphQLClient
from .config import OTClientConfig
from .graphql import DRUG_KNOWN_DISEASES_QUERY, DRUG_MOA_QUERY
from .dataframe import empty_df, ensure_cols
from .uvicorn_logger import setup_logger
from agents import Agent, Runner, function_tool, WebSearchTool
import logging

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



async def resolve_drug_id(drug_name_or_id: str) -> Tuple[str, str]:
    t = (drug_name_or_id or "").strip()
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
    t = (target_symbol_or_id or "").strip()
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
    t = (disease_name_or_id or "").strip()
    if not t:
        raise ValueError("disease_name_or_id is empty")

    if t.upper().startswith(("EFO_", "MONDO_")):
        did = t.upper()
        q = "query($id:String!){disease(efoId:$id){id name}}"
        d = (await _ot.run(q, {"id": did})).get("disease") or {}
        if not d:
            hit = await _ot.search_first_hit(t, "disease")
            if not hit:
                return did, None
            return hit["id"], hit.get("name")
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
    t = (term or "").strip()

    if not t:
        return ResolvedEntity(
            surface_form=None,
            type=None,
            id=None,
            resolution_method="not_found",
        )

    try:
        data = await _ot.run(
            """
            query ($terms:[String!]!) {
              mapIds(queryTerms:$terms) {
                mappings {
                  hits {
                    id
                    entity
                  }
                }
              }
            }
            """,
            {"terms": [t]},
        )

        hits: List[Dict[str, Any]] = []
        for m in (data.get("mapIds") or {}).get("mappings", []):
            hits.extend(m.get("hits") or [])

        if not hits:
            return ResolvedEntity(
                surface_form=t,
                type=None,
                id=None,
                resolution_method="not_found",
            )

        # Deterministic pick: first hit (OpenTargets already ranks)
        best = hits[0]

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


with open("/app/resources/prompts/clarifier_agent.md", "r", encoding="utf-8") as f:
    prompt_md_clarifier = f.read()

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

SEARCH POLICY:
You MAY use web search ONLY if terms are completely unfamiliar. If search doesn't clearly resolve category, output: null

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
    model="gpt-4.1-mini",
    instructions=PATHWAY_MECHANISM_CLASSIFIER,
    output_type=str,
    tools = [WebSearchTool()]
)


# if not prompt_md:
#     message = "Summarization skipped (no prompt loaded)"
# else:


async def call_grok(user_prompt: str, model="grok-4-1-fast-non-reasoning-latest") -> CombinedOutput:
    import httpx
    from openai import OpenAI
    import time
    import os
    import asyncio
    import json
    from pydantic import ValidationError

    client = OpenAI(
        api_key=os.environ["GROK_KEY"],
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(300.0),
    )

    start = time.perf_counter()

    res = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": prompt_md_entity_extractor},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    # Parse and validate with Pydantic
    try:
        json_str = res.choices[0].message.content.strip()
        
        # Remove markdown code blocks if present
        if json_str.startswith("```json"):
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif json_str.startswith("```"):
            json_str = json_str.split("```")[1].split("```")[0].strip()
        
        # Parse JSON
        data = json.loads(json_str)
        
        # Validate and create Pydantic model
        return CombinedOutput(**data)
        
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error(f"[GROK] Failed to parse output: {e}")
        logger.error(f"[GROK] Raw output: {res.choices[0].message.content}")
        return CombinedOutput(entities=[], requested_types=[])



# =========================================================
# Combined NER + intent (LLM)
# =========================================================
async def combined_ner_and_types(query: str) -> CombinedOutput:
    logger.info(f"[combined_ner_and_types][INPUT]: {query}")

    try:
        # Get Pydantic object from Grok
        out = await call_grok(query)
        
        # HARD GUARDRAILS - work with lists, then create new object
        clean_entities: List[str] = []
        for e in out.entities:
            if isinstance(e, str) and e.strip() and e.lower() in query.lower():
                clean_entities.append(e.strip())

        # Filter requested types
        clean_requested_types = [
            t for t in out.requested_types 
            if t in {"drug", "target", "disease", "mechanism_of_action", "pathway"}
        ]

        # Create NEW Pydantic object with cleaned data
        cleaned_output = CombinedOutput(
            entities=clean_entities,
            requested_types=clean_requested_types
        )

        logger.info(
            "[COMBINED TOOL] entities=%s; requested_types=%s",
            cleaned_output.entities,
            cleaned_output.requested_types,
        )

        # Return the new Pydantic object
        return cleaned_output

    except Exception:
        logger.exception("[COMBINED] failed")
        return CombinedOutput(entities=[], requested_types=[])
# # =========================================================
# # Combined NER + intent (LLM)
# # =========================================================



# =========================================================
# Resolution message
# =========================================================
def build_resolution_message(terms: List[str], resolved: List[ResolvedEntity]) -> str:
    named_ok = sum(
        1 for r in resolved
        if r.id and r.resolution_method != "implicit_request"
    )
    requested = [
        r.type for r in resolved
        if r.resolution_method == "implicit_request"
    ]
    return (
        f"Detected terms={terms}; "
        f"resolved_named={named_ok}/{len(terms)}; "
        f"requested_types={requested}."
    )

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
    name_override="interpreter",
    description_override=(
        "Resolve explicit biomedical entities via OpenTargets mapIds "
        "and ALWAYS materialize requested entity types."
    ),
)
async def interpreter(user_query: str) -> QueryResolution:
    uq = (user_query or "").strip()
    logger.info(f"[interpreter][INPUT]: {uq}")

    paraphraser_agent = Agent(
        name="Query paraphraser",
        model="gpt-4.1-mini",
        instructions=prompt_md_clarifier,
        output_type=str,
    )

    paraphrased_query = await Runner.run(paraphraser_agent, uq)
    paraphrased_query = paraphrased_query.final_output

    logger.info(f"[Query paraphraser] : {paraphrased_query}")

    combined = await combined_ner_and_types(paraphrased_query)

    terms = combined.entities
    requested_types = combined.requested_types

    resolved: List[ResolvedEntity] = []

    # 1. Resolve explicit entities (ONLY what appears in text)
    for t in terms:
        resolved_entity = await open_targets_resolver(t)
        if resolved_entity.id:
            resolved.append(resolved_entity)
        else:
            # tmp_unresolved = t #open_targets_resolver(t).surface_form

            result_tmp_unresolved = await Runner.run(agent_pathway_mechanism_classifier, t)
            
            # logger.info(f"[result_tmp_unresolved input]: {t}")
            
            message_tmp_unresolved = result_tmp_unresolved.final_output or ""

            logger.info(f"[result_tmp_unresolved output]: {message_tmp_unresolved}")


            if message_tmp_unresolved in ["pathway_name", "mechanism_of_action"]:

                resolved.append(ResolvedEntity(
                    surface_form=t,
                    type=message_tmp_unresolved,
                    id=None,
                    resolution_method="Web")
                )
        
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

    msg = build_resolution_message(terms, resolved)

    # logger.info(f"[Query Resolution]: {QueryResolution(
    #     query=uq,
    #     resolved_entities=resolved,
    #     message=msg,
    #     tool='interpreter',
    #     paraphrased_query=paraphrased_query
        
    # )}")

    out = QueryResolution(
        query=uq,
        resolved_entities=resolved,
        message=msg,
        tool="interpreter",
        paraphrased_query=paraphrased_query
        
    )

#     qr = QueryResolution(
#     query=uq,
#     resolved_entities=resolved,
#     message=msg,
#     tool="interpreter",
#     paraphrased_query=paraphrased_query,
# )

    logger.info(f"[Query Resolution]: {out}")

    # with open("/app/resources/prompts/opentarget_resolver_summarizer.md", "r", encoding="utf-8") as f:
    #     prompt_md = f.read()
    # try:
    with open("/app/resources/prompts/opentarget_resolver_summarizer.md", "r", encoding="utf-8") as f:
        prompt_md_summarizer = f.read()
    # except Exception as exc:
    #     logger.error("Failed to load summarizer prompt: %s", exc)
    #     prompt_md = ""


    agent_summarizer = Agent(
        name="Summarizer",
        model="gpt-4.1-nano",
        instructions=prompt_md_summarizer,
        output_type=str,
    )


    # if not prompt_md:
    #     message = "Summarization skipped (no prompt loaded)"
    # else:
    result = await Runner.run(agent_summarizer, str(out))
    message = result.final_output or ""


    explicit_entities = [e for e in resolved if is_explicit_entity(e)]
    
    explicit_types = {e.type for e in explicit_entities}

    if "target" in explicit_types:
        look_up_category = "target"
    elif "drug" in explicit_types:
        look_up_category = "drug"
    elif "disease" in explicit_types:
        look_up_category = "disease"

    else:
        look_up_category = "web"







    # look_up_category

    out = QueryResolution(
        query=uq,
        resolved_entities=resolved,
        message=message,
        tool="interpreter",
        paraphrased_query=paraphrased_query or "",
        look_up_category=look_up_category
    )

    logger.info(f"[interpreter] output: \n {out}")
    return out
