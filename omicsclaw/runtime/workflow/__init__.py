"""Workflow runtime (ADR 0016 L1) — domain-neutral execution topology.

v1 ships exactly one primitive, ``fan_out``, with exactly one client
(consensus). ``chain`` and a second client (``pipeline_runner`` re-platformed)
grow together in a later PR — see ``omicsclaw/runtime/CONTEXT.md``.
"""

from __future__ import annotations

from omicsclaw.runtime.workflow.fan_out import (
    DEFAULT_TIMEOUT_SECONDS,
    FanOutResult,
    InsufficientSurvivorsError,
    StepRunResult,
    WorkflowStep,
    fan_out,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FanOutResult",
    "InsufficientSurvivorsError",
    "StepRunResult",
    "WorkflowStep",
    "fan_out",
]
