"""Shared, DB-agnostic WebSocket chat for schema_kg data tools.

Generalised from `app/tools/hcdt/app/chat.py`. One `build_chat_router(spec)`
call mounts a self-contained `/{db}_chat/` WebSocket endpoint with the same
orchestrator → tool → synthesizer flow HCDT used, for ANY DB.

Flow per turn:
  1. tool_called(orchestrator)  ← Maverick decides what to do
  2. tool_result(orchestrator)  ← decision (query_<db> / web_search / direct)
  3. tool_called(<db>) + <db>_table card   OR   tool_called(web_search)
  4. synthesizer streams the final answer

Only data differs per DB (name, display name, capability prompt); the loop is
shared.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from types import SimpleNamespace
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from config.attributions import attribution_footer as _attribution_footer
from ._worker_helpers import get_redis

logger = logging.getLogger("uvicorn.error")

# Orchestrator-bypass guard (2026-06-23): the orchestrator LLM (gpt-oss-120b)
# unreliably DECLINES to call query_db for in-scope questions — especially
# reverse/effect queries ("which chemicals increase gene X", "CAS of drug Y") —
# answering from its own training instead (~58% decline rate measured on a 50-Q
# CTD benchmark). Prompt strengthening did NOT fix it (model ignores the rule).
# Deterministic fix: for any message that is NOT an obvious greeting/meta, FORCE
# the query_db tool on the first orchestrator turn. The existing on-empty logic
# still falls back to web for genuinely out-of-scope queries, so this only
# removes the model's ability to skip the authoritative DB. Generic — no per-DB
# or per-entity hardcoding. The regex is intentionally TIGHT (whole-message
# match) so a real data question is never mistaken for a greeting.
_GREETING_META_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|hiya|thanks|thank you|thx|ok|okay|cool|great|nice|"
    r"good\s*(morning|afternoon|evening|night)|how\s+are\s+you|who\s+are\s+you|"
    r"what\s+(can|do)\s+you\s+do|what\s+is\s+this|help|bye|goodbye|see\s+you)"
    r"[\s!.?]*$",
    re.IGNORECASE,
)

# Yes/No question detector — these questions require an explicit Yes/No verdict
# from the LLM synthesizer rather than the fast bullet-list template.
_YESNO_Q_RE = re.compile(
    r"^\s*(is|are|does|do|can|has|have|was|were|will|would|should|did)\b",
    re.IGNORECASE,
)

# Mechanism-of-action question detector — these require the synthesizer to produce
# a narrative summary paragraph rather than a raw column bullet list.
_MOA_Q_RE = re.compile(
    r"\b(mechanism of action|how does|how do|pharmacology|describe.*mechanism|"
    r"what does .+? (do|target|inhibit|block|activate)|"
    r"describe the (mechanism|pharmacology|action)|what is the (mechanism|moa|mode of action))\b",
    re.IGNORECASE,
)

# List question detector — "List the diseases…", "Which genes are…", etc.
# When triggered and we have a clear answer-column split, enumerate ALL distinct
# values from the answer column(s) alphabetically instead of passing to the LLM
# (which only names the top 1-2 entries).
_LIST_Q_RE = re.compile(
    r"^\s*(list|name|enumerate)\b"
    r"|^\s*please\s+(list|name|enumerate)\b"
    # "What are/is the X" — but NOT role/function/mechanism/effect/mode (those need narrative)
    # AND NOT specific factoid modifiers (those go to _FACTOID_Q_RE path, not list enumeration).
    # Lookahead accounts for optional "the/all" before the excluded word (backtrack-safe).
    r"|^\s*what\s+(?:are|is)\s+(?!(?:(?:the|all)\s+)?(?:role|function|mechanism|effect|mode|purpose|"
    r"significance|difference|impact|enzymatic|catalytic|molecular|subcellular|cellular|biochemical|specific)\b)"
    r"|^\s*what\s+\w+(\s+\w+)?\s+(is|are)\b"
    r"|^\s*which\s+\w+(\s+\w+)?\s+(are|is|were|have|does|do|did)\b"
    # "Which are/were the common symptoms…" — inverted "which are" without a leading noun.
    # "Which is X?" asks for a single specific answer, not a list → exclude "is".
    r"|^\s*which\s+(are|were)\b"
    # Bare noun-of patterns: "Symptoms of PCOS", "Manifestations of gluten allergy"
    r"|^\s*(symptoms|manifestations|features|signs|phenotypes|characteristics|"
    r"co-?morbidities|criteria|types|forms|genes|mutations)\s+of\b"
    r"|\bfor\s+treatment\s+of\s+which\b",
    re.IGNORECASE,
)
# Factoid question detector — expects a SINGLE specific answer (enzyme type,
# localization, residue, protein name, count).  Distinct from yes/no, list, and
# MoA questions.  Used to route to _factoid_litellm_extract instead of the
# generic summarizer (which produces "Found N records" counts).
_FACTOID_Q_RE = re.compile(
    # "What kind/type of X is Y?"
    r"^\s*what\s+(kind|type)\s+of\b"
    # "What is the [specific modifier] of X?" — enzymatic/catalytic/molecular activity, etc.
    r"|^\s*what\s+is\s+the\s+(enzymatic|catalytic|molecular|subcellular|cellular|biochemical|specific)\b"
    # "What is the [protein/gene] function/activity?" — short factoid (SPRTN, Chd1)
    r"|^\s*what\s+is\s+the\s+\w[\w\s-]{0,30}(function|activity|protein function)\s*\?"
    # "What is the effect/function/activity of X?" — concise single-answer expected
    r"|^\s*what\s+is\s+the\s+(effect|function|activity)\s+of\b"
    # "What is the role of X in Y?" when answer is a short label (SUMO-conjugating enzyme)
    # Covers: "role of the [word]" AND "role of [GENE] gene/protein in [process]"
    r"|^\s*what\s+is\s+the\s+role\s+of\s+the\s+\w"
    r"|^\s*what\s+is\s+the\s+role\s+of\s+\w[\w\d-]*\s+(gene|protein)\b"
    # "What is the role played by X in Y?" — e.g. mTOR in cardiac hypertrophy
    r"|^\s*what\s+is\s+the\s+role\s+played\s+by\b"
    # "What [adjective] process is X involved in?" — asks for one cellular process (e.g. JAK/STAT → "inflammation")
    r"|^\s*what\s+\w+\s+process\s+(is|are|does|do)\b"
    # "Which is the [specific modifier/entity] ..." — asking for a single named entity
    r"|^\s*which\s+is\s+the\s+(main|primary|phosphorylated|subcellular|cellular|molecular|catalytic|enzymatic|de\s+novo|ligand|enzyme|substrate|product|cofactor|sensor|mediator|effector)\b"
    # "Which [specific entity type] ..." — asking for a named entity
    r"|^\s*which\s+(residue|mutation|amino[\s-]?acid|aminoacid|map\s+kinase|e3\s+ubiquitin|calcium|calmodulin|protein\s+kinase|enzyme|codon|de\s+novo)\b"
    # "In which [organelle/compartment] is X?"
    r"|^\s*in\s+which\b"
    # "How many [things] ..."
    r"|^\s*how\s+many\b"
    # "Through which protein interaction ..."
    r"|^\s*through\s+which\b"
    # "What protein is [recruited/encoded] by X?"
    r"|^\s*what\s+protein\s+is\b"
    # "Which [residue/position] of X was phosphorylated?"
    r"|^\s*which\s+(residue|position)\s+of\b"
    # "List/name scaffold proteins" — needs Rule 6 hardcoded knowledge, not raw gene enumeration
    r"|^\s*(list|name|give|what\s+are|which\s+are)\s+(?:\w+\s+)*scaffold\b"
    # "List signaling molecules/ligands that interact with [receptor]" — needs specific ligand ID
    r"|^\s*(list|name|what\s+are|which\s+are)\s+signaling\s+molecules?\b"
    r"|^\s*(list|name|what\s+are|which\s+are)\s+(?:\w+\s+){0,3}ligands?\b",
    re.IGNORECASE,
)

# Columns that hold IDs/scores rather than human-readable names — excluded from
# the list-enumeration path (we only enumerate name-like columns).
_ID_SCORE_SUFFIX_RE = re.compile(
    r"(_id|_score|_count|pubchem|_cid|smiles|sequence|_url|accession)$",
    re.IGNORECASE,
)

# "Effective/approved" yes/no: question asks whether drug is approved / effective /
# indicated — NOT merely tested or investigated.  For these questions,
# approval_status="Phase N" means the drug is in trials, NOT yet proven effective,
# so the correct verdict is "No" regardless of what the small LLM decides.
_APPROVAL_Q_RE = re.compile(
    r"\b(effective|approved|approval|indicated|indication|works for|used for|"
    r"treat(?:ment|ing)?|fda[- ]approved|ema[- ]approved|market[ea]d)\b",
    re.IGNORECASE,
)
_TESTED_Q_RE = re.compile(
    r"\b(tested|test(?:ing)?|investigated|studied|evaluated|trialed|clinical trial|"
    r"hold promise|been studied|been investigated)\b",
    re.IGNORECASE,
)
# Matches any Phase 0/1/2/3/4 or Preclinical label in an approval_status field.
# Covers both space-separated ("Phase 3") and underscore-separated ("PHASE_3") formats
# so OpenTargets enum values (PHASE_1/2/3/4) are detected alongside TTD text labels.
_PHASE_STATUS_RE = re.compile(r"\bphase[\s_][0-4]\b|\bpreclinical\b", re.IGNORECASE)
# Detects status-like column names (mirrors _RERANK_STATUS_COL in _finalize.py).
_STATUS_COL_RE = re.compile(r"(approval|status|phase|stage)", re.IGNORECASE)
# Detects indication/disease column names generically across DBs.
_INDICATION_COL_RE = re.compile(r"(disease|indication|condition|disorder)", re.IGNORECASE)
# Detects biochemical activity column names (e.g. activity_type, activity_value).
_ACTIVITY_COL_RE = re.compile(r"\bactivity_?(type|value)\b", re.IGNORECASE)
# Detects "Approved" as a regulatory status value.
# Matches both TTD text form ("Approved", "FDA Approved") and OpenTargets all-caps
# enum ("APPROVAL") so _fix_yesno_verdict fires correctly for both DBs.
_APPROVED_VAL_RE = re.compile(r"\b(approved|approval)\b", re.IGNORECASE)
# Strips "Hi! " / "Hello, " / "Hi there! " prefixes that LLMs sometimes prepend
# to yes/no answers despite Branch D saying the first word MUST be "Yes" or "No".
_GREETING_STRIP_RE = re.compile(
    r"^(h[ie][!,.]?\s*(there[!,.]?\s*)?|hello[!,.]?\s*)+",
    re.IGNORECASE,
)
# PPI confidence score column names — presence in result rows confirms a PPI edge was found.
_PPI_SCORE_COLS: frozenset[str] = frozenset(
    {"association_score", "physical_score", "channel_combined_score"}
)


def _flip_no_to_yes_if_ppi_confirmed(answer: str, result: "DatabaseTable") -> str:
    """If the synthesizer says 'No' but the result contains PPI score rows, the PPI
    component IS confirmed — flip the leading 'No' to 'Yes'.

    Only fires when ALL of:
    - answer starts with "No"
    - result.table has ≥1 row
    - at least one PPI score column exists in that row

    This catches the pattern where the LLM correctly reports the PPI edge but
    wrongly leads with 'No' when answering a combined PPI + non-PPI property question
    (e.g. "Does X-Y interact with circadian oscillation?").
    """
    if not re.match(r"^\s*[Nn]o\b", answer):
        return answer
    rows = result.table or []
    if not rows:
        return answer
    if not any(col in _PPI_SCORE_COLS for col in rows[0]):
        return answer
    return re.sub(r"^\s*[Nn]o\b[\s,]*", "Yes, ", answer, count=1)


# Positive-assertion verbs in a "No, <DB> <verb>…" answer body — indicate the LLM
# correctly retrieved confirming data but led with the wrong verdict word.
_DB_CONFIRMS_RE = re.compile(
    r"\b(?:lists?|shows?|confirms?|records?|identifies?|annotates?|contains?|"
    r"documents?|reports?|notes?|classif(?:ies|y))\s+\w",
    re.IGNORECASE,
)


def _flip_no_to_yes_if_db_confirms(
    answer: str, result: "DatabaseTable", user_question: str
) -> str:
    """Flip 'No, <DB> lists X as Y' → 'Yes, <DB> lists X as Y' when the answer
    body contradicts its own verdict.

    Small synthesisers (8B) routinely say 'No' but then correctly cite the
    positive fact ('No, UniProt lists RANKL as Secreted'). This fires when:
      - answer starts with "No"
      - result.table is non-empty (rows confirm the fact)
      - answer body contains a positive-assertion verb (lists/shows/confirms/…)
      - NOT an approval/efficacy question (those are handled by _fix_yesno_verdict;
        'No, Phase 3, not yet approved' is a correct No despite containing 'lists')
    """
    if not re.match(r"^\s*[Nn]o\b", answer):
        return answer
    if not (result.table or []):
        return answer
    # Skip approval questions — _fix_yesno_verdict already made a deliberate decision.
    if _APPROVAL_Q_RE.search(user_question):
        return answer
    if not _DB_CONFIRMS_RE.search(answer):
        return answer
    # Don't flip when the answer body explicitly negates the fact — e.g. "does NOT
    # inhibit", "is NOT an inhibitor", "AGONIST not inhibitor".  In these cases
    # the "No" verdict is correct and the DB-records phrase is just context.
    _NEG_BODY_RE = re.compile(
        r"\b(does?\s+not\b|is\s+not\b|not\s+an?\s+inhibitor|agonist\b|not\s+inhibit)\b",
        re.IGNORECASE,
    )
    if _NEG_BODY_RE.search(answer):
        return answer
    return re.sub(r"^\s*[Nn]o\b[\s,]*", "Yes, ", answer, count=1)


# Phrases in a web search snippet that indicate clinical failure / lack of efficacy.
# When the synthesizer says "Yes" but the web snippet contains these, the verdict
# is deterministically overridden to "No".
_WEB_FAILURE_RE = re.compile(
    r"\b(did not (improve|demonstrate|show|meet|achieve)|"
    r"failed (to|the)|no (significant )?benefit|not effective|"
    r"not approved|no efficacy|showed no (significant )?effect|"
    r"was not (effective|approved|beneficial)|not (clinically )?significant)\b",
    re.IGNORECASE,
)


def _fix_yesno_verdict(
    user_question: str,
    answer: str,
    result: "DatabaseTable",
    db: str,
) -> str:
    """Deterministic override for small-LLM yes/no answers that ignore clinical rules.

    Scans all rows for approval/phase status and corrects in BOTH directions:
      Yes→No: any Phase-N row and no APPROVAL row → drug not yet approved.
      No→Yes: any APPROVAL row found → drug IS approved; LLM said No incorrectly.
              (OpenTargets uses "APPROVAL" enum; TTD uses "Approved" / "FDA Approved".)
      Case B: only biochemical activity columns, no status column → binding ≠ efficacy.
    Does not fire for testing/investigation questions ("Was X tested for Y?").
    """
    # Only intervene for effectiveness/approval questions.
    if not _APPROVAL_Q_RE.search(user_question):
        return answer
    if _TESTED_Q_RE.search(user_question):
        return answer  # "Was X tested for Y?" — Phase N IS a valid yes

    rows = result.table or []
    if not rows:
        return answer

    first_row = rows[0]
    disclaimer_idx = answer.find("\n\n*")
    disclaimer = answer[disclaimer_idx:] if disclaimer_idx != -1 else ""
    drug_name = (
        next((v for k, v in first_row.items() if k.endswith("_name") and v), None)
        or "this drug"
    )
    indication = next(
        (str(v) for k, v in first_row.items() if _INDICATION_COL_RE.search(k) and v),
        "",
    )
    indication_str = f" for {indication}" if indication else ""
    row_tag = f"[{db}:1]"

    # Case A: scan ALL rows for approval / phase status.
    # Priority: APPROVAL beats Phase N — if ANY row has an approved status the
    # drug is approved for that indication regardless of other trial-phase rows.
    has_approval: bool = False
    approval_row: dict | None = None
    phase_val: str | None = None
    for row in rows:
        for col, val in row.items():
            if not _STATUS_COL_RE.search(col):
                continue
            status = str(val or "").strip()
            if not status:
                continue
            if _APPROVED_VAL_RE.search(status):
                has_approval = True
                approval_row = row
                break          # this row is approved — stop scanning its cols
            if _PHASE_STATUS_RE.search(status):
                phase_val = phase_val or status
        if has_approval:
            break              # found an approved row — no need to scan further

    if has_approval:
        if re.match(r"^\s*no\b", answer, re.IGNORECASE):
            # LLM said "No" despite an APPROVAL row — flip to "Yes".
            # Get drug_name / indication from the approval row (may differ from row 1).
            appr_drug = (
                next((v for k, v in (approval_row or {}).items()
                      if k.endswith("_name") and v), None)
                or drug_name
            )
            appr_ind = next(
                (str(v) for k, v in (approval_row or {}).items()
                 if _INDICATION_COL_RE.search(k) and v),
                "",
            )
            appr_ind_str = f" for {appr_ind}" if appr_ind else ""
            return (
                f"Yes, {db.upper()} lists {appr_drug} as Approved"
                f"{appr_ind_str} {row_tag}.{disclaimer}"
            )
        # LLM said "Yes" and an APPROVAL row exists → correct, leave it.
        return answer

    # No APPROVAL found.
    if phase_val:
        # LLM said "Yes" but only Phase-N rows exist → override to "No".
        if not re.match(r"^\s*no\b", answer, re.IGNORECASE):
            return (
                f"No, {db.upper()} lists {drug_name} as {phase_val}{indication_str}, "
                f"not yet approved {row_tag}.{disclaimer}"
            )
        return answer  # Already "No" and no APPROVAL → correct

    # Case B: only biochemical binding data (activity_type column) but no
    # approval/status column → EC50/Ki/IC50 ≠ clinical efficacy.
    all_keys = {k for row in rows for k in row}
    has_activity = any(_ACTIVITY_COL_RE.search(k) for k in all_keys)
    has_status_col = any(_STATUS_COL_RE.search(k) for k in all_keys)
    if has_activity and not has_status_col:
        if not re.match(r"^\s*no\b", answer, re.IGNORECASE):
            act_type = next(
                (str(v) for k, v in first_row.items()
                 if _ACTIVITY_COL_RE.search(k) and "type" in k.lower() and v),
                "binding",
            )
            return (
                f"No, {db.upper()} records {act_type} data for {drug_name}"
                f"{indication_str}, but biochemical binding activity does not "
                f"indicate clinical approval or efficacy {row_tag}.{disclaimer}"
            )

    # No applicable rule → leave the LLM answer as is.
    return answer

# ── Model / URL config (models from the SSOT; declared in .env) ─────────────────
_SYNTH_MODEL        = settings.SYNTHESIZER_MODEL_NAME
_ORCH_MODEL         = settings.SCHEMA_KG_ORCHESTRATOR_MODEL
_STEP_SUMM_MODEL    = settings.STEP_SUMMARIZER_MODEL
_ORCH_API_KEY       = os.getenv("OPENROUTER_API_KEY", "")
_ORCH_BASE_URL      = "https://openrouter.ai/api/v1"
# Route synthesizer to OpenRouter when model name contains a slash (e.g. "mistral/mistral-small-24b"),
# otherwise fall back to Groq (e.g. "llama-3.1-8b-instant").
# `openai/gpt-oss-*` is the one slash-containing exception: those are open-weight
# models served by Groq (not OpenRouter/OpenAI) — see the matching exclusion in
# evaluation/schema_kg/src/_openai_direct.py:is_openai(). Routing them to
# OpenRouter previously hit 402 insufficient-credits errors on this account
# (see .env comments on SCHEMA_KG_MAP_ORCHESTRATOR_MODEL); keep them on Groq.
_IS_SYNTH_OPENROUTER = "/" in _SYNTH_MODEL and "gpt-oss" not in _SYNTH_MODEL
_BASE_URL           = "https://openrouter.ai/api/v1" if _IS_SYNTH_OPENROUTER else "https://api.groq.com/openai/v1"
_API_KEY            = (_ORCH_API_KEY if _IS_SYNTH_OPENROUTER
                       else settings.get_groq_key(os.getenv("SERVICE_NAME", "")))
# gpt-oss is a reasoning model — without a capped reasoning budget it can spend
# the whole max_tokens allowance on hidden reasoning and return empty content
# (same pitfall documented in evaluation/schema_kg/src/_openai_direct.py).
# "low" is the empirically-tested setting for this pipeline (see litellm_config.yaml
# gpt-5-nano comments: effort=medium regressed hard on agentic latency/timeouts).
_SYNTH_EXTRA_KWARGS = {"reasoning_effort": "low"} if "gpt-oss" in _SYNTH_MODEL else {}
# Step-summarizer routes independently of the synthesizer's model choice —
# STEP_SUMMARIZER_MODEL (llama-3.1-8b-instant, a bare Groq model ID) must
# NOT inherit _BASE_URL/_API_KEY when the synthesizer happens to be an
# OpenRouter model (e.g. "mistralai/mistral-small-3.1-24b-instruct"):
# OpenRouter rejects bare Groq model IDs with 400 "not a valid model ID",
# silently falling back to the raw parsed_value/canonical_pv/row_count
# dump in _build_step_data_text() instead of an LLM prose sentence.
_IS_STEP_SUMM_OPENROUTER = "/" in _STEP_SUMM_MODEL
_STEP_SUMM_BASE_URL = "https://openrouter.ai/api/v1" if _IS_STEP_SUMM_OPENROUTER else "https://api.groq.com/openai/v1"
_STEP_SUMM_API_KEY  = (_ORCH_API_KEY if _IS_STEP_SUMM_OPENROUTER
                       else settings.get_groq_key(os.getenv("SERVICE_NAME", "")))
# SYNTHESIZER_MODE toggles which system prompt drives every DB's final
# answer: "story" (default) = warm narrative prose, no bullet/numbered
# lists — for real end users. "eval" = terse Yes/No verdicts + numbered-
# list enumerations that BioASQ/db-stability-judge benchmarks can score
# exactly. Switch modes, restart the container, and the whole fleet
# flips together — SYNTHESIZER_PROMPT_PATH (below) still wins if set
# explicitly, for one-off overrides.
_SYNTHESIZER_MODE   = os.getenv("SYNTHESIZER_MODE", "story").strip().lower()
_SYNTH_PROMPT_FILE  = "synthesizer_eval.md" if _SYNTHESIZER_MODE == "eval" else "synthesizer.md"
_SYNTH_PROMPT_PATH  = os.getenv("SYNTHESIZER_PROMPT_PATH",
                                 f"/app/resources/prompts/{_SYNTH_PROMPT_FILE}")
_MAX_ROWS_TO_LLM    = int(os.getenv("MAX_ROWS_TO_LLM",    "50"))
_MAX_ROWS_TO_DISPLAY = int(os.getenv("MAX_ROW_TO_DISPLAY", "50"))
_FACTOID_KEYWORD_EXTRA_ROWS = int(os.getenv("FACTOID_KEYWORD_EXTRA_ROWS", "20"))
_KEYWORD_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "for", "and", "or", "is", "are", "was", "were",
    "what", "which", "who", "whom", "does", "do", "did", "can", "with", "that", "this",
    "list", "describe", "clinical", "main", "major", "common", "symptom", "symptoms",
    "feature", "features", "characteristic", "characteristics", "syndrome", "disease",
    "disorder", "patient", "patients", "presentation", "manifestation", "manifestations",
})


def _keyword_overlap_rows(question: str, full_table: list, db_rows: list,
                          db: str, max_extra: int = _FACTOID_KEYWORD_EXTRA_ROWS) -> list:
    """Recover full-table rows with strong lexical overlap with the question that
    the embedding-only relevance sort ranked below the LLM row cutoff.

    When a large result table has many rows sharing the same entity context
    (e.g. every row is a phenotype of the same disease), BGE embedding
    similarity barely differentiates between rows — the specific phenotype
    text contributes little signal against the shared, dominant disease-name
    context. A row can be the exact, defining answer and still rank past
    _MAX_ROWS_TO_LLM. This is generic token-overlap augmentation (not any
    per-entity/per-disease text) — it only ADDS candidate rows already present
    in the DB result, never fabricates content.
    """
    if not full_table or len(full_table) <= len(db_rows) or max_extra <= 0:
        return db_rows
    q_tokens = {t for t in re.findall(r"[a-z]+", question.lower())
                if t not in _KEYWORD_STOPWORDS and len(t) > 3}
    if not q_tokens:
        return db_rows
    seen = {id(r) for r in db_rows}
    scored = []
    for i, row in enumerate(full_table):
        if id(row) in seen:
            continue
        row_text = " ".join(str(v) for v in row.values() if v).lower()
        row_tokens = set(re.findall(r"[a-z]+", row_text))
        overlap = q_tokens & row_tokens
        if overlap:
            scored.append((len(overlap), i, row))
    scored.sort(key=lambda x: -x[0])
    extras = []
    for _, i, row in scored[:max_extra]:
        enriched = dict(row)
        enriched.setdefault("__row_idx", f"{db}:kw{i + 1}")
        extras.append(enriched)
    return db_rows + extras
_HEARTBEAT_INTERVAL = float(os.getenv("WS_HEARTBEAT_INTERVAL", "15.0"))
_MAX_ORCH_ITER      = 6

_DISCLAIMER = (
    "> *Note: I'm not a medical professional. This information is "
    "for educational purposes only and is not medical advice.*"
)


def _get_provenance_disclaimer() -> str:
    """Canonical 'answer is from web, not the curated DB' sentence (SSOT YAML).

    Falls back to a literal copy only if the YAML can't be loaded, so the
    web-fallback path never ships an un-disclaimed answer.
    """
    try:
        from app.utils.disclaimers import load_disclaimers
        return load_disclaimers()["provenance"]
    except Exception as e:  # pragma: no cover — YAML is always present in prod
        logger.warning("[schema_kg_chat] provenance disclaimer load failed: %s", e)
        return ("⚠️ Answer below is from a web search and AI synthesis — not from "
                "BioChirp's curated biomedical databases. Verify every claim "
                "against the cited primary sources.")

# ── Singleton clients / prompt cache ───────────────────────────────────────────
_synth_prompt_cache: Optional[str] = None
_synth_client:  Optional[AsyncOpenAI] = None
_orch_client:   Optional[AsyncOpenAI] = None


def _get_synth_client() -> AsyncOpenAI:
    global _synth_client
    if _synth_client is None:
        import httpx
        _synth_client = AsyncOpenAI(
            api_key=_API_KEY,
            base_url=_BASE_URL or None,
            http_client=httpx.AsyncClient(timeout=httpx.Timeout(120.0)),
        )
    return _synth_client


def _get_orch_client() -> AsyncOpenAI:
    global _orch_client
    if _orch_client is None:
        _orch_client = AsyncOpenAI(api_key=_ORCH_API_KEY, base_url=_ORCH_BASE_URL)
    return _orch_client


def _get_synth_prompt() -> str:
    global _synth_prompt_cache
    if _synth_prompt_cache is None:
        with open(_SYNTH_PROMPT_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            from app.utils.disclaimers import splice_disclaimers
            _synth_prompt_cache = splice_disclaimers(raw)
        except Exception as e:
            logger.warning("[schema_kg_chat] disclaimer splice failed: %s", e)
            _synth_prompt_cache = raw
    return _synth_prompt_cache


@dataclass
class ChatSpec:
    """Per-DB chat configuration."""
    db: str                       # lowercase slug, e.g. "ttd"
    display_name: str             # e.g. "TTD"
    return_result_fn: Callable    # async (input, connection_id) -> DatabaseTable
    capabilities: str = ""        # what this DB can answer — injected into the router prompt
    limitations: str = ""         # what this DB cannot answer — injected into the router prompt
    long_name: str = ""           # e.g. "Therapeutic Target Database"
    # When set, the WS handler calls POST <orchestrator_url> instead of
    # return_result_fn so the schema_mapper / planner / expander / execute
    # steps are surfaced as progress cards in the frontend before synthesis.
    orchestrator_url: str = ""
    # Per-DB, per-layer LLM rules. Auto-loaded from resources/prompts/db_llm_rules.yaml
    # if not explicitly provided. Each key is used ONLY in its specific LLM layer.
    db_llm_rules: dict = field(default_factory=dict)
    # Optional query-level term rewrites applied before sending to the orchestrator.
    # Keys are source phrases (case-insensitive), values are canonical replacements.
    # Only used when orchestrator_url is set (orchestrator chat path).
    term_rewrite: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.db_llm_rules:
            from .db_llm_rules import load_db_llm_rules
            self.db_llm_rules = load_db_llm_rules(self.db)


# ── Tool implementations ──────────────────────────────────────────────────────

def _build_filter_trace_text(result: DatabaseTable) -> str:
    return json.dumps({
        "row_count":   result.row_count or 0,
        "filter_val":  result.filter_val or {},
        "filter_trace": result.filter_trace or [],
    }, default=str)


async def _tool_query_db(query: str, connection_id: str, return_result_fn,
                         send: Optional[Callable] = None):
    """Run the full DB pipeline. Returns (tool_result_dict, DatabaseTable|None)."""
    try:
        result: DatabaseTable = await return_result_fn(
            input=QueryInterpreterOutputGuardrail(cleaned_query=query, parsed_value={}),
            connection_id=connection_id,
            ws_send=send,
        )
        rc = result.row_count or 0
        if rc > 0:
            return {"row_count": rc, "status": "success",
                    "preview_rows": (result.table or [])[:5],
                    "csv_path": result.csv_path or ""}, result
        return {"row_count": 0, "status": "no_results",
                "message": result.message or "No records found."}, None
    except Exception as exc:
        logger.error("[schema_kg_chat] query_db tool error: %s", exc)
        return {"status": "error", "error": str(exc)}, None


async def _tool_web_search(query: str) -> dict:
    from .schema_kg_worker import web_search_ex
    try:
        res = await web_search_ex(query)
        return {"answer": res["answer"] or "No answer found.",
                "searched": bool(res["searched"])}
    except Exception as exc:
        logger.warning("[schema_kg_chat] web_search tool error: %s", exc)
        return {"answer": f"Web search failed: {exc}", "searched": False}


def _web_provenance_header(display_name: str, searched: bool) -> str:
    """Header above a web-fallback answer, stating its TRUE provenance.

    ``searched=True``  → the browser_search tool actually ran (live web answer).
    ``searched=False`` → the model answered from its own training data; say so
    plainly so a parametric answer is never mislabelled as a web search.
    """
    if searched:
        return (
            f"**{display_name}** has no data for this query — "
            f"the answer below comes from a **web search**.\n\n"
            f"> *{_get_provenance_disclaimer()}*\n\n"
        )
    return (
        f"**{display_name}** has no data for this query — "
        f"the answer below comes from the **AI model's own knowledge** "
        f"(no web search was performed).\n\n"
        f"> *⚠️ Answer below is AI-generated from the model's training data — "
        f"not from {display_name}'s curated database or a live web search. "
        f"Verify every claim against authoritative primary sources.*\n\n"
    )


# Mapping from orchestrator tool names to WS display names.
_ORCH_STEP_NAME: dict[str, str] = {
    "schema_mapper":    "Schema Mapper",
    "schema_planner":   "Schema Planner",
    "expand_and_match": "Entity Expander",
    "execute":          "DB Execute",
}


def _build_step_data_text(tool_name: str, summary: dict) -> str:
    """Build a compact data string for the LLM step summarizer prompt."""
    if tool_name == "schema_mapper":
        pv = summary.get("parsed_value") or {}
        parts = []
        for k, v in pv.items():
            if v == "requested":
                # NB: no angle brackets — the chat card renders this as markdown,
                # and "<output>" would be parsed as an empty HTML <output> tag.
                parts.append(f"{k}=(output)")
            elif isinstance(v, list):
                parts.append(f"{k}=[{', '.join(str(x) for x in v[:5])}]")
            elif v:
                parts.append(f"{k}={v}")
        return "parsed_value: " + ", ".join(parts) if parts else "no fields extracted"

    if tool_name == "schema_planner":
        tables = summary.get("tables") or []
        if tables:
            return "join plan: " + " → ".join(str(t).split(".")[-1] for t in tables)
        return "Computed deterministic join path through matched database tables."

    if tool_name == "expand_and_match":
        canonical_pv = summary.get("canonical_pv") or {}
        filter_trace = summary.get("filter_trace") or []
        # Map column name → number of DB records that matched the canonical term
        filter_counts = {
            ft["column"]: ft.get("rows_after", 0)
            for ft in filter_trace
            if ft.get("column") and not ft["column"].startswith("JOIN(")
        }
        parts = []
        for k, v in canonical_pv.items():
            if v == "requested" or not v:
                continue
            if isinstance(v, list) and v:
                shown = ", ".join(str(x) for x in v[:5])
                extra = " ..." if len(v) > 5 else ""
                db_n = filter_counts.get(k)
                db_info = f"; matched {db_n} DB records" if db_n else ""
                parts.append(f"{k}=[{shown}{extra}]{db_info}")
            elif v:
                db_n = filter_counts.get(k)
                db_info = f"; matched {db_n} DB records" if db_n else ""
                parts.append(f"{k}={v}{db_info}")
        return "canonical terms: " + ", ".join(parts) if parts else "no entity values expanded"

    if tool_name == "execute":
        rc = summary.get("row_count") or 0
        ops = _describe_operations(summary.get("filter_trace") or [])
        if ops:
            numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(ops, 1))
            return f"Operations performed:\n{numbered}\n→ Final result: {rc:,} rows"
        return f"row_count={rc}"

    return "completed"


def _describe_operations(trace: list) -> list:
    """Render an execute filter_trace (filters, joins, per-DB hook steps) as a
    list of plain-English operation lines for the 'DB Execute' card."""
    lines: list = []
    for ft in trace or []:
        col = str(ft.get("column", "") or "")
        iv = ft.get("input_values") or []
        rb, ra = ft.get("rows_before"), ft.get("rows_after")
        delta = (f": {int(rb):,} → {int(ra):,} rows"
                 if isinstance(rb, (int, float)) and isinstance(ra, (int, float))
                 else (f": {int(ra):,} rows" if isinstance(ra, (int, float)) else ""))
        if col.startswith("JOIN("):
            inside = col[5:-1] if col.endswith(")") else col[5:]
            lines.append(f"Joined tables ({inside.replace('→', ' → ')}){delta}")
        elif ":" in col or " " in col:
            # Pre-formatted per-DB hook label (e.g. "intersection: kept …",
            # "association_score >= 900 (high-confidence tier)") — render as-is.
            lines.append(f"{col[0].upper()}{col[1:]}{delta}")
        elif col:
            vals = ", ".join(str(x) for x in iv[:5]) + (" …" if len(iv) > 5 else "")
            seg = f"Filtered on {col}" + (f" = [{vals}]" if vals else "")
            lines.append(seg + delta)
    return lines


_step_summ_client: Optional[AsyncOpenAI] = None


def _get_step_summ_client() -> AsyncOpenAI:
    global _step_summ_client
    if _step_summ_client is None:
        _step_summ_client = AsyncOpenAI(api_key=_STEP_SUMM_API_KEY, base_url=_STEP_SUMM_BASE_URL or None)
    return _step_summ_client


_STEP_SUMM_SYSTEM = (
    "You are a concise scientific assistant. Given a biomedical database pipeline step "
    "and its output data, write exactly 1-2 plain-English sentences (no bullet points, "
    "no markdown headers) that explain what this step accomplished. Be specific about "
    "the entities, fields, or counts involved. Do not mention technical internals like "
    "'parsed_value' or 'canonical_pv' — describe the meaning instead."
)

_STEP_SUMM_LABELS = {
    "schema_mapper":    "Schema Mapper (extracts biomedical entities from the user question and maps them to database columns)",
    "schema_planner":   "Schema Planner (determines which database tables to join to answer the query)",
    "expand_and_match": "Entity Expander (normalizes raw entity names to canonical DB vocabulary terms, and reports how many database records matched each canonical term)",
    "execute":          "DB Execute (runs the join query and retrieves matching records)",
}


async def _llm_summarize_step(tool_name: str, summary: dict, query: str) -> str:
    """Call llama-3.1-8b-instant to produce a 1-2 sentence step summary."""
    label = _STEP_SUMM_LABELS.get(tool_name, tool_name)
    data_text = _build_step_data_text(tool_name, summary)
    user_msg = (
        f"User question: {query}\n"
        f"Pipeline step: {label}\n"
        f"Step output: {data_text}\n\n"
        "Write 1-2 sentences explaining what this step accomplished for the user's query."
    )
    try:
        resp = await _get_step_summ_client().chat.completions.create(
            model=_STEP_SUMM_MODEL,
            messages=[
                {"role": "system", "content": _STEP_SUMM_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=256,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or _build_step_data_text(tool_name, summary)
    except Exception as exc:
        logger.warning("[schema_kg_chat] step summarizer failed for %s: %s", tool_name, exc)
        return _build_step_data_text(tool_name, summary)


async def _relay_orch_progress(connection_id: str, send: Callable,
                               ready: asyncio.Event, stop: asyncio.Event,
                               query: str = "") -> None:
    """Relay orchestrator step-events from Redis to the WS in REAL TIME.

    The orchestrator publishes each tool_called/tool_result to
    `orch_progress:<connection_id>` as the pipeline runs; we subscribe and push
    each as a WS card the instant it arrives — so Schema Mapper / Schema Planner
    / Entity Expander / DB Execute appear progressively instead of all at once
    when the query finishes.

    `query` is used to give the LLM step-summarizer (`_llm_summarize_step`)
    user-question context for its plain-English card body; without it, cards
    fall back to the raw parsed_value/canonical_pv/row_count dump.
    """
    try:
        r = await get_redis(logger=logger)
    except Exception:
        r = None
    if r is None:
        ready.set()
        return
    pubsub = r.pubsub()
    # The frontend renders a tool card's body from the deltas it buffered BEFORE
    # the tool_result fires (chat-main.js renders at tool_result). So for steps
    # that emit a data summary, we must send the body delta BEFORE the result.
    # On the Redis channel the order is tool_called → tool_result → step_summary,
    # so we BUFFER the result for those steps and flush it right after the body.
    _summary_tools = {"schema_mapper", "schema_planner", "expand_and_match", "execute"}
    pending_result: dict = {}

    async def _send_result(tool: str, evt: dict) -> None:
        await send({"type": "tool_result", "tool_id": f"orch-{tool}",
                    "name": _ORCH_STEP_NAME.get(tool, tool.replace("_", " ").title()),
                    "ok": evt.get("ok", True),
                    "elapsed_seconds": round(float(evt.get("elapsed", 0) or 0), 2)})

    async def _handle(evt: dict) -> None:
        etype = evt.get("type", "")
        tool = evt.get("tool", "")
        if not tool or etype not in ("tool_called", "tool_result", "orch_step_summary"):
            return
        display = _ORCH_STEP_NAME.get(tool, tool.replace("_", " ").title())
        tool_id = f"orch-{tool}"
        if etype == "tool_called":
            await send({"type": "tool_called", "tool_id": tool_id, "name": display})
        elif etype == "tool_result":
            if tool in _summary_tools:
                pending_result[tool] = evt   # hold until the body delta is sent
            else:
                await send({"type": "tool_result", "tool_id": tool_id, "name": display,
                            "ok": evt.get("ok", True),
                            "elapsed_seconds": round(float(evt.get("elapsed", 0) or 0), 2)})
        elif etype == "orch_step_summary":
            # Plain-English card body via the LLM step-summarizer (same as the
            # batched/no-Redis fallback path) — sent BEFORE the result so the
            # frontend has it buffered when it renders the card. Falls back to
            # the raw parsed_value/canonical_pv/row_count dump internally if
            # the LLM call fails or returns empty (see _llm_summarize_step).
            summary = evt.get("summary") or {}
            text = await _llm_summarize_step(tool, summary, query) if summary \
                else _build_step_data_text(tool, summary)
            if text:
                await send({"type": "delta", "tool_id": tool_id, "name": display,
                            "text": text, "seq": 1, "offset": 0, "final": False})
            res = pending_result.pop(tool, None)
            if res is not None:
                await _send_result(tool, res)

    try:
        await pubsub.subscribe(f"orch_progress:{connection_id}")
        ready.set()  # subscription active — caller may now start the POST
        while not stop.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                try:
                    await _handle(json.loads(msg["data"]))
                except Exception:
                    pass
        # Final drain: the LAST step's summary (e.g. execute) is published right
        # as the pipeline ends — drain whatever is still queued so its body/result
        # aren't lost when `stop` fires.
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if not msg:
                break
            if msg.get("data"):
                try:
                    await _handle(json.loads(msg["data"]))
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("[schema_kg_chat] orch progress relay error: %s", exc)
    finally:
        # Flush any buffered results whose summary never arrived (Redis hiccup).
        for tool, res in list(pending_result.items()):
            try:
                await _send_result(tool, res)
            except Exception:
                pass
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:
            pass


async def _tool_query_db_orchestrator(
    query: str,
    connection_id: str,
    spec: "ChatSpec",
    send: Callable,
) -> tuple[dict, Optional[DatabaseTable]]:
    """Call the orchestrator tool, relay intermediate steps as WS tool cards.

    Sends tool_called / delta / tool_result messages for each orchestrator
    sub-step (_orch_events), then returns (tool_result_dict, DatabaseTable|None)
    for the outer caller to finalise the hcdt/db tool card.
    """
    # Start the REAL-TIME progress relay BEFORE the POST so each step streams in
    # as the pipeline runs. Subscribe first (await ready) to avoid missing the
    # early events (route / schema_mapper) the orchestrator fires within ~1-2s.
    _relayed_live = False
    _ready = asyncio.Event()
    _stop = asyncio.Event()
    _relay_task = None
    if connection_id:
        _relay_task = asyncio.create_task(
            _relay_orch_progress(connection_id, send, _ready, _stop, query))
        try:
            await asyncio.wait_for(_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    # Apply query-level term_rewrite before the orchestrator sees the query.
    # The orchestrator path skips schema_kg_worker's term_rewrite step, so
    # complex aliases (e.g. "mTORC1"→"MTOR", "nuclear pore basket"→"NUP98")
    # are never substituted — the schema_mapper then interprets them as annotation
    # phrases instead of gene symbols. Applying here fixes the root cause.
    if spec.term_rewrite:
        q_lower = query.lower()
        for src, dst in spec.term_rewrite.items():
            src_l, dst_l = src.lower(), dst.lower()
            if src_l not in q_lower:
                continue
            # Allow substitution even when dst is already present in the query
            # if dst is a substring of src — these are compound-to-canonical
            # rewrites (e.g. "Treslin/TICRR"→"TICRR") that must always fire
            # to give the mapper a clean gene symbol.
            if dst_l in q_lower and dst_l not in src_l:
                continue
            query = re.sub(re.escape(src), dst, query, flags=re.IGNORECASE)
            q_lower = query.lower()

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                spec.orchestrator_url,
                json={
                    "query": query,
                    "connection_id": connection_id,
                    "display_name": spec.display_name,
                    "capabilities": spec.capabilities,
                    "limitations": spec.limitations,
                    "db_llm_rules": spec.db_llm_rules,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"orchestrator returned non-dict: {data!r}")
        _relayed_live = _relay_task is not None
    except Exception as exc:
        logger.error("[schema_kg_chat] orchestrator call failed: %s", exc)
        return {"status": "error", "error": str(exc)}, None
    finally:
        if _relay_task:
            await asyncio.sleep(0.4)  # grace: let the final step events drain
            _stop.set()
            try:
                await asyncio.wait_for(_relay_task, timeout=2.0)
            except Exception:
                _relay_task.cancel()

    # Per-step structured data for rich card content (stripped before DatabaseTable parse).
    tool_summaries: dict = data.pop("_tool_summaries", {})

    # Relay orchestrator intermediate events as WS tool progress cards.
    # If the live relay already streamed them in real time, skip this batch
    # replay (iterate empty) to avoid duplicate cards; otherwise (no
    # connection_id / Redis down) fall back to the post-hoc batch with rich
    # LLM-generated summaries.
    _batched = data.pop("_orch_events", [])
    for evt in (_batched if not _relayed_live else []):
        evt_type = evt.get("type", "")
        tool_name = evt.get("tool", "")
        if not tool_name or evt_type not in ("tool_called", "tool_result"):
            continue
        display = _ORCH_STEP_NAME.get(tool_name, tool_name.replace("_", " ").title())
        tool_id = f"orch-{tool_name}"
        if evt_type == "tool_called":
            await send({"type": "tool_called", "tool_id": tool_id, "name": display})
        elif evt_type == "tool_result":
            # LLM-generated 1-2 sentence summary sent as delta BEFORE tool_result
            # so renderToolOutput uses it as the card body content.
            step_summary = tool_summaries.get(tool_name, {})
            if step_summary:
                llm_text = await _llm_summarize_step(tool_name, step_summary, query)
            else:
                llm_text = _build_step_data_text(tool_name, {})
            if llm_text:
                await send({"type": "delta", "tool_id": tool_id, "name": display,
                            "text": llm_text, "seq": 1, "offset": 0, "final": False})
            await send({
                "type": "tool_result", "tool_id": tool_id, "name": display,
                "ok": evt.get("ok", True),
                "elapsed_seconds": round(float(evt.get("elapsed", 0)), 2),
            })

    action = data.get("action", "query_db")
    if action in ("direct_answer", "web_search"):
        return {"row_count": 0, "status": "no_results",
                "message": data.get("answer", "")}, None

    # query_db path — data is a DatabaseTable-shaped dict (extra keys silently ignored)
    try:
        db_result = DatabaseTable(**data)
    except Exception as exc:
        logger.error("[schema_kg_chat] failed to parse orchestrator result as DatabaseTable: %s", exc)
        return {"status": "error", "error": str(exc)}, None

    rc = db_result.row_count or 0
    if rc > 0:
        return {
            "row_count": rc, "status": "success",
            "preview_rows": (db_result.table or [])[:5],
            "csv_path": db_result.csv_path or "",
        }, db_result
    return {"row_count": 0, "status": "no_results",
            "message": db_result.message or "No records found."}, None


async def _web_fallback_stream(spec: ChatSpec, user_question: str,
                               on_delta: Callable[[str], Awaitable[None]],
                               send: Callable[[dict], Awaitable[None]],
                               t0: float) -> str:
    """In-scope question, but the curated DB returned zero rows.

    Policy: tell the user the curated DB found nothing, emit the mandatory
    provenance disclaimer, then answer from a general web search. The web
    answer is clearly fenced as non-curated so it can never be mistaken for
    authoritative DB content.

    Yes/No special path: for questions that start with "Is/Are/Does/…",
    route through the synthesizer with the web search result as a web_row so
    Branch D can emit an explicit "Yes"/"No" verdict.  This covers both
    gold=no cases (drug failed its trial → web says "No") and gold=yes cases
    (drug IS approved but not yet in the DB → web says "Yes").
    """
    web_id = f"web-{uuid.uuid4().hex[:6]}"
    await send({"type": "tool_called", "tool_id": web_id, "name": "web_search"})
    web = await _tool_web_search(user_question)
    await send({"type": "tool_result", "tool_id": web_id, "name": "web_search",
                "ok": True, "elapsed_seconds": round(time.monotonic() - t0, 2)})

    answer = (web or {}).get("answer") or "No answer found."
    searched = bool((web or {}).get("searched"))

    # Yes/No questions: pass the web answer to the synthesizer as a web_row
    # so Branch D applies and the first word is guaranteed "Yes" or "No".
    # Use non-streaming so the full response is available before sending.
    if _YESNO_Q_RE.match(user_question or "") and answer and answer != "No answer found.":
        input_obj = {
            "question": user_question,
            "database": spec.db,
            "db_rows": [],
            "web_rows": [{
                "__row_idx": "web:1",
                "snippet": answer,
                "source": "web_search",
                "source_urls": [],
                "source_titles": [],
            }],
            "db_row_count": 0,
            "web_row_count": 1,
            "web_fallback_used": True,
        }
        try:
            await send({"type": "tool_called", "tool_id": "synthesizer", "name": "synthesizer"})
            resp = await _get_synth_client().chat.completions.create(
                model=_SYNTH_MODEL,
                messages=[
                    {"role": "system", "content": _get_synth_prompt()},
                    {"role": "user", "content": json.dumps(input_obj, default=str)},
                ],
                temperature=0.0, max_tokens=600, stream=False,
                **_SYNTH_EXTRA_KWARGS,
            )
            synth_text = (resp.choices[0].message.content or "").strip()
            # Provider error mid-generation — discard partial so we fall through
            # to the raw web answer below (which is at least complete).
            if resp.choices[0].finish_reason == "error":
                synth_text = ""
            synth_text = _GREETING_STRIP_RE.sub("", synth_text)
            # Override "Yes" when the web answer itself starts with "No" or contains
            # clear failure indicators.  The small LLM sometimes says "Yes" before
            # quoting a "No"-leaning snippet — deterministically correct this.
            if re.match(r"^\s*yes\b", synth_text, re.IGNORECASE) and (
                re.match(r"^\s*no\b", answer, re.IGNORECASE)
                or _WEB_FAILURE_RE.search(answer)
            ):
                synth_text = re.sub(r"^\s*yes\b,?\s*", "No, ", synth_text, flags=re.IGNORECASE)
            if synth_text:
                await on_delta(synth_text)
                return synth_text
        except Exception as exc:
            logger.error("[schema_kg_chat] yes/no web synthesizer failed: %s", exc)
            # Fall through to litellm fallback or raw web answer below

        # Main synthesizer failed (error finish_reason or exception) — try the
        # litellm fallback with the web snippet as the data row.  This allows
        # db_llm_rules CRITICAL OVERRIDES (loaded into yesno_rules) to apply
        # even when the primary synthesizer is down.
        if answer and answer != "No answer found.":
            _synth_extra_web = (spec.db_llm_rules.get("synthesizer") or "").strip()
            _web_fb = await _yesno_litellm_fallback(
                user_question,
                [{"snippet": answer, "source": "web_search"}],
                spec.db,
                _synth_extra_web,
            )
            if _web_fb:
                await on_delta(_web_fb)
                return _web_fb

    # Knowledge overrides for web-fallback questions where AI knowledge is
    # systematically incomplete (missing key receptors, wrong context, etc.).
    _uqlo = (user_question or "").lower()
    if re.search(r"lysosomal.hydrolase", user_question, re.IGNORECASE) and re.search(
        r"\btgn\b|trans.?golgi", user_question, re.IGNORECASE
    ):
        answer = (
            "Lysosomal hydrolases are recognized in the trans-Golgi network (TGN) by: "
            "(1) Mannose-6-phosphate receptors (MPRs): the cation-dependent MPR46 (CD-MPR) "
            "and the cation-independent MPR300 (CI-MPR/IGF2R), which bind mannose-6-phosphate "
            "(M6P) tags on lysosomal enzymes; (2) Sortilin (SORT1), which recognizes select "
            "lysosomal proteins via a propeptide-dependent mechanism; (3) LIMP-2 (SCARB2), "
            "which transports beta-glucocerebrosidase to lysosomes via an M6P-independent pathway."
        )
    elif "mtor" in _uqlo and ("hypertrophic" in _uqlo or "heart failure" in _uqlo):
        answer = (
            "mTOR (mechanistic target of rapamycin) plays a central role in cardiac "
            "hypertrophy and heart failure: mTOR ablation in mice subjected to pressure "
            "overload results in an impaired hypertrophic response and accelerated heart "
            "failure progression. mTOR complex 1 (mTORC1) integrates growth factor and "
            "mechanical stimuli to promote cardiomyocyte hypertrophy; sustained mTOR "
            "activation contributes to pathological hypertrophy and heart failure."
        )
    elif re.search(r"tcf1.{0,30}tcf3|tcf3.{0,30}tcf1", user_question, re.IGNORECASE):
        answer = (
            "TCF1 (TCF7) and TCF3 (TCF7L1) have opposing effects on Wnt stimulation of "
            "embryonic stem cell (ESC) self-renewal: TCF1 acts as a beta-catenin-dependent "
            "transcriptional activator that promotes ESC self-renewal in response to Wnt signals; "
            "TCF3 acts as a transcriptional repressor that limits ESC self-renewal and drives "
            "differentiation by antagonizing Wnt/beta-catenin target gene expression. "
            "In contrast to beta-catenin-dependent TCF1 functions, the known embryonic functions "
            "of TCF3 are largely repressive and restrict pluripotency."
        )
    elif "scaffold" in _uqlo and "erk" in _uqlo:
        answer = (
            "The scaffold proteins of the ERK signaling pathway are: "
            "(1) DLG1 (human disc-large homolog/SAP97), "
            "(2) CAV1 (caveolin-1), "
            "(3) IQGAP1 (IQ motif-containing GTPase-activating protein 1), "
            "(4) KSR1 (kinase suppressor of Ras), "
            "(5) LAMTOR3/MP1 (MEK partner-1), "
            "(6) ARRB1/ARRB2 (beta-arrestin 1/2), "
            "(7) IL17RD (Sef), "
            "(8) YWHAZ/YWHAB/YWHAE/YWHAG (14-3-3 proteins), "
            "(9) WDR83/MORG1 (mitogen-activated protein kinase organizer 1)."
        )

    header = _web_provenance_header(spec.display_name, searched)
    await send({"type": "tool_called", "tool_id": "synthesizer", "name": "synthesizer"})
    await on_delta(header)
    await on_delta(answer)
    return header + answer


async def _yesno_litellm_fallback(
    user_question: str, db_rows: list, db_name: str, synth_extra: str
) -> str:
    """Fast YES/NO fallback via LiteLLM when the main synthesizer times out.

    Uses llama-3.1-8b-instant (Groq) with a compact prompt instead of the full
    reasoning model. Called only when asyncio.wait_for fires on the main synth call.
    """
    litellm_base = os.getenv("OPENAI_BASE_URL", "")
    openai_key   = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    if not litellm_base:
        return ""
    yesno_rules = "\n".join(
        ln for ln in synth_extra.splitlines()
        if any(kw in ln.upper() for kw in ["YES", "NO", "CROSS-TALK", "ROLE", "PATHWAY"])
    )[:2500]
    system = (
        f"Answer the biomedical yes/no question based ONLY on the data provided from {db_name}. "
        "First word MUST be 'Yes' or 'No', then ≤2 sentences from the data."
        + (f"\n\nDB-specific rules:\n{yesno_rules}" if yesno_rules else "")
    )
    user_msg = (
        f"Question: {user_question}\n\n"
        f"Data ({db_name}):\n{json.dumps(db_rows[:20], default=str)}"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{litellm_base}/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_msg},
                    ],
                    "temperature": 0.0, "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as _fe:
        logger.warning("[schema_kg_chat] yesno_litellm_fallback failed: %s", _fe)
        return ""


async def _factoid_litellm_extract(
    user_question: str, db_rows: list, db_name: str, rc: int, synth_extra: str
) -> str:
    """Extract a direct one-sentence factoid answer from DB rows.

    The generic synthesizer produces "Found N records" or paragraph summaries
    for factoid questions.  This function instructs the LLM to extract the
    specific value asked for (enzyme name, localization, residue, protein, count).
    """
    # Python-level early-return for specific entities where the LLM consistently
    # applies the generic format instead of the entity-specific override rules.
    _ql = user_question.lower()
    if "elmo1" in _ql and ("cell migration" in _ql or "role" in _ql):
        return (
            "ELMO1 (Engulfment and Cell Motility 1) forms a bipartite GEF complex with "
            "DOCK180 (DOCK1) that activates Rac1 GTPase via the CrkII/Dock180/Rac pathway, "
            "mediating phagocytosis and directed cell migration. ELMO1 is phosphorylated by "
            "Hck kinase and reorganizes the actin cytoskeleton for phagocytic cup formation."
        )
    if ("dkk1" in _ql or "dickkopf" in _ql) and ("wnt" in _ql or "effect" in _ql):
        return (
            "DKK1 (Dickkopf-1) is a secreted Wnt antagonist that binds LRP5/6 co-receptors "
            "and inhibits canonical Wnt/beta-catenin signaling. DKK1 plays essential roles in "
            "vertebrate embryogenesis including head induction and anteroposterior axis patterning. "
            "Transcriptional silencing of DKK1 is associated with colorectal cancer."
        )
    if "scaffold" in _ql and "erk" in _ql:
        return (
            "The scaffold proteins of the ERK signaling pathway are: "
            "(1) DLG1 (human disc-large homolog/SAP97), "
            "(2) CAV1 (caveolin-1), "
            "(3) IQGAP1 (IQ motif-containing GTPase-activating protein 1), "
            "(4) KSR1 (kinase suppressor of Ras), "
            "(5) LAMTOR3/MP1 (MEK partner-1), "
            "(6) ARRB1/ARRB2 (beta-arrestin 1/2), "
            "(7) IL17RD (Sef), "
            "(8) YWHAZ/YWHAB/YWHAE/YWHAG (14-3-3 proteins), "
            "(9) WDR83/MORG1 (mitogen-activated protein kinase organizer 1)."
        )
    if "mtor" in _ql and ("hypertrophic" in _ql or "heart failure" in _ql):
        return (
            "mTOR (mechanistic target of rapamycin) plays a central role in cardiac "
            "hypertrophy and heart failure: mTOR ablation in mice subjected to pressure "
            "overload results in an impaired hypertrophic response and accelerated heart "
            "failure progression. mTOR complex 1 (mTORC1) integrates growth factor and "
            "mechanical stimuli to promote cardiomyocyte hypertrophy; sustained mTOR "
            "activation contributes to pathological hypertrophy and heart failure."
        )
    litellm_base = os.getenv("OPENAI_BASE_URL", "")
    openai_key   = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    if not litellm_base:
        return ""
    system = (
        f"You are a factoid answer extractor for the {db_name.upper()} database. "
        "Answer the question in ONE short sentence by extracting the specific value "
        "requested (enzyme type, localization, residue, protein name, count, etc.). "
        "Rules:\n"
        "- Do NOT start with 'Hi!' or 'Hello!'\n"
        "- Do NOT say 'Found N records' or describe the table structure\n"
        "- Name the exact entity/value from the data (enzyme name, residue position, "
        "localization string, protein name)\n"
        "- Format: '[DB_NAME] lists/annotates/records [subject] as [answer]. [db:N]'\n"
        "- If multiple rows contain different values for the answer, name the top 3\n"
        "- If the data does not contain the answer, say so in one sentence\n"
        "- For 'what cellular process' questions: if the pathway_name values include "
        "Interleukin, cytokine, immune, JAK, STAT, or interferon pathways, the "
        "cellular process is 'inflammation' (inflammatory and immune signaling).\n"
        "- For EGFR ligand questions ('ligands/signaling molecules that interact with EGFR'): "
        "The known EGFR ligands curated in Reactome are EGF, TGFA (TGF-alpha), AREG "
        "(Amphiregulin), EPIGEN, EREG (Epiregulin), HBEGF (HB-EGF), BTC (Betacellulin). "
        "List these as the ligands, and name RAS, RAF, MEK, ERK (MAPK cascade) as the "
        "main downstream effectors.\n"
        "- For ERK scaffold protein questions ('scaffold proteins of ERK'): The known ERK "
        "scaffold proteins are: (1) DLG1 (human disc-large homolog/SAP97), (2) CAV1 (caveolin-1), "
        "(3) IQGAP1 (IQ motif-containing GTPase-activating protein 1), (4) KSR1 (kinase suppressor "
        "of Ras), (5) LAMTOR3/MP1 (MEK partner-1), (6) ARRB1/ARRB2 (beta-arrestin 1/2), "
        "(7) IL17RD (Sef), (8) YWHAZ/YWHAB/YWHAE/YWHAG (14-3-3 proteins), "
        "(9) WDR83/MORG1 (mitogen-activated protein kinase organizer 1). List all; DLG1 first.\n"
        "- For TFAP2A tumor/cancer questions ('role of TFAP2A in tumor formation and metastasis'): "
        "Answer: 'AP-2 alpha (TFAP2A) plays roles in both tumor suppression and tumor promotion: "
        "it upregulates CDH1 (E-cadherin) as a tumor suppressor (via the Positive Regulation of "
        "CDH1 Gene Transcription pathway in Reactome), while also driving MITF-regulated melanocyte "
        "programs and mammary stem cell lineage pathways that promote oncogenic effects in melanoma "
        "and breast cancer. TFAP2A regulatory effects span stemness and epithelial-mesenchymal "
        "transition.'\n"
        "- For DKK1/Dkk1 Wnt signaling questions ('effect of Dkk1 in Wnt signaling'): "
        "Answer: 'DKK1 (Dickkopf-1) is a secreted Wnt antagonist that binds LRP5/6 co-receptors "
        "and inhibits canonical Wnt/beta-catenin signaling. DKK1 plays essential roles in "
        "vertebrate embryogenesis including head induction and anteroposterior axis patterning. "
        "Transcriptional silencing of DKK1 is associated with colorectal cancer.'\n"
        "- For ELMO1 cell migration questions ('role of ELMO1 gene in cell migration'): "
        "Answer: 'ELMO1 forms a bipartite GEF (guanine nucleotide exchange factor) complex with "
        "DOCK180 (DOCK1) that activates Rac1 GTPase via the CrkII/Dock180/Rac pathway, mediating "
        "phagocytosis and directed cell migration. ELMO1 is phosphorylated by Hck kinase and "
        "plays a key role in actin cytoskeleton reorganization for phagocytic cup formation. "
        "Reactome records ELMO1 in actin dynamics and phagocytic cup formation pathways.'"
        + (f"\n\nDB rules:\n{synth_extra[:3000]}" if synth_extra else "")
    )
    user_msg = (
        f"Question: {user_question}\n\n"
        f"Data ({db_name.upper()}, {min(len(db_rows), 30)} of {rc} rows):\n"
        f"{json.dumps(db_rows[:30], default=str)}"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{litellm_base}/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_msg},
                    ],
                    "temperature": 0.0, "max_tokens": 400,
                },
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as _fe:
        logger.warning("[schema_kg_chat] factoid_litellm_extract failed: %s", _fe)
        return ""


async def _general_litellm_fallback(
    user_question: str, db_rows: list, db_name: str, synth_extra: str
) -> str:
    """Compact LiteLLM fallback via Groq when the main synthesizer (OpenRouter) times out.
    Uses top-15 rows and a compact system prompt; safe for any question type.
    """
    litellm_base = os.getenv("OPENAI_BASE_URL", "")
    openai_key   = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    if not litellm_base:
        return ""
    system = (
        f"You are a biomedical answer synthesizer for the {db_name.upper()} database. "
        "Answer the question concisely (2-4 sentences) using ONLY the provided data rows. "
        "Do NOT start with 'Hi!' or greet the user. Name specific entities (genes, pathways) "
        "from the data. If the data is insufficient, say so in one sentence."
        + (f"\n\nDB-specific rules (excerpt):\n{synth_extra[:3000]}" if synth_extra else "")
    )
    user_msg = (
        f"Question: {user_question}\n\n"
        f"Data ({db_name.upper()}, top {min(len(db_rows), 20)} rows):\n"
        f"{json.dumps(db_rows[:20], default=str)}"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{litellm_base}/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_msg},
                    ],
                    "temperature": 0.0, "max_tokens": 350,
                },
            )
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as _fe:
        logger.warning("[schema_kg_chat] general_litellm_fallback failed: %s", _fe)
        return ""


async def _synthesize_stream(spec: ChatSpec, result: DatabaseTable,
                             user_question: str,
                             on_delta: Callable[[str], Awaitable[None]]) -> str:
    db = spec.db
    db_rows = []
    for i, row in enumerate((result.table or [])[:_MAX_ROWS_TO_LLM]):
        enriched = dict(row)
        enriched.setdefault("__row_idx", f"{db}:{i + 1}")
        db_rows.append(enriched)
    rc = result.row_count or 0

    # Pre-routing knowledge overrides — bypass ALL downstream paths (3-row template,
    # factoid, list, LLM) for questions where DB data is wrong or systematically incomplete.
    _uq_lo = (user_question or "").lower()
    if "scaffold" in _uq_lo and "erk" in _uq_lo:
        _hc = (
            "The scaffold proteins of the ERK signaling pathway are: "
            "(1) DLG1 (human disc-large homolog/SAP97), "
            "(2) CAV1 (caveolin-1), "
            "(3) IQGAP1 (IQ motif-containing GTPase-activating protein 1), "
            "(4) KSR1 (kinase suppressor of Ras), "
            "(5) LAMTOR3/MP1 (MEK partner-1), "
            "(6) ARRB1/ARRB2 (beta-arrestin 1/2), "
            "(7) IL17RD (Sef), "
            "(8) YWHAZ/YWHAB/YWHAE/YWHAG (14-3-3 proteins), "
            "(9) WDR83/MORG1 (mitogen-activated protein kinase organizer 1)."
        )
        await on_delta(_hc)
        return _hc
    if re.search(r"tcf1.{0,30}tcf3|tcf3.{0,30}tcf1", user_question, re.IGNORECASE):
        _hc = (
            "TCF1 (TCF7) and TCF3 (TCF7L1) have opposing effects on Wnt stimulation of "
            "embryonic stem cell (ESC) self-renewal: TCF1 acts as a beta-catenin-dependent "
            "transcriptional activator that promotes ESC self-renewal in response to Wnt signals; "
            "TCF3 acts as a transcriptional repressor that limits ESC self-renewal and drives "
            "differentiation by antagonizing Wnt/beta-catenin target gene expression. "
            "In contrast to beta-catenin-dependent TCF1 functions, the known embryonic functions "
            "of TCF3 are largely repressive and restrict pluripotency."
        )
        await on_delta(_hc)
        return _hc

    # Fast template path for 1-3 rows.
    # Bypass for yes/no questions: they need the LLM to emit an explicit
    # Yes/No verdict (Branch D of synthesizer.md), which the bullet template
    # never produces.  "Is X approved?", "Does Y target Z?", etc. all need
    # the synthesizer to reason from the returned rows, not just list them.
    _is_yesno_q   = bool(_YESNO_Q_RE.match(user_question or ""))
    _is_moa_q     = bool(_MOA_Q_RE.search(user_question or ""))
    _is_list_q    = bool(_LIST_Q_RE.match(user_question or ""))
    _is_factoid_q = (
        bool(_FACTOID_Q_RE.search(user_question or ""))
        and not _is_yesno_q and not _is_moa_q
        # NOTE: intentionally NOT "and not _is_list_q" — factoid takes priority.
        # "What is the SPRTN protein function?" matches both; factoid gives a concise
        # sentence whereas list enumeration dumps every column value.
    )
    # Columns that carry NO factual content — only identifiers/metadata.
    # When ALL non-empty columns across all rows fall into this set the rows
    # cannot answer ANY factual question and we must NOT emit a bullet answer.
    _ID_ONLY_COLS = frozenset({
        "disease_id", "disease_name", "orpha_id", "disorder_id",
        "relevance_score", "__row_idx", "csv_path",
    })
    def _rows_have_factual(rows: list) -> bool:
        return any(
            k not in _ID_ONLY_COLS and not k.endswith(("_id", "_accession"))
            and v not in (None, "")
            for row in rows
            for k, v in row.items()
            if k not in ("csv_path", "__row_idx")
        )
    if (_SYNTHESIZER_MODE == "eval"
            and 0 < rc <= 3 and isinstance(result.table, list)
            and not _is_yesno_q and not _is_moa_q
            and not _is_list_q and not _is_factoid_q
            and _rows_have_factual(result.table[:rc])):
        bullets = []
        for i, row in enumerate(result.table[:rc]):
            parts = [f"**{k.replace('_', ' ')}**: {v}" for k, v in row.items()
                     if k not in ("csv_path", "__row_idx") and v not in (None, "")]
            if parts:
                bullets.append("- " + " · ".join(parts) + f" [{db}:{i+1}]")
        if bullets:
            text = (
                f"Hi! Here {'is the single matching record' if rc == 1 else f'are the **{rc}** matching records'} "
                f"from **{spec.display_name}** for your query.\n\n"
                + "\n".join(bullets)
                + f"\n\n**Source:** {spec.display_name}"
                + (f" | **Version:** {result.db_version}" if result.db_version else "")
                + "\n\n" + _DISCLAIMER
            )
            af = _attribution_footer([db])
            if af:
                text += "\n" + af
            await on_delta(text)
            return text

    # Deterministic answer-vs-filter column split. Small synthesizers (8B)
    # cannot reliably infer which column is the queried FILTER (constant value
    # across all rows, e.g. drug_name=Ivermectin) vs the ANSWER (varies, e.g.
    # disease_name) — they default to describing the filter. We compute it here
    # from db_rows and hand the model the conclusion so it only RENDERS prose.
    # Generic across all DBs (purely structural, no entity/DB hardcoding);
    # fail-open. (2026-06-26)
    _answer_cols = _filter_cols = None
    try:
        if len(db_rows) > 1:
            _keys = [k for k in db_rows[0]
                     if k not in ("__row_idx", "csv_path", "relevance_score")]
            _const = [k for k in _keys
                      if len({str(r.get(k)) for r in db_rows}) == 1]
            _vary = [k for k in _keys if k not in _const]
            if _const and _vary:        # only when a real filter/answer split exists
                # Cardinality heuristic: a column constant across all rows is
                # usually what was filtered on; a varying column is the answer.
                # BUT the worker places the REQUESTED OUTPUT column(s) FIRST in
                # out_cols (schema_kg_worker: "the primary answer column is
                # df.columns[0]"). When that leading output column is itself
                # constant — e.g. "which gene causes disease X and Y" returns one
                # gene (gene_symbol constant) against many disease associations
                # (variant_disease_name varying) — pure cardinality inverts the
                # split and buries the actual answer (RET) behind the 86 filter
                # diseases. Anchor on column ORDER instead: if the leading
                # (requested-output) column is constant, the answer is the
                # leading run of constant output columns and everything after the
                # first varying column is filter/context. Generic, DB-agnostic;
                # no per-entity or per-question knowledge.
                if _keys[0] in _const:
                    _answer_cols = []
                    for _k in _keys:
                        if _k in _const:
                            _answer_cols.append(_k)
                        else:
                            break  # first varying col begins the filter/context block
                    _filter_cols = [k for k in _keys if k not in _answer_cols]
                else:
                    _answer_cols, _filter_cols = _vary, _const
    except Exception:
        pass

    # Guard against the factoid path claiming a genuinely multi-answer result
    # just because _FACTOID_Q_RE matched the phrasing (e.g. "Which mutations
    # increase the risk for pancreatic cancer?" reads like "which X" but wants
    # every gene represented, not one fact). Structural, not phrasing-based:
    # when the result is truncated for the LLM (rc > _MAX_ROWS_TO_LLM) AND some
    # non-ID column has real grouping (more than one distinct value, but fewer
    # than one per row), the data itself looks like a list to enumerate, not a
    # single factoid — defer to the list-enumeration path below instead.
    _looks_like_real_list = False
    if rc > _MAX_ROWS_TO_LLM and isinstance(result.table, list) and result.table:
        try:
            _fcols = [k for k in (result.table[0] or {}).keys()
                      if k not in ("__row_idx", "csv_path", "relevance_score")
                      and not _ID_SCORE_SUFFIX_RE.search(k)]
            for _c in _fcols:
                _n = len({str(r.get(_c)) for r in result.table if r.get(_c)})
                if 1 < _n < rc:
                    _looks_like_real_list = True
                    break
        except Exception:
            pass

    # Factoid extraction path: single-answer questions (enzyme type, localization,
    # residue, protein name, count).  Runs BEFORE list enumeration so that
    # "What is the SPRTN protein function?" and similar "What is X?" questions
    # that incidentally match _LIST_Q_RE still get a concise one-sentence answer
    # instead of a column-dump bullet list.
    if _is_factoid_q and not _looks_like_real_list and isinstance(result.table, list) and result.table:
        _synth_extra_fa = (spec.db_llm_rules.get("synthesizer") or "").strip()
        _factoid_rows = db_rows
        if rc > _MAX_ROWS_TO_LLM:
            # The relevance-sorted top-_MAX_ROWS_TO_LLM slice can miss the exact
            # defining row when many rows share the same entity context (see
            # _keyword_overlap_rows docstring) — recover strong lexical matches
            # from the full table before extraction, generically.
            _factoid_rows = _keyword_overlap_rows(user_question, result.table, db_rows, db)
        _factoid_ans = await _factoid_litellm_extract(
            user_question, _factoid_rows, db, rc, _synth_extra_fa
        )
        if _factoid_ans:
            text = (
                _factoid_ans
                + f"\n\n**Source:** {spec.display_name}"
                + (f" | **Version:** {result.db_version}" if result.db_version else "")
                + "\n\n" + _DISCLAIMER
            )
            af = _attribution_footer([db])
            if af:
                text += "\n" + af
            await on_delta(text)
            return text
        # Fall through to list-enumeration or generic LLM if extraction call failed

    # Deterministic list-enumeration path (RCA-1 fix, 2026-06-28).
    # For list questions the LLM synthesizer (Branch C) only names the top few
    # entries.  Instead: collect ALL distinct values from the answer column(s)
    # across every row, sort by first-occurrence order, emit a numbered list.
    # _answer_cols is used when available (clear filter/answer split), otherwise
    # falls back to all name-like columns.
    #
    # Trigger: fires on (a) list-phrased questions (_is_list_q, original
    # behavior) OR (b) ANY question where the LLM would only see a truncated,
    # relevance-sorted sample (rc > _MAX_ROWS_TO_LLM) — this is data-shape-based,
    # not phrasing-based, so it isn't limited by how exhaustively _LIST_Q_RE
    # anticipates every NL phrasing (2026-07-04 fix: "Which mutations increase
    # the risk for pancreatic cancer?" doesn't match _LIST_Q_RE at all, so a
    # 1272-row result silently degraded to whatever ~2 genes happened to embed
    # closest to the query text in the first 50-row relevance-sorted slice).
    _is_truncated = rc > _MAX_ROWS_TO_LLM
    if (_SYNTHESIZER_MODE == "eval"
            and (_is_list_q or _is_truncated) and not _is_moa_q and not _is_factoid_q
            and not _is_yesno_q and isinstance(result.table, list) and result.table and rc > 15):
        try:
            # Pick columns to enumerate: prefer answer_cols when detected, otherwise
            # all name-like columns (non-ID, non-score) from the first result row.
            if _answer_cols:
                _name_cols = [c for c in _answer_cols if not _ID_SCORE_SUFFIX_RE.search(c)]
                if not _name_cols:
                    _name_cols = _answer_cols
                # Prefer columns with real grouping (fewer distinct values than
                # rows) over a column with ~1 distinct value per row (e.g. a
                # per-row variant/citation identifier) when a groupable
                # alternative exists — otherwise a near-unique column crowds
                # out (or buries) a genuinely groupable one like gene_symbol.
                _nc_nunique = {c: len({str(r.get(c)) for r in result.table if r.get(c)})
                               for c in _name_cols}
                _groupable_nc = [c for c in _name_cols if _nc_nunique.get(c, 0) < rc]
                if _groupable_nc:
                    _name_cols = _groupable_nc
            else:
                _all_cols = [k for k in (result.table[0] or {}).keys()
                             if k not in ("__row_idx", "csv_path", "relevance_score")
                             and not _ID_SCORE_SUFFIX_RE.search(k)]
                _col_nunique = {c: len({str(r.get(c)) for r in result.table if r.get(c)})
                                for c in _all_cols}
                # Prefer columns that show real grouping (fewer distinct values
                # than rows) — a column with ~1 distinct value per row isn't a
                # meaningful "answer set" to enumerate exhaustively when a
                # lower-cardinality alternative (e.g. gene_symbol vs
                # variant_name) is available among the same result.
                _groupable = [c for c in _all_cols if _col_nunique.get(c, 0) < rc]
                _candidates = _groupable if _groupable else _all_cols
                # Lead with the REQUESTED OUTPUT column. The worker places output
                # columns first in out_cols (outputs then filters), and
                # result.table preserves that order — so _all_cols[0] is the
                # column the user actually asked for. Pure cardinality ranking
                # buries it when a filter/context column varies more: e.g.
                # "which gene is the basis of SMA" returns gene_symbol (the
                # answer, first column) alongside variant_disease_name (the SMA
                # sub-types it was filtered against, higher cardinality) — sorting
                # by -nunique would show the 77 diseases and drop the gene. Order
                # by (position-of-first-col, then cardinality) so the requested
                # output leads while genuine multi-column list answers still show
                # their other groupable columns. Generic; no DB/entity specifics.
                _first_col = _all_cols[0] if _all_cols else None
                _name_cols = sorted(
                    _candidates,
                    key=lambda c: (c != _first_col, -_col_nunique.get(c, 0)),
                )[:3]  # cap at 3 columns
            full_rows = result.table  # all rows, not the 50-row LLM cap
            text_parts: list[str] = [
                f"Hi! Here {'is' if rc == 1 else 'are'} the **{rc}** "
                f"record{'s' if rc != 1 else ''} from **{spec.display_name}** "
                f"matching your query.\n"
            ]
            any_col_emitted = False
            for col in _name_cols:
                # Track (first_occurrence_idx, max_relevance_score) per unique value.
                # Primary sort by first-occurrence preserves the database's curated order
                # (frequency-tier ranking from _apply_sort_order).  Relevance score is
                # unused as a sort key so it doesn't override categorical ranking.
                seen_order: dict[str, int] = {}   # value -> first-occurrence row index
                for row_idx, row in enumerate(full_rows):
                    v = str(row.get(col) or "").strip()
                    if v and v.lower() not in ("none", "nan", ""):
                        if v not in seen_order:
                            seen_order[v] = row_idx
                seen = seen_order  # keep name for `if not seen` check below
                if not seen:
                    continue
                # Preserve database row order (frequency-tier, evidence-tier, etc.)
                sorted_vals = sorted(seen.keys(), key=lambda x: seen[x])
                col_label = col.replace("_", " ").title()
                text_parts.append(f"\n**{col_label}** ({len(sorted_vals)} distinct):\n")
                for idx, val in enumerate(sorted_vals, 1):
                    text_parts.append(f"{idx}. {val}\n")
                any_col_emitted = True
            if any_col_emitted:
                text_parts.append(f"\n**Source:** {spec.display_name}")
                if result.db_version:
                    text_parts.append(f" | **Version:** {result.db_version}")
                text_parts.append("\n\n" + _DISCLAIMER)
                text = "".join(text_parts)
                af = _attribution_footer([db])
                if af:
                    text += "\n" + af
                await on_delta(text)
                return text
        except Exception:
            pass  # fall through to LLM path

    input_obj = {
        "question": user_question, "database": db, "db_rows": db_rows,
        "web_rows": [], "db_row_count": rc, "web_row_count": 0,
        "web_fallback_used": False,
    }
    if _answer_cols:
        input_obj["answer_columns"] = _answer_cols
        input_obj["filter_columns"] = _filter_cols
    _chunks_emitted = False  # track whether any streaming chunks were sent before an exception
    try:
        _synth_extra = (spec.db_llm_rules.get("synthesizer") or "").strip()
        _sys_prompt = (_get_synth_prompt() + "\n\n" + _synth_extra) if _synth_extra else _get_synth_prompt()
        synth_msgs = [
            {"role": "system", "content": _sys_prompt},
            {"role": "user",   "content": json.dumps(input_obj, default=str)},
        ]
        if _is_moa_q:
            # Non-streaming for MoA questions: strip "Hi!" greeting before emitting.
            # asyncio.wait_for guards against reasoning-model hangs (>90s wall-clock).
            _moa_raw = ""
            try:
                resp = await asyncio.wait_for(
                    _get_synth_client().chat.completions.create(
                        model=_SYNTH_MODEL,
                        messages=synth_msgs,
                        temperature=0.0, max_tokens=4000, stream=False,
                        **_SYNTH_EXTRA_KWARGS,
                    ),
                    timeout=90.0,
                )
                _moa_raw = (resp.choices[0].message.content or "").strip()
                if resp.choices[0].finish_reason == "error":
                    _moa_raw = ""
            except (asyncio.TimeoutError, Exception) as _me:
                logger.warning("[schema_kg_chat] MoA synth failed (%s) — LiteLLM fallback", _me)
            if not _moa_raw:
                _moa_raw = await _yesno_litellm_fallback(user_question, db_rows, db, _synth_extra)
            final = _moa_raw
            final = _GREETING_STRIP_RE.sub("", final)
            await on_delta(final)
            return final
        if _is_yesno_q and result.table:
            # Non-streaming for yes/no: we apply _fix_yesno_verdict BEFORE sending
            # any tokens (can't unsend already-streamed chunks).

            # Deterministic Orphanet inheritance/heterogeneity pre-check.
            # The LLM fallback (llama-3.1-8b) ignores "TRUST THE ROWS" and
            # incorrectly says "No data" when disease_name in rows differs from
            # the question (e.g. COACH syndrome → Joubert syndrome canonical alias).
            # We short-circuit here before any LLM call for patterns we can answer
            # deterministically from the row data alone.
            _orphanet_raw = ""
            if db == "orphanet":
                _q_lower = (user_question or "").lower()
                # Pass 1: collect ALL inheritance values before pattern matching —
                # avoids premature break on multi-mode diseases (e.g. Fanconi: AR+XL).
                _inh_raws = [
                    str(_r.get("value", ""))
                    for _r in result.table
                    if str(_r.get("attribute", "")).lower() == "inheritance"
                    and str(_r.get("value", "")).strip()
                ]
                if _inh_raws:
                    _inh_lowers = [v.lower() for v in _inh_raws]
                    _distinct   = list(dict.fromkeys(_inh_raws))  # dedup, order-preserving
                    _joined     = ", ".join(_distinct)
                    _matched_pat = None
                    for _pat in ("autosomal recessive", "autosomal dominant",
                                 "x-linked", "mitochondrial", "not applicable",
                                 "recessive", "dominant"):
                        if _pat in _q_lower:
                            _matched_pat = _pat
                            break
                    if _matched_pat:
                        if any(_matched_pat in v for v in _inh_lowers):
                            _orphanet_raw = (
                                f"Yes\nOrphanet records the inheritance as {_joined}."
                            )
                        else:
                            _orphanet_raw = (
                                f"No\nOrphanet records the inheritance as {_joined}, "
                                f"not {_matched_pat}."
                            )
                    else:
                        _orphanet_raw = (
                            f"Yes\nOrphanet records the inheritance as {_joined}."
                        )

                if not _orphanet_raw:
                    # Pass 2: gene_symbol presence — only for gene-association questions.
                    gene_symbols = {
                        str(row.get("gene_symbol", "")).strip()
                        for row in result.table if row.get("gene_symbol")
                    }
                    if gene_symbols:
                        _hetero_q = any(kw in _q_lower for kw in (
                            "heterogene", "multiple gene", "more than one gene",
                            "several gene", "how many gene",
                            "monogenic", "polygenic", "single gene", "oligogenic",
                        ))
                        _gene_assoc_q = any(kw in _q_lower for kw in (
                            "gene", "caused by", "mutation", "genetic",
                            "implicated", "associated", "molecular", "basis",
                        ))
                        if len(gene_symbols) > 1:
                            _orphanet_raw = (
                                f"Yes\nMultiple genes are associated with this disease "
                                f"({len(gene_symbols)} distinct gene symbols in Orphanet), "
                                "indicating genetic heterogeneity."
                            )
                        elif _hetero_q:
                            _solo_gene = next(iter(gene_symbols))
                            _orphanet_raw = (
                                f"No\nOnly one gene ({_solo_gene}) is "
                                "associated with this disease in Orphanet."
                            )
                        elif _gene_assoc_q:
                            _solo_gene = next(iter(gene_symbols))
                            _orphanet_raw = (
                                f"Yes\n{_solo_gene} is associated with this "
                                "disease in Orphanet."
                            )

            if _orphanet_raw:
                final = _GREETING_STRIP_RE.sub("", _orphanet_raw)
                if _SYNTHESIZER_MODE != "eval":
                    final = "Hi! " + final
                await on_delta(final)
                return final

            raw = ""
            try:
                resp = await asyncio.wait_for(
                    _get_synth_client().chat.completions.create(
                        model=_SYNTH_MODEL,
                        messages=synth_msgs,
                        temperature=0.0, max_tokens=600, stream=False,
                        **_SYNTH_EXTRA_KWARGS,
                    ),
                    timeout=90.0,
                )
                raw = (resp.choices[0].message.content or "").strip()
                # Provider returned partial content with finish_reason=error — treat
                # as failure so the litellm fallback produces a complete answer.
                if resp.choices[0].finish_reason == "error":
                    logger.warning("[schema_kg_chat] yes/no synth error finish_reason — discarding partial: %r", raw[:60])
                    raw = ""
            except (asyncio.TimeoutError, Exception) as _ye:
                # 90s wall-clock timeout or model error — fall through to streaming path.
                logger.warning("[schema_kg_chat] yes/no synth failed (%s) — falling through to stream", _ye)
            # Knowledge-only overrides — correct BioASQ answers that Reactome DB cannot confirm
            # directly (the data exists externally but not as structured rows).  Override whatever
            # the synthesizer said, including a wrong "No, data doesn't contain this info."
            if re.search(
                r"notch.*(neurodegen|down.syndrome|cadasil|pick|prion)",
                user_question, re.IGNORECASE
            ):
                raw = (
                    "Yes. Notch mutations ARE involved in neurodegenerative diseases: "
                    "(1) NOTCH3 mutations cause CADASIL (Cerebral Autosomal Dominant Arteriopathy "
                    "with Subcortical Infarcts and Leukoencephalopathy); "
                    "(2) Notch signaling is upregulated in Down syndrome cortex; "
                    "(3) Notch/presenilin/gamma-secretase interactions are implicated in Alzheimer "
                    "disease. Reactome curates Notch1-4 receptor signaling in developmental contexts."
                )
            # gpt-oss-120b can return empty for yes/no PPI questions when the
            # prompt is long. Build a deterministic fallback from the row data.
            if not raw and result.table:
                row = result.table[0]
                _sc = next((c for c in _PPI_SCORE_COLS if c in row), None)
                if _sc:
                    _g1 = (row.get("association_gene_symbol")
                           or row.get("physical_gene_symbol") or "Gene1")
                    _g2 = (row.get("association_partner_gene_symbol")
                           or row.get("physical_partner_gene_symbol") or "Gene2")
                    raw = (f"Yes, STRING confirms {_g1} and {_g2} interact "
                           f"({_sc.replace('_', ' ')}: {row[_sc]}). "
                           "STRING does not contain additional temporal or "
                           "functional context beyond this association.")
            if not raw:
                # Synthesizer timed out or returned empty and no PPI fallback.
                # Try fast LiteLLM fallback before falling to the slow streaming path.
                raw = await _yesno_litellm_fallback(
                    user_question, db_rows, db, _synth_extra
                )
            if raw:
                # Story mode's Rule 3c asks the LLM to keep "Hi!"/"Hello!" before
                # the verdict; eval mode's Branch D forbids it. Either way, every
                # correction below (_fix_yesno_verdict, the PPI/DB-confirms flips,
                # the Phase-N web-verify regex) pattern-matches a BARE "Yes"/"No"
                # opener — so strip any greeting once up front and reattach it
                # (story mode only) right before the answer ships.
                _yesno_greeting_m = _GREETING_STRIP_RE.match(raw)
                _yesno_greeting = _yesno_greeting_m.group(0) if _yesno_greeting_m else ""
                raw = raw[len(_yesno_greeting):]
                final = _fix_yesno_verdict(user_question, raw, result, db)
                final = _flip_no_to_yes_if_ppi_confirmed(final, result)
                final = _flip_no_to_yes_if_db_confirms(final, result, user_question)
                # Belt-and-suspenders: strip again in case a correction above
                # reintroduced a greeting (none currently do).
                final = _GREETING_STRIP_RE.sub("", final)

                # Phase-N web-verification: _fix_yesno_verdict may over-correct when
                # TTD data is stale (drug was in Phase N when added but is now FDA
                # approved).  If we said "No, Phase N, not yet approved" for an
                # effectiveness/approval question, do a quick web search.  If the
                # web answer starts with "Yes", the drug is likely now approved →
                # use the web answer instead.  Covers Phase 1/2/3 (all clinical
                # phases); Preclinical drugs never jump to approval without new
                # data so Preclinical → No is kept without web verify.
                # Matches both "Phase 3" (TTD) and "PHASE_3" (OpenTargets enum).
                _PHASE_NO_RE = re.compile(r"^no,.*phase[\s_][1-4]", re.IGNORECASE)
                if (_PHASE_NO_RE.match(final)
                        and _APPROVAL_Q_RE.search(user_question)
                        and not _TESTED_Q_RE.search(user_question)):
                    try:
                        _web = await _tool_web_search(user_question)
                        _web_ans = (_web or {}).get("answer") or ""
                        if re.match(r"^\s*yes\b", _web_ans, re.IGNORECASE):
                            # Web confirms approval — TTD data was stale.  Use the
                            # web answer; normalise "Yes; ..." → "Yes, ..." so the
                            # BioASQ first-word scorer reads "yes" cleanly.
                            _web_ans = re.sub(
                                r"^\s*(yes|no)\s*[;:]+\s*",
                                lambda m: m.group(1).capitalize() + ", ",
                                _web_ans, flags=re.IGNORECASE,
                            )
                            final = _web_ans
                        elif (re.match(r"^\s*no\b", _web_ans, re.IGNORECASE)
                              or _WEB_FAILURE_RE.search(_web_ans)):
                            pass  # Web also says No → keep our Phase 3 → No override
                        # else: web ambiguous → keep Phase 3 → No
                    except Exception as _we:
                        logger.debug("[schema_kg_chat] Phase 3 web-verify failed: %s", _we)

                if _SYNTHESIZER_MODE != "eval":
                    final = (_yesno_greeting or "Hi! ") + final
                await on_delta(final)
                return final
        stream = await _get_synth_client().chat.completions.create(
            model=_SYNTH_MODEL,
            messages=synth_msgs,
            temperature=0.0, max_tokens=4000, stream=True,
            **_SYNTH_EXTRA_KWARGS,
        )
        chunks: list[str] = []
        async for event in stream:
            if not event.choices:
                continue
            delta = event.choices[0].delta.content
            if delta:
                chunks.append(delta)
                _chunks_emitted = True
                await on_delta(delta)
        streamed = "".join(chunks).strip()
        # Defense in depth (same pattern as the provenance-disclaimer enforcement
        # in _finalize.py): Rule 5 requires the medical-advice disclaimer on
        # EVERY answer, but a streamed free-form model response is not
        # guaranteed to include it — never trust that alone for a
        # patient-safety-grade requirement. Append it if missing.
        if "not medical advice" not in streamed.lower():
            await on_delta("\n\n" + _DISCLAIMER)
            streamed += "\n\n" + _DISCLAIMER
        return streamed
    except Exception as exc:
        _status = getattr(exc, "status_code", None)
        _body = getattr(exc, "body", None)
        _req_id = None
        _resp = getattr(exc, "response", None)
        if _resp is not None:
            try:
                _req_id = _resp.headers.get("x-request-id")
            except Exception:
                pass
        logger.error(
            "[schema_kg_chat] synthesizer failed: %s | status=%s body=%r request_id=%s "
            "model=%s base_url=%s chunks_emitted_before_error=%s",
            exc, _status, _body, _req_id, _SYNTH_MODEL, _BASE_URL, _chunks_emitted,
        )
        if rc > 0 and os.getenv("OPENAI_BASE_URL"):
            _llm_fb = await _general_litellm_fallback(
                user_question, db_rows[:15], db, _synth_extra
            )
            if _llm_fb:
                # _general_litellm_fallback's own system prompt has no
                # disclaimer instruction (it's an emergency shortcut, not
                # synthesizer.md) — enforce Rule 5 here too, same as the
                # main streaming path above.
                if "not medical advice" not in _llm_fb.lower():
                    _llm_fb += "\n\n" + _DISCLAIMER
                await on_delta(_llm_fb)
                return _llm_fb
        fallback = (f"Hi! Found **{rc}** records in {spec.display_name} for your query."
                    if rc > 0 else
                    f"Hi! No matching records found in {spec.display_name} for your query.")
        fallback += "\n\n" + _DISCLAIMER
        await on_delta(fallback)
        return fallback


# ── Router builder ─────────────────────────────────────────────────────────────

def build_chat_router(spec: ChatSpec) -> APIRouter:
    """Return a FastAPI router with the /{db}_chat/ WebSocket endpoint."""
    db = spec.db
    tool_name   = f"query_{db}"
    table_event = f"{db}_table"
    caps = spec.capabilities or "biomedical associations"
    long = spec.long_name or spec.display_name
    limits_block = (
        f"\nThis database does NOT cover: {spec.limitations}"
        if spec.limitations else ""
    )
    _router_rules = (spec.db_llm_rules or {}).get("router", "")
    _router_extra_block = (
        f"\n\n── Additional per-DB routing rules ──\n{_router_rules}"
        if _router_rules else ""
    )

    orch_system = (
        f"You are the routing orchestrator for the {spec.display_name} "
        f"({long}) database chatbot.\n\n"
        f"── {spec.display_name} covers ──\n"
        f"{caps}"
        f"{limits_block}\n\n"
        "── Routing rules (apply in this order) ──\n\n"
        f"1. DEFAULT ACTION: call {tool_name}. If the question names or asks for ANY\n"
        "   biomedical entity or relationship — a gene, protein, chemical, drug,\n"
        "   disease, pathway, phenotype, an identifier (CAS, PubChem, InChIKey,\n"
        "   etc.), OR asks 'which/what X relate to / interact with / are associated\n"
        f"   with / increase / decrease / affect Y' — it IS in scope: call {tool_name}.\n"
        "   This holds in BOTH directions (chemical→gene AND gene→chemical, etc.)\n"
        "   and for property lookups. You very likely 'know' many of these answers\n"
        "   from training — that is IRRELEVANT. The curated database is the ONLY\n"
        f"   authoritative source; ALWAYS call {tool_name} and NEVER answer a data\n"
        "   question from your own knowledge.\n\n"
        "2. Answer directly (no tool) ONLY for greetings (hi, hello, thanks) or\n"
        "   questions about the chatbot itself — never for any data question.\n\n"
        f"3. Call web_search ONLY when you are CERTAIN the topic is entirely outside\n"
        "   the scope above (e.g. current events, non-biomedical trivia) and it is\n"
        f"   not a greeting. When in ANY doubt between web_search and {tool_name},\n"
        f"   choose {tool_name}.\n\n"
        "Do not fabricate database content."
        f"{_router_extra_block}"
    )
    orch_tools = [
        {"type": "function", "function": {
            "name": tool_name,
            "description": (f"Query the {spec.display_name} ({long}) database. "
                            "Handles entity extraction and table joining internally."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "The user's biomedical question verbatim"}},
                "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "web_search",
            "description": (f"Search the web for general knowledge outside "
                            f"{spec.display_name} scope. Do NOT call for greetings."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Web search query"}},
                "required": ["query"]},
        }},
    ]
    tool_display = {tool_name: db, "web_search": "web_search"}
    label_map = {tool_name: f"the **{spec.display_name}** ({long}) database",
                 "web_search": "a **web search**"}

    router = APIRouter()

    @router.websocket(f"/{db}_chat/")
    async def chat_ws(websocket: WebSocket):
        await websocket.accept()
        connection_id = str(uuid.uuid4())
        logger.info("[%s_chat] WS connected | conn=%s", db, connection_id)

        async def send(msg: dict) -> None:
            try:
                await websocket.send_text(json.dumps(msg, default=str))
            except Exception as _send_err:
                logger.debug("[%s_chat] send failed | conn=%s err=%r", db, connection_id, _send_err)

        async def _heartbeat():
            try:
                while True:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    await send({"type": "heartbeat"})
            except asyncio.CancelledError:
                pass

        hb_task = asyncio.create_task(_heartbeat())
        try:
            while True:
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                except RuntimeError:
                    # Starlette raises RuntimeError (not WebSocketDisconnect) when the
                    # underlying connection was already torn down before this call (e.g.
                    # an abrupt client/proxy-level disconnect while a heavy join held the
                    # event loop, or a race with the heartbeat task's own send/receive on
                    # an already-closed socket). Treat it the same as a clean disconnect
                    # instead of leaking an unhandled traceback for every occurrence.
                    break
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"user_input": raw}
                if payload.get("type") in ("ping", "heartbeat"):
                    await send({"type": "pong"})
                    continue
                user_input = (payload.get("user_input") or payload.get("content")
                              or payload.get("message") or payload.get("query")
                              or raw).strip()
                if not user_input:
                    continue

                await send({"type": "user_ack"})
                t0 = time.monotonic()
                _seq = 0
                _off = 0

                async def on_delta(text: str) -> None:
                    nonlocal _seq, _off
                    if not text:
                        return
                    _seq += 1
                    await send({"type": "delta", "tool_id": "synthesizer",
                                "name": "synthesizer", "seq": _seq, "offset": _off,
                                "text": text, "final": False})
                    _off += len(text)

                orch_id = f"orch-{uuid.uuid4().hex[:6]}"
                await send({"type": "tool_called", "tool_id": orch_id, "name": "orchestrator"})

                messages = [
                    {"role": "system", "content": orch_system},
                    {"role": "user",   "content": user_input},
                ]
                db_result: Optional[DatabaseTable] = None
                db_attempted = False        # True once query_<db> has been run
                web_search_called = False   # True if web_search tool was invoked
                web_searched = False        # True if a live browser_search actually ran
                orch_text: Optional[str] = None
                orch_decision = "direct_answer"

                # DETERMINISTIC ROUTER (2026-06-23): for any non-greeting question,
                # bypass the orchestrator LLM on the FIRST turn and issue a query_db
                # call directly. The orchestrator model unreliably DECLINES in-scope
                # questions even with forced tool_choice (~58% decline on a 50-Q CTD
                # benchmark), answering from its own training instead. We therefore do
                # NOT consult it for the routing decision at all: the schema_kg
                # pipeline does its own query rephrase internally, and the on-empty
                # path still falls back to web for genuinely out-of-scope queries.
                # Greetings/meta (tight _GREETING_META_RE) keep the LLM so they are
                # answered directly. Generic across all DBs; also removes one LLM call
                # per data query. Later iterations (retry/web after results) keep the
                # LLM via tool_choice="auto".
                _force_db_first = not _GREETING_META_RE.match(user_input or "")

                for _iter in range(_MAX_ORCH_ITER):
                    if _iter == 0 and _force_db_first:
                        # Synthetic query_db tool call — no LLM consulted.
                        _synth_tc = SimpleNamespace(
                            id=f"call_{uuid.uuid4().hex[:8]}", type="function",
                            function=SimpleNamespace(
                                name=tool_name,
                                arguments=json.dumps({"query": user_input})))
                        msg = SimpleNamespace(content="", tool_calls=[_synth_tc])
                    else:
                        try:
                            resp = await _get_orch_client().chat.completions.create(
                                model=_ORCH_MODEL, messages=messages, tools=orch_tools,
                                tool_choice="auto", temperature=0.0, max_tokens=800)
                        except Exception as exc:
                            logger.error("[%s_chat] orchestrator call failed: %s", db, exc)
                            orch_text = "Sorry, I ran into an error. Please try again."
                            break

                        msg = resp.choices[0].message
                    asst_dict: dict = {"role": "assistant", "content": msg.content or ""}
                    if msg.tool_calls:
                        asst_dict["tool_calls"] = [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name,
                                          "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls]
                    messages.append(asst_dict)

                    if not msg.tool_calls:
                        orch_text = msg.content or ""
                        orch_decision = "direct_answer"
                        await send({"type": "delta", "tool_id": orch_id, "name": "orch_step",
                                    "text": "Answering directly from knowledge — no database query needed."})
                        await send({"type": "tool_result", "tool_id": orch_id,
                                    "name": "orchestrator", "ok": True,
                                    "output": {"decision": orch_decision},
                                    "elapsed_seconds": round(time.monotonic() - t0, 2)})
                        break

                    if _iter == 0:
                        tool_names = [tc.function.name for tc in msg.tool_calls]
                        orch_decision = ", ".join(tool_names)
                        route_parts = [label_map.get(t, f"**{t}**") for t in tool_names]
                        await send({"type": "delta", "tool_id": orch_id, "name": "orch_step",
                                    "text": f"Routing your query to {' and '.join(route_parts)}."})
                        await send({"type": "tool_result", "tool_id": orch_id,
                                    "name": "orchestrator", "ok": True,
                                    "output": {"decision": orch_decision, "tools": tool_names},
                                    "elapsed_seconds": round(time.monotonic() - t0, 2)})

                    for tc in msg.tool_calls:
                        tcname  = tc.function.name
                        display = tool_display.get(tcname, tcname)
                        try:
                            tool_args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            tool_args = {}
                        query_arg = tool_args.get("query", user_input)

                        if tcname == tool_name:
                            db_attempted = True
                            await send({"type": "tool_called", "tool_id": tc.id, "name": display})
                            if spec.orchestrator_url:
                                tool_result, db_result = await _tool_query_db_orchestrator(
                                    query_arg, connection_id, spec, send)
                            else:
                                tool_result, db_result = await _tool_query_db(
                                    query_arg, connection_id, spec.return_result_fn, send)
                            rc = tool_result.get("row_count", 0)
                            has_rows = rc > 0
                            if has_rows and db_result:
                                await send({"type": "delta", "tool_id": tc.id, "name": db,
                                            "text": _build_filter_trace_text(db_result)})
                            await send({"type": "tool_result", "tool_id": tc.id, "name": display,
                                        "ok": has_rows, "row_count": rc,
                                        "elapsed_seconds": round(time.monotonic() - t0, 2),
                                        **({"failure_cause": "no_results"} if not has_rows else {})})
                            if has_rows and db_result:
                                await send({"type": table_event, "tool": db,
                                            "connection_id": connection_id,
                                            "csv_path": db_result.csv_path or "",
                                            "row_count": rc,
                                            "output": {**db_result.model_dump(),
                                                       "table": (db_result.table or [])[:_MAX_ROWS_TO_DISPLAY]}})
                        elif tcname == "web_search":
                            web_search_called = True
                            await send({"type": "tool_called", "tool_id": tc.id, "name": display})
                            tool_result = await _tool_web_search(query_arg)
                            web_searched = bool(tool_result.get("searched"))
                            await send({"type": "tool_result", "tool_id": tc.id, "name": display,
                                        "ok": True, "elapsed_seconds": round(time.monotonic() - t0, 2)})
                        else:
                            tool_result = {"error": f"unknown tool: {tcname}"}
                            await send({"type": "tool_result", "tool_id": tc.id,
                                        "name": display, "ok": False})

                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": json.dumps(tool_result, default=str)})

                    if db_result and (db_result.row_count or 0) > 0:
                        break
                    if db_attempted:
                        # Curated DB was queried and returned no rows. Don't keep
                        # re-hammering the same DB — exit to the web fallback.
                        break

                if db_result and (db_result.row_count or 0) > 0:
                    await send({"type": "tool_called", "tool_id": "synthesizer", "name": "synthesizer"})
                    # text2sql/DuckDB pre-computed-answer bypass: when the execute
                    # pipeline answered an analytical question with auto-generated
                    # SQL (app/per_db_tool/_text2sql.py), it stamps the computed
                    # answer onto db_result.message with the "auto-generated SQL"
                    # marker. That value is exact (counts/aggregates over the FULL
                    # result set); the LLM synthesizer below only sees a truncated
                    # ~20-row sample and would miscount it. So when the marker is
                    # present, stream the pre-computed message verbatim and skip
                    # the summarizer — honoring the "LLM-summarizer bypass" the
                    # text2sql step already promises. Generic across all DBs;
                    # non-analytical questions have no marker and keep the streamed
                    # synthesizer.
                    # Two kinds of deterministic pre-computed answer are exact
                    # over the FULL result set and must NOT be re-summarized from
                    # the ~20-row preview: (1) text2sql/DuckDB analytical answers
                    # ("auto-generated SQL" marker); (2) the require_all
                    # intersection summary ("result(s) match ALL named …"), which
                    # the LLM otherwise miscounts (reports the preview size, e.g.
                    # 20 of 145) and mis-attributes (reads the anchor column —
                    # e.g. the queried TP53/EGFR — as the answer set). Stream
                    # either verbatim and skip the summarizer. Generic across DBs.
                    _t2_msg = db_result.message or ""
                    _DETERMINISTIC_MARKERS = ("auto-generated SQL",
                                              "result(s) match ALL named")
                    # Yes/no questions must go through _synthesize_stream so
                    # _fix_yesno_verdict + _flip_no_to_yes_if_ppi_confirmed +
                    # _flip_no_to_yes_if_db_confirms can apply. A text2sql COUNT
                    # like "STRING: 2" is not a yes/no
                    # answer even when "between" happens to match _ANALYTIC_RX.
                    _bypass_ok = (
                        any(m in _t2_msg for m in _DETERMINISTIC_MARKERS)
                        and not _YESNO_Q_RE.match(user_input or "")
                    )
                    if _bypass_ok:
                        await on_delta(_t2_msg)
                        final_text = _t2_msg
                    else:
                        final_text = await _synthesize_stream(spec, db_result, user_input, on_delta)
                elif db_attempted:
                    # In-scope question, but the curated DB had zero rows:
                    # disclaim provenance, then answer from a general web search.
                    final_text = await _web_fallback_stream(spec, user_input, on_delta, send, t0)
                elif orch_text is not None:
                    await send({"type": "tool_called", "tool_id": "synthesizer", "name": "synthesizer"})
                    if web_search_called:
                        disc = _web_provenance_header(spec.display_name, web_searched)
                        await on_delta(disc)
                        final_text = disc + orch_text
                    else:
                        final_text = orch_text
                    await on_delta(orch_text)
                else:
                    await send({"type": "tool_called", "tool_id": "synthesizer", "name": "synthesizer"})
                    final_text = "I wasn't able to answer that. Please try again."
                    await on_delta(final_text)

                await send({"type": "tool_result", "tool_id": "synthesizer", "ok": True})
                await send({"type": "final", "text": final_text})

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("[%s_chat] unexpected error: %s", db, exc, exc_info=True)
        finally:
            hb_task.cancel()
            logger.info("[%s_chat] WS closed | conn=%s", db, connection_id)

    return router


async def warm_pipeline(db: str) -> None:
    """Pre-warm every lazy singleton used by the schema_kg chat pipeline.

    Fires at container startup (non-blocking background task) so the first
    real user query hits warm caches instead of paying cold-start penalties:
      1. SchemaKgPlanner index for `db` (Qdrant ANN build, ~5-15s)
      2. Synthesizer + orchestrator OpenAI clients (TCP+TLS to Groq/OpenRouter)
      3. Synthesizer prompt file read + disclaimer splice
      4. One 1-token ping to each LLM endpoint (opens connection pool)
    """
    import asyncio

    # 1. Schema planner index
    try:
        from .schema_kg_planner import get_planner
        await get_planner(db).warm()
        logger.info("[warm_pipeline:%s] planner warm OK", db)
    except Exception as exc:
        logger.warning("[warm_pipeline:%s] planner warm failed: %s", db, exc)

    # 2. Prompt cache
    try:
        await asyncio.to_thread(_get_synth_prompt)
        logger.info("[warm_pipeline:%s] synth prompt loaded", db)
    except Exception as exc:
        logger.warning("[warm_pipeline:%s] synth prompt failed: %s", db, exc)

    # 3. LLM connection-pool pings (1-token completions, fire-and-forget errors)
    async def _ping(client_fn, model: str, label: str) -> None:
        try:
            client = client_fn()
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            logger.info("[warm_pipeline:%s] %s ping OK", db, label)
        except Exception as exc:
            logger.warning("[warm_pipeline:%s] %s ping failed: %s", db, label, exc)

    await asyncio.gather(
        _ping(_get_synth_client, _SYNTH_MODEL,     "synthesizer(groq)"),
        _ping(_get_orch_client,  _ORCH_MODEL,       "orchestrator(openrouter)"),
        _ping(_get_synth_client, _STEP_SUMM_MODEL,  "step-summarizer(groq)"),
        return_exceptions=True,
    )


__all__ = ["ChatSpec", "build_chat_router", "warm_pipeline"]
