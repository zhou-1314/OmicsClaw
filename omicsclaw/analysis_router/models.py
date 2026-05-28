"""Contracts for routing user analysis requests.

The router is intentionally a thin layer over the capability resolver for now:
it names the next path without executing skills or autonomous code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omicsclaw.skill.capability_resolver import CapabilityDecision


class AnalysisRouteKind(str, Enum):
    """Top-level route chosen for a user request."""

    CHAT = "chat"
    EXACT_SKILL = "exact_skill"
    PARTIAL_SKILL = "partial_skill"
    NO_SKILL = "no_skill"


@dataclass(frozen=True)
class AnalysisRoute:
    """Structured routing result consumed by future execution orchestration."""

    kind: AnalysisRouteKind
    capability_decision: CapabilityDecision
    preflight_required: bool = False
    missing_params: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chosen_skill(self) -> str:
        return self.capability_decision.chosen_skill

    @property
    def confidence(self) -> float:
        return float(self.capability_decision.confidence)

    @property
    def should_search_web(self) -> bool:
        return bool(self.capability_decision.should_search_web)

    @property
    def is_chat(self) -> bool:
        return self.kind is AnalysisRouteKind.CHAT


__all__ = ["AnalysisRoute", "AnalysisRouteKind"]
