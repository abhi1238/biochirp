

import os
import sys
import json
import asyncio
import logging
from tavily import TavilyClient
from config.guardrail import TavilyInput, TavilyOutput

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

# Configuration
INCLUDE_DOMAINS = [
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "clinicaltrials.gov", "fda.gov", "ema.europa.eu", "who.int",
    "nejm.org", "thelancet.com", "jamanetwork.com",
    "nature.com", "sciencedirect.com", "onlinelibrary.wiley.com", "cell.com",
    "go.drugbank.com", "drugbank.com",
    "ebi.ac.uk", "drugcentral.org", "uniprot.org",
    "guidetopharmacology.org", "pharos.nih.gov", "omim.org", "orpha.net",
    "disgenet.org", "opentargets.org", "platform.opentargets.org"
]

MAX_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS", "3"))
SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "basic")
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "5000"))


def _format_tavily_results(results: dict, query: str) -> str:
    """
    Format Tavily API results into a readable string.
    
    Args:
        results: Raw Tavily API response dict
        query: Original search query
        
    Returns:
        Formatted string suitable for TavilyOutput.message
    """
    if not results or 'results' not in results:
        return "No results found from Tavily search."
    
    search_results = results.get('results', [])
    if not search_results:
        return "No results found from Tavily search."
    
    # Build formatted response
    lines = [f"Found {len(search_results)} results for: {query}\n"]
    
    for idx, result in enumerate(search_results[:MAX_RESULTS], 1):
        title = result.get('title', 'No title')
        url = result.get('url', '')
        content = result.get('content', '')[:200]  # Truncate to 200 chars
        
        lines.append(f"{idx}. {title}")
        if content:
            lines.append(f"   {content}...")
        if url:
            lines.append(f"   Source: {url}")
        lines.append("")  # Empty line between results
    
    return "\n".join(lines)


def _search_tavily_sync(client: TavilyClient, query: str) -> dict:
    """
    Synchronous wrapper for Tavily search.
    Used with asyncio.to_thread() to avoid blocking.
    """
    return client.search(
        query=query,
        max_results=MAX_RESULTS,
        include_domains=INCLUDE_DOMAINS,
        search_depth=SEARCH_DEPTH
    )


async def run_tavily(input: TavilyInput) -> TavilyOutput:
    """
    Tavily Web Search Tool with proper error handling and formatting.
    
    Args:
        input: TavilyInput containing the search query
        
    Returns:
        TavilyOutput: Formatted search results or error message
    """
    tool = "tavily"
    
    # Input validation
    if not input or not input.query:
        message = f"[{tool}] Error: Empty query provided"
        logger.warning(message)
        return TavilyOutput(message=message, tool=tool)
    
    query = input.query.strip()
    if not query:
        message = f"[{tool}] Error: Query contains only whitespace"
        logger.warning(message)
        return TavilyOutput(message=message, tool=tool)
    
    if len(query) > MAX_QUERY_LENGTH:
        message = f"[{tool}] Error: Query exceeds maximum length ({len(query)} > {MAX_QUERY_LENGTH})"
        logger.warning(message)
        return TavilyOutput(message=message, tool=tool)
    
    # Check API key
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        message = f"[{tool}] Error: TAVILY_API_KEY is not set in environment"
        logger.error(message)
        return TavilyOutput(message=message, tool=tool)

    try:
        logger.info(
            f"[{tool}] Starting search. Query: '{query[:100]}...', "
            f"max_results={MAX_RESULTS}, search_depth={SEARCH_DEPTH}"
        )
        
        # Create client
        client = TavilyClient(api_key=api_key)
        
        # FIX: Run synchronous search in thread pool to avoid blocking
        results = await asyncio.to_thread(
            _search_tavily_sync,
            client,
            query
        )
        
        # FIX: Format results as string for message field
        if results and 'results' in results and results['results']:
            message = _format_tavily_results(results, query)
            logger.info(f"[{tool}] Success: {len(results.get('results', []))} results returned")
        else:
            message = f"[{tool}] No results found for query: {query}"
            logger.info(f"[{tool}] No results returned")

    except Exception as e:
        message = f"[{tool}] Error during Tavily API call: {str(e)}"
        logger.exception(f"[{tool}] Exception details: {e}")

    logger.info(f"[{tool}] Returning output (message length: {len(message)} chars)")
    
    # Ensure output conforms to TavilyOutput schema
    return TavilyOutput(message=message, tool=tool)