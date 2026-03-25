# BioChirp

BioChirp is a database-first biomedical QA system that prioritizes deterministic retrieval over free-form generation.
It combines curated biomedical databases (`TTD`, `CTD`, `HCDT`), schema-grounded query planning, entity resolution, and controlled LLM summarization.

- Live demo: https://biochirp.iiitd.edu.in
- Issues: https://github.com/abhi1238/biochirp/issues

## Reviewer Quick Path (Prepared Artifacts)

If you already have prepared parquet + Qdrant storage, this is the shortest reproducible run:

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp
cp .env.example .env
# fill API keys in .env
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net || true
docker run -d --name bioc_qdrant --network semantic_net -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant:latest
docker compose up --build -d
curl -fsS http://localhost:8028/health && echo "TTD chat ready"
curl -fsS http://localhost:8031/health && echo "CTD chat ready"
curl -fsS http://localhost:8029/health && echo "HCDT chat ready"
curl -fsS http://localhost:8026/health && echo "OpenTargets ready"
```

## Who This README Is For

This guide is for anyone who wants to:

1. clone the repository,
2. prepare required data artifacts,
3. run all services with Docker,
4. validate that the system is working reproducibly.

## Reproducible Local Setup (Step by Step)

## 0) Prerequisites

- Docker Engine 24+
- Docker Compose v2
- Linux/macOS shell
- `curl`
- Jupyter (only if you need first-time embedding ingest)

Recommended hardware:

- RAM: 64 GB+ (semantic and CTD paths are memory-heavy)
- CPU: 12+ cores
- GPU: optional (some services request GPU reservations; see Troubleshooting for CPU-only hosts)

## 1) Clone

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp
```

## 2) Configure environment

```bash
cp .env.example .env
```

Edit `.env` with real API keys and model names.

At minimum, set provider keys for the models you actually use (default template uses OpenAI models).

## 3) Prepare data artifacts

BioChirp requires these artifacts under repo root:

- `database/ttd/*.parquet`
- `database/ctd/*.parquet`
- `database/hcdt/*.parquet`
- `resources/values/concept_values_by_db_and_field.pkl`
- Qdrant data via either:
  - `qdrant_storage/` snapshot (recommended), or
  - `resources/embeddings/biochirp_embeddings.pkl` + ingest notebook

### Option A (recommended): use prepared data bundle

Download/extract your project bundle into repo root so paths above exist.

### Option B: preprocess databases from raw files

If you do not have prepared parquet snapshots, run preprocessing notebooks:

- `database/ttd/preprocess.ipynb`
- `database/ctd/preprocess.ipynb`
- `database/hcdt/preprocess.ipynb`

These should generate parquet files in `database/{ttd,ctd,hcdt}/`.

### Verify artifact presence

```bash
# concept dictionary
test -f resources/values/concept_values_by_db_and_field.pkl && echo "values pickle: OK"

# expected parquet counts (current repo snapshot)
echo "ttd parquet count: $(find database/ttd -maxdepth 1 -name '*.parquet' | wc -l)"   # expected: 10
echo "ctd parquet count: $(find database/ctd -maxdepth 1 -name '*.parquet' | wc -l)"   # expected: 9
echo "hcdt parquet count: $(find database/hcdt -maxdepth 1 -name '*.parquet' | wc -l)" # expected: 8

# qdrant source (at least one path should exist)
[ -d qdrant_storage ] && echo "qdrant_storage: OK" || echo "qdrant_storage missing"
[ -f resources/embeddings/biochirp_embeddings.pkl ] && echo "embeddings pickle: OK" || echo "embeddings pickle missing"
```

## 4) Create Docker network required by compose

`docker-compose.yml` uses an external network named `semantic_net`.

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net || true
```

## 5) Start Qdrant on `semantic_net`

```bash
docker run -d \
  --name bioc_qdrant \
  --network semantic_net \
  -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant:latest
