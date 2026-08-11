"""Shared chat-orchestrator helpers used by /opentarget and per-DB chat.

Extracted on 2026-05-17 from byte-near-identical copies in
`opentarget_service/app/main.py` and the per-DB chat services.

Three groups of helpers:

  • `is_orchestrator_metadata` — heuristic that decides whether a stringified
    LLM output is internal routing metadata (should be suppressed from the
    user) or genuine response content (should be displayed).
  • `_unescape_repr` / `_extract_display_text` — pull a human-readable
    message out of arbitrary LLM/tool output shapes (dict, JSON, repr).
  • `publish_table_records_legacy` / `publish_table_from_output` — write a
    `legacy_table` event payload to Redis pubsub so the websocket front-end
    can render it. Both take an already-resolved async redis client.

`is_orchestrator_metadata` accepts the superset of fallback patterns from
the two original copies, so neither service loses sensitivity. Pass
`extra_patterns=...` if you want to add per-service patterns later.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Optional

from app.utils.table_formatters import (
    _build_legacy_table_payload,
    _infer_columns_from_rows,
    _rows_to_csv,
)

# Union of every JSON-decode-failure substring pattern the two services
# matched against pre-extraction. Adding more here can only widen detection
# of orchestrator metadata strings.
DEFAULT_ORCHESTRATOR_METADATA_PATTERNS: tuple[str, ...] = (
    "inputquery",
    "parsed_value",
    '"tool":"ttd',
    '"tool":"ctd',
    '"tool":"hcdt',
    '"tool":"target_tool',
    '"tool":"disease_tool',
    '"tool":"drug_tool',
)


def is_orchestrator_metadata(
    text: str,
    tool_name: str | None = None,
    *,
    extra_patterns: Iterable[str] = (),
) -> bool:
    if tool_name and tool_name.lower() == "interpreter":
        return False
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            keys = {
                "inputquery",
                "inputcleaned_query",
                "parsed_value",
                "status",
                "route",
                "tool",
                "messages",
            }
            if any(k in obj for k in keys):
                if "tool" in obj and not any(
                    k in obj
                    for k in ["result", "data", "table", "rows", "answer", "reasoning"]
                ):
                    return True
                # Small dict with no reasoning keys is almost certainly metadata.
                return len(obj) < 5 and "reasoning" not in obj
        return False
    except json.JSONDecodeError:
        patterns = (*DEFAULT_ORCHESTRATOR_METADATA_PATTERNS, *extra_patterns)
        lowered = text.lower()
        return any(p in lowered for p in patterns)


def _unescape_repr(s: str) -> str:
    return (
        s.replace(r"\\", "\\")
        .replace(r"\'", "'")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
    )


def _extract_display_text(output: Any, tool_name: Optional[str] = None) -> Optional[str]:
    if output is None:
        return None

    if isinstance(output, dict):
        if "message" in output and output["message"] is not None:
            return str(output["message"])
        for k in ("output", "text", "answer", "explanation", "detail"):
            v = output.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for _, v in output.items():
            if isinstance(v, dict) and "message" in v:
                mv = v.get("message")
                if mv is not None:
                    return str(mv)

    s = str(output)

    if '"message"' in s or s.strip().startswith("{"):
        try:
            j = json.loads(s)
            return _extract_display_text(j, tool_name)
        except Exception:
            pass

    m = re.search(r"message\s*=\s*'((?:\\'|[^'])*)'", s)
    if not m:
        m = re.search(r'message\s*=\s*"((?:\\"|[^"])*)"', s)
    if m:
        return _unescape_repr(m.group(1))

    m = re.search(r'"message"\s*:\s*"((?:\\.|[^"\\])*)"', s)
    if m:
        return _unescape_repr(m.group(1))

    if not is_orchestrator_metadata(s, tool_name or ""):
        return s
    return None


async def publish_table_records_legacy(
    redis_client,
    *,
    connection_id: str,
    rows,
    event_type: str,
    columns=None,
    csv_name: str = "results.csv",
    csv_path: str | None = None,
    limit_rows: int = 1000,
    row_count: int | None = None,
) -> None:
    if not connection_id:
        raise ValueError("publish_table_records_legacy requires a connection_id")

    rows = rows or []
    columns = columns or _infer_columns_from_rows(rows)
    rows_view = rows[:limit_rows]
    csv_text = _rows_to_csv(rows_view, columns)
    payload = _build_legacy_table_payload(
        columns=columns,
        rows=rows_view,
        csv_text=csv_text,
        csv_name=csv_name,
        event_type=event_type,
        csv_path=csv_path,
        row_count=row_count,
    )
    await redis_client.publish(connection_id, json.dumps(payload))


async def publish_table_from_output(
    redis_client,
    *,
    output: dict,
    tool_key: str,
    connection_id: str,
    limit_rows: int = 50,
) -> None:
    rows = None
    if isinstance(output, dict):
        rows = output.get("table") or output.get("rows")

    if not isinstance(rows, list) or not rows:
        return

    csv_path = None
    row_count = None
    if isinstance(output, dict):
        csv_path = output.get("csv_path")
        row_count = output.get("row_count")

    columns = _infer_columns_from_rows(rows)
    rows_view = rows[:limit_rows]
    csv_name = f"{tool_key}_results_{int(time.time())}.csv"

    await publish_table_records_legacy(
        redis_client,
        connection_id=connection_id,
        rows=rows_view,
        columns=columns,
        event_type=f"{tool_key}_table",
        csv_name=csv_name,
        csv_path=csv_path,
        limit_rows=limit_rows,
        row_count=row_count if isinstance(row_count, int) else None,
    )
