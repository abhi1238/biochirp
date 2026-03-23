

import os
import sys
import warnings
import logging
import contextlib

# 1. Silence Python warnings
warnings.filterwarnings("ignore")

# 2. Silence all logging (including Streamlit)
logging.disable(logging.CRITICAL)

# 3. Force Streamlit into bare mode
os.environ["STREAMLIT_RUNTIME"] = "bare"
os.environ["STREAMLIT_SUPPRESS_CONFIG_WARNINGS"] = "1"


with open("../../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()


async def return_biochatter_answer(user_prompt, system_prompt=system_prompt, model="gpt-5-nano"):

    from openai import OpenAI, AsyncOpenAI
    from biochatter.llm_connect import GptConversation
    import time
    from io import StringIO
    from IPython.display import display
    # import response
    import pandas as pd
    import os
    import warnings


    os.environ["STREAMLIT_SUPPRESS_CONFIG_WARNINGS"] = "1"
    os.environ["STREAMLIT_RUNTIME"] = "bare"



    conversation = GptConversation(
                model_name=model,
                prompts={}
            )

    conversation.append_system_message(system_prompt)
    success = conversation.set_api_key(os.getenv("OPENAI_API_KEY"), user="my_user")



    start = time.perf_counter()

    answer, token_usage, correction = conversation.query(
        f"""Question: {user_prompt}
    """
    )

    elapsed = time.perf_counter() - start

    # answer = response.choices[0].message.content.strip().lower()

    # answer = response.output_text.strip()
    answer = pd.read_csv(StringIO(answer))
    print(f"Entries retrieved: {answer.shape[0]}")
    # print(df.head(5), "\n")
    display(answer.head(1))

    return answer