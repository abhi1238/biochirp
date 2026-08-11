# BioChirp architecture

A federated retrieval system over 43 curated biomedical databases. This
document maps the major components and the data + decision flow between
them. For per-DB authoring see [`dbs/README.md`](dbs/README.md). For graph
router internals see [`kg/README.md`](kg/README.md).

## High-level shape

```
                 USER QUESTION
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Entry points                                                       │
│  • /bio_chat_v2      (multi-DB chat, port 8030)                     │
│  • /bio_chat_<db>    (per-DB chat, ports 8028 …, one per DB)        │
│  • MCP server        (Claude Desktop / agent integrations)          │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
            ┌────────────────────────────────────────────┐
            │ Decomposer (decomposer.py)                 │
            │ Splits multi-step questions into a DAG of  │
            │ atomic sub-queries.                        │
            └────────────────────────┬───────────────────┘
                                     ▼
            ┌────────────────────────────────────────────┐
            │ Per-step interpreter                       │
            │ NER + entity-type extraction → ParsedValue │
            └────────────────────────┬───────────────────┘
                                     ▼
            ┌────────────────────────────────────────────┐
            │ DB SELECTOR — picks which DBs to query     │
            │                                            │
            │ Two implementations, toggled by env flag:  │
            │   semantic_db_selector  (legacy, default)  │
            │     cosine + LLM rerank                    │
            │   graph_db_selector     (new, opt-in via   │
            │     BIOCHIRP_USE_GRAPH_ROUTER=1)           │
            │     7-stage deterministic graph + optional │
            │     LLM precision filter (Stage 8)         │
            └────────────────────────┬───────────────────┘
                                     ▼
            ┌────────────────────────────────────────────┐
            │ Per-DB tool services (one per DB)          │
            │ HTTP services running shared per_db_tool   │
            │ library; each holds a parquet snapshot or  │
            │ proxies a remote API (AlphaFold, gnomAD,…).│
            └────────────────────────┬───────────────────┘
                                     ▼
            ┌────────────────────────────────────────────┐
            │ Synthesizer                                │
            │ Merges per-DB tables → final answer        │
            └────────────────────────────────────────────┘
```

## Directory map (canonical)

| Path | What lives here | Source-of-truth? |
|---|---|---|
| `dbs/<slug>/manifest.yaml` | **Per-DB authoring surface** (description, schema, valid/invalid queries, ID patterns) | **Yes** (post-onboarding) |
| `config/services_registry.yaml` | Per-DB Docker service config (port, workers, memory, depends_on) | Will be merged into manifests (Tier 2.1) |
| `kg/` | Knowledge graph used by `graph_db_selector` | Yes (4 YAMLs auto-derived from manifests) |
| `bio_chat_service/app/` | Multi-DB orchestrator service (port 8030); routing + decomposition + synthesis | Yes |
| `app/per_db_tool/` | Shared library for the 25 parquet-backed per-DB tool services | Yes |
| `app/per_db_chat/` | Shared library for the 25 per-DB chat services | Yes |
| `app/tools/<db>/` | DB-specific tool logic (one subdir per parquet DB) | Yes |
| `app/chat/<db>/` | DB-specific chat function bindings | Yes |
| `app/utils/` | Shared utilities: `column_embeddings.py`, `dataframe_loader.py`, `term_routing.py`, … | Yes |
| `mcp_server/` | MCP federation server (Claude Desktop tools); router_v2, relation_index | Yes |
| `mcp_server/domains/configs/*.yaml` | Remote-API DB definitions (18 DBs: AlphaFold, gnomAD, GTEx, openFDA, …) | Yes |
| `mcp_server/db_tool_specs.json` | Per-DB inputs/outputs for MCP tools | **Derived** (from manifests via `onboard_db.py`) |
| `opentarget_service/` | Open Targets live-GraphQL aggregator (port 8026) | Yes (active) |
| `orchestrator_service/` | Query planner + KG builder (port 8032) | Yes |
| `frontend/` | Static HTML/JS chat UIs (vanilla JS, WebSocket-based) | Yes |
| `bench/fedbench/` | 216-question labelled bench + `shadow_eval.py` harness | Yes |
| `bench/legacy/` | Pre-fedbench results + runners (archival) | Archival |
| `evaluation/` | Paper-deliverable evaluation suites (Agentic_SQL, MCP, MCQ, OpenTarget, semantic_member_selection, same_question_robustness) | Yes |
| `resources/` | Per-column descriptions, embeddings (`.npz`), value caches (`.pkl`), prompts | Yes |
| `scripts/` | Generators (`gen_compose.py`, `onboard_db.py`, `extract_manifests.py`), audits, migrations | Yes |
| `audit_reports/` | Compliance + round-N audit dumps (licensing, schema, prompt, web availability, routing) | Archival |
| `deploy/` | nginx configs + systemd service files | Yes |
| `config/guardrail.py`, `config/schema.py`, `config/attributions.py`, `config/provenance.py` | Type/safety/schema constants | Yes |
| `litellm_config.yaml` | LiteLLM proxy: model aliases (deepseek-v4-flash, gpt-4.1-mini, …) | Yes |
| `tests/` | pytest unit tests (currently minimal) | Yes |

## How a request flows (concrete example)

Question: *"What drugs target EGFR and what diseases are they approved for?"*

