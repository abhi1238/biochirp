from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sentence_transformers import SentenceTransformer
from pydantic import BaseModel
from typing import List, Dict
import asyncio
import logging

# ------------- Logging setup -------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn.error")

# ------------- Model Registry -------------
MODEL_PATHS = {
    # "FremyCompany/BioLORD-2023-S": "FremyCompany/BioLORD-2023-S",
    "malteos/scincl": "malteos/scincl",
    "pritamdeka/S-PubMedBERT-MS-MARCO": "pritamdeka/S-PubMedBERT-MS-MARCO",
    # "cambridgeltl/SapBERT-from-PubMedBERT-fulltext": "cambridgeltl/SapBERT-from-PubMedBERT-fulltext",
    "nuvocare/WikiMedical_sent_biobert": "nuvocare/WikiMedical_sent_biobert",
}
MODEL_CACHE = {}

# ------------- FastAPI Setup -------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------- Pydantic Schemas -------------
class EmbedInput(BaseModel):
    texts: List[str]
    model: str

class EmbedAllInput(BaseModel):
    term: str

# ------------- Load models on startup -------------
@app.on_event("startup")
def load_models():
    for key, path in MODEL_PATHS.items():
        logger.info(f"Loading model '{key}' from '{path}' ...")
        MODEL_CACHE[key] = SentenceTransformer(path)
    logger.info("All models loaded.")

# ------------- /embed Endpoint (single model) -------------
@app.post("/embed")
async def embed(input: EmbedInput):
    logger.info(f"Received /embed request: {input}")
    model_key = input.model
    if model_key not in MODEL_CACHE:
        logger.error(f"Model '{model_key}' not found!")
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model_key}' not found. Available: {list(MODEL_CACHE.keys())}"
        )
    try:
        # Run in thread pool (async-safe)
        embeddings = await asyncio.to_thread(MODEL_CACHE[model_key].encode, input.texts)
        embeddings = embeddings.tolist()
        logger.info(f"Embedding for model '{model_key}' successful.")
        return {
            "model": model_key,
            "embeddings": embeddings
        }
    except Exception as e:
        logger.error(f"Embedding failed for model '{model_key}': {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Embedding failed for model '{model_key}': {str(e)}"
        )

# ------------- /embed_all Endpoint (all models, async) -------------
@app.post("/embed_all")
async def get_embeddings_all_models(input: EmbedAllInput):
    """
    Returns the embedding for the given term from all models in MODEL_CACHE.
    Output: {model_key: embedding_list}, plus any errors.
    """
    term = input.term
    results = {}
    errors = {}

    async def encode_async(model_key, model):
        try:
            logger.info(f"Encoding term '{term}' for model '{model_key}'")
            emb = await asyncio.to_thread(model.encode, [term])
            emb_list = emb[0].tolist()
            results[model_key] = emb_list
        except Exception as e:
            logger.error(f"Error encoding with model '{model_key}': {e}")
            results[model_key] = str(e)

    # Launch all encodings concurrently
    tasks = [encode_async(model_key, model) for model_key, model in MODEL_CACHE.items()]
    await asyncio.gather(*tasks)

    if not results:
        raise HTTPException(status_code=500, detail=f"All models failed to encode. Errors: {errors}")

    return results
# ------------- Health/Info root endpoint -------------
@app.get("/")
def root():
    return {"message": "Embedding service is running.", "models": list(MODEL_CACHE.keys())}
