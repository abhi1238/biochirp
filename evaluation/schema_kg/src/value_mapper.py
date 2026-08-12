"""
Value Mapper — produces a parsed_value dict in the same format as the
current NLU interpreter, but using only the columns that survived the
filter step (5-10 cols instead of the full schema).

Architecture: dual-mapper with consensus + orchestrator fallback
  1. mapper_1 (MODEL_1) + mapper_2 (MODEL_2) run in PARALLEL on clean_query
  2. If both parsed_values agree (after normalization) → return agreed value
  3. If they disagree → orchestrator (MODEL_2, larger) resolves with full context

Format (matches nlu_extractor.md convention exactly):
  - Filter/WHERE  → "column_name": ["value"]      (list with real value)
  - SELECT/output → "column_name": "requested"     (sentinel string)
  - Absent        → key omitted entirely (never null)

Example for "<drug> IC50 against <gene>" (column names are illustrative —
the actual columns come from the per-DB schema, not hardcoded here):
  {
    "<drug_col>":  ["<drug>"],
    "<gene_col>":  ["<gene>"],
    "<value_col>": "requested"
  }
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import openai

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models

logger = logging.getLogger(__name__)

LLM_MODEL    = settings.SCHEMA_KG_FILTER_MODEL
LLM_MODEL_2  = settings.SCHEMA_KG_ENSEMBLE_MODEL_2
# Tie-breaker model for the value-mapper "orchestrator" (resolves mapper_1 vs
# mapper_2 disagreement). Decoupled from MODEL_2 so it can be an independent
# third model — diverse from both mappers and unbiased toward either lane.
MAP_ORCHESTRATOR_MODEL = settings.SCHEMA_KG_MAP_ORCHESTRATOR_MODEL
LLM_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
LLM_BASE_URL = os.getenv("SCHEMA_KG_FILTER_BASE_URL", "https://openrouter.ai/api/v1")

_GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
if not _GROQ_API_KEY:
    logging.warning("GROQ_API_KEY is not set; Groq-backed value mapper calls will fail at runtime")
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


# ── Mapper system prompt ──────────────────────────────────────────────────────

# _SYSTEM_BASE is the MAPPER's system prompt template (not the orchestrator's).
# Rules 1–4 are identical for every DB: the filter→list / output→"requested" /
# omit convention is a universal contract — it doesn't come from schema_rules.json.
# DB-specific content is injected via three placeholders:
#   {rule5}       — xref_id_columns (almost always outputs)
#   {rule6}       — class_column + class_entity_rules (class terms ≠ filter values)
#   {extra_rules} — enum constraints + co_output_rules from schema_rules.json
_SYSTEM_BASE = """\
You are a biomedical database query extractor.

You receive a natural language question and a SHORT list of schema columns
that are already known to be relevant (pre-filtered by a retrieval system).

Your job: produce a `parsed_value` dict using ONLY those columns.
{critical_pathway_override}
Rules (follow exactly):

