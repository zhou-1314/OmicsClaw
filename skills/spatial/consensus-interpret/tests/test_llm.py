"""Slice 5 — LLM annotation + next-step synthesis with stubbed LLM.

Default CI does NOT hit a real LLM; tests inject a synchronous stub
callable via the ``llm_call=`` parameter. Schema parsing + retry-on-
malformed + InvariantViolationError-on-persistent-failure are the
load-bearing behaviors validated here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _cluster_ctx(cluster_id: int = 0, n_cells: int = 778) -> dict:
    return {
        "cluster_id": cluster_id,
        "n_cells": n_cells,
        "mean_local_purity": 0.617,
        "member_agreement_summary": "leiden_resolution-0.5 (0.92 overlap), leiden_resolution-1.0 (0.78)",
        "nmi_neighbors": "  cluster 3 (NMI=0.597 vs leiden_resolution-1.5)",
        "de_top_k_rows": "1\tPvrl3\t12.3\t1.0e-50\n2\tWfs1\t9.4\t3.0e-30",
    }


def _candidates():
    from _candidates import RankedCandidate, SupportingMarker  # type: ignore[import-not-found]
    return [
        RankedCandidate(
            cell_type="CA1 pyramidal",
            score=1.7,
            supporting_markers=[
                SupportingMarker(gene="Pvrl3", de_rank=1, weight=0.85),
                SupportingMarker(gene="Wfs1", de_rank=2, weight=0.86),
            ],
        ),
        RankedCandidate(
            cell_type="Dentate granule cell",
            score=0.6,
            supporting_markers=[SupportingMarker(gene="Prox1", de_rank=5, weight=0.90)],
        ),
    ]


def _good_llm_response(cluster_id: int = 0) -> str:
    return json.dumps({
        "cluster_id": cluster_id,
        "cell_type": "CA1 pyramidal",
        "confidence": 0.84,
        "evidence_markers": [
            {"gene": "Pvrl3", "de_rank": 1, "db_source": "panglaodb_brain", "db_celltype": "CA1 pyramidal", "weight": 0.85},
        ],
        "narrative": "Cluster 0 strongly expresses Pvrl3 with mean_local_purity 0.617, consistent with CA1 pyramidal identity.",
    })


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #

def test_annotate_cluster_returns_parsed_annotation() -> None:
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    seen_prompts: list[str] = []

    def stub_llm(prompt: str) -> str:
        seen_prompts.append(prompt)
        return _good_llm_response(0)

    annotation = annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=stub_llm)

    assert annotation.cluster_id == 0
    assert annotation.cell_type == "CA1 pyramidal"
    assert annotation.confidence == pytest.approx(0.84)
    assert len(annotation.evidence_markers) == 1
    assert annotation.evidence_markers[0].gene == "Pvrl3"
    # narrative must mention a verbatim cluster-context value
    assert "Pvrl3" in annotation.narrative or "0.617" in annotation.narrative

    # Prompt should include candidate list + DE rows
    assert "Pvrl3" in seen_prompts[0]
    assert "CA1 pyramidal" in seen_prompts[0]
    assert "Cluster 0" in seen_prompts[0] or "cluster_id" in seen_prompts[0].lower()


def test_annotate_cluster_extracts_json_from_markdown_codefence() -> None:
    """Many LLMs wrap JSON in ```json ... ```; the parser must strip it."""
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    fenced = "Here is the analysis:\n\n```json\n" + _good_llm_response(0) + "\n```\n"
    annotation = annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=lambda p: fenced)
    assert annotation.cell_type == "CA1 pyramidal"


# --------------------------------------------------------------------------- #
# Retry on malformed                                                          #
# --------------------------------------------------------------------------- #

def test_annotate_cluster_retries_once_on_malformed_json() -> None:
    """First LLM call returns garbage → retry → second call succeeds."""
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    call_count = {"n": 0}

    def flaky(prompt: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "Sorry, I cannot produce valid JSON."
        return _good_llm_response(0)

    annotation = annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=flaky)
    assert call_count["n"] == 2
    assert annotation.cell_type == "CA1 pyramidal"


def test_annotate_cluster_invariant_violation_after_persistent_malformed() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    def bad(prompt: str) -> str:
        return "I refuse to comply with JSON output."

    with pytest.raises(InvariantViolationError, match="JSON|parse|malformed"):
        annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=bad)


def test_annotate_cluster_invariant_violation_when_cell_type_not_in_candidates() -> None:
    """LLM hallucinated cell_type not in the candidate list → InvariantViolation."""
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    def hallucinator(prompt: str) -> str:
        return json.dumps({
            "cluster_id": 0,
            "cell_type": "MadeUpCell",
            "confidence": 0.9,
            "evidence_markers": [{"gene": "Pvrl3", "de_rank": 1, "db_source": "panglaodb_brain", "db_celltype": "MadeUpCell", "weight": 0.85}],
            "narrative": "Cluster 0 has Pvrl3.",
        })

    with pytest.raises(InvariantViolationError, match="cell_type|candidate"):
        annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=hallucinator)


def test_annotate_cluster_invariant_violation_when_evidence_markers_empty() -> None:
    """cell_type != 'Unknown' but evidence_markers == [] → InvariantViolation."""
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    def empty_evidence(prompt: str) -> str:
        return json.dumps({
            "cluster_id": 0,
            "cell_type": "CA1 pyramidal",
            "confidence": 0.84,
            "evidence_markers": [],   # forbidden
            "narrative": "Cluster 0.",
        })

    with pytest.raises(InvariantViolationError, match="evidence_markers"):
        annotate_cluster(_cluster_ctx(0), _candidates(), llm_call=empty_evidence)


def test_annotate_cluster_allows_unknown_with_empty_markers() -> None:
    """cell_type == 'Unknown' is the one case where evidence_markers may be empty."""
    from _llm import annotate_cluster  # type: ignore[import-not-found]

    def unknown(prompt: str) -> str:
        return json.dumps({
            "cluster_id": 5,
            "cell_type": "Unknown",
            "confidence": 0.1,
            "evidence_markers": [],
            "narrative": "Cluster 5 markers do not match any candidate; mean_local_purity 0.617 suggests low confidence.",
        })

    annotation = annotate_cluster(_cluster_ctx(5), _candidates(), llm_call=unknown)
    assert annotation.cell_type == "Unknown"


# --------------------------------------------------------------------------- #
# Next-step synthesis                                                         #
# --------------------------------------------------------------------------- #

def _good_next_steps_response() -> str:
    return json.dumps({
        "next_steps": [
            {
                "skill": "spatial-de",
                "args_hint": "--groupby consensus_kmode --comparisons cluster_3_vs_5",
                "priority": 1,
                "evidence_refs": [
                    "cross_method_nmi.csv:row=leiden_resolution-0.5,col=leiden_resolution-1.5,value=0.597",
                    "consensus_labels.tsv:cluster_3 has 877 cells",
                ],
                "reason": "Lowest pair-wise NMI in matrix; marker disambiguation needed.",
            },
        ],
    })


def test_synthesize_next_steps_returns_parsed_list() -> None:
    from _llm import synthesize_next_steps  # type: ignore[import-not-found]

    nmi = pd.DataFrame(
        [[1.0, 0.597], [0.597, 1.0]],
        index=["leiden_resolution-0.5", "leiden_resolution-1.5"],
        columns=["leiden_resolution-0.5", "leiden_resolution-1.5"],
    )

    next_steps = synthesize_next_steps(
        annotations=[],  # empty annotations OK for this prompt-shape test
        nmi_matrix=nmi,
        top_k=3,
        llm_call=lambda p: _good_next_steps_response(),
    )

    assert len(next_steps) == 1
    assert next_steps[0].skill == "spatial-de"
    assert next_steps[0].priority == 1
    assert len(next_steps[0].evidence_refs) == 2


def test_synthesize_next_steps_invariant_violation_on_empty_evidence_refs() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _llm import synthesize_next_steps  # type: ignore[import-not-found]

    nmi = pd.DataFrame([[1.0]], index=["m0"], columns=["m0"])
    bad = json.dumps({
        "next_steps": [
            {"skill": "spatial-de", "args_hint": "", "priority": 1, "evidence_refs": [], "reason": "vague"},
        ],
    })

    with pytest.raises(InvariantViolationError, match="evidence_refs"):
        synthesize_next_steps(annotations=[], nmi_matrix=nmi, llm_call=lambda p: bad)


def test_synthesize_next_steps_invariant_violation_on_unknown_skill() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _llm import synthesize_next_steps  # type: ignore[import-not-found]

    nmi = pd.DataFrame([[1.0]], index=["m0"], columns=["m0"])
    bad = json.dumps({
        "next_steps": [
            {
                "skill": "totally-made-up-skill",
                "args_hint": "",
                "priority": 1,
                "evidence_refs": ["cross_method_nmi.csv:m0,m0=1.0"],
                "reason": "test",
            },
        ],
    })

    with pytest.raises(InvariantViolationError, match="skill"):
        synthesize_next_steps(annotations=[], nmi_matrix=nmi, llm_call=lambda p: bad)


def test_synthesize_next_steps_caps_at_top_k() -> None:
    from _llm import synthesize_next_steps  # type: ignore[import-not-found]

    nmi = pd.DataFrame([[1.0]], index=["m0"], columns=["m0"])
    many = json.dumps({
        "next_steps": [
            {"skill": s, "args_hint": "x", "priority": 1, "evidence_refs": ["cross_method_nmi.csv:x=y"], "reason": "r"}
            for s in ("spatial-de", "spatial-deconv", "spatial-communication", "spatial-trajectory", "spatial-cnv")
        ],
    })

    next_steps = synthesize_next_steps(annotations=[], nmi_matrix=nmi, top_k=3, llm_call=lambda p: many)
    assert len(next_steps) == 3
