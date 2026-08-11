"""Multi-hop OpenTargets tools over prior result CSVs.

`join_results_tool` — relational set-ops (intersect / enrich / difference / union)
over two prior tool-result CSVs, with DYNAMIC canonical-key auto-detection. Pure
pandas, offline. Never raises: every failure path returns a status="error"
TableOutput.

`expand_associations` — bounded per-id fan-out that materializes the missing hop
for true chains (e.g. disease→target→drug), reusing the existing per-entity
fetchers; hard-capped to top-K so it cannot explode.

Both SELF-PUBLISH their table via `save_and_publish_csv` (the orchestrator only
auto-renders disease/target/drug tools), and return a `TableOutput`.

Design notes: join keys are the canonical id columns (gene_id=ENSG,
disease_id=EFO/MONDO, drug_id=CHEMBL), identical across all OT tools; ids are
preferred over names because they're stable. `disease_tool` emits
`gene_symbol`/`target_name` instead of `gene_name`, so that is aliased. Nothing
is hardcoded to a specific entity or question — the key and operation are
detected/parameterised from the data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List, Optional, Tuple

import pandas as pd
from agents import function_tool

from .guard_rail import TableOutput
from .utility import df_to_llm_safe_hierarchy
from .utility_shared import save_and_publish_csv, MAX_PREVIEW_ROWS
from .target_data import get_target_drugs_all, get_target_diseases_all, get_target_biological_info
from .disease_data import get_targets_for_disease_all, get_disease_combined_knowledge
from .drug_data import get_drug_mechanisms_of_action, get_drug_known_diseases_targets
from .evidence_data import get_target_disease_evidence

logger = logging.getLogger("uvicorn.error").getChild("opentargets.join")

# Canonical join keys, most-stable first (ids are stable across tools).
_ID_KEYS = ["gene_id", "disease_id", "drug_id"]
_NAME_KEYS = ["gene_name", "drug_name", "disease_name"]
_SCORE_COLS_PRIORITY = ["association_score", "association_score_left", "association_score_right"]

_JOIN_EXPLOSION_FACTOR = float(os.environ.get("OT_JOIN_EXPLOSION_FACTOR", "50"))
_JOIN_MAX_ROWS = int(os.environ.get("OT_JOIN_MAX_ROWS", "50000"))
_EXPAND_MAX_K = int(os.environ.get("OT_EXPAND_MAX_K", "50"))
_EXPAND_CONCURRENCY = int(os.environ.get("OT_EXPAND_CONCURRENCY", "8"))

_ENTITY_ID_COL = {"gene": "gene_id", "disease": "disease_id", "drug": "drug_id"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _err(msg: str, *, tool: str = "join_tool", raw: str = "") -> TableOutput:
    return TableOutput(status="error", raw_query=raw, message=msg,
                       tool=tool, database="OpenTargets", row_count=0)


def _norm_key(v) -> str:
    return "" if v is None else str(v).strip().lower()


def _norm_entity(e: str) -> str:
    e = (e or "").strip().lower()
    if e in ("target", "gene", "protein"):
        return "gene"
    if e in ("disease", "indication", "phenotype", "condition"):
        return "disease"
    if e in ("drug", "compound", "medicine", "molecule"):
        return "drug"
    return e


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    # disease_tool emits gene_symbol / target_name instead of gene_name — alias it.
    if "gene_name" not in df.columns:
        if "gene_symbol" in df.columns:
            df["gene_name"] = df["gene_symbol"]
        elif "target_name" in df.columns:
            df["gene_name"] = df["target_name"]
    return df


def _read_csv_safe(path: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    if not path:
        return None, "a required result-table path was empty"
    if not os.path.exists(path):
        return None, f"result table not found: {os.path.basename(str(path))} — re-run the source query first"
    try:
        df = pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read {os.path.basename(str(path))}: {exc}"
    return _normalize_cols(df), None


def _nonempty_in_both(left: pd.DataFrame, right: pd.DataFrame, col: str) -> bool:
    if col not in left.columns or col not in right.columns:
        return False
    l_ok = left[col].map(_norm_key).replace("", pd.NA).notna().any()
    r_ok = right[col].map(_norm_key).replace("", pd.NA).notna().any()
    return bool(l_ok and r_ok)


def _detect_join_key(left: pd.DataFrame, right: pd.DataFrame,
                     on: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (key_col, confidence). confidence ∈ {explicit,id,name,weak} or (None, reason)."""
    if on:
        on = str(on).strip().lower()
        if on in left.columns and on in right.columns:
            return on, "explicit"
        return None, f"requested key '{on}' is not present in both tables"
    for k in _ID_KEYS:
        if _nonempty_in_both(left, right, k):
            return k, "id"
    for k in _NAME_KEYS:
        if _nonempty_in_both(left, right, k):
            return k, "name"
    shared = [c for c in left.columns if c in right.columns and _nonempty_in_both(left, right, c)]
    if shared:
        return shared[0], "weak"
    return None, None


def _pick_root_col(df: pd.DataFrame) -> Optional[str]:
    for c in ("gene_name", "gene_symbol", "disease_name", "drug_name"):
        if c in df.columns and df[c].notna().any():
            return c
    return None


def _pick_sort_col(df: pd.DataFrame, sort_by: Optional[str]) -> Optional[str]:
    if sort_by and sort_by.strip().lower() in df.columns:
        return sort_by.strip().lower()
    for c in _SCORE_COLS_PRIORITY:
        if c in df.columns:
            return c
    for c in df.columns:
        if c.startswith("score_"):
            return c
    # Drug-association tables carry no association_score; rank by clinical `phase`
    # (higher = more advanced) so "top N drugs" is deterministic, not arbitrary.
    if "phase" in df.columns:
        return "phase"
    return None


