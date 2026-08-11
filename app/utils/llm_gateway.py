"""
Unified LLM gateway for the entire BioChirp repo.

Single interface for all providers — production (through LiteLLM proxy) and
evaluation (Ollama direct, OpenRouter, Groq, xAI).

Usage
-----
    from app.utils.llm_gateway import call_llm, call_llm_sync, get_client

    # Async
    result = await call_llm("synthesizer", messages, provider="litellm")
    result = await call_llm("llama3.2:3b",          messages, provider="ollama")
    result = await call_llm("openai/gpt-4.1-nano",  messages, provider="openrouter")
    result = await call_llm("llama-3.1-8b-instant", messages, provider="groq")
    result = await call_llm("grok-4-1-fast-non-reasoning-latest", messages, provider="grok")

    # Sync (non-async call sites, evaluation notebooks)
    result = call_llm_sync("phi4:14b", messages, provider="ollama")

    # Raw client (for streaming or advanced use)
    client = get_client("litellm")
    stream = await call_llm("synthesizer", messages, provider="litellm", stream=True)

Returns
-------
    {"answer": str, "model": str, "latency": float}   (non-streaming)
    AsyncStream object                                  (stream=True)

Providers
---------
  litellm       Production LiteLLM proxy at $OPENAI_BASE_URL (default :4000).
                All production services use this — routes to Ollama/Gemini/OpenAI
                via litellm_config.yaml.  API key = $OPENAI_API_KEY.

  ollama        Direct Ollama (heavy GPU, biochirp_ollama) via OpenAI-compat /v1.
                Supports extra_body={"options": {...}, "think": False} for
                Qwen3 thinking-mode control.

  ollama_light  Direct Ollama (light GPU, biochirp_ollama_light) via OpenAI-compat /v1.

  openrouter    OpenRouter cloud models.  API key = $OPENROUTER_API_KEY.

  groq          Groq-hosted models (LLaMA etc.).  API key = $GROQ_API_KEY.

  grok          xAI Grok models.  API key = $GROK_KEY.

  openai        Direct OpenAI API (bypasses LiteLLM — use for reproducibility
                benchmarks that must not be cached).  API key = $OPENAI_API_KEY.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

# ── Process-level client cache ─────────────────────────────────────────────────
# Keyed by (base_url, api_key, loop_id) so each event loop gets its own client.
# Including loop_id prevents threads from stealing each other's httpx connection
# pools: a client created on loop_A cannot be used safely on loop_B (its internal
# httpx AsyncClient is bound to the loop it was created on), which caused sporadic
# "Connection error" when 32 benchmark threads shared the old (base_url, api_key) key.
_CACHE: dict[tuple[str, str, int], Any] = {}
_CACHE_LOCK = threading.Lock()


# ── Provider config ────────────────────────────────────────────────────────────
def _ollama_v1_url(env_var: str, default: str) -> str:
    """Return Ollama base URL with /v1 appended (OpenAI-compat endpoint)."""
    base = os.getenv(env_var, default).rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _provider_cfg(provider: str) -> tuple[str, str, float]:
    """Return (base_url, api_key, default_timeout_s) for a provider name."""
    configs: dict[str, tuple[str, str, float]] = {
        "litellm": (
            os.getenv("OPENAI_BASE_URL", "http://biochirp_litellm:4000"),
            os.getenv("OPENAI_API_KEY", "ollama"),
            45.,
        ),
        "ollama": (
            _ollama_v1_url("OLLAMA_BASE_URL", "http://biochirp_ollama:11434"),
            "ollama",
            180.,
        ),
        "ollama_light": (
            _ollama_v1_url("OLLAMA_LIGHT_BASE_URL", "http://biochirp_ollama_light:11434"),
            "ollama",
            180.,
        ),
        "openrouter": (
            "https://openrouter.ai/api/v1",
            os.getenv("OPENROUTER_API_KEY", ""),
            30.,
        ),
        "groq": (
            "https://api.groq.com/openai/v1",
            os.getenv("GROQ_API_KEY", ""),
            120.,
        ),
        "grok": (
            "https://api.x.ai/v1",
            os.getenv("GROK_KEY", ""),
            3600.,
        ),
        "openai": (
            "https://api.openai.com/v1",
            os.getenv("OPENAI_API_KEY", ""),
            30.,
        ),
    }
    if provider not in configs:
        raise ValueError(
            f"Unknown provider {provider!r}. Valid: {sorted(configs)}"
        )
    return configs[provider]


# ── Client factory ─────────────────────────────────────────────────────────────
def get_client(provider: str = "litellm", timeout_override: float | None = None):
    """Return a cached AsyncOpenAI client for the given provider.

    Thread-safe.  One client per (base_url, api_key) — all providers sharing
    the same endpoint reuse the same underlying HTTP connection pool.
    """
    from openai import AsyncOpenAI

    base_url, api_key, default_timeout = _provider_cfg(provider)
    timeout = timeout_override if timeout_override is not None else default_timeout

    # Key the cache on the *running* loop. get_event_loop() is deprecated when
    # no loop is running and can create a fresh loop each call → unbounded cache
    # growth + leaked AsyncOpenAI clients (each holds an httpx pool). get_client
    # is only reached via `await call_llm(...)`, so a running loop exists; the
    # sync wrapper clears _CACHE per fresh loop, so the no-loop fallback is safe.
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (base_url, api_key, loop_id)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )
        return _CACHE[key]


# ── Main async interface ───────────────────────────────────────────────────────
async def call_llm(
    model: str,
    messages: list[dict],
    *,
    provider: str = "litellm",
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    seed: int = 0,
    timeout_override: float | None = None,
    stream: bool = False,
    extra_body: dict | None = None,
) -> dict | Any:
    """Unified async LLM call across all providers.

    Parameters
    ----------
    model          Model name / alias (e.g. "synthesizer", "phi4:14b",
                   "openai/gpt-4.1-nano").
    messages       List of {"role": ..., "content": ...} dicts.
    provider       One of: litellm, ollama, ollama_light, openrouter, groq, grok, openai.
    system         Optional system prompt prepended to messages.
    extra_body     Provider-specific params forwarded verbatim.
                   Ollama examples: {"think": False}, {"options": {"num_ctx": 32768}}.
    stream         Return raw AsyncStream instead of parsed dict.

    Returns
    -------
    Non-streaming: {"answer": str, "model": str, "latency": float}
    Streaming:     openai.AsyncStream (caller iterates chunks)
    """
    if system:
        messages = [{"role": "system", "content": system}, *messages]

    client = get_client(provider, timeout_override)
    t0 = time.perf_counter()

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        seed=seed,
        stream=stream,
    )
    if extra_body:
        kwargs["extra_body"] = extra_body

    resp = await client.chat.completions.create(**kwargs)

    if stream:
        return resp

    msg = resp.choices[0].message
    # Reasoning models (gpt-oss-20b, DeepSeek-R1, o1-mini…) return chain-of-thought in
    # reasoning_content and leave content empty. Fall back to reasoning_content so the
    # final answer is captured. _strip_think in llm_filter_utils then removes <think> tags.
    content = msg.content or getattr(msg, "reasoning_content", None) or ""
    answer = content.strip()
    # routed_model: the actual provider+model OpenRouter selected (may differ from the slug we sent)
    routed_model = getattr(resp, "model", model) or model
    usage = getattr(resp, "usage", None)
    return {
        "answer": answer,
        "model": model,
        "routed_model": routed_model,
        "latency": time.perf_counter() - t0,
        "input_tokens":  getattr(usage, "prompt_tokens",     0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


# ── Sync wrapper ───────────────────────────────────────────────────────────────
def call_llm_sync(
    model: str,
    messages: list[dict],
    *,
    provider: str = "litellm",
    **kwargs,
) -> dict:
    """Sync wrapper around call_llm for non-async call sites.

    Works whether or not an event loop is already running (Jupyter, per-DB
    tool hooks, test runners).
    """
    coro = call_llm(model, messages, provider=provider, **kwargs)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an event loop (Jupyter / asyncio service) — run in a
        # fresh thread so we don't deadlock the caller's loop.
        result: dict = {}
        exc_box: list[BaseException] = []

        def _thread():
            # Use new_event_loop + explicit cleanup so httpx connection-pool
            # tasks can finish before the loop closes → eliminates the
            # "Event loop is closed" RuntimeError noise in Jupyter.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with _CACHE_LOCK:
                _CACHE.clear()   # fresh client bound to this loop
            try:
                result["v"] = loop.run_until_complete(coro)
            except Exception as e:
                exc_box.append(e)
            finally:
                # Let pending cleanup tasks (httpx pool teardown) finish
                try:
                    pending = asyncio.all_tasks(loop)
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()

        t = threading.Thread(target=_thread, daemon=True)
        t.start()
        t.join(timeout=kwargs.get("timeout_override", 200.))
        if exc_box:
            raise exc_box[0]
        return result.get("v", {})
    else:
        # Each asyncio.run() creates a new event loop. AsyncOpenAI clients cache
        # their httpx connection pool bound to the previous loop, causing
        # "Connection error" on first use in the new loop. Clear the cache so a
        # fresh client is created that's bound to the current event loop.
        with _CACHE_LOCK:
            _CACHE.clear()
        return asyncio.run(coro)

