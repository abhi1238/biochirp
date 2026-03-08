from typing import Set, List, Optional, Dict, Any
import os
import uuid
import logging
import pandas as pd
import redis.asyncio as redis
from dataclasses import dataclass, field
from agents import Agent, Runner, function_tool, WebSearchTool
from .guard_rail import TableOutput, QueryResolution
from .trace_explainer import ExecutionTraceExplainer
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
