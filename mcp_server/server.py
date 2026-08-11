"""BioChirp MCP server — 10 curated databases + web search.

Architecture:
  Claude reads each tool description to decide which DB(s) to query.
  Tools execute directly against the live microservices — no LLM in this server.
  Claude synthesises all results in-context.

Tools exposed
─────────────
  query_ttd(question)           → TTD (Therapeutic Target Database)
  query_ctd(question)           → CTD (Comparative Toxicogenomics Database)
  query_hcdt(question)          → HCDT (Human Cancer Drug Target)
  query_hpo(question)           → HPO (Human Phenotype Ontology)
  query_clinvar(question)       → ClinVar (variant pathogenicity)
  query_reactome(question)      → Reactome (biological pathways)
  query_msigdb(question)        → MSigDB (gene signature collections)
  query_orphanet(question)      → Orphanet (rare diseases)
  query_string(question)        → STRING (protein–protein interactions)
  query_uniprot(question)       → UniProt/Swiss-Prot (protein biology)
  web_tool(query)               → Groq browser-search (live web, not a DB)
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

_DB_CATALOGUE: dict[str, dict] = {
    "ttd": {
        "display_name": "TTD — Therapeutic Target Database",
        "scope": (
            "Drugs, drug targets (proteins/genes), drug–disease indications, "
            "mechanisms of action, clinical approval status (e.g. Approved, "
            "Phase 0-4, Investigative, Preclinical, Terminated, Withdrawn from "
            "market — full TTD development-stage vocabulary), drug synonyms, "
            "disease biomarkers, KEGG pathways, compound bioactivity (IC50/Ki/"
            "EC50), and cross-reference IDs (e.g. PubChem CID/SID, ChEBI, CAS, "
            "UniProt, ATC). Best for: approved drugs, target–indication "
            "relationships, drug MoA, clinical pipeline status, target "
            "potency/bioactivity lookups."
        ),
        "port": int(os.getenv("TTD_PORT", "8012")),
    },
    "ctd": {
        "display_name": "CTD — Comparative Toxicogenomics Database",
        "scope": (
            "Chemical–gene/protein interactions, chemical–disease associations, "
            "gene–disease associations, chemical/gene/disease–pathway relationships "
            "(KEGG and Reactome), chemical-phenotype (GO biological process) effects "
            "with anatomical sites, and environmental-exposure studies/events "
            "(stressors, study populations, biomarkers, geography, outcomes). "
            "Covers drugs, environmental chemicals, toxins, genes, diseases."
        ),
        "port": int(os.getenv("CTD_PORT", "8016")),
    },
    "hcdt": {
        "display_name": "HCDT — Human Cancer Drug Target database",
        "scope": (
            "Anti-cancer drug-gene target associations, drug-disease indications, "
            "drug-pathway associations, drug-RNA interactions, and binding affinities "
            "(IC50/Ki/Kd in nM) from a gene-target counter-screen assay. Also drug "
            "physicochemical properties, synonyms/trade names, and cross-reference IDs. "
            "Best for: cancer drug-target associations, drug indications, drug-pathway "
            "links. NOT suitable for: cancer cell-line drug sensitivity screens — HCDT "
            "has no cell-line dimension in its schema."
        ),
        "port": int(os.getenv("HCDT_PORT", "8018")),
    },
    "hpo": {
        "display_name": "HPO — Human Phenotype Ontology",
        "scope": (
            "Human phenotypes/symptoms, gene–phenotype associations (which genes "
            "cause which clinical features), disease–phenotype relationships, "
            "phenotype hierarchy (19 389 terms). Best for: rare disease phenotyping, "
            "symptom→gene or gene→symptom queries. "
            "NOT suitable for: chemical/drug queries (use CTD for chemical–gene/"
            "disease links, TTD for drug treatments), variant pathogenicity (use "
            "ClinVar), or pathway data (use Reactome) — HPO has no chemical, "
            "variant, or pathway anchor."
        ),
        "port": int(os.getenv("HPO_PORT", "8054")),
    },
    "clinvar": {
        "display_name": "ClinVar",
        "scope": (
            "Genetic variants (e.g. SNVs, indels, CNVs, deletions, duplications, "
            "insertions, inversions, translocations, microsatellites) and their "
            "clinical significance (e.g. Pathogenic, Likely Pathogenic, Uncertain "
            "Significance/VUS, Likely Benign, Benign, Conflicting Classifications, "
            "Risk Factor, Drug Response), gene–variant associations, "
            "variant–disease relationships. Best for: variant pathogenicity, which "
            "genes have pathogenic variants for a given disease."
        ),
        "port": int(os.getenv("CLINVAR_PORT", "8062")),
    },
    "reactome": {
        "display_name": "Reactome",
        "scope": (
            "Biological pathways, gene–pathway memberships, pathway hierarchy "
            "(parent/child relationships), and entity cross-references to UniProt, "
            "ChEBI (chemical compounds), Ensembl, and NCBI Entrez gene IDs. Human "
            "(Homo sapiens) only. Best for: which pathways a gene, protein, or "
            "chemical compound participates in, pathway members, sub-pathway "
            "structure — queryable by gene symbol, pathway name, or UniProt/ChEBI/"
            "Ensembl/Entrez ID alone (no gene symbol required). "
            "NOT suitable for: drug-target or drug-indication queries (use TTD/CTD), "
            "variant pathogenicity (use ClinVar), or protein-protein interactions "
            "(use STRING) — no reaction-level/kinetic data."
        ),
        "port": int(os.getenv("REACTOME_PORT", "8064")),
    },
    "msigdb": {
        "display_name": "MSigDB — Molecular Signatures Database",
        "scope": (
            "Gene sets across human, mouse, and rat, in 10 top-level collections "
            "(Hallmark, Positional, Curated, Regulatory, Computational, Ontology, "
            "Oncogenic, Immunologic, CellType, CellLineage), with source-DB "
            "sub-collections within them — e.g. KEGG_MEDICUS, Reactome, BioCarta, "
            "WikiPathways, PID under Curated; GO (biological process/molecular "
            "function/cellular component), Human Phenotype Ontology, "
            "microRNA-target under Ontology. Gene–geneset membership queryable "
            "in BOTH directions — by gene symbol, or by geneset/collection/"
            "sub-collection name alone (e.g. 'genes in HALLMARK_APOPTOSIS' "
            "needs no gene anchor). "
            "NOT suitable for: chemical/drug queries, variant pathogenicity, "
            "PPI/structure, or pathway topology/reactions — membership data only; "
            "use CTD for chemical–gene-set links."
        ),
        "port": int(os.getenv("MSIGDB_PORT", "8079")),
    },
    "orphanet": {
        "display_name": "Orphanet",
        "scope": (
            "Rare diseases (10 247 entries), disease–gene associations (causative, "
            "modifying, susceptibility), inheritance mode, age of onset, "
            "prevalence/incidence estimates, disease classification hierarchy, and "
            "cross-references (OMIM, ICD-10/11, MeSH, UMLS, MedDRA, GARD, MONDO). "
            "Best for: rare disease genetics, inheritance patterns, disease "
            "prevalence. NOT suitable for: drug/treatment queries — Orphanet has "
            "no drug data; use TTD instead."
        ),
        "port": int(os.getenv("ORPHANET_PORT", "8083")),
    },
    "string": {
        "display_name": "STRING — Protein–Protein Interaction Database",
        "scope": (
            "Protein–protein interactions (physical-binding subset and full "
            "functional-association network), confidence scores (overall combined "
            "score plus seven per-channel evidence scores: neighborhood, fusion, "
            "cooccurence, coexpression, experimental, database, textmining), "
            "protein functional annotations (free-text), protein size, and protein "
            "aliases/synonyms (Ensembl, UniProt, RefSeq, KEGG). Best for: PPI networks, "
            "interaction partners of a protein, per-channel interaction evidence, "
            "protein identity/function lookup, cross-database ID mapping. "
            "NOT suitable for: gene expression levels, pathway membership, "
            "drug-target data, disease indications, or 3D structures."
        ),
        "port": int(os.getenv("STRING_PORT", "8087")),
    },
    "uniprot": {
        "display_name": "UniProt / Swiss-Prot",
        "scope": (
            "Protein identity and gene↔protein mapping, GO annotations, subcellular "
            "localisation, functional keywords, post-translational modification "
            "(PTM) sites, natural variant–disease/pathogenicity annotations, "
            "protein-protein interactions, and cross-references to Ensembl, "
            "RefSeq, HGNC, and GeneID (NOT PDB). Best for: protein function, "
            "localisation, PTMs, and variant pathogenicity lookup. "
            "Does NOT contain amino-acid sequences or protein family/domain "
            "classifications."
        ),
        "port": int(os.getenv("UNIPROT_PORT", "8089")),
    },
    "opentargets": {
        "display_name": "Open Targets",
        "scope": (
            "Live query service (Open Targets GraphQL API) for target–disease "
            "associations with per-datasource evidence scores across 10 datatypes "
            "(genetic association, somatic mutation, known drug, affected pathway, "
            "literature, animal model, RNA expression, known variant, clinical, "
            "genetic literature), drug indications, "
            "clinical trial phase/status, and target tractability/druggability "
            "assessments. Drug–target links carry clinical phase and mechanism of "
            "action but NOT their own evidence score (scores are per target-disease "
            "pair). Best queried one entity — gene, disease, or drug — at a time; "
            "combined multi-entity filters use a separate join path. Results depend "
            "on Open Targets' live API being reachable."
        ),
        "port": int(os.getenv("OPENTARGETS_PORT", "8026")),
    },
}

# ── Groq web-search settings ──────────────────────────────────────────────────
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_WEB_MODEL = os.getenv("GROQ_WEB_MODEL", "llama-3.3-70b-versatile")
_GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "90"))

# ── DB query settings ─────────────────────────────────────────────────────────
_DB_HOST = os.getenv("BIOCHIRP_DB_HOST", "localhost")
_DB_TIMEOUT = float(os.getenv("BIOCHIRP_TIMEOUT", "180"))

# Per-DB timeout overrides for services that do large multi-million-row joins.
# ClinVar scans 4-6M variant rows per gene query; 120s is too tight for high-variant
# genes (CFTR, BRCA1). UniProt and MSigDB can also be slow under concurrent load.
_DB_TIMEOUT_OVERRIDES: dict[str, float] = {
    "clinvar": float(os.getenv("CLINVAR_TIMEOUT", "300")),
    "uniprot": float(os.getenv("UNIPROT_TIMEOUT", "240")),
    "msigdb":  float(os.getenv("MSIGDB_TIMEOUT",  "240")),
}

# ── Tool helpers ──────────────────────────────────────────────────────────────

def _rows_to_markdown(rows: list[dict], max_rows: int = 50, *, total_rows: int | None = None) -> str:
    """Render `rows` (already the backend's preview, itself capped at
    HEAD_VIEW_ROW_COUNT before it ever reaches this server) as a markdown
    table, further capped to `max_rows` for display.

    `total_rows` MUST be the single authoritative row count for this query
    (the backend's `row_count`, i.e. the full result-set height BEFORE any
    preview truncation) — the same value used to build the "Results from
    <DB>" header a few lines up in `_query_db`. Without it, this function
    used to fall back to `len(rows)`, which is only the length of the
    already-truncated preview array, NOT the true total — so a query with
    600 matching rows but a 200-row preview cap would render a footer
    claiming "200 total rows", contradicting the header's correct "600
    row(s)" a few lines above it in the SAME response. Passing the
    authoritative total here keeps every count in the response in sync.
    """
    if not rows:
        return ""
    display = rows[:max_rows]
    total = total_rows if total_rows is not None else len(rows)
    truncated = total > len(display)
    # Collect all column names preserving order (use first row as template)
    cols = list(display[0].keys()) if display else []
    header = " | ".join(cols)
    sep = " | ".join(["---"] * len(cols))
    lines = [header, sep]
    for row in display:
        cells = [str(row.get(c, "")).replace("\n", " ")[:120] for c in cols]
        lines.append(" | ".join(cells))
    table = "\n".join(lines)
    if truncated:
        table += f"\n\n*({total} total rows; showing first {len(display)})*"
    return table


_MCP_PUBLIC_BASE = os.getenv("MCP_PUBLIC_BASE", "https://biochirp.iiitd.edu.in/mcp")


async def _query_db(db: str, question: str) -> str:
    """POST a natural-language question to the per-DB microservice."""
    info = _DB_CATALOGUE[db]
    url = f"http://{_DB_HOST}:{info['port']}/{db}"
    payload = {"cleaned_query": question, "parsed_value": {}}
    timeout = _DB_TIMEOUT_OVERRIDES.get(db, _DB_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError:
        return f"Error: {info['display_name']} service is not reachable (port {info['port']})."
    except httpx.TimeoutException:
        return f"Error: {info['display_name']} query timed out after {int(timeout)}s."
    except Exception as exc:
        return f"Error querying {info['display_name']}: {exc}"

    parts: list[str] = []

    msg = (data.get("message") or "").strip()
    if msg:
        parts.append(msg)

    row_count = data.get("row_count")
    rows: list[dict] = data.get("table") or []

    if rows:
        parts.append(f"\n**Results from {info['display_name']}** ({row_count} row(s)):\n")
        # Thread the SAME authoritative row_count (used in the header above)
        # into the footer instead of letting it independently derive a count
        # from len(rows) — rows here is only the backend's preview (capped at
        # HEAD_VIEW_ROW_COUNT), not the true total.
        parts.append(_rows_to_markdown(rows, total_rows=row_count))
    elif row_count == 0:
        parts.append(f"\nNo matching records found in {info['display_name']}.")

    # ── Provenance + CSV link ─────────────────────────────────────────────────
    prov_parts = []
    if data.get("db_version"):
        prov_parts.append(f"version={data['db_version']}")
    if data.get("db_snapshot_date"):
        prov_parts.append(f"snapshot={data['db_snapshot_date']}")
    if prov_parts:
        parts.append(f"\n*Source: {info['display_name']} ({', '.join(prov_parts)})*")

    # CSV download link — construct public URL via the MCP CSV proxy
    csv_path = (data.get("csv_path") or "").strip()
    if csv_path and row_count:
        filename = csv_path.split("/")[-1]
        csv_url = f"{_MCP_PUBLIC_BASE}/csv/{db}/{filename}"
        parts.append(f"\n📥 **Download full results:** [{filename}]({csv_url})")

    # Filter trace — what entities were resolved and used as DB filters
    filter_trace = data.get("filter_trace")
    if filter_trace:
        parts.append(f"\n🔍 **Filter trace:** `{filter_trace}`")

    return "\n".join(parts) if parts else f"No result from {info['display_name']}."


async def _web_search(query: str) -> str:
    """Call Groq browser_search for live web information."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return "Error: GROQ_API_KEY not configured — web search unavailable."

    system = (
        "You are a biomedical research assistant with access to live web search. "
        "Answer the user's question using up-to-date information from the web. "
        "Cite your sources. Be concise and factual.\n\n"
        "IMPORTANT: This answer comes from a web search, NOT from BioChirp's "
        "curated databases. Clearly note this provenance."
    )
    try:
        async with httpx.AsyncClient(timeout=_GROQ_TIMEOUT) as client:
            resp = await client.post(
                _GROQ_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _GROQ_WEB_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": query},
                    ],
                    "tools": [{"type": "browser_search"}],
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_completion_tokens": 2048,
                },
            )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        answer = (msg.get("content") or "").strip()
        searched = bool(msg.get("executed_tools"))
        prov = "*(live web search)*" if searched else "*(model knowledge — no live search performed)*"
        return f"{answer}\n\n{prov}" if answer else "No answer from web search."
    except Exception as exc:
        return f"Web search error: {exc}"


