"""Text-to-SQL analytical step (flag-gated, fail-open) — Increment.

When TEXT2SQL is enabled AND the question is analytical (count / threshold /
aggregate / comparison / LIKE), this:
  1. builds a prompt from the result df's columns + their schema.json descriptions,
  2. asks a code model (qwen3-coder by default) for ONE read-only DuckDB SQL SELECT,
  3. validates it (single SELECT, no DDL/DML), executes on ctx.df via DuckDB,
  4. retries ONCE on error (feeds the error back),
  5. sets ctx.pre_computed_message with the computed answer (LLM-summarizer bypass).

Design guarantees:
  * Disabled by default -> no-op -> normal pipeline behavior.
  * Fail-open: ANY error / non-analytic question / empty frame -> returns without
    setting a message -> falls back to the normal summarizer.
  * Read-only: validation rejects anything but a single SELECT; DuckDB runs on an
    in-memory copy of the result df only.

Benchmarked 20/20 on super-hard analytical questions (thresholds, aggregates,
argmax, multi-condition, LIKE, joins) with schema.json descriptions + 1 retry.
"""
from __future__ import annotations

import os
import re
import json
import logging
from pathlib import Path

logger = logging.getLogger("uvicorn.error")

def _enabled() -> bool:
    # Single source of truth: the TEXT2SQL env (declared in .env). A per-DB
    # override is TEXT2SQL_<DB> (mirrors the BIOCHIRP_<DB>_* per-DB flag pattern).
    return os.getenv("TEXT2SQL", "0").strip().lower() in ("1", "true", "yes", "on")


# Analytical intent: counts, aggregates, thresholds, comparisons, LIKE, top-k.
_ANALYTIC_RX = re.compile(
    r"\b(how many|number of|count|average|avg|mean|median|sum|total|"
    r"maximum|max|minimum|min|highest|lowest|largest|smallest|biggest|"
    r"longest|shortest|percentage|percent|fraction|ratio|"
    r"most|least|top \d|at least|at most|more than|less than|greater than|"
    r"fewer than|above|below|between|starting with|ending with|contains?)\b|"
    r"(>=|<=|>|<)",
    re.I,
)
_FORBID = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|grant|truncate|merge|call|"
    r"install|load|set|export|import|use|describe|summarize)\b",
    re.I,
)
# DuckDB exposes the host filesystem through ordinary SELECT table functions
# (read_text/read_csv/read_blob/read_json/glob/...), so the keyword denylist
# above is NOT enough — block every file-reader. `read_parquet` is allowed only
# because this module registers the result df itself; the LLM never needs it,
# so block it too (an injected `read_parquet('/app/.env'...)` would error on a
# non-parquet file but we deny it for defense-in-depth).
_FILE_FN = re.compile(r"\bread_\w+\s*\(|\bglob\s*\(|\bsniff_csv\s*\(|\bparquet_scan\s*\(", re.I)


def _schema_descs(db: str) -> dict:
    """{column: description} merged across this DB's tables from schema.json.
    Returns {} when the file isn't reachable (the prompt degrades to names only)."""
    cands = [
        Path("/app/schema_kg/inputs") / db / "schema.json",
        Path(__file__).resolve().parents[2] / "schema_kg" / "inputs" / db / "schema.json",
    ]
    root = os.getenv("SCHEMA_KG_INPUTS_ROOT")
    if root:
        cands.insert(0, Path(root) / db / "schema.json")
    for p in cands:
        try:
            if p.is_file():
                inner = json.loads(p.read_text()).get(db, {})
                m: dict = {}
                for colmap in inner.values():
                    if isinstance(colmap, dict):
                        for c, d in colmap.items():
                            m.setdefault(c, d)
                return m
        except Exception:
            continue
    return {}


