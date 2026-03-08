# BioChirp

BioChirp is a database-first biomedical AI platform for structured biological question answering.
It combines curated local biomedical databases, graph-based query planning, identifier-aware entity resolution, and controlled LLM orchestration.

- Live: https://biochirp.iiitd.edu.in
- Issue tracker: https://github.com/abhi1238/biochirp/issues

## Table of Contents

- [What BioChirp Solves](#what-biochirp-solves)
- [System Overview](#system-overview)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Required Data Artifacts](#required-data-artifacts)
- [Quick Start (Local)](#quick-start-local)
- [Service Endpoints](#service-endpoints)
- [Customization](#customization)
- [Graph-Based Planner Details](#graph-based-planner-details)
- [Evaluation and Reproducibility](#evaluation-and-reproducibility)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)
- [License and Citation](#license-and-citation)

## What BioChirp Solves

General-purpose chat LLMs are strong at language but weak at schema-constrained biomedical retrieval.
BioChirp addresses this by making retrieval deterministic and auditable:

- Retrieval first, summarization second.
- Schema-aware joins across curated databases (`TTD`, `CTD`, `HCDT`).
- Online retrieval path for `OpenTargets` with entity grounding and pagination.
- Streaming WebSocket responses with tool-level events and table previews.

## System Overview

BioChirp runs as Dockerized FastAPI microservices on a shared network.

Primary flow:

1. Query arrives at orchestrator (`/chat`) or source-specific chat service.
2. Interpreter extracts intent and field constraints.
3. Expand-and-match pipeline resolves entities via synonym expansion + fuzzy + semantic + LLM filtering.
4. Planner generates a graph-based table join plan from schema foreign keys.
5. DB services execute deterministic retrieval from local Parquet snapshots.
6. Optional OpenTargets/web tools are used for online scope.
7. Final response is streamed with table preview + downloadable CSV path.

## Repository Structure

```text
biochirp/
├── app/
│   ├── tools/                     # Interpreter/planner/fuzzy/semantic/db tools
│   ├── services/                  # Shared service logic (semantic/synonym, etc.)
│   └── utils/                     # Shared dataframe and helper utilities
├── orchestrator_service/          # Main orchestrator WebSocket service
├── opentarget_service/            # OpenTargets orchestrated service
├── ttd_service/                   # TTD chat service
├── ctd_service/                   # CTD chat service
├── hcdt_service/                  # HCDT chat service
├── config/                        # Schema, guardrails, settings
├── database/                      # Local curated DB snapshots
├── resources/
│   ├── prompts/
│   ├── values/
│   └── embeddings/
├── evaluation/                    # Benchmark and robustness analyses
├── frontend/                      # Static HTML clients
├── docker-compose.yml
└── README.md
```

## Prerequisites

- Docker Engine 24+
- Docker Compose v2
- Linux/macOS shell
- Jupyter Notebook (only for first-time embedding ingest)

Recommended hardware (practical):

- RAM: 64 GB+ (CTD and semantic services are memory-heavy)
- CPU: 12+ cores
- GPU optional (some services request GPU in compose; can run CPU-only with small compose edits)

## Required Data Artifacts

BioChirp expects the following local artifacts:

1. **Curated database snapshots** under:
   - `database/ttd/`
   - `database/ctd/`
   - `database/hcdt/`

2. **Concept value dictionary**:
   - `resources/values/concept_values_by_db_and_field.pkl`

3. **Embedding pickle for semantic matching**:
   - `resources/embeddings/biochirp_embeddings.pkl`

4. **Qdrant storage directory** (created automatically by Docker mount):
   - `qdrant_storage/`

Project data bundle used by this repo:
https://drive.google.com/drive/folders/1E6RmupO3Oa3tUFRzZAB-ueUTZkZnpKgU?usp=sharing

## Quick Start (Local)

### 1) Clone

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp
```

### 2) Configure environment

Create a local env file from template:

```bash
cp .env.example .env
```

Edit `.env` with your keys and model names.

### 3) Create Docker network (required by compose)

`docker-compose.yml` uses an **external** network named `semantic_net`.

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

If it already exists, Docker will report it and continue.

### 4) Start Qdrant on the same network

```bash
docker run -d \
  --name bioc_qdrant \
  --network semantic_net \
  -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant:latest
```

### 5) Ingest embeddings into Qdrant (first-time only)

Use either notebook:

- `qdrant_without_text_index.ipynb`
- `qdrant_ingest.ipynb`

Set Qdrant URL to `http://localhost:6333` and run all cells.

### 6) Launch BioChirp services

```bash
docker compose up --build -d
```

### 7) Verify health

```bash
curl http://localhost:8010/health
curl http://localhost:8026/health
curl http://localhost:8028/health
curl http://localhost:8031/health
curl http://localhost:8029/health
```

### 8) View logs

```bash
docker compose logs -f --tail=200
```

## Service Endpoints

### WebSocket chat endpoints

- Main orchestrator: `ws://localhost:8010/chat`
- OpenTargets chat: `ws://localhost:8026/opentarget`
- TTD chat: `ws://localhost:8028/ttd_chat`
- CTD chat: `ws://localhost:8031/ctd_chat`
- HCDT chat: `ws://localhost:8029/hcdt_chat`

### HTTP endpoints

- Health: `GET /health` on each service
- Download (CSV):
  - Orchestrator: `GET http://localhost:8010/download?path=<file>`
  - OpenTargets: `GET http://localhost:8026/download?path=<file>`
- Share snapshot:
  - `POST /share` and `GET /s/{share_id}` on orchestrator and DB chat services

### Tool service ports (from compose)

| Service | Port |
|---|---:|
| interpreter | 8005 |
| web | 8006 |
| readme | 8007 |
| tavily | 8008 |
| expand_and_match_db | 8009 |
| orchestrator | 8010 |
| planner | 8011 |
| ttd tool | 8012 |
| fuzzy | 8013 |
| synonyms expander | 8014 |
| semantic filter | 8015 |
| ctd tool | 8016 |
| llm member filter | 8017 |
| hcdt tool | 8018 |
| opentarget service | 8026 |
| ttd chat service | 8028 |
| hcdt chat service | 8029 |
| ctd chat service | 8031 |
| unrestricted synonym expander | 8032 |

### WebSocket payload example

Send:

```json
{"user_input": "What drugs are used to treat rickets?"}
```

Keepalive (optional):

```json
{"type": "ping"}
```

Common emitted event types:

- `user_ack`
- `tool_called`
- `tool_result`
- `delta`
- `ttd_table` / `ctd_table` / `hcdt_table`
- `heartbeat`
- `final`
- `error`

## Customization

All primary knobs are in `.env`.

### Model/provider configuration

- `OPENAI_API_KEY`
- `GROQ_API_KEY`
- `GEMINI_API_KEY`
- `GROK_KEY` (some components also accept `GROK_API_KEY`)
- `TAVILY_API_KEY`

Model selectors:

- `INTERPRETER_MODEL_NAME`
- `INTERPRETER_AGENT_SCHEMA_MAPPER_MODEL_NAME`
- `ORCHESTRATOR_MODEL_NAME`
- `SUMMARIZER_MODEL_NAME`
- `WEB_MODEL_NAME`
- `LLM_FILTER_MODEL_NAME`
- `SEMANTIC_MATCHING_MODEL_NAME`

### Planner and routing controls

- `AGENT_TIMEOUT_SEC`
- `ROUTE_TIMEOUT_SEC`
- `MAX_TIMEOUT`
- `OPENAI_HTTP_TIMEOUT`
- `WEB_TIMEOUT_SEC`
- `WEB_TOOL_TIMEOUT`

Graph planner controls:

- `USE_GREEDY_ALGORITHM`
- `MAX_COMBINATIONS`
- `MAX_TABLES_IN_COVERAGE`
- `STEINER_TIMEOUT_SECONDS`

### Matching controls

- `FUZZY_SCORE_CUT_SCORE`
- `USE_KNEE_CUT_OFF`
- `KNEE_CUT_OFF`

### Preview controls

- `HEAD_VIEW_ROW_COUNT`
- `OT_PREVIEW_ROWS`

## Graph-Based Planner Details

Planner implementation: `app/tools/planner/app/graph.py`

The planner builds a minimal connected table cover for requested concepts over schema FK topology:

1. Build table graph from `foreign_keys_by_db`.
2. Map requested concept columns to candidate tables.
3. Find connected cover using greedy or exhaustive strategy.
4. Construct spanning tree and explicit join pairs (`left_on`, `right_on`).
5. Return ordered table plan + parent map + per-table concept columns.

This design keeps join logic deterministic and auditable.

## Evaluation and Reproducibility

- Benchmarks and robustness analyses: `evaluation/`
- Methods details: `docs/METHODS.md`
- Reproducibility package guidance: `docs/REPRODUCIBILITY.md`

## Troubleshooting

### `network semantic_net not found`

Create it manually:

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

### Qdrant connection failures in semantic service

- Ensure container is named `bioc_qdrant`.
- Ensure it is attached to `semantic_net`.
- Ensure ports `6333`/`6334` are exposed.

### GPU reservation errors on CPU-only host

In `docker-compose.yml`, remove `deploy.resources.reservations.devices` sections for services that request NVIDIA GPU (notably semantic/opentarget services).

### Very slow startup

First startup loads large models/resources. Use:

```bash
docker compose logs -f --tail=200
```

### Frontend points to production URLs

`frontend/*.html` pages use hardcoded host values (e.g., `biochirp.iiitd.edu.in`).
For local use, change host constants to `localhost:<port>` in the corresponding HTML/JS.

## Security Notes

- Never commit real API keys.
- Keep `.env` local and use `.env.example` for templates.
- Rotate keys immediately if exposed.
- Review logs before sharing (`docker compose logs`) because tool traces may include sensitive query text.

## License and Citation

- License: MIT (`LICENSE`)
- If you use BioChirp in research, cite the project repository and the manuscript/preprint once available.

---

For method-level manuscript documentation, use:

- `docs/METHODS.md`
- `docs/REPRODUCIBILITY.md`
