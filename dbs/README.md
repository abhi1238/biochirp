# `dbs/` — per-database manifests + onboarding

Each subdirectory under `dbs/` is a single biomedical database. One file —
`manifest.yaml` — is the human-authored documentation surface for that DB
(prose description, key columns, Docker service spec). It is validated by
[`_schema/manifest_schema.py`](_schema/manifest_schema.py).

> **Onboarding is lean (rewritten 2026-06-23).** The historical one-shot
> onboarder wrote seven artifacts for the *old* routing stack — `bio_chat_service`'s
> `db_catalog`, `mcp_server/db_tool_specs.json`, and the `kg/*.yaml` graph router.
> All three were deleted on the `remove-per-db-agent-chat` branch, so that
> onboarder (and the matrix of "14 integration points" it targeted) is **gone**.
> The current routing path is simpler: a per-DB LLM router reads only the
> `capabilities`/`limitations` strings in each tool's `SchemaKgConfig`, plus the
> schema_mapper's ANN over `schema_kg/inputs/<slug>/schema.json`. No `db_catalog`,
> no `relation_db_map`, no capability_triggers. The live surfaces are the few
> steps below.
>
> The manifest automation knobs left over from that era — `skip_relations`,
> `force_relations`, `explicit_capability_triggers` — are now **inert** (nothing
> reads them). They still validate for backward compatibility; leave them out.

## Tooling

| Command | What it does |
|---|---|
| `python scripts/onboard_db.py --check [slug]` | Per-touch-point wiring report (a "doctor"). No slug = all schema_kg DBs. Exits non-zero if any REQUIRED surface is missing. |
| `python scripts/onboard_db.py --scaffold <slug>` | Create author-input skeletons (manifest + thin tool stubs) for a brand-new DB. Non-destructive. |
| `python scripts/schema_manifest_sync.py [--db slug]` | Report `config/schema.py` ↔ `manifest.yaml` table/column drift. |
| `python scripts/check_table_naming.py` | Enforce the canonical `_<db>` table-suffix convention (fails on new non-conforming tables; existing ones grandfathered). |
| `python scripts/preflight_schema_check.py --db <slug>` | Verify `config/schema.py` matches the actual parquet. |
| `python scripts/gen_compose.py` | Regenerate `docker-compose.yml` + `nginx_chat_routes.conf` from the manifests' `service:` blocks + the schema_kg DB set. |

## Adding a new database

```bash
# 0. Scaffold the author-input files (manifest + tool stubs).
python scripts/onboard_db.py --scaffold <slug>

# 1. Land the data + fill the manifest.
mkdir -p database/<slug>                  # drop the parquet tables here
$EDITOR dbs/<slug>/manifest.yaml          # fill the TODOs; see "Manifest format"

# 2. Declare the joinable schema for the planner. config/schema.py is
#    HAND-MAINTAINED and authoritative for joins. Scaffold a starting block
#    (non-destructive — prints only), then apply the single-PK-per-master rule
#    and drop any column absent from the actual parquet:
python scripts/schema_manifest_sync.py --emit <slug>

# 3. Build the schema_kg planner inputs (schema/queryable/concept_type/rules/
#    questions JSON). See evaluation/schema_kg/inputs/README.md for the file contract and
#    schema_kg/src/build.py for the builder. Dropping
#    schema_kg/inputs/<slug>/schema.json AUTO-REGISTERS the DB as schema_kg-
#    enabled: config/schema_kg_dbs.py discovers it, so gen_compose.py (lean image
#    + nginx route) and the schema_mapper / schema_planner warm-lists all pick it
#    up with NO further edits.

# 4. Fill the tool code. app/tools/<slug>/app/{main.py, <slug>.py, database_loader.py}
#    were stubbed by --scaffold. The capabilities/limitations text in <slug>.py IS
#    the router's input. Copy a lean DB (e.g. uniprot) for the loader pattern;
#    clean DBs need zero hooks. (Optional generic hooks — pre_join, partner_denorm,
#    text2sql — are config-driven; see app/per_db_tool/.)

# 5. Register the remaining human-facing surfaces:
#    - resources/prompts/db_notes.yaml          (summarizer notes — REQUIRED)
#    - resources/prompts/db_llm_rules.yaml      (per-layer LLM nudges — OPTIONAL)
#    - frontend/configs/db_chats.json           (unified-chat UI list)
#    - resources/db_profiles/registry.md        (human registry doc)
#    Set the Docker service config under `service:` in the manifest, then:
python scripts/gen_compose.py

# 6. Verify everything is wired:
python scripts/onboard_db.py --check <slug>          # all REQUIRED surfaces present?
python scripts/preflight_schema_check.py --db <slug> # schema.py ↔ parquet
python scripts/schema_manifest_sync.py  --db <slug>  # schema.py ↔ manifest drift

# 7. Build + bring up:
docker compose build biochirp_<slug>_tool && docker compose up -d biochirp_<slug>_tool
```

