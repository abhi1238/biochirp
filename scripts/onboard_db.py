#!/usr/bin/env python3
"""Onboard / audit a schema_kg database against the LIVE wiring surfaces.

This is the lean replacement for the historical one-shot onboarder (which wrote
seven artifacts for the now-deleted `bio_chat_service` / `kg/*.yaml` routing
stack — see `dbs/README.md`). The current routing path is simpler, so onboarding
touches far fewer files. This tool does two things:

  --check <slug>      Report, per touch-point, whether the DB is wired in.
                      A "doctor" for new or in-progress DBs. Exits non-zero if
                      any REQUIRED surface is missing (so it can gate CI / deploy).
  --scaffold <slug>   Create the author-input skeletons (manifest + tool stubs)
                      for a brand-new DB. Non-destructive — never overwrites.

With no slug, --check runs over every schema_kg-enabled DB.

The single source of truth for "is this DB schema_kg-enabled" is
`config/schema_kg_dbs.discover_schema_kg_dbs()` (it scans this tree). Dropping
`schema_kg/inputs/<slug>/schema.json` auto-registers the DB for gen_compose +
the schema_mapper/schema_planner warm-lists; the remaining surfaces below are the
ones a human still authors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Required schema_kg input files (see schema_kg/inputs/README.md).
SCHEMA_KG_REQUIRED = [
    "schema.json", "queryable.json", "concept_type.json",
    "schema_rules.json", "questions.json",
]

OK, MISS, WARN = "✓", "✗", "⚠"


# ─── individual touch-point probes ──────────────────────────────────────────


def _probe_manifest(slug: str) -> tuple[str, bool, str]:
    p = ROOT / "dbs" / slug / "manifest.yaml"
    if not p.is_file():
        return MISS, True, "dbs/%s/manifest.yaml absent" % slug
    try:
        from dbs._schema import manifest_schema
        manifest_schema.load(p)
        return OK, True, "validates"
    except Exception as exc:  # noqa: BLE001 — surface any validation error
        return MISS, True, "INVALID: %s" % exc


def _probe_schema_py(slug: str) -> tuple[str, bool, str]:
    try:
        from config.schema import database_schemas
    except Exception as exc:  # noqa: BLE001
        return MISS, True, "config.schema import failed: %s" % exc
    if slug in database_schemas:
        return OK, True, "%d tables in database_schemas" % len(database_schemas[slug])
    return MISS, True, "no database_schemas[%r] entry" % slug


def _probe_schema_kg(slug: str) -> tuple[str, bool, str]:
    d = ROOT / "evaluation" / "schema_kg" / "inputs" / slug
    if not d.is_dir():
        return MISS, True, "schema_kg/inputs/%s/ absent (DB will NOT auto-register)" % slug
    missing = [f for f in SCHEMA_KG_REQUIRED if not (d / f).is_file()]
    if missing:
        return MISS, True, "missing files: %s" % ", ".join(missing)
    # Consistency: every queryable/concept_type key must resolve into schema.json.
    try:
        schema = json.loads((d / "schema.json").read_text())
        body = schema.get(slug) or next(iter(schema.values()))
        valid = {f"{slug}.{t}.{c}" for t, cols in body.items() for c in cols}
        bad = []
        for fname in ("queryable.json", "concept_type.json"):
            obj = json.loads((d / fname).read_text())
            for k in obj:
                if k == "_comment":
                    continue
                if k not in valid:
                    bad.append(f"{fname}:{k}")
        if bad:
            return WARN, True, "keys not in schema.json: %s" % "; ".join(bad[:4])
    except Exception as exc:  # noqa: BLE001
        return WARN, True, "consistency check failed: %s" % exc
    return OK, True, "5 files present + internally consistent"


def _probe_tool_code(slug: str) -> tuple[str, bool, str]:
    base = ROOT / "app" / "tools" / slug / "app"
    missing = [f for f in ("main.py", "database_loader.py") if not (base / f).is_file()]
    # The per-DB logic module is normally <slug>.py, but a slug that shadows a
    # stdlib name uses <slug>_db.py (e.g. string → string_db.py). Accept either.
    db_module = next((f"{slug}{s}.py" for s in ("", "_db")
                      if (base / f"{slug}{s}.py").is_file()), None)
    if db_module is None:
        missing.append(f"{slug}.py (or {slug}_db.py)")
    if missing:
        return MISS, True, "missing: %s" % ", ".join(missing)
    return OK, True, "main.py + database_loader.py + %s present" % db_module


def _probe_yaml_key(path: Path, slug: str, label: str, required: bool) -> tuple[str, bool, str]:
    if not path.is_file():
        return (MISS if required else WARN), required, "%s absent" % label
    doc = yaml.safe_load(path.read_text()) or {}
    if slug in doc:
        return OK, required, "has %r entry" % slug
    return (MISS if required else WARN), required, "no %r entry in %s" % (slug, label)


def _probe_compose(slug: str) -> tuple[str, bool, str]:
    p = ROOT / "docker-compose.yml"
    if not p.is_file():
        return MISS, True, "docker-compose.yml absent (run gen_compose.py)"
    txt = p.read_text()
    if f"biochirp_{slug}_tool:" in txt:
        return OK, True, "biochirp_%s_tool service present" % slug
    return MISS, True, "no biochirp_%s_tool service — run scripts/gen_compose.py" % slug


def _probe_nginx(slug: str) -> tuple[str, bool, str]:
    p = ROOT / "nginx_chat_routes.conf"
    if not p.is_file():
        return WARN, False, "nginx_chat_routes.conf absent"
    if f"/{slug}_chat/" in p.read_text():
        return OK, False, "/%s_chat/ route present" % slug
    return MISS, False, "no /%s_chat/ route — run scripts/gen_compose.py" % slug


def _probe_frontend(slug: str) -> tuple[str, bool, str]:
    p = ROOT / "frontend" / "configs" / "db_chats.json"
    if not p.is_file():
        return WARN, False, "frontend/configs/db_chats.json absent"
    try:
        d = json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        return WARN, False, "unparseable: %s" % exc
    if slug in d:
        return OK, False, "in unified-chat DB list"
    return MISS, False, "not in frontend/configs/db_chats.json (UI list)"


def _probe_registry(slug: str) -> tuple[str, bool, str]:
    p = ROOT / "resources" / "db_profiles" / "registry.md"
    if not p.is_file():
        return WARN, False, "resources/db_profiles/registry.md absent"
    txt = p.read_text().lower()
    if slug.lower() in txt:
        return OK, False, "documented in registry.md"
    return MISS, False, "not in resources/db_profiles/registry.md"


def _probe_data(slug: str) -> tuple[str, bool, str]:
    d = ROOT / "database" / slug
    if not (ROOT / "database").is_dir():
        return WARN, False, "database/ dir not in this checkout — skipped"
    if not d.is_dir():
        return MISS, False, "database/%s/ absent" % slug
    n = len(list(d.glob("*.parquet")))
    if n:
        return OK, False, "%d parquet file(s)" % n
    return MISS, False, "database/%s/ has no *.parquet" % slug


def _is_remote(slug: str) -> bool:
    p = ROOT / "dbs" / slug / "manifest.yaml"
    if not p.is_file():
        return False
    doc = yaml.safe_load(p.read_text()) or {}
    return bool(doc.get("is_remote", False))


# ─── check command ──────────────────────────────────────────────────────────


def check_db(slug: str) -> bool:
    """Print a per-touch-point report. Return True if all REQUIRED surfaces pass."""
    remote = _is_remote(slug)
    notes = ROOT / "resources" / "prompts" / "db_notes.yaml"
    rules = ROOT / "resources" / "prompts" / "db_llm_rules.yaml"

    rows: list[tuple[str, tuple[str, bool, str]]] = [
        ("manifest.yaml",            _probe_manifest(slug)),
        ("config/schema.py",         _probe_schema_py(slug)),
        ("schema_kg/inputs",         _probe_schema_kg(slug)),
        ("tool code",                _probe_tool_code(slug)),
        ("db_notes.yaml",            _probe_yaml_key(notes, slug, "db_notes.yaml", True)),
        ("db_llm_rules.yaml",        _probe_yaml_key(rules, slug, "db_llm_rules.yaml", False)),
        ("docker-compose.yml",       _probe_compose(slug)),
        ("nginx route",              _probe_nginx(slug)),
        ("frontend db list",         _probe_frontend(slug)),
        ("db_profiles registry",     _probe_registry(slug)),
        ("database/ parquet",        _probe_data(slug)),
    ]
    if remote:
        # Remote-API DBs run no parquet container or schema_kg planner.
        skip = {"schema_kg/inputs", "tool code", "docker-compose.yml",
                "nginx route", "database/ parquet"}
        rows = [(n, (WARN, False, "remote-API DB — N/A")) if n in skip else (n, r)
                for n, r in rows]

    print(f"\n{slug}{'  (remote-API)' if remote else ''}")
    all_required_ok = True
    for name, (mark, required, detail) in rows:
        tag = "REQ" if required else "opt"
        print(f"  {mark} [{tag}] {name:<22} {detail}")
        if required and mark == MISS:
            all_required_ok = False
    print(f"  → {'READY' if all_required_ok else 'INCOMPLETE — required surfaces missing'}")
    return all_required_ok


def cmd_check(slugs: list[str]) -> int:
    from config.schema_kg_dbs import discover_schema_kg_dbs
    if not slugs:
        slugs = sorted(discover_schema_kg_dbs(ROOT / "evaluation" / "schema_kg" / "inputs"))
    print(f"Onboarding status for {len(slugs)} DB(s):")
    ok = True
    for s in slugs:
        ok &= check_db(s)
    print()
    print("All required surfaces present. ✓" if ok else
          "Some DB(s) INCOMPLETE — see ✗ [REQ] rows above.")
    return 0 if ok else 1


# ─── scaffold command ───────────────────────────────────────────────────────


_MANIFEST_TMPL = """\
name: {slug}
display_name: {disp}
description: |
  Three- to five-sentence prose describing what {disp} contains, which
  biomedical entities it answers questions about, and what it does NOT cover.
  The first ~1500 chars feed the BGE-small embedding and the router, so be
  specific and ground every example in the actual data.
