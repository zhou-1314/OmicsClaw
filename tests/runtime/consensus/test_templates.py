"""Tests for the workflow-template registry + provenance fold (ADR 0016 T5)."""

from __future__ import annotations

from omicsclaw.runtime.consensus.dispatch import select_consensus_mode
from omicsclaw.runtime.consensus.driver import run_typed_consensus
from omicsclaw.runtime.consensus.templates import TEMPLATES, provenance_of


def test_categorical_is_typed() -> None:
    assert TEMPLATES["categorical"].provenance == "typed"


def test_narrative_is_exploratory() -> None:
    assert TEMPLATES["narrative"].provenance == "exploratory"


def test_provenance_of_helper() -> None:
    assert provenance_of("categorical") == "typed"
    assert provenance_of("narrative") == "exploratory"


def test_categorical_driver_is_run_typed_consensus() -> None:
    assert TEMPLATES["categorical"].driver is run_typed_consensus


def test_dispatch_derives_mode_from_template_provenance() -> None:
    # spatial-domains binds the categorical template (provenance=typed).
    assert select_consensus_mode("spatial-domains") == "typed"
    # unknown skill -> B path.
    assert select_consensus_mode("spatial-velocity") == "narrative"
    # force override still wins.
    assert select_consensus_mode("spatial-velocity", force_mode="typed") == "typed"
