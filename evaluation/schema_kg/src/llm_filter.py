"""
LLM-based False Positive filter for Schema KG retrieval plans.

Receives candidates from the ANN retriever (hybrid_retrieval.py).
Removes columns that share vocabulary but aren't needed for the SQL query.

A column is KEEP if needed to FILTER rows (WHERE), RETURN data (SELECT),
or bridge a JOIN. A column is DROP if it fills none of those roles.

The LLM may also name columns not in the candidate list; the code resolves
these from the SchemaGraph so retrieval misses don't cause silent failures.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional, Tuple

import openai

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models

logger = logging.getLogger(__name__)

# ── Model config (models from the SSOT; keys/URLs stay env) ───────────────────

LLM_MODEL    = settings.SCHEMA_KG_FILTER_MODEL
LLM_API_KEY  = os.getenv("OPENROUTER_API_KEY",         "")
LLM_BASE_URL = os.getenv("SCHEMA_KG_FILTER_BASE_URL", "https://openrouter.ai/api/v1")

_GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
if not _GROQ_API_KEY:
    logging.warning("GROQ_API_KEY is not set; Groq-backed LLM filter calls will fail at runtime")
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


# ── Prompt ────────────────────────────────────────────────────────────────────

# Default descriptive phrase per entity type — can be overridden per-DB via
# entity_descriptions in schema_rules.json.
_DEFAULT_ENTITY_DESCRIPTIONS: dict[str, str] = {
    "drug":      "drug, compound, or medication",
    "disease":   "disease, condition, or indication",
    "gene":      "gene, protein, or molecular target",
    "pathway":   "pathway or biological process",
    "rna":       "RNA molecule",
    "phenotype": "phenotype, symptom, or clinical feature",
    "protein":   "protein or sequence",
    "variant":   "variant, mutation, or SNP",
    "go_term":   "Gene Ontology term, biological process, molecular function, or cellular component",
    "compound":  "chemical compound or small molecule",
    "tissue":    "tissue, organ, or anatomical site",
    "cell":      "cell type or cell line",
}

# ── Template for dynamic system prompt ───────────────────────────────────────

_SYSTEM_TEMPLATE = """\
You are a database schema expert for a biomedical database called {db_display_name}.
Given a user question and a list of candidate schema columns (each with a name,
description, and similarity score), decide which columns are genuinely needed
to answer the question.

A column should be KEEP if it is needed for ANY of these roles:
  1. SELECT output — the value the user wants returned
  2. WHERE filter  — a value mentioned in the question used to narrow rows
  3. JOIN bridge   — needed to connect tables even if not directly named

MANDATORY RULES — apply regardless of candidate scores:
{mandatory_rules}

CO-OUTPUT DEPENDENCIES — some columns require companions:
{co_output_rules}

If a column required by the rules above is not in the candidate list, include
it in your keep list anyway — the system will resolve it from the schema.

Self-check before responding:
{self_check}

Examples (generic patterns):
  "[ID type] for [pathway] affected by [entity]"
    → keep ID column (output) + name column (WHERE/JOIN) + entity column (WHERE)
  "which databases record [relation] of [entity]?"
    → keep datasource (output) + entity column (WHERE) + relation column (JOIN bridge)
  "[ID type] for [entity-A] associated with [entity-B]"
    → keep ID column (output) + entity-A column (WHERE/JOIN) + entity-B column (WHERE)

{db_filter_examples}When in doubt — KEEP. A false positive (extra column) is harmless; a false negative
(missing required column) breaks the query. Only DROP when you are certain the column
is irrelevant to the question.

