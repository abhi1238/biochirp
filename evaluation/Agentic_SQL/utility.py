import os
import re
import time
import asyncio
from typing import Dict, Any
from dotenv import load_dotenv
from phi.model.groq import Groq
import pandas as pd
from sqlalchemy import create_engine, text

load_dotenv()


WS_HCDT_URL = "wss://biochirp.iiitd.edu.in/hcdt_chat/"
WS_HCDT_DOWNLOAD_URL = "http://localhost:8029/download"

def extract_sql(response: str) -> str:
    """Extract SQL from LLM response."""
    pattern = r"```(?:sql)?\s*(.*?)\s*```"
    match = re.search(pattern, response, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else response.strip()

def execute_sql_safe(sql: str, engine) -> Dict[str, Any]:
    """Execute SQL and return results."""
    try:
        if "LIMIT" not in sql.upper():
            sql = f"{sql.rstrip(';')} LIMIT 100000;"
        
        with engine.connect() as conn:
            df = pd.read_sql(text(sql), conn)
        df = df.drop_duplicates()
        
        return {
            "dataframe": df,
            "sql": sql,
            "success": True,
            "error": None,
            "rows": len(df)
        }
    except Exception as e:
        return {
            "dataframe": pd.DataFrame(),
            "sql": sql,
            "success": False,
            "error": str(e),
            "rows": 0
        }

# =============================================================================
# LLM PROVIDER CALLS
# =============================================================================

async def _groq_call(user_prompt, system_prompt, model, temperature):
    """Call Groq API."""
    from groq import Groq
    
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    
    client = Groq(api_key=api_key)
    start = time.perf_counter()
    
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    
    latency = time.perf_counter() - start
    answer = resp.choices[0].message.content.strip()
    
    return {
        "provider": "groq",
        "model": model,
        "answer": answer,
        "latency": latency,
    }

async def _openai_call(user_prompt, system_prompt, model, temperature):
    """Call OpenAI API."""
    from openai import OpenAI
    
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    
    client = OpenAI(api_key=api_key)
    start = time.perf_counter()
    
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    
    latency = time.perf_counter() - start
    answer = resp.choices[0].message.content.strip()
    
    return {
        "provider": "openai",
        "model": model,
        "answer": answer,
        "latency": latency,
    }

async def _gemini_call(user_prompt, system_prompt, model, temperature):
    """Call Google Gemini API."""
    from google import genai
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    
    client = genai.Client(api_key=api_key)
    start = time.perf_counter()
    
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=user_prompt,
        config={
            "system_instruction": system_prompt,
            "temperature": temperature,
        },
    )
    
    latency = time.perf_counter() - start
    answer = resp.text.strip()
    
    return {
        "provider": "gemini",
        "model": model,
        "answer": answer,
        "latency": latency,
    }

async def _grok_call(user_prompt, system_prompt, model, temperature):
    """Call xAI Grok API."""
    import httpx
    from openai import OpenAI
    
    api_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or os.getenv("GROK_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY not set")
    
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
        timeout=httpx.Timeout(600.0, connect=60.0)
    )
    
    start = time.perf_counter()
    
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
    )
    
    latency = time.perf_counter() - start
    answer = resp.choices[0].message.content.strip()
    
    return {
        "provider": "grok",
        "model": model,
        "answer": answer,
        "latency": latency,
    }

async def llm_generate(provider: str, user_prompt: str, system_prompt: str, model: str, temperature: float = 0.0):
    """Unified LLM adapter."""
    if provider == "groq":
        return await _groq_call(user_prompt, system_prompt, model, temperature)
    if provider == "openai":
        return await _openai_call(user_prompt, system_prompt, model, temperature)
    if provider == "gemini":
        return await _gemini_call(user_prompt, system_prompt, model, temperature)
    if provider == "grok":
        return await _grok_call(user_prompt, system_prompt, model, temperature)
    raise ValueError(f"Unknown provider: {provider}")

# # =============================================================================
# # AGENT FACTORY (CREATE ONCE PER MODEL)
# # =============================================================================

# class BiomedicalSQLAgent:
#     """Wrapper for LLM-based SQL generation agent."""
    
#     def __init__(self, provider: str, model: str, system_prompt: str):
#         self.provider = provider
#         self.model = model
#         self.system_prompt = system_prompt
    
#     async def generate_sql(self, question: str) -> Dict[str, Any]:
#         """Generate SQL for a question."""
#         llm_result = await llm_generate(
#             provider=self.provider,
#             user_prompt=question,
#             system_prompt=self.system_prompt,
#             model=self.model,
#             temperature=0.0
#         )
        
#         sql = extract_sql(llm_result["answer"])
        
