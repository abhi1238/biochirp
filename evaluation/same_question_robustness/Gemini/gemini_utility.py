

with open("../../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()


async def return_gemini_answer(
    user_prompt: str,
    system_prompt=system_prompt,
    model: str = "gemini-2.5-flash-lite",
    timeout_sec: float = 5.0,
):
    from google import genai
    import asyncio
    import time
    from io import StringIO
    import pandas as pd

    client = genai.Client()
    start = time.perf_counter()

    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=user_prompt,
                config={"system_instruction": system_prompt},
            ),
            timeout=timeout_sec,
        )

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start
        raise TimeoutError(
            f"Gemini timed out after {timeout_sec:.1f}s "
            f"(elapsed={elapsed:.2f}s)"
        )

    # Only runs if successful
    elapsed = time.perf_counter() - start

    df = pd.read_csv(StringIO(answer.text))
    print(f"Entries retrieved: {df.shape[0]} | time={elapsed:.2f}s")

    return df
