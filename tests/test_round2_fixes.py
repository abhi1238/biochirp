"""Regression tests for the round-2 reliability fixes.

Covers:
    Fix #1 — TTL cache thundering-herd lock (utils/dataframe_loader.py)
    Fix #4 — env-var resolution for EXPAND/PLANNER URLs (per_db_tool/_orchestrator.py)
    Fix #5 — orphan-CSV sweep (per_db_tool/_main.py)

Fixes #2 (Redis lock) and #3 (httpx aclose) are not covered here — both need
real redis/httpx packages and would only re-test the double-check pattern
already proven by Fix #1's test. The patches themselves are line-for-line
trivial; review the diff is enough.

Run from the repo root:
    pytest tests/test_round2_fixes.py -q
"""
import asyncio
import logging
import os
import pathlib
import sys
import threading
import time

import pytest

# Repo root on the import path so `tests/` can reach `app.utils.*` etc.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# ─── Fix #1: TTL cache lock ────────────────────────────────────────────────

def _load_dataframe_loader_cache_only():
    """Exec only the cache section of dataframe_loader.py so the test doesn't
    require polars to be installed locally. The full module imports polars
    at the top, which CI may not have."""
    src = pathlib.Path("app/utils/dataframe_loader.py").read_text()
    start = src.index("# Process-wide cache")
    ns: dict = {"logger": logging.getLogger("test")}
    exec(src[start:], ns)
    return ns


def test_ttl_cache_warm_hit_returns_same_object():
    ns = _load_dataframe_loader_cache_only()
    calls = []

    def loader():
        calls.append(1)
        return {"tbl": object()}

    get_db = ns["ttl_cached_db"]("warm_hit_test", loader, ttl_seconds=3600)
    first = get_db()
    second = get_db()
    assert first is second, "warm hit must return the exact cached object"
    assert len(calls) == 1, "loader should only run once across warm hits"


