# BioChirp deployment — three credentialed steps

Everything that doesn't need credentials has already been built and verified. Three steps remain — each is a small one-liner once you have the relevant credential.

| # | Action | Credential needed | Time |
|---|---|---|---|
| 1 | Publish to PyPI | PyPI account + API token | 5 min |
| 2 | Mint a Zenodo DOI | Zenodo account (free; ORCID sign-in) | 10 min |
| 3 | Reverse-proxy HTTP/SSE behind biochirp.iiitd.edu.in | server SSH/sudo | 15 min |

Pre-built artefacts you already have on disk:

| Artefact | Path | Size |
|---|---|---|
| Wheel | `pkg/dist/biochirp_mcp-0.1.0-py3-none-any.whl` | 38 KB |
| Source dist | `pkg/dist/biochirp_mcp-0.1.0.tar.gz` | 30 KB |
| DXT bundle | `mcp_server/dxt/biochirp-0.1.0.dxt` | 36 KB |
| Zenodo deposit (12 files) | `dist_zenodo/` | 12 MB |
| Zenodo metadata | `.zenodo.json` + `CITATION.cff` | — |
| nginx config | `deploy/nginx-mcp.conf` | — |
| systemd unit | `deploy/biochirp-mcp.service` | — |

---

## Step 1 — publish `biochirp-mcp` to PyPI

### Pre-flight (already done by the build pipeline, but re-run if changed)

```bash
cd /home/abhishekh/abhi/biochirp/pkg
python -m build # produces dist/biochirp_mcp-0.1.0-{py3-none-any.whl, tar.gz}
twine check dist/* # validates the metadata
```

### Get a PyPI token (one time, ~3 min)

1. Sign in / register at https://pypi.org/account/register/
2. Settings → API tokens → "Add API token" with scope **"Project: biochirp-mcp"**
 *(if first upload, choose scope "Entire account" temporarily, then narrow after)*
3. Copy the token (starts with `pypi-`)

### Test on TestPyPI first (recommended)

```bash
twine upload --repository testpypi pkg/dist/* \
 --username __token__ \
 --password pypi-<your-test-token>

# Verify install from TestPyPI:
pip install --index-url https://test.pypi.org/simple/ \
 --extra-index-url https://pypi.org/simple/ \
 biochirp-mcp
biochirp-mcp-config | head
```

### Publish to real PyPI

```bash
twine upload pkg/dist/* \
 --username __token__ \
 --password pypi-<your-real-token>
```

After this, anyone in the world can:
```bash
pip install biochirp-mcp
biochirp-mcp # stdio MCP server
biochirp-mcp-http --port 8765 # HTTP/SSE remote server
biochirp-db-mcp --db biogrid # single-DB MCP
```

---

## Step 2 — mint a Zenodo DOI for citation

### Option A: GitHub-Zenodo integration (recommended, persistent)

1. Sign in at https://zenodo.org/ with your ORCID
2. Go to https://zenodo.org/account/settings/github/ → toggle ON the BioChirp repository
3. On GitHub, create a release: `gh release create v0.1.0 --notes "Initial public release"`
4. Zenodo auto-mints a DOI within minutes; the `.zenodo.json` already in the repo populates the metadata
5. Add the DOI badge to the README (Zenodo gives the markdown snippet)

### Bundling the parquet `database/` tree (optional, large artifact)

The default Zenodo deposit above is **metadata + code only** (~12 MB). To
additionally publish the preprocessed 26-database parquet tree, run:

```bash
scripts/package_data_for_release.sh
# → dist_zenodo/biochirp_data_YYYYMMDD.tar.gz
```

The script auto-excludes directories containing a `LICENSE_RESTRICTED` marker
(see [REDISTRIBUTION_RESTRICTED.md](REDISTRIBUTION_RESTRICTED.md)). End users
who need those sources must obtain their own license; manifests in each
directory document version, schema, and citation so they can fetch and rebuild.

