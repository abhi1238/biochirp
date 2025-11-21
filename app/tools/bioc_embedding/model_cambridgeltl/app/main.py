from sentence_transformers import SentenceTransformer
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import torch

from pydantic import BaseModel
from typing import List

class EmbedInput(BaseModel):
    texts: List[str]

model = SentenceTransformer("cambridgeltl/SapBERT-from-PubMedBERT-fulltext")
device = "cuda" if torch.cuda.is_available() else "cpu"
model = model.to(device)
# 
async def embed(input: EmbedInput):
    # embeddings = model.encode(input.texts).tolist()
    embeddings = model.encode(
    input.texts,
    device=device,
    show_progress_bar=False
).tolist()
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
    return {"message": "cambridgeltl/SapBERT-from-PubMedBERT-fulltext service tool is running"}

# Health check endpoint (replaces /ws/health)
@app.get("/health")
async def health():
    return {"status": "OK"}



@app.post("/embed_cambridgeltl")
async def embed_cambridgeltl(input: EmbedInput):

    payload = await asyncio.wait_for(embed(input), timeout=60)

    return payload