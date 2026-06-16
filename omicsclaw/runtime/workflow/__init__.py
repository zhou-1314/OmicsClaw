"""Workflow runtime (ADR 0016 L1) — domain-neutral execution topology.

v1 ships exactly one primitive, ``fan_out``, with a single client today; a
``chain`` primitive and a second client (``pipeline_runner`` re-platformed)
grow together in a later PR — see ``omicsclaw/runtime/CONTEXT.md`` (which names
the client roster). This package imports no concrete step type of its own.
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
