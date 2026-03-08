from pydantic import BaseModel, Field
from typing import List, Optional, Union
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Extra, Field, constr, model_validator, ValidationError
from pydantic import BaseModel, Field, field_validator
import re

class Item(BaseModel):
    name: str = Field(..., example="Aspirin")
    description: str = Field(None, example="Pain reliever and fever reducer")
    price: float = Field(..., example=12.5)
    in_stock: bool = Field(..., example=True)


class OrchestratorInput(BaseModel):
    """
    Input for the interpreter agent.
    """
    query: str = Field(..., example="What is drug for tb?")


class OrchestratorOutput(BaseModel):
    # Field now contains ONLY the answer
    
    answer: str = Field(..., example="Default response from Orchestrator API is returned..")

class WebToolInput(BaseModel):
    query: str = Field(..., example="What is drug for tb?")



class WebToolOutput(BaseModel):
    message: str = Field(
        ..., 
        description="Search results or error message",
        example="The Taj Mahal is located in Agra, India."
    )
    tool: str = Field(
        default="web",
        description="Tool identifier",
        example="web"
    )
    
    @field_validator('message')
    @classmethod
    def clean_message(cls, v: str) -> str:
        """Remove control characters that break JSON parsing."""
        if not v:
            return v
        # Replace newlines with spaces
        v = v.replace('\n', ' ').replace('\r', ' ')
        # Remove any remaining control characters
        v = re.sub(r'[\x00-\x1F\x7F]', ' ', v)
        # Collapse multiple spaces
        v = re.sub(r'\s+', ' ', v)
        return v.strip()

class TavilyInput(BaseModel):
    query: str = Field(..., example="What is drug for tb?")



class TavilyOutput(BaseModel):
    message: str = Field(..., example="Default response from Tavily tool is returned..")
    tool: str = Field(..., example="tavily")

class ReadmeInput(BaseModel):
    query: str = Field(..., example="What I can ask you?")

class ReadmeOutput(BaseModel):
    # Field now contains ONLY the answer
    answer: str = Field(..., example="Default response from README API is returned..")
    tool: str = Field(...,  example="readme")
    message: str =  Field(..., example="Sucessfuly finished readme tool call.")

class InterpreterInput(BaseModel):
    """
    Input for the interpreter agent.
    """
    query: str = Field(..., example="What is drug for tb?")


class CommonFields(BaseModel):
    """All biomedical schema fields, used for input, query, and output stages."""
    drug_name: Optional[Union[str, List[str],  None]] = Field(default=None, example="requested")
    target_name: Optional[Union[str, List[str], None]] = Field(default=None)
    gene_name: Optional[Union[str, List[str], None]] = Field(default=None)
    disease_name: Optional[Union[str, List[str], None]] = Field(default=None, example=["Fever"])
    pathway_name: Optional[Union[str, List[str], None]] = Field(default=None)
    biomarker_name: Optional[Union[str, List[str], None]] = Field(default=None)
    drug_mechanism_of_action_on_target: Optional[Union[str, List[str], None]] = Field(default=None)
    approval_status: Optional[Union[str, List[str], None]] = Field(default=None)


    class Config:
        extra = "forbid"





class ParsedValue(CommonFields):
    """Fields extracted from user query after NER/LLM parsing."""
    class Config:
        extra = "forbid"
ParsedValue.model_rebuild()


class QueryInterpreterOutputGuardrail(BaseModel):
    """LLM-powered query interpreter output."""
    cleaned_query: Optional[str] = Field(default=None, example="What is the drug for fever?")
    status: Optional[str] = Field(default=None, example="valid")
    route: Optional[str] = Field(default=None, example="biochirp")
    message: Optional[str] = Field(default=None, example="Your question is clear. BioChirp will answer using its workflow.")
    relevant_databases: Optional[List[Literal["TTD", "CTD", "HCDT"]]] = Field(default=None, example=["TTD"])
    dropped_constraints: Optional[List[str]] = Field(default=None, example=["SMILES string"])
    parsed_value: ParsedValue
    tool: Optional[str] = Field(default=None, example="interpreter")

    class Config:
        extra = "forbid"



