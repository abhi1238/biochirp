
# models.py
import torch
from sentence_transformers import SentenceTransformer
import logging

base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")
# logger.info("🔹 Loading embedding models...")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# models.py
MODEL_NAMES = {
    "scincl": "malteos/scincl",
    "pubmed_marco": "pritamdeka/S-PubMedBERT-MS-MARCO",
    "wikimed": "nuvocare/WikiMedical_sent_biobert",
}

MODEL_CACHE = {
    key: SentenceTransformer(name, device=DEVICE)
    for key, name in MODEL_NAMES.items()
}

# logger.info("🔹 Loading embedding models...")

MODEL_CACHE = {
    k: SentenceTransformer(v, device=DEVICE)
    for k, v in MODEL_NAMES.items()
}

for m in MODEL_CACHE.values():
    m.eval()
    for p in m.parameters():
        p.requires_grad = False

# logger.info("🔹 Loading embedding models...")