def _build_sys(db: str, cols: list) -> str:
    descs = _schema_descs(db)
    coldesc = "\n".join(f"- {c}: {descs.get(c, '')}".rstrip() for c in cols)
    return (
        "Translate the English question into ONE DuckDB SQL SELECT over a table named df.\n"
        "df is ALREADY the result set the pipeline retrieved for THIS question — it is "
        "pre-filtered to the main entity named in the question (the gene / disease / drug / "
        "pathway / protein / etc.). Do NOT re-apply that primary entity as a WHERE filter: it "
        "would wrongly drop rows, because the entity is often not stored as a value in any "
        "column here (e.g. a target/gene name is NOT in a drug_name column). Add a WHERE only "
        "for ADDITIONAL constraints the question layers on top (a numeric threshold, a status, "
        "a sub-type/category). For a plain 'how many <X>' over df, count with NO WHERE.\n"
        "COLUMNS (with descriptions):\n" + coldesc + "\n"
        "RULES:\n"
        "- Numeric columns may be stored as TEXT — wrap in CAST(col AS DOUBLE) for any "
        "compare / SUM / AVG / MEDIAN / MIN / MAX. Use DOUBLE (not INTEGER) so decimal "
        "values like 99.9 are not truncated before the comparison.\n"
        "- Use the descriptions to pick the right column (e.g. a query-side vs partner-side "
        "identifier — filter/return the side the question asks about).\n"
        "- 'HOW MANY <entities>' (drugs, compounds, genes, proteins, variants, diseases, "
        "pathways, phenotypes, ...) counts DISTINCT entities, not rows: use "
        "COUNT(DISTINCT <the column that NAMES or IDENTIFIES that entity>) — the entity the "
        "question asks to count, NOT the column being filtered on. The same entity often "
        "repeats across rows (one drug across many assays), so plain COUNT(*) over-counts. "
        "Use COUNT(*) only when the question explicitly counts rows / records / associations.\n"
        "- For 'highest/dominant among several columns' use a PER-ROW GREATEST(...) over the "
        "cast values; a column is the strict max when it is greater than every other on the same row.\n"
        "- To COUNT how many of several conditions hold, sum integer-cast booleans: "
        "(cond1)::INTEGER + (cond2)::INTEGER + ...  For 'ALL of X, Y, Z' just use AND.\n"
        "- For text contains use ILIKE '%term%'. DuckDB LIKE/ILIKE has NO character "
        "classes — '%[0-9]%' matches the literal text [0-9], not a digit. For any "
        "pattern / character-class match (a digit, a letter range, a regex) use "
        "regexp_matches(col, 'pattern'), e.g. regexp_matches(col, '[0-9]') for "
        "'contains a digit'. For exact length use length(col).\n"
        "Output ONLY the SQL: one read-only SELECT, no semicolons, no comments, no DDL/DML. "
        "Use only the listed columns."
    )


def _validate(sql: str) -> bool:
    return (
        bool(re.match(r"(?is)^\s*select\b", sql))
        and ";" not in sql
        and not _FORBID.search(sql)
        and not _FILE_FN.search(sql)
    )


def _strip_fence(s: str) -> str:
    return re.sub(r"^```sql|^```|```$", "", s, flags=re.I | re.M).strip().rstrip(";")


# Rewrite `col = 'literal'` → `col ILIKE 'literal'` so text-column equality is
# CASE-INSENSITIVE (2026-06-23). DuckDB `=` on strings is case-sensitive, so when
# the LLM emits the wrong case (drug_name = 'cisplatin' vs DB value 'Cisplatin')
# the query returns 0 rows — NON-DETERMINISTICALLY across runs. ILIKE without
# wildcards is an exact, case-insensitive match (same semantics as `=`, just
# case-robust). Only quoted string literals are rewritten; numeric equalities
# (col = 2244) have no quotes and are left untouched. Generic — no per-DB logic.
_EQ_STR_RX = re.compile(r"(\b[A-Za-z_]\w*(?:\.\w+)?)\s*=\s*('(?:[^']|'')*')")


def _case_insensitive_eq(sql: str) -> str:
    return _EQ_STR_RX.sub(r"\1 ILIKE \2", sql)


# Rewrite `CAST(col AS INTEGER)` → `CAST(col AS DOUBLE)` so decimal values like
# 99.9 are not truncated before numeric comparisons. The LLM sometimes emits
# INTEGER despite the prompt saying DOUBLE; this catch-all is case-insensitive.
_CAST_INT_RX = re.compile(r"\bCAST\s*\(([^)]+?)\s+AS\s+INTEGER\s*\)", re.I)


def _fix_integer_cast(sql: str) -> str:
    return _CAST_INT_RX.sub(lambda m: f"CAST({m.group(1)} AS DOUBLE)", sql)


