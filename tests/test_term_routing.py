"""Unit tests for app/utils/term_routing.py.

Run from the repo root:
    pytest tests/test_term_routing.py -q
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.utils.term_routing import (
    split_terms_from_field,
    route_terms,
    filter_hgvs_like,
    mirror_columns,
    reattach_preserved,
    get_db_vocabulary,
)


def test_split_exact_match():
    kept, matched = split_terms_from_field(["Pathogenic"], {"pathogenic"})
    assert kept == [] and matched == ["pathogenic"]


def test_split_substring_longest_first():
    # "likely pathogenic missense" should match "likely pathogenic", not "pathogenic"
    kept, matched = split_terms_from_field(
        ["likely pathogenic missense"],
        {"pathogenic", "likely pathogenic"},
    )
    assert matched == ["likely pathogenic"]
    assert kept == []


def test_split_no_match_kept():
    kept, matched = split_terms_from_field(["NM_000546.6:c.215C>G"], {"pathogenic"})
    assert kept == ["NM_000546.6:c.215C>G"] and matched == []


def test_split_mixed_input():
    kept, matched = split_terms_from_field(
        ["TP53", "pathogenic", "NM_x:c.1A>T"],
        {"pathogenic"},
    )
    assert "pathogenic" in matched
    assert "TP53" in kept and "NM_x:c.1A>T" in kept


def test_split_non_string_passthrough():
    kept, matched = split_terms_from_field([None, 5, "pathogenic"], {"pathogenic"})
    assert None in kept and 5 in kept and matched == ["pathogenic"]


def test_split_empty_term_set():
    kept, matched = split_terms_from_field(["anything"], set())
    assert kept == ["anything"] and matched == []


def test_route_terms_clinvar_pathogenic():
    pv = {"gene_symbol": ["TP53"], "variant_name": ["pathogenic"]}
    preserved = route_terms(pv, db="clinvar")
    assert preserved == {"clinical_significance": ["pathogenic"]}
    assert pv.get("variant_name") is None  # field dropped after extraction
    assert pv["clinical_significance"] == ["pathogenic"]


def test_route_terms_civic_evidence_with_normalizer():
    pv = {"gene_name": ["EGFR"], "variant_name": ["Level A"]}
    preserved = route_terms(
        pv, db="civic",
        normalizers={"evidence_level": lambda s: s.split()[-1].upper()},
    )
    assert preserved == {"evidence_level": ["A"]}
    assert pv["evidence_level"] == ["A"]
    assert pv.get("variant_name") is None


def test_route_terms_no_match_unchanged():
    pv = {"gene_symbol": ["BRCA1"], "variant_name": ["NM_007294:c.66dupA"]}
    before = dict(pv)
    preserved = route_terms(pv, db="clinvar")
    assert preserved == {}
    assert pv == before


def test_route_terms_unknown_db_no_op():
    pv = {"variant_name": ["pathogenic"]}
    preserved = route_terms(pv, db="not-a-real-db-name")
    assert preserved == {}
    assert pv == {"variant_name": ["pathogenic"]}


def test_filter_hgvs_like_drops_concept():
    pv = {"variant_name": ["de novo", "missense"]}
    filter_hgvs_like(pv, "variant_name")
    assert "variant_name" not in pv


def test_filter_hgvs_like_keeps_real():
    pv = {"variant_name": ["NM_000546.6(TP53):c.524G>A", "de novo"]}
    filter_hgvs_like(pv, "variant_name")
    assert pv["variant_name"] == ["NM_000546.6(TP53):c.524G>A"]


def test_filter_hgvs_like_protein_change():
    pv = {"variant_name": ["p.R175H"]}
    filter_hgvs_like(pv, "variant_name")
    assert pv["variant_name"] == ["p.R175H"]


def test_mirror_columns_unions_both_directions():
    pv = {"gene_symbol": ["TP53"], "gene_name": ["tumor protein p53"]}
    mirror_columns(pv, "gene_name", "gene_symbol")
    assert pv["gene_name"] == pv["gene_symbol"]
    assert "TP53" in pv["gene_name"] and "tumor protein p53" in pv["gene_name"]


def test_mirror_columns_one_sided():
    pv = {"gene_symbol": ["TP53"]}
    mirror_columns(pv, "gene_name", "gene_symbol")
    assert pv["gene_name"] == ["TP53"] and pv["gene_symbol"] == ["TP53"]


def test_mirror_columns_handles_string_values():
    pv = {"gene_symbol": "BRCA1"}
    mirror_columns(pv, "gene_name", "gene_symbol")
    assert pv["gene_name"] == ["BRCA1"]


def test_reattach_preserved_only_when_missing():
    fv = {"clinical_significance": ["benign"]}
    reattach_preserved(fv, {"clinical_significance": ["pathogenic"], "evidence_level": ["A"]})
    # Existing not overwritten
    assert fv["clinical_significance"] == ["benign"]
    # New field added
    assert fv["evidence_level"] == ["A"]


def test_get_db_vocabulary_lowercased():
    vocab = get_db_vocabulary("clinvar")
    assert "clinical_significance" in vocab
    assert "pathogenic" in vocab["clinical_significance"]


if __name__ == "__main__":
    failures = 0
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ! {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)-failures}/{len(tests)} passed")
    sys.exit(failures)