#         return {
#             "sql": sql,
#             "latency": llm_result["latency"],
#             "raw_response": llm_result["answer"]
#         }

# def create_agent(provider: str, model: str, SYSTEM_PROMPT:str) -> BiomedicalSQLAgent:
#     """
#     Create a BiomedicalSQLAgent with the system prompt.
    
#     This is called ONCE per model, similar to Phidata's create_phidata_agent().
#     """
#     return BiomedicalSQLAgent(
#         provider=provider,
#         model=model,
#         system_prompt=SYSTEM_PROMPT  # System prompt defined once at top
#     )



# =============================================================================
# ABSTRACT INTERFACE (COMMON FOR ALL FRAMEWORKS)
# =============================================================================

from abc import ABC, abstractmethod
from typing import Dict, Any

class BiomedicalSQLAgent(ABC):
    """Abstract base class for SQL generation agents."""
    
    def __init__(self, provider: str, model: str, system_prompt: str):
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
    
    @abstractmethod
    async def generate_sql(self, question: str) -> Dict[str, Any]:
        """
        Generate SQL for a question.
        
        Returns:
            Dict with keys: sql, latency, raw_response
        """
        pass

# =============================================================================
# LANGCHAIN IMPLEMENTATION
# =============================================================================

class LangChainSQLAgent(BiomedicalSQLAgent):
    """LangChain-based SQL generation agent."""
    
    async def generate_sql(self, question: str) -> Dict[str, Any]:
        """Generate SQL using LangChain."""
        llm_result = await llm_generate(
            provider=self.provider,
            user_prompt=question,
            system_prompt=self.system_prompt,
            model=self.model,
            temperature=0.0
        )
        
        sql = extract_sql(llm_result["answer"])
        
        return {
            "sql": sql,
            "latency": llm_result["latency"],
            "raw_response": llm_result["answer"]
        }

# =============================================================================
# PHIDATA IMPLEMENTATION
# =============================================================================
class PhidataSQLAgent(BiomedicalSQLAgent):
    """Phidata-based SQL generation agent with multi-provider support."""
    
    def __init__(self, provider: str, model: str, system_prompt: str):
        super().__init__(provider, model, system_prompt)
        
        from phi.agent import Agent
        
        # Get LLM based on provider
        llm = self._create_llm(provider, model)
        
        self.agent = Agent(
            model=llm,
            instructions=system_prompt,
            markdown=False,
            show_tool_calls=False,
            debug_mode=False
        )
    
    def _create_llm(self, provider: str, model: str):
        """Create LLM instance based on provider."""
        
        if provider == "openai":
            from phi.model.openai import OpenAIChat
            return OpenAIChat(
                id=model,
                api_key=os.getenv("OPENAI_API_KEY")
            )
        
        elif provider == "groq":
            from phi.model.groq import Groq
            return Groq(
                id=model,
                api_key=os.getenv("GROQ_API_KEY")
            )
        
        elif provider == "gemini":
            from phi.model.google import Gemini
            return Gemini(
                id=model,
                api_key=os.getenv("GEMINI_API_KEY")
            )
        
        elif provider == "grok":
            # Grok uses OpenAI-compatible API
            from phi.model.openai import OpenAIChat
            
            api_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or os.getenv("GROK_KEY")
            if not api_key:
                raise RuntimeError("XAI_API_KEY, GROK_API_KEY, or GROK_KEY not set")
            
            return OpenAIChat(
                id=model,
                api_key=api_key,
                base_url="https://api.x.ai/v1"
            )
        
        elif provider == "anthropic":
            from phi.model.anthropic import Claude
            return Claude(
                id=model,
                api_key=os.getenv("ANTHROPIC_API_KEY")
            )
        
        else:
            raise ValueError(f"Unsupported provider: {provider}. Supported: openai, groq, gemini, grok, anthropic")
    
    async def generate_sql(self, question: str) -> Dict[str, Any]:
        """Generate SQL using Phidata."""
        import time
        
        start = time.perf_counter()
        response = await asyncio.to_thread(self.agent.run, question)
        latency = time.perf_counter() - start
        
        sql = extract_sql(str(response.content))
        
        return {
            "sql": sql,
            "latency": latency,
            "raw_response": str(response.content)
        }

