import torch
import asyncio
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor
from .models import MODEL_CACHE
import logging

# Reuse a single executor (important)
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=3)

base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.target")
# logger.info("🔹 Loading embedding models...")

def _embed_single_model(
    texts: List[str],
    model_name: str,
    normalize: bool,
):
    model = MODEL_CACHE[model_name]
    return model.encode(
        texts,
        batch_size=64,
        convert_to_numpy=True,
        normalize_embeddings=normalize,
        show_progress_bar=False,
    ).tolist()


@torch.inference_mode()
async def embed_list_multi_model(
    texts: List[str],
    models: List[str]= ["malteos/scincl", "pritamdeka/S-PubMedBERT-MS-MARCO", "nuvocare/WikiMedical_sent_biobert"],
    normalize: bool = True,
) -> Dict[str, List[List[float]]]:

    loop = asyncio.get_running_loop()

    # Validate once
    missing = [m for m in models if m not in MODEL_CACHE]
    if missing:
        raise ValueError(f"Unknown models: {missing}")

    tasks = {
        model: loop.run_in_executor(
            _EMBED_EXECUTOR,
            _embed_single_model,
            texts,
            model,
            normalize,
        )
        for model in models
    }

    logger.info("[Embedding all model] Embedding task started ...")

    results = {}
    for model, task in tasks.items():
        results[model] = await task

    return results
