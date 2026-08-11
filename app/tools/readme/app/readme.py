# readme.py

import os
import sys
import logging
from pathlib import Path
from textwrap import dedent
from config.guardrail import ReadmeInput, ReadmeOutput

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

# README content path (option 1: load from file)
README_PATH = Path(os.getenv("README_FILE_PATH", "/app/resources/readme/BIOCHIRP_README.md"))

# Option 2: Keep in code but use dedent for clean formatting
DEFAULT_README = dedent("""
    # **BioChirp — Conversational Retrieval for Biomedical Knowledge**

    **BioChirp** is a conversational assistant designed for **biomedical research**.
    Ask questions in plain language—BioChirp interprets your intent, expands synonyms and aliases, and retrieves **structured, explainable results** from curated biomedical databases.

    ---

    ## 🛠 **Core Tools & Microservices**

    * **Interpreter** Extracts and normalizes biomedical entities from queries
    * **Memory** Maintains prior question answer context
    * **Planner** Optimizes table joins and filter logic
    * **Synonym Expansion & Matching** Alias expansion, fuzzy matching, and embedding-based similarity constrained to database values
    * **Databases**:
      * **TTD** Therapeutic Target Database
      * **CTD** Comparative Toxicogenomics Database
      * **HCDT** Highly Confident Drug-Target Database
      * **Web & Tavily Search** Evidence-backed retrieval from external biomedical sources when needed

    ---

    ## ✅ **What BioChirp Can Answer (In Scope)**

    * Drugs, genes/targets, diseases, pathways, biomarkers
    * Cross-database queries with schema-aware joins
    * Synonym and acronym expansion
    * Disambiguation of ambiguous biomedical terms
    * Structured outputs: tables, summaries, CSV export

    ### Example In-Scope Questions

    * *Drugs for tuberculosis*
    * *Targets and pathways associated with BRCA1*
    * *Mechanism of action of imatinib*
    * *Biomarkers linked to cancer*

    ---

    ## 🌐 **Out-of-Scope Queries → Web Evidence**

    For questions beyond curated databases (e.g., **recent papers, clinical guidelines, trials**), BioChirp automatically uses **Tavily + web search** to retrieve evidence from trusted sources such as **PubMed, Nature, DrugBank, Open Targets, ClinicalTrials.gov, FDA, EMA, and WHO**, with **citations included**.

    ---

    ## 📤 **Output Formats**

    * Interactive tables in chat
    * CSV downloads
    * Concise, citation-backed summaries

    ---

    ## 💡 **Usage Tips**

    * Use common names or official symbols (e.g., *EGFR*, *imatinib*, *COPD*)
    * Be explicit for multi-hop queries (e.g., *disease → target → drug*)
    * Ask for "latest studies" or "recent guidelines" to trigger web-based evidence

    ---

    ## ⚠️ **Notes**

    BioChirp supports research and exploration only and **does not provide medical advice**.
""").strip()


async def run_readme(input: ReadmeInput) -> ReadmeOutput:
    """
    Returns a well-structured Markdown summary of BioChirp's key features, supported queries,
    output formats, and the out-of-scope fallback behavior (Tavily + web search to trusted sources).
    Provides a user-friendly fallback message if any error occurs.
    
    Args:
        input: ReadmeInput containing the query (not used for README retrieval)
        
    Returns:
        ReadmeOutput: README content or error message
    """
    tool = "readme"
    
    # Basic input validation
    if not input:
        logger.warning(f"[{tool} code] None input received")
        return ReadmeOutput(
            answer="# Error\nInvalid request.",
            tool=tool,
            message="Error: No input provided"
        )
    
    logger.info(f"[{tool} code] Running. Query: '{input.query}'")

    try:
        # Option 1: Load from file (preferred for easy updates)
        if README_PATH.exists():
            answer = README_PATH.read_text(encoding="utf-8").strip()
            logger.info(f"[{tool} code] Loaded README from {README_PATH}")
        else:
            # Option 2: Use default content from constant
            answer = DEFAULT_README
            logger.info(f"[{tool} code] Using default README (file not found at {README_PATH})")
        
        msg = "Successfully retrieved README content"
        logger.info(f"[{tool} code] Finished successfully")

        return ReadmeOutput(
            answer=answer,
            tool=tool,
            message=msg
        )

    except Exception as e:
        answer = dedent("""
            # BioChirp: Capabilities Unavailable

            Sorry, an unexpected error occurred while retrieving BioChirp's feature summary.
            Please try again later or contact the support team if this issue persists.
        """).strip()
        
        msg = f"Error in readme tool: {str(e)}"
        logger.exception(f"[{tool} code] Error: {msg}")

        return ReadmeOutput(
            answer=answer,
            tool=tool,
            message=msg
        )