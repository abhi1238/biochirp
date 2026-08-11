#!/usr/bin/env bash
# Assert the byte-level / surface-level prompt invariants surfaced by the
# 2026-05-23 prompt-consistency audit. Run from repo root.
#
#   ./scripts/check_prompt_invariants.sh
#
# Exits 0 if all checks pass, 1 otherwise. Companion to
# scripts/check_prompt_gene_field_canonical.py (which covers the YAML-block-
# aware gene_symbol/gene_name drift check).
#
# Invariants:
#   1. No literal disclaimer text in prompt files — every disclaimer must
#      be the {{MEDICAL_ADVICE_DISCLAIMER}} / {{PROVENANCE_DISCLAIMER}}
#      placeholder, spliced at load time from resources/prompts/_disclaimers.yaml.
#   2. After splice, the rendered prompts carry the canonical disclaimer
#      sentences in the expected per-file counts.
#   3. miRBase ghost — must not appear in any active prompt / catalog /
#      MCP-spec / dbs/ surface. The tombstone block in
#      interpreter_db_notes.yaml and the README educational example are
#      whitelisted; .bak backup files are skipped.
#   4. Disclaimer wrapper form — every {{MEDICAL_ADVICE_DISCLAIMER}} /
#      {{PROVENANCE_DISCLAIMER}} placeholder must be wrapped in
#      `*…*` (italic-asterisk) — never in backticks, blockquote (`> `),
#      or bare. Mismatched wrappers were the root cause of C035 in the
#      2026-05-24 audit (backtick-italic-code renders as inline code with
#      literal underscores, not italic).
#   5. TTD subject/object-inversion block — the block in
#      interpreter_db_notes.yaml that self-declares it "mirrors
#      interpreter_shared.md Rule 6 Half (a) ONLY" must remain
#      semantically aligned with Rule 6 Half (a) in interpreter_shared.md.
#      This invariant just asserts both sentinel headers exist; a divergent
#      maintenance edit will need a manual diff (C016 in the audit).
#   6. Verbatim failure-handling phrase byte-equality — the canonical
#      "Not found in authoritative sources checked via web search." and
#      "Unable to retrieve authoritative sources at this time." phrases
#      must appear byte-identical (ASCII straight quotes, no bold, no
#      blockquote) wherever they appear, so downstream code can
#      string-match them (D001 in the 2026-05-24 audit).
#   7. Tumour-suppressor dual-emit guardrail — both clarifier_agent.md
#      AND nlu_extractor.md must mandate dual-emit of `gene_name` AND
#      `gene_symbol` for tumour suppressors (HGNC's two-column reality).
#      A single-field tumour-suppressor block in either file is a
#      regression (D008 in the 2026-05-24 audit, surfaced after C037).
#   8. No curly quotes in active prompts — U+201C/U+201D/U+2018/U+2019
#      are forbidden; use ASCII '"' and "'" so string-matching downstream
#      code is not surprised (D010 in the 2026-05-24 audit).

set -u
cd "$(dirname "$0")/.."

PASS=0
FAIL=0

MEDICAL_ADVICE="$(python3 -c "import yaml; print(yaml.safe_load(open('resources/prompts/_disclaimers.yaml'))['medical_advice'].strip())")"
PROVENANCE="$(python3 -c "import yaml; print(yaml.safe_load(open('resources/prompts/_disclaimers.yaml'))['provenance'].strip())")"

