

with open("../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()

async def return_gemini_answer(
    user_prompt: str,
    system_prompt=system_prompt,
    model: str = "gemini-2.5-flash-lite",
):
    """
    Async wrapper for Gemini using google.genai (new SDK).
    Runs the synchronous client call in a thread to avoid blocking.
    """

    from google import genai
    import asyncio
    import time
    from io import StringIO
    from IPython.display import display
    import pandas as pd

    client = genai.Client()

    start = time.perf_counter()

    answer = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=user_prompt,
        config={
            "system_instruction": system_prompt,
        },
    )

    elapsed = time.perf_counter() - start


    # answer = response.output_text.strip()
    answer = pd.read_csv(StringIO(answer.text))
    print(f"Entries retrieved: {answer.shape[0]}")
    # print(df.head(5), "\n")
    display(answer.head(1))

    return answer
    # return {
    #     "model": model,
    #     "answer": response.text,
    #     "latency": elapsed,
    # }
