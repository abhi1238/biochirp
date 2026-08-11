"""Process-wide HTTP keep-alive session shared by per-DB tool workers.

Extracted from the byte-for-byte identical block that appeared at the top
of the per-DB worker files (e.g. `app/tools/ttd/app/ttd.py`,
`app/tools/hcdt/app/hcdt.py`, …). Pool sizes match the original
exactly: http(8/32), https(4/16).

Worker files should now do:

    from app.per_db_tool import HTTP_SESSION as _HTTP_SESSION

…instead of constructing their own session. One singleton per process,
identical behaviour, zero diff in connection-pool semantics.

Services that intentionally use a different HTTP client (ttd uses
`httpx.AsyncClient`) keep their own setup and do not import this.
"""
import requests


def _build_session() -> requests.Session:
    s = requests.Session()
    try:
        from requests.adapters import HTTPAdapter
        s.mount("http://",  HTTPAdapter(pool_connections=8, pool_maxsize=32))
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
    except Exception:
        # Adapter import shouldn't fail in any environment that has requests,
        # but the original code swallowed this and we preserve that behaviour.
        pass
    return s


HTTP_SESSION: requests.Session = _build_session()
