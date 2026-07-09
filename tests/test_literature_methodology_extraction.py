"""extract_methodology() — P5 corpus-derived scaffolding's iron rule.

Every candidate it returns must carry a verifiable (quote, char_span) slice
into the source text — never a fabricated/paraphrased value. This is
Tier-1-only (fixed-vocabulary "KEY (op) NUMBER" regex): no gene-association
extraction, no "mentioned but unparseable" TODO tier (see
docs/proposals/skill-acquisition-plan.md P5 + the plan mode decision log).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "literature"))

from core.extractor import extract_methodology  # noqa: E402


def _assert_spans_consistent(candidates):
    for c in candidates:
        start, end = c["char_span"]
        assert start < end
        assert c["quote"] != ""


# ---- span-consistency invariant ----


def test_span_consistency_holds_for_every_candidate():
    text = (
        "Leiden clustering (resolution=0.8) was applied after computing "
        "n_pcs=30 principal components."
    )
    candidates = extract_methodology(text)
    assert candidates
    for c in candidates:
        start, end = c["char_span"]
        assert text[start:end] == c["quote"]
    _assert_spans_consistent(candidates)


def test_span_consistency_holds_with_multibyte_prefix():
    # A preceding multi-byte (CJK) string must not desync the span — Python
    # string indexing is character-based, unlike ast.col_offset's UTF-8-byte
    # offsets (the P2a gotcha), but this is still worth pinning explicitly.
    text = "A sentence with a unicode char (你好) before resolution=0.5."
    candidates = extract_methodology(text)
    assert len(candidates) == 1
    c = candidates[0]
    assert text[c["char_span"][0]:c["char_span"][1]] == c["quote"] == "resolution=0.5"
    assert c["value"] == 0.5


# ---- positive matches ----


def test_positive_match_resolution_equals():
    [c] = extract_methodology("Leiden clustering (resolution=0.8) was applied.")
    assert c == {
        "param": "resolution",
        "operator": "=",
        "value": 0.8,
        "quote": "resolution=0.8",
        "char_span": [19, 33],
        "todo": False,
    }


def test_positive_match_fdr_less_than():
    [c] = extract_methodology("Differential expression used FDR < 0.1 as threshold.")
    assert c["param"] == "fdr"
    assert c["operator"] == "<"
    assert c["value"] == 0.1
    assert c["quote"] == "FDR < 0.1"
    assert c["todo"] is False


def test_positive_match_n_pcs_integer():
    [c] = extract_methodology("n_pcs=30 principal components were retained.")
    assert c["param"] == "n_pcs"
    assert c["value"] == 30
    assert isinstance(c["value"], int)


def test_positive_match_colon_operator():
    [c] = extract_methodology("Genes were filtered with min_genes: 200 per cell.")
    assert c["param"] == "min_genes"
    assert c["operator"] == ":"
    assert c["value"] == 200


def test_positive_match_case_insensitive_alias():
    [c] = extract_methodology("RESOLUTION=0.8 was used for clustering.")
    assert c["param"] == "resolution"
    assert c["quote"] == "RESOLUTION=0.8"


def test_positive_match_scientific_notation_value():
    [c] = extract_methodology("Significance threshold was p-value=1e-5.")
    assert c["param"] == "p_value"
    assert c["value"] == 1e-5


# ---- negative / no-match ----


def test_no_match_on_plain_prose():
    assert extract_methodology("This is plain prose with no recognized parameters.") == []


def test_no_match_when_alias_present_but_no_adjacent_number():
    # "p-value" is mentioned but not immediately followed by an operator+number
    # ("of" sits in between) — Tier 1 must not guess a value here.
    assert extract_methodology("We used a p-value of 0.05 for significance.") == []


def test_no_fabrication_only_literally_present_params_appear():
    text = "resolution=0.8 was the only parameter mentioned in this text."
    candidates = extract_methodology(text)
    params = {c["param"] for c in candidates}
    assert params == {"resolution"}
    assert "fdr" not in params and "n_pcs" not in params


# ---- first-match-wins dedup ----


def test_first_match_wins_on_repeated_mentions():
    text = "First we set resolution=0.8, but later re-ran with resolution=1.2 for validation."
    candidates = extract_methodology(text)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["value"] == 0.8
    assert c["quote"] == "resolution=0.8"
    assert text[c["char_span"][0]:c["char_span"][1]] == c["quote"]


def test_multiple_distinct_params_all_returned():
    text = "resolution=0.8, n_pcs=30, min_cells=3, FDR < 0.1, log2fc=1.5"
    candidates = extract_methodology(text)
    params = {c["param"] for c in candidates}
    assert params == {"resolution", "n_pcs", "min_cells", "fdr", "log2fc"}
    _assert_spans_consistent(candidates)


# ---- Tier 1 scope guard: never emits todo=True itself ----


def test_never_emits_todo_candidates():
    # Tier 1 only returns candidates it can back with a real span — no
    # "mentioned but unparseable" placeholder tier (deferred, see plan).
    text = "See Table S2 for the full list of clustering parameters and thresholds."
    assert extract_methodology(text) == []


# ---- adversarial regressions (found by codex cross-validation) ----


def test_number_embedded_in_larger_token_is_rejected_not_truncated():
    # "0.8abc" must not silently become 0.8 (dropping "abc") NOR silently
    # truncate further to a wrong shorter number (e.g. just "0") via regex
    # backtracking — either would misrepresent the source. No match at all.
    assert extract_methodology("resolution=0.8abc was used for clustering.") == []
    assert extract_methodology("n_pcs=30cells were retained.") == []


def test_truncated_exponent_with_no_digits_is_rejected():
    # "1e-" has no digit after the exponent sign — not a valid number at all;
    # must not be silently read as just "1".
    assert extract_methodology("p-value=1e- was reported.") == []


def test_exponent_with_explicit_plus_sign_is_captured_in_full():
    [c] = extract_methodology("p-value=1e+5 was the threshold.")
    assert c["param"] == "p_value"
    assert c["value"] == 1e5
    assert c["quote"] == "p-value=1e+5"


def test_sentence_ending_period_does_not_suppress_a_real_match():
    # A trailing '.' (common prose) must not be treated as an ambiguous
    # numeric-continuation boundary the way a trailing letter/digit is.
    [c] = extract_methodology("The threshold used was resolution=0.8.")
    assert c["value"] == 0.8
    assert c["quote"] == "resolution=0.8"