version: "1.0"
license: TODO
sources:
  - https://TODO/

schema:
  inputs:
    - field: gene_symbol           # TODO: CommonField slugs the DB accepts
      examples: [TP53, KRAS, EGFR]
  outputs:
    - field: TODO
  tables:
    {slug}_master_table:
      description: One-line table summary (TODO)
      key_columns:
        - {{name: TODO_id, description: "What this column holds + a real example"}}

service:
  tool:
    port: 0          # TODO: assign a free port
    workers: 2
    memory_limit: 4g
  chat:
    port: 0          # TODO: assign a free port
    db_name: {disp}
"""

_TOOL_MAIN_TMPL = '''\
"""{disp} data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only {disp}'s identity + capability blurb are injected here.
Modeled on app/tools/uniprot/app/main.py — keep it this thin.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.{slug} import return_{slug}_result, get_{slug}_db, _{up}_CAPABILITIES

app = build_app(
    db_short="{slug}",
    return_result_fn=return_{slug}_result,
    get_db_fn=get_{slug}_db,
    display_name="{disp}",
)

app.include_router(build_chat_router(ChatSpec(
    db="{slug}",
    display_name="{disp}",
    long_name="{disp}",
    return_result_fn=return_{slug}_result,
    capabilities=_{up}_CAPABILITIES,
)))
'''

_TOOL_DB_TMPL = '''\
"""{disp} schema_kg config + handler. Modeled on app/tools/uniprot/app/{slug}.py."""
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_{slug}


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_{slug}_db = \\
    setup_service_globals("{slug}", "{disp}", return_preprocessed_{slug})