Respond with a JSON object only — no prose, no markdown fences.
Format: {{"keep": ["col_name1", ...], "drop": ["col_name3", ...], "reasoning": "one sentence"}}
"""


def _build_filter_system(rules: dict) -> str:
    """Build LLM filter system prompt dynamically from schema_rules."""
    db_name = rules.get("db_display_name", rules.get("db_name", "the database").upper())
    mandatory = rules.get("mandatory_entity_columns", {})
    entity_descriptions = {
        **_DEFAULT_ENTITY_DESCRIPTIONS,
        **rules.get("entity_descriptions", {}),
    }

    mand_lines: list[str] = []
    check_lines: list[str] = []
    i = 1

    for entity_type, cols in mandatory.items():
        if not cols:
            continue
        primary = cols[0]
        alts    = cols[1:]
        desc    = entity_descriptions.get(entity_type, entity_type.replace("_", " "))
        label   = desc.split(",")[0].split()[0].upper()

        # If the cols list contains a "name" column alongside other columns,
        # the name bridges a join and the others are ID outputs — make this explicit.
        name_col = next((c for c in cols if "name" in c), None)
        id_cols  = [c for c in cols if c != name_col] if name_col else []

        if name_col and id_cols:
            id_str = " or ".join(id_cols)
            mand_lines.append(
                f"  • Any {desc} mentioned by name OR concept → keep {name_col}.\n"
                f"    {name_col} is NEVER redundant with {id_str} —\n"
                f"    the name column bridges the join, the ID column is the output."
            )
            check_lines.append(f"  {i}. {label} mentioned?         {name_col} in keep?")
        elif alts:
            alts_str = " / ".join(alts)
            mand_lines.append(
                f"  • Any specific {desc} named → keep {primary}\n"
                f"    (or {alts_str} if {primary} is not a candidate)."
            )
            check_lines.append(
                f"  {i}. {label} named?        {primary} or {' or '.join(alts[:2])} in keep?"
            )
        else:
            col_str = " or ".join(cols)
            mand_lines.append(f"  • Any specific {desc} named → keep {col_str}.")
            check_lines.append(f"  {i}. {label} named?        {primary} in keep?")

        i += 1

    if not mand_lines:
        mand_lines = ["  (No specific mandatory column rules for this database.)"]
        check_lines = ["  1. Ensure all entity types mentioned in the question are covered."]

    co_output_rules = rules.get("co_output_rules", [])
    co_output_lines: list[str] = []
    for rule in co_output_rules:
        trigger = rule.get("trigger_columns", [])
        require = rule.get("require_columns", [])
        if not require:
            require = rule.get("require_column", [])
        if isinstance(require, str):
            require = [require]
        reason      = rule.get("reason", "")
        behavior    = rule.get("require_behavior", "requested")
        kw          = rule.get("trigger_keywords", [])

        trigger_str = " / ".join(trigger)
        require_str = " and ".join(require)

        if behavior == "infer_from_question" and kw:
            kw_str = ", ".join(f'"{k}"' for k in kw[:6])
            co_output_lines.append(
                f"  • If you keep {trigger_str} AND the question contains one of {kw_str}:\n"
                f"    → keep {require_str} ({reason})\n"
                f"    Otherwise → DROP {require_str} (irrelevant without explicit keyword)"
            )
        elif behavior == "infer_from_question":
            co_output_lines.append(
                f"  • If you keep {trigger_str} AND the question explicitly mentions {require_str}:\n"
                f"    → keep {require_str} ({reason})\n"
                f"    Otherwise → omit (only include when directly relevant to the question)"
            )
        else:
            co_output_lines.append(
                f"  • If you keep {trigger_str} → MUST also keep {require_str}\n"
                f"    ({reason})"
            )

    if not co_output_lines:
        co_output_lines = ["  (No co-output dependencies for this database.)"]

    # ── DB-specific filter few-shot examples ─────────────────────────────────
    filter_examples = rules.get("filter_few_shot_examples", [])
    if filter_examples:
        ex_lines = ["DB-specific examples for this database:"]
        for ex in filter_examples:
            ex_lines.append(f'  Q: "{ex["question"]}"')
            ex_lines.append(f'     keep: {ex["keep"]}')
            if ex.get("drop"):
                ex_lines.append(f'     drop: {ex["drop"]}')
            if ex.get("note"):
                ex_lines.append(f'     note: {ex["note"]}')
        db_filter_examples_str = "\n".join(ex_lines) + "\n\n"
    else:
        db_filter_examples_str = ""

    return _SYSTEM_TEMPLATE.format(
        db_display_name=db_name,
        mandatory_rules="\n".join(mand_lines),
        co_output_rules="\n".join(co_output_lines),
        self_check="\n".join(check_lines),
        db_filter_examples=db_filter_examples_str,
    )


def _build_user_prompt(question: str, candidates: List[Tuple[str, float, str]]) -> str:
    lines = [f'Question: "{question}"\n', "Candidate columns:"]
    for col_id, score, desc in candidates:
        col_name = col_id.split(".")[-1]
        table    = col_id.split(".")[1] if "." in col_id else "?"
        lines.append(f"  - {col_name} (table={table}, score={score:.3f}): {desc}")
    lines.append('\nReturn JSON: {"keep": [...], "drop": [...], "reasoning": "..."}')
    return "\n".join(lines)


def _db_of(candidates, graph) -> str:
    """DB tag for this request, from the graph if available else the col_id prefix
    (col_ids are 'db.table.column')."""
    for col_id, *_ in candidates:
        node = getattr(graph, "col_nodes", {}).get(col_id) if graph is not None else None
        if node is not None and getattr(node, "db", None):
            return node.db
        if "." in col_id:
            return col_id.split(".")[0]
    return ""


def _inject_filter_fewshots(rules, question: str, db: str):
    """Swap `filter_few_shot_examples` for the top-K col_selection bank examples for
    this db+question; on empty/error return `rules` unchanged (static examples stay)."""
    if not db or not question:
        return rules
    try:
        from .fewshot_bank import select_fewshots
    except Exception:  # noqa: BLE001 — bank is optional; never break filtering
        return rules
    picked = select_fewshots(question, db, "col_selection")
    if not picked:
        return rules
    eff = dict(rules or {})
    exs = []
    for e in picked:
        ans = e["answer"] if isinstance(e["answer"], dict) else {}
        exs.append({"question": e["question"],
                    "keep": ans.get("keep", []),
                    "drop": ans.get("drop", []),
                    "note": e["note"]})
    eff["filter_few_shot_examples"] = exs
    return eff


def llm_filter_columns(
    question:   str,
    candidates: List[Tuple[str, float, str]],   # (col_id, score, description)
    model:      str            = LLM_MODEL,
    graph:      Optional[object] = None,         # SchemaGraph — lets LLM mandate missed columns
    rules:      Optional[dict] = None,           # schema_rules dict; drives dynamic system prompt
) -> Tuple[List[Tuple[str, float]], dict]:
    """
    Filter candidate columns using an LLM.

    Parameters
    ----------
    question   : the user's natural language question
    candidates : list of (col_id, score, description) from ANN search
    model      : LLM model name
    graph      : optional SchemaGraph; if provided, columns the LLM names that
                 weren't retrieved are looked up and added at score 0.0
    rules      : schema_rules dict from the DB's schema_rules.json;
                 when provided, builds a dynamic system prompt tailored to the DB

    Returns
    -------
    kept    : list of (col_id, score) that passed the filter
    meta    : {"kept": [...], "dropped": [...], "reasoning": str, "raw_response": str}
    """
    if not candidates:
        return [], {"kept": [], "dropped": [], "reasoning": "no candidates", "raw_response": "", "elapsed_s": 0.0}

    # Dynamic few-shot: swap static filter examples for top-K bank examples for
    # this db+question. Falls back to `rules` unchanged when the bank is empty/down.
    eff_rules = _inject_filter_fewshots(rules, question, _db_of(candidates, graph))
    system_prompt = _build_filter_system(eff_rules or {})
    user_prompt = _build_user_prompt(question, candidates)

    try:
        _t_start = time.perf_counter()
        response = _get_client_for_model(model).chat.completions.create(
            model=api_model(model),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0,
            seed=42,  # near-determinism on OpenAI; ignored where unsupported
            **token_kwargs(model, 1024),
            **extra_create_kwargs(model),
            response_format={"type": "json_object"},
        )
        _t_end = time.perf_counter()
        raw    = response.choices[0].message.content
        if raw is None:
            raise ValueError("LLM returned None content (possibly extended thinking or API error)")
        raw = raw.strip()
        parsed = json.loads(raw)
    except Exception as e:
        model_name = model.split('/')[-1] if '/' in model else model
        logger.warning("LLM filter failed [%s] (%s) — returning all candidates as-is", model_name, e)
        kept = [(col_id, score) for col_id, score, _ in candidates]
        return kept, {"kept": [c.split(".")[-1] for c, _, _ in candidates],
                      "dropped": [], "reasoning": f"LLM error: {e}", "raw_response": "", "elapsed_s": 0.0}

    keep_names = {n.strip().lower() for n in parsed.get("keep", [])}
    drop_names = {n.strip().lower() for n in parsed.get("drop", [])}
    reasoning  = parsed.get("reasoning", "")

    kept    = []
    dropped = []
    for col_id, score, desc in candidates:
        col_name = col_id.split(".")[-1].lower()
        if col_name in keep_names:
            kept.append((col_id, score))
        elif col_name in drop_names:
            dropped.append(col_id)
        else:
            logger.debug("LLM did not classify %s — keeping by default", col_name)
            kept.append((col_id, score))

    # If the LLM named columns that weren't retrieved, resolve them from the graph.
    # This handles the case where the retriever missed a mandatory entity column
    # (e.g. disease_name for a disease-focused question) but the LLM's self-check
    # correctly identified it as required.
    # col_nodes.values() (not queryable_columns) so that non-queryable co-output
    # columns (e.g. drug_synonyms) named by the LLM via a co_output_rule can still
    # be resolved — they are never in ANN candidates but must reach the mapper.
    if graph is not None:
        present = {col_id.split(".")[-1].lower() for col_id, _ in kept}
        present.update(col_id.split(".")[-1].lower() for col_id in dropped)
        for col_name in keep_names - present:
            for col in graph.col_nodes.values():
                if col.column.lower() == col_name:
                    kept.append((col.col_id, 0.0))
                    logger.info("LLM-mandated column resolved from graph: %s", col.col_id)
                    break

    meta = {
        "kept":         [c.split(".")[-1] for c, _ in kept],
        "dropped":      [c.split(".")[-1] for c in dropped],
        "reasoning":    reasoning,
        "raw_response": raw,
        "elapsed_s":    _t_end - _t_start,
    }
    logger.info("LLM filter: %d→%d columns kept (dropped: %s)",
                len(candidates), len(kept), meta["dropped"])
    return kept, meta
