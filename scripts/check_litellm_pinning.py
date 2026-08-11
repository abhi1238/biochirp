#!/usr/bin/env python3
"""CI gate: assert litellm configs still match the paper-pinned mapping.

Two configs are validated, with different cache expectations:

  * `litellm_config.yaml`         (prod)   — cache MUST be `true`
  * `litellm_config_bench.yaml`   (bench)  — cache MUST be `false`

The alias → upstream mapping below MUST be identical across both files —
prod and bench have to resolve the same model for the same alias so a cache
miss on bench gives byte-for-byte the same response prod would have served.
The manuscript-cited result tables were generated under this mapping on
2026-05-12; silently re-routing any alias (e.g. flipping `gpt-4.1-mini`'s
upstream away from `openai/gpt-5.4-nano`) would invalidate every result in
evaluation/ without anybody noticing.

Updating the mapping requires editing ALL of:
  1. EXPECTED_MAPPING below
  2. The paper-pinned table at the top of litellm_config.yaml
  3. The paper-pinned table at the top of litellm_config_bench.yaml

and appending a CHANGE-LOG entry to both yaml files. The four edits must
land in the same commit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# (config path → expected litellm_settings.cache value)
CONFIGS: list[tuple[Path, bool]] = [
    (REPO_ROOT / "litellm_config.yaml",       True),   # prod
    (REPO_ROOT / "litellm_config_bench.yaml", False),  # bench
]

# Paper-pinned (alias → expected upstream model name as used in litellm_params.model).
EXPECTED_MAPPING: dict[str, str] = {
    "gpt-5-mini":    "openai/gpt-5-mini",
    "gpt-4.1-mini":  "openai/gpt-5.4-nano",
    "gpt-4.1-nano":  "openai/gpt-4.1-nano",
    "gpt-4o-mini":   "openai/gpt-4.1-nano",
    "gpt-5.4-mini":  "openai/gpt-5.4-mini",
    "gpt-5.4-nano":  "openai/gpt-5.4-nano",
}


def _resolve_mapping(cfg: dict) -> tuple[dict[str, str], list[str]]:
    """Return ({alias: upstream}, [duplicate-alias errors]). First entry wins."""
    actual: dict[str, str] = {}
    errs: list[str] = []
    for entry in cfg.get("model_list", []):
        name = entry.get("model_name")
        upstream = (entry.get("litellm_params") or {}).get("model")
        if name and upstream:
            if name in actual and actual[name] != upstream:
                errs.append(
                    f"alias {name!r} declared twice with different upstreams: "
                    f"{actual[name]!r} vs {upstream!r}"
                )
            actual.setdefault(name, upstream)
    return actual, errs


def _check_one(path: Path, expected_cache: bool, failures: list[str]) -> dict[str, str]:
    if not path.is_file():
        failures.append(f"missing config: {path}")
        return {}
    with path.open() as f:
        cfg = yaml.safe_load(f)

    actual, dupe_errs = _resolve_mapping(cfg)
    failures.extend(f"{path.name}: {e}" for e in dupe_errs)

    for alias, expected in EXPECTED_MAPPING.items():
        got = actual.get(alias)
        if got is None:
            failures.append(
                f"{path.name}: alias {alias!r} missing from model_list "
                f"(expected → {expected!r})"
            )
        elif got != expected:
            failures.append(
                f"{path.name}: alias {alias!r} drift: expected upstream "
                f"{expected!r}, got {got!r}. Update EXPECTED_MAPPING in this "
                f"script AND the CHANGE-LOG in both litellm yaml files in the "
                f"same commit."
            )

    cache_on = (cfg.get("litellm_settings") or {}).get("cache")
    if cache_on is not expected_cache:
        failures.append(
            f"{path.name}: litellm_settings.cache must be {expected_cache!r} "
            f"(prod=on for cost savings, bench=off for eval reproducibility); "
            f"got {cache_on!r}."
        )
    return actual


def main() -> int:
    failures: list[str] = []
    mappings: list[tuple[Path, dict[str, str]]] = []

    for path, expected_cache in CONFIGS:
        mappings.append((path, _check_one(path, expected_cache, failures)))

    # Cross-config check: every alias in EXPECTED_MAPPING must resolve to the
    # same upstream in every config. (Above we already checked each config
    # individually against EXPECTED_MAPPING; this catches a config that
    # contains an additional alias declared with different upstreams.)
    all_aliases = set()
    for _, m in mappings:
        all_aliases.update(m.keys())
    for alias in sorted(all_aliases):
        seen: dict[str, list[str]] = {}
        for path, m in mappings:
            if alias in m:
                seen.setdefault(m[alias], []).append(path.name)
        if len(seen) > 1:
            failures.append(
                f"alias {alias!r} resolves differently between configs: "
                + "; ".join(f"{up} ({', '.join(fs)})" for up, fs in seen.items())
            )

    if failures:
        print("LiteLLM pinning check FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    summaries = ", ".join(
        f"{p.name}: cache={ec!r}" for p, ec in CONFIGS
    )
    print(
        f"LiteLLM pinning check OK ({len(EXPECTED_MAPPING)} aliases verified "
        f"across {len(CONFIGS)} configs; {summaries})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