`--check` distinguishes **REQUIRED** surfaces (the DB won't work without them)
from **opt** surfaces (UI list, registry doc, optional LLM rules). A DB is
"READY" once every REQUIRED row is ✓.

## Manifest format

A minimum-viable manifest (the `--scaffold` template, with TODOs filled):

```yaml
name: my_new_db                        # slug: lowercase, [a-z][a-z0-9_]+
display_name: MyNewDB                  # human-readable
description: |
  Three- to five-sentence prose describing what this database contains,
  what biomedical entities it answers questions about, and what it does NOT
  cover. The first ~1500 chars feed the BGE-small embedding + the router, so
  be specific and ground every example in the actual data.
version: "1.0"
license: CC-BY-SA 4.0
sources:
  - https://my-db-source.org/

schema:
  inputs:                               # CommonField slugs (gene_symbol, drug_name, …)
    - field: gene_symbol
      examples: [TP53, KRAS, EGFR]
  outputs:                              # what the DB emits
    - field: drug_name
  tables:                               # documentation; key_columns are a curated subset
    main_table:
      description: One-line table summary
      key_columns:
        - {name: col1, description: What this column holds + a real example}

# Docker / nginx service spec — gen_compose.py reads this block directly.
service:
  tool:  {port: 80NN, workers: 2, memory_limit: 4g}
  chat:  {port: 82NN, db_name: MyNewDB}

# Optional author overrides
valid_queries:   ["An example question this DB CAN answer."]
invalid_queries: ["A question it CANNOT answer (use OtherDB — reason)."]
explicit_id_patterns:
  - {regex: 'MYDB-\d+', description: "MyNewDB accession format"}
```

`config/schema.py` (planner join schema) is the authoritative column set;
`manifest.yaml` `key_columns` are a curated documentation subset, so
`schema_manifest_sync.py` will report some columns present in one but not the
other. FK `*_id` join columns (in schema.py, not in manifest key_columns) and
single-PK-omitted xref columns (in manifest, not joinable) are **expected** and
by-design.

## Relationship to other files

| File | Role |
|---|---|
| `dbs/<slug>/manifest.yaml` | Human-authored docs + Docker service spec |
| `config/schema.py` `database_schemas[slug]` | Authoritative joinable schema (planner) |
| `evaluation/schema_kg/inputs/<slug>/` | Schema-KG planner inputs — see its [README](../evaluation/schema_kg/inputs/README.md) |
| `resources/prompts/db_notes.yaml` | Per-DB summarizer notes |
| `resources/prompts/db_llm_rules.yaml` | Per-layer per-DB LLM nudges (SSOT) |
| `app/tools/<slug>/` | The per-DB tool service (thin wrapper over `app/per_db_tool/`) |

Remote-API DBs (no parquet container) set `is_remote: true` and leave `service:`
empty; `--check` marks the container/schema_kg surfaces N/A for them.
