"""Workflow templates (ADR 0016 L2.5) — one consensus math/synthesis shape each.

Each template carries an explicit ``provenance`` (``typed``/verified vs
``exploratory``). The verified/exploratory boundary is now read from here
(``TEMPLATES[source.template].provenance``) rather than from bare membership in
``TYPED_CONSENSUS_REGISTRY`` — ADR 0016, amending ADR 0010's single-file audit
claim. The registry is open but **controlled**: adding a template means adding a
new "verified" math guarantee and requires its own ADR.

Import hygiene: this module imports only the categorical driver
(``run_typed_consensus``), which does not import ``dispatch``. The narrative
B-path executes via ``narrative/{extractor,synthesizer}`` (and ``synthesizer``
imports ``dispatch``), so its ``driver`` is left unbound here to avoid a
``dispatch → templates → narrative → dispatch`` import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from omicsclaw.runtime.consensus.driver import run_typed_consensus

Provenance = Literal["typed", "exploratory"]


@dataclass(frozen=True)
class WorkflowTemplate:
    """A consensus math/synthesis shape + its provenance."""

    provenance: Provenance
    driver: Callable | None = None


#: Open-but-controlled (ADR 0016 B4a). A new entry = a new ADR.
TEMPLATES: dict[str, WorkflowTemplate] = {
    "categorical": WorkflowTemplate(provenance="typed", driver=run_typed_consensus),
    "narrative": WorkflowTemplate(provenance="exploratory", driver=None),
    # v2 "rank" (DE-RRA) / v3 "interval" (variant/SV merge) — each its own ADR.
}


def provenance_of(template: str) -> Provenance:
    """Provenance for a template name. Raises ``KeyError`` for unknown templates."""
    return TEMPLATES[template].provenance
