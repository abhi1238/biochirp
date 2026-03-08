


import sys
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/expand_synonyms/app")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/services/synonyms")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/config")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/fuzzy/app")
sys.path.insert(0, "/home/abhishekh/abhi/biochirp/app/tools/llm_member_filter/app")

from fuzzy import fuzzy_filter_choices_multi_scorer
import numpy as np
import pandas as pd
import os
import math
import random
import pickle
import asyncio
import importlib



with open("../../resources/prompts/semantic_match_agent.md", "r", encoding="utf-8") as f:
    prompt_md_semantic_match_filter = f.read()

# fuzzy_cutoff = 90

def return_fuzzy_member(term, universe, fuzzy_cutoff=90):


    fuzzy_pred_raw = set(
        fuzzy_filter_choices_multi_scorer(
            queries=term,
            choices=universe,
            min_score=universe
        )
    )

    fuzzy_pred = fuzzy_pred_raw & set(universe)


    return list(fuzzy_pred_raw)