```

Health check:

```bash
curl -fsS http://localhost:6333/readyz && echo "Qdrant: OK"
```

## 6) Ingest embeddings (first-time only)

Skip this step if your `qdrant_storage/` already contains loaded collections.

Use either notebook:

- `qdrant.ipynb`
- `qdrant_ingest.ipynb`

Set Qdrant URL to `http://localhost:6333` and run all cells.

## 7) Launch BioChirp services

```bash
docker compose up --build -d
```

## 8) Validate service health

### Chat-facing services

```bash
curl -fsS http://localhost:8028/health && echo "TTD chat: OK"
curl -fsS http://localhost:8031/health && echo "CTD chat: OK"
curl -fsS http://localhost:8029/health && echo "HCDT chat: OK"
curl -fsS http://localhost:8026/health && echo "OpenTargets: OK"
```

### Core internal services

```bash
curl -fsS http://localhost:8011/health && echo "Planner: OK"
curl -fsS http://localhost:8009/health && echo "Expand+Match: OK"
curl -fsS http://localhost:8015/health && echo "Semantic: OK"
```

## 9) Use WebSocket endpoints

- TTD chat: `ws://localhost:8028/ttd_chat`
- CTD chat: `ws://localhost:8031/ctd_chat`
- HCDT chat: `ws://localhost:8029/hcdt_chat`
- OpenTargets chat: `ws://localhost:8026/opentarget`

Payload format:

```json
{"user_input": "What drugs are used to treat rickets?"}
```

Optional keepalive:

```json
{"type": "ping"}
```

Common emitted events include `tool_called`, `tool_result`, `delta`, `*_table`, `final`, `error`.

## 10) Output artifacts

- Preview rows are streamed via WebSocket.
- Full result tables are written to `results/` and can be downloaded via each chat service `/download` endpoint.

## Current Runtime Topology

This compose setup exposes domain chat services directly (`ttd_chat`, `ctd_chat`, `hcdt_chat`, `opentarget`).
A separate top-level orchestrator service (`8010`) is present in codebase but commented out in the current `docker-compose.yml`.

## Planner Behavior (Code-Aligned)

Planner code: `app/tools/planner/app/graph.py`

Current behavior:

1. strict concept-to-table mapping (fails on missing/ambiguous concepts),
2. Steiner connection of terminal tables using NetworkX (Mehlhorn),
3. deterministic BFS extraction for parent-child join order,
4. schema-validated join key emission.

Execution engine (`app/utils/dataframe_filtering.py`) then applies filters, executes strict joins, checks join-explosion thresholds, projects requested columns, and deduplicates output.

## Reproducibility Checklist

Before reporting results, confirm:

- same git commit hash,
- same `.env` model configuration,
- same database parquet snapshot,
- same Qdrant contents,
- same Docker image rebuild state,
- same planner/runtime flags.

Note: output row *content* should match under fixed environment; row *order* may vary unless explicitly sorted downstream.

## Troubleshooting

### `network semantic_net not found`

```bash
docker network create --driver bridge --subnet 172.35.0.0/16 semantic_net
```

### Qdrant connection failures

- Ensure container name is `bioc_qdrant`.
- Ensure it is attached to `semantic_net`.
- Ensure `6333/6334` ports are reachable.

### CPU-only machine and GPU reservation errors

Some services request NVIDIA devices in `docker-compose.yml`.
For CPU-only hosts, remove `deploy.resources.reservations.devices` blocks for those services.

### Slow first startup

```bash
docker compose logs -f --tail=200
```

### Frontend points to production host

`frontend/*.html` may contain production host constants. For local testing, replace them with `localhost:<port>`.

## Security

- Do not commit real API keys.
- Keep `.env` local; use `.env.example` as template only.
- Review logs before sharing (queries and tool traces may contain sensitive text).

## License

MIT (`LICENSE`)

## Citation

If BioChirp is used in research, cite this repository and associated manuscript/preprint.
