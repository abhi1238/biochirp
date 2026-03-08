


import logging
import asyncio
import traceback
import time
import os
import sys
import json
import ast
import httpx
from typing import List, Set, Optional
from fastapi_mcp import FastApiMCP
from openai import OpenAI
from agents import Agent, Runner
from config.guardrail import Llm_Member_Selector_Output, Llm_Member_Selector_Input

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# Load prompt
MD_FILE_PATH = "/app/resources/prompts/semantic_match_agent.md"
with open(MD_FILE_PATH, "r", encoding="utf-8") as f:
    prompt_md = f.read()

# Configuration
LLM_FILTER_MODEL_NAME = os.getenv("LLM_FILTER_MODEL_NAME", "gpt-4o-mini")
LLM_FALLBACK_MODEL_NAME = os.getenv("LLM_FALLBACK_MODEL_NAME", "gpt-4.1-mini")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-1-fast-non-reasoning-latest")
SEMANTIC_CHUNK_SIZE = int(os.getenv("SEMANTIC_CHUNK_SIZE", "2000"))
SEMANTIC_MAX_RETRIES = int(os.getenv("SEMANTIC_MAX_RETRIES", "3"))
_raw_semantic_timeout = float(
    os.getenv("SEMANTIC_TIMEOUT_SEC", os.getenv("LLM_FILTER_TIMEOUT_SEC", "60"))
)
_api_timeout_cap = float(os.getenv("LLM_FILTER_TIMEOUT_SEC", "60"))
SEMANTIC_TIMEOUT_SEC = min(_raw_semantic_timeout, _api_timeout_cap)


def safe_parse_list(text: str) -> List[str]:
    """
    Safely parse LLM output into a list without using eval().
    
    Args:
        text: String to parse
        
    Returns:
        List of parsed items
    """
    text = text.strip()
    
    # Try JSON first
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    
    # Try ast.literal_eval (safer than eval)
    try:
        result = ast.literal_eval(text)
        if isinstance(result, (list, tuple, set)):
            return list(result)
    except (ValueError, SyntaxError):
        pass
    
    # Fallback: treat as newline-separated
    return text.splitlines()


def _normalize_term(term: str) -> Optional[str]:
    if not isinstance(term, str):
        return None
    norm = term.strip().lower()
    return norm if norm else None


def _build_norm_map(strings: List[str]) -> dict:
    """
    Map normalized string -> first original occurrence.
    """
    mapping = {}
    for s in strings:
        n = _normalize_term(s)
        if n and n not in mapping:
            mapping[n] = s
    return mapping


