# Reproducibility Checklist for BioChirp

Use this checklist when preparing internal reports, public releases, or manuscript supplements.

## A. Software Snapshot

Record:

- Git commit hash of repository
- `docker-compose.yml` version and any local modifications
- Image digests (or build tags) for each service
- Python package versions if running outside containers

Recommended commands:

```bash
git rev-parse HEAD
docker compose config > compose.resolved.yml
docker images --digests | grep -E 'biochirp|opentarget|redis|qdrant'
```

## B. Environment Configuration

Archive non-secret config:

- full `.env` key list
- model names used per component
- timeout and planner settings
- fuzzy/semantic thresholds

Do **not** store secrets in shared artifacts.

## C. Hardware/Runtime

Report at minimum:

- CPU model and core count
- RAM
- GPU model (if used)
- OS and kernel
- Docker and Compose versions

## D. Data Provenance

For each data source used in evaluation:

- source name (`TTD`, `CTD`, `HCDT`, `OpenTargets`)
- release/version/date
- download date
- file-level checksum for critical artifacts

Critical local artifacts:

- `database/ttd/*`
- `database/ctd/*`
- `database/hcdt/*`
- `resources/values/concept_values_by_db_and_field.pkl`
- `resources/embeddings/biochirp_embeddings.pkl`

Checksum example:

```bash
sha256sum resources/values/concept_values_by_db_and_field.pkl
sha256sum resources/embeddings/biochirp_embeddings.pkl
```

## E. Qdrant and Embedding Reproducibility

Record:

- notebook/script used for ingest
- collection naming/version convention
- embedding model list (`config/settings.py`)
- Qdrant version and index settings

## F. Planner Reproducibility

Record planner variables:

- `USE_GREEDY_ALGORITHM`
- `MAX_COMBINATIONS`
- `MAX_TABLES_IN_COVERAGE`
- `STEINER_TIMEOUT_SECONDS`

Also archive planner outputs (tables order, join pairs) for representative queries.

## G. Benchmark Protocol Reporting

For each benchmark:

- exact query list
- number of repeated runs per question
- model/source route used
- scoring method (directional coverage, Jaccard, Dice, etc.)
- handling of empty outputs/timeouts

Store raw outputs before aggregation:

- per-run table rows
- resolved IDs
- timing/latency logs

## H. Failure and Retry Accounting

Track and report:

- API failures (HTTP errors)
- JSON parse failures
- timeout counts
- retry counts and split-batch fallbacks

This is necessary to interpret retrieval completeness and agreement metrics.

## I. Minimal End-to-End Validation Script

After deployment, verify all primary services are healthy:

```bash
for p in 8010 8026 8028 8031 8029 8011 8013 8014 8015 8016 8017 8018; do
  echo "--- $p ---"
  curl -s "http://localhost:$p/health" || true
  echo
 done
```

## J. Manuscript Supplement Package (Recommended)

Include:

1. `README` with exact reproduction steps
2. question lists and random seeds (if any)
3. raw run outputs (`.pkl/.json`) used to create figures
4. scripts/notebooks that generate all paper figures
5. environment and data snapshot metadata

This package should allow independent reruns with no hidden steps.