### Option B: manual upload (if not using GitHub yet)

1. https://zenodo.org/uploads/new
2. Upload **everything** in `dist_zenodo/`:
 - `biochirp_mcp-0.1.0-py3-none-any.whl`
 - `biochirp_mcp-0.1.0.tar.gz`
 - `biochirp-0.1.0.dxt`
 - `biochirp_kg_v1.tsv` *(75 K-edge KG)*
 - `biochirp_kg_v1_with_contradictions.tsv` *(epistemic-uncertainty extension)*
 - `contradiction_report.json`
 - `BioRetrieve_v2_hard_tasks.json` *(15-task benchmark)*
 - `biochirp_paper_metrics_v6.json` *(reproducibility-κ data)*
 - `budget_competitive_ratio.json` *(MCKP greedy vs DP optimum, 90 instances)*
 - `methods.md` *(formal methods write-up)*
 - `CITATION.cff` + `.zenodo.json`
3. Zenodo will pre-fill from `.zenodo.json`. Click **Publish**.

### Result

- A DOI like `10.5281/zenodo.NNNNNNNN`
- A versioned record (every future GitHub release auto-mints a child DOI)
- Cite as: *"Gupta A. (2026). BioChirp v0.1.0 [Software]. Zenodo. https://doi.org/10.5281/zenodo.NNNNNNNN"*

This DOI goes into the manuscript's *Code & Data Availability* section verbatim.

---

## Public-tier vs private-tier deployment

The repo supports two deployment modes; choose based on your audience.

| Mode | Command | What runs | Use when |
|---|---|---|---|
| **Public tier** (default) | `docker compose up` | All chat services | Hosting at a public URL for anonymous users |

Public-tier additional env vars:

```bash
# Activates OmniPath license filter (drops rows whose `sources` include
# academic-only upstreams like KEA, ProtMapper, Phospho.ELM, HPMR, …).
BIOCHIRP_PUBLIC_TIER=1
```

Public-tier deployment checklist:

