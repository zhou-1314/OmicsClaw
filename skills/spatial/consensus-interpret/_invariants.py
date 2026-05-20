"""T3 invariant enforcement per ADR 0012 §"T3 invariants".

These checks run over the *final* structured interpreted output (the
same shape Slice 7 writes to interpreted_assignments.json). They are
intentionally redundant with the in-LLM-parse checks in `_llm.py` —
two independent grep-tested layers lock the boundary so any future
output-construction path is caught even if it bypasses the LLM call
(e.g. degrade mode, programmatic post-hoc augmentation).

These functions raise :class:`InvariantViolationError` (exit 7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _errors import InvariantViolationError

if TYPE_CHECKING:
    from _llm import ClusterAnnotation, NextStep


ALLOWED_BANNERS: frozenset[str] = frozenset({
    "[A+I: Interpreted on verified consensus]",
    "[I-noLLM: Structural patterns only — biology annotation disabled]",
})


def enforce_interpreted_invariants(
    *,
    annotations: "list[ClusterAnnotation]",
    next_steps: "list[NextStep]",
    banner: str,
) -> None:
    """Raise :class:`InvariantViolationError` on any T3 violation.

    Checks
    ------
    1. Banner is exactly one of the two allowed values.
    2. Every cell-type claim (cell_type != "Unknown") has
       ``evidence_markers`` non-empty.
    3. Every next-step recommendation has ``evidence_refs`` non-empty.

    Any violation here is a CODE bug (Slice 5 should already have
    raised), not an LLM bug — making it explicit at the structural
    boundary defends against future degrade-mode / agent-loop /
    post-hoc paths that bypass the LLM parser.
    """
    if banner not in ALLOWED_BANNERS:
        raise InvariantViolationError(
            f"banner {banner!r} not in allowed set {sorted(ALLOWED_BANNERS)}. "
            f"ADR 0012 §banner invariant."
        )

    for a in annotations:
        if a.cell_type == "Unknown":
            continue
        if not a.evidence_markers:
            raise InvariantViolationError(
                f"cluster {a.cluster_id} cell_type={a.cell_type!r} but "
                f"evidence_markers is empty. ADR 0012 T3 marker-grounding "
                f"invariant."
            )

    for ns in next_steps:
        if not ns.evidence_refs:
            raise InvariantViolationError(
                f"next_step skill={ns.skill!r} has empty evidence_refs. "
                f"ADR 0012 T3 evidence_refs invariant."
            )
