
import os
import time
import asyncio
from dotenv import load_dotenv
import json
from typing import Dict, Any, Optional
from openai import OpenAI




load_dotenv()

# =========================
# MODEL CONFIG
# =========================
MODEL_CONFIG = {
    "llama": {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    },
    "openai": {
        "provider": "openai",
        "model": "gpt-5-nano",
    },
    "gemini": {
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
    },
    "grok": {
        "provider": "grok",
        "model": "grok-4-1-fast-non-reasoning-latest",
    },
}

# =========================
# PROVIDER CALLS
# =========================

async def call_groq(user_prompt, system_prompt, model):
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    start = time.perf_counter()
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
    )
    return {
        "answer": resp.choices[0].message.content.strip(),
        "latency": time.perf_counter() - start,
    }

async def call_openai(user_prompt, system_prompt, model):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    start = time.perf_counter()
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # temperature=0.0,
    )
    return {
        "answer": resp.choices[0].message.content.strip(),
        "latency": time.perf_counter() - start,
    }

async def call_gemini(user_prompt, system_prompt, model):
    from google import genai
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    start = time.perf_counter()
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=user_prompt,
        config={
            "system_instruction": system_prompt,
            "temperature": 0.0,
        },
    )
    return {
        "answer": resp.text.strip(),
        "latency": time.perf_counter() - start,
    }

async def call_grok(user_prompt, system_prompt, model):
    from openai import OpenAI
    import httpx

    api_key = (
        os.getenv("XAI_API_KEY")
        or os.getenv("GROK_API_KEY")
        or os.getenv("GROK_KEY")
    )

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(600.0, connect=60.0),
    )

    start = time.perf_counter()
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
    )
    return {
        "answer": resp.choices[0].message.content.strip(),
        "latency": time.perf_counter() - start,
    }

# =========================
# MAIN MULTI-MODEL WRAPPER
# =========================

async def run_all_models(
    user_prompt: str,
    system_prompt: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns:
        {
            "llama":  { "model": ..., "answer": ..., "latency": ... },
            "openai": { ... },
            "gemini": { ... },
            "grok":   { ... }
        }
    """

    results = {}

    tasks = {
        "llama": call_groq(
            user_prompt,
            system_prompt,
            MODEL_CONFIG["llama"]["model"],
        ),
        "openai": call_openai(
            user_prompt,
            system_prompt,
            MODEL_CONFIG["openai"]["model"],
        ),
        "gemini": call_gemini(
            user_prompt,
            system_prompt,
            MODEL_CONFIG["gemini"]["model"],
        ),
        "grok": call_grok(
            user_prompt,
            system_prompt,
            MODEL_CONFIG["grok"]["model"],
        ),
    }

    completed = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for key, output in zip(tasks.keys(), completed):
        if isinstance(output, Exception):
            results[key] = {
                "model": MODEL_CONFIG[key]["model"],
                "error": str(output),
            }
        else:
            results[key] = {
                "model": MODEL_CONFIG[key]["model"],
                **output,
            }

    return results



def _compact_candidates(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Keep only what the judge needs: provider key, model, answer/error.
    """
    out: Dict[str, Any] = {}
    for k, v in results.items():
        out[k] = {
            "model": v.get("model"),
            "answer": v.get("answer"),
            "error": v.get("error"),
        }

        print(out[k])
    return out


async def biochirp_agent_output(
    user_prompt: str,
    system_prompt: str,
    # low_cost_model_results: Dict[str, Dict[str, Any]],
    judge_model: str = "gpt-4.1-mini",
    # max_output_tokens: int = 10000,
) -> str:
    """
    Orchestrator/Judge:
    - Inputs: user_prompt, the SAME rewrite_system_prompt you gave cheap models,
              and low_cost_results from run_all_models()
    - Output: ONE final sentence rewrite (string).
    - Uses web_search tool when necessary for ambiguity/role clarification only.
    """

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    low_cost_model_results = await run_all_models(
    user_prompt=user_prompt,
    system_prompt=system_prompt)


    candidates = _compact_candidates(low_cost_model_results)

    judge_instructions = f"""

        You are the Orchestrator and Final Judge for a multi-model decision pipeline.

        You will be given:
        1) The user's original input
        2) A SYSTEM POLICY that defines how the final answer must be produced
        3) Candidate outputs from multiple supporting models (may be incomplete, inconsistent, or incorrect)

        Your job:
        - Produce the SINGLE best final answer that strictly follows the SYSTEM POLICY.
        - You are NOT required to copy any candidate output.
        - You may combine, correct, or discard candidate outputs as needed.
        - You MUST NOT hallucinate facts, values, entities, or constraints.
        - If the SYSTEM POLICY specifies how to handle missing or unspecified values, you MUST follow that rule exactly.

        Tool usage:
        - You MAY use web_search only when necessary to verify definitions, resolve ambiguity, or validate factual correctness.
        - Web search MUST NOT be used to invent new constraints, assumptions, or implied values.
        - Any information used from tools must already be consistent with the SYSTEM POLICY.

        Output requirements:
        - Produce ONLY the final answer.
        - No explanations, no reasoning, no metadata, no citations.
        - Follow the format, structure, and constraints defined in the SYSTEM POLICY exactly.


"""

    # Pack everything as model-readable JSON inside the input (safe & deterministic)
    payload = {
        "user_prompt": user_prompt,
        "rewrite_system_prompt": system_prompt,
        "candidate_outputs": candidates,
        "decision_rules": {
            "must_follow_rewrite_system_prompt": True,
            "not_bound_to_candidates": True,
            "web_search_only_for_role_clarification": True,
            "output_exactly_one_sentence": True,
        },
    }

    resp = client.responses.create(
        model=judge_model,
        input=[
            {"role": "system", "content": judge_instructions},
            {"role": "user", "content": "Decide the best final rewrite.\n\nDATA:\n" + json.dumps(payload, indent=2)},
        ],
        # Enable built-in web search tool (agent decides whether to call it)
        tools=[{"type": "web_search"}],
        # Keep it deterministic
        # temperature=0.0,
        # max_output_tokens=max_output_tokens,
    )

    # The SDK provides resp.output_text as the assistant's final text output
    final_text = (resp.output_text or "").strip()

    # Optional hard guardrail: ensure it's one line / one sentence-ish
    final_text = " ".join(final_text.split())
    return final_text



