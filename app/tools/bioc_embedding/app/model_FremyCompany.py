from sentence_transformers import SentenceTransformer
from .guardrail import EmbedInput

model_FremyCompany = SentenceTransformer("FremyCompany/BioLORD-2023-S")

# @app.post("/embed_pritamdeka")
async def embed_FremyCompany(input: EmbedInput):
    embeddings = model_FremyCompany.encode(input.texts).tolist()
    return {"embeddings": embeddings}
