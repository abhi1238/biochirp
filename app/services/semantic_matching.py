import logging
from typing import List
import logging
import traceback
from agents import Agent, Runner
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn.error")

md_file_path = "/app/resources/prompts/semantic_match_agent.md"

with open(md_file_path, "r", encoding="utf-8") as f:
    prompt_md = f.read()

SEMANTIC_MATCHING_MODEL_NAME = os.getenv("SEMANTIC_MATCHING_MODEL_NAME", "gpt-4o-mini")

semantic_match_agent = Agent(name="SemanticMatchAgent", model=SEMANTIC_MATCHING_MODEL_NAME, instructions=prompt_md, tools=[], output_type=List[str])

async def find_semantic_matches(
    category,
    single_term,
    string_list,
    chunk_size=2000,
    max_retries=3,
    # agent=None,
):
    def chunk_list(data, size):
        for i in range(0, len(data), size):
            yield data[i:i + size]

    final_results = set()
    chunks = list(chunk_list(string_list, chunk_size))
    total = len(chunks)

    for idx, chunk in enumerate(chunks, start=1):
        chunk_str = str(chunk)
        prompt = f"Category: {category}, Term: {single_term}, List of Strings: {chunk_str}"

        chunk_msg = f"[semantic_validation] We are performing semantic search for {single_term} in column {category} : iteration {idx}/{total}"

        # print(chunk_msg)
        logger.info(chunk_msg)

        for attempt in range(1, max_retries + 1):
            try:
                res = await Runner.run(semantic_match_agent, prompt)
                matches = []
                if res.final_output:
                    try:
                        if isinstance(res.final_output, list):
                            matches = res.final_output
                        elif isinstance(res.final_output, str):
                            matches = eval(res.final_output.strip())
                        else:
                            matches = []
                    except Exception as inner_eval_err:
                        err_msg = f"[semantic_validation] Eval error: {inner_eval_err}"
                        logger.warning(err_msg)
                        matches = []
                final_results.update(matches)
                break
            except Exception as e:
                tb_str = traceback.format_exc()
                msg = f"[semantic_validation] Agent error: {e}. Retrying ({attempt}/{max_retries})..."
                logger.warning(msg)
                logger.warning(tb_str)
        else:
            err_msg = f"[semantic_validation] Failed to process chunk {idx}/{total} after {max_retries} attempts."
            logger.error(err_msg)

    return list(final_results)