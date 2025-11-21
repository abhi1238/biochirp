from sentence_transformers import SentenceTransformer
# from .guardrail import EmbedInput
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware
# from .guardrail import EmbedInput
import asyncio

from pydantic import BaseModel
from typing import List

class EmbedInput(BaseModel):
    texts: List[str]

model = SentenceTransformer("malteos/scincl")

async def embed(input: EmbedInput):
    embeddings = model.encode(input.texts).tolist()
    return {"embeddings": embeddings}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"], 
)


# Root endpoint
@app.get("/")
def root():
    return {"message": "malteos/scincl Transformer service tool is running"}

# Health check endpoint (replaces /ws/health)
@app.get("/health")
async def health():
    return {"status": "OK"}



@app.post("/embed_malteos")
async def embed(input: EmbedInput):

    payload = await asyncio.wait_for(embed(input), timeout=60)

    return payload