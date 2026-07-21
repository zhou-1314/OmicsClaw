"""Back-compat shim — fan-out moved to ``runtime/workflow/fan_out.py`` (ADR 0016 L1).

The team-runtime machinery is now the domain-neutral ``fan_out`` primitive in
the workflow runtime. This module re-exports the consensus-flavoured aliases
(``run_team``, ``MemberRunResult``, ``TeamRunResult``, ...) so existing
importers (``driver.py``, ``test_team_runtime.py``) keep working unchanged.

The consensus survivor policy — "a verified run needs at least two surviving
members" — lives here, in consensus land, not in the neutral L1 primitive.
``run_team`` defaults ``required_survivors`` to ``MIN_SURVIVING_MEMBERS`` so the
historical team-runtime contract is preserved, while raw ``fan_out`` callers opt
in to a minimum explicitly (or not at all).

New code should import from ``omicsclaw.runtime.workflow.fan_out`` directly.
"""

from __future__ import annotations

from typing import Any, Sequence

from omicsclaw.runtime.workflow.fan_out import (  # noqa: F401
    DEFAULT_TIMEOUT_SECONDS,
    FanOutResult as TeamRunResult,
    InsufficientSurvivorsError,
    StepRunResult as MemberRunResult,
    WorkflowStep,
    fan_out,
)

# Consensus survivor minimum: a verified A-path run requires at least two
# surviving members. This default lives in consensus land, never in the neutral
# workflow runtime — ``fan_out`` itself sets no threshold.
MIN_SURVIVING_MEMBERS = 2


async def run_team(
    steps: Sequence[WorkflowStep],
    *,
    required_survivors: int | None = MIN_SURVIVING_MEMBERS,
    **kwargs: Any,
) -> TeamRunResult:
    """Consensus-flavoured ``fan_out``: defaults to requiring ≥2 survivors.

    Back-compat wrapper so existing team-runtime callers keep the historical
    "raise ``InsufficientSurvivorsError`` when fewer than two members survive"
    behaviour. Pass ``required_survivors=None`` to opt out, or a different
    integer to change the minimum. All other arguments forward to ``fan_out``
    unchanged.
    """
    return await fan_out(steps, required_survivors=required_survivors, **kwargs)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_SURVIVING_MEMBERS",
    "InsufficientSurvivorsError",
    "MemberRunResult",
    "TeamRunResult",
    "WorkflowStep",
    "run_team",
]
