"""Shared CSV payload helpers used by the chat orchestrators.

Extracted on 2026-05-17 from byte-near-identical copies that lived
in the per-DB chat services and `opentarget_service/app/main.py`.

Every chat service that builds a `legacy_table` event payload over the
websocket needs:
  • `_infer_columns_from_rows` — best-effort column order from list-of-dict
  • `_rows_to_csv` — DictWriter with deterministic column order
  • `_build_legacy_table_payload` — the websocket event envelope
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable


def _infer_columns_from_rows(rows: Iterable[Any]) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows or []:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(str(k))
    return cols


def _rows_to_csv(rows: Iterable[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows or []:
        w.writerow({k: r.get(k, "") for k in columns})
    return buf.getvalue()


def _build_legacy_table_payload(
    *,
    columns: list[str],
    rows: list[dict],
    csv_text: str,
    csv_name: str,
    event_type: str,
    csv_path: str | None = None,
    row_count: int | None = None,
) -> dict:
    payload = {
        "type": event_type,
        "columns": columns,
        "rows": rows,
        "csv": csv_text,
        "csv_name": csv_name,
        "csv_path": csv_path,
    }
    if row_count is not None:
        payload["row_count"] = row_count
    return payload


