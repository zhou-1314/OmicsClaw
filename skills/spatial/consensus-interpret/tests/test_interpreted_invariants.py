"""Slice 6 — T3 grep-tested invariants per ADR 0012.

Three structural checks lock the boundary integrity claim of the
interpreted layer. They run over the final structured output (the
same JSON the report writer emits), so any path that produces the
final artifact is covered — not just the LLM-call code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _annotation(cluster_id: int, cell_type: str, markers: int = 1):
    from _candidates import SupportingMarker  # noqa: F401 (kept for symmetry)
    from _llm import ClusterAnnotation, EvidenceMarker  # type: ignore[import-not-found]
    return ClusterAnnotation(
        cluster_id=cluster_id,
        n_cells=100,
        cell_type=cell_type,
        confidence=0.8,
        evidence_markers=[
            EvidenceMarker(gene=f"G{i}", de_rank=i + 1, db_source="t", db_celltype=cell_type, weight=0.8)
            for i in range(markers)
        ],
        narrative="Cluster 0 has G0.",
    )


def _next_step(evidence_refs: list[str] | None = None):
    from _llm import NextStep  # type: ignore[import-not-found]
    return NextStep(
        skill="spatial-de",
        args_hint="--groupby consensus_kmode",
        priority=1,
        evidence_refs=evidence_refs if evidence_refs is not None else ["cross_method_nmi.csv:row=m0,col=m1,value=0.5"],
        reason="test",
    )


_BANNER_AI = "[A+I: Interpreted on verified consensus]"
_BANNER_NOLLM = "[I-noLLM: Structural patterns only — biology annotation disabled]"


# --------------------------------------------------------------------------- #
# Banner invariant                                                            #
# --------------------------------------------------------------------------- #

def test_enforce_invariants_accepts_AI_banner() -> None:
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    enforce_interpreted_invariants(
        annotations=[_annotation(0, "CA1 pyramidal")],
        next_steps=[_next_step()],
        banner=_BANNER_AI,
    )  # no raise = pass


def test_enforce_invariants_accepts_noLLM_banner() -> None:
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    # noLLM mode: no annotations, no next steps
    enforce_interpreted_invariants(
        annotations=[],
        next_steps=[],
        banner=_BANNER_NOLLM,
    )


def test_enforce_invariants_rejects_wrong_banner() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    with pytest.raises(InvariantViolationError, match="banner"):
        enforce_interpreted_invariants(
            annotations=[_annotation(0, "CA1 pyramidal")],
            next_steps=[_next_step()],
            banner="[A: Verified consensus]",  # typed banner, wrong for interpreted layer
        )


def test_enforce_invariants_rejects_missing_banner() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    with pytest.raises(InvariantViolationError, match="banner"):
        enforce_interpreted_invariants(
            annotations=[_annotation(0, "CA1 pyramidal")],
            next_steps=[_next_step()],
            banner="",
        )


# --------------------------------------------------------------------------- #
# Marker grounding (every non-Unknown claim cites ≥1 marker)                  #
# --------------------------------------------------------------------------- #

def test_enforce_invariants_rejects_celltype_claim_without_markers() -> None:
    """The headline grep test promised in ADR 0012 §"T3 invariants"."""
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    bad = _annotation(0, "CA1 pyramidal", markers=0)  # 0 markers; cell_type != Unknown
    with pytest.raises(InvariantViolationError, match="evidence|marker"):
        enforce_interpreted_invariants(
            annotations=[bad],
            next_steps=[_next_step()],
            banner=_BANNER_AI,
        )


def test_enforce_invariants_allows_unknown_without_markers() -> None:
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    unknown = _annotation(0, "Unknown", markers=0)
    enforce_interpreted_invariants(
        annotations=[unknown],
        next_steps=[_next_step()],
        banner=_BANNER_AI,
    )


# --------------------------------------------------------------------------- #
# next_step evidence_refs                                                     #
# --------------------------------------------------------------------------- #

def test_enforce_invariants_rejects_nextstep_without_evidence_refs() -> None:
    from _errors import InvariantViolationError  # type: ignore[import-not-found]
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    with pytest.raises(InvariantViolationError, match="evidence_refs"):
        enforce_interpreted_invariants(
            annotations=[_annotation(0, "CA1 pyramidal")],
            next_steps=[_next_step(evidence_refs=[])],
            banner=_BANNER_AI,
        )


# --------------------------------------------------------------------------- #
# Composability: a no-annotation, no-next-step interpret pass is valid        #
# --------------------------------------------------------------------------- #

def test_enforce_invariants_empty_annotations_AI_banner_ok() -> None:
    """A fully-degraded run (every cluster Unknown or low_confidence) with no
    next-steps is allowed if banner is correct — it's an HONEST report."""
    from _invariants import enforce_interpreted_invariants  # type: ignore[import-not-found]

    enforce_interpreted_invariants(
        annotations=[],
        next_steps=[],
        banner=_BANNER_AI,
    )
