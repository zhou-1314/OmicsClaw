"""consensus-interpret exception classes.

Each exception maps 1:1 to a CLI exit code per ADR 0012 §"Failure
semantics". Continuous with ADR 0010's consensus-domains convention
(3 / 5 / 6); 4 / 7 / 8 are new for this skill.
"""

from __future__ import annotations


class ConsensusInterpretError(Exception):
    """Base for all consensus-interpret failures.

    Subclasses set ``exit_code`` so the CLI wrapper can map exceptions
    to exit codes without branching on exception type.
    """

    exit_code: int = 1


class TypedRunInvalidError(ConsensusInterpretError):
    """T1: ``--input`` is not a usable typed-consensus run directory.

    Triggers:
    - ``plan.json`` missing or malformed
    - ``consensus_labels.tsv`` missing
    - ``plan.json`` lacks ``input_path`` *and* no ``--adata`` override
    """

    exit_code = 3


class AdataMismatchError(ConsensusInterpretError):
    """T1: the adata file is missing or its obs index does not contain
    the observation ids the typed run produced labels for."""

    exit_code = 4


class MarkerDBUnavailableError(ConsensusInterpretError):
    """T1: requested ``--tissue`` has no bundled DB and no ``--markers``."""

    exit_code = 5


class LLMUnavailableError(ConsensusInterpretError):
    """T1: LLM endpoint unreachable and ``--no-llm`` not given."""

    exit_code = 6


class InvariantViolationError(ConsensusInterpretError):
    """T3: LLM output violated marker-grounding / evidence-ref / banner
    contract. Non-recoverable; a bug, not a degradation."""

    exit_code = 7


class CoverageBelowThresholdError(ConsensusInterpretError):
    """T2 escalated to T1: fewer than ``--coverage-floor`` fraction of
    clusters could be interpreted (after per-cluster T2 degradation)."""

    exit_code = 8
