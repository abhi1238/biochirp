"""
Query expansion for Schema KG retrieval.

Each call returns TWO outputs as a dict:
  "expansion"   — 3-10 sentence verbose description for ANN search
  "clean_query" — concise normalized restatement for value mapping
                  (abbreviations expanded, trade names → INN, aliases → HGNC,
                   but NO new entities added)

The expansion maximises ANN recall (semantic breadth).
The clean_query removes ambiguity for the mapper without hallucinating new entities.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import openai

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models

logger = logging.getLogger(__name__)

LLM_MODEL    = settings.SCHEMA_KG_FILTER_MODEL
LLM_API_KEY  = os.getenv("OPENROUTER_API_KEY",         "")
LLM_BASE_URL = os.getenv("SCHEMA_KG_FILTER_BASE_URL", "https://openrouter.ai/api/v1")

_GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
if not _GROQ_API_KEY:
    logging.warning("GROQ_API_KEY is not set; Groq-backed query expansion calls will fail at runtime")
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_MODELS: frozenset[str] = frozenset(filter(None, settings.SCHEMA_KG_GROQ_MODELS.split(",")))

_client:      openai.OpenAI | None = None
_groq_client: openai.OpenAI | None = None


from ._openai_direct import is_openai, api_model, token_kwargs, extra_create_kwargs, make_client, get_client as _openai_direct_client  # noqa: E402


def _get_client_for_model(model: str) -> openai.OpenAI:
    global _client, _groq_client
    if is_openai(model):                       # openai/* → OpenAI portal directly
        return _openai_direct_client()
    if model in _GROQ_MODELS:
        if _groq_client is None:
            _groq_client = make_client(_GROQ_API_KEY, _GROQ_BASE_URL)
        return _groq_client
    if _client is None:
        _client = make_client(LLM_API_KEY, LLM_BASE_URL)
    return _client


# ── Prompt templates ──────────────────────────────────────────────────────────

_SYSTEM_BASE = """\
You are a biomedical database schema analyst.

Given a natural language question, produce two outputs:

── expansion ──────────────────────────────────────────────────────────────────
Write 3-10 sentences that make every entity type explicit. For each named
entity say what kind of thing it is:{entity_type_list}
Describe which values need to be returned (SELECT) and which are filters (WHERE).
Describe which tables or concepts need to be joined.
Use schema-relevant vocabulary throughout.

── clean_query ─────────────────────────────────────────────────────────────────
A concise, normalized restatement of the original question using standard
biomedical names — with INLINE ENTITY TYPE ANNOTATIONS. Apply these rules strictly:

After normalizing names, annotate every named entity with its column type and role:
  [column_name:role]
where role is either "filter" (the entity is a WHERE constraint) or "output" (a SELECT value).

Annotate the column placeholder, not just the entity value. Examples:
  "Which [gene_symbol:output] are associated with fever [disease_name:filter]?"
  "Which [disease_name:output] are treated by imatinib [drug_name:filter]?"
  "Which [drug_name:output] target EGFR [gene_symbol:filter]?"
  "Which [pathway_name:output] is JAK2 [gene_symbol:filter] involved in?"
  "IC50 of gefitinib [drug_name:filter] against EGFR [gene_symbol:filter] [ic50_nm:output]"

Normalization (apply BEFORE annotating):
• Expand ALL medical abbreviations using biomedical knowledge — e.g. TNBC→triple-negative breast cancer, CML→chronic myeloid leukemia, AML→acute myeloid leukemia, RA→rheumatoid arthritis, HTN→hypertension, MI→myocardial infarction, pyrexia→fever
• Expand trade/brand names to INN — e.g. Glivec→imatinib, Herceptin→trastuzumab, Keytruda→pembrolizumab
• Normalize gene aliases to HGNC official symbols — e.g. PD-L1→CD274, p53→TP53
• Do NOT expand class terms (TKI, kinase inhibitor, mAb) — keep as-is

