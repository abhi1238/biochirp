"""Read-only analytical SQL over a PRIOR OpenTargets result CSV (flag-gated).

`analyze_results(csv_path, question)` lets the agent compute counts / aggregates /
thresholds / group-bys / ratios / rankings over a table a previous tool already
produced (combine, traverse, disease_tool, target_tool, …) — instead of letting
the synthesizer do the arithmetic in prose (a fabrication risk). It asks a code
model for ONE read-only DuckDB SELECT, validates it, runs it on an in-memory copy
of the CSV, retries once on error, and returns the computed rows.

SQL generation is gated on actual analytical intent (`_ANALYTIC_RX`: count /
average / threshold / ratio / group-by / top-N, mirroring `_text2sql.py`'s
gate). The orchestrator's A3 list-branch also calls this tool for plain
"list/enumerate more rows" requests (to get past the 5-row SYNTHESIZER-HINT
cap) — those don't match the analytic gate, so no SQL is generated for them;
they're served directly from the CSV's existing row order instead, which is
already correctly ranked per entity type by the domain tool that produced it.
This keeps DuckDB SQL reserved for genuine analytical questions rather than
running on every call.

Why this is safe to ship:
  * DISABLED by default — the tool is only registered when TEXT2SQL[_OPENTARGETS]
    is set, so the default build is byte-identical (the tool does not exist).
  * Read-only — validation rejects anything but a single SELECT/WITH; the DuckDB
    file-reading table functions (read_csv/read_parquet/glob/…) are denied; the
    query runs only against the in-memory dataframe registered as `t`.
  * Fail-closed — every error path returns an {"ok": false, ...} JSON; it never
    raises, so it cannot crash a turn. duckdb is imported lazily inside the call.

This mirrors the per-DB `app/per_db_tool/_text2sql.py` step, adapted to OT's
"tools operate on a prior csv_path" pattern (like join_results_tool /
filter_targets_by_annotation).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import pandas as pd
from agents import function_tool

logger = logging.getLogger("uvicorn.error").getChild("opentargets.analyze")

# Gate: per-OT override TEXT2SQL_OPENTARGETS, else the shared TEXT2SQL flag.
ANALYZE_ENABLED = os.getenv(
    "TEXT2SQL_OPENTARGETS", os.getenv("TEXT2SQL", "0")
).strip().lower() in ("1", "true", "yes", "on")

# SQL-generation model. Default to the OT orchestrator model, which is a Groq
# model this service can actually reach (OT calls api.groq.com directly). NOTE:
# do NOT default to the per-DB TEXT2SQL_MODEL (qwen3-coder) — that lives on the
# LiteLLM gateway, not Groq, and 404s here. Override with OT_TEXT2SQL_MODEL only
# if it names a Groq-available model.
_MODEL = os.environ.get("OT_TEXT2SQL_MODEL") or os.environ.get("OT_ORCHESTRATOR_MODEL", "")
_MAX_RESULT_ROWS = int(os.getenv("OT_ANALYZE_MAX_RESULT_ROWS", "200"))

# Read-only guards (same denylists as _text2sql.py): no DDL/DML, no filesystem
# table-functions (DuckDB exposes the host FS through ordinary SELECT functions).
_FORBID = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|grant|truncate|"
    r"merge|call|install|load|set|export|import|use|describe|summarize)\b", re.I)
_FILE_FN = re.compile(
    r"\bread_\w+\s*\(|\bglob\s*\(|\bsniff_csv\s*\(|\bparquet_scan\s*\(", re.I)

# Analytical intent gate (mirrors _text2sql.py's _ANALYTIC_RX): only questions
# that actually need computation get a fresh LLM-authored SQL query. Plain
# "list / enumerate more rows" calls (the A3 list-branch's mandatory second
# call, used only to get past the 5-row SYNTHESIZER-HINT cap) do NOT match
# this and are served straight from the CSV's existing order below — which
# is already correctly ranked per entity type (score/phase) by the domain
# tool that produced it. This avoids the LLM inventing a fresh, semantically
# blind ORDER BY column (e.g. score_genetic_association) for a plain list.
_ANALYTIC_RX = re.compile(
    r"\b(how many|number of|count|average|avg|mean|median|sum|total|"
    r"maximum|max|minimum|min|highest|lowest|largest|smallest|biggest|"
    r"percentage|percent|fraction|ratio|top \d|"
    r"at least|at most|more than|less than|greater than|fewer than|above|below|between)\b|"
    r"(>=|<=|>|<)",
    re.I,
)
_ROWLIMIT_RX = re.compile(r"\b(?:top|first|up to)\s+(\d+)\b", re.I)
_DEFAULT_LIST_LIMIT = 25


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip().rstrip(";").strip()


def _validate(sql: str) -> bool:
    s = (sql or "").strip()
    if not re.match(r"(?is)^\s*(with|select)\b", s):
        return False
    if ";" in s.rstrip(";"):          # reject multiple statements
        return False
    if _FORBID.search(s) or _FILE_FN.search(s):
        return False
    return True


def _sys_prompt(cols: list) -> str:
    return (
        "You translate the user's question into exactly ONE read-only DuckDB SQL "
        "query over a single table named `t`.\n"
        f"`t` has these columns: {', '.join(cols)}.\n"
        "Rules: output ONE statement only — a SELECT (optionally a leading WITH); "
        "no INSERT/UPDATE/DELETE/CREATE/etc.; no semicolons; no file-reading "
        "functions; reference only table `t` and the columns listed. "
        "Score/phase columns may be strings — CAST to DOUBLE for numeric "
        "comparisons. Return ONLY the SQL, with no prose and no code fences."
    )


async def _gen_sql(question: str, cols: list, prev_err: Optional[str] = None) -> str:
    from openai import AsyncOpenAI
    from config import settings
    user = question if not prev_err else (
        f"{question}\n\nThe previous SQL failed with: {prev_err}\n"
        "Return a corrected single read-only SELECT.")
    client = AsyncOpenAI(base_url="https://api.groq.com/openai/v1",
                         api_key=settings.get_groq_key("opentargets"))
    resp = await client.chat.completions.create(
        model=_MODEL, temperature=0,
        messages=[{"role": "system", "content": _sys_prompt(cols)},
                  {"role": "user", "content": user}],
    )
    return _strip_fence(resp.choices[0].message.content or "")


@function_tool(
    strict_mode=False,
    name_override="analyze_results",
    description_override=(
        "Compute an ANALYTICAL answer over a PRIOR result table by its csv_path — "
        "counts, aggregates (avg/sum/min/max), thresholds (e.g. score > 0.5), "
        "group-by / distributions, ratios, or rankings that the row-returning tools "
        "do not compute themselves. Pass the csv_path from a previous tool result "
        "plus a natural-language analytical question, e.g. 'how many have "
        "score_genetic_association above 0.5', 'count approved vs investigational by "
        "phase', 'average association_score by datatype', 'top 10 rows by phase'. "
        "Read-only SQL over the existing rows. NOT for fetching new data — run a data "
        "tool first to obtain a csv_path. Also used for plain 'list all rows sorted "
        "descending, return up to N' enumeration requests — those skip SQL generation "
        "and are served directly in the table's existing (already correctly ranked) "
        "order, so no sort column needs to be named for a plain list."
    ),
)
async def analyze_results(csv_path: str, question: str,
                          connection_id: Optional[str] = None) -> str:
    if not ANALYZE_ENABLED:
        return json.dumps({"ok": False, "error": "analyze_results is disabled"})
    try:
        if not csv_path or not os.path.exists(csv_path):
            return json.dumps({"ok": False, "error": f"result table not found: {csv_path}"})
        df = pd.read_csv(csv_path)
        if df.empty:
            return json.dumps({"ok": False, "error": "result table is empty — nothing to analyze"})
        cols = [str(c) for c in df.columns]

        if not _ANALYTIC_RX.search(question or ""):
            # Not an analytical question (no count/aggregate/threshold/ratio
            # intent) — this is a plain enumeration ask. Skip SQL generation
            # entirely and serve more rows straight from the CSV's existing
            # order (already ranked correctly by the domain tool).
            m = _ROWLIMIT_RX.search(question or "")
            limit = min(int(m.group(1)), _MAX_RESULT_ROWS) if m else _DEFAULT_LIST_LIMIT
            rows = df.head(limit).where(pd.notna(df.head(limit)), None)
            return json.dumps({
                "ok": True, "sql": None, "row_count": int(len(df)),
                "rows": rows.to_dict(orient="records"),
                "is_truncated": bool(len(df) > limit),
                "source_rows": int(len(df)),
                "note": "non-analytical question: served in existing table order, no SQL generated",
            }, default=str)

        import duckdb  # lazy: keeps service import safe even if duckdb were absent
        con = duckdb.connect()
        try:
            con.register("t", df)
            err = None
            for _ in range(2):  # one retry, feeding the error back
                sql = await _gen_sql(question, cols, err)
                if not _validate(sql):
                    err = f"rejected non read-only / multi-statement SQL: {sql[:160]}"
                    continue
                try:
                    res = con.execute(sql).fetchdf()
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    continue
                rows = res.head(_MAX_RESULT_ROWS).where(pd.notna(res.head(_MAX_RESULT_ROWS)), None)
                return json.dumps({
                    "ok": True, "sql": sql, "row_count": int(len(res)),
                    "rows": rows.to_dict(orient="records"),
                    "is_truncated": bool(len(res) > _MAX_RESULT_ROWS),
                    "source_rows": int(len(df)),
                }, default=str)
            return json.dumps({"ok": False, "error": f"no valid SQL answer after retry: {err}"})
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — fail-closed, never raise
        logger.warning("[analyze_results] failed: %s", exc)
        return json.dumps({"ok": False, "error": f"analyze failed: {type(exc).__name__}: {exc}"})
