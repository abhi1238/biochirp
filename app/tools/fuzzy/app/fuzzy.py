"""Thin re-export shim.

The canonical implementation now lives in ``app/utils/fuzzy_match.py`` (shared
between the fuzzy and expand_and_match_db services to avoid a byte-identical
duplicate). This module is retained because it is bind-mounted into other
services (e.g. opentarget_service mounts this file as ``/app/fuzzy.py`` and
does ``from fuzzy import fuzzy_filter_choices_multi_scorer``). All those mounts
also mount ``app/utils`` on the import path, so the re-export below resolves.
"""
from utils.fuzzy_match import (  # noqa: F401
    FUZZY_SEARCH_CUT_OFF,
    fuzzy_filter_choices_multi_scorer,
)

__all__ = ["fuzzy_filter_choices_multi_scorer", "FUZZY_SEARCH_CUT_OFF"]
