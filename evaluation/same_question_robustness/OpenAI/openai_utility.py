



with open("../../../resources/prompts/llm_table_generator.md", "r", encoding="utf-8") as f:
    system_prompt = f.read()


async def return_openai_answer(user_prompt, system_prompt=system_prompt, model="gpt-5-nano"):

    from openai import OpenAI, AsyncOpenAI
    import time
    from io import StringIO
    from IPython.display import display
    # import response
    import pandas as pd

    client = AsyncOpenAI()

    start = time.perf_counter()

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    elapsed = time.perf_counter() - start

    answer = response.choices[0].message.content.strip().lower()

    # answer = response.output_text.strip()
    answer = pd.read_csv(StringIO(answer))
    print(f"Entries retrieved: {answer.shape[0]}")
    # print(df.head(5), "\n")
    display(answer.head(1))

    return answer


# answer = response.choices[0].message.content.strip().lower()