```
1. POST /bio_chat_v2  (bio_chat_service:8030)
       │
2. decomposer.py → 2-step DAG
       │  step 1: "What drugs target EGFR?"
       │  step 2: "What diseases are <drugs from step 1> approved for?"
       │
3. For each step:
       │
       ├─ interpreter_tool → ParsedValue{gene_symbol: [EGFR]}
       │
       ├─ DB SELECTOR
       │    (if BIOCHIRP_USE_GRAPH_ROUTER=1)
       │      → graph_db_selector.select(question, entity_types)
       │      → ['ChEMBL', 'DGIdb', 'DrugCentral', 'TTD', ...]
       │
       ├─ For each selected DB (in parallel):
       │    → POST to biochirp_<db>_tool (port from services_registry.yaml)
       │    → DB tool: planner → expand_and_match_db → parquet query
       │    → returns rows
       │
       └─ Harvest values for the next step's substitution
                       │
                       ▼
4. _synthesize() merges all step results → final answer
       │
5. WebSocket streams tool cards + final text to the UI (frontend/)
```

## Routing systems (today)

Two routers exist in parallel:

| Router | Where | When it runs | Tech |
|---|---|---|---|
| `semantic_db_selector` (legacy) | `bio_chat_service/app/semantic_db_selector.py` | Default (env flag off) | Cosine over `db_catalog.embed_text` → `gpt-4.1-nano` LLM rerank |
| `graph_db_selector` (new) | `bio_chat_service/app/graph_db_selector.py` | When `BIOCHIRP_USE_GRAPH_ROUTER=1` | 7-stage deterministic graph (BioChirpKG) + optional Stage 8 LLM filter (deepseek-v4-flash) |

Both implementations live behind the same `_select_dbs_for_step()` interface in `pipeline_v2.py` — switching is a one-env-var flip with legacy fallback on errors.

See [`kg/README.md`](kg/README.md) for the graph router's full design and the seven SoT files it consumes.

## Onboarding a new DB

**Two parallel registries today** (will converge in Tier 2.1):

1. `dbs/<slug>/manifest.yaml` — authoring surface for routing artifacts (see [`dbs/README.md`](dbs/README.md))
2. `config/services_registry.yaml` — authoring surface for Docker service config (port, workers, depends_on)

After editing both:

```bash
python -m scripts.onboard_db <slug>     # routing artifacts (db_catalog, relation_db_map, triggers, …)
python scripts/gen_compose.py           # docker-compose.yml + nginx_chat_routes.conf
docker compose up -d biochirp_<slug>_tool biochirp_<slug>_chat
```

After Tier 2.1 these merge into a single `manifest.yaml` with a `service:` block, run by a single command.

## Capabilities at a glance

| Capability | Status |
|---|---|
| Federated multi-DB query routing | ✅ Production (legacy router); ✅ graph router opt-in |
| MCP federation for external agents | ✅ Production |
| 25 parquet-backed DBs + 18 remote-API DBs | ✅ Production |
| Query decomposition (multi-step DAG) | ✅ Production |
| Entity expansion + fuzzy + synonym matching | ✅ Production |
| Column-aware DB selection | ✅ MCP side only; chat side via opt-in graph router |
| Relation classification (19 relations) | ✅ Production |
| LLM-based precision filter (deepseek-v4-flash) | ✅ Built, opt-in via `BIOCHIRP_GRAPH_LLM_FILTER_ENABLED=1` |
| Auto-DB onboarding from a single manifest | ✅ Built (`scripts/onboard_db.py`); 43 DBs migrated |
| Reproducible decisions (graph hash + model hash stamped on every routing) | ✅ Built |
| 216-question labelled bench, 100 % hit_min | ✅ Built (`bench/fedbench/`) |
| Vanilla-JS WebSocket UI (multi-DB + per-DB chats) | ✅ Production |
| Sharing + provenance disclaimers (regulatory) | ✅ Production |
| Licensing/safety audits | ✅ Compliance |

## Env-flag glossary (what each toggles)

| Env var | Default | What it does |
|---|---|---|
| `BIOCHIRP_USE_GRAPH_ROUTER` | `0` | When `1`, `pipeline_v2._select_dbs_for_step()` routes through the deterministic graph instead of cosine+LLM |
| `BIOCHIRP_GRAPH_ROUTER_SHADOW` | `0` | When `1`, also runs legacy router and logs per-step diffs for audit |
| `BIOCHIRP_GRAPH_LLM_FILTER_ENABLED` | `0` | When `1`, applies Stage 8 LLM precision filter to graph output |
| `BIOCHIRP_GRAPH_LLM_FILTER_MODEL` | `deepseek-v4-flash` | LiteLLM alias for the filter model |
| `BIOCHIRP_GRAPH_LLM_FILTER_TOP_K` | `12` | Final DB count when filter is enabled |
| `BIOCHIRP_V2_PER_STEP_DBS` | `5` | Per-decomposition-step DB cap (legacy router) |
| `BIOCHIRP_V2_PER_DB_TIMEOUT` | `75` | Per-DB call timeout in seconds |
| `LITELLM_BASE_URL` | `http://biochirp_litellm:4000/v1` | LiteLLM proxy URL; override for dev / external |

## Decisions log

| Date | Decision |
|---|---|
| 2026-05-19 | Per-DB services consolidated into `app/per_db_chat` + `app/per_db_tool` shared libraries (commit `28b4ec5`) |
| 2026-05-19 | `config/services_registry.yaml` established as SoT for Docker side; `scripts/gen_compose.py` generates compose |
| 2026-05-20 | Graph router (`graph_db_selector.py` + `kg/`) built and shipped behind env flag |
| 2026-05-20 | Per-DB routing-side manifests (`dbs/<slug>/manifest.yaml`) + `scripts/onboard_db.py` shipped |
| 2026-05-20 | LLM precision filter (Stage 8, deepseek-v4-flash default) shipped behind env flag |
| 2026-05-20 | 216-question labelled bench (questions_v0 + researcher_bench) at 100 % hit_min |
