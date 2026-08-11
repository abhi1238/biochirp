"""Load disclaimer texts from resources/prompts/_disclaimers.yaml and expose
helpers for splicing them into prompts at load time.

Two functions cover the typical splice patterns:

1. ``load_disclaimers()`` returns the raw dict so callers that compose their
   own templates can pull just the text they need.

2. ``splice_disclaimers(prompt_text)`` replaces the two well-known
   placeholders inside a prompt body:
     ``{{MEDICAL_ADVICE_DISCLAIMER}}`` → medical-advice sentence (no markup)
     ``{{PROVENANCE_DISCLAIMER}}``     → provenance sentence (no markup)
   The prompt files themselves provide the surrounding markdown / italics,
   so the YAML keeps the plain sentence and never the formatting.

CI gate ``scripts/check_prompt_invariants.sh`` asserts that no prompt file
in ``resources/prompts/`` carries a literal copy of these sentences; every
occurrence must be a placeholder spliced via this module.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import TypedDict

import yaml


class _DisclaimerSet(TypedDict):
    medical_advice: str
    provenance: str


_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATE_ROOTS = [
    os.path.normpath(os.path.join(_HERE, "..", "..")),  # repo root in dev
    "/app",                                              # container root
]


def _find_yaml_path() -> str:
    for root in _CANDIDATE_ROOTS:
        p = os.path.join(root, "resources", "prompts", "_disclaimers.yaml")
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "_disclaimers.yaml not found under any of: "
        + ", ".join(_CANDIDATE_ROOTS)
    )


@lru_cache(maxsize=1)
def load_disclaimers() -> _DisclaimerSet:
    """Return the two disclaimer sentences as a TypedDict.

    Cached at module level — the YAML is read once per process.
    """
    with open(_find_yaml_path(), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    medical = (data.get("medical_advice") or "").strip()
    provenance = (data.get("provenance") or "").strip()
    if not medical or not provenance:
        raise ValueError(
            "_disclaimers.yaml must define non-empty `medical_advice` "
            "and `provenance` keys; got "
            f"medical={medical!r}, provenance={provenance!r}"
        )
    return {"medical_advice": medical, "provenance": provenance}


def splice_disclaimers(prompt_text: str) -> str:
    """Replace `{{MEDICAL_ADVICE_DISCLAIMER}}` and `{{PROVENANCE_DISCLAIMER}}`
    in *prompt_text* with the loaded sentences.

    Idempotent — calling repeatedly leaves the output unchanged.
    """
    d = load_disclaimers()
    return (
        prompt_text
        .replace("{{MEDICAL_ADVICE_DISCLAIMER}}", d["medical_advice"])
        .replace("{{PROVENANCE_DISCLAIMER}}", d["provenance"])
    )
