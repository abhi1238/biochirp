from sentence_transformers import SentenceTransformer
from .guardrail import EmbedInput

model_malteos = SentenceTransformer("malteos/scincl")

# @app.post("/embed_pritamdeka")
async def embed_malteos(input: EmbedInput):
    embeddings = model_malteos.encode(input.texts).tolist()
    return {"embeddings": embeddings}
