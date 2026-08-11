from dataclasses import dataclass
import os

@dataclass(frozen=True)
class OTClientConfig:
    url: str = "https://api.platform.opentargets.org/api/v4/graphql"
    timeout: int = int(os.getenv("OT_REQUEST_TIMEOUT", "45"))
    page_size: int = int(os.getenv("OT_PAGE_SIZE", "1000"))
    max_cursor_pages: int = int(os.getenv("OT_MAX_CURSOR_PAGES", "100"))
    max_retries: int = int(os.getenv("OT_MAX_RETRIES", "4"))
    backoff_base_s: float = float(os.getenv("OT_BACKOFF_BASE_S", "0.6"))
    retry_jitter_s: float = float(os.getenv("OT_RETRY_JITTER_S", "0.2"))
    retry_on_status: str = os.getenv("OT_RETRY_STATUS", "429,500,502,503,504")
    log_queries: bool = os.getenv("OT_LOG_QUERIES", "0").lower() in ("1", "true", "yes")
    timeout_connect: float = float(os.getenv("OT_HTTP_TIMEOUT_CONNECT", "10"))
    timeout_read: float = float(os.getenv("OT_HTTP_TIMEOUT_READ", "45"))
    timeout_write: float = float(os.getenv("OT_HTTP_TIMEOUT_WRITE", "45"))
    timeout_pool: float = float(os.getenv("OT_HTTP_TIMEOUT_POOL", "10"))
    max_connections: int = int(os.getenv("OT_HTTP_MAX_CONNECTIONS", "100"))
    max_keepalive_connections: int = int(os.getenv("OT_HTTP_MAX_KEEPALIVE", "20"))
    metadata_concurrency: int = int(os.getenv("OT_ONTOLOGY_META_CONCURRENCY", "4"))
