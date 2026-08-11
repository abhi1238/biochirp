from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
import redis.asyncio as redis
from dataclasses import dataclass, field
from agents import Agent, Runner, function_tool
from .guard_rail import TableOutput, QueryResolution
import json


base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")


@dataclass
class ToolExecutionLog:
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        step: str,
        action: str,
        before: Optional[int] = None,
        after: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        entry = {
            "step": step,
            "action": action,
            "rows_before": before,
            "rows_after": after,
            "delta": (before - after) if (before is not None and after is not None) else None,
            "details": details or {},
        }
        self.steps.append(entry)

        # Also log to console
        line = f"[EXEC_LOG][{step}] {action}"
        if before is not None:
            line += f" | {before} → {after} rows"
            if entry["delta"] is not None:
                line += f" (Δ{entry['delta']:+d})"
        if details:
            line += f" | {details}"
        logger.info(line)

    def to_text(self) -> str:
        out = []
        for s in self.steps:
            line = f"[{s['step']}] {s['action']}"
            if s["rows_before"] is not None:
                line += f" | {s['rows_before']} → {s['rows_after']}"
            if s["details"]:
                line += f" | details={s['details']}"
            out.append(line)
        return "\n".join(out)

    def to_filter_trace(self) -> List[Dict[str, Any]]:
        """Convert steps to the filter_trace format pipeline.py renders as a funnel."""
        _STEP_TO_COLUMN = {
            "association_retrieval": "initial fetch",
            "drug_indication_retrieval": "initial fetch",
            "target_drug_retrieval": "initial fetch",
            "drug_filter": "drug_name",
            "target_filter": "gene_name",
            "mechanism_filter": "mechanism_of_action",
            "disease_filter": "disease_name",
            "deduplication": "deduplication",
            "score_filter": "score",
            "phase_filter": "phase",
        }
        trace = []
        for s in self.steps:
            if s["rows_before"] is None and s["rows_after"] is None:
                continue
            col = _STEP_TO_COLUMN.get(s["step"], s["step"])
            details = s.get("details") or {}
            input_values = (
                details.get("input_drugs")
                or details.get("input_targets")
                or details.get("input_mechanisms")
                or details.get("expanded_terms_used")
                or ([details["disease"]] if "disease" in details else [])
                or []
            )
            trace.append({
                "column": col,
                "rows_before": s["rows_before"],
                "rows_after": s["rows_after"],
                "input_values": list(input_values)[:10],
                "table": "",
            })
        return trace
