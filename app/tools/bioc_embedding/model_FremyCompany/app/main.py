from sentence_transformers import SentenceTransformer
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware
import asyncio

from pydantic import BaseModel
from typing import List

class EmbedInput(BaseModel):
    texts: List[str]

model_cambridgeltl = SentenceTransformer("FremyCompany/BioLORD-2023-S")

async def embed(input: EmbedInput):
    embeddings = model_cambridgeltl.encode(input.texts).tolist()
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
    return {"message": "FremyCompany/BioLORD-2023-S tool is running"}

# Health check endpoint (replaces /ws/health)
@app.get("/health")
async def health():
    return {"status": "OK"}



@app.post("/embed_FremyCompany")
async def embed(input: EmbedInput):

    payload = await asyncio.wait_for(embed(input), timeout=60)

    return payload