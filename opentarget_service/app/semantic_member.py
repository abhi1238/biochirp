

import sys
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/expand_synonyms/app")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/services/synonyms")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/config")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/fuzzy/app")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/llm_member_filter/app")

# =====================
# Standard library
# =====================
import os
import math
import random
import pickle
import asyncio
import importlib
import utility_evaluation

# =====================
# Third-party libraries
# =====================
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from kneed import KneeLocator
from sentence_transformers import SentenceTransformer, util

# =====================
# Local / project modules
# =====================
# import utility
from fuzzy import fuzzy_filter_choices_multi_scorer
from config.settings import (
    BIOMEDICAL_MODELS,
    SUPPORTED_DBS,
    DB_VALUE_PATH,
)

# from google import genai


# =====================
# Reloads (only if actively developing)
# =====================
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message=".*google.generativeai.*"
)

random.seed(42)
np.random.seed(42)

model_names = BIOMEDICAL_MODELS
print(f"Retrived biomedical transformers are {model_names}")


with open("../../resources/prompts/semantic_match_agent.md", "r", encoding="utf-8") as f:
    prompt_md_semantic_match_filter = f.read()

malteos_transformer = SentenceTransformer('malteos/scincl')
pritamdeka_transformer = SentenceTransformer('pritamdeka/S-PubMedBERT-MS-MARCO')
nuvocare_transformer = SentenceTransformer('nuvocare/WikiMedical_sent_biobert')


# vecs_malteos = malteos_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
# vecs_pritamdeka = pritamdeka_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
# vecs_nuvocare = nuvocare_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)

async def async_llm_member_filter(**kwargs):
    return await utility_evaluation.llm_member_filter(**kwargs)



async def get_union_hits_for_term(
    vecs_malteos, vecs_pritamdeka, vecs_nuvocare,
    term: str,
    combined_list: list,
    category: str,

):
    """
    For a single reference term, compute semantic hits across models,
    apply knee thresholding, LLM filtering, and return the union.
    """

    # ---------- Encode query once per model ----------
    q_malteos = malteos_transformer.encode(
        term, normalize_embeddings=True, show_progress_bar=False
    ).astype(np.float32)

    q_pritamdeka = pritamdeka_transformer.encode(
        term, normalize_embeddings=True, show_progress_bar=False
    ).astype(np.float32)

    q_nuvocare = nuvocare_transformer.encode(
        term, normalize_embeddings=True, show_progress_bar=False
    ).astype(np.float32)

    # ---------- Similarities ----------
    sims_malteos = vecs_malteos @ q_malteos
    sims_pritamdeka = vecs_pritamdeka @ q_pritamdeka
    sims_nuvocare = vecs_nuvocare @ q_nuvocare

    # ---------- Data-driven thresholds ----------
    th_malteos = utility_evaluation.knee_threshold(sims_malteos)
    th_pritamdeka = utility_evaluation.knee_threshold(sims_pritamdeka)
    th_nuvocare = utility_evaluation.knee_threshold(sims_nuvocare)

    # ---------- Filter + sort ----------
    hits_malteos = utility_evaluation.filter_and_sort_hits(combined_list, sims_malteos, th_malteos)
    hits_pritamdeka = utility_evaluation.filter_and_sort_hits(combined_list, sims_pritamdeka, th_pritamdeka)
    hits_nuvocare = utility_evaluation.filter_and_sort_hits(combined_list, sims_nuvocare, th_nuvocare)

    # ---------- Strip scores ----------
    term_hits_malteos = [t for t, _ in hits_malteos]
    term_hits_pritamdeka = [t for t, _ in hits_pritamdeka]
    term_hits_nuvocare = [t for t, _ in hits_nuvocare]


    llm_hits_malteos, llm_hits_pritamdeka, llm_hits_nuvocare = await asyncio.gather(
    async_llm_member_filter(
        category=category,
        single_term=term,
        string_list=term_hits_malteos,
    ),
    async_llm_member_filter(
        category=category,
        single_term=term,
        string_list=term_hits_pritamdeka,
    ),
    async_llm_member_filter(
        category=category,
        single_term=term,
        string_list=term_hits_nuvocare,
    ),
)


    # ---------- UNION ----------
    union_hits = list(
        set(llm_hits_malteos)
        | set(llm_hits_pritamdeka)
        | set(llm_hits_nuvocare)
    )

    return union_hits



async def return_semantic_member(terms, universe, category):

    malteos_transformer = SentenceTransformer('malteos/scincl')
    pritamdeka_transformer = SentenceTransformer('pritamdeka/S-PubMedBERT-MS-MARCO')
    nuvocare_transformer = SentenceTransformer('nuvocare/WikiMedical_sent_biobert')


    vecs_malteos = malteos_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
    vecs_pritamdeka = pritamdeka_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
    vecs_nuvocare = nuvocare_transformer.encode(terms, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)



    print("Running Embedding smilarity search ..................")
    embed_llm_hits = await get_union_hits_for_term(vecs_malteos, vecs_pritamdeka, vecs_nuvocare,
        term=terms,
        combined_list=universe,
        category=category,
    )
    embed_llm_hits = {x.lower() for x in embed_llm_hits} & set(universe)

    return embed_llm_hits
