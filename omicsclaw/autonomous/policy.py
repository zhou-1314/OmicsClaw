"""Policy adapter for autonomous code runner commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from omicsclaw.runtime.policy.policy import (
    TOOL_POLICY_ALLOW,
    TOOL_POLICY_DENY,
    TOOL_POLICY_REQUIRE_APPROVAL,
    ToolPolicyDecision,
    evaluate_tool_policy,
)
from omicsclaw.runtime.policy.state import ToolPolicyState
from omicsclaw.runtime.tools.orchestration import ToolExecutionRequest, ToolExecutionResult
from omicsclaw.runtime.tools.spec import (
    APPROVAL_MODE_ASK,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_MEDIUM,
    ToolSpec,
)

from .contracts import PermissionTier


AUTONOMOUS_ANALYSIS_WRITE_TOOL = "autonomous_code_runner.analysis_write"
AUTONOMOUS_SYSTEM_MUTATION_TOOL = "autonomous_code_runner.system_mutation"


@dataclass(frozen=True, slots=True)
class AutonomousApprovalOutcome:
    """Result of routing an autonomous command through the tool policy channel."""

    allowed: bool
    request: ToolExecutionRequest
    policy_decision: ToolPolicyDecision | None
    message: str = ""


def autonomous_tool_spec(permission_tier: PermissionTier) -> ToolSpec:
    """Build the synthetic ToolSpec used for existing approval/policy plumbing."""
    if permission_tier == PermissionTier.ANALYSIS_WRITE:
        return ToolSpec(
            name=AUTONOMOUS_ANALYSIS_WRITE_TOOL,
            description=(
                "Execute generated Python or R analysis code inside an isolated "
                "Autonomous Code Runner workspace."
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
            read_only=False,
            concurrency_safe=False,
            risk_level=RISK_LEVEL_MEDIUM,
            approval_mode=APPROVAL_MODE_ASK,
            writes_workspace=True,
            touches_network=False,
            allowed_in_background=False,
            policy_tags=(
                "analysis",
                "workflow",
                "autonomous",
                "autonomous_code_runner",
                "analysis_write",
            ),
        )
    return ToolSpec(
        name=AUTONOMOUS_SYSTEM_MUTATION_TOOL,
        description=(
            "Attempt a system-mutating autonomous command such as package "
            "installation, network download, external write, service startup, "
            "or unknown binary execution."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": True},
        read_only=False,
        concurrency_safe=False,
        risk_level=RISK_LEVEL_HIGH,
        approval_mode=APPROVAL_MODE_ASK,
        writes_workspace=True,
        touches_network=True,
        allowed_in_background=False,
        policy_tags=(
            "analysis",
            "workflow",
            "autonomous",
            "autonomous_code_runner",
            "system_mutation",
        ),
    )


def build_autonomous_tool_request(
    *,
    permission_tier: PermissionTier,
    argv: list[str],
    workspace_root: str,
    attempt_index: int,
    runtime_context: Mapping[str, Any] | None = None,
) -> ToolExecutionRequest:
    """Build a synthetic request for the shared policy/approval machinery."""
    spec = autonomous_tool_spec(permission_tier)
    context = dict(runtime_context or {})
    return ToolExecutionRequest(
        call_id=f"autonomous_{uuid4().hex[:12]}",
        name=spec.name,
        arguments={
            "argv": list(argv),
            "workspace_root": workspace_root,
            "attempt_index": attempt_index,
            "permission_tier": permission_tier.value,
        },
        spec=spec,
        executor=None,
        runtime_context=context,
        policy_decision=evaluate_tool_policy(
            spec.name,
            spec,
            runtime_context=context,
        ),
    )


def normalize_approval_resolution(
    raw: Any,
    *,
    request: ToolExecutionRequest,
    fallback_surface: str = "",
) -> tuple[str, ToolPolicyState | None, str]:
    """Normalize the existing Surface approval callback response shape."""
    if raw is None:
        return ("deny", None, "")
    if isinstance(raw, str):
        behavior = raw.strip().lower()
        if behavior in {"allow", "deny"}:
            return (behavior, None, "")
        return ("deny", None, raw.strip())
    if not isinstance(raw, Mapping):
        return ("deny", None, "")

    behavior = str(raw.get("behavior") or raw.get("decision") or "").strip().lower()
    if behavior not in {"allow", "deny"}:
        behavior = "deny"

    policy_state = None
    if raw.get("policy_state") is not None:
        policy_state = ToolPolicyState.from_mapping(
            raw.get("policy_state"),
            surface=str(
                (request.runtime_context or {}).get("surface")
                or fallback_surface
                or ""
            ).strip(),
        )
    message = str(raw.get("message", "") or "").strip()
    return (behavior, policy_state, message)


async def request_autonomous_approval(
    *,
    permission_tier: PermissionTier,
    argv: list[str],
    workspace_root: str,
    attempt_index: int,
    request_tool_approval: Any = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> AutonomousApprovalOutcome:
    """Evaluate and, when needed, ask approval for an autonomous command."""
    request = build_autonomous_tool_request(
        permission_tier=permission_tier,
        argv=argv,
        workspace_root=workspace_root,
        attempt_index=attempt_index,
        runtime_context=runtime_context,
    )
    decision = request.policy_decision
    if decision is None:
        return AutonomousApprovalOutcome(
            allowed=False,
            request=request,
            policy_decision=None,
            message="Autonomous command has no policy decision.",
        )
    if decision.action == TOOL_POLICY_ALLOW:
        return AutonomousApprovalOutcome(
            allowed=True,
            request=request,
            policy_decision=decision,
        )
    if decision.action == TOOL_POLICY_DENY:
        return AutonomousApprovalOutcome(
            allowed=False,
            request=request,
            policy_decision=decision,
            message=decision.reason,
        )
    if decision.action != TOOL_POLICY_REQUIRE_APPROVAL or request_tool_approval is None:
        return AutonomousApprovalOutcome(
            allowed=False,
            request=request,
            policy_decision=decision,
            message=decision.reason,
        )

    blocked_result = ToolExecutionResult(
        request=request,
        output=decision.reason,
        success=False,
        status="policy_blocked",
        policy_decision=decision,
    )
    raw_resolution = request_tool_approval(request, blocked_result)
    if hasattr(raw_resolution, "__await__"):
        raw_resolution = await raw_resolution
    behavior, policy_state, message = normalize_approval_resolution(
        raw_resolution,
        request=request,
        fallback_surface=str((runtime_context or {}).get("surface", "") or ""),
    )
    if behavior != "allow":
        return AutonomousApprovalOutcome(
            allowed=False,
            request=request,
            policy_decision=decision,
            message=message or "Autonomous command approval was denied.",
        )

    approved_context = dict(runtime_context or {})
    if policy_state is None:
        base_state = ToolPolicyState.from_mapping(
            approved_context.get("policy_state"),
            surface=str(approved_context.get("surface", "") or ""),
        )
        policy_state = ToolPolicyState(
            surface=base_state.surface,
            trusted=base_state.trusted,
            background=base_state.background,
            auto_approve_ask=base_state.auto_approve_ask,
            approved_tool_names=(
                base_state.approved_tool_names | frozenset({request.name})
            ),
        )
    approved_context["policy_state"] = policy_state
    approved_decision = evaluate_tool_policy(
        request.name,
        request.spec,
        runtime_context=approved_context,
    )
    return AutonomousApprovalOutcome(
        allowed=bool(approved_decision and approved_decision.allows_execution),
        request=request,
        policy_decision=approved_decision,
        message="" if approved_decision and approved_decision.allows_execution else decision.reason,
    )