1. For each column the question provides a SPECIFIC VALUE for (a filter / WHERE condition):
   emit  "column_name": ["<exact value copied verbatim from the question>"]
    Always a JSON list, even for a single value.
    Copy the value verbatim; do NOT normalise or paraphrase.

   ⚠️ CRITICAL ANTI-HALLUCINATION RULE (enforce strictly):
   - ONLY extract values that appear WORD-FOR-WORD in the question text.
   - NEVER add values based on external knowledge, domain training, or domain inference.
   - NEVER output a value ["X"] if X is NOT mentioned explicitly in question.
   - Exception: ONLY abbreviation normalization if abbrev appears in text (e.g., "tb" → "tuberculosis").

   EXAMPLES (the principle applies to ANY entity type / column):
   ✓ CORRECT:  q names value A for col1 AND value B for col2  →  col1: ["A"], col2: ["B"]  (both explicit)
   ✗ WRONG:    q="what <entity-Y> are linked to <entity-X>?"  →  Y_col: ["<a specific Y>"]  ❌ HALLUCINATION (that Y is NOT in q)
   ✓ CORRECT:  q="what <entity-Y> are linked to <entity-X>?"  →  Y_col: "requested"  (no filter, ask for output)

   IF question does NOT mention a specific value → use "requested" instead of making up values.

   ⚠️ CONTEXT vs. FILTER — a term can appear WORD-FOR-WORD in the question and
   still NOT be a real filter, if it only explains WHY the user is asking
   (motivation / backstory) rather than WHAT the answer should be restricted to.
   DECISION TEST: remove the clause containing the term. Does the question
   still ask for the exact same thing?
     YES → the term was background/motivation. Do NOT filter on it — omit the
           column, or mark it "requested" if the question also asks for that
           entity type as output.
     NO  → the term is a real filter. Extract it per the rule above.
   Typical motivation/backstory phrasing to watch for: "I'm investigating...",
   "keeps showing up in relation to...", "as part of...", "I want to
   understand...", "before we...", "which I've been studying...".
     ✗ WRONG:  q="I'm investigating gene X, which keeps showing up in relation
                   to disease Y, and I want to know what pathway it belongs to"
                → pathway_col: ["Y"]  ❌ (removing "in relation to Y" still
                   leaves the same question — "what pathway does gene X
                   belong to?" — so Y was backstory, not a filter)
     ✓ RIGHT:  same question → gene_col: ["X"], pathway_col: "requested"
     ✓ RIGHT:  q="What pathways does gene X share with disease Y?" → BOTH
                gene_col: ["X"] AND disease_col: ["Y"] are real filters here —
                removing "with disease Y" changes the question, so Y is not
                just backstory.
{db_examples}
2. For each column the question wants RETURNED as output (a SELECT column,
   i.e. the user asks "what is the X?" or "list the X values"):
   emit  "column_name": "requested"
    This is the exact sentinel string, not a list.

3. OMIT any column that is neither a filter nor an output.
   Never emit null. Never emit an empty list.

   ⚠️ CRITICAL OUTPUT RULE — extract ONLY the DIRECT ANSWER entity type:
   - "requested" means "this column IS the answer the user wants back".
   - Mark a column "requested" ONLY when the question's own words explicitly ask
     for that entity TYPE as the result.
   - PRESENCE IN THE INPUT LIST IS NOT PERMISSION. The retrieval system surfaces
     many related columns; that does not mean you should request them all.
     If a column's entity type is not what the question asks for as output → OMIT it.
{rule3_entity_hints}
   EXAMPLES (apply this logic to any entity type / database):
   ✓ q="What genes are implicated in Alzheimer's disease?"
       → disease_name: ["Alzheimer's disease"]   (filter — named in question)
       → gene_symbol: "requested"                 (direct answer — question asks for genes)
       → drug_name: OMIT  ❌ not "requested" (user did not ask for drugs)
       → pathway_name: OMIT  ❌ not "requested" (user did not ask for pathways)

   ✓ q="Which drugs treat type 2 diabetes?"
       → disease_name: ["type 2 diabetes"]        (filter)
       → drug_name: "requested"                   (direct answer)
       → gene_symbol: OMIT  ❌ (user did not ask for genes)

   ✗ WRONG pattern — requesting every column that appeared in the input list:
       disease_name: ["Alzheimer's disease"], gene_symbol: "requested",
       drug_name: "requested", pathway_name: "requested"   ← CARTESIAN EXPLOSION

   When unsure whether a column is the direct answer → OMIT it.

4. **CRITICAL**: ONLY output columns from the input list.
   If a column is not in the input list, DO NOT include it in parsed_value.
   Exception: DB-specific co-output rules (Rules 7+ below) override this — follow them exactly.
   Outside those explicit rules, do NOT add columns from domain associations.

5. Every column in the input list must end up as EITHER a filter, "requested",
   or omitted — no other values allowed.

{rule5}
{rule6}
{extra_rules}
Return ONLY valid JSON — no markdown, no explanation:
{{
  "parsed_value": {{
    "<col>": ["<value>"] | "requested",
    ...
  }},
  "reasoning": "<one sentence>"
}}
"""


# Natural-language synonyms for each entity type — used to build Rule 3 hints.
_ENTITY_WORDS: dict[str, str] = {
    "drug":      "drugs / compounds / medications / inhibitors",
    "disease":   "diseases / indications / conditions / disorders",
    "gene":      "genes / targets / proteins",
    "pathway":   "pathways / gene sets / biological processes",
    "rna":       "RNA / miRNA / lncRNA / non-coding RNA",
    "phenotype": "phenotypes / symptoms / clinical features",
    "variant":   "variants / mutations / SNPs / alleles",
    "protein":   "proteins / sequences / structures",
    "go_term":   "GO terms / gene ontology / biological processes / molecular functions / cellular components",
    "compound":  "compounds / chemicals / molecules / structures",
    "tissue":    "tissues / organs / anatomical sites",
    "cell":      "cell types / cell lines",
}


def _build_rule3_entity_hints(mandatory: dict) -> str:
    """Build the DB-specific word→column mapping block for Rule 3."""
    if not mandatory:
        return ""
    lines = ["   Map natural language words → columns for this database:\n"]
    for entity_type, cols in mandatory.items():
        words = _ENTITY_WORDS.get(entity_type, entity_type + "s")
        col_str = " / ".join(cols)
        lines.append(f"     {words} → {col_str}\n")
        if entity_type == "pathway":
            lines.append(
                "     ⚠️  PATHWAY EXCEPTION (OVERRIDES the 'requested' default above):\n"
                "     pathway_name is 'requested' ONLY when the question does NOT name\n"
                "     a specific pathway. If the question names a pathway category\n"
                "     ('cell cycle', 'apoptosis', 'MAPK signaling', 'mTOR', 'PI3K/AKT',\n"
                "     'VEGF signaling', 'p53 pathway', etc.), pathway_name becomes a\n"
                "     FILTER list ['<pathway name>'], NOT 'requested'.\n"
                "     DECISION TEST: remove the pathway name from the question. Does it\n"
                "     become a more open question? YES → pathway_name is a FILTER.\n"
                "       ✗ WRONG: 'cell cycle pathways involving CDK4' → pathway_name: 'requested'\n"
                "       ✓ RIGHT: 'cell cycle pathways involving CDK4' → pathway_name: ['cell cycle']\n"
                "       ✓ RIGHT: 'apoptosis pathways involving BCL2' → pathway_name: ['apoptosis']\n"
                "       ✓ RIGHT: 'Is the mTOR pathway affected by rapamycin?' → pathway_name: ['mTOR']\n"
                "       ✓ OPEN:  'Which pathways is JAK2 involved in?' → pathway_name: 'requested'\n"
                "                (no specific pathway named → open output)\n"
            )
    return "".join(lines)


def _build_mapper_system(rules: dict) -> str:
    """Build value mapper system prompt dynamically from schema_rules."""
    xref_cols  = rules.get("xref_id_columns", [])
    enum_cols  = rules.get("enum_columns", {})
    co_rules   = rules.get("co_output_rules", [])
    mandatory  = rules.get("mandatory_entity_columns", {})

    # Rule 5 — cross-reference IDs are almost always outputs
    if xref_cols:
        xref_list = ", ".join(xref_cols)
        rule5 = (
            f"5. Cross-reference ID columns ({xref_list}):\n"
            "   These are almost always SELECT outputs → \"requested\"\n"
            "   Only use a filter value if the question provides a specific ID as input\n"
            "   (e.g. \"the gene with UniProt ID P00533\" → uniprot: [\"P00533\"])."
        )
    else:
        rule5 = (
            "5. Cross-reference ID columns (any column whose name ends in _id, _xref,\n"
            "   _code, or represents a structural property like smiles, inchi, formula):\n"
            "   These are almost always SELECT outputs → \"requested\"\n"
            "   Only use a filter value if the question provides the specific value as input."
        )

    # Rule 6 — class terms guard (driven by class_column + class_entity_rules in schema_rules)
    class_col   = rules.get("class_column")
    class_rules = rules.get("class_entity_rules", [])
    if class_col and class_rules:
        gene_cols = mandatory.get("gene", [])
        gene_col  = gene_cols[0] if gene_cols else "the gene/target column"
        terms     = [m.group(1) for r in class_rules
                     if (m := re.match(r'^"([^"]+)"', r))]
        class_hint = ", ".join(terms) if terms else "class terms"
        rule6 = (
            f"6. Class terms are NOT valid filter values for {class_col}.\n"
            f"   If only a class term is named ({class_hint}) without a specific value,\n"
            f"   emit {class_col} as \"requested\" and use {gene_col} as the filter instead."
        )
    else:
        rule6 = ""

    # Rules 7+ — DB-specific enum constraints and co-output rules
    extra_parts: list[str] = []
    rule_num = 7

    for col, values in enum_cols.items():
        vals_str = ", ".join(str(v) for v in values)
        extra_parts.append(
            f"{rule_num}. {col} values MUST be one of: {vals_str}.\n"
            "   Never emit any other form."
        )
        rule_num += 1

    for co_rule in co_rules:
        triggers = co_rule["trigger_columns"]
        # Support both require_column (str) and require_columns (list).
        # Use `is not None` so an explicit [] means "skip" rather than falling back.
        _req = co_rule.get("require_columns") if co_rule.get("require_columns") is not None else co_rule.get("require_column")
        require_list: list[str] = _req if isinstance(_req, list) else ([_req] if _req else [])
        behavior    = co_rule.get("require_behavior", "requested")
        reason      = co_rule.get("reason", "")
        trigger_str = ", ".join(triggers)

        if not require_list:
            continue

        if len(require_list) == 1:
            require = require_list[0]
            if behavior == "infer_from_question":
                kw = co_rule.get("trigger_keywords", [])
                if kw:
                    kw_str = ", ".join(f'"{k}"' for k in kw[:6])
                    extra_parts.append(
                        f"{rule_num}. CO-OUTPUT RULE ({reason}):\n"
                        f"   \"{require}\" is OPTIONAL — only include it when:\n"
                        f"   ({trigger_str}) appears in your output AND the question contains one of {kw_str}\n"
                        f"   If the keyword condition is met:\n"
                        f"   - Question names a specific value → \"{require}\": [\"<value>\"]\n"
                        f"   - Question uses generic phrasing  → \"{require}\": \"requested\"\n"
                        f"   ✗ WRONG: adding \"{require}\" just because ({trigger_str}) is in your output.\n"
                        f"   DEFAULT: omit \"{require}\" unless the keyword condition is met."
                    )
                else:
                    extra_parts.append(
                        f"{rule_num}. CO-OUTPUT RULE ({reason}):\n"
                        f"   Whenever ANY of ({trigger_str}) appears in your output,\n"
                        f"   you MUST also emit \"{require}\" — choose:\n"
                        f"   - Question names a specific value → \"{require}\": [\"<value>\"]\n"
                        f"   - Question names multiple values  → \"{require}\": [\"v1\", \"v2\"]\n"
                        f"   - Question uses generic phrasing  → \"{require}\": \"requested\"\n"
                        f"   NEVER omit {require} when any trigger column is present."
                    )
            else:
                trigger_behavior = co_rule.get("trigger_behavior", "any")
                condition = (
                    f"Whenever ANY of ({trigger_str}) is set to \"requested\" "
                    f"(output mode — NOT when it is a filter list)"
                    if trigger_behavior == "output_only"
                    else f"Whenever ANY of ({trigger_str}) appears in your output"
                )
                extra_parts.append(
                    f"{rule_num}. CO-OUTPUT RULE ({reason}):\n"
                    f"   {condition},\n"
                    f"   you MUST also emit:\n"
                    f"     \"{require}\": \"requested\"\n"
                    f"   even if the user did not explicitly ask for {require}."
                )
        else:
            cols_str = ", ".join(f'"{c}"' for c in require_list)
            if behavior == "infer_from_question":
                kw = co_rule.get("trigger_keywords", [])
                per_col = "\n".join(
                    f"   - \"{c}\": [\"<value>\"] if asked specifically, "
                    f"\"requested\" if generic, omit if not mentioned"
                    for c in require_list
                )
                neg_example = require_list[0]
                if kw:
                    kw_str = ", ".join(f'"{k}"' for k in kw[:6])
                    keyword_phrase = f"explicitly mentions one of {kw_str}"
                else:
                    keyword_phrase = "explicitly mentions them or uses words like 'identifier', 'ID', 'code', 'cross-reference', or names one of: " + cols_str
                extra_parts.append(
                    f"{rule_num}. CO-OUTPUT RULE ({reason}):\n"
                    f"   These columns are OPTIONAL and must ONLY appear when the question\n"
                    f"   {keyword_phrase}\n"
                    f"   Trigger columns present in your output: ({trigger_str})\n"
                    f"{per_col}\n"
                    f"   ✗ WRONG: adding \"{neg_example}\": \"requested\" just because ({trigger_str}) is in your output.\n"
                    f"   ✓ RIGHT: omit ALL of {cols_str} unless the question explicitly asks for them.\n"
                    f"   DEFAULT: omit ALL of them."
                )
            else:
                trigger_behavior = co_rule.get("trigger_behavior", "any")
                condition = (
                    f"Whenever ANY of ({trigger_str}) is set to \"requested\" "
                    f"(output mode — NOT when it is a filter list)"
                    if trigger_behavior == "output_only"
                    else f"Whenever ANY of ({trigger_str}) appears in your output"
                )
                per_col = "\n".join(f'     "{c}": "requested"' for c in require_list)
                extra_parts.append(
                    f"{rule_num}. CO-OUTPUT RULE ({reason}):\n"
                    f"   {condition},\n"
                    f"   you MUST also emit:\n"
                    f"{per_col}\n"
                    f"   even if the user did not explicitly ask for them."
                )
        rule_num += 1

    extra_rules = "\n\n" + "\n\n".join(extra_parts) + "\n" if extra_parts else "\n"

    # Append schema_grounding_notes so both mappers see the same DB-specific facts
    # the orchestrator already receives. Without this, correlated mapper errors on
    # grounding-note constraints are never caught (orchestrator only fires on disagreement).
    grounding = (rules or {}).get("schema_grounding_notes", [])
    if grounding:
        note_lines = ["DB-specific grounding notes — treat as hard constraints:"]
        for note in grounding:
            note_lines.append(f"  • {note}")
        extra_rules = extra_rules.rstrip("\n") + "\n\n" + "\n".join(note_lines) + "\n"

    # Optional per-DB mapper rule (from resources/prompts/db_llm_rules.yaml, carried
    # in as `_mapper_note` on a per-request copy of `rules`). APPENDED — never
    # overwrites the base prompt. Empty → no change.
    _mapper_note = (rules or {}).get("_mapper_note", "")
    if _mapper_note and _mapper_note.strip():
        extra_rules = (extra_rules.rstrip("\n") + "\n\nADDITIONAL ENTITY-MAPPING "
                       "RULE (DB-specific):\n" + _mapper_note.strip() + "\n")

    db_examples = _build_db_examples(rules.get("few_shot_examples", []))

    rule3_entity_hints = _build_rule3_entity_hints(mandatory)

    # Inject a CRITICAL OVERRIDE at the very top of the prompt (before Rule 1) when
    # the DB has a pathway column. Primacy ensures the LLM sees this before any other
    # rule — tests show models ignore it when buried mid-prompt inside Rule 3.
    pathway_cols = mandatory.get("pathway", [])
    if pathway_cols:
        pcol = pathway_cols[0]
        critical_pathway_override = (
            "\n⚠️  CRITICAL OVERRIDE — read before all rules below:\n"
            f"   NAMED PATHWAY FILTER: when the question names a specific pathway BEFORE\n"
            f"   the word 'pathway' or 'pathways', {pcol} MUST be a FILTER list, NOT 'requested'.\n"
            f"   Decision test: does the pathway name appear BEFORE the word 'pathway(s)'? YES → filter.\n"
            f"   ✗ WRONG: q='cell cycle pathways involving CDK4' → {pcol}: 'requested'\n"
            f"   ✓ RIGHT: q='cell cycle pathways involving CDK4' → {pcol}: ['cell cycle']\n"
            f"   ✗ WRONG: q='apoptosis pathways involving BCL2' → {pcol}: 'requested'\n"
            f"   ✓ RIGHT: q='apoptosis pathways involving BCL2' → {pcol}: ['apoptosis']\n"
            f"   ✗ WRONG: q='Is the mTOR pathway affected by rapamycin?' → {pcol}: 'requested'\n"
            f"   ✓ RIGHT: q='Is the mTOR pathway affected by rapamycin?' → {pcol}: ['mTOR']\n"
            f"   ✓ OPEN (no named pathway → use 'requested'): 'Which pathways is JAK2 involved in?'\n"
        )
    else:
        critical_pathway_override = ""

    return _SYSTEM_BASE.format(rule5=rule5, rule6=rule6, extra_rules=extra_rules,
                               db_examples=db_examples,
                               rule3_entity_hints=rule3_entity_hints,
                               critical_pathway_override=critical_pathway_override)


def _build_db_examples(examples: list) -> str:
    """
    Render optional per-DB concrete few-shot examples into the mapper prompt.

    Each example in schema_rules.json["few_shot_examples"] is:
      {"question": "...", "parsed_value": {"<col>": ["v"] | "requested"},
       "note": "<optional one-liner>"}

    Returns "" when none are supplied — the neutral, DB-agnostic examples baked
    into the prompt body remain the default, so any DB works without this field.
    """
    if not examples:
        return ""
    lines = ["", "   CONCRETE EXAMPLES for this database (mirror this exactly):"]
    for ex in examples:
        q  = ex.get("question", "")
        pv = ex.get("parsed_value", {})
        note = ex.get("note", "")
        rendered = json.dumps(pv, ensure_ascii=False)
        suffix = f"  ({note})" if note else ""
        lines.append(f'   ✓  q="{q}"  →  {rendered}{suffix}')
    return "\n".join(lines) + "\n"


# ── Orchestrator system prompt ────────────────────────────────────────────────

_ORCHESTRATOR_BASE = """\
You are a biomedical data SYNTHESIZER. Two mappers disagreed — produce the CORRECT parsed_value.
You can override both mappers. The original query is the sole source of truth for filter values.

RULES:

1. HALLUCINATION CHECK (mandatory for every filter value)
   ✓ ACCEPT: value appears VERBATIM in the original query
   ✓ ACCEPT: standard abbreviation expansion (e.g. "tb" → "tuberculosis" when "tb" is in query)
   ✗ REJECT: any value NOT explicitly in query — use "requested" or omit instead
   Example: query="<entity> linked to <X>?" → assigning a specific value NOT in the query is HALLUCINATION ❌

2. DISAGREEMENT RESOLUTION
   For each differing column:
   a) Value in query → use as filter ["value"]
   b) Value NOT in query → reject, output "requested" or omit
   c) Both mappers wrong → override with correct answer

