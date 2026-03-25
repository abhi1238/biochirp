

import logging
import json
import os
import sys
from agents import Agent, Runner, function_tool
from config.guardrail import MemoryToolOutput

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

logger = logging.getLogger("uvicorn.error")

MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "120"))  # fallback to 120s


# ---------------- MEMORY TOOL ----------------
@function_tool(
    name_override="memory_tool",
    description_override=(
        "Memory check tool for biomedical queries (drugs, targets, genes, diseases, biomarkers, pathways). "
        "Input: JSON string with 'user_input' (current query) and 'last_5_pairs' "
        "(up to 5 prior Q/A pairs, oldest first). "
        "Decides in priority: reuse prior answer if exact same biomedical intent (RETRIEVAL), "
        "rewrite by incorporating single constraint from short fragment (<15 tokens) to most recent prior (MODIFY), "
        "or forward unchanged (PASS). "
        "Must be invoked first on every non-greeting query. "
        "Output: JSON dict with 'decision', 'message' (≤100 words, no biomedical facts), "
        "'passed_question', 'retrieved_answer' (or null), 'matched_question' (or null)."
    ),
)
async def memory_tool(input_str: str):
    # ---------- Parse input safely ----------
    try:
        input_data = json.loads(input_str)
        user_input = input_data.get("user_input", "")
    except Exception:
        input_data = {}
        user_input = ""

    # ---------- Load memory agent prompt ----------
    with open("/app/resources/prompts/agent_memory.md", "r", encoding="utf-8") as f:
        prompt_md = f.read()

    memory_agent = Agent(
        name="memory",
        instructions=prompt_md,
        tools=[],
        model="gpt-4.1-mini",
        output_type=MemoryToolOutput,  # framework may already parse to Pydantic
    )

    # ---------- Run agent ----------
    result = await Runner.run(memory_agent, input_str)
    final_output = result.final_output

    logger.info(
        "memory_tool final_output type=%s value=%r",
        type(final_output),
        final_output,
    )

    # ---------- Normalize output ----------
    try:
        # CASE 1: Already a Pydantic object
        if isinstance(final_output, MemoryToolOutput):
            return final_output

        # CASE 2: Dict-like output
        if isinstance(final_output, dict):
            return MemoryToolOutput(**final_output)

        # CASE 3: String output (JSON or plain text)
        if isinstance(final_output, str):
            text = final_output.strip()

            if text.startswith("{") and text.endswith("}"):
                output_dict = json.loads(text)
            else:
                message = text or "Empty memory output. Treating as fresh query."
                if message.startswith("Message:"):
                    message = message[len("Message:"):].strip()
                output_dict = {
                    "decision": "PASS",
                    "message": message,
                    "passed_question": user_input,
                    "retrieved_answer": None,
                    "matched_question": None,
                }

            return MemoryToolOutput(**output_dict)

        # CASE 4: Unexpected type
        return MemoryToolOutput(
            decision="PASS",
            message=f"Unexpected output type {type(final_output)}. Treating as fresh query.",
            passed_question=user_input,
            retrieved_answer=None,
            matched_question=None,
        )

    except Exception as e:
        logger.exception("memory_tool output processing failed")
        return MemoryToolOutput(
            decision="PASS",
            message=f"Error processing agent output: {str(e)}. Treating as fresh query.",
            passed_question=user_input,
            retrieved_answer=None,
            matched_question=None,
        )
