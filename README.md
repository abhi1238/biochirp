# BioChirp

**Database-first biomedical question answering with deterministic retrieval and controlled summarization.**

BioChirp routes natural-language biomedical questions to curated knowledge bases (TTD, CTD, HCDT) and online sources (OpenTargets) through schema-grounded graph planning, hybrid entity resolution, and evidence-anchored LLM summarization. It is distributed as a set of FastAPI microservices orchestrated with Docker Compose.

- **Live demo**: https://biochirp.iiitd.edu.in
- **Code**: https://github.com/abhi1238/biochirp
- **Issues**: https://github.com/abhi1238/biochirp/issues
- **Companion docs**: [docs/METHODS.md](docs/METHODS.md) (implementation detail) · [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) (manuscript checklist)

---

## Table of contents

1. [Reviewer express path (≤ 30 min)](#1-reviewer-express-path--30-min)
2. [System overview](#2-system-overview)
3. [Prerequisites](#3-prerequisites)
4. [Data artifacts](#4-data-artifacts)
5. [Full setup from scratch](#5-full-setup-from-scratch)
6. [Verifying reproduction](#6-verifying-reproduction)
7. [Reproducing paper evaluations](#7-reproducing-paper-evaluations)
8. [Troubleshooting](#8-troubleshooting)
9. [Repository map](#9-repository-map)
10. [Citation, license, contact](#10-citation-license-contact)

---

## 1. Reviewer express path (≤ 30 min)

This path assumes you download the pre-built data bundle (Google Drive) and run everything with Docker Compose. No embedding ingest, no preprocessing — everything ships ready-to-use.

### 1.1 Download the data bundle

We distribute all non-code artifacts (Qdrant snapshot, Parquet databases, concept value dictionary) as a single archive on Google Drive:

- **Bundle URL**: `<GOOGLE_DRIVE_BUNDLE_URL>` *(replace with the share link before release)*
- **Bundle name**: `biochirp_data_bundle.zip`
- **Size**: ~17 GB compressed (~20 GB extracted)

Contents of the bundle, extracted at repo root:

```
biochirp/
├── qdrant_storage/                                # ~17 GB, Qdrant collections
├── database/ttd/*.parquet                         # 10 files
├── database/ctd/*.parquet                         # 9 files
├── database/hcdt/*.parquet                        # 8 files
├── database/drugcentral/*.parquet                 # 13 files (v11012023, CC BY-SA 4.0)
└── resources/values/concept_values_by_db_and_field.pkl   # ~76 MB
```

### 1.2 One-shot bring-up

```bash
# Clone and enter the repo
git clone https://github.com/abhi1238/biochirp.git
cd biochirp

# Extract the downloaded bundle at repo root (overwrites empty placeholders)
unzip ~/Downloads/biochirp_data_bundle.zip -d .

# Configure environment (edit .env after copy — at minimum set OPENAI_API_KEY)
cp .env.example .env
$EDITOR .env

# Create the Docker network BioChirp compose expects
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net || true

# Start Qdrant on that network, pointing at the extracted snapshot
docker rm -f bioc_qdrant >/dev/null 2>&1 || true
docker run -d --name bioc_qdrant --network semantic_net \
  -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant:latest

# Build and start all BioChirp services
docker compose up --build -d
```

First build takes 20–40 min depending on hardware (PyTorch + embedding models dominate). Subsequent `up` runs take seconds.

### 1.3 Smoke test (≤ 2 min)

```bash
# Health checks
for u in \
  http://localhost:6333/readyz \
  http://localhost:8028/health \
  http://localhost:8031/health \
  http://localhost:8029/health \
  http://localhost:8045/health \
  http://localhost:8026/health \
  http://localhost:8044/health \
  http://localhost:8011/health \
  http://localhost:8015/health; do
  printf "%-45s" "$u"
  curl -fsS "$u" >/dev/null && echo "OK" || echo "FAIL"
done
```

Expected: **all OK**. If any fail, see [§8 Troubleshooting](#8-troubleshooting).

### 1.4 Live query via frontend (optional)

```bash
python3 -m http.server 8080 --directory frontend
```

Then open one of:

- http://localhost:8080/ttd_chat_api.html
- http://localhost:8080/ctd_chat_api.html
- http://localhost:8080/hcdt_chat_api.html
- http://localhost:8080/drugcentral_chat_api.html
- http://localhost:8080/opentarget_api.html

These pages are pre-wired to `ws://localhost:80{28,31,29,45,26}` and stream tool events as they arrive. Try the canonical test query from [§6](#6-verifying-reproduction).

---

## 2. System overview

### 2.1 What runs where

| Role | Service name | Host port | Purpose |
|---|---|---|---|
| **User-facing chat** | `biochirp_ttd_chat` | `8028` | WebSocket orchestrator for TTD questions |
| **User-facing chat** | `biochirp_ctd_chat` | `8031` | WebSocket orchestrator for CTD questions |
| **User-facing chat** | `biochirp_hcdt_chat` | `8029` | WebSocket orchestrator for HCDT questions |
| **User-facing chat** | `biochirp_drugcentral_chat` | `8045` | WebSocket orchestrator for DrugCentral questions |
| **User-facing chat** | `biochirp_opentargets` | `8026` | OpenTargets GraphQL pipeline |
| Planner | `biochirp_planner_tool` | `8011` | Graph-based query plan (concepts → tables → joins) |
| Entity: synonyms | `biochirp_synonyms_expander` | `8014` | Synonym/variant expansion over schema |
| Entity: synonyms (free) | `biochirp_synonyms_expander_unrestricted` | `8032` | Unrestricted expansion variant |
| Entity: fuzzy | `biochirp_fuzzy_tool` | `8013` | Fuzzy string matching against concept dictionary |
| Entity: semantic | `biochirp_semantic_tool` | `8015` | Qdrant-backed dense retrieval (GPU-preferred) |
| Entity: compose | `biochirp_expand_and_match_db_tool` | `8009` | Orchestrates the four entity resolvers |
| Data: TTD | `biochirp_ttd_tool` | `8012` | Executes plans on TTD parquet |
| Data: CTD | `biochirp_ctd_tool` | `8016` | Executes plans on CTD parquet |
| Data: HCDT | `biochirp_hcdt_tool` | `8018` | Executes plans on HCDT parquet |
| Data: DrugCentral | `biochirp_drugcentral_tool` | `8044` | Executes plans on DrugCentral parquet (v11012023) |
| Enrichment | `biochirp_web_tool` | `8006` | Web search tool wrapper |
| Enrichment | `biochirp_tavily_tool` | `8008` | Tavily API wrapper |
| Enrichment | `biochirp_readme_tool` | `8007` | README/tool doc retrieval |
| Interpretation | `biochirp_interpreter_tool` | `8005` | NL → structured request |
| Vector store | `bioc_qdrant` *(external)* | `6333/6334` | Qdrant — semantic embeddings |
| Cache / pub-sub | `biochirp_redis_tool` | — | Redis 7, used by chat services |

All containers share the `semantic_net` bridge network (subnet `172.35.0.0/16`).

### 2.2 How a query flows

```
  user text
     │
     ▼
 interpreter (8005)  ──►  route selector
     │
     ├─► local DB route: expand_and_match_db (8009)
     │     │   ├─ synonyms (8014)
     │     │   ├─ fuzzy (8013)
     │     │   ├─ semantic/Qdrant (8015)
     │     │   └─ LLM filter (8017)
     │     ▼
     │   planner (8011)  →  ttd/ctd/hcdt_tool (8012/8016/8018)
     │                         │
     │                         ▼
     │                  Parquet execution + strict joins
     │
     └─► online route: opentarget (8026)  →  GraphQL + web/tavily fallback
     │
     ▼
 chat orchestrator (8028/8031/8029/8026)  →  WebSocket stream to user
```

Full implementation detail in [docs/METHODS.md](docs/METHODS.md).

---

## 3. Prerequisites

### 3.1 Software

| Component | Tested version | Notes |
|---|---|---|
| Docker Engine | 24+ | `docker --version` |
| Docker Compose | v2.20+ | bundled as `docker compose` subcommand |
| OS | Ubuntu 22.04 / 24.04 | macOS works for small tests; not validated for full eval |
| `curl`, `unzip` | any | for smoke tests and bundle extraction |

### 3.2 Hardware

| Resource | Minimum (CPU-only) | Recommended (paper-config) |
|---|---|---|
| RAM | 48 GB | **64 GB+** |
| CPU | 8 cores | 12+ cores |
| Disk | 60 GB free | 120 GB free (bundle + images + results) |
| GPU | none | 1× NVIDIA GPU with ≥ 12 GB VRAM (CUDA 12) |

**CPU-only note**: two services request NVIDIA reservations in `docker-compose.yml` — `biochirp_semantic_tool` and `opentargets`. On a CPU-only host, remove the `deploy.resources.reservations.devices` blocks for those two services before `docker compose up`. See [§8.3](#83-gpu-reservation-error-on-cpu-only-host).

### 3.3 API keys

You need at least an OpenAI key. Tavily is required only for web-search steps in evaluation.

```bash
cp .env.example .env
```

Edit `.env` and set:

```
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...        # optional, only for web search evaluations
```

The default `.env.example` pins model names used in the paper (`gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o-mini`). Changing these will change numerical results.

---

## 4. Data artifacts

### 4.1 What is shipped in git vs. in the bundle

| Path | In git? | In bundle? | Size | Purpose |
|---|---|---|---|---|
| `database/{ttd,ctd,hcdt}/*.parquet` | ❌ | ✅ | 115 MB total | Curated snapshots of TTD/CTD/HCDT |
| `database/drugcentral/*.parquet` | ❌ | ✅ | ~30 MB | DrugCentral v11012023 snapshot (CC BY-SA 4.0) |
| `database/{ttd,ctd,hcdt}/preprocess.ipynb` | ✅ | — | — | Re-build parquet from raw (optional) |
| `database/drugcentral/preprocess.py` | ✅ | — | — | Re-build DrugCentral parquet from SQL dump |
| `resources/values/concept_values_by_db_and_field.pkl` | ❌ | ✅ | ~76 MB | Concept dictionary for fuzzy/semantic matching |
| `qdrant_storage/` | ❌ | ✅ | ~16 GB | Pre-loaded Qdrant collections |
| `resources/prompts/*.md` | ✅ | — | — | LLM prompts (versioned with code) |
| `config/{schema,settings,guardrail}.py` | ✅ | — | — | Schema, runtime config, guardrails |
| `app/` (all services) | ✅ | — | — | Python source |
| `.env.example` | ✅ | — | — | Env template |

### 4.2 Bundle layout

The Google Drive archive `biochirp_data_bundle.zip` has this exact structure — unzipping at the repo root places every file in the right place:

```
qdrant_storage/collections/...
database/ttd/*.parquet
database/ctd/*.parquet
database/hcdt/*.parquet
database/drugcentral/*.parquet
resources/values/concept_values_by_db_and_field.pkl
```

### 4.3 Verify artifact presence

Run this after unzipping — it fails loudly if anything is missing, before you spend time on Docker:

```bash
set -e

# concept dictionary
test -f resources/values/concept_values_by_db_and_field.pkl && echo "values pickle: OK"

# parquet counts (snapshot used in the paper)
[ "$(find database/ttd        -maxdepth 1 -name '*.parquet' | wc -l)" = "10" ] && echo "ttd parquet: OK"
[ "$(find database/ctd        -maxdepth 1 -name '*.parquet' | wc -l)" =  "9" ] && echo "ctd parquet: OK"
[ "$(find database/hcdt       -maxdepth 1 -name '*.parquet' | wc -l)" =  "8" ] && echo "hcdt parquet: OK"
[ "$(find database/drugcentral -maxdepth 1 -name '*.parquet' | wc -l)" = "13" ] && echo "drugcentral parquet: OK"

# qdrant storage
[ -d qdrant_storage/collections ] && echo "qdrant_storage: OK"

echo "All artifacts present."
```

### 4.4 Rebuilding the bundle from raw (optional)

Each preprocessing pipeline ships in two equivalent forms: an `argparse`-driven script (preferred for CI and reproducibility) and the original notebook (preferred for interactive exploration). Both produce the same parquet output when run on the same input.

#### Script form (recommended)

```bash
# Discover the CLI for any database
python database/ttd/preprocess.py --help

# Run preprocessing in-place (writes parquets next to the raw inputs)
python database/ttd/preprocess.py  --input-dir database/ttd
python database/ctd/preprocess.py  --input-dir /path/to/ctd_raw_csvs
python database/hcdt/preprocess.py --input-dir /path/to/hcdt_raw_files

# Or write outputs to a separate directory
python database/ttd/preprocess.py  --input-dir database/ttd --output-dir /tmp/ttd_out
```

Raw inputs are obtained from each project's primary source (TTD: `db.idrblab.net/ttd`; CTD: `ctdbase.org`; HCDT: project-internal). The TTD raw downloads ship with the repo for convenience; CTD and HCDT raw files do not.

#### Notebook form (interactive)

```bash
jupyter nbconvert --to notebook --execute database/ttd/preprocess.ipynb
jupyter nbconvert --to notebook --execute database/ctd/preprocess.ipynb
jupyter nbconvert --to notebook --execute database/hcdt/preprocess.ipynb
```

#### Sanity-check the scripts

```bash
pytest database/ttd/test_preprocess.py
pytest database/hcdt/test_preprocess.py
pytest database/drugcentral/test_preprocess.py
```

The test suites verify CLI surface and that shipped parquet snapshots are readable. End-to-end pipeline tests run automatically when raw inputs are present and skip cleanly otherwise.

#### Embeddings

```bash
# Ingest embeddings into a fresh Qdrant instance
# (start Qdrant first, then run the notebook)
jupyter nbconvert --to notebook --execute qdrant_ingest.ipynb
```

The `concept_values_by_db_and_field.pkl` is regenerated as part of the preprocess notebooks.

---

## 5. Full setup from scratch

This section expands §1 with explanations. Skip it if the express path worked.

### 5.1 Clone and extract data

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp
unzip ~/Downloads/biochirp_data_bundle.zip -d .
```

### 5.2 Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Required at minimum: `OPENAI_API_KEY`. All other keys are optional — the system will skip the corresponding route. Do **not** change model names unless you are intentionally running a variant experiment; paper numbers assume the defaults.

### 5.3 Create the shared Docker network

BioChirp compose declares `semantic_net` as external. Create it once:

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

If another project uses the same subnet, pick a free `/16` and update both the `docker network create` command and the `networks.semantic_net.ipam.config.subnet` field in `docker-compose.yml`.

### 5.4 Start Qdrant

Qdrant is not part of the compose file (so it can be reused across projects). Start it attached to `semantic_net`:

```bash
docker rm -f bioc_qdrant >/dev/null 2>&1 || true
docker run -d \
  --name bioc_qdrant \
  --network semantic_net \
  -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant:latest

curl -fsS http://localhost:6333/readyz && echo "Qdrant ready"
```

**Important**: the container name **must** be `bioc_qdrant` — BioChirp services resolve Qdrant by that DNS name on `semantic_net`.

### 5.5 Start BioChirp services

```bash
docker compose up --build -d
```

Watch progress:

```bash
docker compose logs -f --tail=100
```

### 5.6 Verify health

```bash
docker compose ps
```

All containers should report `(healthy)` within a few minutes of start. If any stay `(unhealthy)`, see [§8.1](#81-containers-stuck-unhealthy).

---

## 6. Verifying reproduction

Health checks only confirm that processes are alive. To confirm the **pipeline** is correct, run the canonical test query below and compare against the expected signature.

### 6.1 Canonical query (TTD)

Over WebSocket `ws://localhost:8028/ttd_chat/`:

```json
{"user_input": "What drugs are used to treat rickets?"}
```

Expected event stream (abridged):

```
tool_called   → interpreter_tool
tool_result   → {"route": "ttd", ...}
tool_called   → expand_and_match_db_tool
tool_result   → {"disease": ["rickets", ...]}
tool_called   → planner_tool
tool_result   → {"tables": ["Drug_disease", "TTD_drug_download"], "joins": [...]}
tool_called   → ttd_tool
tool_result   → {"preview": [...], "row_count": <N>, "download_url": "/download/..."}
delta         → "Approved and clinical drugs for rickets include ..."
final
```

A correct run produces a non-zero `row_count` and a non-empty `preview`. Full tables are written to `results/` and downloadable via the chat service `/download` endpoint.

### 6.2 Smoke-test harness

For automated verification, the repository includes a minimal Python harness that exercises each chat endpoint with a canonical query and asserts a non-empty structured table:

```bash
python evaluation/MCQ/biochirp_agent.py --smoke
```

(This file is the same agent used for the paper's MCQ evaluation, with a `--smoke` flag for single-question dry runs.)

### 6.3 What should match exactly vs. what may vary

| Artifact | Expected behavior |
|---|---|
| Structured table **row IDs** | Deterministic under fixed `.env`, fixed bundle, fixed git commit |
| Row **order** | May differ unless a downstream sort is applied |
| LLM summary **text** | Not deterministic even with temperature=0 at provider side; the **evidence table** is what should be compared |
| Planner **join pairs** | Deterministic |
| Latency | Varies (hardware, network to OpenAI) |

For manuscript-grade comparison, archive the raw table outputs, not the summary text. See [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) §G.

### 6.4 Continuous integration (forthcoming)

A GitHub Actions workflow that automatically verifies run-to-run determinism on every push is in development as part of the Nature-portfolio submission preparation (Task T1.2). Once landed, every pushed commit will produce a reproducibility badge tied to the canonical query above. Until then, run the canonical query manually after any code change that touches the planner, executor, or entity-resolution path.

---

## 7. Reproducing paper evaluations

All evaluation notebooks live under `evaluation/`. Each folder corresponds to one figure/table family in the paper.

| Folder | Paper component | Entry points |
|---|---|---|
| `evaluation/Agentic_SQL/` | NL→SQL accuracy, latency | `nl2SQL.ipynb`, `final_plot_ai.ipynb`, `latency.R`, `plot.R` |
| `evaluation/MCQ/` | Multiple-choice benchmark | `MCQ_evaluator.ipynb`, `biochirp_agent.py`, `performance.R`, `accuracy_vs_latency.R` |
| `evaluation/MCP/` | MCP comparison (agentic + chat endpoint) | `mcp_agentic.ipynb`, `mcp_chat_end_point.ipynb`, `plot_mcp.R` |
| `evaluation/OpenTarget/` | Cross-model OpenTargets Q&A | `biochirp_{openai,gemini,grok,llama}_questions_response.ipynb`, `plot.ipynb` |
| `evaluation/same_question_robustness/` | Run-to-run and model-to-model agreement (Jaccard heatmaps) | `plot_retrival_count.ipynb`, `biochirp_model_latency_vs_retrival.R`, `opentargets_grounding.py` |
| `evaluation/semantic_member_selection/` | Semantic matching ablation | notebooks within the folder |

### 7.1 Running an evaluation notebook

Evaluation notebooks call the **running** BioChirp services over their HTTP/WebSocket ports. Before running any notebook:

1. Make sure `docker compose ps` shows everything healthy.
2. Activate a Python environment with the packages used by the notebooks (Jupyter, pandas, requests, websockets, matplotlib/seaborn). A requirements file is not pinned separately — the paper used the same `requirements.txt` used by the root `dockerfile`.
3. Launch Jupyter from the repo root so relative paths resolve:

```bash
jupyter lab
```

### 7.2 Regenerating figures

Figures are generated by the R scripts and the `plot*.ipynb` notebooks in each folder. For example, the Jaccard heatmap in the robustness section is produced by:

```bash
cd evaluation/same_question_robustness
# after running the data-collection notebooks once:
Rscript biochirp_model_latency_vs_retrival.R
```

R scripts expect the `.xlsx`/`.pkl` result files produced by the corresponding data-collection notebooks to already exist in the folder.

### 7.3 Evaluation input data

Question lists and raw responses used in the paper are checked in under each `evaluation/*` subfolder (`*.xlsx`, `*.json`, `*.pkl`). This means you can regenerate figures **without** re-running the LLM calls. To fully re-collect responses, run the `*_question_generator.ipynb` and `*_questions_response.ipynb` notebooks in order; expect ≥ 2 h of API usage per model sweep.

---

## 8. Troubleshooting

### 8.1 Containers stuck `(unhealthy)`

**Symptom**: `docker compose ps` shows services as `(unhealthy)` even though `curl http://localhost:<port>/health` from the host returns `200 OK`.

**Cause**: older image tags had a `curl`-based healthcheck, but the service images don't ship `curl`, so the in-container check fails with `curl: not found`.

**Fix**: the current `docker-compose.yml` uses a Python-based healthcheck (`python -c "import urllib.request; urllib.request.urlopen(...)"`), which is compatible with every BioChirp image. If you pull an older tag, either rebuild (`docker compose up -d --force-recreate`) or apply the fix manually by replacing healthcheck `test:` lines with the Python form.

### 8.2 `network semantic_net not found`

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

If the subnet is taken on your host, pick a free one and update it in both the command and `docker-compose.yml` (`networks.semantic_net.ipam.config.subnet`).

### 8.3 GPU reservation error on CPU-only host

**Symptom**: `could not select device driver "" with capabilities: [[gpu]]`.

**Fix**: in `docker-compose.yml`, remove or comment out the GPU reservation for the two affected services:

```yaml
# under biochirp_semantic_tool.deploy.resources.reservations:
#   devices:
#     - driver: nvidia
#       count: 1
#       capabilities: [gpu]

# same for opentargets
```

Semantic matching will fall back to CPU. Expect ~3–5× slowdown on embedding-heavy queries.

### 8.4 Qdrant connection errors from BioChirp services

Check all three conditions:

```bash
docker ps --filter name=bioc_qdrant                    # must be running, named bioc_qdrant
docker network inspect semantic_net | grep bioc_qdrant # must be attached
curl -fsS http://localhost:6333/readyz                 # must return OK
```

The container **name** matters — services resolve Qdrant by DNS name `bioc_qdrant` on `semantic_net`.

### 8.5 Port conflicts

Every host port used by BioChirp is listed in [§2.1](#21-what-runs-where). If one is already in use on your host, remap it in `docker-compose.yml` (`ports: - "HOSTPORT:CONTAINERPORT"`) and update any notebook/frontend references that assume the default.

### 8.6 First-time build hangs or OOM-kills

Two services pull large ML deps at build time (`biochirp_semantic_tool`, `biochirp_opentargets`). If the build OOMs:

```bash
docker compose build --memory=8g biochirp_semantic_tool
docker compose build --memory=8g opentargets
docker compose up -d
```

### 8.7 Frontend shows production URLs

`frontend/index.html` is wired for the production domain (`biochirp.iiitd.edu.in`) and a reverse proxy. For local reviewer testing, always use the service-specific pages (`*_chat_api.html`, `opentarget_api.html`) — they talk directly to `ws://localhost:80{28,31,29,26}`.

### 8.8 Healthy but wrong answers / empty tables

If services are healthy but queries return empty tables:

1. Confirm Qdrant collections are populated:
   `curl -s http://localhost:6333/collections`
2. Confirm the concept dictionary loaded:
   `docker compose logs biochirp_fuzzy_tool | grep -i 'loaded'`
3. Confirm the parquet files are mounted correctly:
   `docker compose exec biochirp_ttd_tool ls /app/database/ttd`

If (1) is empty, your `qdrant_storage/` is missing collections — re-extract the bundle, or run `qdrant_ingest.ipynb` to rebuild.

---

## 9. Repository map

```
biochirp/
├── README.md                         # this file
├── docker-compose.yml                # full service graph
├── dockerfile                        # root orchestrator image (optional, port 8010)
├── .env.example                      # env template — copy to .env
├── LICENSE                           # MIT
│
├── app/
│   ├── services/                     # shared business logic (synonyms, semantic, ...)
│   ├── tools/                        # one folder per microservice
│   │   ├── planner/                  #   graph-based query planner (§3.3 METHODS.md)
│   │   ├── expand_and_match_db/      #   orchestrates entity resolution
│   │   ├── expand_synonyms/          #   schema-aware synonym expansion
│   │   ├── expand_synonyms_unrestricted/
│   │   ├── fuzzy/                    #   rapidfuzz matching against concept dict
│   │   ├── semantic_filter/          #   Qdrant dense retrieval
│   │   ├── llm_member_filter/        #   LLM disambiguation
│   │   ├── interpreter_agent/        #   NL → structured request
│   │   ├── ttd/ ctd/ hcdt/           #   DB execution tools
│   │   ├── web/ tavily/ readme/      #   enrichment tools
│   │   └── bioc_embedding/           #   embedding helpers
│   └── utils/
│       └── dataframe_filtering.py    #   strict-join execution engine
│
├── app/per_db_chat/                           # shared WebSocket chat factory (replaces 25 *_chat_service/ clones)
├── app/per_db_tool/                           # shared per-DB tool FastAPI factory
├── app/chat/<slug>/                           # per-DB chat extras (requirements + <slug>_tool.py) — selected by DB_SLUG
├── app/tools/<slug>/                          # per-DB data tools (25 of them, including drugcentral)
├── opentarget_service/                        # OpenTargets GraphQL pipeline (federation backend; chat front-door removed)
├── bio_chat_service/                          # multi-DB chat aggregator (port 8030, /bio_chat/)
│
├── config/
│   ├── schema.py                     # canonical schema (master/association tables, PK/FK)
│   ├── settings.py                   # runtime config, model names, thresholds
│   └── guardrail.py                  # request guardrails
│
├── database/
│   ├── ttd/   *.parquet + preprocess.ipynb   # from bundle; preprocess rebuilds
│   ├── ctd/   *.parquet + preprocess.ipynb
│   ├── hcdt/  *.parquet + preprocess.ipynb
│   └── drugcentral/ *.parquet + preprocess.py + SOURCE.md  # v11012023 dump; CC BY-SA 4.0
│
├── resources/
│   ├── prompts/                      # versioned LLM prompts
│   ├── values/concept_values_by_db_and_field.pkl   # from bundle
│   ├── embeddings/                   # populated by qdrant_ingest.ipynb (optional)
│   └── diagrams/                     # architecture figures
│
├── qdrant_storage/                   # from bundle, mounted into bioc_qdrant container
├── qdrant.ipynb  qdrant_ingest.ipynb # embedding ingest (only if not using bundle)
│
├── frontend/                         # static HTML clients (production index + per-service pages)
│
├── evaluation/                       # see §7
│   ├── Agentic_SQL/
│   ├── MCQ/
│   ├── MCP/
│   ├── OpenTarget/
│   ├── same_question_robustness/
│   └── semantic_member_selection/
│
├── results/                          # runtime outputs (CSV tables per query)
├── docs/
│   ├── METHODS.md                    # implementation detail
│   └── REPRODUCIBILITY.md            # manuscript reporting checklist
└── Supplementary Tables.xlsx         # paper supplement
```

---

## 10. Citation, license, contact

### Citation

If you use BioChirp in academic work, please cite:

```bibtex
@article{biochirp,
  title   = {BioChirp: Database-first biomedical question answering with deterministic retrieval},
  author  = {... (please update with final author list)},
  journal = {Nature Computational Science},
  year    = {2026},
  note    = {Manuscript under review. DOI: <to be assigned on acceptance>. Code: https://github.com/abhi1238/biochirp}
}
```

### License

MIT — see [LICENSE](LICENSE).

### Security & privacy

- Do not commit real API keys. `.env` is git-ignored; `.env.example` is the only template.
- Runtime logs and `results/` outputs may contain user queries and tool outputs; both paths are git-ignored. Review before sharing externally.

### Contact

- **Demo**: https://biochirp.iiitd.edu.in
- **Issues / bug reports**: https://github.com/abhi1238/biochirp/issues
- **Corresponding author**: *(please update with email before release)*