# ── MCP server definition ─────────────────────────────────────────────────────

server = Server(
    "biochirp",
    version="2.1.0",
    instructions=(
        "BioChirp gives you direct access to 11 curated biomedical databases plus live web search.\n"
        "The tools listed here ARE the databases — call them directly. No intermediate steps needed.\n\n"
        "ABSOLUTE RULES (violation = incorrect response):\n"
        "- NEVER use curl, bash, fetch, or the Anthropic API to reach BioChirp. Call the tools here.\n"
        "- NEVER write HTML/CSS widgets, loading animations, or 'live' UI elements — just call the tools.\n"
        "- NEVER fabricate rows, scores, interactions, or associations not returned by a tool call.\n"
        "- If a DB returns 0 rows: before reporting a gap, retry once or twice with a shorter, "
        "entity-only phrasing — strip narrative/motivation clauses and keep just the core gene/"
        "drug/disease/entity name plus the direct question. Verbose questions can cause the "
        "query parser to misread background context as an extra filter, silently returning "
        "0 rows even when the data actually exists. Only report the gap literally, e.g. "
        "'VEGFA not found in STRING (0 rows)', after a simplified retry still returns 0 rows. "
        "Do NOT substitute training-knowledge data for missing DB results.\n"
        "- NEVER combine multiple genes/drugs/diseases/entities of the SAME type into one "
        "tool call (e.g. 'EGFR or ERBB2', 'BRCA1, TP53, and PTEN', 'drugs for X, Y, or Z'). "
        "Different DBs handle multi-entity queries inconsistently and some fail silently: "
        "OpenTargets drops every entity but the first with no warning (a query about 'EGFR "
        "or ERBB2' returns ONLY EGFR results, no error, no indication ERBB2 was dropped); "
        "STRING can misread 'gene X or gene Y' as 'does X interact with Y' and return just "
        "that one relationship instead of both genes' interactors. Make ONE call per entity "
        "instead, then merge/union the results yourself — this works correctly on every DB "
        "regardless of how that DB's backend handles multi-entity input.\n"
        "- For narrative/story-form questions (motivation or backstory phrasing — 'I'm "
        "investigating...', 'as part of...', 'which keeps showing up in relation to...'), "
        "run the SAME question as two versions: (a) a maximally terse, entity-only version — "
        "just the core gene/drug/disease/entity name plus the direct question, all narrative "
        "context stripped; and (b) the fuller version closer to the original phrasing. Compare "
        "results:\n"
        "   - Agree (same/near-same row count) → proceed normally, no caveat needed.\n"
        "   - Disagree (e.g. one returns far more rows than the other) → trust the terser "
        "version's result; the fuller phrasing likely had a background/motivation term "
        "misread as a filter. Briefly note the discrepancy (e.g. 'a broader phrasing "
        "returned N rows vs M for a more literal one — using the broader result').\n"
        "   For already-terse questions with no narrative framing, one query is enough — "
        "don't double every call.\n\n"
        "Workflow:\n"
        "1. Read each tool description to decide which DB(s) apply. Call them directly.\n"
        "2. Pass a focused natural-language question; entity resolution happens inside the service.\n"
        "3. Call multiple tools in parallel when the question spans several databases.\n"
        "4. For live/current information outside DB scope, call web_search_live().\n"
        "5. Synthesise all results, attributing every fact to its source database.\n"
        "6. End EVERY multi-DB response with a '## Data Pipeline' section:\n"
        "   - Each DB queried, rows returned, and the CSV download link (📥) from the tool output.\n"
        "   - The filter/entity terms resolved (🔍 filter trace) if shown.\n"
        "   - A one-line arrow flow: DB1 (N rows) → DB2 (M rows) → Answer.\n"
        "   - Show ONLY actual tool results — never inferred or supplemented data.\n"
    ),
)