Entity typing (for annotations):
• Diseases, conditions, syndromes, symptoms, phenotypes (cancer, fever, hypertension, inflammation, triple-negative breast cancer) → disease_name
• Gene symbols, protein targets, HGNC symbols (BRAF, EGFR, HTT, TP53, ERBB2) → gene_symbol
• Drug names, compounds, INN names (imatinib, gefitinib, pembrolizumab) → drug_name
• Pathways, biological processes, gene sets (MAPK pathway, Wnt signaling) → pathway_name
• RNA molecules (miRNA, lncRNA names) → rna_name
• Do NOT annotate non-entity words (articles, verbs, prepositions, conjunctions)

"""

_SYSTEM_FOOTER = """\
Do NOT add any entity not present in the original question.

──────────────────────────────────────────────────────────────────────────────
Return ONLY valid JSON (no markdown fences):
{
  "expansion": "...",
  "clean_query": "..."
}
"""

_SCHEMA_SECTION = """\

── Schema grounding ─────────────────────────────────────────────────────────────
The clean_query MUST align to the column constraints below.
{extra_grounding}
{schema_context}
────────────────────────────────────────────────────────────────────────────────"""


# Natural-language phrase per entity type — same vocabulary as llm_filter defaults.
_ENTITY_TYPE_PHRASES: dict[str, str] = {
    "drug":      "drug / compound / medication / inhibitor",
    "disease":   "disease / condition / indication / disorder",
    "gene":      "gene / protein / molecular target",
    "pathway":   "pathway / biological process / gene set",
    "rna":       "RNA molecule / miRNA / lncRNA / non-coding RNA",
    "phenotype": "phenotype / symptom / clinical feature",
    "protein":   "protein / sequence / structure",
    "variant":   "variant / mutation / SNP / allele",
    "go_term":   "Gene Ontology term / biological process / molecular function / cellular component",
    "compound":  "chemical compound / small molecule",
    "tissue":    "tissue / organ / anatomical site",
    "cell":      "cell type / cell line",
}

_ENTITY_TYPE_FALLBACK = (
    " drug/compound/medication, disease/condition/indication,"
    " gene/protein/molecular target, pathway/biological process, or database/datasource."
)


def _build_entity_type_list(rules: dict | None) -> str:
    """Build the entity-type enumeration line for the expansion instruction."""
    if not rules:
        return _ENTITY_TYPE_FALLBACK
    mandatory = rules.get("mandatory_entity_columns", {})
    if not mandatory:
        return _ENTITY_TYPE_FALLBACK
    phrases = [_ENTITY_TYPE_PHRASES.get(et, et.replace("_", " ")) for et in mandatory]
    return "\n  " + "\n  ".join(f"• {p}" for p in phrases)


def _build_maps_section(rules: dict) -> str:
    """Build the DB-specific normalization rules from schema_rules fields."""
    parts: list[str] = []

    abbrev = rules.get("abbreviation_map", {})
    if abbrev:
        lines = "\n".join(f"  {k} → {v}" for k, v in abbrev.items())
        parts.append(f"EXPAND abbreviations and acronyms:\n{lines}")

    trade = rules.get("trade_name_map", {})
    if trade:
        lines = "\n".join(f"  {k} → {v}" for k, v in trade.items())
        parts.append(f"USE trade names → INN drug names:\n{lines}")

    gene_aliases = rules.get("gene_alias_map", {})
    if gene_aliases:
        lines = "\n".join(f"  {k} → {v}" for k, v in gene_aliases.items())
        parts.append(f"USE HGNC official gene symbols (alias → official):\n{lines}")

    pathway_aliases = rules.get("pathway_alias_map", {})
    if pathway_aliases:
        lines = "\n".join(f"  {k} → {v}" for k, v in pathway_aliases.items())
        parts.append(f"USE canonical pathway names (alias → canonical):\n{lines}")

    class_rules = rules.get("class_entity_rules", [])
    if class_rules:
        lines = "\n".join(f"  {r}" for r in class_rules)
        parts.append(
            f"KEEP class/category names as-is — do NOT substitute with a specific member:\n{lines}"
        )

    cq_examples = rules.get("expander_clean_query_examples", [])
    if cq_examples:
        lines = "\n".join(f'  {e}' for e in cq_examples)
        parts.append(f"ADDITIONAL clean_query annotation examples for this database:\n{lines}")

    return "\n\n".join(parts) + "\n\n" if parts else ""


def expand_query(
    question:       str,
    model:          str = LLM_MODEL,
    schema_context: Optional[str] = None,
    rules:          Optional[dict] = None,
) -> dict:
    """
    Expand a question for ANN search and normalize for value mapping.

    Parameters
    ----------
    schema_context : compact column list built from the target SchemaGraph.
                     When provided, the clean_query is constrained to use
                     only valid column values (drug classes, rna_type enums, etc.).
    rules          : schema_rules dict from the DB's schema_rules.json.
                     Drives abbreviation_map, trade_name_map, gene_alias_map,
                     class_entity_rules, column_notes_override, schema_grounding_notes.

    Returns {"expansion": str, "clean_query": str}.
    Falls back to {"expansion": question, "clean_query": question} on error.
    """
    # Base prompt — entity type list is DB-specific, built from mandatory_entity_columns
    system = _SYSTEM_BASE.format(entity_type_list=_build_entity_type_list(rules))
    if rules:
        system += _build_maps_section(rules)
    system += _SYSTEM_FOOTER

    # Optional per-DB col_selection rule (from resources/prompts/db_llm_rules.yaml,
    # carried in as `_col_selection_note` on a per-request copy of `rules`).
    # APPENDED — never overwrites the base prompt. Empty → no change.
    _col_note = (rules or {}).get("_col_selection_note", "") if rules else ""
    if _col_note and _col_note.strip():
        system += ("\n\nADDITIONAL COLUMN-SELECTION RULE (DB-specific):\n"
                   + _col_note.strip())

    # Optional schema grounding block (column constraints injected when schema_context given)
    if schema_context:
        extra_grounding = ""
        if rules:
            bullets: list[str] = []
            for col, note in rules.get("column_notes_override", {}).items():
                bullets.append(f"  • {col}: {note}")
            for note in rules.get("schema_grounding_notes", []):
                bullets.append(f"  • {note}")
            if bullets:
                extra_grounding = "Key rules:\n" + "\n".join(bullets)
        system += _SCHEMA_SECTION.format(
            schema_context=schema_context,
            extra_grounding=extra_grounding,
        )

    try:
        _t_start = time.perf_counter()
        resp = _get_client_for_model(model).chat.completions.create(
            model=api_model(model),
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": f"Question: {question}"},
            ],
            temperature=0,
            seed=42,  # near-determinism on OpenAI; ignored where unsupported
            **token_kwargs(model, 600),
            **extra_create_kwargs(model),
            response_format={"type": "json_object"},
        )
        _t_end = time.perf_counter()

        # Handle extended thinking models (gpt-5-nano returns None in content)
        raw = resp.choices[0].message.content
        if raw is None:
            # Extended thinking fallback: try to get content from thinking field
            if hasattr(resp.choices[0].message, 'thinking'):
                logger.debug("Extended thinking detected, using thinking field")
                raise ValueError("Extended thinking returned but content field is None")
            else:
                raise ValueError("LLM returned None content (possibly extended thinking or API error)")
        raw = raw.strip()
        parsed = json.loads(raw)
        expansion   = parsed.get("expansion",   question)
        clean_query = parsed.get("clean_query", question)
        logger.info("Expansion (%d chars), clean_query: %s", len(expansion), clean_query[:80])
        return {"expansion": expansion, "clean_query": clean_query, "elapsed_s": _t_end - _t_start}
    except Exception as exc:
        model_name = model.split('/')[-1] if '/' in model else model
        logger.warning("Query expansion failed [%s] (%s) — using raw question", model_name, exc)
        return {"expansion": question, "clean_query": question, "elapsed_s": 0.0}
