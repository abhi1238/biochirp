import os
import sys
import logging
import json
import ast
import asyncio
import traceback
from typing import List, Set, Optional
from pathlib import Path

from agents import Agent, Runner

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# Configuration
SEMANTIC_MATCHING_MODEL_NAME = os.getenv("SEMANTIC_MATCHING_MODEL_NAME", "gpt-4o-mini")
SEMANTIC_PROMPT_PATH = Path(os.getenv(
    "SEMANTIC_PROMPT_PATH",
    "/app/resources/prompts/semantic_match_agent.md"
))
SEMANTIC_CHUNK_SIZE = int(os.getenv("SEMANTIC_CHUNK_SIZE", "2000"))
SEMANTIC_MAX_RETRIES = int(os.getenv("SEMANTIC_MAX_RETRIES", "3"))
SEMANTIC_TIMEOUT_SEC = float(os.getenv("SEMANTIC_TIMEOUT_SEC", "60"))

# Cache for prompt and agent
_prompt_cache: Optional[str] = None
_agent_cache: Optional[Agent] = None


def load_prompt() -> str:
    """
    Lazy load semantic matching prompt with error handling.
    
    Returns:
        Prompt text
        
    Raises:
        FileNotFoundError: If prompt file doesn't exist
    """
    global _prompt_cache
    
    if _prompt_cache is not None:
        return _prompt_cache
    
    try:
        if not SEMANTIC_PROMPT_PATH.exists():
            raise FileNotFoundError(
                f"Semantic prompt file not found: {SEMANTIC_PROMPT_PATH}"
            )
        
        with open(SEMANTIC_PROMPT_PATH, "r", encoding="utf-8") as f:
            _prompt_cache = f.read()
        
        logger.info(f"Loaded semantic matching prompt from {SEMANTIC_PROMPT_PATH}")
        return _prompt_cache
        
    except Exception as e:
        logger.exception(f"Failed to load semantic matching prompt: {e}")
        raise


def get_semantic_agent() -> Agent:
    """
    Lazy load and cache semantic matching agent.
    
    Returns:
        Configured Agent instance
        
    Raises:
        Exception: If agent creation fails
    """
    global _agent_cache
    
    if _agent_cache is not None:
        return _agent_cache
    
    try:
        prompt = load_prompt()
        
        logger.info(f"Creating semantic matching agent with model: {SEMANTIC_MATCHING_MODEL_NAME}")
        
        _agent_cache = Agent(
            name="SemanticMatchAgent",
            model=SEMANTIC_MATCHING_MODEL_NAME,
            instructions=prompt,
            tools=[],
            output_type=List[str]
        )
        
        logger.info("Semantic matching agent created successfully")
        return _agent_cache
        
    except Exception as e:
        logger.exception(f"Failed to create semantic matching agent: {e}")
        raise


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
            return [str(item) for item in result if item]
    except json.JSONDecodeError:
        pass
    
    # Try ast.literal_eval (safer than eval)
    try:
        result = ast.literal_eval(text)
        if isinstance(result, (list, tuple, set)):
            return [str(item) for item in result if item]
    except (ValueError, SyntaxError):
        pass
    
    # Fallback: treat as newline-separated
    lines = text.splitlines()
    return [line.strip() for line in lines if line.strip()]


def chunk_list(data: List[str], size: int) -> List[List[str]]:
    """
    Split a list into chunks of specified size.
    
    Args:
        data: List to chunk
        size: Chunk size
        
    Returns:
        List of chunks
    """
    chunks = []
    for i in range(0, len(data), size):
        chunks.append(data[i:i + size])
    return chunks


async def find_semantic_matches(
    category: str,
    single_term: str,
    string_list: List[str],
    chunk_size: int = SEMANTIC_CHUNK_SIZE,
    max_retries: int = SEMANTIC_MAX_RETRIES,
) -> List[str]:
    """
    Find semantic matches for a single term in a list of strings using LLM.
    
    Chunks the string_list for processing and retries on failures.
    
    Args:
        category: Category/field name (e.g., "disease_name")
        single_term: Term to search for (e.g., "tuberculosis")
        string_list: List of candidate strings to search in
        chunk_size: Size of chunks for processing
        max_retries: Maximum retry attempts per chunk
        
    Returns:
        List of matched strings
    """
    logger.info(
        f"[semantic_validation] Starting semantic search for '{single_term}' "
        f"in category '{category}' with {len(string_list)} candidates"
    )
    
    # Input validation
    if not string_list:
        logger.warning(f"[semantic_validation] Empty string_list provided")
        return []
    
    if not isinstance(string_list, list):
        logger.warning(
            f"[semantic_validation] string_list is not a list: {type(string_list)}"
        )
        return []
    
    if chunk_size <= 0:
        logger.warning(f"[semantic_validation] Invalid chunk_size: {chunk_size}")
        chunk_size = SEMANTIC_CHUNK_SIZE
    
    # Get agent (cached)
    try:
        agent = get_semantic_agent()
    except Exception as e:
        logger.error(f"[semantic_validation] Failed to get agent: {e}")
        return []
    
    # Split into chunks
    chunks = chunk_list(string_list, chunk_size)
    total_chunks = len(chunks)
    
    logger.info(
        f"[semantic_validation] Processing {len(string_list)} items in "
        f"{total_chunks} chunks for '{single_term}' in {category}"
    )
    
    final_results: Set[str] = set()
    
    for idx, chunk in enumerate(chunks, start=1):
        chunk_str = str(chunk)
        prompt = (
            f"Category: {category}, "
            f"Term: {single_term}, "
            f"List of Strings: {chunk_str}"
        )
        
        logger.info(
            f"[semantic_validation] Processing chunk {idx}/{total_chunks} "
            f"({len(chunk)} items) for '{single_term}' in {category}"
        )
        
        # Retry loop for this chunk
        for attempt in range(1, max_retries + 1):
            try:
                # FIX: Add timeout
                res = await asyncio.wait_for(
                    Runner.run(agent, prompt),
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
                                f"[semantic_validation] Unexpected output type: "
                                f"{type(res.final_output)}"
                            )
                            matches = []
                    except Exception as parse_err:
                        logger.warning(
                            f"[semantic_validation] Parse error: {parse_err}"
                        )
                        matches = []
                
                final_results.update(matches)
                
                logger.info(
                    f"[semantic_validation] Chunk {idx}/{total_chunks} "
                    f"found {len(matches)} matches"
                )
                
                # Success, break retry loop
                break
                
            except asyncio.TimeoutError:
                logger.warning(
                    f"[semantic_validation] Timeout on chunk {idx}/{total_chunks}. "
                    f"Retry {attempt}/{max_retries}"
                )
                
            except Exception as e:
                tb_str = traceback.format_exc()
                logger.warning(
                    f"[semantic_validation] Error on chunk {idx}/{total_chunks}: {e}. "
                    f"Retry {attempt}/{max_retries}"
                )
                logger.debug(f"[semantic_validation] Traceback:\n{tb_str}")
        
        else:
            # All retries exhausted
            logger.error(
                f"[semantic_validation] Failed to process chunk {idx}/{total_chunks} "
                f"after {max_retries} retries"
            )
    
    result_list = list(final_results)
    
    logger.info(
        f"[semantic_validation] Finished. Found {len(result_list)} total unique matches "
        f"for '{single_term}' in {category}"
    )
    
    return result_list