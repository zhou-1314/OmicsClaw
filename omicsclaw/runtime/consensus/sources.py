"""Consensus source registry (ADR 0016 L3) — flavour-keyed declarative contracts.

One ``ConsensusSource`` row per consensus flavour, binding a Workflow template +
the fanned-out ``member_skill`` + a ``MemberArtifactReader`` + a ``MemberPlanner``
+ metadata. Adding a flavour = one row here (+ maybe a new reader/planner). This
is the registry the generic ``run.py`` entry consumes.

Keyed by the **flavour name** (the routable skill name). The derived
``TYPED_CONSENSUS_REGISTRY`` below re-keys the same sources by ``member_skill``
for ``dispatch``'s typed/narrative routing — one source of truth, no second copy.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.runtime.consensus.planners import (
    ChairLLMPlanner,
    IntegrationRepSweepPlanner,
    SweepPlanner,
)
from omicsclaw.runtime.consensus.source_registry import (
    ConsensusSource,
    ScClusteringArtifactReader,
    SpatialDomainsArtifactReader,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _param_hints(*skill_path_parts: str) -> Path:
    """``<repo>/skills/<...>/parameters.yaml`` for a member skill."""
    return _REPO_ROOT.joinpath("skills", *skill_path_parts, "parameters.yaml")


CONSENSUS_SOURCES: dict[str, ConsensusSource] = {
    "consensus-domains": ConsensusSource(
        reader=SpatialDomainsArtifactReader(),
        name="consensus-domains",
        template="categorical",
        member_skill="spatial-domains",
        planner=ChairLLMPlanner(),
        domain="spatial",
        report_title="Verified consensus — spatial domains",
        param_hints_path=_param_hints("spatial", "spatial-domains"),
        intrinsic_panel="spatial",
    ),
    "sc-consensus-clustering": ConsensusSource(
        reader=ScClusteringArtifactReader(),
        name="sc-consensus-clustering",
        template="categorical",
        member_skill="sc-clustering",
        planner=SweepPlanner(),
        domain="singlecell",
        report_title="Verified consensus — sc clustering",
        param_hints_path=_param_hints("singlecell", "scrna", "sc-clustering"),
    ),
    "sc-consensus-integration": ConsensusSource(
        reader=ScClusteringArtifactReader(),
        name="sc-consensus-integration",
        template="categorical",
        member_skill="sc-integrate-cluster",
        planner=IntegrationRepSweepPlanner(),
        domain="singlecell",
        report_title="Verified consensus — sc integration",
        param_hints_path=_param_hints("singlecell", "scrna", "sc-integrate-cluster"),
        intrinsic_panel="integration",
    ),
}


#: Derived member_skill-keyed view for ``dispatch``'s typed/narrative routing.
#: Single source of truth = ``CONSENSUS_SOURCES`` (ADR 0016 T9 — replaces the
#: old hand-maintained dict in ``source_registry.py``). A member skill present
#: here is on the A (typed) path; anything else falls back to B (narrative).
TYPED_CONSENSUS_REGISTRY: dict[str, ConsensusSource] = {
    source.member_skill: source for source in CONSENSUS_SOURCES.values()
}