3. ENTITY-TYPE VALIDATION — assign values to the correct column type:
{entity_type_rules}

4. FIELD-ASSIGNMENT CHECK
   A value belongs in the column matching its entity type.
   Do NOT assign a value of one entity type to a column of another entity type
   (e.g. a gene value into a pathway column, or vice-versa).

5. PATHWAY NAME NORMALIZATION (exception to Rule 1 verbatim check)
   For pathway_name FILTER values, the mapper's value does not need to be a
   verbatim substring. Accept it if the CORE PATHWAY TERM appears verbatim.
   Normalize to the shortest verbatim form instead of rejecting to "requested".
   Example: query says "mTOR pathway", mapper says ["mTOR signaling pathway"] →
     "mTOR signaling pathway" is not verbatim, but "mTOR" IS → normalize: ["mTOR"] ✓
     Do NOT reject to "requested" — that loses the named-pathway filter entirely.
   If neither mapper got the filter value right, add it yourself from the verbatim term.

6. PATHWAY CONTEXT ≠ GENE_SYMBOL
   Pathway names that match gene symbols (mTOR, MAPK, VEGF, PI3K, BCL2, CDK4, etc.)
   must go into pathway_name, NOT gene_symbol, when the question is about pathways.
   If the question says "Is the mTOR pathway affected by X?", "mTOR" is a PATHWAY
   IDENTIFIER — do NOT add gene_symbol: ["mTOR"].
   Rule: when pathway_name is a filter, NEVER also add the same term to gene_symbol.

