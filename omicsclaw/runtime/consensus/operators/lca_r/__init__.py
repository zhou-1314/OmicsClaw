"""LCA consensus via R subprocess (diceR::LCA).

Python equivalent is intentionally not provided — see ADR 0011 for why
LCA stays in R while kmode/weighted are reimplemented in Python.
"""

from omicsclaw.runtime.consensus.operators.lca_r.wrapper import (
    LCAUnavailableError,
    lca_consensus,
    rscript_available,
)

__all__ = ["LCAUnavailableError", "lca_consensus", "rscript_available"]
