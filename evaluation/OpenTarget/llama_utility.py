

with open("../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()


async def return_llama_answer(
    user_prompt: str,
    system_prompt: str=system_prompt,
    model: str = "llama-3.3-70b-versatile",
):
    """
    Async wrapper for Groq LLaMA models.
    Groq SDK is synchronous ? run in thread.
    """
    import os
    import time
    import asyncio
    from groq import Groq
    import pandas as pd
    from io import StringIO
    from IPython.display import display
    # import response

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    start = time.perf_counter()

    reply = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    elapsed = time.perf_counter() - start

    answer = reply.choices[0].message.content.strip()

    # answer = answer.output_text.strip()
    answer = pd.read_csv(StringIO(answer))
    print(f"Entries retrieved: {answer.shape[0]}")
    # print(df.head(5), "\n")
    display(answer.head(1))

    return answer