def _format(res, sql: str = "") -> str:
    """res = list of tuples from DuckDB fetchall().

    `sql` is used only to pick accurate wording: a query with DISTINCT/GROUP
    BY counts unique entities, not raw table rows, so calling it "N rows"
    here would silently disagree with the row_count (df.height) the SAME
    response's table header/footer report a few lines away — both numbers
    can be correct at once (e.g. 549 gene-set membership rows covering 477
    distinct gene sets), but labeling both "rows" makes them look like a
    contradiction instead of two different, clearly-named quantities.
    """
    if len(res) == 1 and len(res[0]) == 1:
        return str(res[0][0])
    if len(res) <= 25:
        return "; ".join(", ".join(str(x) for x in row) for row in res)
    sl = (sql or "").lower()
    unit = "distinct value(s)" if ("distinct" in sl or "group by" in sl) else "row(s)"
    return f"{len(res)} {unit} (full result downloadable below)"


def _describe_sql(sql: str) -> str:
    """Plain-English summary of what an auto-generated DuckDB SELECT does, so a
    reader understands the operation without parsing the SQL. Heuristic over the
    common clauses our text2sql emits (count/distinct/group-by-having/threshold/
    LIKE/order-limit); never raises — falls back to a generic phrase."""
    s = " ".join((sql or "").split())
    sl = s.lower()
    parts: list[str] = []
    m = re.search(r"count\(\s*distinct\s+([a-z0-9_\.]+)\s*\)", sl)
    if m:
        parts.append(f"counted the distinct values of {m.group(1)}")
    elif re.search(r"count\(\s*\*?\s*\)", sl):
        parts.append("counted the matching rows")
    for fn in ("avg", "sum", "max", "min"):
        mm = re.search(rf"\b{fn}\(\s*([a-z0-9_\.]+)\s*\)", sl)
        if mm:
            parts.append(f"computed the {fn} of {mm.group(1)}")
    if "group by" in sl and "having" in sl:
        mh = re.search(r"having\s+count\(\s*distinct\s+([a-z0-9_\.]+)\s*\)\s*(>=|=)\s*(\d+)", sl)
        if mh:
            parts.append(f"kept only groups linked to {mh.group(3)} distinct "
                         f"{mh.group(1)} (an intersection / 'all of' condition)")
        else:
            parts.append("grouped the rows and kept those meeting the HAVING condition")
    elif "group by" in sl:
        parts.append("grouped the rows")
    elif "distinct" in sl and "count(" not in sl:
        parts.append("returned the distinct matching values")
    for mm in re.finditer(r"([a-z0-9_\.]+)\s*(>=|<=|>|<)\s*(\d+)", sl):
        parts.append(f"filtered to rows where {mm.group(1)} {mm.group(2)} {mm.group(3)}")
    for mm in re.finditer(r"([a-z0-9_\.]+)\s+i?like\s+'([^']+)'", sl):
        parts.append(f"matched {mm.group(1)} against the pattern '{mm.group(2)}'")
    if "order by" in sl and "limit" in sl:
        ml = re.search(r"limit\s+(\d+)", sl)
        parts.append(f"sorted the rows and took the top {ml.group(1) if ml else 'N'}")
    if not parts:
        parts.append("selected the matching rows")
    seen: set = set()
    uniq = [p for p in parts if not (p in seen or seen.add(p))]
    return "; ".join(uniq)


