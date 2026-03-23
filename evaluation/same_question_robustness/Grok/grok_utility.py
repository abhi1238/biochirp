
with open("../../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()

async def return_grok_answer(
    user_prompt: str,
    system_prompt=system_prompt,
    model: str = "grok-4-1-fast-non-reasoning-latest",
):
    """
    Async wrapper for Grok (xAI) via OpenAI-compatible SDK.
    The SDK is synchronous ? run in a worker thread.
    """
    import os
    import time
    import asyncio
    import httpx
    from openai import OpenAI
    import pandas as pd
    from io import StringIO
    from IPython.display import display

    grok_client = OpenAI(
        api_key=os.environ.get("GROK_KEY"),
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(3600.0),
    )

    start = time.perf_counter()

    response = await asyncio.to_thread(
        grok_client.responses.create,
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        store=False,
    )

    elapsed = time.perf_counter() - start

    answer = response.output_text.strip()
    answer = pd.read_csv(StringIO(answer))
    print(f"Entries retrieved: {answer.shape[0]}")
    # print(df.head(5), "\n")
    display(answer.head(1))

    return answer

