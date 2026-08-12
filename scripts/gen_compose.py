#!/usr/bin/env python3
"""Generate docker-compose.yml + nginx_chat_routes.conf from the DB manifests.

Single source of truth: dbs/<slug>/manifest.yaml `service.tool` blocks (read via
load_db_services()). config/services_registry.yaml was dropped 2026-06-18 — it
was a denormalised copy of those blocks.
Static (non-DB) compose portions: scripts/compose_head.yaml, scripts/compose_tail.yaml

Run after editing a manifest's service block (or compose_head/tail):
    python3 scripts/gen_compose.py

The generated docker-compose.yml has a leading "DO NOT EDIT" banner so that
contributors who try to hand-edit will be redirected to the manifests.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DBS_DIR = ROOT / "dbs"
HEAD = ROOT / "scripts" / "compose_head.yaml"
TAIL = ROOT / "scripts" / "compose_tail.yaml"
COMPOSE_OUT = ROOT / "docker-compose.yml"
NGINX_OUT = ROOT / "nginx_chat_routes.conf"

# Per-DB tool-service defaults (formerly config/services_registry.yaml `defaults:`).
# The registry was a denormalised copy of every manifest's `service:` block; it
# was dropped 2026-06-18 once reconciled, and gen_compose now reads the manifests
# directly (single source of truth). A manifest's `service.tool` block overrides
# any of these per DB.
TOOL_DEFAULTS = {
    "workers": 2,
    "memory_limit": "4g",
    "memory_reservation": "1g",
    "healthcheck_start_period": "60s",
    "depends_on": ["biochirp_planner_tool", "biochirp_expand_and_match_db_tool"],
}


def load_db_services() -> dict:
    """Build {slug: {"tool": {...}}} from every dbs/<slug>/manifest.yaml that has
    a `service.tool` block (parquet-backed DB tools). Remote/API DBs (no service
    block) and tail-defined services (e.g. opentargets) are intentionally absent.
    This replaces the hand-maintained config/services_registry.yaml."""
    out: dict[str, dict] = {}
    for manifest in sorted(DBS_DIR.glob("*/manifest.yaml")):
        doc = yaml.safe_load(manifest.read_text()) or {}
        tool = ((doc.get("service") or {}).get("tool"))
        if not tool:
            continue
        if "port" not in tool:
            raise ValueError(f"{manifest}: service.tool has no `port`")
        out[manifest.parent.name] = {"tool": tool}
    return out

# Module names that differ from the DB slug (e.g. string → string_db.py)
MODULE_OVERRIDES = {"string": "string_db"}

# DBs served by the schema_kg pipeline. HYBRID architecture: the heavy
# mapper/planner (bge model + torch + Qdrant) runs ONCE in the shared
# biochirp_schema_mapper_tool service; these per-DB tools stay LEAN
# (Dockerfile.service, ~519 MB) and POST to it over HTTP. They still mount
# main.py (wires the per-DB chat router) and get an nginx /<db>_chat/ +
# /services/<db>/ route pair. The worker/chat/planner code is shared in
# app/per_db_tool/ (mounted via *v-per-db-tool) — no torch in these images.
#
# SINGLE SOURCE OF TRUTH: the set is discovered from schema_kg/inputs/<db>/ via
# config.schema_kg_dbs (shared with the schema_mapper service's warm list), so a
# new DB is picked up automatically with no edit here.
sys.path.insert(0, str(ROOT))
from config.schema_kg_dbs import discover_schema_kg_dbs  # noqa: E402

SCHEMA_KG_DBS = discover_schema_kg_dbs(ROOT / "evaluation" / "schema_kg" / "inputs")

BANNER = """# ┌────────────────────────────────────────────────────────────────────┐
# │  GENERATED FILE — DO NOT EDIT BY HAND                              │
# │  Source: dbs/*/manifest.yaml (service blocks) + scripts/compose_*.yaml │
# │  Regenerate with: python3 scripts/gen_compose.py                   │
# │  Hand-editing this file is safe to debug, but changes will be      │
# │  overwritten on the next generator run. To make a real change,     │
# │  edit the DB's manifest service block (or compose_head/tail) and   │
# │  re-run the generator.                                             │
# └────────────────────────────────────────────────────────────────────┘
"""


def render_tool_block(slug: str, tool: dict, defaults: dict) -> str:
    """Render one per-DB data-tool service block."""
    workers = tool.get("workers", defaults["workers"])
    port = tool["port"]
    mem_lim = tool.get("memory_limit", defaults["memory_limit"])
    mem_res = tool.get("memory_reservation", defaults["memory_reservation"])
    hc_start = tool.get("healthcheck_start_period", defaults["healthcheck_start_period"])
    module = tool.get("module", MODULE_OVERRIDES.get(slug, slug))
    depends = list(defaults["depends_on"]) + list(tool.get("extra_depends_on", []))
    env_extras = dict(tool.get("env", {}) or {})
    vol_extras = list(tool.get("extra_volumes", []) or [])

    is_schema_kg = slug in SCHEMA_KG_DBS
    if is_schema_kg:
        # HYBRID: stay LEAN. The model lives in the shared schema_mapper service;
        # these tools just POST to it. Wire the schema_mapper host and
        # depend on the mapper being healthy.
        env_extras.setdefault("SCHEMA_MAPPER_HOST", "biochirp_schema_mapper_tool")
        env_extras.setdefault("SCHEMA_MAPPER_PORT", "8019")
        if "biochirp_schema_mapper_tool" not in depends:
            depends = depends + ["biochirp_schema_mapper_tool"]

    # All per-DB tools (incl. schema_kg ones, now lean) use Dockerfile.service.
    dockerfile = "../../../Dockerfile.service"

    lines = [
        f"  biochirp_{slug}_tool:",
        f"    <<: *defaults",
        f"    build:",
        f"      context: ./app/tools/{slug}",
        f"      dockerfile: {dockerfile}",
        f"      args:",
        f'        SERVICE_PORT: "{port}"',
        f'        SERVICE_WORKERS: "{workers}"',
        f"    image: biochirp_{slug}_tool:latest",
        f"    container_name: biochirp_{slug}_tool",
        f"    environment:",
        f"      <<: *db-tool-env",
        f"      SERVICE_NAME: {slug}",
    ]
    for k, v in sorted(env_extras.items()):
        lines.append(f'      {k}: "{v}"' if not isinstance(v, str) or not v.startswith("biochirp_") else f"      {k}: {v}")
    lines += [
        f"    volumes:",
        f"      - *v-guardrail",
        f"      - *v-attributions",
        f"      - *v-provenance",
        f"      - *v-schema",
        f"      - *v-settings",
        f"      - ./database/{slug}/:/app/database/{slug}/:ro",
    ]
    for v in vol_extras:
        lines.append(f"      - {v}")
    lines += [
        f"      - *v-utils",
        f"      - *v-utils-app",
        f"      - *v-per-db-tool",
        f"      - *v-results",
        f"      - *v-prompts",
        f"      - *v-field-aliases",
        f"      - ./app/tools/{slug}/app/{module}.py:/app/app/{module}.py:ro",
        f"      - ./app/tools/{slug}/app/database_loader.py:/app/app/database_loader.py:ro",
    ]
    if is_schema_kg:
        # schema.json column descriptions feed the text-to-SQL analytical step
        # (app/per_db_tool/_text2sql.py) for every schema_kg DB.
        lines.append(f"      - ./evaluation/schema_kg/inputs/:/app/schema_kg/inputs/:ro")
        # main.py wires the schema_kg chat router; mount it for hot-reload.
        lines.append(f"      - ./app/tools/{slug}/app/main.py:/app/app/main.py:ro")
    lines += [
        f"    ports:",
        # Bound to 127.0.0.1, not 0.0.0.0: nginx (host process) reverse-proxies
        # these via 127.0.0.1 already, so this only closes the direct,
        # un-proxied path that bypasses nginx's rate limiting entirely.
        f'      - "127.0.0.1:{port}:{port}"',
        f"    healthcheck:",
        f"      <<: *healthcheck-http",
        f"      start_period: {hc_start}",
        f"    deploy:",
        f"      resources:",
        f"        limits:",
        f"          memory: {mem_lim}",
        f"        reservations:",
        f"          memory: {mem_res}",
        f"    depends_on:",
    ]
    for d in depends:
        lines.append(f"      {d}:")
        lines.append(f"        condition: service_healthy")
    return "\n".join(lines) + "\n"


def render_nginx_routes(items: list) -> str:
    """Regenerate the per-DB schema_kg chat WS routes.

    The multi-DB orchestrator routes (/bio_chat/ + /bio_chat_v2/ → port 8030)
    were removed 2026-06-18: the bio_chat backend was decommissioned and nothing
    serves 8030. Re-add them here if/when a unified multi-DB backend returns."""
    lines = [
        "# ┌────────────────────────────────────────────────────────────────────┐",
        "# │  GENERATED FILE — DO NOT EDIT BY HAND                              │",
        "# │  Source: dbs/*/manifest.yaml (service blocks)                      │",
        "# │  Regenerate with: python3 scripts/gen_compose.py                   │",
        "# └────────────────────────────────────────────────────────────────────┘",
        "#",
        "# WebSocket common-headers snippet is installed at /etc/nginx/snippets/",
        "# ws_common.conf (see ws_common.conf at repo root).",
    ]
    # Per-DB schema_kg self-contained chat (WebSocket) + REST service card.
    for slug, entry in items:
        if slug not in SCHEMA_KG_DBS:
            continue
        port = entry["tool"]["port"]
        lines += [
            "",
            f"# ── {slug.upper()} self-contained chat (WebSocket, port {port}) ──",
            f"location ^~ /{slug}_chat/ {{",
            f"    proxy_pass http://127.0.0.1:{port};",
            "    include /etc/nginx/snippets/ws_common.conf;",
            "}",
            "",
            f"# ── {slug.upper()} REST tool + health (index.html service card) ──",
            f"location ^~ /services/{slug}/ {{",
            f"    proxy_pass http://127.0.0.1:{port}/;",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "    proxy_set_header X-Forwarded-Proto $scheme;",
            "}",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    databases = load_db_services()
    head = HEAD.read_text()
    tail = TAIL.read_text()

    # Render DB tool blocks, sorted by tool port so the diff is stable
    items = sorted(
        databases.items(),
        key=lambda kv: kv[1]["tool"]["port"],
    )

    tool_blocks = [render_tool_block(slug, entry["tool"], TOOL_DEFAULTS) for slug, entry in items]

    # Per-DB execute routing. The orchestrator's execute_tool calls back the
    # per-DB /execute endpoint, which runs the join against ITS OWN loaded
    # dataset (the ?database= query param is ignored). So each DB's execute MUST
    # be dispatched to that DB's own container (biochirp_<slug>_tool:<port>) —
    # the global EXECUTE_HOST fallback (biochirp_hcdt_tool) would run every DB's
    # join against the hcdt dataset. We inject EXECUTE_HOST_<DB>/EXECUTE_PORT_<DB>
    # right after the orchestrator's global EXECUTE_HOST/EXECUTE_PORT lines in
    # compose_head.yaml, generated from each manifest's service port.
    exec_routes = []
    for slug, entry in items:
        port = entry["tool"]["port"]
        exec_routes.append(f"      EXECUTE_HOST_{slug.upper()}: biochirp_{slug}_tool")
        exec_routes.append(f'      EXECUTE_PORT_{slug.upper()}: "{port}"')
    routes_block = "\n".join(exec_routes)
    _anchor_re = re.compile(
        r'(\n      EXECUTE_HOST: \S+\n      EXECUTE_PORT: "\d+")'
    )
    head, _n = _anchor_re.subn(lambda m: m.group(1) + "\n" + routes_block, head, count=1)
    if _n == 0:
        print("  ! WARNING: orchestrator EXECUTE_HOST/EXECUTE_PORT anchor not found "
              "in compose_head.yaml; per-DB execute routes NOT injected — every "
              "non-hcdt DB's execute will fall back to biochirp_hcdt_tool and fail.",
              file=sys.stderr)

    parts = [
        BANNER,
        head.rstrip() + "\n",
        "",
        f"  # ─── Generated per-DB data-tool services ({len(tool_blocks)}) ──────────────────────",
        "",
        "\n".join(tool_blocks),
        "",
        tail.lstrip("\n"),
    ]
    out = "\n".join(parts)
    COMPOSE_OUT.write_text(out)

    NGINX_OUT.write_text(render_nginx_routes(items))

    print(f"✓ {COMPOSE_OUT.relative_to(ROOT)}: {out.count(chr(10))} lines, {len(out)} bytes")
    print(f"✓ {NGINX_OUT.relative_to(ROOT)}: {NGINX_OUT.read_text().count(chr(10))} lines")
    print(f"  - {len(items)} DB tool blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
