import asyncio
import re
import weakref
import time
import random
import httpx
from typing import Dict, Any, Optional, List, Iterable, Set
from .config import OTClientConfig
from .uvicorn_logger import setup_logger

import logging
# =========================================================
# Logging
# =========================================================
# logger = setup_logger("biochirp.opentargets.resolvers")
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.client")

# logger = setup_logger("opentargets.client")

class OpenTargetsUpstream(RuntimeError): ...

_CLIENTS: "weakref.WeakSet[OTGraphQLClient]" = weakref.WeakSet()


class OTGraphQLClient:
    def __init__(self, cfg: OTClientConfig):
        self.cfg = cfg
        timeout = httpx.Timeout(
            connect=self.cfg.timeout_connect,
            read=self.cfg.timeout_read,
            write=self.cfg.timeout_write,
            pool=self.cfg.timeout_pool,
        )
        limits = httpx.Limits(
            max_connections=self.cfg.max_connections,
            max_keepalive_connections=self.cfg.max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
                "User-Agent": "biochirp-opentargets/1.0",
            },
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
        )
        self._retry_on_status: Set[int] = {
            int(s.strip())
            for s in (self.cfg.retry_on_status or "").split(",")
            if s.strip().isdigit()
        }
        self._log_queries = bool(self.cfg.log_queries)
        _CLIENTS.add(self)

    @staticmethod
    def _extract_query_name(query: str) -> str:
        match = re.search(r"\b(query|mutation)\s+([A-Za-z_]\w*)", query or "")
        return match.group(2) if match else "anonymous_query"

    @staticmethod
    def _shorten(text: Optional[str], limit: int = 300) -> Optional[str]:
        if text is None:
            return None
        text = str(text)
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    async def run(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables}
        query_name = self._extract_query_name(query)

        for attempt in range(self.cfg.max_retries + 1):
            try:
                started = time.monotonic()
                r = await self._client.post(self.cfg.url, json=payload)
                if self._retry_on_status and r.status_code in self._retry_on_status:
                    raise httpx.HTTPStatusError(
                        f"Retryable status {r.status_code}",
                        request=r.request,
                        response=r,
                    )
                r.raise_for_status()
                try:
                    data = r.json()
                except ValueError:
                    body = self._shorten(r.text)
                    logger.error(
                        "OpenTargets non-JSON response query=%s status=%s body=%s",
                        query_name,
                        r.status_code,
                        body,
                    )
                    raise OpenTargetsUpstream(f"{query_name}: non-JSON response")
                if data.get("errors"):
                    logger.error(
                        "OpenTargets GraphQL error query=%s errors=%s",
                        query_name,
                        data.get("errors"),
                    )
                    first_error = data["errors"][0]
                    message = (
                        first_error.get("message")
                        if isinstance(first_error, dict)
                        else str(first_error)
                    )
                    raise OpenTargetsUpstream(f"{query_name}: {message}")
                if self._log_queries:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    logger.debug(
                        "OpenTargets query=%s ok in %.1fms",
                        query_name,
                        elapsed_ms,
                    )
                return data["data"]
            except httpx.TimeoutException as e:
                logger.warning(
                    "OpenTargets timeout query=%s attempt=%d err=%s",
                    query_name,
                    attempt,
                    e,
                )
                if attempt >= self.cfg.max_retries:
                    raise
            except httpx.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                body = self._shorten(getattr(e.response, "text", None))
                logger.warning(
                    "OpenTargets HTTP error query=%s status=%s body=%s",
                    query_name,
                    status,
                    body,
                )
                if attempt >= self.cfg.max_retries:
                    raise
            except Exception as e:
                if isinstance(e, OpenTargetsUpstream):
                    raise
                if attempt >= self.cfg.max_retries:
                    raise
            delay = (
                self.cfg.backoff_base_s * (2 ** attempt)
                + random.uniform(0, self.cfg.retry_jitter_s)
            )
            await asyncio.sleep(delay)

    async def fetch_cursor_rows(
        self, query: str, variables: Dict[str, Any], root: str, node: str
    ) -> List[Dict[str, Any]]:
        rows, cursor = [], None
        for _ in range(self.cfg.max_cursor_pages):
            v = dict(variables, cursor=cursor)
            data = await self.run(query, v)
            block = (data.get(root) or {}).get(node) or {}
            page = block.get("rows") or []
            if not page:
                break
            rows.extend(page)
            cursor = block.get("cursor")
            if not cursor:
                break
        return rows



    async def search_first_hit(self, term: str, entity: str) -> Optional[Dict[str, Any]]:
        q = """
        query ($term: String!, $entity: [String!]) {
          search(queryString: $term, entityNames: $entity) {
            hits { id name entity }
          }
        }
        """
        data = await self.run(q, {"term": term, "entity": [entity]})
        hits = (data.get("search") or {}).get("hits") or []
        return hits[0] if hits else None

    async def aclose(self) -> None:
        await self._client.aclose()


async def close_all_open_targets_clients() -> None:
    clients: Iterable[OTGraphQLClient] = list(_CLIENTS)
    if not clients:
        return
    await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)
