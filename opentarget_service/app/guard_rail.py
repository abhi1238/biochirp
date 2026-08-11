


"""Data models for API responses and tool outputs."""

from __future__ import annotations
import json as _json
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, ConfigDict, Field, model_validator




class ResolvedEntity(BaseModel):
    """Represents a resolved biomedical entity.

    Attributes:
        surface_form: Original text from query
        type: Entity type (disease, target, drug)
        id: Resolved entity ID
        resolution_method: How entity was resolved
    """
    surface_form: Optional[str] = None
    # NOTE: Using Optional[str] (not Literal) so that the JSON schema sent to
    # Groq's tool-call validator does not use anyOf[enum, null].  Groq rejects
    # tool calls when the model passes a valid value that doesn't precisely
    # match the enum (even when the same value would deserialise correctly
    # after the API returns it).  The tool code filters by the valid values
    # internally, so widening the schema here is safe.
    type: Optional[str] = None
    id: Optional[str] = None
    resolution_method: Optional[str] = None  # wide type; interpreter guarantees one of: mapIds/not_found/implicit_request/Web/search

    model_config = ConfigDict(extra="ignore")


class QueryResolution(BaseModel):
    """Result of query resolution.

    Attributes:
        query: Original user query
        resolved_entities: List of resolved entities
        message: Human-readable resolution message
        tool: Tool that produced this resolution
    """
    # `query`/`resolved_entities` carry defaults for the SAME reason as the
    # Optional[str] fields below: when the orchestrator LLM forwards the
    # interpreter result as the `input` arg it occasionally omits a field, and
    # Groq's strict tool-call validator then rejects the WHOLE call ("missing
    # properties: 'query'") — hard-failing the turn. `query` is only used as the
    # display `raw_query`, and the tools guard an empty entity list gracefully,
    # so defaulting them degrades softly instead of erroring.
    query: str = ""
    resolved_entities: List[ResolvedEntity] = []
    message: str = ""
    tool: str = "interpreter"
    paraphrased_query: str = ""
    # Wide Optional[str] types below: Groq's strict tool-call validator
    # rejects anyOf[enum, null] fields when the model omits them or passes
    # a synonym.  The code uses these values with .get() guards and falls
    # back gracefully, so widening is safe.
    look_up_category: Optional[str] = None
    requested_output: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_from_str(cls, v: object) -> object:
        # The agents SDK passes the interpreter's return value as a str to the
        # model, and the model may forward that same str as the `input` argument
        # to disease_tool/target_tool/drug_tool. Accept a JSON string so the
        # tool can still deserialise correctly.
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except (_json.JSONDecodeError, ValueError):
                pass
        return v


class TableOutput(BaseModel):
    """Standard table output from data tools.

    Attributes:
        status: Success or error status
        raw_query: Original user query
        message: Human-readable result message
        table: Preview data (first N rows)
        csv_path: Full path to CSV file for download
        row_count: Total number of rows
        tool: Tool that produced this output
        database: Database/source name
        description: Additional context about the data
        synonym: Synonyms of entry
    """
    status: Literal["success", "error"]
    raw_query: str
    message: Optional[str] = None
    # table: Optional[List[Dict[str, Any]]] = None
    table: dict = None
    csv_path: Optional[str] = None
    row_count: int = 0
    preview_row_count: Optional[int] = None
    is_truncated: Optional[bool] = None
    tool: str
    database: str = "OpenTargets"
    description: Optional[str] = None
    synonym: List[str] = None
    metadata: Optional[Dict[str, Any]] = None
    filter_trace: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="forbid")

    def get_download_filename(self) -> Optional[str]:
        """Extract filename from csv_path for download URL."""
        if self.csv_path:
            import os
            return os.path.basename(self.csv_path)
        return None

    def to_frontend_dict(self) -> Dict[str, Any]:
        """Convert to frontend-friendly dictionary with download URL."""
        result = self.model_dump()
        if self.csv_path:
            result["download_url"] = f"/download/{self.get_download_filename()}"
            result["csv_filename"] = self.get_download_filename()
        return result


# class CombinedOutput(BaseModel):
#     """Combined NER and intent detection output.

#     Attributes:
#         entities: Extracted entity surface forms
#         requested_types: Requested entity types
#     """
#     entities: List[str]
#     requested_types: List[Literal["drug", "target", "disease", "mechanism_of_action", "pathway"]]

#     model_config = ConfigDict(extra="forbid")

class CombinedOutput(BaseModel):
    """Combined NER and intent detection output.

    Attributes:
        entities: Extracted entity surface forms (guardrail-filtered, literal substring of query)
        entities_raw: Pre-guardrail entities as returned by NER (may include expansions/normalizations)
        requested_types: Requested entity types

    NOTE: "disease" is included for drug→disease intent (e.g. "which diseases
    does valdecoxib treat?"). Keep this Literal in sync with the prompt's
    allowed enum and the filter in resolvers.py call_grok().
    """
    entities: List[str]
    entities_raw: List[str] = []
    requested_types: List[Literal["drug", "target", "disease", "mechanism_of_action", "pathway"]]

    model_config = ConfigDict(extra="ignore")