class OutputFields(CommonFields):
    """Filtered/matched fields after fuzzy/semantic DB matching."""
    class Config:
        extra = Extra.forbid
OutputFields.model_rebuild()


class FuzzyFilteredOutputs(BaseModel):
    database: str = Field(..., example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="fuzzy")

FuzzyFilteredOutputs.model_rebuild()



class SimilarityFilteredOutputs(BaseModel):
    database: str = Field(..., example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="fuzzy")

SimilarityFilteredOutputs.model_rebuild()




class PlanGenerator(BaseModel):
    """Dict of OutputFields for each database."""
    database: str = Field(default=None, example="ttd")
    plan: Any
    tool: str = Field(default=None, example="planner")

PlanGenerator.model_rebuild()


class DatabaseTable(BaseModel):
    database: str
    table: Optional[List[dict]] = None
    csv_path: Optional[str] = None
    row_count: Optional[int] = None
    tool: str
    message: Optional[str] = None



class ExpandSynonymsOutput(BaseModel):
    database: Optional[str] = Field(None, example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="expand_synonyms")

ExpandSynonymsOutput.model_rebuild()




class ExpandMemberOutput(BaseModel):
    database: str = Field(None, example="ttd")
    value: Optional[OutputFields] = None
    tool: str = Field(..., example="expand_and_match_db")
    message: Optional[str] = None
    errors: Optional[dict] = None

ExpandMemberOutput.model_rebuild()


class Llm_Member_Selector_Output(BaseModel):
    # value: Optional[OutputFields] = None
    value: List[Any] = Field(default_factory=list)

Llm_Member_Selector_Output.model_rebuild()



class Llm_Member_Selector_Input(BaseModel):
    # value: Optional[OutputFields] = None
    # value: List[Any] = Field(default_factory=list)
    category: str= Field(..., example="disease_name")

    single_term: str = Field(..., example="fever")
    string_list : List[str] = Field(..., example=["fever","cancer"])


Llm_Member_Selector_Input.model_rebuild()

class MemoryPair(BaseModel):
    question: str = Field(..., description="A previous user question (verbatim).")
    answer:   str = Field(..., description="The assistant's prior answer to that question (verbatim).")

MemoryPair.model_rebuild()

class MemoryToolInput(BaseModel):
    user_input:         str                   = Field(..., description="The user?s latest question.")
    last_5_pairs: List[MemoryPair]     = Field(..., description="Up to five prior question?answer pairs, from oldest to most recent.")

MemoryToolInput.model_rebuild()

class MemoryToolOutput(BaseModel):

    decision: Literal["RETRIEVAL", "MODIFY", "PASS"]
    message: str
    passed_question: str   
    retrieved_answer: Optional[str] = None      # Only for RETRIEVAL
    matched_question: Optional[str] = None      # Only for RETRIEVAL



MemoryToolOutput.model_rebuild()





class ShareIn(BaseModel):
    html: str
    title: Optional[str] = "BioChirp Chat"

class ShareOut(BaseModel):
    id: str
    url: str
    expires_in_seconds: int




class BioChirpClassification(BaseModel):
    decision: Literal[
        "README_RETRIEVAL",
        "BIOCHIRP_STRUCTURED_RETRIEVAL",
        "BIOMEDICAL_REASONING_REQUIRED",
        "BIOMEDICAL_OUT_OF_SCOPE_FOR_RETRIEVAL",
        "NON_BIOMEDICAL",
        "UNCLASSIFIABLE_OR_OTHER",
    ] = Field(
        ...,
        description="Single deterministic classification label"
    )

    message: str = Field(
        ...,
        min_length=5,
        max_length=800,
        description=(
            "2-4 short sentences explaining the classification decision. "
            "Must refer only to query type and decision rules. "
            "Must not restate the query or introduce biomedical facts."
        )
    )