def _coalesce_suffixes(df: pd.DataFrame) -> pd.DataFrame:
    """After an intersect merge, columns shared by both inputs carry _left/_right
    suffixes (gene_id_left/gene_id_right). Collapse each back to a clean unsuffixed
    column (prefer the left value, fall back to right) so intersect output has the
    SAME schema as difference/union — a usable join key AND a non-empty preview.
    Columns already present unsuffixed (e.g. `association_score` from
    _add_intersect_score) are left untouched so the symmetric min-rank survives."""
    df = df.copy()
    for c in list(df.columns):
        if not c.endswith("_left"):
            continue
        base = c[:-len("_left")]
        if base in df.columns:          # already materialized (e.g. association_score)
            continue
        rc = base + "_right"
        left = df[c]
        if rc in df.columns:
            mask = left.notna() & (left.astype(str).str.strip() != "")
            df[base] = left.where(mask, df[rc])
        else:
            df[base] = left
    drop = [c for c in df.columns if c.endswith("_left") or c.endswith("_right")]
    return df.drop(columns=drop, errors="ignore")


def _add_intersect_score(df: pd.DataFrame) -> pd.DataFrame:
    """After an intersect merge (suffixes _left/_right), the correct rank for an
    'in BOTH' question is the score that is strong on BOTH sides → the row-wise MIN
    of the two association scores. Materialize it as `association_score` so
    `_pick_sort_col` (which prefers `association_score`) ranks symmetrically instead
    of by only the left side. No-op when both score columns aren't present."""
    l, r = "association_score_left", "association_score_right"
    if l in df.columns and r in df.columns and "association_score" not in df.columns:
        df = df.copy()
        lv = pd.to_numeric(df[l], errors="coerce")
        rv = pd.to_numeric(df[r], errors="coerce")
        # skipna=False: a missing score on EITHER side means we cannot confirm the
        # entity is strong in both, so it ranks at the bottom (correct "in BOTH").
        df["association_score"] = pd.concat([lv, rv], axis=1).min(axis=1, skipna=False)
    return df


def _phase_ordinal(v) -> float:
    """Rank OpenTargets clinical-stage enums (maxClinicalStage: 'APPROVAL',
    'PHASE_4', 'PHASE_3', 'PHASE_2_3', 'PHASE_1', 'PRECLINICAL', …) by clinical
    advancement. An APPROVED/MARKETED drug is the MOST advanced — above every
    trial phase — so it ranks at 5 (above the max phase digit 4). Otherwise take
    the largest embedded digit (so a future 'PHASE_5' still ranks correctly);
    digit-less named stages (PRECLINICAL) rank at 0; missing/None rank at -1."""
    s = str(v).strip().upper()
    if not s or s in ("NONE", "NAN", ""):
        return -1.0
    if "APPROV" in s or "MARKET" in s:   # APPROVAL / APPROVED / MARKETED
        return 5.0
    digits = re.findall(r"\d+", s)
    if digits:
        return float(max(int(d) for d in digits))
    return 0.0


def _sort_by_score(df: pd.DataFrame, sort_by: Optional[str]) -> pd.DataFrame:
    col = _pick_sort_col(df, sort_by)
    if not col or df.empty:
        return df
    tmp = "__sort_num__"
    df = df.copy()
    if col == "phase":
        # phase is a string enum, not numeric — rank by clinical-stage ordinal
        df[tmp] = df[col].map(_phase_ordinal)
    else:
        df[tmp] = pd.to_numeric(df[col], errors="coerce").fillna(-1.0)
    df = df.sort_values(by=tmp, ascending=False).drop(columns=[tmp])
    return df


# Model-facing preview is bounded so wide graph tables (evidence with PMID lists,
# traverse drug rows, …) never balloon the tool-result the synthesizer must read.
# The FULL table still goes to the CSV (csv_path) — only the in-context preview is
# capped, in rows and in per-cell length.
_PREVIEW_ROWS = min(MAX_PREVIEW_ROWS, int(os.environ.get("OT_GRAPH_PREVIEW_ROWS", "25")))
_PREVIEW_CELL = int(os.environ.get("OT_GRAPH_PREVIEW_CELL", "200"))


def _bounded_preview(df: pd.DataFrame) -> pd.DataFrame:
    head = df.head(_PREVIEW_ROWS).fillna("").copy()
    for c in head.columns:
        if head[c].dtype == object:
            head[c] = head[c].map(lambda v: (s[:_PREVIEW_CELL] + "…")
                                  if isinstance((s := str(v)), str) and len(s) > _PREVIEW_CELL else v)
    return head


async def _emit(df: pd.DataFrame, *, tool: str, prefix: str, connection_id: Optional[str],
                raw: str, message: str, metadata: dict, truncated: bool) -> TableOutput:
    row_count = len(df)
    preview_n = min(_PREVIEW_ROWS, row_count)
    table: dict = {}
    if row_count:
        head = _bounded_preview(df)
        root = _pick_root_col(head)
        if root:
            try:
                table = df_to_llm_safe_hierarchy(head, root_col=root)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] preview build failed: %s", tool, exc)
                table = {}
    csv_path = await save_and_publish_csv(df, connection_id, prefix, tool, tool, row_count)
    return TableOutput(
        status="success", raw_query=raw, message=message, table=table,
        csv_path=csv_path, row_count=row_count, preview_row_count=preview_n,
        is_truncated=bool(truncated), tool=tool, database="OpenTargets",
        metadata=metadata,
    )


# ── join_results_tool ────────────────────────────────────────────────────────