_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "Natural-language question to answer from this database. "
                "Be specific: include gene symbols, disease names, drug names, "
                "or other entities exactly as known."
            ),
        }
    },
    "required": ["question"],
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    # Tool names embed key biomedical concepts so Claude.ai's name-based tool
    # search can find the right tool without needing a separate manifest lookup.
    _DB_TOOL_NAMES = {
        "ttd":      "drugs_and_targets_ttd",
        "ctd":      "chemicals_genes_diseases_ctd",
        "hcdt":     "cancer_drugs_sensitivity_hcdt",
        "hpo":      "phenotypes_gene_disease_hpo",
        "clinvar":  "genetic_variants_pathogenicity_clinvar",
        "reactome": "biological_pathways_reactome",
        "msigdb":   "gene_sets_signatures_msigdb",
        "orphanet": "rare_diseases_genetics_orphanet",
        "string":      "protein_interactions_network_string",
        "uniprot":     "protein_function_localisation_uniprot",
        "opentargets": "drug_target_disease_evidence_opentargets",
    }

    tools: list[Tool] = []

    for db, info in _DB_CATALOGUE.items():
        tools.append(Tool(
            name=_DB_TOOL_NAMES[db],
            description=f"{info['display_name']}. {info['scope']}",
            inputSchema=_QUESTION_SCHEMA,
        ))

    tools.append(Tool(
        name="web_search_live",
        description=(
            "Live web search via Groq browser-search. Use for questions outside "
            "the scope of the curated databases (recent publications, clinical trial "
            "updates, news, methods). Always note this information comes from the web, "
            "NOT from BioChirp curated databases."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for live web information.",
                }
            },
            "required": ["query"],
        },
    ))

    return tools