class PydanticAISQLAgent(BiomedicalSQLAgent):
    """PydanticAI-based SQL generation agent."""
    
    def __init__(self, provider: str, model: str, system_prompt: str):
        super().__init__(provider, model, system_prompt)
        
        from pydantic_ai import Agent
        
        # Create model string
        model_str = self._get_model_string(provider, model)
        
        # Create agent
        self.agent = Agent(
            model_str,
            system_prompt=system_prompt,
        )
    
    def _get_model_string(self, provider: str, model: str) -> str:
        """Get PydanticAI model string."""
        
        if provider == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY not set")
            return f"openai:{model}"
        
        elif provider == "groq":
            if not os.getenv("GROQ_API_KEY"):
                raise RuntimeError("GROQ_API_KEY not set")
            return f"groq:{model}"
        
        elif provider == "gemini":
            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY not set")
            return model
        
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            return f"anthropic:{model}"
        
        elif provider == "grok":
            api_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or os.getenv("GROK_KEY")
            if not api_key:
                raise RuntimeError("XAI_API_KEY, GROK_API_KEY, or GROK_KEY not set")
            
            # Temporarily set environment variables for PydanticAI
            # PydanticAI's OpenAI model reads from OPENAI_API_KEY and OPENAI_BASE_URL
            os.environ["OPENAI_API_KEY"] = api_key
            os.environ["OPENAI_BASE_URL"] = "https://api.x.ai/v1"
            
            # Use openai: prefix to use OpenAI-compatible interface
            return f"openai:{model}"
        
        else:
            raise ValueError(f"Unsupported provider: {provider}")
    
    async def generate_sql(self, question: str) -> Dict[str, Any]:
        """Generate SQL using PydanticAI."""
        import time
        
        start = time.perf_counter()
        
        # Run agent
        result = await self.agent.run(question)
        
        latency = time.perf_counter() - start
        
        sql = extract_sql(str(result.output))
        
        return {
            "sql": sql,
            "latency": latency,
            "raw_response": str(result.output) 
        }
# =============================================================================
# CREWAI IMPLEMENTATION
# =============================================================================
# =============================================================================
# CREWAI IMPLEMENTATION WITH GROK SUPPORT
# =============================================================================