- [ ] [`TERMS_OF_SERVICE.md`](TERMS_OF_SERVICE.md) is reachable at a stable URL on the public host (link from the web UI and from API error pages).
- [ ] [`ATTRIBUTIONS.md`](ATTRIBUTIONS.md) is reachable at a stable URL.
- [ ] `BIOCHIRP_PUBLIC_TIER=1` is in the public environment (e.g. `.env.public` or the systemd unit's `Environment=` line).
- [ ] (Optional) Reverse-proxy adds a `Link:` HTTP header pointing at `/terms` and `/attributions` so machine consumers see the license without parsing the body.

---

## Step 3 — reverse-proxy HTTP/SSE behind biochirp.iiitd.edu.in/mcp/sse

### Server-side (one shell on biochirp.iiitd.edu.in)

```bash
# 1) install the package + http extras
sudo useradd -r -m -d /opt/biochirp biochirp || true
sudo -u biochirp python3 -m venv /opt/biochirp/venv
sudo -u biochirp /opt/biochirp/venv/bin/pip install 'biochirp-mcp[http]'

# 2) drop the systemd unit
sudo cp deploy/biochirp-mcp.service /etc/systemd/system/biochirp-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now biochirp-mcp.service
sudo systemctl status biochirp-mcp # should be "active (running)"
curl -s http://127.0.0.1:8765/health # should print {"status":"ok",...}

# 3) drop the nginx snippet
sudo cp deploy/nginx-mcp.conf /etc/nginx/snippets/biochirp-mcp.conf

# 4) edit the existing site-block to include it inside the TLS server { } block:
# include /etc/nginx/snippets/biochirp-mcp.conf;
sudo nginx -t # validate
sudo systemctl reload nginx

# 5) verify externally
curl -s https://biochirp.iiitd.edu.in/mcp/health
# {"status":"ok","service":"biochirp-mcp","transport":"sse"}
curl -s -I https://biochirp.iiitd.edu.in/mcp/sse | head
# HTTP/2 200
# content-type: text/event-stream; charset=utf-8
# x-accel-buffering: no
```

### End-user side (any Claude Desktop user, anywhere in the world)

Add to `claude_desktop_config.json`:

```json
{
 "mcpServers": {
 "biochirp": {
 "type": "sse",
 "url": "https://biochirp.iiitd.edu.in/mcp/sse"
 }
 }
}
```

Restart Claude Desktop. **Zero install on the user's machine.**

---

## Rebuild `biochirp_semantic_tool` after Qdrant batched-search changes (2026-05-12)

Two source-tree changes need to be baked into the `biochirp_semantic_tool` image — they are running live in the current container (via `docker cp` + in-container `pip install`) but will be lost on the next clean rebuild unless you re-run the build.

| Source change | File |
|---|---|
| Batched multi-term Qdrant search (`search_reference_terms_BATCH`) — N round-trips per field → 1 batched call | [app/tools/semantic_filter/app/filter.py](app/tools/semantic_filter/app/filter.py) |
| `pyarrow==24.0.0` added (needed by `pandas.read_parquet` for ingest scripts) | [app/tools/semantic_filter/requirements.txt](app/tools/semantic_filter/requirements.txt) |

Rebuild + restart the service:

```bash
cd /home/abhishekh/abhi/biochirp
docker compose build --no-cache biochirp_semantic_tool
docker compose up -d --force-recreate biochirp_semantic_tool

# Verify
docker exec biochirp_semantic_tool python3 -c "import pyarrow; print('pyarrow', pyarrow.__version__)"
docker exec biochirp_semantic_tool grep -c "search_reference_terms_BATCH" /app/app/filter.py # should print 1+
curl -s http://localhost:8015/health # {"status":"OK","device":"cuda","service":"semantic"}
```

Smoke-test the batched path end-to-end:

```bash
curl -s -m 10 -X POST "http://localhost:8015/semantic?database=civic" \
 -H "Content-Type: application/json" \
 -d '{"clinical_significance":["pathogenic","likely_pathogenic"]}'
docker logs --since 30s biochirp_semantic_tool 2>&1 | grep "QDRANT BATCH"
# Expect: "[QDRANT BATCH] Searching 2 terms in civic.clinical_significance" — one call, not two
```

Related prompt-cache change in [app/tools/interpreter_agent/app/interpreter.py](app/tools/interpreter_agent/app/interpreter.py) (memoized `_build_database_context` + reordered `effective_prompt = nlu_prompt + db_context`) is on a bind-mounted file and needs only a service restart, not a rebuild:

```bash
docker restart biochirp_interpreter_tool
```

---

## After all three steps

You can write in the manuscript:

> *"BioChirp is publicly available at three access tiers, each with no authentication required: as a PyPI package (`pip install biochirp-mcp`), as a one-click Claude Desktop extension (`biochirp-0.1.0.dxt`), and as a hosted Model Context Protocol endpoint (`https://biochirp.iiitd.edu.in/mcp/sse`). All artefacts — including the 75 K-edge knowledge graph, the BioRetrieve-v2 benchmark with ground truth, the MCKP competitive-ratio data, and the formal methods document — are deposited at Zenodo (DOI: 10.5281/zenodo.NNNNNNNN)."*

That single paragraph in the *Code Availability* section is what reviewers want to see, and it covers every standard publication mandate (open code, open data, hosted demo, reproducible reference deployment).

---

## What still requires your action (cannot be done autonomously)

| Action | Why I can't do it |
|---|---|
| `twine upload` | Needs your PyPI API token |
| Zenodo deposit | Needs your Zenodo / ORCID login |
| nginx + systemd install | Needs root SSH on biochirp.iiitd.edu.in |

Each is **one command** once you're logged in. The configs and bundles are already in the repo, ready to ship.
