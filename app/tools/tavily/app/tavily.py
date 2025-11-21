# tavily.py: 

import os
import logging
from tavily import TavilyClient
from config.guardrail import TavilyInput, TavilyOutput

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

async def run_tavily(input: TavilyInput) -> TavilyOutput:
    """
    Tavily Web Search Tool (returns results as TavilyOutput, logs errors).
    """
    tool = "tavily"
    message = "Unknown error occurred."

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
    max_results: int = 3
    search_depth = "basic"
    api_key = os.environ.get("TAVILY_API_KEY")

    if not api_key:
        message = f" [{tool}] TAVILY_API_KEY is not set in environment"
        logger.error(message)
        return TavilyOutput(message=message, tool=tool)

    try:
        logger.info(f"[{tool}] Query='{input.query}', max_results={max_results}, domains={INCLUDE_DOMAINS}, search_depth={search_depth}")
        client = TavilyClient(api_key=api_key)
        results = client.search(
            query=input.query,
            max_results=max_results,
            include_domains=INCLUDE_DOMAINS,
            search_depth=search_depth
        )
        # Optionally, extract a formatted answer from results or just stringify
        if results and 'results' in results and results['results']:
            # You can customize message formatting here, if desired
            message = results  # Return full dict, or do `json.dumps(results)` if model expects str
            logger.info(f"[{tool}] [TavilySearch] {len(results.get('results', []))} results returned.")
        else:
            message = "[{tool}] No results found."
            logger.info("[{tool}] No results returned.")

    except Exception as e:
        message = f" [{tool}] [TavilySearch] Error during Tavily API call: {e}"
        logger.exception(message)

    logger.info("[{tool}]  Tavily output: {message}")

    # Ensure output conforms to TavilyOutput schema
    return TavilyOutput(message=message, tool=tool)