import asyncio
import os
import random
import httpx
from typing import Optional, Set

_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()


def _build_timeout() -> httpx.Timeout:
    connect = float(os.getenv("HTTP_TIMEOUT_CONNECT", "10"))
    read = float(os.getenv("HTTP_TIMEOUT_READ", "60"))
    write = float(os.getenv("HTTP_TIMEOUT_WRITE", "60"))
    pool = float(os.getenv("HTTP_TIMEOUT_POOL", "10"))
    return httpx.Timeout(connect=connect, read=read, write=write, pool=pool)


def _build_limits() -> httpx.Limits:
    max_connections = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
    max_keepalive = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
    )


async def get_http_client() -> httpx.AsyncClient:
    global _client
    async with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=_build_timeout(),
                limits=_build_limits(),
                headers={"User-Agent": "biochirp/1.0"},
            )
        return _client


async def close_http_client() -> None:
    global _client
    async with _lock:
        if _client and not _client.is_closed:
            await _client.aclose()
        _client = None


async def post_json_with_retries(
    url: str,
    payload: dict,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: Optional[float] = None,
    max_retries: int = 3,
    backoff_base_s: float = 0.6,
    jitter_s: float = 0.2,
) -> httpx.Response:
    last_exc: Optional[Exception] = None
    retry_on_status: Set[int] = {
        int(s.strip())
        for s in os.getenv("HTTP_RETRY_STATUS", "429,500,502,503,504").split(",")
        if s.strip().isdigit()
    }
    for attempt in range(max_retries + 1):
        try:
            client = await get_http_client()
            resp = await client.post(
                url,
                json=payload,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            if retry_on_status and resp.status_code in retry_on_status:
                raise httpx.HTTPStatusError(
                    f"Retryable status {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            status = getattr(e.response, "status_code", None)
            if status and status not in retry_on_status:
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
            if attempt >= max_retries:
                raise
            delay = backoff_base_s * (2 ** attempt) + random.uniform(0, jitter_s)
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("post_json_with_retries failed without exception")