async def find_semantic_matches(
    category: str,
    single_term: str,
    string_list: List[str],
    chunk_size: int = SEMANTIC_CHUNK_SIZE,
    max_retries: int = SEMANTIC_MAX_RETRIES,
    model_name: Optional[str] = None,
) -> Set[str]:
    """
    Find semantic matches using LLM agent with chunking and retries.
    
    Args:
        category: Category name
        single_term: Term to search for
        string_list: List of strings to search in
        chunk_size: Size of each chunk for processing
        max_retries: Maximum retry attempts per chunk
        
    Returns:
        Set of matched strings
    """
    # Input validation
    if not string_list:
        logger.warning("[semantic_validation] Empty string_list provided")
        return set()
    
    if not isinstance(string_list, list):
        logger.warning(f"[semantic_validation] string_list is not a list: {type(string_list)}")
        return set()

    # Create agent
    semantic_match_agent = Agent(
        name="SemanticMatchAgent",
        model=model_name or LLM_FILTER_MODEL_NAME,
        instructions=prompt_md,
        tools=[],
        output_type=List[str]
    )

    def chunk_list(data: List[str], size: int):
        """Split list into chunks."""
        for i in range(0, len(data), size):
            yield data[i:i + size]

    final_results = set()
    chunks = list(chunk_list(string_list, chunk_size))
    total = len(chunks)

    logger.info(
        f"[semantic_validation] Processing {len(string_list)} items in {total} chunks "
        f"for term '{single_term}' in category '{category}'"
    )

    for idx, chunk in enumerate(chunks, start=1):
        chunk_str = str(chunk)
        prompt = f"Category: {category}, Term: {single_term}, List of Strings: {chunk_str}"

        logger.info(
            f"[semantic_validation] Processing chunk {idx}/{total} "
            f"({len(chunk)} items) for '{single_term}' in {category}"
        )

        for attempt in range(1, max_retries + 1):
            try:
                # Run with timeout
                res = await asyncio.wait_for(
                    Runner.run(semantic_match_agent, prompt),
                    timeout=SEMANTIC_TIMEOUT_SEC
                )
                
                matches = []
                if res.final_output:
                    try:
                        if isinstance(res.final_output, list):
                            matches = res.final_output
                        elif isinstance(res.final_output, str):
                            # FIX: Use safe parsing instead of eval()
                            matches = safe_parse_list(res.final_output)
                        else:
                            logger.warning(
                                f"[semantic_validation] Unexpected output type: {type(res.final_output)}"
                            )
                            matches = []
                    except Exception as parse_err:
                        logger.warning(f"[semantic_validation] Parse error: {parse_err}")
                        matches = []
                
                final_results.update(matches)
                logger.info(f"[semantic_validation] Chunk {idx}/{total} found {len(matches)} matches")
                break
                
            except asyncio.TimeoutError:
                logger.warning(
                    f"[semantic_validation] Timeout on chunk {idx}/{total}. "
                    f"Retry {attempt}/{max_retries}"
                )
            except Exception as e:
                logger.warning(
                    f"[semantic_validation] Error on chunk {idx}/{total}: {e}. "
                    f"Retry {attempt}/{max_retries}"
                )
                logger.debug(traceback.format_exc())
        else:
            logger.error(
                f"[semantic_validation] Chunk {idx}/{total} failed after {max_retries} retries"
            )

    logger.info(f"[semantic_validation] Found {len(final_results)} total matches: {final_results}")
    return final_results


