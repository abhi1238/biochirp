# Licensing Removal Log

**Date:** 2026-05-14
**Action:** Permanent removal of DisGeNET, OncoKB, and COSMIC code paths from BioChirp.
**Database count at the time:** 28 → 26 → **25**. (Historical snapshot — later decommissionings brought the count down further; see [README.md](README.md) for the current 11-database list.)
**Status:** Removed from working tree. History scrubbed via `git filter-repo` for paths that had ever been tracked. Local backup retained as tag `pre-licensing-purge`, branch `backup/pre-licensing-purge`, and bundle `/tmp/biochirp-pre-licensing-purge.bundle`.

> **No public exposure occurred.** Verified after the initial pass: none of the
> DisGeNET/OncoKB code or data was ever pushed to `origin/main` — they lived
> only in the 25 unpushed local commits prior to this purge. COSMIC code and
> data likewise never reached the public repo. The history rewrite is
> therefore housekeeping, not security remediation; no licensor disclosure
> email is required. The `.gitignore` block and this log exist to prevent
> future re-introduction.

---

## Why these two databases were removed

Both DisGeNET and OncoKB shipped with BioChirp's earlier deployments
under the assumption that academic use covered redistribution via a
public web service. A licensing audit on 2026-05-14 concluded this
assumption is incorrect for both sources. A public‑facing BioChirp
instance cannot host, mirror, or proxy these databases without explicit
written permission and (in most cases) a paid commercial license.

### DisGeNET

- **License regime since 2023:** custom license administered by
  MedBioinformatics Solutions SL (disgenet.com). The previous CC
  BY‑NC‑SA 4.0 release line was discontinued.
- **What is allowed for free academic users:** personal, non‑commercial
  research use after registration with a per‑user API token.
- **What is prohibited under the free tier (relevant to BioChirp):**
  - Redistribution of the database (dumps, parquet, CSV, JSON).
  - Making the data available to any third party — including via a
    public web UI that returns DisGeNET results to anonymous users.
  - Sharing API tokens across users or proxying queries with a single
    shared key for unauthenticated callers.
- **Why "live query, no dump" did not save us:** the prohibition on
  "making the data available to third parties" applies regardless of
  whether the data is materialised on disk. A public proxy that returns
  DisGeNET responses to anonymous users is the third‑party access
  pattern they explicitly forbid.

### OncoKB

- **Owner / licensor:** Memorial Sloan Kettering Cancer Center (MSK).
- **License:** custom OncoKB Terms of Use.
- **Free academic tier:** non‑commercial research only; per‑user
  registration and a personal, non‑transferable API token are required.
- **Prohibited under the free tier (relevant to BioChirp):**
  - Providing access to OncoKB content to any third party.
  - Sharing or proxying API tokens.
  - Any commercial or clinical use without a paid commercial license.
- **Same conclusion as DisGeNET:** a public BioChirp instance that
  returns OncoKB content to anonymous users — even with no on‑disk
  dump — is not permitted under the free tier.

### COSMIC (Catalogue Of Somatic Mutations In Cancer)

- **Owner / licensor:** Wellcome Sanger Institute. Commercial licensing
  via QIAGEN.
- **License:** Sanger custom Terms of Use + CC BY‑NC‑SA 4.0 on certain
  components (Cancer Gene Census).
- **Free academic tier:** local research use only after registering for a
  free academic account at https://cancer.sanger.ac.uk/cosmic and
  accepting the COSMIC terms.
- **Prohibited under the free tier (relevant to BioChirp):**
  - Redistribution of COSMIC data files in any form (CSV, parquet, TSV,
    JSON dumps, etc.).
  - Making COSMIC content available to third parties via a hosted API
    or web UI — explicitly identified as "onward distribution".
  - Any commercial use without a paid Sanger/QIAGEN license.
- **Why removed even though COSMIC was already behind a `restricted`
  Docker profile:** the existing defense‑in‑depth (services tagged
  `profiles: ["restricted"]` so default `docker compose up` does not
  start them; parquets bind‑mounted from local disk rather than baked
  into the image) was a good design but relied on operator discipline.
  Removing the code paths entirely eliminates the failure mode where
  someone accidentally launches with `--profile restricted` on a
  public host or publishes a Docker image with COSMIC tooling.
