# BioChirp

**Database-first biomedical question answering with deterministic retrieval and controlled summarization.**

BioChirp answers natural-language biomedical questions by routing them to one or more of 11 curated biomedical databases through a shared schema-grounded planning pipeline (entity resolution → schema mapping → query planning → parquet/GraphQL execution → LLM summarization). It is distributed as a set of FastAPI microservices orchestrated with Docker Compose, plus a Model Context Protocol (MCP) server for direct use from Claude and other MCP clients.

- **Live demo**: https://biochirp.iiitd.edu.in
- **Code**: https://github.com/abhi1238/biochirp
- **Issues**: https://github.com/abhi1238/biochirp/issues

---

## Table of contents

1. [The 11 databases](#1-the-11-databases)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Prerequisites](#3-prerequisites)
4. [Data you need to supply](#4-data-you-need-to-supply)
5. [Bring-up](#5-bring-up)
6. [Verifying it works](#6-verifying-it-works)
7. [Using the MCP server](#7-using-the-mcp-server)
8. [Troubleshooting](#8-troubleshooting)
9. [Repository map](#9-repository-map)
10. [Citation, license, contact](#10-citation-license-contact)

---

## 1. The 11 databases

| Database | What it covers |
|---|---|
| TTD | Drugs, targets, indications, mechanism of action, approval status |
| CTD | Chemical–gene/disease and gene–disease associations |
| HCDT | Anti-cancer drug–target associations, drug–pathway links |
| HPO | Phenotypes, gene–phenotype, disease–phenotype associations |
| ClinVar | Variant pathogenicity, gene–variant associations |
| Reactome | Biological pathways, gene–pathway memberships |
| MSigDB | Gene sets (Hallmark, KEGG, GO, Reactome, …) |
| Orphanet | Rare diseases, disease–gene associations |
| STRING | Protein–protein interaction networks |
| UniProt | Protein function, gene→protein mapping, localisation |
| Open Targets | Live drug–target–disease evidence via GraphQL (the one DB with no local parquet snapshot — everything else is a curated offline snapshot) |

Plus a live web-search fallback for questions outside all 11. This list is authoritative — it's generated from [`mcp_server/web/manifest.json`](mcp_server/web/manifest.json), which every MCP client reads directly.

---

## 2. Architecture at a glance

Ten of the databases share one code path: a thin `app/tools/<db>/` service wraps the common [`app/per_db_tool/`](app/per_db_tool/) library, which handles entity resolution, schema-aware planning, query execution, and summarization identically across all ten. Open Targets is the exception — it queries a live GraphQL API instead of a local parquet snapshot, so it's its own standalone service, [`opentarget_service/`](opentarget_service/).

```
                              user question
                                    │
                                    ▼
                              biochirp_<db>_tool  (one per DB, ports 8012–8089)
                              drives its own query end-to-end in-process:
                                    │
                   ┌────────────────┴────────────────┐
                   ▼                                  ▼
   biochirp_expand_and_match_db_tool (8009)   biochirp_schema_mapper_tool (8019)
   entity resolution — fans out to:           ANN retrieval over evaluation/schema_kg/
     synonyms_expander        (8014)          inputs/<db>/schema.json (shared across DBs)
     fuzzy_tool               (8013)                    │
     semantic_tool  (8015, GPU-preferred)                ▼
                                             biochirp_schema_planner_tool (8020)
                                             deterministic join/plan assembly
                                                          │
                                                          ▼
                              executes the plan against database/<db>/*.parquet
                              (Open Targets: opentargets service, port 8026,
                               queries the live Open Targets GraphQL API instead)
                                                          │
                                                          ▼
                                             LLM summarization → answer
```

`biochirp_orchestrator_tool` (8021) exists as a centralized-routing alternative to this in-process path, but it's an opt-in cutover gated by the `SCHEMA_KG_ORCHESTRATOR_DBS` env var (`app/per_db_tool/schema_kg_worker.py`) — unset by default in `.env.example` and `docker-compose.yml`, so every DB uses the in-process path above unless you explicitly enable it per DB.

Supporting infrastructure: `biochirp_litellm` (4000, model-routing proxy), `bioc_qdrant` (6333/6334, vector store for `semantic_tool`), `biochirp_redis_tool` (query-state cache), `biochirp_readme_tool` / `biochirp_tavily_tool` / `biochirp_share_tool` (enrichment/citation helpers). Every full port/service list is in [`docker-compose.yml`](docker-compose.yml) — it's generated by [`scripts/gen_compose.py`](scripts/gen_compose.py) from each `dbs/<db>/manifest.yaml`'s `service:` block, so it's always the source of truth if this diagram drifts.

For the module-level breakdown of `app/per_db_tool/` and the full request lifecycle, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 3. Prerequisites

### 3.1 Software

| Component | Notes |
|---|---|
| Docker Engine + Docker Compose v2 | `docker compose version` |
| `curl` | for health checks |

### 3.2 Hardware

`biochirp_semantic_tool` and the `opentargets` service both request an NVIDIA GPU reservation in `docker-compose.yml` (embedding-heavy workloads). On a CPU-only host, remove the `deploy.resources.reservations.devices` block for those two services before `docker compose up` — see [§8](#8-troubleshooting). Everything else runs CPU-only.

### 3.3 API keys

```bash
cp .env.example .env
$EDITOR .env
```

At minimum, set `OPENAI_API_KEY`. `.env.example` documents every other variable in place, including the optional per-DB `GROQ_API_KEY_<DB>` / `OPENROUTER_API_KEY_<DB>` isolation pattern (falls back to the shared `GROQ_API_KEY`/`OPENROUTER_API_KEY` if unset).

---

## 4. Data you need to supply

`database/<db>/*.parquet` (the 10 offline databases) and the derived caches under `resources/values/*.pkl` + `resources/db_column_embeddings.npz` (alias maps, concept dictionaries, column embeddings — built by the generator scripts under [`scripts/`](scripts/), e.g. `build_alias_map.py`, `build_concept_values.py`, `precompute_column_embeddings.py`) are **not included in this repository** — they're large, per-DB derived artifacts read at runtime via the `docker-compose.yml` volume mounts (`./database/<db>/:/app/database/<db>/:ro`, etc.).

There is currently no published, one-command way to obtain or rebuild this data from this repo alone — the preprocessing pipelines that turn each database's original public release into the parquet snapshot BioChirp reads live outside this repository. If you want to stand up your own instance, reach out via [Issues](https://github.com/abhi1238/biochirp/issues) or the contact in [§10](#10-citation-license-contact) for the data artifacts, or rebuild them yourself from each database's public source using the generator scripts in `scripts/` as a starting point for the derived caches.

`evaluation/schema_kg/inputs/` and `evaluation/schema_kg/src/` — the schema-planner's own required inputs — **are** included in this repo (they're small, KB-sized planner config, not the data snapshot).

---

## 5. Bring-up

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp

cp .env.example .env
$EDITOR .env                     # at minimum, set OPENAI_API_KEY

# Place the data described in §4 under database/<db>/ before continuing.

# semantic_net is declared `external: true` in docker-compose.yml, so it
# must exist before `docker compose up` — Compose will not create it.
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net || true

docker compose up --build -d
```

Qdrant (`bioc_qdrant`) and the LiteLLM proxy (`biochirp_litellm`) are both regular services in `docker-compose.yml` — no separate `docker run` step is needed for either.

First build pulls and builds ML-heavy images (`biochirp_semantic_tool`, `opentargets`) and can take a while depending on hardware and network speed. Subsequent `docker compose up` runs are fast.

```bash
docker compose ps          # everything should reach (healthy)
docker compose logs -f --tail=100
```

---

## 6. Verifying it works

```bash
curl -fsS http://localhost:6333/readyz && echo "Qdrant OK"
curl -fsS http://localhost:8021/health && echo "orchestrator OK"
curl -fsS http://localhost:8012/health && echo "ttd tool OK"
curl -fsS http://localhost:8026/health && echo "opentargets OK"
```

For an interactive check, serve the frontend and open a per-DB console:

```bash
python3 -m http.server 8080 --directory frontend
```

Then open `http://localhost:8080/db_chat.html?db=ttd` (or any of the other 9 offline DB slugs, or `http://localhost:8080/opentarget_api.html` for Open Targets) and ask a real question, e.g. *"What drugs are used to treat rickets?"* for TTD. A correct run returns a non-empty result table plus a grounded summary.

---

## 7. Using the MCP server

BioChirp also runs as an MCP server exposing all 11 databases + web search as tools — this is what powers the Claude Desktop/Claude.ai connector at the live demo. See [`mcp_server/web/manifest.json`](mcp_server/web/manifest.json) for the exact tool list and [`CONNECTOR_FROM_WEB.md`](CONNECTOR_FROM_WEB.md) for connecting a client. The MCP server (`mcp_server/http_server.py`) runs independently of the `docker compose` stack above — see `mcp_server/` for details.

---

## 8. Troubleshooting

### 8.1 `network semantic_net not found`

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

If that subnet is already taken on your host, pick a free `/16` and update it in both the command and `docker-compose.yml`'s `networks.semantic_net.ipam.config.subnet`.

### 8.2 GPU reservation error on a CPU-only host

**Symptom**: `could not select device driver "" with capabilities: [[gpu]]`.

**Fix**: in `docker-compose.yml`, remove the `deploy.resources.reservations.devices` block (the one with `capabilities: [gpu]`) under both `biochirp_semantic_tool` and `opentargets`. Both fall back to CPU; expect slower embedding-heavy queries.

### 8.3 Containers healthy but queries return empty tables

1. Confirm the data described in [§4](#4-data-you-need-to-supply) is actually present under `database/<db>/`:
   `docker compose exec biochirp_ttd_tool ls /app/database/ttd`
2. Confirm Qdrant has collections loaded: `curl -s http://localhost:6333/collections`
3. Check the specific per-DB tool's logs: `docker compose logs biochirp_ttd_tool`

### 8.4 Port conflicts

Every host port BioChirp uses is declared in `docker-compose.yml`'s per-service `ports:` block. If one's already taken on your host, remap it there (`"HOSTPORT:CONTAINERPORT"`).

---

## 9. Repository map

```
biochirp/
├── README.md                     # this file
├── ARCHITECTURE.md                # module-level design + request lifecycle
├── docker-compose.yml             # full service graph — generated, see scripts/gen_compose.py
├── Dockerfile.base(-schemakg)     # shared base images
├── Dockerfile.service(-schemakg)  # shared per-service build template
├── .env.example                   # env template — copy to .env
├── LICENSE                        # MIT
│
├── app/
│   ├── per_db_tool/               # shared library every per-DB tool builds on
│   ├── tools/
│   │   ├── <ttd|ctd|hcdt|hpo|clinvar|reactome|msigdb|orphanet|string|uniprot>/
│   │   │                          # one thin wrapper per offline DB
│   │   ├── orchestrator/          # routes each query to schema_mapper/schema_planner
│   │   ├── schema_mapper/         # ANN retrieval over evaluation/schema_kg/inputs/
│   │   ├── schema_planner/        # deterministic join/plan assembly
│   │   ├── expand_and_match_db/   # entity-resolution fan-out
│   │   ├── expand_synonyms(_unrestricted)/, fuzzy/, semantic_filter/
│   │   └── planner/, readme/, share/, tavily/
│   ├── services/synonyms/         # drug/gene/disease synonym lookups
│   └── utils/                     # shared utilities (dataframe handling, LLM gateway, …)
│
├── opentarget_service/            # Open Targets — standalone, live GraphQL, not per_db_tool-based
├── mcp_server/                    # MCP server (http_server.py, server.py)
│
├── config/                        # schema.py (join schema, authoritative), settings.py, guardrail.py, …
├── dbs/<slug>/manifest.yaml       # per-DB authoring surface — see dbs/README.md
├── evaluation/schema_kg/          # schema-planner's required inputs (inputs/, src/) — see §4
│
├── resources/
│   ├── prompts/                   # versioned LLM prompts
│   └── db_column_descriptions.md, db_field_aliases.md, db_profiles/
│
├── frontend/                      # static HTML/JS clients — index.html routes to db_chat.html?db=<slug>
├── deploy/                        # nginx configs + SECURITY.md
├── scripts/                       # CI gates, cache generators, onboarding tooling
└── database/<db>/                 # NOT in git — parquet snapshots you supply, see §4
```

---

## 10. Citation, license, contact

### Citation

See [`CITATION.cff`](CITATION.cff) for the current citation metadata.

### License

MIT — see [LICENSE](LICENSE). See [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md) and [`LICENSING_REMOVAL.md`](LICENSING_REMOVAL.md) for third-party database licensing terms and databases removed for licensing reasons.

### Security & privacy

- Do not commit real API keys. `.env` is git-ignored; `.env.example` is the only template.
- Runtime logs and `results/` outputs may contain user queries and tool outputs; both paths are git-ignored. Review before sharing externally.

### Contact

- **Demo**: https://biochirp.iiitd.edu.in
- **Issues / bug reports**: https://github.com/abhi1238/biochirp/issues