async def return_grok_member(
    user_prompt: str,
    model: str = GROK_MODEL
) -> Set[str]:
    """
    Calls Grok API and returns extracted items as a set.
    
    Args:
        user_prompt: Prompt to send to Grok
        model: Grok model name
        
    Returns:
        Set of matched strings (lowercased)
    """
    # Validate API key
    api_key = os.environ.get("GROK_KEY")
    if not api_key:
        logger.error("[grok] GROK_KEY not set in environment")
        return set()

    grok_client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(3600.0),
    )

    start = time.perf_counter()
    logger.info(f"[grok] Calling {model}")

    try:
        # FIX: Run synchronous call in thread pool
        response = await asyncio.to_thread(
            grok_client.responses.create,
            model=model,
            input=[
                {"role": "system", "content": prompt_md},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            store=False,
        )

        elapsed = time.perf_counter() - start
        logger.info(f"[grok] {model} completed in {elapsed:.3f}s")

        # Normalize output to a list
        text = response.output_text.strip()
        
        # FIX: Use safe parsing instead of eval()
        items = safe_parse_list(text)

        # Always lowercase and strip
        cleaned = {
            item.strip().lower() 
            for item in items 
            if isinstance(item, str) and item.strip()
        }

        logger.info(f"[grok] Found {len(cleaned)} matches: {cleaned}")
        return cleaned

    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.exception(f"[grok] Error after {elapsed:.3f}s: {e}")
        return set()


async def run_llm_member_selection_filter(
    input: Llm_Member_Selector_Input
) -> Llm_Member_Selector_Output:
    """
    Run parallel LLM member selection using both Grok and semantic matching.
    
    Args:
        input: Input containing category, single_term, and string_list
        
    Returns:
        Llm_Member_Selector_Output with combined results from both methods
    """
    logger.info(
        f"[LLM filter selection] Starting for term '{input.single_term}' "
        f"in category '{input.category}' with {len(input.string_list)} candidates"
    )
    
    # Input validation
    if not input.string_list:
        logger.warning("[LLM filter selection] Empty string_list provided")
        return Llm_Member_Selector_Output(value=[])
    
    # Build the Grok prompt
    user_prompt = (
        f"Category: {input.category}, "
        f"Term: {input.single_term}, "
        f"Strings: {input.string_list}"
    )

    # Prepare parallel tasks (Grok + Semantic)
    grok_task = return_grok_member(user_prompt)
    semantic_task = find_semantic_matches(
        category=input.category,
        single_term=input.single_term,
        string_list=input.string_list
    )
    
    # Run in parallel with error handling
    results = await asyncio.gather(grok_task, semantic_task, return_exceptions=True)
    
    # Process results with validation
    grok_matches = set()
    semantic_matches = set()
    
    # Handle Grok results
    if isinstance(results[0], Exception):
        logger.error(f"[LLM filter selection] Grok error: {results[0]}")
    elif results[0]:
        if isinstance(results[0], (list, set, tuple)):
            grok_matches = set(results[0])
        else:
            logger.warning(f"[LLM filter selection] Unexpected Grok result type: {type(results[0])}")
    
    # Handle semantic results
    if isinstance(results[1], Exception):
        logger.error(f"[LLM filter selection] Semantic matching error: {results[1]}")
    elif results[1]:
        if isinstance(results[1], set):
            semantic_matches = results[1]
        else:
            logger.warning(f"[LLM filter selection] Unexpected semantic result type: {type(results[1])}")
    
    # Combine results: case-insensitive intersection (restricted to candidates)
    norm_map = _build_norm_map(input.string_list)
    valid_norms = set(norm_map.keys())

    grok_norm = {n for m in grok_matches if (n := _normalize_term(m)) and n in valid_norms}
    semantic_norm = {n for m in semantic_matches if (n := _normalize_term(m)) and n in valid_norms}

    intersection_norm = grok_norm.intersection(semantic_norm)
    intersection = {norm_map[n] for n in intersection_norm}

    # Disputed terms (only in one model) → validate with fallback model
    disputed_norm = grok_norm.symmetric_difference(semantic_norm)
    disputed_candidates = [norm_map[n] for n in disputed_norm]

    fallback_matches = set()
    if disputed_candidates:
        logger.info(
            f"[LLM filter selection] Validating {len(disputed_candidates)} disputed terms "
            f"with fallback model '{LLM_FALLBACK_MODEL_NAME}'"
        )
        try:
            fallback_matches = await find_semantic_matches(
                category=input.category,
                single_term=input.single_term,
                string_list=disputed_candidates,
                model_name=LLM_FALLBACK_MODEL_NAME,
            )
        except Exception as e:
            logger.warning(
                f"[LLM filter selection] Fallback model failed ({LLM_FALLBACK_MODEL_NAME}): {e}. "
                f"Retrying with primary model '{LLM_FILTER_MODEL_NAME}'."
            )
            fallback_matches = await find_semantic_matches(
                category=input.category,
                single_term=input.single_term,
                string_list=disputed_candidates,
                model_name=LLM_FILTER_MODEL_NAME,
            )

    fallback_norm = {n for m in fallback_matches if (n := _normalize_term(m)) and n in valid_norms}
    fallback = {norm_map[n] for n in fallback_norm}

    combined = intersection.union(fallback)

    
    logger.info(
        f"[LLM filter selection] Finished. "
        f"Grok: {len(grok_matches)}, Semantic: {len(semantic_matches)}, "
        f"Intersection: {len(intersection)}, Fallback: {len(fallback)}, "
        f"Combined: {len(combined)} matches"
    )
    
    return Llm_Member_Selector_Output(value=list(combined))