- **Local data preserved (at the time):** the user's 24 GB of COSMIC parquets in
  `database/cosmic/` were retained on local disk for personal academic
  research (which the license permits), with the directory `.gitignore`'d
  so it couldn't be accidentally committed or pushed. `database/cosmic/`
  is no longer present on local disk as of this repository's current state.

### What would have made hosting safe

For completeness, the only scenarios that would have been licensing‑safe
for a public BioChirp deployment were:

1. Each end user supplies their **own** DisGeNET API key / OncoKB API
   token, BioChirp never holds a shared key, no server‑side caching of
   responses, **and** written permission from both licensors describing
   that exact architecture.
2. A paid commercial / redistribution license from each licensor.

BioChirp does neither, so the only correct action was full removal.

---

## What was removed

### Code and service directories (deleted from disk)

- `disgenet_service/`, `oncokb_service/`, `cosmic_service/` — entire chat orchestrator services.
- `app/tools/disgenet/`, `app/tools/oncokb/`, `app/tools/cosmic/` — entire data tools.
- `database/disgenet/`, `database/oncokb/` — data directories (incl. the
  historical `gene_master_table_oncokb.parquet` data dump that was
  previously tracked in git and triggered the history‑rewrite step).
- `database/cosmic/` — **not deleted from disk at the time** (24 GB of
  parquets were retained locally for the user's academic research use,
  `.gitignore`'d to prevent future commits or pushes); the directory is
  no longer present on local disk as of this repository's current state.
- `orchestrator_service/app/disgenet_tool.py`, `oncokb_tool.py`, `cosmic_tool.py`.
- `bio_chat_service/app/disgenet_tool.py`, `oncokb_tool.py`, `cosmic_tool.py`.

### Frontend (deleted)

- `frontend/disgenet_chat_api.html`, `oncokb_chat_api.html`, `cosmic_chat_api.html`.
- Matching `.legacy.bak` backups for all three.

### Deployment / serving (deleted)

- `docs/nginx_disgenet_snippet.conf`, `nginx_oncokb_snippet.conf`, `nginx_cosmic_snippet.conf`.
- Three pairs of `docker-compose.yml` service blocks
  (`biochirp_disgenet_tool/chat`, `biochirp_oncokb_tool/chat`,
  `biochirp_cosmic_tool/chat`). `VALID_DATABASES` env vars trimmed
  on every service that carried them (final list = 25 DBs).
- Backup compose files (`*.bak_*`) had matching blocks removed.

### Cached query results (deleted from disk; were already gitignored)

- ~200 `results/disgenet_results_*.csv` files.
- ~200 `results/oncokb_results_*.csv` files.
- ~250 `results/cosmic_results_*.csv` files.

These contained API responses, i.e. licensed data.

### Environment snapshots (deleted from disk; were untracked)

- `.env.bench_*`, `.env.full_card_bench_*`, `.env.pre_qwen_*` snapshots
  that contained DisGeNET / OncoKB API keys and per‑run config.

### Code references edited (146 files, ~all surface‑area trimmed)

Edited cleanly to remove both DBs while preserving file validity:

- Runtime configs: `config/schema.py`, `config/guardrail.py`,
  `config/attributions.py`, `.env.example`.
- Orchestrator: `orchestrator_service/app/main.py`, `pipeline.py`,
  `db_knowledge_graph.py`, `query_planner_tool.py`.
- `bio_chat_service/app/{main,pipeline,db_catalog,database_selector_tool,semantic_db_selector}.py`.
- MCP server: `mcp_server/{server,db_mcp,budget_planner}.py`,
  `mcp_server/web/manifest.json`, `mcp_server/dxt/manifest.json`
  (database counts 28→26, 29→27 mcp‑server counts).
- Prompts: `resources/prompts/db_notes.yaml`,
  `resources/prompts/interpreter_db_notes.yaml`,
  `resources/prompts/router.md`,
  `resources/prompts/biomedical_query_classifier.md`,
  `resources/prompts/interpreter_shared.md`,
  `resources/db_profiles/registry.md`.
- `app/tools/interpreter_agent/app/interpreter.py`,
  `app/tools/tavily/app/tavily.py`,
  `app/utils/dataframe_filtering.py`,
  `app/utils/summarizer_prompt_builder.py`.
- Scripts / generators: `scripts/generate_db_services.py`,
  `generate_chat_services.py`, `generate_frontends.py`,
  `update_concept_values.py`, `bench_local_models*.py`.
- Frontend HTMLs (5 files cleaned by initial pass +
  bulk‑pass over all `frontend/*_chat_api.html`).
- Audit reports, paper drafts, KG dumps, zenodo manifest, citation
  files, bench reports, and all `biochirp_plan/*.md` docs.

### Binary data (stripped in place)

- `resources/values/concept_values_by_db_and_field.pkl`:
  top‑level `disgenet`, `oncokb`, and `cosmic` keys removed
  (28 → 25 keys); file kept tracked so downstream tooling still loads it.

### Git history rewrite

Historical paths scrubbed via `git filter-repo` (two passes —
DisGeNET/OncoKB first, then COSMIC):

DisGeNET / OncoKB pass:
```
app/tools/disgenet
app/tools/oncokb
database/disgenet
database/oncokb
orchestrator_service/app/disgenet_tool.py
orchestrator_service/app/oncokb_tool.py
```

COSMIC pass:
```
app/tools/cosmic
database/cosmic/download.py
database/cosmic/preprocess.py
database/cosmic/SOURCE.md
database/cosmic/test_preprocess.py
orchestrator_service/app/cosmic_tool.py
```

Note `database/oncokb/gene_master_table_oncokb.parquet` was tracked in
history and was scrubbed by the first pass. No COSMIC data file was
ever in git history; only COSMIC code paths needed scrubbing. The
local 24 GB COSMIC data dir survives on disk and is now gitignored.

---

## Reproducible commands (for the audit trail)

```bash
# Pass 1 — DisGeNET / OncoKB working‑tree removal
git commit -m "Remove DisGeNET and OncoKB for licensing compliance"
~/.local/bin/git-filter-repo --force --invert-paths \
  --path app/tools/disgenet --path app/tools/oncokb \
  --path database/disgenet --path database/oncokb \
  --path orchestrator_service/app/disgenet_tool.py \
  --path orchestrator_service/app/oncokb_tool.py

# Pass 2 — COSMIC working‑tree removal
git commit -m "Remove COSMIC code paths for licensing compliance"
~/.local/bin/git-filter-repo --force --invert-paths \
  --path app/tools/cosmic \
  --path database/cosmic/download.py \
  --path database/cosmic/preprocess.py \
  --path database/cosmic/SOURCE.md \
  --path database/cosmic/test_preprocess.py \
  --path orchestrator_service/app/cosmic_tool.py

# Then force‑push (operator decides when):
git remote add origin https://github.com/abhi1238/biochirp.git
git push --force-with-lease origin main
```

---

## Caveats and required follow‑up

1. **No public exposure occurred.** The two‑week pre‑purge window was
   entirely no‑push, and verification with `git log --until=2026-04-30`
   confirmed none of the three databases ever appeared in a commit that
   reached `origin/main`. The history rewrite is therefore cosmetic
   cleanup of local commits, not a remediation of public exposure.
   No licensor disclosure email is required.

2. **Do not re‑introduce these databases** without:
   - Written permission from the licensor matching the deployment shape, **or**
   - A BYO‑key flow where every end user supplies their own personal
     API token and BioChirp does not cache responses server‑side, **plus**
     written confirmation from the licensor that this pattern is
     acceptable.
   The `.gitignore` block at the bottom of `.gitignore` is the
   first‑line guard against accidental re‑introduction.

3. **Collaborator action required after the force‑push:** anyone with
   an existing clone must re‑clone or run
   `git fetch && git reset --hard origin/main`. Any open PRs or
   feature branches based on pre‑rewrite SHAs will need to be rebased
   onto the new history.

4. **Backups retained locally** at:
   - tag `pre-licensing-purge`
   - branch `backup/pre-licensing-purge`
   - bundle `/tmp/biochirp-pre-licensing-purge.bundle` (430 MB)

   Delete these only after the force‑push has been completed and
   confirmed.
