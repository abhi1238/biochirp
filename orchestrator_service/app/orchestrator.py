
import sys
from pathlib import Path
import optparse
import requests
from agents import Agent, Runner
from config.guardrail import OrchestratorInput, OrchestratorOutput
from app.web_tool import web
from app.interpreter_tool import interpreter
from app.readme_tool import readme
# from app.expand_and_match_db import expand_and_match_db
from app.tavily_tool import tavily
from app.ttd_tool import ttd
from app.ctd_tool import ctd
from app.hcdt_tool import hcdt
import os

# load_dotenv(override=True)

md_file_path = "/app/resources/prompts/agent_orchestrator.md"

# Read the file
with open(md_file_path, "r", encoding="utf-8") as f:
    prompt_md = f.read()

OrchestratorInput.model_rebuild()
OrchestratorOutput.model_rebuild()

AGENT_TIMEOUT_SEC = float(os.getenv("AGENT_TIMEOUT_SEC", "60"))
MODEL_NAME = os.getenv("INTERPRETER_MODEL_NAME", "gpt-5-mini")




async def run_orchestrator(input):

    # try:

    orchestrator_agent = Agent(
        name="Orchestrator", instructions= prompt_md,
        # tools=[web_tool, interpreter_tool, readme_tool],
        # tools=[web, interpreter_tool, readme_tool, expand_and_match_db, tavily],
        # tools=[web, interpreter_tool, readme_tool, ttd, ctd, hcdt],
        tools=[web, interpreter_tool, readme_tool, ctd, hcdt],
        model=MODEL_NAME)
        # output_type=OrchestratorOutput)

    # result = await Runner.run(orchestrator_agent, input.query)
    result = Runner.run_streamed(orchestrator_agent, input=input.query)


    return result
        
    #     fo = getattr(result, "final_output", None)

    #     if isinstance(fo, OrchestratorOutput):
    #         return fo
    #     elif isinstance(fo, dict):
    #         return OrchestratorOutput(
    #             answer=str(fo.get("answer", "No answer found."))
    #         )
    #     else:
    #         return OrchestratorOutput(
    #             answer=str(fo) if fo is not None else "Agent returned empty response."
    #         )
        
    # except Exception as e:
    #     # raise Exception(f"Orchestrator execution failed: {e}")
    #     return OrchestratorOutput(
    #                 answer=str(fo) if fo is not None else "Agent returned empty response."
    #             )