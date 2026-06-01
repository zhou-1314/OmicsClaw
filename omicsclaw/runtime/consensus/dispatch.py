"""Typed vs narrative consensus dispatch — the A/B path router.

Boundary: a skill whose registered source binds a template with ``typed``
provenance is on the A path; anything else falls back to the B (narrative)
path. Provenance is read from ``templates.TEMPLATES`` (ADR 0016, amending
ADR 0010 — the verified/exploratory boundary is now two explicit fields,
``source.template`` + ``template.provenance``, rather than one allowlist set).

The flavour registry (``CONSENSUS_SOURCES``) and its derived member_skill-keyed
view (``TYPED_CONSENSUS_REGISTRY``) live in ``sources.py``. This module only
owns the A/B routing decision and the URI / banner conventions.
"""

from __future__ import annotations

from typing import Literal

from omicsclaw.runtime.consensus.sources import TYPED_CONSENSUS_REGISTRY
from omicsclaw.runtime.consensus.templates import provenance_of

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
    # Provenance is the source of truth (ADR 0016, amending ADR 0010): a skill
    # is on the A (typed) path iff its registered source binds a template whose
    # provenance is "typed". Unknown skills fall back to the B (narrative) path.
    source = TYPED_CONSENSUS_REGISTRY.get(skill_name)
    if source is None:
        return "narrative"
    return "typed" if provenance_of(source.template) == "typed" else "narrative"


def consensus_namespace(run_id: str, mode: ConsensusMode, thread_id: str = "") -> str:
    """Return the graph-memory URI for a consensus run.

    ADR 0010 splits ``analysis://typed/<run_id>`` and
    ``analysis://exploratory/<run_id>``; future meta-analysis defaults to
    reading only ``typed/*``.

    Bench (ADR 0018): when ``thread_id`` is set the run's lineage is scoped
    under the investigation thread — ``analysis://<thread_id>/typed/<run_id>`` —
    so a thread rolls up only its own runs. Empty ``thread_id`` preserves the
    legacy un-scoped URIs (backward compatible); ``thread_id`` is orthogonal to
    the typed/narrative routing decision.
    """
    sub = "typed" if mode == "typed" else "exploratory"
    if thread_id:
        return f"analysis://{thread_id}/{sub}/{run_id}"
    return f"analysis://{sub}/{run_id}"


def output_banner(mode: ConsensusMode) -> str:
    """Mandatory, non-configurable report header per ADR 0010."""
    if mode == "typed":
        return "[A: Verified consensus]"
    return "[B: Exploratory synthesis — NOT statistical consensus]"
