"""Shared constants and helpers used by utility_target, utility_disease, and utility_drug."""

from __future__ import annotations

import logging
import os
import uuid
from typing import List, Optional

import pandas as pd

logger = logging.getLogger("uvicorn.error").getChild("opentargets.shared")

RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/app/results").rstrip("/")
MAX_PREVIEW_ROWS = int(os.environ.get("OT_PREVIEW_ROWS", "50"))
MAX_ASSOC_ROWS = int(os.environ.get("OT_MAX_ASSOC_ROWS", "0"))  # 0 = unlimited


def _safe(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum() or c in ("-_"))


def _csv_path(prefix: str, suffix: str = "") -> str:
    suffix = _safe(suffix) or uuid.uuid4().hex
    path = os.path.join(RESULTS_ROOT, f"{prefix}_{suffix}.csv")
    logger.info(f"[csv path]: {path}")
    return path


def _is_missing_field_error(exc: Exception, field: str, type_name: str) -> bool:
    """True when *exc* is an OpenTargets GraphQL "Cannot query field" error for
    *field* on *type_name* (used to detect schema drift across API versions)."""
    msg_parts = [str(exc)]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            msg_parts.append(resp.text or "")
        except Exception:
            pass
    msg = " ".join(msg_parts)
    return (
        "Cannot query field" in msg
        and f"'{field}'" in msg
        and f"type '{type_name}'" in msg
    )


def extract_surface_forms(entities, entity_type: str) -> List[str]:
    return [e.surface_form for e in entities if e.type == entity_type and e.surface_form]


def is_explicit_entity(e) -> bool:
    return (
        e.surface_form is not None
        and e.type is not None
        and e.id != "requested"
        and getattr(e, "resolution_method", None) != "implicit_request"
    )


async def apply_ontology_filter(
    df, *, col, input_names, expander,   # expander: async callable(name)->List[str], normalized, never raises
    exec_log, step, action, detail_key, log_prefix="", expand_noun="terms",
):
    universe_lc = {u.lower() for u in df[col] if u}
    expanded_terms = set()
    for name in input_names:
        try:
            expanded = [t.lower().strip() for t in (await expander(name) or [])]
            expanded_terms.update(expanded)
            logger.info(f"{log_prefix} Expanded '{name}' to {len(expanded)} {expand_noun}")
        except Exception as e:
            logger.warning(f"{log_prefix} Expansion failed for '{name}': {e}")
    overlapping_terms = sorted(expanded_terms & universe_lc)
    if not overlapping_terms and input_names:
        overlapping_terms = sorted({t.lower().strip() for t in input_names} & universe_lc)
    before = len(df)
    if not overlapping_terms:
        # Expansion failed AND no exact match — the filter term could not be resolved
        # to any value in this column (e.g. "PD-1" classified as drug but is a protein
        # alias, "cancer immunotherapy" is a modality phrase not an OT disease entity).
        # Skip the filter to avoid false-zero results rather than keeping only null rows.
        logger.warning(
            f"{log_prefix} {step}: no expansion and no exact match for {input_names!r}; "
            "skipping filter to avoid false-zero results"
        )
        exec_log.add(step=step, action=f"{action} (SKIPPED — unresolvable term)", before=before, after=before, details={
            detail_key: list(input_names),
            "expanded_terms_used": [],
            "expanded_terms_used_count": 0,
        })
        return df
    overlap_lc = set(overlapping_terms)
    mask = (df[col].notna() & df[col].str.lower().isin(overlap_lc)) | df[col].isna() | (df[col] == "")
    df = df[mask]
    after = len(df)
    exec_log.add(step=step, action=action, before=before, after=after, details={
        detail_key: list(input_names),
        "expanded_terms_used": overlapping_terms[:10],
        "expanded_terms_used_count": len(overlapping_terms),
    })
    logger.info(f"{log_prefix} {step}: {before} -> {after} rows")
    return df


async def save_and_publish_csv(
    df: pd.DataFrame,
    connection_id: Optional[str],
    path_prefix: str,
    log_label: str,
    service_name: str,
    row_count: int,
) -> Optional[str]:
    """Write *df* to a uniquely-named CSV and publish a WebSocket notification.

    Returns the CSV path on success, or None if ``connection_id`` is falsy or
    the write/publish step raises an exception.
    """
    if not connection_id:
        return None

    from .redis import _publish_ws  # local import avoids circular dependency at module load

    csv_path = _csv_path(path_prefix)
    try:
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        df.to_csv(csv_path, index=False)
        logger.info(
            "[%s function] %s CSV saved: %s (%d rows)",
            service_name,
            log_label,
            csv_path,
            df.shape[0],
        )
        await _publish_ws(connection_id, csv_path, row_count, service_name=service_name)
    except Exception as exc:
        logger.error(
            "[%s function] %s CSV write failed: %s",
            service_name,
            log_label,
            exc,
            exc_info=True,
        )
        csv_path = None

    return csv_path
