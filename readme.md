
# BioChirp

**BioChirp** is a database-first biomedical AI assistant for precision biological exploration. It unifies LLM reasoning with curated datasets (TTD, CTD, HCDT) and real-time web research—returning **auditable, structured answers** with citations and microservice-level provenance. ([biochirp.iiitd.edu.in][1])

**Why BioChirp vs general LLMs?**

* **Database-first, LLM-last.** Sources of truth are curated biomedical tables; LLMs only interpret/summarize—never invent.
* **Auditable by design.** Every step (retrieval → joining → ranking → summarization) is logged with traceable citations and raw tool outputs.
* **Schema-aware outputs.** You get clean tables with stable columns (e.g., target, pathway, biomarker, approval status), exportable as CSV/JSON. ([biochirp.iiitd.edu.in][1])

**Best suited for**

* Drug ↔ target ↔ disease mappings, biomarkers, pathways, gene families, and approval status lookups that map to TTD/CTD/HCDT schemas (+ optional web/literature refresh). ([biochirp.iiitd.edu.in][1])

---

## 🚀 Quick Start

<p align="center">
  <img src="biochirp_demo.gif" alt="BioChirp Demo" width="720"/>
</p>

### 0) Prerequisites

* **Docker** (24+) and **Docker Compose v2**
* **Optional (for embedding ingestion):** Jupyter Notebook / Python 3.10+
* **Disk space:** ~10–15 GB for databases, embeddings, and Qdrant storage

---

### 1) Clone

```bash
git clone https://github.com/abhi1238/biochirp.git
cd biochirp
```

---

### 2) Get data & embeddings (Google Drive)

