from sentence_transformers import SentenceTransformer
from .guardrail import EmbedInput

model_cambridgeltl = SentenceTransformer("cambridgeltl/SapBERT-from-PubMedBERT-fulltext")

# @app.post("/embed_pritamdeka")
async def embed_cambridgeltl(input: EmbedInput):
    embeddings = model_cambridgeltl.encode(input.texts).tolist()
    return {"embeddings": embeddings}
