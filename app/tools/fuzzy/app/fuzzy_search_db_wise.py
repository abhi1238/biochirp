import os
import sys
import logging
from typing import Union, Any, Dict

from config.guardrail import (
    ParsedValue,
    OutputFields,
    FuzzyFilteredOutputs,
)
from utils.fuzzy_match import fuzzy_filter_choices_multi_scorer
from utils.concept_values import get_db_concept_values

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Configuration
FUZZY_SCORE_CUT_SCORE = float(os.getenv("FUZZY_SCORE_CUT_SCORE", "82"))

# Fields that should bypass the LLM-based member filter and rely solely on the
# fuzzy match. These are STRUCTURED values (identifiers, codes, categoricals)
# where exact / case-insensitive equality is ground truth — the LLM judge has
# no useful prior and tends to reject valid matches (e.g. "EGFR_HUMAN" →
# "EGFR_HUMAN" was being dropped because the judge couldn't reason about
# UniProt entry-name semantics).
STRUCTURED_FIELDS_BYPASS_LLM: set = {
    # IDs / cross-references
    "uniprot_xref", "uniprot_id",
    "pubchem_cid", "pubchem_sid",
    "cas_number", "chebi_xref", "chebi_id",
    "superdrug_atc", "superdrug_cas",
    "drug_compound_id",
    "entrez_id", "ensembl_id", "hgnc_id",
    "gene_partner_id", "protein_partner_id", "substrate_id",
    # Categorical / enum-style fields
    "activity_type", "activity_operator", "activity_unit",
    "target_type", "approval_status",
    "evidence_type", "evidence_direction", "evidence_level",
    "clinical_significance", "review_status", "variant_type",
    "regulation_type", "organism", "species",
    "locus_type", "locus_group", "tier", "role_in_cancer", "gene_type",
    "xref_db",
    # Structured chemistry strings
    "formula",
    # Drug synonyms — the fuzzy step (cut-off 90 across 4 scorers) is already
    # tight; the LLM judge has been seen rejecting valid hits like
    # "Glivec" → "Glivec (TN)" because it doesn't recognise the `synonym`
    # field semantics. Accepting fuzzy matches directly is the right call.
    "synonym",
}



async def compute_fuzzy_filtered_outputs(
    parsed: Union[ParsedValue, Dict[str, Any]],
    database: str,
    raw: bool = False,
) -> FuzzyFilteredOutputs:
    """
    Perform fuzzy matching for database fields. Always returns raw candidates;
    LLM filtering is handled by the caller (expand_and_match_db unified filter).
    The `raw` parameter is kept for API compatibility but has no effect.
    """
    tool = "fuzzy"
    
    logger.info(f"[{tool}] Starting for database: {database}")
    
    # Input validation
    if not parsed:
        logger.warning(f"[{tool}] Empty parsed input")
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    # Convert to dict if needed
    try:
        fields = parsed.model_dump(exclude_none=True) if hasattr(parsed, 'model_dump') else parsed
    except Exception as e:
        logger.warning(f"[{tool}] Failed to convert parsed to dict: {e}")
        fields = parsed if isinstance(parsed, dict) else {}

    if not fields:
        logger.warning(f"[{tool}] Empty fields after parsing")
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )

    db_lookup_key = database.lower()
    db_fields = get_db_concept_values(db_lookup_key)
    if not db_fields:
        logger.warning(
            f"[{tool}] No concept values found for database '{database}' "
            f"(key: '{db_lookup_key}'). Run: python scripts/build_concept_values.py {db_lookup_key}"
        )
        return FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )

    logger.info(
        f"[{tool}] Found database '{database}' with {len(db_fields)} fields. "
        f"Processing {len(fields)} parsed fields."
    )
    
    field_matches = {}
    
    for field_name, user_terms in fields.items():
        # Sentinel: "requested" means "include this column in output, do not filter".
        # Preserve as-is — never wrap or fuzzy-match.
        if isinstance(user_terms, str) and user_terms.strip().lower() == "requested":
            field_matches[field_name] = user_terms
            logger.debug(f"[{tool}] Field '{field_name}' is the 'requested' sentinel, preserving")
            continue

        # Coerce non-sentinel strings into single-element lists so the fuzzy
        # pipeline can canonicalise them. Previously a bare string ("Glivec")
        # was assumed to be "already matched" and passed through verbatim — but
        # the actual stored value is "Glivec (TN)", and a literal-equality
        # filter against the raw user term then returned 0 rows.
        if isinstance(user_terms, str):
            user_terms = [user_terms]
        
        # Skip if not a list
        if not isinstance(user_terms, list):
            logger.warning(
                f"[{tool}] Skipping non-list field '{field_name}': {type(user_terms)}"
            )
            continue
        
        # Skip empty lists
        if not user_terms:
            logger.info(f"[{tool}] Field '{field_name}' has empty user_terms")
            field_matches[field_name] = []
            continue
        
        # Skip if no choices available in database
        db_choices = db_fields.get(field_name)
        if not db_choices:
            logger.info(
                f"[{tool}] No database choices available for field '{field_name}'"
            )
            field_matches[field_name] = []
            continue

        # Ensure choices is a list (pkl may store as set)
        if not isinstance(db_choices, (list, tuple)):
            db_choices = list(db_choices)

        # Fuzzy search
        logger.info(
            f"[{tool}] Fuzzy matching {len(user_terms)} terms against "
            f"{len(db_choices)} choices for field '{field_name}'"
        )

        try:
            fuzzy_matches = fuzzy_filter_choices_multi_scorer(
                queries=user_terms,
                choices=db_choices,
                min_score=FUZZY_SCORE_CUT_SCORE
            )
        except Exception as e:
            logger.exception(
                f"[{tool}] Fuzzy matching failed for field '{field_name}': {e}"
            )
            field_matches[field_name] = []
            continue
        
        logger.info(
            f"[{tool}] Fuzzy search found {len(fuzzy_matches)} matches for '{field_name}'"
        )
        
        if not fuzzy_matches:
            logger.info(f"[{tool}] No fuzzy matches found for field '{field_name}'")
            field_matches[field_name] = []
            continue

        # Return fuzzy candidates — structured fields use as-is, all others
        # are returned raw for the caller (expand_and_match_db) to union and
        # pass through its unified LLM filter in one shot.
        if field_name not in STRUCTURED_FIELDS_BYPASS_LLM:
            logger.info(
                f"[{tool}][RAW] Returning {len(list(dict.fromkeys(fuzzy_matches)))} "
                f"raw fuzzy candidates for '{field_name}'"
            )
        field_matches[field_name] = list(dict.fromkeys(fuzzy_matches))
    
    # Build result
    try:
        result = FuzzyFilteredOutputs(
            database=database,  # Return original uppercase for consistency
            value=OutputFields(**field_matches) if field_matches else OutputFields(),
            tool=tool
        )
    except Exception as e:
        logger.exception(f"[{tool}] Failed to create FuzzyFilteredOutputs: {e}")
        result = FuzzyFilteredOutputs(
            database=database,
            value=OutputFields(),
            tool=tool
        )
    
    total_matches = sum(
        len(matches) if isinstance(matches, list) else 0
        for matches in field_matches.values()
    )
    logger.info(
        f"[{tool}] Finished for database '{database}'. "
        f"Processed {len(field_matches)} fields with {total_matches} total matches."
    )
    
    return result
