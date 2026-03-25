import logging
import json
import os
import sys
from agents import Agent, Runner, function_tool, WebSearchTool
from config.guardrail import BioChirpClassification


# ---------------- LOGGING SETUP ----------------
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("biochirp.router")

MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "120"))  # fallback to 120s


@function_tool(
    name_override="router_tool",
    description_override=(
        "Deterministic query classification and justification tool for the BioChirp system. "
        "Input: a single raw user query string. "
        "Classifies the query into exactly ONE predefined category based strictly on the query's "
        "explicit content and intent, without answering, rewriting, normalizing, or inferring "
        "entities, relationships, or missing intent. "
        "Categories include: README_RETRIEVAL (queries about BioChirp capabilities, scope, tools, or documentation), "
        "BIOCHIRP_STRUCTURED_RETRIEVAL (biomedical queries answerable by direct structured lookup over drugs, "
        "targets/genes, diseases, pathways, biomarkers, drug-target mechanisms, or approval status), "
        "BIOMEDICAL_REASONING_REQUIRED (queries requiring explanation, inference, comparison, or causal reasoning), "
        "BIOMEDICAL_OUT_OF_SCOPE_FOR_RETRIEVAL (biomedical queries outside structured retrieval such as diagnostics, "
        "lab values, physiology, staging, procedures, or guidelines), "
        "NON_BIOMEDICAL, and UNCLASSIFIABLE_OR_OTHER. "
        "Classification follows a strict priority order and defaults to the most conservative category when uncertain. "
        "Output: a JSON dictionary with exactly two keys: "
        "'decision' (one uppercase category label) and "
        "'message' (a brief 2–4 sentence rule-based explanation with no biomedical facts or query restatement)."
    ),
)
async def router_tool(input_str: str):

    # ---------- Load router prompt ----------
    with open("/app/resources/prompts/router.md", "r", encoding="utf-8") as f:
        prompt_md = f.read()


        # with open("/app/resources/prompts/agent_memory.md", "r", encoding="utf-8") as f:
        # prompt_md = f.read()

    # ---------- Configure agent ----------
    router_agent = Agent(
        name="router_tool",
        instructions=prompt_md,
        tools=[WebSearchTool()],  # IMPORTANT: deterministic classifier → no tools
        model="gpt-4.1-mini",
        output_type=BioChirpClassification,
    )

    # ---------- Run agent ----------
    result = await Runner.run(router_agent, input_str)
    final_output = result.final_output

    logger.info(
        "router_tool final_output type=%s value=%r",
        type(final_output),
        final_output,
    )

    # ---------- Normalize output ----------
    try:
        # CASE 1: Already a valid Pydantic object
        if isinstance(final_output, BioChirpClassification):
            return final_output

        # CASE 2: Dict-like output
        if isinstance(final_output, dict):
            return BioChirpClassification(**final_output)

        # CASE 3: String output (JSON or plain text)
        if isinstance(final_output, str):
            text = final_output.strip()

            if text.startswith("{") and text.endswith("}"):
                output_dict = json.loads(text)
                return BioChirpClassification(**output_dict)

            # Plain text fallback → conservative classification
            message = text or "Empty router output. Defaulting to UNCLASSIFIABLE_OR_OTHER."
            return BioChirpClassification(
                decision="UNCLASSIFIABLE_OR_OTHER",
                message=message,
            )

        # CASE 4: Unexpected output type
        return BioChirpClassification(
            decision="UNCLASSIFIABLE_OR_OTHER",
            message=f"Unexpected output type {type(final_output)}. Defaulting to UNCLASSIFIABLE_OR_OTHER.",
        )

    except Exception as e:
        logger.exception("router_tool output processing failed")
        return BioChirpClassification(
            decision="UNCLASSIFIABLE_OR_OTHER",
            message=f"Error processing router output: {str(e)}. Defaulting to UNCLASSIFIABLE_OR_OTHER.",
        )
