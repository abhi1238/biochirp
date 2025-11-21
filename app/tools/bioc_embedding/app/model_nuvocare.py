from sentence_transformers import SentenceTransformer
from .guardrail import EmbedInput

model_nuvocare = SentenceTransformer("nuvocare/WikiMedical_sent_biobert")

# @app.post("/embed_pritamdeka")
async def embed_nuvocare(input: EmbedInput):
    embeddings = model_nuvocare.encode(input.texts).tolist()
    return {"embeddings": embeddings}