# TODO: describe, grounded in the parquet, exactly what {disp} can answer.
_{up}_CAPABILITIES = (
    "- TODO capability line 1\\n"
    "- TODO capability line 2"
)
# TODO: what it canNOT answer (steers the router away from off-topic queries).
_{up}_LIMITATIONS = "TODO"

_{up}_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_{slug}_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_{up}_CAPABILITIES,
    limitations=_{up}_LIMITATIONS,
)

return_{slug}_result = make_schema_kg_handler(_{up}_CONFIG)
'''

_TOOL_LOADER_TMPL = '''\
"""Load + preprocess {disp} parquet tables into the in-memory dict the planner joins.

TODO: return {{table_name: polars.DataFrame}} for every table in
config.schema.database_schemas["{slug}"]. See app/tools/uniprot/app/database_loader.py
for a worked example. Use an atomic write if you ever rewrite parquet on disk
(see the HCDT stale-mmap note in project memory).
"""


def return_preprocessed_{slug}():  # noqa: ANN201
    raise NotImplementedError("TODO: load {slug} parquet tables")
'''


def _write_if_absent(path: Path, content: str, created: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    created.append(str(path.relative_to(ROOT)))


def cmd_scaffold(slug: str) -> int:
    from dbs._schema import manifest_schema  # noqa: F401 — validate import works
    if not __import__("re").fullmatch(r"[a-z][a-z0-9_]+", slug):
        print(f"slug {slug!r} must match [a-z][a-z0-9_]+", file=sys.stderr)
        return 2
    disp = slug.replace("_", " ").title()
    up = slug.upper()
    created: list[str] = []

    _write_if_absent(ROOT / "dbs" / slug / "manifest.yaml",
                     _MANIFEST_TMPL.format(slug=slug, disp=disp), created)
    appdir = ROOT / "app" / "tools" / slug / "app"
    _write_if_absent(appdir / "__init__.py", "", created)
    _write_if_absent(appdir / "main.py",
                     _TOOL_MAIN_TMPL.format(slug=slug, disp=disp, up=up), created)
    _write_if_absent(appdir / f"{slug}.py",
                     _TOOL_DB_TMPL.format(slug=slug, disp=disp, up=up), created)
    _write_if_absent(appdir / "database_loader.py",
                     _TOOL_LOADER_TMPL.format(slug=slug, disp=disp), created)

    if created:
        print("Created (non-destructive — existing files untouched):")
        for f in created:
            print(f"  + {f}")
    else:
        print("Nothing created — all scaffold files already exist.")
    print("\nNext (see dbs/README.md for the full procedure):")
    print("  1. Fill the manifest TODOs + drop parquet under database/%s/" % slug)
    print("  2. Add database_schemas['%s'] to config/schema.py" % slug)
    print("     (scaffold: python scripts/schema_manifest_sync.py --emit %s)" % slug)
    print("  3. Build schema_kg inputs: python schema_kg/src/build.py --inputs schema_kg/inputs/%s" % slug)
    print("  4. Fill the tool code TODOs (capabilities/limitations + loader)")
    print("  5. Add a db_notes.yaml entry; register frontend + registry surfaces")
    print("  6. python scripts/gen_compose.py   (regenerates compose + nginx)")
    print("  7. python scripts/onboard_db.py --check %s" % slug)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", nargs="*", metavar="SLUG",
                   help="report wiring status (no slug = all schema_kg DBs)")
    g.add_argument("--scaffold", metavar="SLUG",
                   help="create author-input skeletons for a NEW db (non-destructive)")
    args = ap.parse_args()

    if args.scaffold:
        return cmd_scaffold(args.scaffold)
    # Default action is --check (with or without slugs).
    return cmd_check(args.check or [])


if __name__ == "__main__":
    sys.exit(main())
