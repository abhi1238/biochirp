# BioChirp Methods (Implementation-Level)

This document provides method-level detail aligned with the current codebase for technical reporting and manuscript preparation.

## 1. System Architecture and Deployment

BioChirp is implemented as a distributed set of FastAPI microservices orchestrated via Docker Compose.
Each service has a narrow function boundary (interpreter, planner, matching, source retrieval, web enrichment, summarization), and all services communicate over the `semantic_net` Docker network.

### Interfaces

- HTTP endpoints for health checks, tool calls, and file download.
- WebSocket endpoints for real-time chat orchestration and progressive output streaming.

Primary WebSocket entry points:

- `orchestrator_service`: `/chat`
- `opentarget_service`: `/opentarget`
- `ttd_service`: `/ttd_chat`
- `ctd_service`: `/ctd_chat`
- `hcdt_service`: `/hcdt_chat`

### Stateful orchestration

The orchestrator creates one agent context per WebSocket connection, tracks last-turn memory, dispatches tools, and streams events (`tool_called`, `tool_result`, `delta`, `final`, `error`).
Redis is used for pub/sub event relay and short-lived snapshot sharing.

### Containerized deployment

The deployment is defined in `docker-compose.yml`, with explicit host ports per service and an external network requirement.
Qdrant is run as a separate container and used by semantic matching.

## 2. Query Interpretation and Routing

Interpretation occurs before retrieval to enforce schema compatibility.

### 2.1 Input normalization

The interpreter converts free-text user questions into a structured request representation including:

- normalized intent
- constrained fields
- extracted entities/values
- route metadata

This stage supports multiple LLM providers via environment-selected models.

### 2.2 Route selection

Routing determines whether a query should go to:

- local curated database pipeline (`TTD`, `CTD`, `HCDT`)
- OpenTargets online pipeline
- web/tavily fallback tools

### 2.3 Scope guardrails

The system restricts structured retrieval to known schema fields, reducing invalid joins and out-of-schema generation.

## 3. Offline Retrieval from Curated Local Databases

Local retrieval operates over curated Parquet snapshots under `database/ttd`, `database/ctd`, and `database/hcdt`.

### 3.1 Database curation and schema layer

The canonical schema is defined in `config/schema.py`.

- master tables (`*_master_table`) represent normalized entity dictionaries
- association tables (`*_association`) represent relations

At startup, schema validation enforces:

- non-empty tables
- unique column names per table
- exactly one `_id` key for each master table

Primary keys and foreign keys are generated programmatically from the schema definition.

### 3.2 Entity resolution for local KB execution

Before planning, input values are expanded and matched using a hybrid strategy:

1. Synonym expansion tools
2. Fuzzy string matching (`fuzzy` service)
3. Semantic retrieval against Qdrant vectors (`semantic_filter` service)
4. LLM-based candidate filtering / disambiguation (`llm_member_filter` service)

The composite output is a constrained set of candidate values per requested field.

### 3.3 Deterministic query planning (graph-based)

Planner implementation: `app/tools/planner/app/graph.py`.

Given requested concept columns, the planner:

1. Builds a table graph from foreign keys.
2. Maps each concept to all tables containing that column.
3. Finds a connected cover (greedy default; exhaustive optional).
4. Builds a spanning tree and parent mapping.
5. Produces explicit join pairs (`left_on`, `right_on`) for deterministic execution.

Planner controls include:

- `USE_GREEDY_ALGORITHM`
- `MAX_COMBINATIONS`
- `MAX_TABLES_IN_COVERAGE`
- `STEINER_TIMEOUT_SECONDS`

### 3.4 Query execution and output table generation

Database tool services (`ttd`, `ctd`, `hcdt`) execute using the generated plan and resolved candidates, returning structured tables and optional CSV artifacts.
Each table payload includes preview rows plus metadata (e.g., row counts and file paths for downloads).

## 4. Online Retrieval via OpenTargets API

OpenTargets retrieval is served by `opentarget_service` and differs from local SQL-like planning.

### 4.1 Online entity grounding and resolution

The pipeline resolves user entities to OpenTargets-compatible identifiers before retrieval.
Both direct and fallback resolution paths are supported.

### 4.2 GraphQL tooling and pagination

OpenTargets retrieval tools perform GraphQL queries with paging support to avoid truncation in large result sets.
The service streams preview rows and downloadable CSV outputs similarly to local DB services.

### 4.3 Hybrid fallback behavior

If direct online retrieval is insufficient for user intent, the orchestrated route can invoke web/Tavily tools for supporting context.

## 5. Response Synthesis and Streaming Output

BioChirp returns both machine-usable tables and human-readable summaries.

### 5.1 Evidence-first synthesis

Summarization operates on retrieved outputs, not unconstrained generation.
The pipeline maintains source context and tool outputs during synthesis.

### 5.2 Real-time streaming protocol

WebSocket messages stream incremental progress:

- tool lifecycle events
- partial text deltas
- table previews
- final completion marker

This supports UI transparency and allows users to inspect intermediate evidence.

### 5.3 Structured export

CSV outputs are persisted under `results/` and retrievable through `/download` endpoints.

## 6. Benchmark and Evaluation Framework

Evaluation workflows are under `evaluation/` and include repeated-run robustness analyses across models and sources.

### 6.1 Cross-run/cross-model agreement

Current analyses include directional coverage matrices and symmetric set-agreement metrics (Jaccard, Dice) computed over normalized identifier-based rows.

### 6.2 Identifier grounding

Cross-source comparisons are standardized by identifier grounding (e.g., Ensembl, ontology IDs, ChEMBL) to reduce lexical mismatch artifacts.

### 6.3 Reportable reproducibility metadata

For manuscript reporting, record:

- commit hash and image versions
- environment variable snapshot (without secrets)
- hardware and runtime specs
- data snapshot versions and download dates
- exact benchmark question sets and run counts

See `docs/REPRODUCIBILITY.md` for a full checklist.
