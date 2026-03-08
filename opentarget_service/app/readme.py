from agents import function_tool

@function_tool(
    name_override="readme_tool",
    description_override=(
        "Information about BioChirp capabilities and supported queries. "
        "Use when users ask what BioChirp can do or need examples."
    ),
)
def readme_tool() -> str:
    """Return BioChirp capability information."""
    
    return """
# BioChirp OpenTargets Guide

## Tools

**Disease Tool** - Drugs & targets for diseases
Returns: Drugs, targets, association scores, phases, synonyms, description, CSV download
Example: "Give list of Breast cancer drugs"

**Drug Tool** - Indications & targets for drugs
Returns: Diseases, targets, mechanisms, phases, synonyms, description, CSV download
Example: "What does aspirin treat?" 

**Target Tool** - Diseases & drugs for genes/proteins
Returns: Diseases, drugs, association scores, phases, synonyms, description, CSV download
Example: "Disease associated with TP53"

**Web Search** - Current info beyond database

## Entities Supported

**Diseases:** Medical names → EFO IDs
**Drugs:** Generic/brand names → ChEMBL IDs
**Targets:** Gene symbols → Ensembl IDs
**Mechanisms:** inhibitor, antagonist, modulator, agonist, blocker, activator
**Pathways:** PI3K/AKT pathway, MAPK signaling, etc.

## Resolution Methods

**OpenTargets mapping:** Direct match (fast, accurate)
**Web search:** Fallback when not in mapping
You'll see: "matched in OpenTargets" or "found via web search"

## Query Patterns

**Single:** "Melanoma drugs" "What does metformin treat?" 
**Filtered:** "Give drug that target TP53 in breast cancer treatment." 


## Output

- Entity IDs with resolution method
- Entity synonyms and descriptions
- Preview (50 rows) + full CSV download
- Smart column filtering based on query

## Tips

✓ Specific names, standard terms, combine entities ("X for Y")
✗ Single vague words, colloquial-only terms

## Examples

"What does aspirin treat?"
"What is the target of Aspirin?"
What drugs are used to treat TB?
"""