def test_ttl_cache_no_thundering_herd_under_concurrent_misses():
    """The critical race-fix: N threads all hitting a cold cache must collapse
    to a single loader invocation. Pre-fix this would have been N invocations
    on every TTL boundary.

    Don't use a Barrier here — only one thread ever enters the loader (the
    others block at the cache lock), so a barrier requiring N participants
    would deadlock. A short sleep in the loader is enough to ensure the
    other threads pile up at the lock before the holder finishes.
    """
    ns = _load_dataframe_loader_cache_only()
    calls = []

    def slow_loader():
        time.sleep(0.1)
        calls.append(1)
        return {"tbl": "data"}

    get_db = ns["ttl_cached_db"]("herd_test", slow_loader, ttl_seconds=3600)
    threads = [threading.Thread(target=get_db) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, (
        f"thundering-herd lock defeated: expected 1 loader call, got {len(calls)}"
    )


def test_ttl_cache_clear_evicts():
    ns = _load_dataframe_loader_cache_only()
    calls = []

    def loader():
        calls.append(1)
        return {"v": len(calls)}

    get_db = ns["ttl_cached_db"]("clear_test", loader, ttl_seconds=3600)
    get_db()
    assert "clear_test" in ns["_DB_CACHE"]
    get_db.cache_clear()
    assert "clear_test" not in ns["_DB_CACHE"]
    get_db()  # re-loads
    assert len(calls) == 2


def test_ttl_cache_expiry_triggers_reload():
    ns = _load_dataframe_loader_cache_only()
    calls = []

    def loader():
        calls.append(1)
        return {"v": len(calls)}

    get_db = ns["ttl_cached_db"]("expiry_test", loader, ttl_seconds=0)
    get_db()
    # ttl_seconds=0 means every call past the first looks expired
    time.sleep(0.01)
    get_db()
    assert len(calls) == 2


# ─── Fix #5: CSV sweep ──────────────────────────────────────────────────────

@pytest.fixture
def csv_sweep_module():
    """Import _main.py's _sweep_old_results without booting FastAPI."""
    # _main.py imports openai_agents at module load — bypass by loading just
    # the function body via exec, similar to the cache test above. Cheap
    # and avoids depending on fastapi/agents in the test environment.
    src = pathlib.Path("app/per_db_tool/_main.py").read_text()
    fn_start = src.index("async def _sweep_old_results")
    fn_end = src.index("\n\ndef build_app", fn_start)
    body = src[fn_start:fn_end]
    ns: dict = {
        "os": os,
        "time": time,
        "Path": pathlib.Path,
        "logging": logging,
    }
    exec(body, ns)
    return ns["_sweep_old_results"]


def test_csv_sweep_removes_files_older_than_ttl(tmp_path, csv_sweep_module, monkeypatch):
    old = tmp_path / "old.csv"
    fresh = tmp_path / "fresh.csv"
    other = tmp_path / "keep.txt"
    old.write_text("a")
    fresh.write_text("b")
    other.write_text("c")
    # Backdate old.csv to 30 days ago.
    old_mtime = time.time() - 30 * 86400
    os.utime(old, (old_mtime, old_mtime))

    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("RESULTS_TTL_DAYS", "14")

    asyncio.run(csv_sweep_module("test-svc", logging.getLogger("test")))

    assert not old.exists(), "old CSV should be deleted"
    assert fresh.exists(), "fresh CSV must be kept"
    assert other.exists(), "non-CSV files must be untouched"


def test_csv_sweep_disabled_when_ttl_zero(tmp_path, csv_sweep_module, monkeypatch):
    old = tmp_path / "old.csv"
    old.write_text("a")
    os.utime(old, (time.time() - 30 * 86400,) * 2)

    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("RESULTS_TTL_DAYS", "0")

    asyncio.run(csv_sweep_module("test-svc", logging.getLogger("test")))

    assert old.exists(), "TTL=0 must disable the sweep entirely"


def test_csv_sweep_handles_missing_root(tmp_path, csv_sweep_module, monkeypatch):
    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("RESULTS_TTL_DAYS", "14")
    # Must not raise.
    asyncio.run(csv_sweep_module("test-svc", logging.getLogger("test")))


def test_csv_sweep_idempotent_under_concurrent_runs(tmp_path, csv_sweep_module, monkeypatch):
    """Two services starting simultaneously both sweep — the second one must
    not crash when files vanish mid-iteration."""
    for i in range(5):
        f = tmp_path / f"old_{i}.csv"
        f.write_text("x")
        os.utime(f, (time.time() - 30 * 86400,) * 2)

    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("RESULTS_TTL_DAYS", "14")

    async def both():
        await asyncio.gather(
            csv_sweep_module("svc-a", logging.getLogger("a")),
            csv_sweep_module("svc-b", logging.getLogger("b")),
        )

    asyncio.run(both())
    remaining = list(tmp_path.glob("*.csv"))
    assert remaining == [], f"all old CSVs should be gone, found: {remaining}"


def test_csv_sweep_invalid_ttl_falls_back_to_default(
    tmp_path, csv_sweep_module, monkeypatch
):
    old = tmp_path / "old.csv"
    old.write_text("x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)

    monkeypatch.setenv("RESULTS_ROOT", str(tmp_path))
    monkeypatch.setenv("RESULTS_TTL_DAYS", "not-a-number")

    asyncio.run(csv_sweep_module("test-svc", logging.getLogger("test")))
    # Default is 14 days, old.csv is 30 days → must be deleted.
    assert not old.exists()


# ─── Fix #4: env-var URL resolution ─────────────────────────────────────────

def _extract_url_resolution_block():
    """Pull the env-resolution snippet out of _orchestrator.py and wrap it in
    a tiny function so we can exercise it with monkeypatched env vars without
    pulling in the rest of the orchestrator (which needs fastapi, polars, etc).
    """
    src = pathlib.Path("app/per_db_tool/_orchestrator.py").read_text()
    start = src.index("    if expand_url is None:")
    end = src.index("    ctx = WorkerCtx(", start)
    block = src[start:end].strip()
    # Indent-strip the 4-space prefix.
    block = "\n".join(line[4:] if line.startswith("    ") else line
                      for line in block.splitlines())

    def resolve(env: dict, expand_url=None, planner_url=None):
        ns = {"os": _StubOs(env), "expand_url": expand_url, "planner_url": planner_url}
        exec(block, ns)
        return ns["expand_url"], ns["planner_url"]

    return resolve


class _StubOs:
    """Just enough os interface for the resolution block."""
    def __init__(self, env: dict):
        self.environ = env

    def getenv(self, key, default=None):
        return self.environ.get(key, default)


def test_url_resolution_defaults_match_compose_hostnames():
    resolve = _extract_url_resolution_block()
    expand, planner = resolve(env={})
    assert expand == (
        "http://biochirp_expand_and_match_db_tool:8009/expand_and_match_db"
    )
    assert planner == "http://biochirp_planner_tool:8011/planner"


def test_url_resolution_uses_host_port_env():
    resolve = _extract_url_resolution_block()
    expand, planner = resolve(env={
        "EXPAND_AND_MATCH_DB_HOST": "expand.internal",
        "EXPAND_AND_MATCH_DB_PORT": "9001",
        "PLANNER_HOST": "planner.internal",
        "PLANNER_PORT": "9002",
    })
    assert expand == "http://expand.internal:9001/expand_and_match_db"
    assert planner == "http://planner.internal:9002/planner"


def test_url_resolution_full_url_override_wins():
    resolve = _extract_url_resolution_block()
    expand, planner = resolve(env={
        "EXPAND_TOOL_URL": "https://lb.example/expand",
        "PLANNER_TOOL_URL": "https://lb.example/planner",
        "EXPAND_AND_MATCH_DB_HOST": "ignored",
        "PLANNER_HOST": "ignored",
    })
    assert expand == "https://lb.example/expand"
    assert planner == "https://lb.example/planner"


def test_url_resolution_explicit_kwarg_still_wins():
    """A caller-passed expand_url must NOT be overwritten by env resolution."""
    resolve = _extract_url_resolution_block()
    expand, planner = resolve(
        env={"EXPAND_TOOL_URL": "https://from-env"},
        expand_url="https://from-caller",
    )
    assert expand == "https://from-caller", (
        "explicit kwarg must short-circuit env resolution"
    )