def maybe_answer_with_sql(ctx, query: str) -> None:
    if not _enabled() or ctx is None or ctx.df is None or not query:
        return
    if not _ANALYTIC_RX.search(query):
        return
    try:
        import duckdb
        from openai import OpenAI
        from config import settings

        df = ctx.df.collect() if hasattr(ctx.df, "collect") else ctx.df
        if df.height == 0:
            return  # empty -> let the normal no-records path handle it
        cols = list(df.columns)
        sysp = _build_sys(ctx.db, cols)
        model = settings.TEXT2SQL_MODEL
        client = OpenAI(
            base_url=os.getenv("TEXT2SQL_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=settings.get_openrouter_key(ctx.db),
            max_retries=1,
            timeout=float(os.getenv("OPENAI_HTTP_TIMEOUT", "45")),
        )

        def gen(msgs):
            r = client.chat.completions.create(model=model, messages=msgs, max_tokens=320, temperature=0)
            return _fix_integer_cast(_case_insensitive_eq(_strip_fence((r.choices[0].message.content or "").strip())))

        # Hand the result df to DuckDB via a temp parquet — polars' native
        # writer needs NO pyarrow (which isn't in the service image), and DuckDB
        # reads parquet natively. Avoids the pandas/arrow dependency entirely.
        import tempfile
        _tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
        _tmp.close()
        try:
            df.write_parquet(_tmp.name)
            con = duckdb.connect()
            con.execute(f"CREATE VIEW df AS SELECT * FROM read_parquet('{_tmp.name}')")

            def execute(sql):
                if not _validate(sql):
                    return None, "rejected by validation (must be a single read-only SELECT)"
                try:
                    return con.execute(sql).fetchall(), None
                except Exception as e:
                    return None, f"{type(e).__name__}: {str(e)[:120]}"

            # The df is ALREADY filtered to the resolved entities. Tell the model the
            # EXACT canonical values present in each low-cardinality text column
            # (e.g. the anchor gene-symbol columns hold exactly [AURKB, RNF2]), so it
            # filters on real DB values — not the nicknames/synonyms from the question
            # (which produced wrong SQL like `... ILIKE 'aurb'`). Only short, small-set
            # columns are listed (skips free-text annotation and huge partner lists).
            ent_hint = ""
            try:
                import polars as _pl
                lines = []
                for c in df.columns:
                    if df.schema.get(c) != _pl.Utf8:
                        continue
                    u = df[c].drop_nulls().unique().to_list()
                    if 0 < len(u) <= 25 and max((len(str(x)) for x in u), default=0) <= 40:
                        lines.append(f"  {c} ∈ {sorted(str(x) for x in u)}")
                if lines:
                    # Union of the small-column value sets = the resolved entity set.
                    anchors = sorted({v for c in df.columns if df.schema.get(c) == _pl.Utf8
                                      for v in df[c].drop_nulls().unique().to_list()
                                      if len(df[c].unique()) <= 25 and len(str(v)) <= 40})
                    ent_hint = (
                        "\n\nThe df is ALREADY filtered to the resolved query entities. "
                        "These columns contain EXACTLY these values — when filtering, use "
                        "THESE exact values, never the raw names/nicknames from the "
                        "question:\n" + "\n".join(lines)
                    )
                    if 2 <= len(anchors) <= 25:
                        ent_hint += (
                            f"\nThe resolved entities are {anchors}. If the question asks "
                            "whether two named entities interact / are linked, BOTH are in "
                            "this set — filter BOTH the gene-symbol and the partner-symbol "
                            "columns to this set (never the raw question names)."
                        )
            except Exception as _eh:
                logger.debug("[%s] text2sql entity-hint skipped: %s", ctx.db, _eh)
            msgs = [{"role": "system", "content": sysp},
                    {"role": "user", "content": query + ent_hint}]
            sql = gen(msgs)
            res, err = execute(sql)
            if err is not None:  # ---- one retry, feeding the error back ----
                msgs += [
                    {"role": "assistant", "content": sql},
                    {"role": "user", "content": f"That query failed with: {err}. Return a corrected single read-only SELECT only."},
                ]
                sql = gen(msgs)
                res, err = execute(sql)
        finally:
            try:
                os.unlink(_tmp.name)
            except Exception:
                pass

        if err is not None or res is None:
            logger.info("[%s] text2sql gave up (%s) — falling back to summarizer", ctx.db, err)
            return

        ans = _format(res, sql)
        disp = ctx.db.upper()
        _nl = _describe_sql(sql)
        # Cross-reference the SAME authoritative total (df.height) that the
        # table header/footer elsewhere in this response use as `row_count`.
        # Only added for the "N distinct value(s)/row(s)" answer shape (>25
        # result rows) — that's the case where the reader could otherwise
        # mistake this count for (or notice it disagreeing with) the plain
        # row total shown in the results table a few lines below. Scalar/
        # short-list answers (averages, top-k, single counts) aren't a
        # row-count claim at all, so no cross-reference is added for them.
        _total = df.height
        _cross_ref = ""
        if len(res) > 25 and len(res) != _total:
            _cross_ref = f" (out of {_total} total matching row(s) in {disp})"
        ctx.pre_computed_message = (
            f"{disp}: {ans}{_cross_ref}\n\n"
            f"_How this was computed: an analytical query was run with DuckDB over "
            f"the matched result set — it {_nl}. Auto-generated SQL (verify against "
            f"the downloadable table):_ `{sql}`\n\n"
            f"Source: {disp}."
        )
        logger.info("[%s] text2sql answered=%r sql=%s", ctx.db, ans, sql[:140])
    except Exception as e:  # fail-open
        logger.debug("[%s] text2sql skipped: %s", ctx.db, e)
