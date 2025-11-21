from pydantic import BaseModel, Field
from typing import List, Optional, Union
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Extra, Field, constr, model_validator, ValidationError


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
    # Field now contains ONLY the answer
    message: Any  = Field(..., example="Default response from WEB tool is returned..")
    tool: str = Field(..., example="web")
    # message: str =  Field(..., example="Sucessfuly finished Web tool call.")


class TavilyInput(BaseModel):
    query: str = Field(..., example="What is drug for tb?")

class TavilyOutput(BaseModel):
    # Field now contains ONLY the answer
    message: Any = Field(..., example="Default response from Tavily tool is returned..")
    tool: str = Field(..., example="tavily")
    # message: str =  Field(..., example="Sucessfuly finished Tavily tool call.")


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

# AllowedScalar = Literal["requested"]
# AllowedValue = Union[AllowedScalar, List[str]]

# class CommonFields(BaseModel):
#     drug_name: Optional[AllowedValue] = Field(default=None)
#     target_name: Optional[AllowedValue] = Field(default=None)
#     gene_name: Optional[AllowedValue] = Field(default=None)
#     disease_name: Optional[AllowedValue] = Field(default=None)
#     pathway_name: Optional[AllowedValue] = Field(default=None)
#     biomarker_name: Optional[AllowedValue] = Field(default=None)
#     drug_mechanism_of_action_on_target: Optional[AllowedValue] = Field(default=None)
#     approval_status: Optional[AllowedValue] = Field(default=None)

#     class Config:
#         extra = "forbid"

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


# class QueryInterpreterOutputGuardrail(BaseModel):
#     """LLM-powered query interpreter output."""
#     cleaned_query: str = Field(..., example="What is the drug for fever?")
#     status: str = Field(..., example="valid")
#     route: str = Field(..., example="biochirp")
#     message: str =  Field(..., example="Your question is clear. BioChirp will answer using its workflow.")
#     parsed_value: ParsedValue
#     tool: str = Field(..., example="interpreter")

#     class Config:
#         extra = "forbid"


# from typing import Optional
# from pydantic import BaseModel, Field

class QueryInterpreterOutputGuardrail(BaseModel):
    """LLM-powered query interpreter output."""
    cleaned_query: Optional[str] = Field(default=None, example="What is the drug for fever?")
    status: Optional[str] = Field(default=None, example="valid")
    route: Optional[str] = Field(default=None, example="biochirp")
    message: Optional[str] = Field(default=None, example="Your question is clear. BioChirp will answer using its workflow.")
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


# class ExpandMemberOutput(BaseModel):
#     database: str = Field(..., example="ttd")
#     value: Optional[OutputFields] = None
#     tool: str = Field(..., example="expand_synonyms")

# ExpandMemberOutput.model_rebuild()


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