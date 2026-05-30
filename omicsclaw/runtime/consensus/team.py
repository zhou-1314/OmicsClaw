"""Back-compat shim — fan-out moved to ``runtime/workflow/fan_out.py`` (ADR 0016 L1).

The team-runtime machinery is now the domain-neutral ``fan_out`` primitive in
the workflow runtime. This module re-exports the consensus-flavoured aliases
(``run_team``, ``MemberRunResult``, ``TeamRunResult``, ...) so existing
importers (``driver.py``, ``test_team_runtime.py``) keep working unchanged.

New code should import from ``omicsclaw.runtime.workflow.fan_out`` directly.
"""

from __future__ import annotations

from omicsclaw.runtime.workflow.fan_out import (  # noqa: F401
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PARALLEL_CEILING,
    FanOutResult as TeamRunResult,
    InsufficientSurvivorsError,
    MIN_SURVIVING_STEPS as MIN_SURVIVING_MEMBERS,
    StepRunResult as MemberRunResult,
    WorkflowStep,
    _compute_max_parallel,  # re-exported: test_team_runtime imports it directly
    fan_out as run_team,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_PARALLEL_CEILING",
    "MIN_SURVIVING_MEMBERS",
    "InsufficientSurvivorsError",
    "MemberRunResult",
    "TeamRunResult",
    "WorkflowStep",
    "run_team",
]
