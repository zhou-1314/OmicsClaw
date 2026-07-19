"""Route natural-language requests into chat, skill, or autonomous paths."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from omicsclaw.skill.capability_resolver import (
    CapabilityDecision,
    resolve_capability,
)
from omicsclaw.skill.preconditions import InputProfile
from omicsclaw.skill.skill_dag import candidate_plan_digest

from .models import AnalysisRoute, AnalysisRouteKind

_HELP_OR_META_HINTS = (
    "help",
    "usage",
    "install",
    "installation",
    "version",
    "what is omicsclaw",
    "how do i use",
    "怎么用",
    "帮助",
    "安装",
    "版本",
)

_NON_ANALYSIS_REASON_HINTS = (
    "empty request",
    "does not look like an omics analysis task",
    "non-analysis",
)


CapabilityResolver = Callable[..., CapabilityDecision]


class AnalysisRouter:
    """Small wrapper around capability resolution.

    The router does not execute anything. It only preserves the resolver's
    decision and adds a route kind that can include non-analysis chat.
    """

    def __init__(self, resolver: CapabilityResolver = resolve_capability) -> None:
        self._resolver = resolver

    def route(
        self,
        query: str,
        file_path: str = "",
        domain_hint: str = "",
        input_profile: InputProfile | dict | None = None,
        method_bindings: Mapping[str, str] | None = None,
    ) -> AnalysisRoute:
        resolver_kwargs = {
            "file_path": file_path,
            "domain_hint": domain_hint,
        }
        if input_profile is not None:
            resolver_kwargs["input_profile"] = input_profile
        if method_bindings:
            resolver_kwargs["method_bindings"] = dict(method_bindings)
        decision = self._resolver(query, **resolver_kwargs)
        kind = self._route_kind(query=query, file_path=file_path, decision=decision)
        preflight_required = bool(
            decision.precondition_evaluated and not decision.execution_ready
        )
        candidate_chain = decision.candidate_chain
        candidate_plan_execution_ready = bool(
            candidate_chain
            and candidate_chain.get("validated_order") is True
            and candidate_chain.get("resource_ready") is True
        )
        return AnalysisRoute(
            kind=kind,
            capability_decision=decision,
            preflight_required=preflight_required,
            missing_params=(
                list(decision.missing_preconditions) if preflight_required else []
            ),
            metadata=(
                {
                    "candidate_chain": candidate_chain,
                    "requires_confirmation": candidate_plan_execution_ready,
                    "plan_digest": candidate_plan_digest(candidate_chain),
                }
                if candidate_chain
                else {}
            ),
        )

    def _route_kind(
        self,
        *,
        query: str,
        file_path: str,
        decision: CapabilityDecision,
    ) -> AnalysisRouteKind:
        if self._should_route_to_chat(query=query, file_path=file_path, decision=decision):
            return AnalysisRouteKind.CHAT

        try:
            return AnalysisRouteKind(str(decision.coverage))
        except ValueError:
            return AnalysisRouteKind.NO_SKILL

    @staticmethod
    def _should_route_to_chat(
        *,
        query: str,
        file_path: str,
        decision: CapabilityDecision,
    ) -> bool:
        if decision.chosen_skill:
            return False

        query_lower = (query or "").strip().lower()
        if not query_lower and not file_path:
            return True

        if not file_path and any(hint in query_lower for hint in _HELP_OR_META_HINTS):
            return True

        reasoning = " ".join(decision.reasoning).lower()
        return any(hint in reasoning for hint in _NON_ANALYSIS_REASON_HINTS)


def route_analysis_request(
    query: str,
    file_path: str = "",
    domain_hint: str = "",
    input_profile: InputProfile | dict | None = None,
    method_bindings: Mapping[str, str] | None = None,
) -> AnalysisRoute:
    """Convenience function for callers that do not need a router instance."""

    return AnalysisRouter().route(
        query,
        file_path=file_path,
        domain_hint=domain_hint,
        input_profile=input_profile,
        method_bindings=method_bindings,
    )


__all__ = ["AnalysisRouter", "route_analysis_request"]
