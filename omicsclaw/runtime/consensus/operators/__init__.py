"""Typed consensus operators (categorical: kmode + weighted; LCA via R subprocess)."""

from omicsclaw.runtime.consensus.operators.alignment import align_labels
from omicsclaw.runtime.consensus.operators.categorical import (
    ConsensusResult,
    kmode_consensus,
    normalize_by_frequency,
    weighted_consensus,
)

__all__ = [
    "ConsensusResult",
    "align_labels",
    "kmode_consensus",
    "normalize_by_frequency",
    "weighted_consensus",
]
