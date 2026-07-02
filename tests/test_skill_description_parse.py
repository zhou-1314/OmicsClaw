"""Tests for the skip_when description parser in scripts/migrate_to_skill_yaml.py.

Locks the fixes for the real v1 descriptions that the heuristic used to mangle:
paren-aware clause split, balanced-paren conditions, and redirect extraction for
use / go-straight-to / run-X-first / use-a-/-b.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.migrate_to_skill_yaml import split_description  # noqa: E402


def _skips(desc: str):
    _, rules, _ = split_description("Load when doing x. " + desc)
    return rules


def test_use_redirect_simple():
    assert _skips("Skip when A (use spatial-register) or for B (use spatial-domains).") == [
        {"condition": "A", "use": "spatial-register"},
        {"condition": "B", "use": "spatial-domains"},
    ]


def test_or_inside_redirect_paren_not_split():
    # "(run a or b first)" must NOT fabricate a third clause.
    rules = _skips(
        "Skip when scRNA-only inference (use `sc-cell-communication`) or when no "
        "cell-type labels exist (run `spatial-annotate` or `spatial-domains` first)."
    )
    assert rules == [
        {"condition": "scRNA-only inference", "use": "sc-cell-communication"},
        {"condition": "no cell-type labels exist", "use": "spatial-annotate"},
    ]


def test_redirect_inside_paren_with_other_text_keeps_balance():
    rules = _skips(
        "Skip when aligning coordinates (use spatial-register) or for single-batch "
        "data (no integration needed — go straight to spatial-domains)."
    )
    assert rules == [
        {"condition": "aligning coordinates", "use": "spatial-register"},
        {"condition": "single-batch data (no integration needed)", "use": "spatial-domains"},
    ]


def test_non_redirect_paren_kept_no_use():
    rules = _skips(
        "Skip when data is single-slice (no registration needed) or for cross-sample "
        "integration (use spatial-integrate)."
    )
    assert rules[0] == {"condition": "data is single-slice (no registration needed)"}
    assert rules[1] == {"condition": "cross-sample integration", "use": "spatial-integrate"}


def test_use_list_picks_first_skill():
    rules = _skips("Skip when non-spatial FASTQ (use bulkrna-read-qc / sc-fastq-qc).")
    assert rules == [{"condition": "non-spatial FASTQ", "use": "bulkrna-read-qc"}]


def test_go_straight_to_redirect():
    rules = _skips("Skip when input is a count matrix (go straight to spatial-preprocess).")
    assert rules == [{"condition": "input is a count matrix", "use": "spatial-preprocess"}]


def test_single_word_skill_redirect():
    rules = _skips("Skip when forward routing (use orchestrator).")
    assert rules == [{"condition": "forward routing", "use": "orchestrator"}]


# --- known_skills gating: reject redirect targets that are not real skills -----

_KNOWN = {"genomics-variant-calling", "sc-perturb"}


def _skips_gated(desc: str):
    _, rules, _ = split_description("Load when doing x. " + desc, _KNOWN)
    return rules


def test_gate_rejects_article_keeps_actionable_note():
    # "(run a phaser first)" is an action, not a `use <skill>`; the regex grabs the
    # article "a" — gating drops it and preserves the parenthetical in the condition.
    rules = _skips_gated(
        "Skip when the input is unphased (run a phaser first) or when calling small "
        "variants (use `genomics-variant-calling`)."
    )
    assert rules == [
        {"condition": "the input is unphased (run a phaser first)"},
        {"condition": "calling small variants", "use": "genomics-variant-calling"},
    ]


def test_gate_rejects_determiner_and_tool_names():
    # "the" (determiner), "MACS"/"Manta" (tool names) are not skills → no use:.
    assert _skips_gated("Skip when peak files are the input (use the relevant downstream skill).") == [
        {"condition": "peak files are the input (use the relevant downstream skill)"}
    ]
    assert _skips_gated("Skip when calling peaks from BAM (use MACS2/MACS3 first).") == [
        {"condition": "calling peaks from BAM (use MACS2/MACS3 first)"}
    ]
    assert _skips_gated("Skip when raw guide-calling from FASTQ (use upstream demuxlet pipelines).") == [
        {"condition": "raw guide-calling from FASTQ (use upstream demuxlet pipelines)"}
    ]


def test_gate_accepts_real_skill():
    assert _skips_gated("Skip when analysing perturbation effects (use `sc-perturb`).") == [
        {"condition": "analysing perturbation effects", "use": "sc-perturb"}
    ]


def test_comma_when_clause_split():
    # Enumerated "… (use X), when Y (use Z), or when W (use V)" — the comma before
    # 'when' must split, or the middle clause merges and its redirect is lost.
    rules = _skips_gated(
        "Skip when filtering / merging VCFs (use `genomics-variant-calling`), when calling "
        "structural variants (use `sc-perturb`), or when adding annotations (use `sc-perturb`)."
    )
    assert rules == [
        {"condition": "filtering / merging VCFs", "use": "genomics-variant-calling"},
        {"condition": "calling structural variants", "use": "sc-perturb"},
        {"condition": "adding annotations", "use": "sc-perturb"},
    ]


def test_comma_when_split_preserves_non_redirect_action():
    # First clause's parenthetical is an action (bcftools), not a skill redirect.
    rules = _skips_gated(
        "Skip when input is a raw VCF (convert with `bcftools +split-vep` first), when calling "
        "raw variants (use `genomics-variant-calling`)."
    )
    assert rules == [
        {"condition": "input is a raw VCF (convert with `bcftools +split-vep` first)"},
        {"condition": "calling raw variants", "use": "genomics-variant-calling"},
    ]
