"""Typed vs narrative consensus dispatch — the A/B path router.

ADR 0010 boundary: a skill in ``TYPED_CONSENSUS_REGISTRY`` has a typed
operator and is allowed on the A path. Anything else falls back to the B
(narrative) path. New skills must opt in explicitly — there is no implicit
output-schema sniffing, because the verified/exploratory boundary must be
auditable from a single file.

The registry itself is defined in ``source_registry.py`` (a dict of
``TypedConsensusSource`` carrying the per-source ``MemberArtifactReader``).
This module only owns the A/B routing decision and the URI / banner
conventions; it imports the registry for membership checks.
"""

from __future__ import annotations

from typing import Literal

from omicsclaw.runtime.consensus.source_registry import TYPED_CONSENSUS_REGISTRY

__all__ = [
    "ConsensusMode",
    "TYPED_CONSENSUS_REGISTRY",
    "consensus_namespace",
    "output_banner",
    "select_consensus_mode",
]

ConsensusMode = Literal["typed", "narrative"]


def select_consensus_mode(
    skill_name: str,
    force_mode: ConsensusMode | None = None,
) -> ConsensusMode:
    """Route a fan-out target to ``typed`` (A path) or ``narrative`` (B path).

    ``force_mode`` is the explicit user override; pass ``None`` to use the
    registry-based default.
    """
    if force_mode is not None:
        if force_mode not in ("typed", "narrative"):
            raise ValueError(
                f"force_mode must be 'typed' or 'narrative', got {force_mode!r}"
            )
        return force_mode
    return "typed" if skill_name in TYPED_CONSENSUS_REGISTRY else "narrative"


def consensus_namespace(run_id: str, mode: ConsensusMode) -> str:
    """Return the graph-memory URI for a consensus run.

    ADR 0010 splits ``analysis://typed/<run_id>`` and
    ``analysis://exploratory/<run_id>``; future meta-analysis defaults to
    reading only ``typed/*``.
    """
    if mode == "typed":
        return f"analysis://typed/{run_id}"
    return f"analysis://exploratory/{run_id}"


def output_banner(mode: ConsensusMode) -> str:
    """Mandatory, non-configurable report header per ADR 0010."""
    if mode == "typed":
        return "[A: Verified consensus]"
    return "[B: Exploratory synthesis — NOT statistical consensus]"
