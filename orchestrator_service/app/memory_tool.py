

import logging
import json
from agents import Agent, Runner, function_tool
from config.guardrail import MemoryToolOutput
import os
import sys

# --- LOGGING SETUP: Place at startup (main.py or as early as possible) ---
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger("uvicorn.error")


# # logger = logging.getLogger("uvicorn.error")
# logger = logging.getLogger("uvicorn.error")
# logger.setLevel(logging.INFO)  # Or logging.DEBUG for more details

# # Add handler to send logs to stdout
# stdout_handler = logging.StreamHandler(sys.stdout)
# stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)

# Optional: Custom format with timestamps
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# stdout_handler.setFormatter(formatter)


MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))  # fallback to 60s if not set

# Update the memory_tool to use structured input/output
@function_tool(
    name_override="memory_tool",
    description_override=(
        "Memory check tool for biomedical queries (drugs, targets, genes, diseases, biomarkers, pathways). Input: JSON string with 'user_input' (current query) and 'last_5_pairs' (up to 5 prior Q/A pairs, oldest first). Decides in priority: reuse prior answer if exact same biomedical intent (RETRIEVAL), rewrite by incorporating single constraint from short fragment (<15 tokens) to most recent prior (MODIFY), or forward unchanged (PASS). Must be invoked first on every query. Output: JSON dict with 'decision', 'message' (≤100 words explanation, no biomedical facts), 'passed_question', 'retrieved_answer' (or null), 'matched_question' (or null). See prompt for rules, synonyms, and examples."
    ),
)
async def memory_tool(input_str: str):
    # Parse input early for defaults
    try:
        input_data = json.loads(input_str)
        user_input = input_data.get('user_input', '')
    except json.JSONDecodeError:
        user_input = ''
        input_data = {}

    with open("/app/resources/prompts/agent_memory.md", "r", encoding="utf-8") as f:
        prompt_md = f.read()
    
    memory_agent = Agent(
        name="memory",
        instructions=prompt_md,
        tools=[],
        model="gpt-4o-mini",
        output_type=None,  # Consider changing to 'json' if framework supports
        # Optional: Enforce JSON if Agent allows additional kwargs
        # additional_kwargs={"response_format": {"type": "json_object"}}
    )
    
    result = await Runner.run(memory_agent, input_str)
    
    final_output = result.final_output.strip() if result.final_output else ""
    
    try:
        # If it looks like JSON, parse it
        if final_output.startswith('{') and final_output.endswith('}'):
            output_dict = json.loads(final_output)
        else:
            # Handle partial/non-JSON output (e.g., just "Message: ...")
            message = final_output if final_output else "Error: Empty agent output. Treating as fresh query."
            if message.startswith("Message: "):
                message = message[len("Message: "):].strip()
            output_dict = {
                "decision": "PASS",
                "message": message,
                "passed_question": user_input,
                "retrieved_answer": None,
                "matched_question": None
            }
        

        return MemoryToolOutput(**output_dict)

    except Exception as e:
        # Ultimate fallback on any error
        output_dict =  {
            "decision": "PASS",
            "message": f"Error processing agent output: {str(e)}. Treating as fresh query.",
            "passed_question": user_input,
            "retrieved_answer": None,
            "matched_question": None
        }

        return MemoryToolOutput(**output_dict)
