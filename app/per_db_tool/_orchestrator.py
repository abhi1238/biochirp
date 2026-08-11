"""Shared per-DB worker orchestration.

Every `app/tools/<db>/app/<db>.py` worker walked the same pipeline:

  load DB → canonicalize parsed_value → POST expand_and_match_db →
  POST planner → publish_planner_step → join_and_filter_database →
  write CSV → publish_ws → build QueryState → finalize_db_result

with 1–5 stage-specific tweaks per DB. The orchestration boilerplate
(~90 lines) was duplicated across all workers.

`execute_db_query()` runs that pipeline once. Per-DB logic plugs in via
four optional hooks that mutate a shared `WorkerCtx`:

  pre_expand    — after parsed_value canonicalization, before expand POST
  post_expand   — after expand response is parsed, before planner POST
  post_planner  — after planner response is parsed, before join
  pre_join      — last chance to mutate filter_val / out_cols / plan

Each hook is `async def hook(ctx: WorkerCtx) -> None`. A hook may set
`ctx.error_msg` to short-circuit the rest of the pipeline (the same way
inline `if not error_msg:` chains worked in the legacy workers).

Async-post workers (ttd / ctd / hcdt) opt in via
`use_async_post=True`; the rest use the sync `post_with_retry` shim.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

import polars as pl

from config import settings  # repo-wide model SSOT (reads .env); never os.environ for models
from config.guardrail import DatabaseTable, QueryInterpreterOutputGuardrail
from config.schema import database_schemas
from utils.dataframe_filtering import join_and_filter_database, NoFilterTermsError
from utils.preprocess import _csv_path

from ._finalize import QueryState, finalize_db_result
from ._worker_helpers import (
    post_with_retry,
    post_async,
    publish_planner_step,
    publish_ws,
    valid_columns,
)
from .schema_kg_chat import _llm_summarize_step, _build_step_data_text
from utils._row_relevance import score_and_sort as _score_and_sort  # noqa: F401 — imported here so the module-level pre-warm thread fires at startup


Hook = Callable[["WorkerCtx"], Union[None, Awaitable[None]]]

# Multi-condition INTERSECTION intent ("both X and Y", "all of A, B and C",
# "X as well as Y"). When present AND a parsed_value field carries ≥2 distinct
# entities, the OUTPUT entity must be linked to ALL of them (set intersection),
# not just one (the default `is_in`/OR behaviour). We detect the intent here and
# stash the field→N(original entities) map in ctx.plan["require_all_fields"];
# join_and_filter_database applies a generic group-by-having pass. Phrases are
# explicit-intersection only — a bare "and"/comma list stays OR (union), the
# safe default. Generic across all DBs, no per-DB/per-entity hardcoding.
_INTERSECTION_RX = re.compile(
    r"\b(both|all of|each of|as well as|simultaneously|at the same time|"
    r"in common|shared by|shared between|common to|in both|overlap between|"
    r"common between|genes shared|shared genes)\b",
    re.I,
)


@dataclass
class WorkerCtx:
    """Mutable pipeline state passed to every hook.

    Hooks read and mutate fields in place. Setting `error_msg` short-circuits
    the rest of the pipeline (downstream stages skip).
    """
    db: str
    display_name: str
    input: QueryInterpreterOutputGuardrail
    connection_id: Optional[str]
    log: logging.Logger

    # Populated by orchestrator stages in order:
    data: Any = None
    inp: dict = field(default_factory=dict)              # input.model_dump() copy
    expand_response: Optional[dict] = None               # raw JSON from expand_and_match_db
    expand_for_planner: Optional[dict] = None            # override payload sent to planner
    filter_val: dict = field(default_factory=dict)
    out_cols: list = field(default_factory=list)
    plan: Optional[dict] = None
    df: Optional[pl.DataFrame] = None
    filter_stats: list = field(default_factory=list)
    csv_path: str = ""
    preview: list = field(default_factory=list)
    error_msg: Optional[str] = None

    # Bypass the per-DB LLM summarizer in finalize_db_result by setting this
    # to a non-None string. Used by ttd / ctd / hcdt when their
    # BIOCHIRP_<DB>_WORKER_LLM_SUMMARY env var is unset/0 — the multi-DB
    # front-door synthesizer is the only LLM call on that path.
    pre_computed_message: Optional[str] = None

    # Per-DB scratch: pre_expand can stash flags read later by post_expand /
    # post_planner / pre_join (e.g. per-DB preserved dict or identifier flags).
    extras: dict = field(default_factory=dict)

    # Optional async callable for emitting intermediate step events to the WS
    # client (tool_called / delta / tool_result). Hooks call this to surface
    # schema_mapper, expand_and_match, and execute progress without requiring
    # a Redis/orchestrator relay. No-op when None (headless / test callers).
    ws_send: Optional[Callable] = None

    def strip_unsupported_fields(self) -> None:
        """Null out filter fields that aren't columns in this DB's schema, then
        recompute ``out_cols``.

        This was copy-pasted byte-for-byte into ~half a dozen per-DB
        ``_post_expand`` hooks: compute ``schema_cols`` from
        ``database_schemas[self.db]``, null any filter key whose value is a
        *real* value (not None / "requested") but whose column doesn't exist in
        the schema, in BOTH ``filter_val`` and the expand response's ``value``
        dict (usually the same object), then set
        ``out_cols = valid_columns(filter_val, db)``.

        No-op (apart from recomputing out_cols) when ``expand_response`` has no
        ``value`` — matching the early-return the inline blocks used.
        """
        ev = self.expand_response.get("value") if self.expand_response else None
        if ev is not None:
            schema_cols = {c for tbl in (database_schemas.get(self.db) or {}).values() for c in tbl}
            for k in list(ev.keys()):
                if k not in schema_cols and ev.get(k) not in (None, "requested"):
                    ev[k] = None
                    self.filter_val[k] = None
        self.out_cols = valid_columns(self.filter_val, self.db)


def _literal_floor_filter(parsed_value: dict) -> dict:
    """Worker-level literal-term floor (2026-06-23).

    Builds a filter directly from the user's literal terms in ``parsed_value``
    (the planner's extracted entities), bypassing synonym expansion. Used as a
    fallback when the expand_and_match SERVICE is unreachable / times out / returns
    malformed JSON: rather than aborting the query with an error (→ 0 rows → web
    fallback even though the literal term would have matched), we filter on what
    the user actually typed. Downstream DB matching is case-insensitive, so e.g.
    drug_name=["cisplatin"] still resolves to "Cisplatin". Synonyms are lost for
    that one query, but the canonical term always resolves — graceful degradation
    instead of failure. Generic across all DBs; no per-entity hardcoding.
    """
    fv: dict = {}
    for k, v in (parsed_value or {}).items():
        if isinstance(v, list):
            terms = [t.strip() for t in v
                     if isinstance(t, str) and t.strip() and t.strip().lower() != "requested"]
            if terms:
                fv[k] = terms
        elif v == "requested":
            fv[k] = "requested"
    return fv


async def _call_hook(hook: Optional[Hook], ctx: WorkerCtx) -> None:
    """Run a hook if provided; tolerate both sync and async hook bodies."""
    if hook is None:
        return
    res = hook(ctx)
    if asyncio.iscoroutine(res):
        await res


async def _post_either(url: str, *, use_async: bool, json: dict,
                       logger: logging.Logger):
    """Single POST that handles both the sync (requests) and async (httpx)
    helpers. Returns the Response on success or None on failure."""
    if use_async:
        return await post_async(url, json=json, logger=logger)
    # Sync helper blocks the event loop. Wrap in to_thread so the orchestrator
    # stays cooperative when an async hook is awaiting other work.
    return await asyncio.to_thread(post_with_retry, url, json=json, logger=logger)


def _apply_sort_order(df, sort_order):
    """STABLE re-sort `df` by a declarative per-DB `sort_order`, applied ON TOP
    of the existing (relevance) order so within-tier order is preserved.

    Each key is a dict:
      {"col": <name>, "dir": "asc"|"desc"}        numeric / lexical sort
      {"col": <name>, "order": [v1, v2, ...]}     categorical value-priority
                                                  (earlier value ranks first)
      {"col": <name>, "contains_first": [s1, ..]} substring priority — rows whose
                                                  column contains ANY substring rank
                                                  first (e.g. interaction_actions
                                                  containing 'binding'/'activity' =
                                                  the mechanistic target, above
                                                  downstream 'expression' effects)
    Keys whose column is absent from `df` are skipped — so a DB can declare a
    rich order and each query honours only the columns it actually returned.
    Generic and DB-agnostic; the caller passes the per-DB spec. Returns `df`
    unchanged when nothing applied (no matching columns)."""
    import polars as pl
    if df is None or df.is_empty():
        return df
    by, descending, tmp = [], [], []
    for key in (sort_order or []):
        col = key.get("col")
        if not col or col not in df.columns:
            continue
        if key.get("order"):
            rank = {str(v): i for i, v in enumerate(key["order"])}
            miss = len(rank)
            rcol = f"__so_{len(tmp)}"
            df = df.with_columns(
                pl.col(col).cast(pl.Utf8)
                  .map_elements(lambda v, r=rank, m=miss: r.get(v, m),
                                return_dtype=pl.Int32)
                  .alias(rcol))
            by.append(rcol); descending.append(False); tmp.append(rcol)
        elif key.get("contains_first"):
            _pat = "|".join(str(s) for s in key["contains_first"])
            rcol = f"__so_{len(tmp)}"
            df = df.with_columns(
                pl.when(pl.col(col).cast(pl.Utf8).str.contains("(?i)" + _pat))
                  .then(0).otherwise(1).alias(rcol))
            by.append(rcol); descending.append(False); tmp.append(rcol)
        else:
            by.append(col)
            descending.append(str(key.get("dir", "asc")).lower() == "desc")
    if not by:
        return df
    df = df.sort(by=by, descending=descending, nulls_last=True, maintain_order=True)
    return df.drop(tmp) if tmp else df


async def execute_db_query(
    *,
    input: QueryInterpreterOutputGuardrail,
    connection_id: Optional[str],
    db: str,
    display_name: str,
    get_db: Callable[[], Any],
    prompt_md: str,
    summarizer_model: Optional[str] = None,
    head_view_rows: Optional[int] = None,
    use_async_post: bool = False,
    pre_expand: Optional[Hook] = None,
    post_expand: Optional[Hook] = None,
    post_planner: Optional[Hook] = None,
    pre_join: Optional[Hook] = None,
    # An intercept hook may bypass the expand+planner round-trips entirely by
    # setting `ctx.plan`, `ctx.filter_val`, and `ctx.out_cols` itself. Used for
    # explicit cross-reference or DB-id queries where the answer is a
    # single-table lookup and the LLM expand step would only destroy the
    # literal ID. When the hook leaves `ctx.plan` None the orchestrator runs
    # the normal expand+planner pipeline.
    intercept: Optional[Hook] = None,
    # Last-resort fallback when the join produced zero rows. Hook may populate
    # `ctx.df` from an alternate table (e.g. per-DB fallback to an alternate
    # association table when the primary lookup returns no rows). If `ctx.df`
    # is non-empty afterwards, the orchestrator clears `error_msg` and
    # continues with that frame.
    on_empty_result: Optional[Hook] = None,
    # Optional per-DB declarative result ordering (list of {col, dir|order}).
    # When set, applied as a STABLE re-sort after relevance scoring so curated
    # columns (e.g. CTD evidence tier) rank above semantic relevance, which is
    # preserved within ties. None → pure relevance order (unchanged behaviour).
    sort_order: Optional[list] = None,
    # Runs after a successful join, before CSV write + finalize. Workers use
    # this to populate `ctx.pre_computed_message` (skipping the per-DB LLM
    # summarizer) or to project columns on the resulting frame.
    post_join: Optional[Hook] = None,
    # Default to None and resolve from env at call time — keeps the kwarg
    # defaults out of the docker-compose-coupled URL space at import time
    # and lets a per-DB worker or test override either source.
    expand_url: Optional[str] = None,
    planner_url: Optional[str] = None,
    ws_send: Optional[Callable] = None,
) -> DatabaseTable:
    """Run the canonical per-DB worker pipeline. Hooks mutate `WorkerCtx` to
    customize per-DB behavior; everything else (HTTP POSTs, error chaining,
    CSV write, planner-card publish, QueryState build, finalize_db_result)
    is shared.
    """
    log = logging.getLogger("uvicorn.error")
    if summarizer_model is None:
        summarizer_model = settings.SUMMARIZER_MODEL_NAME
    if head_view_rows is None:
        head_view_rows = int(os.getenv("HEAD_VIEW_ROW_COUNT", "50"))
    if expand_url is None:
        # Honour an explicit full-URL override first (useful for tests / a
        # load balancer); otherwise compose from the host/port env vars the
        # x-db-tool-env compose anchor already exports for every per-DB
        # service.
        expand_url = os.getenv("EXPAND_TOOL_URL") or (
            f"http://{os.getenv('EXPAND_AND_MATCH_DB_HOST', 'biochirp_expand_and_match_db_tool')}"
            f":{os.getenv('EXPAND_AND_MATCH_DB_PORT', '8009')}/expand_and_match_db"
        )
    if planner_url is None:
        planner_url = os.getenv("PLANNER_TOOL_URL") or (
            f"http://{os.getenv('PLANNER_HOST', 'biochirp_planner_tool')}"
            f":{os.getenv('PLANNER_PORT', '8011')}/planner"
        )

    ctx = WorkerCtx(
        db=db, display_name=display_name,
        input=input, connection_id=connection_id, log=log,
        df=pl.DataFrame(), ws_send=ws_send,
    )

    # 1. Load DB.
    try:
        ctx.data = get_db()
    except Exception as e:
        ctx.error_msg = f"Failed to load {display_name} DB: {e}"

    # 2. Build inp dict + canonicalize parsed_value.
    if not ctx.error_msg:
        try:
            ctx.inp = input.model_dump(exclude_none=True)
            try:
                from utils.parsed_value_canonical import canonicalize as _canon
                if isinstance(ctx.inp.get("parsed_value"), dict):
                    ctx.inp["parsed_value"] = _canon(ctx.inp["parsed_value"], db=db)
            except Exception:
                pass
        except Exception:
            ctx.error_msg = "Invalid input format."

    # 3. pre_expand hook — DB-specific parsed_value massaging.
    if not ctx.error_msg:
        await _call_hook(pre_expand, ctx)

    # 3b. Intercept hook — may bypass expand+planner by populating
    #     ctx.plan / ctx.filter_val / ctx.out_cols directly.
    if not ctx.error_msg and intercept is not None:
        await _call_hook(intercept, ctx)

    # 4. POST expand_and_match_db (skipped if intercept produced a plan).
    if not ctx.error_msg and ctx.plan is None:
        expand_resp = await _post_either(
            f"{expand_url}?database={db}", use_async=use_async_post,
            json=ctx.inp, logger=log,
        )
        _parsed_value = (ctx.inp or {}).get("parsed_value", {}) or {}
        if expand_resp is None:
            # Expand SERVICE unreachable / timed out. Do NOT abort — floor to the
            # user's literal terms so the DB filter is never empty just because an
            # external synonym API was slow (the literal matches the DB case-
            # insensitively). Graceful degradation; loses synonyms for this query.
            ctx.filter_val = _literal_floor_filter(_parsed_value)
            ctx.expand_response = {"value": ctx.filter_val}
            ctx.out_cols = valid_columns(ctx.filter_val, db)
            log.warning("[%s] expand unreachable — using literal-term floor: %s",
                        db, ctx.filter_val)
        elif expand_resp.status_code >= 400:
            # Expand tool rejected this DB (400 = unknown database). Treat as
            # empty expand so post_expand can build the plan from raw input.
            ctx.expand_response = {"value": {}}
            ctx.filter_val = {}
            ctx.out_cols = []
            log.info("[%s] expand tool returned %s — proceeding with empty filter", db, expand_resp.status_code)
        else:
            try:
                ctx.expand_response = expand_resp.json()
                ctx.filter_val = ctx.expand_response.get("value", {}) or {}
                # Per-field literal floor: if expand returned EMPTY for a field the
                # user DID specify (a single synonym source timed out), back-fill
                # that field with its literal terms so a concrete entity can never
                # silently drop on a partial timeout. Synonyms for other fields keep.
                for _k, _v in _parsed_value.items():
                    if isinstance(_v, list) and _v and not ctx.filter_val.get(_k):
                        _lit = [t.strip() for t in _v
                                if isinstance(t, str) and t.strip()
                                and t.strip().lower() != "requested"]
                        if _lit:
                            ctx.filter_val[_k] = _lit
                            log.warning("[%s] expand returned empty for %r — literal "
                                        "floor: %s", db, _k, _lit)
                ctx.out_cols = valid_columns(ctx.filter_val, db)
            except Exception:
                # Malformed JSON — same literal-term floor as the unreachable case.
                ctx.filter_val = _literal_floor_filter(_parsed_value)
                ctx.expand_response = {"value": ctx.filter_val}
                ctx.out_cols = valid_columns(ctx.filter_val, db)
                log.warning("[%s] expand malformed JSON — using literal-term floor: %s",
                            db, ctx.filter_val)

    # 5. post_expand hook — mutate expand_response / filter_val / out_cols.
    if not ctx.error_msg and ctx.expand_response is not None:
        await _call_hook(post_expand, ctx)

    # 5b. Emit expand_and_match step event (schema_kg path only — when expand
    #     ran AND ws_send is wired). Skipped when intercept short-circuited
    #     (error_msg set) or expand itself was skipped (ctx.plan already set
    #     before step 4, which only happens on direct-ID intercepts).
    if ctx.ws_send and ctx.expand_response is not None and not ctx.error_msg:
        import uuid as _uuid
        _exp_id = f"expand-{_uuid.uuid4().hex[:6]}"
        _canon = {k: v for k, v in ctx.filter_val.items()
                  if v and v != "requested"}
        # Card body goes through the LLM step-summarizer (plain English)
        # instead of a raw "canonical terms: k=[...]" dump; falls back to
        # the raw dump on any failure — this is a cosmetic card and must
        # NEVER be able to abort the actual query (see 2026-08-03 incident:
        # an unguarded 'FilterStat' AttributeError here silently killed the
        # whole request and fell through to the web/AI-knowledge fallback).
        _exp_summary = {"canonical_pv": _canon,
                        "filter_trace": [asdict(ft) for ft in ctx.filter_stats]}
        try:
            _exp_text = await _llm_summarize_step(
                "expand_and_match", _exp_summary, (ctx.inp or {}).get("cleaned_query", ""))
        except Exception as _summ_exc:
            log.debug("[%s] expand_and_match step-summarizer failed: %s", db, _summ_exc)
            _exp_text = _build_step_data_text("expand_and_match", _exp_summary)
        try:
            await ctx.ws_send({"type": "tool_called", "tool_id": _exp_id, "name": "Entity Expander"})
            await ctx.ws_send({"type": "delta", "tool_id": _exp_id, "name": "Entity Expander",
                               "text": _exp_text, "seq": 1, "offset": 0, "final": False})
            await ctx.ws_send({"type": "tool_result", "tool_id": _exp_id,
                               "name": "Entity Expander", "ok": True})
        except Exception as _ws_exc:
            log.debug("[%s] ws_send expand event failed: %s", db, _ws_exc)

    # 6. POST planner (skipped if intercept produced a plan). Use
    #    expand_for_planner if a hook set it; otherwise forward the full
    #    expand response.
    if not ctx.error_msg and ctx.expand_response is not None and ctx.plan is None:
        planner_payload = ctx.expand_for_planner or ctx.expand_response
        plan_resp = await _post_either(
            f"{planner_url}?database={db}", use_async=use_async_post,
            json=planner_payload, logger=log,
        )
        if not plan_resp:
            ctx.error_msg = "Planner unreachable."
        else:
            try:
                plan_obj = plan_resp.json().get("plan")
                ctx.plan = (
                    plan_obj.get("plan")
                    if isinstance(plan_obj, dict) and "plan" in plan_obj
                    else plan_obj
                )
                # Planner card publishing — OFF for ALL DBs, matching the TTD
                # template. The chip added noise above the synthesizer with no
                # UI value (single-table queries). It was TTD-only-disabled on
                # 2026-05-22; disabled fleet-wide on 2026-05-23 so every DB's
                # tool-call trace matches TTD. Set BIOCHIRP_PLANNER_CARD_<DB>=1
                # to opt a single DB back in.
                _planner_card_enabled = (
                    os.environ.get(
                        f"BIOCHIRP_PLANNER_CARD_{db.upper()}", ""
                    ).strip().lower() in {"1", "true", "yes"}
                )
                if _planner_card_enabled:
                    try:
                        await publish_planner_step(connection_id, db, ctx.plan or {})
                    except Exception as _planner_card_err:
                        log.warning("[%s] planner card publish failed: %s",
                                    db, _planner_card_err)
            except Exception:
                ctx.plan = None
            if not ctx.plan:
                ctx.error_msg = "Planner failed."

    # 7. post_planner hook — reattach preserved fields, plan overrides, etc.
    if not ctx.error_msg:
        await _call_hook(post_planner, ctx)

    # 8. pre_join hook — last chance to mutate filter_val / plan.
    if not ctx.error_msg:
        await _call_hook(pre_join, ctx)

    # 8b. Keep-columns for analytical TEXT2SQL queries: the planner prunes the
    #     projection to the few "output" columns, but text2sql needs ALL columns
    #     of the MATCHED table(s) to compute aggregates / channel thresholds
    #     (e.g. textmining). Widen out_cols to every column of the tables in the
    #     plan (NOT the whole DB schema — bounds the join/memory on wide DBs).
    #     join_and_filter keeps only those present in the joined result. No-op
    #     unless TEXT2SQL is on AND the question is analytical → inert for all else.
    if not ctx.error_msg:
        try:
            from ._text2sql import _enabled as _t2_on, _ANALYTIC_RX as _t2_rx
            if _t2_on() and _t2_rx.search(input.cleaned_query or ""):
                _schema = database_schemas.get(db) or {}
                # schema_kg plan_tables are db-suffixed (ppi_detailed_channels_string)
                # while database_schemas keys are bare (ppi_detailed_channels) —
                # match by bare suffix; fall back to ALL columns if nothing resolves
                # so text2sql is never starved of columns.
                _sk = ctx.extras.get("sk_plan") or {}
                _plan_tbls = {str(t).removesuffix(f"_{db}") for t in (_sk.get("plan_tables") or [])}
                _tables = [t for t in _schema if t in _plan_tbls] or list(_schema.keys())
                _cols = sorted({c for t in _tables for c in _schema.get(t, [])})
                if not _cols:
                    _cols = sorted({c for cols in _schema.values() for c in cols})
                if _cols:
                    ctx.out_cols = _cols
                    log.info("[%s] text2sql keep-columns: %d cols across %s", db, len(_cols), _tables)
        except Exception as _kc:
            log.debug("[%s] keep-columns skipped: %s", db, _kc)

    # 8z. Multi-condition INTERSECTION ("both X and Y"). Detect the explicit
    # intent and, for every parsed_value field that named ≥2 distinct entities,
    # record N = the ORIGINAL entity count (NOT the post-expansion synonym count
    # — synonyms of one entity must not inflate N). join_and_filter_database
    # keeps only output entities linked to all N. Generic; no hardcoding.
    if not ctx.error_msg and isinstance(ctx.plan, dict):
        try:
            _q_text = (getattr(input, "cleaned_query", "") or "")
            if _INTERSECTION_RX.search(_q_text):
                _pv = (ctx.inp or {}).get("parsed_value", {}) or {}
                _require_all = {
                    k: len({str(x).strip().lower() for x in v if str(x).strip()})
                    for k, v in _pv.items()
                    if isinstance(v, (list, tuple))
                    and len({str(x).strip().lower() for x in v if str(x).strip()}) >= 2
                }
                if _require_all:
                    ctx.plan["require_all_fields"] = _require_all
                    log.info("[%s] intersection intent → require_all=%s", db, _require_all)
        except Exception as _ie:
            log.debug("[%s] intersection-intent detection skipped: %s", db, _ie)

    # 9. join_and_filter_database.
    if not ctx.error_msg:
        try:
            ctx.df, ctx.filter_stats = join_and_filter_database(
                ctx.data, ctx.plan, db, ctx.out_cols, ctx.filter_val,
            )
        except NoFilterTermsError:
            # Entity expansion understood the query but couldn't match it to a
            # specific DB entity — degrade to the same "no rows matched" path
            # as a real empty join (runs on_empty_result, no hard error_msg),
            # instead of a "Query failed" that reads as a system malfunction.
            log.info("[%s] no filter terms after entity expansion — treating as empty result", db)
            ctx.df = pl.DataFrame()
        except Exception as e:
            ctx.error_msg = f"Query failed: {e}"
        if not ctx.error_msg and ctx.df.is_empty():
            # Give the per-DB on_empty hook a chance to populate ctx.df from
            # an alternate source before we declare "no rows".
            if on_empty_result is not None:
                await _call_hook(on_empty_result, ctx)
            if ctx.df is None or ctx.df.is_empty():
                ctx.error_msg = "No rows matched."

    # 9b. Emit execute step event once we know whether join succeeded.
    if ctx.ws_send:
        import uuid as _uuid2
        _exec_id = f"exec-{_uuid2.uuid4().hex[:6]}"
        _exec_ok = not ctx.error_msg
        _rc = ctx.df.height if (_exec_ok and ctx.df is not None) else 0
        # Card body goes through the LLM step-summarizer on success (plain
        # English instead of "row_count=N"); the error case stays a raw,
        # deterministic status line — no need to dress up a failure message,
        # and it must never be silently reworded by an LLM. The summarizer
        # call is guarded: this is a cosmetic card and must NEVER be able
        # to abort the actual query (see 2026-08-03 incident: an unguarded
        # 'FilterStat' AttributeError here silently killed the whole
        # request and fell through to the web/AI-knowledge fallback).
        if _exec_ok:
            _exec_summary = {"row_count": _rc,
                             "filter_trace": [asdict(ft) for ft in ctx.filter_stats]}
            try:
                _exec_text = await _llm_summarize_step(
                    "execute", _exec_summary, (ctx.inp or {}).get("cleaned_query", ""))
            except Exception as _summ_exc:
                log.debug("[%s] execute step-summarizer failed: %s", db, _summ_exc)
                _exec_text = _build_step_data_text("execute", _exec_summary)
        else:
            _exec_text = f"0 rows — {ctx.error_msg or 'no match'}"
        try:
            await ctx.ws_send({"type": "tool_called", "tool_id": _exec_id, "name": "DB Execute"})
            await ctx.ws_send({"type": "delta", "tool_id": _exec_id, "name": "DB Execute",
                               "text": _exec_text, "seq": 1, "offset": 0, "final": False})
            await ctx.ws_send({"type": "tool_result", "tool_id": _exec_id,
                               "name": "DB Execute", "ok": _exec_ok})
        except Exception as _ws_exc2:
            log.debug("[%s] ws_send execute event failed: %s", db, _ws_exc2)

    # 9c. post_join hook — for pre_computed_message / column projection.
    if not ctx.error_msg:
        await _call_hook(post_join, ctx)

    # NOTE: text2sql (the generic analytical DuckDB step) runs LATER — after the
    # null-drop/dedup at 9d — so its COUNT/AVG/MAX are computed over the SAME final
    # frame the user sees and downloads (the CSV). Running it here, before 9d,
    # over-counted: outer-join / partial-match artefact rows that 9d removes were
    # still in the frame (e.g. ClinVar BRCA1 reported 15363 vs the final 13283).

    # 9c. Row-level relevance scoring: embed each row as text tuple vs. the
    #     user query using BGE-small (fastembed ONNX, CPU). Adds a
    #     `relevance_score` column and sorts descending so the most relevant
    #     rows appear first in the preview and in the CSV download.
    if not ctx.error_msg:
        try:
            ctx.df = await asyncio.to_thread(
                _score_and_sort, input.cleaned_query, ctx.df
            )
        except BaseException as _bge_exc:
            # Catches SystemExit/KeyboardInterrupt from ONNX/numpy C extensions —
            # these bypass the try/except inside score_and_sort. Never let them
            # crash the uvicorn worker; just proceed without scoring.
            log.warning("[%s] BGE scoring aborted (%s); continuing without relevance sort", db, _bge_exc)
        # Per-DB deterministic ordering: stable re-sort ON TOP of relevance so a
        # curated rank (e.g. CTD therapeutic > marker/mechanism) leads, with
        # relevance preserved within each tier. Inert when sort_order is None or
        # none of its columns are present. (2026-06-26)
        if sort_order and ctx.df is not None and not ctx.df.is_empty():
            try:
                ctx.df = _apply_sort_order(ctx.df, sort_order)
            except Exception as _so_exc:
                log.warning("[%s] sort_order re-sort failed: %s", db, _so_exc)

    # 9d. Drop any rows where data columns contain nulls (outer-join artefacts,
    #     partial expand matches, etc.). Exclude relevance_score — it is added
    #     by scoring and is always non-null when present; excluding it avoids
    #     accidental drops if a future code path leaves it null.
    #     Also exclude cross-reference / supplementary annotation columns: a null
    #     kegg_hsa_xref (SMPDB-only pathway) or omim_xref (no OMIM entry) means
    #     "no ID assigned", NOT a missing row — dropping on them silently kills
    #     valid results (e.g. all 4 HTR2A pathways are SMPDB-sourced, kegg_hsa_xref=null).
    _NULLABLE_SUFFIXES = ("_xref", "xref_id", "xref_source", "icd11")
    if not ctx.error_msg and ctx.df is not None and not ctx.df.is_empty():
        _data_cols = [
            c for c in ctx.df.columns
            if c != "relevance_score"
            and not any(c.endswith(s) or c == s for s in _NULLABLE_SUFFIXES)
        ]
        if _data_cols:
            _dropped = ctx.df.drop_nulls(subset=_data_cols)
            # Only apply the null-drop if it LEAVES rows. If it would empty an
            # otherwise non-empty result, the rows are valid entity matches whose
            # only nulls are in OPTIONAL annotation columns (e.g. a chemical with
            # no definition / InChIKey, like Cisplatin) — emptying here would
            # discard a real answer AND any text2sql summary already computed from
            # those rows, producing a false "no data" → web/AI fallback. When
            # some rows survive the drop, the artefact-removal intent still holds
            # (outer-join / partial-match rows are dropped, clean rows kept).
            # Generic; no per-DB/per-column hardcoding. (2026-06-23)
            ctx.df = _dropped if not _dropped.is_empty() else ctx.df
        if ctx.df.is_empty():
            ctx.error_msg = "No rows matched after filtering."

    # 9e-text2sql. Generic analytical step (flag-gated TEXT2SQL, fail-open).
    # For analytical questions (count/aggregate/threshold/LIKE), generates a
    # read-only DuckDB SQL SELECT from the question + schema.json descriptions,
    # runs it on the result df, and sets pre_computed_message (LLM-summarizer
    # bypass). Runs HERE — after 9d null-drop/dedup — so its COUNT/AVG/MAX match
    # the final downloadable frame exactly. Only fires if no prior hook
    # (post_join / tier1) already set pre_computed_message. A pre_join hook may also
    # opt out (ctx.extras["skip_text2sql"]) when it has already produced the exact
    # deterministic answer that text2sql would only corrupt (e.g. reactome hierarchy).
    # Deterministic intersection summary (Case 3). The require_all group-by-having
    # pass has ALREADY kept exactly the output entities linked to ALL named
    # values, so we summarize HERE rather than letting either text2sql (whose
    # independent LLM re-aggregation garbles non-deterministically — e.g. a
    # per-group COUNT rendered as "1; 1; 1") or the row-counting summarizer (which
    # said "6 gene sets" for 3 sets × 2 genes) describe it. We count at the
    # OUTPUT-ENTITY grain: drop every column that shares the intersected field's
    # entity stem (e.g. gene_symbol/gene_id/gene_organism for a gene ∩), then
    # de-duplicate — so the count is distinct sets, not member rows. Generic
    # across DBs; no per-DB hardcoding.
    _is_intersection = bool(isinstance(ctx.plan, dict)
                            and ctx.plan.get("require_all_fields"))
    if (_is_intersection and not ctx.error_msg
            and ctx.pre_computed_message is None
            and ctx.df is not None and not ctx.df.is_empty()):
        try:
            import polars as _pl

            def _stem(c: str) -> str:
                for _s in ("_name", "_symbol", "_id", "_organism"):
                    if c.endswith(_s) and len(c) > len(_s):
                        return c[: -len(_s)]
                return c

            _raf_stems = {_stem(f) for f in ctx.plan["require_all_fields"]}
            _out_cols = [c for c in ctx.df.columns
                         if _stem(c) not in _raf_stems and c != "relevance_score"]
            if _out_cols:
                _ent = ctx.df.select(_out_cols).unique()
                _name_col = next((c for c in _out_cols if c.endswith("_name")),
                                 _out_cols[0])
                _names = (_ent.get_column(_name_col).cast(_pl.Utf8)
                          .drop_nulls().unique().to_list())
                _named = ", ".join(ctx.plan["require_all_fields"].keys())
                _disp = ctx.db.upper()
                _shown = "; ".join(sorted(_names)[:50])
                _more = "" if len(_names) <= 50 else f" (showing first 50 of {len(_names)})"
                ctx.pre_computed_message = (
                    f"{_disp}: {_ent.height} result(s) match ALL named {_named} "
                    f"values{_more}: {_shown}\n\nSource: {_disp}."
                )
                log.info("[%s] intersection summary: %d output entities", db, _ent.height)
        except Exception as _ie2:
            log.debug("[%s] intersection summary skipped: %s", db, _ie2)

    if (not ctx.error_msg and ctx.pre_computed_message is None
            and not _is_intersection
            and not (getattr(ctx, "extras", None) or {}).get("skip_text2sql")):
        try:
            from ._text2sql import maybe_answer_with_sql
            maybe_answer_with_sql(ctx, input.cleaned_query)
        except Exception as _t2exc:  # never let it crash the request
            log.debug("[%s] text2sql error (ignored): %s", db, _t2exc)

    # 10. Build preview + write CSV + publish WS event.
    ctx.preview = [] if ctx.error_msg else ctx.df.head(head_view_rows).to_dicts()
    # 2026-05-21 fix: write the CSV WHENEVER there are rows, regardless of
    # whether the caller passed a connection_id. Previously this was gated
    # on `connection_id` so that purely-headless callers wouldn't write
    # to disk; but the agentic surface's orchestrator-LLM (devstral, gpt-oss
    # via OpenRouter) sometimes silently drops `connection_id` when it
    # reconstructs the db-tool args from the interpreter output. Without
    # the CSV file the frontend has no Download link AND can't build the
    # filter/sort UI (which is bootstrapped from a CSV fetch). The CSV is
    # cheap to write (a few hundred KB) and the file exists at a stable
    # path under RESULTS_ROOT so the /download endpoint can serve it
    # even without a live WS subscriber. The WS publish below remains
    # gated on connection_id (publish_ws() itself no-ops on None, but
    # we keep the explicit check for clarity).
    if not ctx.error_msg:
        ctx.csv_path = _csv_path(f"{db}_results")
        try:
            os.makedirs(os.path.dirname(ctx.csv_path), exist_ok=True)
            ctx.df.write_csv(ctx.csv_path)
        except Exception as e:
            ctx.error_msg = f"CSV write failed: {e}"
            ctx.csv_path = ""
        # Best-effort WS publish — no-op if connection_id is None (the
        # LLM dropped it). The download/filter UI relies on csv_path
        # being in the tool's RETURN value, not on the WS event; the
        # agent_chat handler will build the <db>_table event from the
        # tool's return value regardless.
        if connection_id:
            await publish_ws(connection_id, ctx.csv_path, ctx.df.height)

    # 11. Build QueryState + finalize.
    state = QueryState(
        db=db, tool=db, DB_NAME=display_name, input=input,
        df=ctx.df, filter_stats=ctx.filter_stats, plan=ctx.plan,
        filter_val=ctx.filter_val,
        schema_cols={c for tbl in (database_schemas.get(db) or {}).values() for c in tbl},
        prompt_md=prompt_md, summarizer_model=summarizer_model,
        error_msg=ctx.error_msg, csv_path=ctx.csv_path, preview=ctx.preview,
        pre_computed_message=ctx.pre_computed_message,
    )
    return await finalize_db_result(state)


def make_db_result_handler(
    *,
    db: str,
    display_name: str,
    get_db: Callable,
    prompt_md: str,
    summarizer_model: str,
    **hooks: Any,
) -> Callable:
    """Return a pre-configured async query handler for a single-DB service.

    Replaces the boilerplate ``async def return_<db>_result(...)`` that was
    copy-pasted across every service file. Pass hook callables as keyword
    arguments (pre_expand, post_expand, pre_join, post_join, …).
    """
    async def _handler(
        input: QueryInterpreterOutputGuardrail,
        connection_id: Optional[str] = None,
        ws_send: Optional[Callable] = None,
    ) -> DatabaseTable:
        return await execute_db_query(
            input=input,
            connection_id=connection_id,
            db=db,
            display_name=display_name,
            get_db=get_db,
            prompt_md=prompt_md,
            summarizer_model=summarizer_model,
            ws_send=ws_send,
            **hooks,
        )
    return _handler


__all__ = ["WorkerCtx", "execute_db_query", "make_db_result_handler"]