Download from:
**[https://drive.google.com/drive/folders/1E6RmupO3Oa3tUFRzZAB-ueUTZkZnpKgU?usp=sharing](https://drive.google.com/drive/folders/1E6RmupO3Oa3tUFRzZAB-ueUTZkZnpKgU?usp=sharing)**

Extract to the following locations:

```
biochirp/
├─ database/                       # <- from database.zip
└─ resources/
   └─ embeddings/
      └─ biochirp_embeddings.pkl   # <- from embeddings.zip
```

> Ensure the final file path is:
> `./resources/embeddings/biochirp_embeddings.pkl`

---

### 3) Launch Qdrant (vector DB)

BioChirp uses Qdrant for fast vector search. ([biochirp.iiitd.edu.in][1])

```bash
docker run -d \
  --name bioc_qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant:latest
```

---

### 4) (Option A) Prebuilt embeddings → **Skip ingestion**

If you placed `biochirp_embeddings.pkl` as shown above, **you can skip ingestion** and proceed to step 6.

### 4) (Option B) Re-ingest embeddings (optional)

Start Jupyter and run the notebook pipeline:

```bash
jupyter notebook
```

Open `notebooks/qdrant_without_text_index.ipynb`, set Qdrant URL to `http://localhost:6333`, and **Run All** to upload embeddings.

---

### 5) Create the isolated Docker network

```bash
docker network create --subnet=10.10.0.0/16 semantic_net
```

---

### 6) Start Redis (for caching)

```bash
docker run -d \
  --name biochirp_redis_tool \
  --network semantic_net \
  redis:7
```

---

### 7) Start all BioChirp microservices

```bash
docker compose up --build 2>&1 | tee -a combined_logs.log
```

> **GPU note:** If your machine **doesn’t** have a GPU, remove the `deploy.resources.reservations.devices` block for services that declare it in `docker-compose.yml`.

```yaml
deploy:
  resources:
    limits:
      memory: 20g
    reservations:
      memory: 16g
      # Remove this whole `devices:` section on non-GPU hosts
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

---

## 🧩 Architecture (microservices)

* **Interpreter** → parses the query, expands synonyms, extracts biomedical entities
* **Planner** → creates join/filter plans against curated tables
* **Database services** → TTD, CTD, HCDT retrieval (each is its own service)
* **Synonym & semantic matchers** → fuzzy match + transformer embeddings (separate services)
* **Web/Literature** → augments with fresh citations when needed
* **Summarizer** → LLM-only for summarizing validated tables
* **Orchestrator** → coordinates services; logs provenance and citations end-to-end

All services run on a dedicated `semantic_net` for performance and reproducibility. The UI presents **auditable** answers with citations and stable schemas. ([biochirp.iiitd.edu.in][1])

---

## 🧑‍🔬 How to use BioChirp

* Open the **live site**: [https://biochirp.iiitd.edu.in](https://biochirp.iiitd.edu.in) (or your local UI)
* Ask schema-friendly questions such as:

  * “Approved drugs targeting **EGFR** in **NSCLC**”
  * “List **PARP inhibitors** for **ovarian cancer**”
  * “Pathways associated with **NF-κB**”
* Expand/inspect provenance; download CSV/JSON for downstream analysis. ([biochirp.iiitd.edu.in][1])

---

## 🔍 When to use BioChirp (and when not)

**Use BioChirp when…**

* You need **trustworthy, citable** drug–target–disease answers.
* You want **structured tables** aligned to TTD/CTD/HCDT with reproducible provenance.
* You need **fresh** supplemental web/literature evidence, ranked and deduplicated. ([biochirp.iiitd.edu.in][1])

**Not ideal for…**

* Open-ended clinical advice, subjective opinions, or questions without grounding in curated schemas.

---

## ⚙️ Configuration

* **Qdrant:** `http://localhost:6333` (REST), `:6334` (gRPC)
* **Redis:** `biochirp_redis_tool` on `semantic_net`
* **Logs:** `combined_logs.log` (root) captures all compose output

Optional `.env` (example):

```env
TZ=Asia/Kolkata
PYTHONUNBUFFERED=1
```

---

## 🧾 Exports & auditability

* Download results as **CSV/JSON**.
* Inspect raw tool outputs and citations per step (provenance panel in UI). ([biochirp.iiitd.edu.in][1])

---

## ❓ FAQ

**How is BioChirp different from ChatGPT/general LLMs?**
It’s **Database-GPT**: curated tables first; LLMs only interpret/summarize. Outputs are citable and reproducible with tool-by-tool provenance. ([biochirp.iiitd.edu.in][1])

**What questions does it answer best?**
Drug, target/gene, disease, biomarker, pathway, and approval status queries that map to TTD/CTD/HCDT schemas. ([biochirp.iiitd.edu.in][1])

**Does it give clinical advice?**
No—research and knowledge exploration only.

**How often are databases updated?**
You control snapshots locally; refresh when official releases arrive. Release notes will be posted in the changelog.

**Is user data private?**
No login required; no user-identifiable data is stored in the app.

**Can I export results?**
Yes—CSV/JSON for Python/R/Excel workflows.

---

## 🛠️ Troubleshooting

* **Port in use:** Change host ports in `docker-compose.yml` or stop conflicting services.
* **Qdrant not reachable:** Ensure container is running and `qdrant_storage` is writable.
* **GPU errors on non-GPU host:** Remove the `devices:` reservation blocks as shown above.
* **Slow queries:** Confirm embeddings were uploaded; check Redis is running; verify network `semantic_net` exists.
* **Logs:** Everything from `docker compose up` is tee’d into `combined_logs.log`.

---

## 🧪 Live, Issues, Support

* **Live:** [https://biochirp.iiitd.edu.in](https://biochirp.iiitd.edu.in)
* **Issues:** [https://github.com/abhi1238/biochirp/issues](https://github.com/abhi1238/biochirp/issues)
* **Email:** [abhishekh@iiitd.ac.in](mailto:abhishekh@iiitd.ac.in)

> Built for biological researchers who demand clarity. ([biochirp.iiitd.edu.in][1])

---

### Repro checklist (TL;DR)

1. `git clone … && cd biochirp`
2. Place `./database/` and `./resources/embeddings/biochirp_embeddings.pkl` from Google Drive
3. `docker run -d --name bioc_qdrant -p 6333:6333 -p 6334:6334 -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant:latest`
4. `docker network create --subnet=10.10.0.0/16 semantic_net`
5. `docker run -d --name biochirp_redis_tool --network semantic_net redis:7`
6. `docker compose up --build 2>&1 | tee -a combined_logs.log`
7. Open the UI → ask schema-friendly questions → export CSV/JSON

---

If you want, I can also split this into **README.md** + **docs/SETUP.md** (with GPU/CPU variants and screenshots).

[1]: https://biochirp.iiitd.edu.in/ "BioChirp Biological Intelligence Hub"
