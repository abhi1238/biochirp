# readme.py

from config.guardrail import ReadmeInput, ReadmeOutput

import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("uvicorn.error")

async def run_readme(input: ReadmeInput) -> ReadmeOutput:
    """
    Returns a well-structured Markdown summary of BioChirp's key features, supported queries,
    output formats, and the out-of-scope fallback behavior (Tavily + web search to trusted sources).
    Provides a user-friendly fallback message if any error occurs.
    """
    tool = "readme"
    logger.info(f"[{tool} code] Running")

    try:
        answer = (
            "# BioChirp: Conversational Retrieval of Biomedical Knowledge\n\n"
            "BioChirp is a conversational agent optimized for **biomedical research**"
            "Ask questions in plain language; BioChirp parses your intent, expands synonyms/aliases, and retrieves structured, explainable results from curated biomedical datasets.\n\n"

            "## What BioChirp Can Do (In Scope)\n"
            "- **Entity coverage**: drugs/compounds, genes/targets, diseases/phenotypes, pathways.\n"
            "- **Multi-DB retrieval** (schema-aware):\n"
            "  - **TTD** (Therapeutic Target Database)\n"
            "  - **CTD** (Comparative Toxicogenomics Database)\n"
            "  - **HCDT** (Highly Confident Drug-Target Database)\n"
            "  - (Extensible to additional curated tables as configured)\n"
            "- **Synonym & acronym expansion**: catches alternate spellings, aliases, abbreviations.\n"
            "- **Join-aware lookup**: pulls only **relevant columns** across sources, respecting database schemas.\n"
            "- **Disambiguation**: when terms are ambiguous, BioChirp interprets context to narrow results.\n"
            "- **Structured outputs**: interactive tables in chat, concise summaries, and CSV export.\n\n"

            "### Example In-Scope Queries\n"
            "- *List approved drugs for TB.*\n"
            "- *Targets and pathways associated with BRCA1.*\n"
            "- *Mechanism of action for imatinib.*\n"
            "- *Map disease → target → drug relationships for asthma.*\n\n"

            "## Out-of-Scope? We Use Tavily + Web Search\n"
            "If your question goes beyond the curated tables (e.g., **very recent literature**, **broad reviews**, "
            "**HTS datasets**, **case reports**, or **specific clinical guidelines**), BioChirp automatically uses "
            "**Tavily** and a web search tool to gather evidence from **trusted biomedical sources**, then summarizes "
            "and cites them. Typical sources include:\n"
            "- **NCBI / PubMed** (peer-reviewed articles)\n"
            "- **Nature / Science family journals** (publisher sites)\n"
            "- **ChEMBL** (bioactivity database)\n"
            "- **DrugBank** (drug/target annotations)\n"
            "- **Open Targets** (target–disease evidence)\n"
            "- **ClinicalTrials.gov** (trial registrations)\n"
            "- **Regulatory & public health**: FDA, EMA, **WHO**, **CDC** (as applicable)\n\n"
            "**How it works**\n"
            "1) BioChirp attempts a **curated DB retrieval**.\n"
            "2) If information is missing/ambiguous/out-of-scope, it triggers **Tavily-assisted web search**.\n"
            "3) Results are **filtered for reliability**, synthesized, and **returned with citations**.\n\n"

            "## Output Formats\n"
            "- **Interactive tables** (paged preview in chat)\n"
            "- **CSV downloads** for full results\n"
            "- **Concise, evidence-linked summaries** (with citations for web-sourced answers)\n\n"

            "## Usage Tips\n"
            "- Use common names or official symbols (e.g., *\"imatinib\", \"EGFR\", \"COPD\"*). "
            "BioChirp will expand to synonyms/aliases.\n"
            "- For complex multi-hop questions, be explicit about the relation (e.g., *disease → target → drug*).\n"
            "- If you need **latest papers or guidelines**, say so; BioChirp will switch to the web-evidence route.\n\n"

            "## Examples: In-Scope vs Out-of-Scope\n"
            "- **In-scope**: *\"Show targets for BRCA1 and pathways they participate in (TTD/HCDT).\"*\n"
            "- **Out-of-scope**: *\"Summarize 2024–2025 RCTs on GLP-1 agonists for NASH with primary outcomes.\"* "
            "→ BioChirp will use **Tavily + web search** and return a citation-backed summary.\n\n"

            "## Notes & Limitations\n"
            "- BioChirp summarizes evidence and provides links so you can **inspect the sources**.\n"
            "- This system **does not provide medical advice**; consult qualified professionals for clinical decisions.\n"
        )

        msg = "Successfully finished readme tool call."
        logger.info(f"[{tool} code] Finished successfully")

        return ReadmeOutput(
            answer=answer,
            tool=tool,
            message=msg
        )

    except Exception as e:
        answer = (
            "# BioChirp: Capabilities Unavailable\n\n"
            "Sorry, an unexpected error occurred while retrieving BioChirp's feature summary. "
            "Please try again later or contact the support team if this issue persists."
        )
        msg = f"Error in readme tool: {str(e)}"
        logger.exception(f"[{tool} code] Finished with error: {msg}")

        return ReadmeOutput(
            answer=answer,
            tool=tool,
            message=msg
        )