@function_tool(
    strict_mode=False,
    name_override="join_results_tool",
    description_override=(
        "Combine TWO prior OpenTargets result tables (by their csv_path) on a shared "
        "biological key. how='intersect' (entities in BOTH tables), 'enrich' (keep all "
        "left rows, attach matching right columns), 'difference' (in left but NOT right), "
        "'union' (all rows from both). The join key (gene_id / disease_id / drug_id) is "
        "auto-detected; pass `on` to override. Optional top_k + sort_by to rank. "
        "Use for cross-result questions: 'genes associated with BOTH disease A and B', "
        "'for disease X's targets show <property>', 'genes for A but not B'. "
        "Requires you to have already run the two source lookups and captured their csv_path."
    ),
)
async def join_results_tool(
    left_csv_path: str,
    right_csv_path: str,
    how: str = "intersect",
    on: Optional[str] = None,
    top_k: Optional[int] = None,
    sort_by: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> TableOutput:
    raw = f"join({how})"
    try:
        how = (how or "intersect").strip().lower()
        if how not in ("intersect", "enrich", "difference", "union"):
            return _err(f"unknown how='{how}' (use intersect | enrich | difference | union)", raw=raw)

        left, lerr = _read_csv_safe(left_csv_path)
        if lerr:
            return _err(f"left table: {lerr}", raw=raw)
        right, rerr = _read_csv_safe(right_csv_path)
        if rerr:
            return _err(f"right table: {rerr}", raw=raw)

        # Empty-input fast paths (a valid negative, not an error).
        if left.empty and right.empty:
            return await _emit(pd.DataFrame(), tool="join_tool", prefix="join_tool", connection_id=connection_id,
                               raw=raw, message="Both input tables were empty.", metadata={"how": how}, truncated=False)
        if how == "union" and (left.empty or right.empty):
            out = (right if left.empty else left).copy()
            return await _emit(out, tool="join_tool", prefix="join_tool", connection_id=connection_id, raw=raw,
                               message=f"Union: one input was empty; returned the other ({len(out)} rows).",
                               metadata={"how": how}, truncated=False)
        if how == "difference" and right.empty:
            return await _emit(left.copy(), tool="join_tool", prefix="join_tool", connection_id=connection_id, raw=raw,
                               message=f"Difference: right table empty; all {len(left)} left rows retained.",
                               metadata={"how": how}, truncated=False)
        if left.empty or right.empty:
            return await _emit(pd.DataFrame(), tool="join_tool", prefix="join_tool", connection_id=connection_id,
                               raw=raw, message="One input table was empty — no overlap.",
                               metadata={"how": how}, truncated=False)

        key, conf = _detect_join_key(left, right, on)
        if not key:
            shared = sorted(set(left.columns) & set(right.columns))
            reason = conf if isinstance(conf, str) else "no shared key column"
            return _err(
                f"cannot join: {reason}. Shared columns: {shared or 'none'}. "
                f"Expected one of {_ID_KEYS} present in both tables.", raw=raw)

        # Normalised hidden key; keep display columns untouched.
        lk, rk = left.copy(), right.copy()
        lk["__jk__"] = lk[key].map(_norm_key)
        rk["__jk__"] = rk[key].map(_norm_key)
        lk = lk[lk["__jk__"] != ""]
        rk = rk[rk["__jk__"] != ""]

        pre = max(len(lk), len(rk), 1)
        if how == "difference":
            right_keys = set(rk["__jk__"])
            out = lk[~lk["__jk__"].isin(right_keys)].copy()
        elif how == "union":
            cols = list(dict.fromkeys(list(lk.columns) + list(rk.columns)))
            out = pd.concat([lk.reindex(columns=cols), rk.reindex(columns=cols)], ignore_index=True)
            out = out.drop_duplicates(subset="__jk__", keep="first")
        else:  # intersect | enrich
            merge_how = "inner" if how == "intersect" else "left"
            out = lk.merge(rk.drop(columns=[]), on="__jk__", how=merge_how, suffixes=("_left", "_right"))
            if how == "intersect":
                # symmetric rank (min of both sides) then keep best-scored row/entity
                out = _add_intersect_score(out)
                out = _coalesce_suffixes(out)   # clean schema (enrich keeps both sides)
                out = _sort_by_score(out, sort_by).drop_duplicates(subset="__jk__", keep="first")

        truncated = False
        post = len(out)
        factor = post / pre
        if factor > _JOIN_EXPLOSION_FACTOR or post > _JOIN_MAX_ROWS:
            out = _sort_by_score(out, sort_by).head(_JOIN_MAX_ROWS)
            truncated = True

        out = _sort_by_score(out, sort_by)
        if top_k and top_k > 0:
            out = out.head(int(top_k))
        out = out.drop(columns=["__jk__"], errors="ignore")

        keyed = "gene/drug/disease id" if conf == "id" else f"{key} ({conf})"
        msg = (f"Join '{how}' on {key} → {len(out)} rows (left {len(left)} ⋈ right {len(right)}, "
               f"matched on {keyed}).")
        if truncated:
            msg += " Many-to-many expansion detected; showing top results by score."
        if conf in ("name", "weak"):
            msg += " ⚠️ matched on names, not stable IDs — possible synonym mismatch."
        return await _emit(out, tool="join_tool", prefix="join_tool", connection_id=connection_id, raw=raw,
                           message=msg, metadata={"how": how, "join_key": key, "key_confidence": conf,
                                                  "left_rows": len(left), "right_rows": len(right)},
                           truncated=truncated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[join_results_tool] failed: %s", exc, exc_info=True)
        return _err(f"join failed: {exc}", raw=raw)


# ── expand_associations ──────────────────────────────────────────────────────

_FANOUT = {
    ("gene", "drug"): get_target_drugs_all,
    ("gene", "disease"): get_target_diseases_all,
    ("disease", "gene"): get_targets_for_disease_all,
    ("drug", "gene"): get_drug_mechanisms_of_action,
    ("drug", "disease"): get_drug_known_diseases_targets,
}


async def _fanout_df(
    src: pd.DataFrame, from_entity: str, to_entity: str,
    top_k: int = 25, sort_by: Optional[str] = None,
) -> Tuple[pd.DataFrame, int]:
    """Fan the top_k `from_entity` rows of `src` out to `to_entity` via the
    canonical OT edge fetcher, concatenating the results into ONE table.

    Returns (out_df, n_ids_expanded). out_df is empty when the hop is
    unsupported, no source ids exist, or no associations are found. Shared by
    expand_associations (CSV-in) and combine(then_expand=…) (DataFrame-in) so the
    bounded, concurrency-capped fan-out logic lives in exactly one place."""
    fe, te = _norm_entity(from_entity), _norm_entity(to_entity)
    fetcher = _FANOUT.get((fe, te))
    id_col = _ENTITY_ID_COL.get(fe)
    if fetcher is None or id_col is None or id_col not in src.columns:
        return pd.DataFrame(), 0
    src = _sort_by_score(src, sort_by)
    k = max(1, min(int(top_k or 25), _EXPAND_MAX_K))
    ids, seen = [], set()
    for v in src[id_col].tolist():
        s = "" if v is None else str(v).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            ids.append(s)
        if len(ids) >= k:
            break
    if not ids:
        return pd.DataFrame(), 0
    sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)

    async def _one(eid: str) -> Optional[pd.DataFrame]:
        async with sem:
            try:
                return await fetcher(eid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[_fanout_df] %s(%s) failed: %s",
                               getattr(fetcher, "__name__", "?"), eid, exc)
                return None

    frames = await asyncio.gather(*[_one(i) for i in ids])
    good = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
    if not good:
        return pd.DataFrame(), len(ids)
    out = _sort_by_score(_normalize_cols(pd.concat(good, ignore_index=True)), sort_by)
    if len(out) > _JOIN_MAX_ROWS:
        out = out.head(_JOIN_MAX_ROWS)
    return out, len(ids)


@function_tool(
    strict_mode=False,
    name_override="expand_associations",
    description_override=(
        "Chain across one hop: take the top_k entities of `from_entity` in a PRIOR result "
        "CSV and fetch their `to_entity` associations, returning ONE joinable table. "
        "from_entity/to_entity ∈ {gene, disease, drug}. Supports disease→target→drug "
        "style chains, e.g. from a disease→targets CSV use from_entity='gene', "
        "to_entity='drug' to get drugs for the disease's top genes (with phase/status). "
        "Bounded to top_k (default 25, max 50) so it never explodes. Requires a prior "
        "result csv_path."
    ),
)
async def expand_associations(
    source_csv_path: str,
    from_entity: str,
    to_entity: str,
    top_k: int = 25,
    sort_by: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> TableOutput:
    raw = f"expand({from_entity}->{to_entity})"
    try:
        fe, te = _norm_entity(from_entity), _norm_entity(to_entity)
        fetcher = _FANOUT.get((fe, te))
        if fetcher is None:
            return _err(
                f"unsupported hop {fe}→{te}. Supported: " +
                ", ".join(f"{a}→{b}" for a, b in _FANOUT), tool="expand_tool", raw=raw)

        src, serr = _read_csv_safe(source_csv_path)
        if serr:
            return _err(f"source table: {serr}", tool="expand_tool", raw=raw)
        if src.empty:
            return _err("source table is empty — nothing to expand.", tool="expand_tool", raw=raw)

        id_col = _ENTITY_ID_COL[fe]
        if id_col not in src.columns:
            # Defensive auto-derivation: the agent often feeds a disease→drug table
            # (which drops gene_id) for a disease→target→drug chain. If we need gene
            # ids but the source only carries a disease anchor, derive the disease's
            # targets first (bounded), so the chain still works. No hardcoded query —
            # we resolve the required key from whatever anchor IS present.
            if fe == "gene" and "disease_id" in src.columns:
                dz = [str(v).strip() for v in src["disease_id"].tolist() if str(v).strip()]
                dz = list(dict.fromkeys(dz))[:3]  # usually one; cap to avoid blow-up
                if dz:
                    derived = await asyncio.gather(*[get_targets_for_disease_all(d) for d in dz],
                                                   return_exceptions=True)
                    good = [d for d in derived if isinstance(d, pd.DataFrame) and not d.empty]
                    if good:
                        src = _normalize_cols(pd.concat(good, ignore_index=True))
                        logger.info("[expand_associations] derived %d target rows from %d disease(s) "
                                    "because source lacked gene_id", len(src), len(dz))
            if id_col not in src.columns:
                return _err(f"source table has no '{id_col}' column to read {fe} ids from, and "
                            f"none could be derived. Columns: {sorted(src.columns)}",
                            tool="expand_tool", raw=raw)

        src = _sort_by_score(src, sort_by)
        k = max(1, min(int(top_k or 25), _EXPAND_MAX_K))
        ids: List[str] = []
        seen = set()
        for v in src[id_col].tolist():
            s = "" if v is None else str(v).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                ids.append(s)
            if len(ids) >= k:
                break
        if not ids:
            return _err(f"no {fe} ids found in source '{id_col}' column.", tool="expand_tool", raw=raw)

        sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)

        async def _one(eid: str) -> Optional[pd.DataFrame]:
            async with sem:
                try:
                    return await fetcher(eid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[expand_associations] %s(%s) failed: %s", fetcher.__name__, eid, exc)
                    return None

        frames = await asyncio.gather(*[_one(i) for i in ids])
        good = [f for f in frames if f is not None and not f.empty]
        if not good:
            return await _emit(pd.DataFrame(), tool="expand_tool", prefix="expand_tool",
                               connection_id=connection_id, raw=raw,
                               message=f"No {te} associations found for the top {len(ids)} {fe}(s).",
                               metadata={"from": fe, "to": te, "ids": len(ids)}, truncated=False)

        out = pd.concat(good, ignore_index=True)
        out = _normalize_cols(out)
        out = _sort_by_score(out, sort_by)
        truncated = False
        if len(out) > _JOIN_MAX_ROWS:
            out = out.head(_JOIN_MAX_ROWS)
            truncated = True
        msg = (f"Expanded {len(ids)} {fe}(s) → {len(out)} {fe}↔{te} rows. "
               f"This table is joinable on its canonical id columns.")
        return await _emit(out, tool="expand_tool", prefix="expand_tool", connection_id=connection_id,
                           raw=raw, message=msg,
                           metadata={"from": fe, "to": te, "ids_expanded": len(ids), "top_k": k},
                           truncated=truncated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[expand_associations] failed: %s", exc, exc_info=True)
        return _err(f"expand failed: {exc}", tool="expand_tool", raw=raw)


# ── filter_targets_by_annotation ─────────────────────────────────────────────
# Set-combine / traverse / disease_tool give you a SET of targets; this filters
# that set by a per-target functional annotation (tractability / druggability)
# that no association table carries. Answers "of these genes, which are tractable
# small-molecule targets" and the negation "which are undruggable" deterministically
# instead of leaving the agent to annotate-then-filter N genes by hand (which stalls).

def _truthy_tractability(tract: dict, modality: Optional[str] = None) -> List[str]:
    """Truthy tractability bucket labels, optionally restricted to one modality
    code (OT codes: SM=small molecule, AB=antibody, PR=PROTAC, OC=other clinical)."""
    hits: List[str] = []
    for mod, buckets in (tract or {}).items():
        if modality and str(mod).upper() != modality.upper():
            continue
        for label, val in (buckets or {}).items():
            if val:
                hits.append(f"{mod}:{label}")
    return hits


# predicate token → (modality filter, keep-when-present). 'undruggable' inverts.
_ANNOT_PREDICATES = {
    "tractable": (None, True),
    "tractable_sm": ("SM", True),
    "tractable_small_molecule": ("SM", True),
    "tractable_ab": ("AB", True),
    "tractable_antibody": ("AB", True),
    "undruggable": (None, False),
    "not_tractable": (None, False),
}


@function_tool(
    strict_mode=False,
    name_override="filter_targets_by_annotation",
    description_override=(
        "Filter a PRIOR target/gene result CSV by a per-target functional annotation "
        "that association tables do NOT carry. Use after combine / traverse / "
        "disease_tool / target_tool when the question adds a druggability qualifier. "
        "predicate ∈ {tractable, tractable_sm, tractable_ab, undruggable}: "
        "'tractable_sm' keeps targets with a small-molecule tractability bucket; "
        "'undruggable' keeps targets with NO tractability bucket (the negation). "
        "top_k bounds how many of the source's top-ranked targets are checked "
        "(default 25, max 50). Pass sort_by='score_genetic_association' to rank the "
        "source by genetic evidence first (e.g. 'top genetic targets that are "
        "undruggable'). Requires a prior result csv_path."
    ),
)
async def filter_targets_by_annotation(
    source_csv_path: str,
    predicate: str,
    top_k: int = 25,
    sort_by: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> TableOutput:
    pred = (predicate or "").strip().lower()
    raw = f"filter_targets({pred})"
    if pred not in _ANNOT_PREDICATES:
        return _err(f"unknown predicate='{predicate}' "
                    f"(use {', '.join(sorted(_ANNOT_PREDICATES))})",
                    tool="filter_tool", raw=raw)
    modality, keep_when_present = _ANNOT_PREDICATES[pred]
    src, serr = _read_csv_safe(source_csv_path)
    if serr:
        return _err(f"source table: {serr}", tool="filter_tool", raw=raw)
    if src is None or src.empty:
        return _err("source table is empty — nothing to filter.", tool="filter_tool", raw=raw)
    src = _normalize_cols(src)
    id_col = _ENTITY_ID_COL["gene"]
    name_col = _NAME_COL["gene"]
    key_col = id_col if id_col in src.columns else ("gene_symbol" if "gene_symbol" in src.columns
                                                    else (name_col if name_col in src.columns else None))
    if key_col is None:
        return _err("source table has no gene_id / gene_symbol / gene_name column to "
                    f"annotate. Columns: {sorted(src.columns)}", tool="filter_tool", raw=raw)
    src = _sort_by_score(src, sort_by)
    k = max(1, min(int(top_k or 25), _EXPAND_MAX_K))
    rows, seen = [], set()
    for _, r in src.iterrows():
        gid = str(r.get(key_col) or "").strip()
        if gid and gid.lower() not in seen:
            seen.add(gid.lower())
            rows.append(r)
        if len(rows) >= k:
            break
    if not rows:
        return _err("no gene identifiers found in source table.", tool="filter_tool", raw=raw)

    sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)

    async def _annot(r) -> Optional[dict]:
        gid = str(r.get(key_col)).strip()
        async with sem:
            try:
                info = await get_target_biological_info(gid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[filter_targets] annotation failed for %s: %s", gid, exc)
                return None
        if not info:
            return None
        buckets = _truthy_tractability(info.get("tractability") or {}, modality)
        present = len(buckets) > 0
        if present != keep_when_present:
            return None
        out_row = r.to_dict()
        out_row["tractability_buckets"] = "; ".join(buckets) if buckets else ""
        out_row["matched_predicate"] = pred
        return out_row

    kept = [x for x in await asyncio.gather(*[_annot(r) for r in rows]) if x is not None]
    out = _normalize_cols(pd.DataFrame(kept)) if kept else pd.DataFrame()
    msg = (f"{len(out)} of the top {len(rows)} target(s) match predicate '{pred}' "
           f"(checked per-target Open Targets tractability).")
    return await _emit(out, tool="filter_tool", prefix="filter_tool",
                       connection_id=connection_id, raw=raw, message=msg,
                       metadata={"predicate": pred, "checked": len(rows),
                                 "modality": modality}, truncated=False)


# ── Generic graph primitives: combine (set-ops) + traverse (path walks) ───────
# The Open Targets graph has 3 node types (gene/target, disease, drug) and 6
# directed association edges, each backed by an existing single-arg fetcher.
# Every multi-hop question is either a SET-COMBINE (intersect/difference/union of
# two same-type lookups) or a PATH-TRAVERSE (walk a chain of edges). These two
# DETERMINISTIC tools do the fetching+combining in code, so the agent makes ONE
# reliable call instead of orchestrating several. Nothing about specific entities
# is hardcoded — only the (fixed-biology) edge structure is.

_RETRIEVE_ID = {"gene": "gene_id", "disease": "disease_id", "drug": "drug_id"}
_NAME_COL = {"gene": "gene_name", "disease": "disease_name", "drug": "drug_name"}


def _resolved_name(df: pd.DataFrame, entity_type: str) -> Optional[str]:
    """The actual entity the free-text name resolved to (for transparency)."""
    col = _NAME_COL.get(_norm_entity(entity_type))
    if col and col in df.columns:
        s = df[col].dropna()
        if len(s):
            return str(s.iloc[0])
    return None

# (anchor_type, retrieve_type) → fetcher(name_or_id) -> DataFrame with canonical ids
_EDGE = {
    ("disease", "gene"): get_targets_for_disease_all,
    ("disease", "drug"): get_disease_combined_knowledge,
    ("gene", "disease"): get_target_diseases_all,
    ("gene", "drug"): get_target_drugs_all,
    ("drug", "gene"): get_drug_mechanisms_of_action,
    ("drug", "disease"): get_drug_known_diseases_targets,
}


def _edge(anchor_type: str, retrieve: str):
    return _EDGE.get((_norm_entity(anchor_type), _norm_entity(retrieve)))


async def _fetch_retry(fetcher, arg: str):
    """Call a fetcher with one retry, to ride out a transient OT API blip."""
    try:
        return await fetcher(arg)
    except Exception as exc:  # noqa: BLE001
        logger.info("[graph] %s(%s) retry after: %s", getattr(fetcher, "__name__", "?"), arg, exc)
        await asyncio.sleep(0.6)
        return await fetcher(arg)


def _setop_df(left: pd.DataFrame, right: pd.DataFrame, key: str, how: str,
              sort_by: Optional[str] = None) -> pd.DataFrame:
    lk, rk = left.copy(), right.copy()
    lk["__jk__"] = lk[key].map(_norm_key)
    rk["__jk__"] = rk[key].map(_norm_key)
    lk = lk[lk["__jk__"] != ""]
    rk = rk[rk["__jk__"] != ""]
    if how == "difference":
        out = lk[~lk["__jk__"].isin(set(rk["__jk__"]))].copy()
    elif how == "union":
        cols = list(dict.fromkeys(list(lk.columns) + list(rk.columns)))
        out = pd.concat([lk.reindex(columns=cols), rk.reindex(columns=cols)], ignore_index=True)
    else:  # intersect — _left/_right suffixes so _pick_sort_col finds the score column
        out = lk.merge(rk, on="__jk__", how="inner", suffixes=("_left", "_right"))
        out = _add_intersect_score(out)   # symmetric rank = min(left, right)
        out = _coalesce_suffixes(out)     # restore clean schema (gene_id, gene_name, …)
    # Sort by score BEFORE de-duping so keep="first" retains the best-scored row per
    # entity, then collapse to ONE row per distinct entity. (Previously `difference`
    # never de-duped → it over-counted duplicate id rows; and intersect used _a/_b
    # suffixes the score-priority list never matched → results were mis-sorted.)
    out = _sort_by_score(out, sort_by)
    out = out.drop_duplicates(subset="__jk__", keep="first")
    return out.drop(columns=["__jk__"], errors="ignore")


@function_tool(
    strict_mode=False,
    name_override="combine",
    description_override=(
        "DETERMINISTIC set-combine across TWO entities of the SAME type — answers "
        "'in BOTH A and B' (intersect), 'A but not B' (difference), 'either' (union). "
        "It fetches both sides itself (no prior lookups needed). Params: "
        "anchor_type ∈ {disease,target,drug} (the type of A and B); anchor_a, anchor_b "
        "(the two entity names); retrieve ∈ {gene,drug,disease} (what to pull from each); "
        "operation ∈ {intersect,difference,union}. "
        "Examples: genes in BOTH Alzheimer's and Parkinson's → "
        "anchor_type='disease', anchor_a='Alzheimer's', anchor_b='Parkinson's', "
        "retrieve='gene', operation='intersect'. Drugs common to two diseases → "
        "retrieve='drug'. Optional then_expand ∈ {gene,drug,disease} adds a second "
        "hop on the combined set in ONE call: 'drugs acting on the genes shared by A "
        "and B' → retrieve='gene', then_expand='drug' (expands the top_k shared genes, "
        "default 25). Prefer THIS over manual disease_tool+join for such questions."
    ),
)
async def combine(
    anchor_type: str,
    anchor_a: str,
    anchor_b: str,
    retrieve: str,
    operation: str = "intersect",
    top_k: Optional[int] = None,
    then_expand: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> TableOutput:
    raw = f"combine({anchor_type}:{retrieve}:{operation})"
    try:
        op = (operation or "intersect").strip().lower()
        if op not in ("intersect", "difference", "union"):
            return _err(f"unknown operation='{op}' (intersect|difference|union)", tool="combine_tool", raw=raw)
        rt = _norm_entity(retrieve)
        key = _RETRIEVE_ID.get(rt)
        fetcher = _edge(anchor_type, retrieve)
        if fetcher is None or key is None:
            return _err(f"unsupported {_norm_entity(anchor_type)}→{rt}. "
                        f"Supported edges: {', '.join(f'{a}->{b}' for a,b in _EDGE)}",
                        tool="combine_tool", raw=raw)
        da, db = await asyncio.gather(_fetch_retry(fetcher, anchor_a),
                                      _fetch_retry(fetcher, anchor_b),
                                      return_exceptions=True)
        for nm, d in (("A", da), ("B", db)):
            if isinstance(d, Exception):
                return _err(f"fetch failed for {nm} ({anchor_a if nm=='A' else anchor_b}): {d}",
                            tool="combine_tool", raw=raw)
        da, db = _normalize_cols(da), _normalize_cols(db)
        if key not in da.columns or key not in db.columns:
            return _err(f"retrieve='{rt}' produced no '{key}' column to combine on.",
                        tool="combine_tool", raw=raw)
        out = _setop_df(da, db, key, op)
        out = _sort_by_score(out, None)
        ra = _resolved_name(da, anchor_type) or anchor_a
        rb = _resolved_name(db, anchor_type) or anchor_b
        clause = {"intersect": f"shared by {ra} & {rb}",
                  "difference": f"in {ra} but not {rb}",
                  "union": f"across {ra} & {rb}"}[op]
        diffs = []
        if ra.strip().lower() != str(anchor_a).strip().lower():
            diffs.append(f"'{anchor_a}'→{ra}")
        if rb.strip().lower() != str(anchor_b).strip().lower():
            diffs.append(f"'{anchor_b}'→{rb}")
        note = (" [resolved: " + ", ".join(diffs) + "]") if diffs else ""
        n_combined = len(out)

        # Optional second hop: expand the combined entities to `then_expand` (e.g.
        # "drugs acting on the genes shared by A and B" = retrieve='gene' +
        # then_expand='drug'). Bounded to the top_k combined entities (default 25)
        # so the fan-out stays deterministic and small — replaces an unreliable
        # combine→expand LLM chain with one call. Generic over every edge.
        if then_expand and str(then_expand).strip():
            te = _norm_entity(then_expand)
            exp, n_ids = await _fanout_df(out, rt, te, top_k=top_k or 25)
            if not exp.empty:
                msg = (f"{len(exp)} {te}(s) acting on the top {n_ids} {rt}(s) {clause}{note} "
                       f"(via {rt}→{te}; {n_combined} {rt}(s) {clause} total).")
                return await _emit(exp, tool="combine_tool", prefix="combine_tool",
                                   connection_id=connection_id, raw=raw, message=msg,
                                   metadata={"anchor_type": _norm_entity(anchor_type),
                                             "retrieve": rt, "operation": op,
                                             "then_expand": te, "expanded_from": n_ids,
                                             "resolved_a": ra, "resolved_b": rb}, truncated=False)
            # Fall through to the combined set with a note when the hop yields nothing.
            note = note + f" (no {te} found expanding {rt}→{te})"

        if top_k and top_k > 0:
            out = out.head(int(top_k))
        msg = (f"{len(out)} {rt}(s) {clause}{note} "
               f"(A:{len(da)} rows, B:{len(db)} rows, op={op}).")
        return await _emit(out, tool="combine_tool", prefix="combine_tool",
                           connection_id=connection_id, raw=raw, message=msg,
                           metadata={"anchor_type": _norm_entity(anchor_type), "retrieve": rt,
                                     "operation": op, "key": key,
                                     "resolved_a": ra, "resolved_b": rb}, truncated=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[combine] failed: %s", exc, exc_info=True)
        return _err(f"combine failed: {exc}", tool="combine_tool", raw=raw)


@function_tool(
    strict_mode=False,
    name_override="traverse",
    description_override=(
        "DETERMINISTIC path-walk across the Open Targets graph — answers multi-hop "
        "chains from ONE start entity. It does every hop itself. Params: "
        "start_type ∈ {disease,target,drug}; start (entity name); hop1 ∈ "
        "{gene,drug,disease} (first neighbor type); hop2 (optional, second neighbor "
        "type — leave empty for a single hop); top_k (cap on the intermediate set, "
        "default 25). Example — approved drugs targeting ALS's top genes: "
        "start_type='disease', start='amyotrophic lateral sclerosis', hop1='gene', "
        "hop2='drug', top_k=25. Prefer THIS over manual disease_tool+expand for chains."
    ),
)
async def traverse(
    start_type: str,
    start: str,
    hop1: str,
    hop2: Optional[str] = None,
    top_k: int = 25,
    connection_id: Optional[str] = None,
) -> TableOutput:
    raw = f"traverse({start_type}->{hop1}->{hop2 or ''})"
    try:
        st = _norm_entity(start_type)
        h1 = _norm_entity(hop1)
        f1 = _edge(st, h1)
        if f1 is None:
            return _err(f"unsupported first hop {st}→{h1}. Edges: "
                        f"{', '.join(f'{a}->{b}' for a,b in _EDGE)}", tool="traverse_tool", raw=raw)
        try:
            df1 = _normalize_cols(await _fetch_retry(f1, start))
        except Exception as exc:  # noqa: BLE001
            return _err(f"first hop fetch failed for {start}: {exc}", tool="traverse_tool", raw=raw)
        if df1.empty:
            return await _emit(pd.DataFrame(), tool="traverse_tool", prefix="traverse_tool",
                               connection_id=connection_id, raw=raw,
                               message=f"No {h1}(s) found for {start}.",
                               metadata={"start": st, "hop1": h1}, truncated=False)
        df1 = _sort_by_score(df1, None)

        if not (hop2 and str(hop2).strip()):
            r0 = _resolved_name(df1, start_type) or start
            return await _emit(df1, tool="traverse_tool", prefix="traverse_tool",
                               connection_id=connection_id, raw=raw,
                               message=f"{len(df1)} {h1}(s) for {r0} (single hop).",
                               metadata={"start": st, "hop1": h1, "resolved_start": r0}, truncated=False)

        h2 = _norm_entity(hop2)
        f2 = _edge(h1, h2)
        id_col1 = _RETRIEVE_ID.get(h1)
        if f2 is None or id_col1 is None or id_col1 not in df1.columns:
            return _err(f"unsupported second hop {h1}→{h2} (or no '{id_col1}' to walk from).",
                        tool="traverse_tool", raw=raw)
        k = max(1, min(int(top_k or 25), _EXPAND_MAX_K))
        ids, seen = [], set()
        for v in df1[id_col1].tolist():
            s = "" if v is None else str(v).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower()); ids.append(s)
            if len(ids) >= k:
                break
        sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)

        async def _one(eid):
            async with sem:
                try:
                    return await f2(eid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[traverse] %s(%s) failed: %s", getattr(f2, "__name__", "?"), eid, exc)
                    return None

        frames = await asyncio.gather(*[_one(i) for i in ids])
        good = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
        if not good:
            return await _emit(pd.DataFrame(), tool="traverse_tool", prefix="traverse_tool",
                               connection_id=connection_id, raw=raw,
                               message=f"No {h2}(s) found for the top {len(ids)} {h1}(s) of {start}.",
                               metadata={"start": st, "hop1": h1, "hop2": h2}, truncated=False)
        out = _sort_by_score(_normalize_cols(pd.concat(good, ignore_index=True)), None)
        # De-dup to ONE row per distinct hop2 entity (a hop2 node reachable from many
        # hop1 nodes would otherwise be over-counted, skewing the count and top_k).
        # Key on the id, falling back to the name when a row has no id (e.g. a drug
        # MoA whose target has no resolved Ensembl id) so named-but-id-less entities
        # are not silently dropped. Only rows empty on BOTH are removed.
        id_col2 = _RETRIEVE_ID.get(h2)
        name_col2 = _NAME_COL.get(h2)
        if id_col2 and id_col2 in out.columns:
            dk = out[id_col2].map(_norm_key)
            if name_col2 and name_col2 in out.columns:
                dk = dk.where(dk != "", out[name_col2].map(_norm_key))
            out = out.assign(__dk__=dk)
            out = out[out["__dk__"] != ""].drop_duplicates(subset="__dk__", keep="first")
            out = out.drop(columns="__dk__")
        truncated = len(out) > _JOIN_MAX_ROWS
        if truncated:
            out = out.head(_JOIN_MAX_ROWS)
        r_start = _resolved_name(df1, start_type) or start
        note = (f" [resolved: '{start}'→{r_start}]"
                if r_start.strip().lower() != str(start).strip().lower() else "")
        msg = (f"{r_start} → {len(ids)} {h1}(s) → {len(out)} {h1}↔{h2} rows.{note}")
        return await _emit(out, tool="traverse_tool", prefix="traverse_tool",
                           connection_id=connection_id, raw=raw, message=msg,
                           metadata={"start": st, "hop1": h1, "hop2": h2, "ids": len(ids)},
                           truncated=truncated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[traverse] failed: %s", exc, exc_info=True)
        return _err(f"traverse failed: {exc}", tool="traverse_tool", raw=raw)


# ── evidence_tool: target↔disease evidence by datasource ──────────────────────

@function_tool(
    strict_mode=False,
    name_override="evidence_tool",
    description_override=(
        "The per-datasource EVIDENCE behind a target↔disease association — answers "
        "'what is the evidence linking gene X to disease Y?', 'why is X associated "
        "with Y?', 'genetic/literature/animal-model evidence for X in Y'. Returns "
        "individual evidence rows (datasource, datatype, score, PMIDs, clinical "
        "stage). Params: target (gene symbol/Ensembl), disease (name/EFO/MONDO), "
        "optional datasource (e.g. 'europepmc','cancer_gene_census','eva',"
        "'ot_genetics_portal','impc','chembl'), size. NOT for the aggregated "
        "association score (use target_tool/disease_tool)."
    ),
)
async def evidence_tool(
    target: str,
    disease: str,
    datasource: Optional[str] = None,
    size: int = 50,
    connection_id: Optional[str] = None,
) -> TableOutput:
    raw = f"evidence({target} x {disease})"
    try:
        df, (tname, dname), total = await get_target_disease_evidence(target, disease, datasource, size)
        if df is None or df.empty:
            ds_note = f" from datasource '{datasource}'" if datasource else ""
            return _err(f"No Open Targets evidence linking {tname} and {dname}{ds_note}.",
                        tool="evidence_tool", raw=raw)
        df = _sort_by_score(_normalize_cols(df), "evidence_score")
        srcs = sorted({s for s in df["datasource"].dropna().tolist()})
        ds_note = f" (datasource={datasource})" if datasource else ""
        # truncation: `total` is the real count; df is capped at `size`
        truncated = total > len(df)
        count_note = f"{len(df)} of {total}" if truncated else f"{total}"
        # surface resolution so an ambiguous-abbrev mis-resolve is visible
        rdiffs = []
        if str(tname).strip().lower() != str(target).strip().lower():
            rdiffs.append(f"'{target}'→{tname}")
        if str(dname).strip().lower() != str(disease).strip().lower():
            rdiffs.append(f"'{disease}'→{dname}")
        res_note = (" [resolved: " + ", ".join(rdiffs) + "]") if rdiffs else ""
        msg = (f"{count_note} evidence record(s) linking {tname} ↔ {dname}{ds_note}{res_note}; "
               f"datasources: {', '.join(srcs[:10])}"
               f"{' …' if len(srcs) > 10 else ''}.")
        return await _emit(df, tool="evidence_tool", prefix="evidence_tool",
                           connection_id=connection_id, raw=raw, message=msg,
                           metadata={"target": tname, "disease": dname, "datasource": datasource,
                                     "datasources": srcs, "total": total, "returned": len(df)},
                           truncated=truncated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[evidence_tool] failed: %s", exc, exc_info=True)
        return _err(f"evidence lookup failed: {exc}", tool="evidence_tool", raw=raw)