class CrewAISQLAgent(BiomedicalSQLAgent):
    """CrewAI-based SQL generation agent with Grok support."""
    
    def __init__(self, provider: str, model: str, system_prompt: str):
        super().__init__(provider, model, system_prompt)
        
        from crewai import Agent, LLM
        
        # Create LLM with proper configuration
        llm = self._create_llm(provider, model)
        
        self.agent = Agent(
            role="SQL Query Specialist",
            goal="Generate accurate SQL queries",
            backstory=system_prompt,
            llm=llm,
            verbose=False,
            allow_delegation=False
        )
    
    def _create_llm(self, provider: str, model: str):
        """
        Create CrewAI LLM.
        
        CrewAI uses LiteLLM format:
        - openai/gpt-4o-mini
        - groq/llama-3.3-70b-versatile
        - gemini/gemini-2.0-flash-exp
        - For Grok: use openai/ prefix with custom base_url
        """
        from crewai import LLM
        

        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not set")
            
            # Support custom base URL
            base_url = os.getenv("OPENAI_BASE_URL")
            
            return LLM(
                model=f"openai/{model}",
                api_key=api_key,
                base_url=base_url if base_url else None,
                temperature=0.0
            )
        
        elif provider == "groq":
            if not os.getenv("GROQ_API_KEY"):
                raise RuntimeError("GROQ_API_KEY not set")
            
            return LLM(
                model=f"groq/{model}",
                temperature=0.0
            )
        
        elif provider == "gemini":
            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY not set")
            
            return LLM(
                model=f"gemini/{model}",
                temperature=0.0
            )
        
        elif provider == "grok":
            # ✅ GROK SUPPORT: Use openai/ prefix with custom base_url
            api_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or os.getenv("GROK_KEY")
            if not api_key:
                raise RuntimeError("XAI_API_KEY, GROK_API_KEY, or GROK_KEY not set")
            
            return LLM(
                model=f"openai/{model}",  # Use openai/ prefix
                api_key=api_key,
                base_url="https://api.x.ai/v1",
                temperature=0.0
            )
        
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            
            return LLM(
                model=f"anthropic/{model}",
                temperature=0.0
            )
        
        else:
            raise ValueError(f"Unsupported provider: {provider}")
    
    async def generate_sql(self, question: str) -> Dict[str, Any]:
        """Generate SQL using CrewAI."""
        import time
        from crewai import Task, Crew, Process
        
        start = time.perf_counter()
        
        # Create task
        task = Task(
            description=f"Generate SQL query for: {question}",
            expected_output="Valid SQL query",
            agent=self.agent
        )
        
        # Create crew
        crew = Crew(
            agents=[self.agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False
        )
        
        # Run crew (synchronous, so use to_thread)
        result = await asyncio.to_thread(crew.kickoff)
        
        latency = time.perf_counter() - start
        
        # Extract SQL
        sql = extract_sql(str(result))
        
        return {
            "sql": sql,
            "latency": latency,
            "raw_response": str(result)
        }

# =============================================================================
# FACTORY FUNCTION (SWITCH BETWEEN FRAMEWORKS)
# =============================================================================

def create_agent(
    provider: str, 
    model: str, 
    system_prompt: str,
    framework: str = "langchain"  # ← Switch framework here
) -> BiomedicalSQLAgent:
    """
    Create a SQL generation agent.
    
    Args:
        provider: LLM provider (openai, groq, gemini, etc.)
        model: Model name
        system_prompt: System prompt for SQL generation
        framework: Which framework to use (langchain, phidata, pydanticai, crewai)
    
    Returns:
        BiomedicalSQLAgent instance
    """
    if framework == "langchain":
        return LangChainSQLAgent(provider, model, system_prompt)
    elif framework == "phidata":
        return PhidataSQLAgent(provider, model, system_prompt)
    elif framework == "pydanticai":
        return PydanticAISQLAgent(provider, model, system_prompt)
    elif framework == "crewai":
        return CrewAISQLAgent(provider, model, system_prompt)
    else:
        raise ValueError(f"Unknown framework: {framework}")

# =============================================================================
# BATCH PROCESSING (SAME FOR ALL FRAMEWORKS!)
# =============================================================================

# =============================================================================
# MAIN (COMPARE ALL FRAMEWORKS)
# =============================================================================

# async def compare_all_frameworks():
#     """Run comparison across all frameworks."""
    
#     frameworks = ["langchain", "phidata", "pydanticai", "crewai"]
#     all_results = {}
    
#     for framework in frameworks:
#         print("\n" + "="*80)
#         print(f"TESTING FRAMEWORK: {framework.upper()}")
#         print("="*80)
        
#         try:
#             results = await run_batch(framework=framework)
#             all_results[framework] = results
#         except Exception as e:
#             print(f"Framework {framework} failed: {e}")
    
#     return all_results

# if __name__ == "__main__":
#     import asyncio
    
#     # Option 1: Test single framework
#     print("Testing LangChain...")
#     results = asyncio.run(run_batch(framework="langchain"))
    
#     # Option 2: Compare all frameworks
#     # all_results = asyncio.run(compare_all_frameworks())







async def run_biochirp_query_hcdt(query: str, ws_url:str = WS_HCDT_URL, ws_url_csv:str = WS_HCDT_DOWNLOAD_URL):
    """
    Returns
    -------
    final_answer : str
    dfs          : dict[str, pd.DataFrame]   # keys: ttd / ctd / hcdt
    csv_paths    : dict[str, str]
    """

    import websockets
    import json
    from io import StringIO
    import requests

    csv_paths: dict[str, str] = {}
    final_answer: str = ""
    WS_HCDT_URL = "wss://biochirp.iiitd.edu.in/hcdt_chat/"

    TABLE_EVENTS = {
    "ttd_table": "ttd",
    "ctd_table": "ctd",
    "hcdt_table": "hcdt",
}

    async with websockets.connect(ws_url) as ws:
        # ---------------- Handshake ----------------
        init = json.loads(await ws.recv())
        connection_id = init.get("session_id")
        print(f"Connected to orchestrator | connection_id={connection_id}")

        # ---------------- Send query ----------------
        await ws.send(json.dumps({
            "user_input": query,
            "session_id": connection_id
        }))

        # ---------------- Listen ----------------
        completed = False

        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                msg_type = msg.get("type")

                # ---- CSV EVENTS (Redis → WS) ----
                if msg_type in TABLE_EVENTS:
                    tool = TABLE_EVENTS[msg_type]
                    csv_path = msg.get("csv_path")
                    row_count = msg.get("row_count")

                    if csv_path:
                        csv_paths[tool] = csv_path
                        print(f"{tool.upper()} CSV announced | rows={row_count}")

                # ---- FINAL ANSWER ----
                elif msg_type in {"final", "orchestrator_final"}:
                    final_answer = msg.get("content", "")
                    print("Orchestrator finished")
                    completed = True

                # ---- EXIT CONDITION ----
                if completed:
                    break

        except websockets.ConnectionClosed:
            # Defensive: server-side close
            print("WebSocket closed by server")

    # --------------------------------------------------------
    # DOWNLOAD CSVs
    # --------------------------------------------------------
    dfs: dict[str, pd.DataFrame] = {}

    for tool, path in csv_paths.items():
        r = requests.get(ws_url_csv, params={"path": path})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # print(f"Downloaded {tool.upper()} | shape={dfs[tool].shape}")

    return df