_TOOL_NAME_TO_DB = {
    "drugs_and_targets_ttd":              "ttd",
    "chemicals_genes_diseases_ctd":       "ctd",
    "cancer_drugs_sensitivity_hcdt":      "hcdt",
    "phenotypes_gene_disease_hpo":        "hpo",
    "genetic_variants_pathogenicity_clinvar": "clinvar",
    "biological_pathways_reactome":       "reactome",
    "gene_sets_signatures_msigdb":        "msigdb",
    "rare_diseases_genetics_orphanet":    "orphanet",
    "protein_interactions_network_string":    "string",
    "protein_function_localisation_uniprot":  "uniprot",
    "drug_target_disease_evidence_opentargets": "opentargets",
    # legacy names — keep so any cached claude.ai session still works
    "query_ttd": "ttd", "query_ctd": "ctd", "query_hcdt": "hcdt",
    "query_hpo": "hpo", "query_clinvar": "clinvar", "query_reactome": "reactome",
    "query_msigdb": "msigdb", "query_orphanet": "orphanet",
    "query_string": "string", "query_uniprot": "uniprot",
    "query_opentargets": "opentargets",
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name in _TOOL_NAME_TO_DB:
        db = _TOOL_NAME_TO_DB[name]
        question = (arguments.get("question") or "").strip()
        if not question:
            return [TextContent(type="text", text="Error: 'question' is required.")]
        result = await _query_db(db, question)
        return [TextContent(type="text", text=result)]

    if name in ("web_tool", "web_search_live"):
        query = (arguments.get("query") or "").strip()
        if not query:
            return [TextContent(type="text", text="Error: 'query' is required.")]
        result = await _web_search(query)
        return [TextContent(type="text", text=result)]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]