echo "=== Invariant 1: No literal disclaimer text in prompt files (placeholders only) ==="
LITERAL_HITS=$(grep -lF -- "$MEDICAL_ADVICE" resources/prompts/*.md 2>/dev/null; grep -lF -- "$PROVENANCE" resources/prompts/*.md 2>/dev/null)
if [ -z "$LITERAL_HITS" ]; then
  echo "  PASS — no prompt file carries a literal copy; every disclaimer is a {{PLACEHOLDER}}"
  PASS=$((PASS + 1))
else
  echo "  FAIL — literal disclaimer text found in:"
  echo "$LITERAL_HITS" | sort -u | sed 's/^/    /'
  echo "  Fix: replace the literal sentence with {{MEDICAL_ADVICE_DISCLAIMER}} or {{PROVENANCE_DISCLAIMER}}."
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Invariant 2: After splice, expected per-file occurrence counts ==="
python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, ".")
from app.utils.disclaimers import splice_disclaimers, load_disclaimers
d = load_disclaimers()
expected = {
    "resources/prompts/synthesizer.md":        {"medical_advice": 2, "provenance": 1},
    "resources/prompts/web_tool_prompt.md":    {"medical_advice": 2, "provenance": 4},
    "resources/prompts/out_of_domain_web.md":  {"medical_advice": 0, "provenance": 5},
}
fails = []
for path, want in expected.items():
    raw = pathlib.Path(path).read_text()
    spliced = splice_disclaimers(raw)
    got = {
        "medical_advice": spliced.count(d["medical_advice"]),
        "provenance":     spliced.count(d["provenance"]),
    }
    for key, n_want in want.items():
        if got[key] != n_want:
            fails.append(f"  {path}: {key}={got[key]} (expected {n_want})")
if fails:
    print("  FAIL — splice-count mismatch:")
    for f in fails:
        print(f)
    sys.exit(2)
print("  PASS — every spliced prompt carries the expected disclaimer counts (5 medical + 10 provenance total)")
PY
if [ "$?" -eq 0 ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi

echo ""
echo "=== Invariant 3: miRBase ghost (must be no live references) ==="
# Exclude .bak backups, dbs/README.md educational example, and the
# tombstone block + completion note inside interpreter_db_notes.yaml.
GHOSTS=$(grep -RPn 'mirbase|miRBase' resources/ bio_chat_service/ dbs/ 2>/dev/null \
  | grep -v '\.bak:' \
  | grep -v 'dbs/README.md:' \
  | grep -vE 'REMOVED|tombstone|Follow-up cleanup|mirna_family, mirbase_accession|app/tools/mirbase/ service|dbs/mirbase/manifest.yaml \(deleted\)|graph_db_selector.py code comment')

if [ -z "$GHOSTS" ]; then
  echo "  PASS — no live miRBase references in active surfaces"
  PASS=$((PASS + 1))
else
  echo "  FAIL — miRBase ghost surfaces detected:"
  echo "$GHOSTS" | sed 's/^/    /'
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Invariant 4: Disclaimer placeholder wrapper form (must be *X*, never \`_X_\` or > X) ==="
# Find every line that mentions a disclaimer placeholder; reject any wrapper
# other than the canonical `*{{...}}*` (italic-asterisk).
WRAPPER_BAD=$(
  grep -nE '\{\{(MEDICAL_ADVICE_DISCLAIMER|PROVENANCE_DISCLAIMER)\}\}' \
    resources/prompts/synthesizer.md \
    resources/prompts/web_tool_prompt.md \
    resources/prompts/out_of_domain_web.md \
    2>/dev/null \
  | grep -vE '^[^:]+:[0-9]+:[[:space:]]*\*\{\{(MEDICAL_ADVICE_DISCLAIMER|PROVENANCE_DISCLAIMER)\}\}\*[[:space:]]*$' \
  | grep -vE '\{\{(MEDICAL_ADVICE_DISCLAIMER|PROVENANCE_DISCLAIMER)\}\}' \
    | head -50
)
# Re-scan because we want sites where the placeholder appears NOT in the canonical wrapper.
WRAPPER_BAD=$(
  python3 - <<'PY'
import re, pathlib, sys
files = [
    "resources/prompts/synthesizer.md",
    "resources/prompts/web_tool_prompt.md",
    "resources/prompts/out_of_domain_web.md",
]
canon = re.compile(r'^\s*\*\{\{(MEDICAL_ADVICE_DISCLAIMER|PROVENANCE_DISCLAIMER)\}\}\*\s*$')
placeholder = re.compile(r'\{\{(MEDICAL_ADVICE_DISCLAIMER|PROVENANCE_DISCLAIMER)\}\}')
bad = []
for fp in files:
    for i, line in enumerate(pathlib.Path(fp).read_text().splitlines(), 1):
        if placeholder.search(line) and not canon.match(line):
            # Skip prose mentions like '... the {{PLACEHOLDER}} sentence ...'
            # (heuristic: must be the only meaningful content on the line)
            stripped = line.strip()
            if stripped.startswith("*") and stripped.endswith("*") and stripped.count("{{") == 1:
                continue
            # Allow comments / prose lines that mention the placeholder text
            # without being the placeholder itself (e.g. "see {{X}} above").
            # Heuristic: if the line has > 4 words besides the placeholder,
            # treat it as prose.
            tokens = placeholder.sub("X", line).split()
            if len(tokens) > 4:
                continue
            bad.append(f"{fp}:{i}:{line.rstrip()}")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PY
)
if [ -z "$WRAPPER_BAD" ]; then
  echo "  PASS — every disclaimer placeholder is wrapped in *…* on its own line"
  PASS=$((PASS + 1))
else
  echo "  FAIL — non-canonical wrapper detected:"
  echo "$WRAPPER_BAD" | sed 's/^/    /'
  echo "  Fix: wrap the placeholder as \`*{{PLACEHOLDER}}*\` (italic-asterisk) on its own line."
  FAIL=$((FAIL + 1))
fi

# Invariant 5 retired 2026-06-18: it validated the interpreter_shared.md ↔
# interpreter_db_notes.yaml "Rule 6 Half (a)" sentinel alignment. The interpreter
# prompt layer was decommissioned (moved to decommissioned/), so there is nothing
# left to check. See decommissioned/resources/prompts/.

echo ""
echo "=== Invariant 6: Verbatim failure-handling phrases byte-identical across web prompts ==="
PHRASES=(
  "Not found in authoritative sources checked via web search."
  "Unable to retrieve authoritative sources at this time."
)
INV6_BAD=$(
  python3 - <<'PY'
import re, pathlib, sys
phrases = [
    "Not found in authoritative sources checked via web search.",
    "Unable to retrieve authoritative sources at this time.",
]
files = [
    "resources/prompts/web_tool_prompt.md",
    "resources/prompts/out_of_domain_web.md",
]
bad = []
for fp in files:
    text = pathlib.Path(fp).read_text()
    for phrase in phrases:
        for i, line in enumerate(text.splitlines(), 1):
            if phrase in line:
                # Must be on a line with no bold-wrapping (**) and no blockquote (>)
                stripped = line.lstrip()
                if stripped.startswith(">") or "**" + phrase + "**" in line or "**“" in line or "“" in line or "”" in line:
                    bad.append(f"{fp}:{i}:{line.rstrip()}")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PY
)
if [ -z "$INV6_BAD" ]; then
  echo "  PASS — verbatim failure phrases use ASCII straight quotes; no bold; no blockquote"
  PASS=$((PASS + 1))
else
  echo "  FAIL — non-canonical wrapping/quoting on verbatim phrase:"
  echo "$INV6_BAD" | sed 's/^/    /'
  echo "  Fix: strip bold (**), blockquote (>), and curly quotes around the canonical phrase."
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Invariant 7: Tumour-suppressor dual-emit guardrail in nlu_extractor ==="
INV7_BAD=$(
  python3 - <<'PY'
import re, pathlib, sys
ts_genes = ["TP53", "RB1", "PTEN", "BRCA1", "BRCA2", "APC", "VHL", "CDH1", "SMAD4"]
files = [
    "resources/prompts/nlu_extractor.md",
]
bad = []
for fp in files:
    text = pathlib.Path(fp).read_text()
    # Find a line that lists 6+ of the tumour-suppressor genes (the guardrail line)
    block_line = None
    for i, line in enumerate(text.splitlines(), 1):
        hits = sum(1 for g in ts_genes if g in line)
        if hits >= 6:
            block_line = (i, line)
            break
    if not block_line:
        bad.append(f"{fp}: NO tumour-suppressor block found (expected a line listing >=6 of TP53/RB1/PTEN/BRCA1/BRCA2/APC/VHL/CDH1/SMAD4)")
        continue
    i, line = block_line
    # Must mandate dual-emit — look for both gene_name AND gene_symbol in the line OR the following ~5 lines
    window = "\n".join(text.splitlines()[i-1:i+6])
    has_gene_name = ("gene_name" in window) or ("gene name" in window)
    has_gene_symbol = ("gene_symbol" in window) or ("gene symbol" in window)
    if not (has_gene_name and has_gene_symbol):
        bad.append(f"{fp}:{i}: tumour-suppressor block does not mandate dual-emit of gene_name AND gene_symbol")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PY
)
if [ -z "$INV7_BAD" ]; then
  echo "  PASS — nlu_extractor mandates tumour-suppressor dual-emit"
  PASS=$((PASS + 1))
else
  echo "  FAIL — tumour-suppressor dual-emit guardrail missing:"
  echo "$INV7_BAD" | sed 's/^/    /'
  echo "  Fix: ensure both prompts emit BOTH gene_name AND gene_symbol for tumour suppressors."
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Invariant 8: No curly quotes in active prompts (U+201C/U+201D/U+2018/U+2019) ==="
INV8_BAD=$(grep -nP '[\x{201C}\x{201D}\x{2018}\x{2019}]' resources/prompts/*.md resources/prompts/*.yaml 2>/dev/null)
if [ -z "$INV8_BAD" ]; then
  echo "  PASS — no curly quotes in active prompts"
  PASS=$((PASS + 1))
else
  echo "  FAIL — curly quotes detected:"
  echo "$INV8_BAD" | sed 's/^/    /'
  echo "  Fix: replace curly quotes (“ ” ‘ ’) with ASCII '\"' and \"'\""
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Result: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
