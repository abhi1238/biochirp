from fastapi import FastAPI
from sentence_transformers import SentenceTransformer
from fastapi.middleware.cors import CORSMiddleware
from .model_pritamdeka import embed_pritamdeka
from .guardrail import EmbedInput
from pydantic import BaseModel
from typing import List
import asyncio

model_pritamdeka = SentenceTransformer("pritamdeka/S-PubMedBERT-MS-MARCO")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"], 
)
@app.post("/embed_pritamdeka")
async def embed(input: EmbedInput):
    payload = await asyncio.wait_for(embed_pritamdeka(input), timeout=60)
    return payload
