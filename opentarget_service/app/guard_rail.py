


"""Data models for API responses and tool outputs."""

from __future__ import annotations
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, ConfigDict, Field




class ResolvedEntity(BaseModel):
    """Represents a resolved biomedical entity.
    
    Attributes:
        surface_form: Original text from query
        type: Entity type (disease, target, drug)
        id: Resolved entity ID
        resolution_method: How entity was resolved
    """
    surface_form: Optional[str] = None
    type: Optional[Literal["disease", "target", "drug", "mechanism_of_action", "pathway"]] = None
    id: Optional[str] = None
    resolution_method: Literal["mapIds", "not_found", "implicit_request", 'Web']
    
    model_config = ConfigDict(extra="forbid")


class QueryResolution(BaseModel):
    """Result of query resolution.
    
    Attributes:
        query: Original user query
        resolved_entities: List of resolved entities
        message: Human-readable resolution message
        tool: Tool that produced this resolution
    """
    query: str
    resolved_entities: List[ResolvedEntity]
    message: str
    tool: str = "interpreter"
    paraphrased_query: str
    look_up_category: Optional[Literal["disease", "target", "drug", "web"]] = None
    
    model_config = ConfigDict(extra="forbid")


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


class RequestedTypes(BaseModel):
    """Requested entity types from query.
    
    Attributes:
        requested_types: List of entity types requested
    """
    requested_types: List[Literal["drug", "target", "disease"]]
    
    model_config = ConfigDict(extra="forbid")


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
        entities: Extracted entity surface forms
        requested_types: Requested entity types
    """
    entities: List[str]
    requested_types: List[Literal["drug", "target", "disease", "mechanism_of_action", "pathway"]]
    
    model_config = ConfigDict(extra="forbid")
# ============================================================================
# WebSocket Message Models
# ============================================================================

class TableEvent(BaseModel):
    """WebSocket table event payload."""
    type: Literal["table"] = "table"
    table_id: str
    title: str
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    total_rows: int
    csv_path: str
    csv_filename: str
    download_url: str
    
    model_config = ConfigDict(extra="forbid")


class ErrorEvent(BaseModel):
    """WebSocket error event payload."""
    type: Literal["error"] = "error"
    message: str
    tool: Optional[str] = None
    
    model_config = ConfigDict(extra="forbid")


class StatusEvent(BaseModel):
    """WebSocket status event payload."""
    type: Literal["status"] = "status"
    status: Literal["processing", "complete", "error"]
    message: Optional[str] = None
    
    model_config = ConfigDict(extra="forbid")
