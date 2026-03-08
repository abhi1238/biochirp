
import logging
import json
import os
from typing import Dict, Any
from agents import Agent, Runner, function_tool
from config.guardrail import MemoryToolOutput

# Configuration
TOOL_NAME = "memory_tool"
MEMORY_PROMPT_PATH = "/app/resources/prompts/agent_memory.md"
MEMORY_AGENT_MODEL = os.getenv("MEMORY_AGENT_MODEL", "gpt-4.1-mini")

# Get logger (assuming logging configured in main.py)
logger = logging.getLogger(__name__)

# Load prompt once at module initialization
try:
    with open(MEMORY_PROMPT_PATH, "r", encoding="utf-8") as f:
        MEMORY_PROMPT = f.read()
    logger.info(f"[{TOOL_NAME}] Loaded prompt from {MEMORY_PROMPT_PATH}")
except Exception as e:
    logger.error(f"[{TOOL_NAME}] Failed to load prompt: {e}")
    MEMORY_PROMPT = "You are a memory assistant for biomedical queries."


def parse_input(input_str: str) -> tuple[Dict[str, Any], str]:
    """
    Parse input string and extract user_input.
    
    Returns:
        tuple: (input_data dict, user_input string)
    """
    try:
        input_data = json.loads(input_str)
        if not isinstance(input_data, dict):
            logger.warning(f"[{TOOL_NAME}] Input is not a dict, treating as plain text")
            return {}, input_str
        return input_data, input_data.get("user_input", "")
    except json.JSONDecodeError as e:
        logger.warning(f"[{TOOL_NAME}] Invalid JSON input: {e}. Treating as plain text.")
        return {}, input_str
    except Exception as e:
        logger.error(f"[{TOOL_NAME}] Unexpected error parsing input: {e}")
        return {}, ""


def normalize_output(
    final_output: Any,
    user_input: str,
    input_str: str
) -> MemoryToolOutput:
    """
    Normalize agent output to MemoryToolOutput.
    
    Args:
        final_output: Raw output from agent
        user_input: Original user query
        input_str: Raw input string for logging
        
    Returns:
        MemoryToolOutput object
    """
    try:
        # CASE 1: Already a Pydantic object
        if isinstance(final_output, MemoryToolOutput):
            logger.debug(f"[{TOOL_NAME}] Output already MemoryToolOutput")
            return final_output

        # CASE 2: Dict-like output
        if isinstance(final_output, dict):
            logger.debug(f"[{TOOL_NAME}] Converting dict to MemoryToolOutput")
            return MemoryToolOutput(**final_output)

        # CASE 3: String output (JSON or plain text)
        if isinstance(final_output, str):
            text = final_output.strip()

            if text.startswith("{") and text.endswith("}"):
                try:
                    output_dict = json.loads(text)
                    logger.debug(f"[{TOOL_NAME}] Parsed JSON string to dict")
                    return MemoryToolOutput(**output_dict)
                except json.JSONDecodeError as e:
                    logger.error(
                        f"[{TOOL_NAME}] Failed to parse JSON output: {e}. "
                        f"Text: {text[:200]}"
                    )
                    # Fall through to plain text handling
            
            # Plain text handling
            message = text or "Empty memory output. Treating as fresh query."
            if message.startswith("Message:"):
                message = message[len("Message:"):].strip()
            
            logger.debug(f"[{TOOL_NAME}] Treating as plain text, creating PASS decision")
            return MemoryToolOutput(
                decision="PASS",
                message=message,
                passed_question=user_input,
                retrieved_answer=None,
                matched_question=None,
            )

        # CASE 4: Unexpected type
        logger.warning(
            f"[{TOOL_NAME}] Unexpected output type: {type(final_output)}. "
            f"Output: {str(final_output)[:200]}"
        )
        return MemoryToolOutput(
            decision="PASS",
            message=f"Unexpected output type {type(final_output).__name__}. Treating as fresh query.",
            passed_question=user_input,
            retrieved_answer=None,
            matched_question=None,
        )

    except Exception as e:
        logger.exception(
            f"[{TOOL_NAME}] Output normalization failed. "
            f"Input: {input_str[:200]}, Output type: {type(final_output)}, "
            f"Output: {str(final_output)[:200]}"
        )
        return MemoryToolOutput(
            decision="PASS",
            message=f"Error processing memory output: {str(e)}. Treating as fresh query.",
            passed_question=user_input,
            retrieved_answer=None,
            matched_question=None,
        )


@function_tool(
    name_override=TOOL_NAME,
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
async def memory_tool(input_str: str) -> MemoryToolOutput:
    """
    Processes user queries against conversation history to determine if:
    - A previous answer can be retrieved (RETRIEVAL)
    - The query should be modified based on context (MODIFY)
    - The query should pass through unchanged (PASS)
    
    Args:
        input_str: JSON string containing 'user_input' and 'last_5_pairs'
        
    Returns:
        MemoryToolOutput with decision and relevant context
    """
    # Parse input
    input_data, user_input = parse_input(input_str)
    
    logger.info(
        f"[{TOOL_NAME}] Processing query: '{user_input[:100]}...'"
        f"{' (truncated)' if len(user_input) > 100 else ''}"
    )
    
    # Log conversation history if present
    if "last_5_pairs" in input_data:
        pairs_count = len(input_data["last_5_pairs"])
        logger.info(f"[{TOOL_NAME}] Context: {pairs_count} previous Q/A pairs")

    # Create memory agent
    memory_agent = Agent(
        name="memory",
        instructions=MEMORY_PROMPT,
        tools=[],
        model=MEMORY_AGENT_MODEL,
        output_type=MemoryToolOutput,
    )

    # Run agent
    try:
        result = await Runner.run(memory_agent, input_str)
        final_output = result.final_output
        
        logger.info(
            f"[{TOOL_NAME}] Agent output type: {type(final_output).__name__}"
        )
        logger.debug(f"[{TOOL_NAME}] Raw output: {final_output}")
        
    except Exception as e:
        logger.exception(f"[{TOOL_NAME}] Agent execution failed: {e}")
        return MemoryToolOutput(
            decision="PASS",
            message=f"Memory agent execution failed: {str(e)}. Treating as fresh query.",
            passed_question=user_input,
            retrieved_answer=None,
            matched_question=None,
        )

    # Normalize and return output
    output = normalize_output(final_output, user_input, input_str)
    
    logger.info(
        f"[{TOOL_NAME}] Decision: {output.decision}, "
        f"Message: '{output.message[:100]}...'"
        f"{' (truncated)' if len(output.message) > 100 else ''}"
    )
    
    return output