7. CO-OUTPUT RULES — add columns the mappers missed (see DB rules below):
{rules_section}
Return ONLY valid JSON:
{{
  "parsed_value": {{ ... }},
  "reasoning": "<one sentence: what you changed from the mappers and why>"
}}
"""


def _build_orchestrator_system(rules: dict | None, kept_col_names: set | None = None) -> str:
    """
    Build orchestrator system prompt from schema_rules.

    Gives the orchestrator field-by-field validation rules: entity-type matching,
    xref_id_columns, class_column/class_entity_rules, enum constraints,
    co_output_rules (only if both trigger & require columns are queryable),
    and schema_grounding_notes.

    Parameters
    ----------
    rules : dict or None
        Schema rules from schema_rules.json
    kept_col_names : set or None
        Queryable columns available in the current context.
        If None, all co-output rules are included (backward compatibility).
    """
    if not rules:
        return _ORCHESTRATOR_BASE.format(entity_type_rules="", rules_section="")

    # Default: if kept_col_names not provided, assume all columns are queryable
    if kept_col_names is None:
        kept_col_names = set()

    # ── Entity-type rules: map columns to their entity types ─────────────────────
    mandatory = rules.get("mandatory_entity_columns", {})
    entity_type_lines = []
    for entity_type, cols in mandatory.items():
        if cols:
            col_str = ", ".join(cols)
            entity_type_lines.append(f"  {entity_type.upper()}: {col_str}")

    if entity_type_lines:
        entity_type_rules = "Values MUST match their entity type:\n" + "\n".join(entity_type_lines)
    else:
        entity_type_rules = "(No entity-type constraints for this database.)"

    sections: list[str] = []

    # ── XRef ID columns: almost always outputs ────────────────────────────────
    xref_cols = rules.get("xref_id_columns", [])
    if xref_cols:
        sections.append(
            "Cross-reference ID columns — almost always SELECT outputs:\n"
            f"  {', '.join(xref_cols)}\n"
            "  Emit as \"requested\" unless the query supplies a specific ID value."
        )

    # ── Class column: class terms are NOT valid filter values ─────────────────
    class_col   = rules.get("class_column")
    class_rules = rules.get("class_entity_rules", [])
    if class_col and class_rules:
        mandatory = rules.get("mandatory_entity_columns", {})
        gene_col  = (mandatory.get("gene") or [None])[0] or "gene column"
        rules_str = "\n".join(f"  • {r}" for r in class_rules)
        sections.append(
            f"Class terms are NOT valid filter values for {class_col}.\n"
            f"  If only a class term is given without a specific {class_col} value:\n"
            f"  emit {class_col}: \"requested\" and filter on {gene_col} instead.\n"
            f"  Class term rules:\n{rules_str}"
        )

    # ── Enum constraints: values must be from the allowed set ─────────────────
    enum_cols = rules.get("enum_columns", {})
    if enum_cols:
        enum_lines = ["Enum column constraints — values MUST match exactly:"]
        for col, vals in enum_cols.items():
            enum_lines.append(f"  {col}: {vals}")
        sections.append("\n".join(enum_lines))

    # ── Co-output rules: columns that must travel together ────────────────────
    # ⚠️ IMPORTANT: Only enforce co-output rules if required columns are VALID OUTPUT columns
    # (i.e., exist in the schema and CAN be output), regardless of whether they're in kept_col_names.
    # The whole point: orchestrator CAN ADD missing co-outputs even if not initially retrieved.
    co_rules = rules.get("co_output_rules", [])
    if co_rules:
        co_lines = ["Co-output rules (applied if trigger columns present):"]
        # Build set of valid output columns from the co-output rules themselves
        # (any column that appears in trigger_columns or require_columns is a valid output column)
        all_valid_output_cols = set()
        for _r in co_rules:
            all_valid_output_cols.update(_r.get("trigger_columns", []))
            _rq = (_r.get("require_columns") if _r.get("require_columns") is not None
                   else ([_r.get("require_column")] if _r.get("require_column") else []))
            reqs = _rq if isinstance(_rq, list) else ([_rq] if _rq else [])
            all_valid_output_cols.update(reqs)

        for _r in co_rules:
            trigs = _r.get("trigger_columns", [])
            _rq   = (_r.get("require_columns") if _r.get("require_columns") is not None
                     else ([_r.get("require_column")] if _r.get("require_column") else []))
            reqs  = _rq if isinstance(_rq, list) else ([_rq] if _rq else [])
            if not reqs or not trigs:
                continue

            # Check if ALL require columns are VALID (exist in schema)
            # Do NOT check if they're in kept_col_names — orchestrator should add them!
            all_reqs_valid = all(req in all_valid_output_cols or req in kept_col_names for req in reqs)
            if not all_reqs_valid:
                # Skip this rule — required column doesn't exist in schema
                continue

            behavior = _r.get("require_behavior", "requested")
            trig_str = ", ".join(trigs)
            req_str  = ", ".join(reqs)
            reason   = _r.get("reason", "co-output dependency")
            if behavior == "requested":
                trigger_behavior = _r.get("trigger_behavior", "any")
                condition = (
                    f"If any of ({trig_str}) is set to \"requested\" (output mode, not a filter)"
                    if trigger_behavior == "output_only"
                    else f"If any of ({trig_str}) is in output"
                )
                co_lines.append(
                    f"  • ({reason}): {condition}:\n"
                    f"    → ALWAYS add {req_str}: \"requested\" if not already present"
                )
            else:
                co_lines.append(
                    f"  • ({reason}): If any of ({trig_str}) is in output AND query explicitly requests {req_str}:\n"
                    f"    → add {req_str} as filter [\"value\"] or \"requested\" per context"
                )

        if len(co_lines) > 1:  # Only add section if there are rules after filtering
            sections.append("\n".join(co_lines))
        else:
            sections.append("Co-output rules: (none applicable — require columns not queryable)")

    # ── Schema grounding notes: DB-specific facts the parsers were told ────────
    grounding = rules.get("schema_grounding_notes", [])
    if grounding:
        note_lines = ["DB-specific grounding notes (same facts the parsers received):"]
        for note in grounding:
            note_lines.append(f"  • {note}")
        sections.append("\n".join(note_lines))

    rules_section = ("\n\n" + "\n\n".join(sections) + "\n") if sections else ""

    # ── Orchestrator few-shot examples ────────────────────────────────────────
    # Section 6: concrete mapper-vs-mapper disagreement examples (from orchestrator_examples)
    orch_examples = rules.get("orchestrator_examples", [])
    examples_section = ""
    if orch_examples:
        lines = ["\n6. CONCRETE DISAGREEMENT EXAMPLES (follow these exactly):\n"]
        for ex in orch_examples:
            q  = ex.get("question", "")
            m1 = json.dumps(ex.get("mapper_1", {}), ensure_ascii=False)
            m2 = json.dumps(ex.get("mapper_2", {}), ensure_ascii=False)
            ok = json.dumps(ex.get("correct",   {}), ensure_ascii=False)
            why = ex.get("reasoning", "")
            lines.append(f'   q="{q}"')
            lines.append(f'   Mapper-1: {m1}')
            lines.append(f'   Mapper-2: {m2}')
            lines.append(f'   ✓ CORRECT: {ok}')
            lines.append(f'   Why: {why}\n')
        examples_section = "\n".join(lines)

    # Section 7: correct-output examples (from few_shot_examples) — same pool the
    # mappers saw; orchestrator needs them to resolve disagreements correctly
    # (e.g. pathway vs disease flip: mapper-1 says pathway_name, mapper-2 says disease_name)
    shared_examples = _build_db_examples(rules.get("few_shot_examples", []))
    if shared_examples:
        shared_examples = (
            "\n7. CORRECT OUTPUT EXAMPLES (what the final parsed_value should look like):"
            + shared_examples
        )

    system = (_ORCHESTRATOR_BASE.format(entity_type_rules=entity_type_rules, rules_section=rules_section)
              + examples_section + shared_examples)

    # Optional per-DB tiebreaker rule (from resources/prompts/db_llm_rules.yaml,
    # carried in as `_tiebreaker_note` on a per-request copy of `rules`). This is
    # the dual-mapper DISAGREEMENT RESOLVER ("orchestrator") — the tie-breaker
    # that decides between mapper_1 and mapper_2. APPENDED — never overwrites.
    _tb_note = (rules or {}).get("_tiebreaker_note", "")
    if _tb_note and _tb_note.strip():
        system += ("\n\nADDITIONAL TIE-BREAK RULE (DB-specific):\n" + _tb_note.strip())
    return system


# ── Co-output enforcement (Python-level safety net) ──────────────────────────

# Fallback column sets used ONLY when no schema_rules.json is supplied (rules is
# None). With rules present — every production path — these are derived per-DB
# from co_output_rules / xref columns, so the fallbacks stay empty: no DB-specific
# column names leak into the generic path.
_STRUCTURAL_COLS: set[str] = set()
_DISEASE_ID_COLS: set[str] = set()
# Pattern that genuine cross-ref ID values follow (UniProt P…, HGNC:…, ENSG…, hsa…, SMP…)
import re as _re
_ID_FORMAT_RE = _re.compile(
    r'^(P\d{5}|[OQ]\d{4}|HGNC:\d+|ENSG\d+|ENST\d+|hsa\d+|SMP\d+|R-HSA-\d+|'
    r'\d+|D\d+|C\d+|MIM:\d+|HP:\d+)'
)
# Gene-symbol-like pattern: 2-12 uppercase letters/digits, possibly with hyphen
_GENE_SYM_RE = _re.compile(r'^[A-Z][A-Z0-9\-]{1,11}$')
# Drug code pattern: 3+ consecutive digits (MK-2206, BMS-354825) distinguishes drugs from genes
_DRUG_CODE_RE = _re.compile(r'\d{3,}')


def _enforce_co_outputs(
    pv: dict,
    kept_col_names: set,
    question: str = "",
    rules: dict | None = None,
) -> dict:
    """
    Post-process parsed_value to enforce co-output rules LLMs sometimes miss.

    All column sets and entity column names are derived from `rules`
    (co_output_rules + mandatory_entity_columns + class_column + enum_columns).
    Module-level fallback constants are used only when rules is None.
    """
    result = dict(pv)

    # ── Derive entity column names from schema rules ───────────────────────────
    mandatory   = rules.get("mandatory_entity_columns", {}) if rules else {}
    class_col   = rules.get("class_column") if rules else None         # e.g. "drug_name"
    disease_col = (mandatory.get("disease") or [None])[0]             # e.g. "disease_name"
    gene_col    = (mandatory.get("gene")    or [None])[0]             # e.g. "gene_symbol"
    pathway_col = (mandatory.get("pathway") or [None])[0]             # e.g. "pathway_name"

    # Paired enum columns: replaces the mandatory.get("rna") heuristic.
    # JSON field: "paired_enum_columns": [{"name_col": "rna_name", "type_col": "rna_type",
    #                                       "verbatim_extraction": true}]
    # Any DB with a (name_col, type_col) pair where type_col is an enum gets
    # bidirectional pairing + optional verbatim extraction — not just RNA.
    _paired: list[dict] = rules.get("paired_enum_columns", []) if rules else []
    rna_name_col: str | None = _paired[0].get("name_col") if _paired else None
    rna_type_col: str | None = _paired[0].get("type_col") if _paired else None
    _verbatim_extract: bool  = _paired[0].get("verbatim_extraction", False) if _paired else False

    # ── Derive column sets from co_output_rules ───────────────────────────────
    if rules:
        # Read xref ID columns directly from JSON fields instead of inferring
        # from co_output_rules — the heuristic (single trigger = entity col)
        # misclassifies msigdb's geneset_name and wikipathways' pathway_name.
        _gene_xref_cols: set[str]  = set(rules.get("gene_xref_id_columns", []))
        _disease_id_cols: set[str] = set(rules.get("disease_xref_id_columns", []))
        _rna_type_enums: list[str] = (
            list(rules.get("enum_columns", {}).get(rna_type_col, []))
            if rna_type_col else []
        )
    else:
        _disease_id_cols = _DISEASE_ID_COLS
        _gene_xref_cols  = {'hgnc_id', 'entrez_id', 'ensembl_id', 'uniprot_id'}
        _rna_type_enums  = []

    # ── Gene-symbol-like value in a gene-xref ID column ──────────────────────
    # e.g. uniprot_id: ["VEGFA"] → move to gene_col, set ID col to "requested"
    for _id_col in _gene_xref_cols:
        if _id_col in result and isinstance(result[_id_col], list):
            _gene_like = [v for v in result[_id_col]
                          if isinstance(v, str)
                          and _GENE_SYM_RE.match(v) and not _ID_FORMAT_RE.match(v)]
            if _gene_like:
                if gene_col and gene_col in kept_col_names and gene_col not in result:
                    result[gene_col] = _gene_like
                result[_id_col] = 'requested'

    # ── Disease xref in kept → disease_col output ────────────────────────────
    if (disease_col
            and any(c in kept_col_names for c in _disease_id_cols)
            and disease_col in kept_col_names
            and disease_col not in result):
        result[disease_col] = 'requested'

    # ── Contaminated disease_col filter: contains a class_col value ───────────
    # e.g. model returns disease_name: ["indications of pembrolizumab"] when
    # drug_name: ["pembrolizumab"] is already a filter → disease_name is output.
    if (disease_col and class_col
            and isinstance(result.get(disease_col), list)
            and isinstance(result.get(class_col), list)):
        _drug_pats = [_re.escape(v.lower()) for v in result[class_col]
                      if isinstance(v, str)]
        _dn_text   = ' '.join(v.lower() for v in result[disease_col]
                              if isinstance(v, str))
        if _drug_pats and any(
            _re.search(r'\b' + _dp + r'\b', _dn_text) for _dp in _drug_pats
        ):
            result[disease_col] = 'requested'

    # ── rna_type_col verbatim extraction ──────────────────────────────────────
    if rna_type_col and _verbatim_extract and result.get(rna_type_col) == 'requested' and question and _rna_type_enums:
        _q_rna = question.lower()
        _found_rna = [t for t in _rna_type_enums if t.lower() in _q_rna]
        if _found_rna:
            result[rna_type_col] = _found_rna

    # ── RNA type ↔ name bidirectional pairing ─────────────────────────────────
    if rna_type_col and rna_name_col:
        if (rna_type_col in result
                and isinstance(result.get(rna_type_col), list)
                and rna_name_col in kept_col_names
                and rna_name_col not in result):
            result[rna_name_col] = 'requested'
        if (rna_name_col in result
                and rna_type_col in kept_col_names
                and rna_type_col not in result):
            result[rna_type_col] = 'requested'

    # ── Gene xref IDs co-travel + gene_col ───────────────────────────────────
    if any(result.get(c) == 'requested' for c in _gene_xref_cols):
        for c in _gene_xref_cols:
            if c in kept_col_names and c not in result:
                result[c] = 'requested'
        if gene_col and gene_col in kept_col_names and gene_col not in result:
            result[gene_col] = 'requested'

    # ── Gene-like value in class_col ──────────────────────────────────────────
    # Catches: ["BRAF"] (gene sym used as drug filter) and
    #          ["ERBB2-targeting drugs"] (gene-descriptor, not an INN).
    # Drug codes (MK-2206, BMS-354825) are excluded via _DRUG_CODE_RE (3+ digits).
    if class_col and class_col in result and isinstance(result.get(class_col), list):
        _real_drugs: list[str] = []
        _extr_genes: list[str] = []
        for _v in result[class_col]:
            if not isinstance(_v, str):
                _real_drugs.append(_v)
                continue
            if (_GENE_SYM_RE.match(_v) and not _ID_FORMAT_RE.match(_v)
                    and not _DRUG_CODE_RE.search(_v)):
                _extr_genes.append(_v)
            else:
                _gm = _re.match(r'^([A-Z][A-Z0-9]{1,11})-targeting\s+drugs?$',
                                 _v, _re.IGNORECASE)
                if _gm:
                    _extr_genes.append(_gm.group(1).upper())
                else:
                    _real_drugs.append(_v)
        if _extr_genes and not _real_drugs:
            result[class_col] = 'requested'
            if gene_col and gene_col in kept_col_names and gene_col not in result:
                result[gene_col] = _extr_genes

    # ── pathway_col implicit output ───────────────────────────────────────────
    # Only add pathway_name as "requested" when the question explicitly mentions
    # pathways. Do NOT add it just because ANN retrieved it; that causes Cartesian
    # join explosions for disease→gene queries where pathway tables are unrelated.
    _pathway_kws = {'pathway', 'pathways', 'kegg', 'biocarta', 'gene set', 'gene-set'}
    _q_for_pathway = question.lower() if question else ''
    # Add pathway_col when the question explicitly mentions pathways — keyword guard
    # is sufficient; do NOT require it in kept_col_names because ANN retrieval is
    # dominated by drug-target columns for gene queries (e.g. DRD2) and pathway_name
    # would otherwise be silently dropped despite the 3-hop FK path existing in TTD.
    if (pathway_col and pathway_col not in result
            and any(kw in _q_for_pathway for kw in _pathway_kws)):
        result[pathway_col] = 'requested'

    # ── Pathway filter extraction from query ──────────────────────────────────
    # "IDs/identifier for [the] X pathways/affecting/involving" → X is a FILTER.
    # Uses pathway_col from mandatory_entity_columns; skipped for DBs without one.
    if pathway_col and result.get(pathway_col) == 'requested' and question:
        _q_pf = question.lower()
        _pm = _re.search(
            r'\b(?:ids?|identifiers?)\s+for\s+(?:the\s+)?'
            r'([\w][\w\s\-]+?)\s+(?:pathways?|affected|involving|connected)\b',
            _q_pf
        )
        if _pm:
            _extracted = _pm.group(1).strip()
            _orig_words = set(question.split())
            _ewords = _extracted.split()
            _is_gene = (
                len(_ewords) == 1
                and _ewords[0].upper() in _orig_words
                and _GENE_SYM_RE.match(_ewords[0].upper())
            )
            _is_generic = _extracted.lower() in ('pathway', 'pathways')
            if not _is_gene and not _is_generic:
                result[pathway_col] = [_extracted]

    # ── Keyword-guarded infer_from_question rules ────────────────────────────
    # For rules that declare `trigger_keywords`, strip their require_columns
    # from the result if NONE of the keywords appear in the question.
    # Prevents LLMs from adding columns that belong to a different table
    # (e.g. ki_nm/ic50_nm added for a general drug+gene association query).
    if rules and question:
        _q_kw = question.lower()
        for _r in rules.get("co_output_rules", []):
            _kws = _r.get("trigger_keywords", [])
            if not _kws:
                continue
            _reqs = set(_r.get("require_columns", []))
            _in_result = _reqs & set(result)
            if not _in_result:
                continue
            if not any(_kw.lower() in _q_kw for _kw in _kws):
                for _c in _in_result:
                    # Only strip output (requested) columns — never remove filter values
                    # (lists). A synonym: ['RTA-408'] is a query filter, not a co-output
                    # hallucination; the keyword guard must not erase it.
                    if result.get(_c) == "requested":
                        del result[_c]

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_user_prompt(query: str, kept: List[Tuple[str, float]], graph) -> str:
    """Build mapper user prompt. query should be the clean_query when available."""
    lines = [
        f'Question: "{query}"',
        "",
    ]
    # If the clean_query contains inline entity-type annotations [column_name:role],
    # add a decoder line so the mapper LLM knows how to use them.
    if "[" in query and ":filter]" in query or "[" in query and ":output]" in query:
        lines.append(
            "Note: the question above contains inline annotations [column_name:role]. "
            "These are authoritative entity-type labels from the expander. "
            "Use them directly: ':filter' → set that column to [\"value\"], ':output' → set to \"requested\"."
        )
        lines.append("")
    lines += [
        "Relevant schema columns (use ONLY these):",
    ]
    for col_id, _score in kept:
        col_name = col_id.split(".")[-1]
        desc = graph.node_description(col_id) if graph else ""
        lines.append(f"  - {col_name}: {desc}")
    lines += [
        "",
        'Produce a parsed_value dict. Filters → ["value"], outputs → "requested", absent → omit.',
    ]
    return "\n".join(lines)


def _clean_pv(pv: dict) -> dict:
    """Ensure every value is either a non-empty list or the string 'requested'."""
    if not isinstance(pv, dict):
        logger.warning("_clean_pv: expected dict, got %s — treating as empty", type(pv).__name__)
        return {}
    clean = {}
    for col, val in pv.items():
        if val == "requested":
            clean[col] = "requested"
        elif isinstance(val, list) and len(val) == 1 and val[0] == "requested":
            # LLM sometimes wraps the sentinel in a list; treat as "requested"
            clean[col] = "requested"
        elif isinstance(val, list) and val:
            clean[col] = val
        elif isinstance(val, str) and val and val != "requested":
            clean[col] = [val]   # coerce bare string → list
    return clean


def _normalize_pv(pv: dict) -> dict:
    """Lowercase keys and values, sort lists — for agreement comparison only."""
    norm = {}
    for k, v in pv.items():
        kn = k.lower().strip()
        if v == "requested":
            norm[kn] = "requested"
        elif isinstance(v, list):
            norm[kn] = sorted(x.lower().strip() for x in v if x)
    return norm


def _agree(pv1: dict, pv2: dict) -> bool:
    """True if both parsed_values are structurally identical after normalization.

    Also checks that both mappers output the SAME SET OF KEYS — a mapper that
    hallucinated extra fields (e.g. gene_symbol with a specific gene name that
    the other mapper omitted) must be sent to the orchestrator, not silently merged.
    """
    norm1 = _normalize_pv(pv1)
    norm2 = _normalize_pv(pv2)
    if norm1.keys() != norm2.keys():
        return False
    return norm1 == norm2


def _enforce_cooutput_rules_directly(pv: dict, kept_col_names: set, rules: Optional[dict] = None) -> dict:
    """
    Directly enforce co-output rules by adding missing required columns.

    If a trigger column is present, automatically add all required columns as "requested".
    This avoids expensive orchestrator calls when the fix is straightforward.

    Example:
      Input:  pv = {"drug_smiles_iso": "requested"}
      Rule:   if drug_smiles_iso → require drug_name
      Output: pv = {"drug_smiles_iso": "requested", "drug_name": "requested"}
    """
    if not rules:
        return pv

    co_rules = rules.get("co_output_rules", [])
    if not co_rules:
        return pv

    result = dict(pv)  # Work on a copy
    changes_made = False

    for rule in co_rules:
        trigs = rule.get("trigger_columns", [])
        _rq = (rule.get("require_columns") if rule.get("require_columns") is not None
               else ([rule.get("require_column")] if rule.get("require_column") else []))
        reqs = _rq if isinstance(_rq, list) else ([_rq] if _rq else [])
        reason = rule.get("reason", "co-output dependency")

        if not reqs or not trigs:
            continue

        # Rules with require_behavior="infer_from_question" are CONDITIONAL on
        # question content — already encoded in the LLM mapper prompt. Skip them
        # here to avoid unconditionally adding optional columns (e.g. ki_nm/ic50_nm/kd_nm
        # being added for any drug query just because drug_name is present).
        if rule.get("require_behavior") == "infer_from_question":
            continue

        # Check: is ANY trigger column present in result?
        # trigger_behavior="output_only" → only fire when the column value is "requested"
        # (i.e., output intent), not when it is a filter list.
        trigger_behavior = rule.get("trigger_behavior", "any")
        if trigger_behavior == "output_only":
            trigger_present = any(
                trig in result and result[trig] == "requested"
                for trig in trigs
            )
        else:
            trigger_present = any(trig in result for trig in trigs)
        if not trigger_present:
            continue

        # If trigger present, ADD ALL required columns as "requested" if missing
        for req in reqs:
            if req not in result:
                result[req] = "requested"
                changes_made = True
                logger.info(
                    "Co-output enforcement: trigger %s present → adding %s: 'requested' (%s)",
                    trigs, req, reason
                )

    return result


# ── Single mapper call ────────────────────────────────────────────────────────

def _map_single(
    query:  str,
    kept:   List[Tuple[str, float]],
    graph,
    model:  str,
    rules:  Optional[dict] = None,
) -> Tuple[Dict, dict]:
    """One mapper LLM call. Returns (parsed_value, meta)."""
    if not kept:
        return {}, {"reasoning": "no columns", "raw_response": ""}

    system_prompt = _build_mapper_system(rules or {})
    user_prompt = _build_user_prompt(query, kept, graph)
    _t0 = time.perf_counter()

    # ── RETRY LOOP: Max 3 attempts (initial + 2 retries) on JSON parsing errors
    max_retries = 2
    attempt = 0
    parsed = None
    last_error = None

    while attempt <= max_retries:
        try:
            response = _get_client_for_model(model).chat.completions.create(
                model=api_model(model),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0,
                seed=42,  # near-determinism on OpenAI; ignored by providers that don't support it
                **token_kwargs(model, 1024),
                **extra_create_kwargs(model),
                response_format={"type": "json_object"},
            )
            raw    = response.choices[0].message.content.strip()
            parsed = json.loads(raw)
            break  # Success — exit retry loop
        except json.JSONDecodeError as json_err:
            last_error = json_err
            attempt += 1
            if attempt <= max_retries:
                logger.warning("Mapper (%s) JSON parse error (attempt %d/%d): %s — retrying...",
                              model.split('/')[-1], attempt, max_retries + 1, str(json_err)[:60])
            else:
                logger.warning("Mapper (%s) JSON parse error after %d attempts — fallback to all-requested",
                              model.split('/')[-1], max_retries + 1)
                col_names = [c.split(".")[-1] for c, _ in kept]
                return (
                    {col: "requested" for col in col_names},
                    {"reasoning": f"JSON error after {max_retries+1} attempts: {last_error}",
                     "raw_response": "", "llm_s": 0.0},
                )
        except Exception as e:
            logger.warning("Mapper (%s) request failed (%s) — fallback to all-requested", model, e)
            col_names = [c.split(".")[-1] for c, _ in kept]
            return (
                {col: "requested" for col in col_names},
                {"reasoning": f"LLM error: {e}", "raw_response": "", "llm_s": 0.0},
            )
    _t1 = time.perf_counter()

    pv   = _clean_pv(parsed.get("parsed_value", {}))

    # ── QUERYABILITY FILTER: Remove mapper outputs for non-queryable columns
    # Mapper is told "use ONLY these columns" but may add columns via co-output rules.
    # Strip any fields not in the provided kept list, EXCEPT columns that are
    # declared as required by a co_output_rule — LLM may correctly emit them even
    # when ANN didn't retrieve the trigger column (e.g. target_name alongside gene_symbol).
    kept_col_names = {col_id.split(".")[-1] for col_id, _ in kept}
    _co_output_cols: set[str] = set()
    if rules:
        for _co_rule in rules.get("co_output_rules", []):
            for _req_col in (_co_rule.get("require_columns") or []):
                _co_output_cols.add(_req_col)
    pv_filtered = {}
    for col, val in pv.items():
        if col in kept_col_names or col in _co_output_cols:
            pv_filtered[col] = val
        else:
            logger.debug("Mapper (%s) output non-queryable field %s → stripped", model[:20], col)
    pv = pv_filtered

    meta = {"reasoning": parsed.get("reasoning", ""), "raw_response": raw, "llm_s": _t1 - _t0}
    logger.info("Mapper (%s): %s", model[:20], pv)
    return pv, meta


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _orchestrator_fallback_pv(pv1: dict, pv2: dict) -> tuple:
    """Choose the better fallback when the orchestrator itself fails.

    When mapper_1 crashed and fell back to "all-requested" (no filter values),
    its degenerate output adds every disease-specific table to needed_tables —
    the parallel-table collapse then arbitrarily discards the correct one.
    Prefer pv2 (which succeeded) in that case so the planner sees a clean,
    focused plan. Returns (chosen_pv, resolution_string).
    """
    pv1_is_degenerate = pv1 and not any(isinstance(v, list) for v in pv1.values())
    pv2_is_good = pv2 and any(isinstance(v, list) for v in pv2.values())
    if pv1_is_degenerate and pv2_is_good:
        logger.info("Orchestrator fallback: pv1 is degenerate (all-requested) — using pv2 instead")
        return pv2, "fallback to mapper_2 (pv1 degenerate)"
    return pv1, "fallback to mapper_1"


def _orchestrate(
    original_query: str,
    clean_query:    str,
    pv1:            dict,
    pv2:            dict,
    kept:           List[Tuple[str, float]],
    graph,
    model:          str,
    rules:          Optional[dict] = None,
    clean_query_2:  Optional[str] = None,
) -> Tuple[Dict, dict]:
    """Resolve disagreement between pv1 and pv2 using an orchestrator LLM."""
    # Dynamic few-shot: swap static examples for the top-K tiebreaker bank examples
    # for this db+question. Falls back to `rules` unchanged when the bank is empty.
    rules = _inject_fewshots(rules, original_query, _db_of(graph, kept), "tiebreaker")

    col_lines = []
    for col_id, _score in kept:
        col_name = col_id.split(".")[-1]
        desc = graph.node_description(col_id) if graph else ""
        col_lines.append(f"  - {col_name}: {desc}")

    m1_short = LLM_MODEL.split('/')[-1]
    m2_short = LLM_MODEL_2.split('/')[-1]
    # Each parser received its own model's cleaned query (trade names → INN,
    # aliases → HGNC, abbreviations expanded). Show which query each used.
    cq2_line = (
        f'\nClean query ({m2_short} expander): "{clean_query_2}"'
        if clean_query_2 else ""
    )
    p2_cq_note = "its own clean query above" if clean_query_2 else "the same clean query as Parser-1"

    # Build field-by-field comparison table for clarity (case-insensitive for strings)
    all_cols = sorted(set(pv1.keys()) | set(pv2.keys()))
    comparison_lines = ["FIELD-BY-FIELD COMPARISON:"]
    for col in all_cols:
        v1 = pv1.get(col, "(absent)")
        v2 = pv2.get(col, "(absent)")
        # Normalize for comparison: lowercase strings, sort lists
        def _normalize_for_compare(v):
            if isinstance(v, str):
                return v.lower()
            elif isinstance(v, list):
                return sorted(x.lower() if isinstance(x, str) else str(x) for x in v)
            return v
        v1_norm = _normalize_for_compare(v1)
        v2_norm = _normalize_for_compare(v2)
        mark = "✓ AGREE" if v1_norm == v2_norm else "⚠ DISAGREE"
        comparison_lines.append(f"  {col}: {mark}")
        comparison_lines.append(f"    Parser-1: {v1}")
        comparison_lines.append(f"    Parser-2: {v2}")

    user_prompt = f"""ORIGINAL QUERY (use for validation): "{original_query}"
