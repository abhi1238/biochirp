"""Route `openai/*` model ids directly to the OpenAI portal (api.openai.com).

Ops policy (2026-06-20): OpenAI models are served/billed through the first-party
OpenAI API using OPENAI_API_KEY — NOT via OpenRouter, and NOT via the litellm
proxy that OPENAI_BASE_URL points at. Non-OpenAI models (gemini, llama, …) keep
their existing OpenRouter/Groq routing.

OpenRouter ids look like `openai/gpt-4o-mini`; the OpenAI API wants the bare
`gpt-4o-mini`, so `api_model()` strips the `openai/` prefix.
"""
import os

import httpx
import openai

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Force the real OpenAI portal. Do NOT use OPENAI_BASE_URL (it points at litellm);
# override only via OPENAI_DIRECT_BASE_URL if ever needed.
_OPENAI_DIRECT_BASE = os.getenv("OPENAI_DIRECT_BASE_URL", "https://api.openai.com/v1")
_PREFIX = "openai/"
_client: "openai.OpenAI | None" = None

# 2026-06-26 incident: schema_kg clients had no timeout, so a hung OpenRouter
# response (observed on gemini-2.5-flash-lite / llama-4-maverick, which sits in
# front of an occasionally-overloaded Google backend) blocked for the openai
# SDK's 600s default, freezing the whole (serial) question loop. Every client
# built for schema_kg — Groq, OpenRouter, and this OpenAI-direct one — must use
# this timeout so a stuck upstream fails fast instead of hanging the pipeline.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)
DEFAULT_MAX_RETRIES = 1


def make_client(api_key: str, base_url: str) -> "openai.OpenAI":
    """Build an openai.OpenAI client with the shared schema_kg timeout policy."""
    return openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
    )


def is_openai(model: str) -> bool:
    """True only for OpenAI *portal* models (gpt-4o, gpt-5, o-series).

    NOT for `openai/gpt-oss-*`: those are open-weight models served by Groq/etc.
    whose id merely starts with `openai/` — they must route via their provider
    (e.g. SCHEMA_KG_GROQ_MODELS), keeping the full id, not the OpenAI portal.
    """
    return isinstance(model, str) and model.startswith(_PREFIX) and "gpt-oss" not in model


def api_model(model: str) -> str:
    """`openai/gpt-4o-mini` → `gpt-4o-mini` (bare id for the OpenAI portal).

    Left unchanged for non-portal ids (e.g. `openai/gpt-oss-120b` on Groq keeps
    its full id; OpenRouter ids keep theirs)."""
    return model[len(_PREFIX):] if is_openai(model) else model


def extra_create_kwargs(model: str) -> dict:
    """Model-specific extra create() params.

    gpt-oss is a reasoning model — without a low reasoning budget it can spend
    the whole max_tokens on reasoning and return empty content. `reasoning_effort`
    is honored by Groq (and OpenAI o-series); harmless to omit elsewhere.
    """
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    return {}


def get_client() -> "openai.OpenAI":
    global _client
    if _client is None:
        _client = make_client(_OPENAI_API_KEY, _OPENAI_DIRECT_BASE)
    return _client


def token_kwargs(model: str, n: int) -> dict:
    """Output-token cap kwarg, model-aware.

    GPT-5 and o-series reject `max_tokens` and require `max_completion_tokens`;
    every other model (gemini/llama via OpenRouter, gpt-4o-mini) uses `max_tokens`.
    Spread into `chat.completions.create(..., **token_kwargs(model, n))`.
    """
    m = api_model(model)
    if m.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"max_completion_tokens": n}
    return {"max_tokens": n}