Clean query ({m1_short} expander): "{clean_query}"{cq2_line}

Relevant columns:
{chr(10).join(col_lines)}

Parser-1 ({m1_short}):
{json.dumps(pv1, indent=2)}

Parser-2 ({m2_short}):
{json.dumps(pv2, indent=2)}

{chr(10).join(comparison_lines)}

Apply the rules from your system prompt.
For each field: only values VERBATIM in the original query above are valid filters.
Override both parsers if needed. Add any missing co-outputs per the DB rules."""

    # Extract kept column names for co-output rule queryability check
    kept_col_names = {col_id.split(".")[-1] for col_id, _ in kept}

    _t_orch0 = time.perf_counter()

    # ── RETRY LOOP: Max 3 attempts (initial + 2 retries) on JSON parsing errors
    max_retries = 2
    attempt = 0
    parsed = None
    last_error = None

    while attempt <= max_retries:
        try:
            response = _get_client_for_model(model).chat.completions.create(
                model=api_model(model),
                messages=[
                    {"role": "system", "content": _build_orchestrator_system(rules, kept_col_names)},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0,
                seed=42,  # near-determinism on OpenAI; ignored where unsupported
                **token_kwargs(model, 512),
                **extra_create_kwargs(model),
                response_format={"type": "json_object"},
            )
            raw    = response.choices[0].message.content.strip()
            parsed = json.loads(raw)
            break  # Success — exit retry loop
        except json.JSONDecodeError as json_err:
            last_error = json_err
            attempt += 1
            if attempt <= max_retries:
                logger.warning("Orchestrator (%s) JSON parse error (attempt %d/%d): %s — retrying...",
                              model.split('/')[-1], attempt, max_retries + 1, str(json_err)[:60])
            else:
                logger.warning("Orchestrator (%s) JSON parse error after %d attempts — falling back to mapper_1",
                              model.split('/')[-1], max_retries + 1)
                _fb_pv, _fb_res = _orchestrator_fallback_pv(pv1, pv2)
                return _fb_pv, {"reasoning": f"Orchestrator JSON error after {max_retries+1} attempts: {last_error}",
                           "resolution": _fb_res, "llm_s": 0.0}
        except Exception as e:
            logger.warning("Orchestrator (%s) request failed (%s) — falling back to mapper_1", model, e)
            _fb_pv, _fb_res = _orchestrator_fallback_pv(pv1, pv2)
            return _fb_pv, {"reasoning": f"Orchestrator error: {e}", "resolution": _fb_res, "llm_s": 0.0}

    # After successful retry loop, process the result
    pv     = _clean_pv(parsed.get("parsed_value", {}))
    kept_col_names_final = {col_id.split(".")[-1] for col_id, _ in kept}

    # ── FIELD-ASSIGNMENT VALIDATION: Detect and fix category confusion errors (Q038-type)
    # Check if orchestrator assigned a value to the wrong column type
    mandatory = (rules or {}).get("mandatory_entity_columns", {})
    if mandatory:
        _qa_lower = original_query.lower()

        # Derive the pathway/gene column names from schema_rules rather than
        # assuming they are literally named "pathway_name"/"gene_symbol" — keeps
        # this safety net portable to any DB whose entity columns differ.
        _pathway_col = (mandatory.get("pathway") or [None])[0]
        _gene_col    = (mandatory.get("gene")    or [None])[0]

        # If the pathway column holds a single-word gene value (like "MTOR"
        # without "pathway"), reassign it to the gene column.
        if (_pathway_col and _gene_col
                and _pathway_col in pv and isinstance(pv[_pathway_col], list)):
            new_paths = []
            new_genes = []
            for val in pv[_pathway_col]:
                val_lower = val.lower().strip()
                # Single word + no "pathway" keyword + in query = likely gene not pathway
                if (len(val.split()) == 1 and "pathway" not in val_lower and
                    val_lower in _qa_lower):
                    logger.warning(
                        "Field-assignment fix: %s is gene not pathway, reassigning %s ← %s",
                        val, _gene_col, _pathway_col
                    )
                    new_genes.append(val)
                else:
                    new_paths.append(val)
            if new_genes:
                pv[_gene_col] = new_genes
            if new_paths:
                pv[_pathway_col] = new_paths
            elif _pathway_col in pv:
                del pv[_pathway_col]

    # ── ANTI-HALLUCINATION SAFETY NET: Override if either mapper accepts hallucinated values
    # Use ONLY the original query for scope validation — the clean/expanded query may
    # contain biomedical knowledge added by the expander LLM (e.g. "EGFR" expanded from
    # "the gene targeted by gefitinib") which would falsely allow hallucinated filter values.
    import re
    _q_orig_lower = original_query.lower()
    _q_words = set(re.findall(r'\b\w+\b', _q_orig_lower))

    def _value_in_query(value_str: str) -> bool:
        """Check if value appears word-boundary-wise in the ORIGINAL user query."""
        v_lower = value_str.lower().strip()
        if v_lower in _q_words:
            return True
        # Substring match (for multi-word phrases like "breast cancer")
        if v_lower in _q_orig_lower:
            return True
        return False

    for col, val in list(pv.items()):
        # Check BOTH directions: pv1→pv2 and pv2→pv1

        # Direction 1: If pv1 has filter but pv2 rejects it
        if (col in pv1 and isinstance(pv1.get(col), list) and
            (pv2.get(col) == "requested" or col not in pv2)):
            _vals_in_query = [v for v in pv1[col] if isinstance(v, str) and _value_in_query(v)]
            if not _vals_in_query and isinstance(val, list):
                logger.warning(
                    "Anti-hallucination override (pv1→pv2): %s %s not in query → using pv2 value (%s)",
                    col, val, pv2.get(col)
                )
                if pv2.get(col) == "requested" or col in kept_col_names_final:
                    pv[col] = "requested"
                else:
                    del pv[col]

        # Direction 2: If pv2 has filter but pv1 rejects it
        elif (col in pv2 and isinstance(pv2.get(col), list) and
              (pv1.get(col) == "requested" or col not in pv1)):
            _vals_in_query = [v for v in pv2[col] if isinstance(v, str) and _value_in_query(v)]
            if not _vals_in_query and isinstance(val, list):
                logger.warning(
                    "Anti-hallucination override (pv2→pv1): %s %s not in query → using pv1 value (%s)",
                    col, val, pv1.get(col)
                )
                if pv1.get(col) == "requested" or col in kept_col_names_final:
                    pv[col] = "requested"
                else:
                    del pv[col]

    # ── GENE SYMBOL INTEGRITY CHECK (inside _orchestrate) ───────────────────────
    # HGNC gene symbols are ALL-CAPS (e.g. PIK3CA, BCL2, MTOR). If the orchestrator
    # placed a mixed/lowercase value in gene_symbol (like "mTOR", "apoptosis",
    # "PI3K-Akt") it's a category error — the value is a pathway concept, not a gene.
    # Fix: restore from pv1's valid gene_symbol; then also restore pathway_name if the
    # orchestrator erased it (put "requested") while the invalid gene value was a
    # pathway term from pv1's filter or from the original query context.
    _mandatory_ec = (rules or {}).get("mandatory_entity_columns", {})
    _gene_col_oc   = (_mandatory_ec.get("gene")    or [None])[0] if _mandatory_ec else None
    _pathway_col_oc= (_mandatory_ec.get("pathway") or [None])[0] if _mandatory_ec else None
    if _gene_col_oc and _gene_col_oc in pv and isinstance(pv[_gene_col_oc], list):
        valid_genes, invalid_genes = [], []
        for gval in pv[_gene_col_oc]:
            if isinstance(gval, str) and gval == gval.upper() and len(gval) >= 2:
                valid_genes.append(gval)
            else:
                invalid_genes.append(gval)
        if invalid_genes:
            pv1_gene_list = pv1.get(_gene_col_oc) if isinstance(pv1.get(_gene_col_oc), list) else []
            pv1_valid = [g for g in pv1_gene_list if isinstance(g, str) and g == g.upper() and len(g) >= 2]
            if pv1_valid:
                logger.warning("Gene symbol fix (orchestrator): %s → pv1 %s", invalid_genes, pv1_valid)
                pv[_gene_col_oc] = pv1_valid + valid_genes
            elif valid_genes:
                pv[_gene_col_oc] = valid_genes
            else:
                logger.warning("Gene symbol fix (orchestrator): removing invalid %s (no fallback)", invalid_genes)
                del pv[_gene_col_oc]

            # Pathway restore: if pathway_name is "requested" but pv1 had a filter
            # whose value contains the invalid gene term, restore it (normalized to
            # the shortest verbatim form).  Also detect pathway context from the query.
            if _pathway_col_oc and pv.get(_pathway_col_oc) == "requested":
                pv1_paths = pv1.get(_pathway_col_oc)
                if isinstance(pv1_paths, list):
                    for igene in invalid_genes:
                        if any(igene.lower() in pw.lower() for pw in pv1_paths):
                            pv[_pathway_col_oc] = [igene]
                            logger.warning("Pathway restore (orchestrator, pv1): %s ← [%r]",
                                           _pathway_col_oc, igene)
                            break
                else:
                    for igene in invalid_genes:
                        igene_l = igene.lower()
                        if (re.search(rf'\b{re.escape(igene_l)}\b.{{0,20}}\bpathway', _q_orig_lower, re.I) or
                                re.search(rf'\bpathway.{{0,20}}\b{re.escape(igene_l)}\b', _q_orig_lower, re.I)):
                            pv[_pathway_col_oc] = [igene]
                            logger.warning("Pathway restore (orchestrator, query): %s ← [%r]",
                                           _pathway_col_oc, igene)
                            break

    # ── QUERYABILITY FILTER: Remove fields orchestrator added that aren't queryable
    # BUT: Allow valid co-output fields even if not initially retrieved
    # (kept_col_names_final already computed above)

    # Build valid co-output columns from the actual rules in schema_rules.json
    _valid_cooutput_cols = set()
    if rules:
        co_rules = rules.get("co_output_rules", [])
        for _r in co_rules:
            # Add all require_columns from co-output rules
            _rq = (_r.get("require_columns") if _r.get("require_columns") is not None
                   else ([_r.get("require_column")] if _r.get("require_column") else []))
            reqs = _rq if isinstance(_rq, list) else ([_rq] if _rq else [])
            _valid_cooutput_cols.update(reqs)
    # Fallback: if no rules provided, use empty set (only allow explicitly retrieved)
    if not _valid_cooutput_cols:
        logger.debug("No co-output rules in schema — queryability filter will be strict")

    pv_filtered = {}
    for col, val in pv.items():
        if col in kept_col_names_final:
            # Explicitly retrieved by ANN/filter
            pv_filtered[col] = val
        elif col in _valid_cooutput_cols:
            # Valid co-output field that orchestrator can add
            # Example: drug_name when drug_smiles_iso present
            pv_filtered[col] = val
            logger.debug("Allowing co-output field from orchestrator: %s", col)
        else:
            # Not retrieved and not a valid co-output
            logger.debug("Removing non-queryable field from orchestrator output: %s", col)
    pv = pv_filtered

    # ── CO-OUTPUT ENFORCEMENT on orchestrator output (same as agreement path) ──
    # Catches: orchestrator drops disease_name but keeps omim_xref (Q002),
    #          orchestrator drops pathway_name but keeps kegg_xref (Q033, Q038).
    pv = _enforce_cooutput_rules_directly(pv, kept_col_names_final, rules)

    _t_orch1 = time.perf_counter()
    return pv, {
        "reasoning":       parsed.get("reasoning", ""),
        "field_decisions": parsed.get("field_decisions", {}),
        "raw_response":    raw,
        "llm_s":           _t_orch1 - _t_orch0,
    }


# ── Dynamic few-shot injection ────────────────────────────────────────────────

def _db_of(graph, kept) -> str:
    """Best-effort DB tag from the kept columns (all share one DB per request)."""
    for col_id, _ in kept:
        node = getattr(graph, "col_nodes", {}).get(col_id)
        if node is not None and getattr(node, "db", None):
            return node.db
    return ""


def _inject_fewshots(rules: Optional[dict], question: str, db: str,
                     stage: str) -> Optional[dict]:
    """
    Return a per-request copy of `rules` whose `few_shot_examples` are the top-K
    bank examples for (db, stage, question). If the bank has nothing (or errors),
    `rules` is returned UNCHANGED so the static few_shot_examples keep working.
    """
    if not db or not question:
        return rules
    try:
        from .fewshot_bank import select_fewshots
    except Exception:  # noqa: BLE001 — bank is optional; never break mapping
        return rules
    picked = select_fewshots(question, db, stage)
    if not picked:
        return rules
    eff = dict(rules or {})
    eff["few_shot_examples"] = [
        {"question": e["question"], "parsed_value": e["answer"], "note": e["note"]}
        for e in picked
    ]
    return eff


# ── Approval-adjective override ───────────────────────────────────────────────

# Matches "approved drugs", "FDA-approved agents", "marketed compounds", etc.
# — "approved" must precede the drug keyword (adjective pattern, not post-modifier).
_APPROVED_ADJ_RE = re.compile(
    r'\b(?:fda.?approved|approved|marketed)\s+(?:\w+\s+){0,2}(?:drug|compound|medication|agent|therapeutic)s?\b',
    re.I,
)


def _apply_approved_adjective_fix(pv: dict, question: str) -> dict:
    """If the query restricts drugs by approval status ('approved drugs targeting X'),
    override approval_status: 'requested' → ['Approved'].  Takes priority over the
    co-output rule (which always emits 'requested' alongside disease_name)."""
    if pv.get("approval_status") == "requested" and _APPROVED_ADJ_RE.search(question):
        pv = dict(pv)
        pv["approval_status"] = ["Approved"]
        logger.info("approval_status adjective override: 'requested' → ['Approved']")
    return pv


# ── Public API ────────────────────────────────────────────────────────────────

def map_values(
    question:      str,
    kept:          List[Tuple[str, float]],
    graph,
    clean_query:   Optional[str] = None,
    clean_query_2: Optional[str] = None,
    model_1:       str = LLM_MODEL,
    model_2:       str = LLM_MODEL_2,
    rules:         Optional[dict] = None,
) -> Tuple[Dict, dict]:
    """
    Map the question onto the filtered columns in interpreter parsed_value format.

    Parameters
    ----------
    question      : original user query (used as fallback + orchestrator context)
    kept          : [(col_id, score), ...] from union filter
    graph         : SchemaGraph for column descriptions
    clean_query   : MODEL_1 expander's normalized query; mapper_1 uses this
    clean_query_2 : MODEL_2 expander's normalized query; mapper_2 uses this
                    (falls back to clean_query if not provided)
    model_1       : first mapper model
    model_2       : second mapper model + orchestrator
    rules         : schema_rules dict from the DB's schema_rules.json

    Returns
    -------
    parsed_value : dict  — e.g. {"drug_name": ["imatinib"], "ic50_nM": "requested"}
    meta         : dict  — includes mapper_1_pv, mapper_2_pv, mapper_agreement,
                           orchestrator_used, reasoning
    """
    if not kept:
        return {}, {"reasoning": "no columns", "mapper_agreement": True,
                    "orchestrator_used": False}

    query_1 = clean_query   if clean_query   else question
    query_2 = clean_query_2 if clean_query_2 else query_1

    # Dynamic few-shot: swap the static few_shot_examples for the top-K mapper
    # examples retrieved from the fewshot_bank for THIS db + question. Falls back
    # to `rules` unchanged when the bank is empty/unreachable.
    mapper_rules = _inject_fewshots(rules, question, _db_of(graph, kept), "mapper")

    # ── Step 1: run both mappers in parallel, each with its own clean query ───
    _t_map0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_map_single, query_1, kept, graph, model_1, mapper_rules)
        f2 = ex.submit(_map_single, query_2, kept, graph, model_2, mapper_rules)
        pv1, meta1 = f1.result()
        pv2, meta2 = f2.result()
    _t_map1 = time.perf_counter()

    # ── Step 2: consensus check ───────────────────────────────────────────────
    kept_col_names = {c.split(".")[-1] for c, _ in kept}

    agrees = _agree(pv1, pv2)

    # Degenerate-consensus guard: two mappers trivially agreeing on a
    # filter-free parsed_value (every field is "requested", i.e. an output
    # column — nothing is a concrete list filter) is NOT reassuring when the
    # retrieval stage kept more than one candidate column. It usually means
    # BOTH mappers independently failed to extract the same filter(s) from a
    # multi-constraint question (e.g. "drugs approved to treat DISEASE by
    # targeting GENE" — a disease filter AND a gene filter, spanning 2+
    # tables). Because pv1 == pv2 the code below would normally short-circuit
    # straight to the trusted "agreement" fast path, which SKIPS the
    # orchestrator entirely — so the orchestrator's field-assignment/
    # verbatim-in-query validation (the only safety net that can recover a
    # filter neither mapper produced) never runs. Left unguarded, the planner
    # then joins on whatever's left (e.g. only a co-output rule like
    # approval_status) and silently returns a large, essentially-unfiltered
    # result instead of the correctly filtered rows or an honest empty set.
    # Route this case through Step 3 (disagreement → orchestrator) instead of
    # trusting the trivial agreement. Generic — no per-DB/per-column
    # hardcoding; applies to any DB whose dual mappers happen to agree on a
    # too-good-to-be-true "nothing to filter" result.
    if agrees and len(kept_col_names) > 1:
        _consensus_is_degenerate = pv1 and not any(
            isinstance(v, list) for v in pv1.values()
        )
        if _consensus_is_degenerate:
            logger.warning(
                "Mapper consensus is degenerate (no filter values extracted: %s) "
                "despite %d candidate columns retrieved (%s) — treating trivial "
                "agreement as unreliable and routing to the orchestrator for "
                "verification instead of trusting it blindly.",
                pv1, len(kept_col_names), sorted(kept_col_names),
            )
            agrees = False

    if agrees:
        # Enforce co-output rules directly (programmatically, not via orchestrator)
        pv1 = _enforce_cooutput_rules_directly(pv1, kept_col_names, rules)

        # Also apply other enforcement (gene/disease/rna pairing, etc.)
        pv1 = _enforce_co_outputs(pv1, kept_col_names, question=question, rules=rules)

        pv1 = _apply_approved_adjective_fix(pv1, question)
        logger.info("Mapper consensus reached with co-output enforcement: %s", pv1)
        return pv1, {
            "reasoning":        meta1.get("reasoning", ""),
            "mapper_1_pv":      pv1,
            "mapper_2_pv":      pv2,
            "mapper_agreement": True,
            "orchestrator_used": False,
            "map_reasoning_1":  meta1.get("reasoning", ""),
            "map_reasoning_2":  meta2.get("reasoning", ""),
            "map_wall_s":       _t_map1 - _t_map0,
            "mapper1_s":        meta1.get("llm_s", 0.0),
            "mapper2_s":        meta2.get("llm_s", 0.0),
            "orchestrator_s":   0.0,
        }

    # ── Step 3: disagreement → orchestrator ──────────────
    logger.info("Mapper disagreement — pv1=%s  pv2=%s — calling orchestrator", pv1, pv2)
    _t_orch0 = time.perf_counter()
    pv_final, orch_meta = _orchestrate(question, query_1, pv1, pv2, kept, graph,
                                       model=MAP_ORCHESTRATOR_MODEL, rules=rules,
                                       clean_query_2=query_2 if query_2 != query_1 else None)

    # Safety net: restore consensus values that the orchestrator incorrectly overrode.
    q_lower = question.lower()
    for col in set(list(pv1.keys()) + list(pv2.keys())):
        m1_val = pv1.get(col)
        m2_val = pv2.get(col)
        # Both mappers agreed on a specific filter list value
        if (isinstance(m1_val, list) and isinstance(m2_val, list)
                and _normalize_pv({col: m1_val}) == _normalize_pv({col: m2_val})
                and pv_final.get(col) != m1_val):
            pv_final[col] = m1_val
            logger.info("Restored consensus filter value for %s: %s", col, m1_val)
        # BOTH mappers had different filter list values but orchestrator set "requested".
        # Orchestrator should never demote a column that BOTH mappers identified as a
        # filter — pick the shorter (more conservative) value to avoid over-specificity.
        elif (isinstance(m1_val, list) and isinstance(m2_val, list)
              and (pv_final.get(col) == 'requested' or col not in pv_final)):
            shorter = m1_val if sum(len(v) for v in m1_val) <= sum(len(v) for v in m2_val) else m2_val
            pv_final[col] = shorter
            logger.info("Both mappers had filter for %s (values differed); restored shorter: %s", col, shorter)
        # Both mappers agreed on "requested" but orchestrator dropped or changed it.
        # Exception: if the orchestrator's filter value IS verbatim in the original query,
        # the orchestrator correctly caught a named filter both mappers missed — keep it.
        elif m1_val == 'requested' and m2_val == 'requested':
            if col not in pv_final:
                pv_final[col] = 'requested'
                logger.info("Restored dropped consensus 'requested' for %s", col)
            elif pv_final[col] != 'requested':
                orch_val = pv_final[col]
                orch_verbatim = (
                    isinstance(orch_val, list)
                    and all(isinstance(v, str) and v.lower() in q_lower for v in orch_val)
                )
                if not orch_verbatim:
                    pv_final[col] = 'requested'
                    logger.info("Restored orchestrator-overridden consensus 'requested' for %s", col)
        # One mapper has a filter, other has "requested" or omitted it entirely;
        # orchestrator dropped the column → prefer the filter if ALL values appear
        # verbatim in the original query (e.g. MTOR in "pathways involving MTOR").
        elif pv_final.get(col) == 'requested' or col not in pv_final:
            filter_val = None
            if isinstance(m1_val, list) and (m2_val == 'requested' or m2_val is None):
                filter_val = m1_val
            elif isinstance(m2_val, list) and (m1_val == 'requested' or m1_val is None):
                filter_val = m2_val
            if filter_val and all(v.lower() in q_lower for v in filter_val):
                pv_final[col] = filter_val
                logger.info("Preferred verbatim filter for %s: %s", col, filter_val)

    # ── GENE SYMBOL INTEGRITY CHECK (map_values orchestrator path) ───────────────
    # Catches cases where the orchestrator corrupted pv1's correct gene symbol
    # (e.g. PIK3CA→PI3K-Akt, BCL2→apoptosis) — runs AFTER the safety net so
    # Branch-2 doesn't interfere with our pathway restore.
    _mandatory_mv = (rules or {}).get("mandatory_entity_columns", {})
    _gene_col_mv    = (_mandatory_mv.get("gene")    or [None])[0] if _mandatory_mv else None
    _pathway_col_mv = (_mandatory_mv.get("pathway") or [None])[0] if _mandatory_mv else None
    if _gene_col_mv and _gene_col_mv in pv_final and isinstance(pv_final[_gene_col_mv], list):
        valid_genes_mv, invalid_genes_mv = [], []
        for gval in pv_final[_gene_col_mv]:
            if isinstance(gval, str) and gval == gval.upper() and len(gval) >= 2:
                valid_genes_mv.append(gval)
            else:
                invalid_genes_mv.append(gval)
        if invalid_genes_mv:
            pv1_gene_list_mv = pv1.get(_gene_col_mv) if isinstance(pv1.get(_gene_col_mv), list) else []
            pv1_valid_mv = [g for g in pv1_gene_list_mv
                            if isinstance(g, str) and g == g.upper() and len(g) >= 2]
            if pv1_valid_mv:
                logger.warning("Gene symbol fix (map_values): %s → pv1 %s",
                               invalid_genes_mv, pv1_valid_mv)
                pv_final[_gene_col_mv] = pv1_valid_mv + valid_genes_mv
            elif valid_genes_mv:
                pv_final[_gene_col_mv] = valid_genes_mv
            else:
                logger.warning("Gene symbol fix (map_values): removing invalid %s", invalid_genes_mv)
                del pv_final[_gene_col_mv]

            # Restore / normalize pathway_name when the invalid gene term was a pathway concept
            if _pathway_col_mv:
                cur_path = pv_final.get(_pathway_col_mv)
                pv1_paths_mv = pv1.get(_pathway_col_mv)
                if cur_path == "requested":
                    if isinstance(pv1_paths_mv, list):
                        for igene in invalid_genes_mv:
                            if any(igene.lower() in pw.lower() for pw in pv1_paths_mv):
                                pv_final[_pathway_col_mv] = [igene]
                                logger.warning("Pathway restore (map_values, pv1): %s ← [%r]",
                                               _pathway_col_mv, igene)
                                break
                    else:
                        for igene in invalid_genes_mv:
                            igene_l = igene.lower()
                            if (re.search(rf'\b{re.escape(igene_l)}\b.{{0,20}}\bpathway',
                                          q_lower, re.I) or
                                    re.search(rf'\bpathway.{{0,20}}\b{re.escape(igene_l)}\b',
                                              q_lower, re.I)):
                                pv_final[_pathway_col_mv] = [igene]
                                logger.warning("Pathway restore (map_values, query): %s ← [%r]",
                                               _pathway_col_mv, igene)
                                break
                elif isinstance(cur_path, list):
                    # Normalize: if current pathway value contains the invalid gene as a
                    # prefix (e.g. "PI3K-Akt pathway" where invalid was "PI3K-Akt"), shorten.
                    normalized_paths_mv = []
                    for igene in invalid_genes_mv:
                        for pw in list(cur_path):
                            if (igene.lower() in pw.lower()
                                    and len(pw.split()) > len(igene.split())):
                                normalized_paths_mv.append(igene)
                                logger.warning("Pathway normalize (map_values): %r → %r", pw, igene)
                            else:
                                normalized_paths_mv.append(pw)
                    if normalized_paths_mv:
                        pv_final[_pathway_col_mv] = list(dict.fromkeys(normalized_paths_mv))

    # If pathway_name is a filter list, remove any gene_symbol value
    # that is a prefix of the pathway name (e.g. "MAPK" when pathway is
    # "MAPK signaling" — orchestrator put the pathway qualifier in the wrong column).
    if (_pathway_col_mv and isinstance(pv_final.get(_pathway_col_mv), list)
            and _gene_col_mv and isinstance(pv_final.get(_gene_col_mv), list)):
        path_joined = " ".join(v.lower() for v in pv_final[_pathway_col_mv])
        clean_genes = [g for g in pv_final[_gene_col_mv]
                       if not path_joined.startswith(g.lower())]
        if len(clean_genes) < len(pv_final[_gene_col_mv]):
            logger.warning("Removed pathway-prefix gene values %s",
                           [g for g in pv_final[_gene_col_mv] if g not in clean_genes])
            if clean_genes:
                pv_final[_gene_col_mv] = clean_genes
            else:
                del pv_final[_gene_col_mv]

    pv_final = _apply_approved_adjective_fix(pv_final, question)
    pv_final = _enforce_co_outputs(pv_final, kept_col_names, question=question, rules=rules)
    _t_orch1 = time.perf_counter()
    return pv_final, {
        "reasoning":        orch_meta.get("reasoning", ""),
        "resolution":       orch_meta.get("resolution", ""),
        "mapper_1_pv":      pv1,
        "mapper_2_pv":      pv2,
        "mapper_agreement": False,
        "orchestrator_used": True,
        "map_reasoning_1":  meta1.get("reasoning", ""),
        "map_reasoning_2":  meta2.get("reasoning", ""),
        "map_wall_s":       _t_map1 - _t_map0,
        "mapper1_s":        meta1.get("llm_s", 0.0),
        "mapper2_s":        meta2.get("llm_s", 0.0),
        "orchestrator_s":   _t_orch1 - _t_orch0,
    }
