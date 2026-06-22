"""Top-level orchestration for an Autonomous Code Mini-Agent run (ADR 0032).

Ties the pieces into one ``AutonomousRunResult`` with output-shape parity to the
legacy runner:

    create workspace -> capability/envelope gate -> start sandboxed kernel ->
    tactical loop -> REPLAY gate (fresh process) -> manifest + completion report.

Acceptance requires BOTH ``ReturnAnswer`` in the live loop AND a passing replay;
a live answer with a failing replay is a failed run (no false reproducibility).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .budget import MiniAgentBudget, TerminationReason
from .code_loop import ProviderChatClient
from .contracts import (
    AutonomousAttempt,
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
    PermissionTier,
    utcnow_iso,
)
from .kernel_envelope import envelope_available
from .kernel_session import REPO_ROOT, KernelSession, KernelStartError
from .mini_agent import run_mini_agent
from .replay import validate_replay
from .runner import write_run_records
from .skill_facade import SKILL_CALLS_LOG
from .workspace import create_workspace

MINI_AGENT_VERSION = "0.1.0"

# Budget fields a caller may override via request.metadata["mini_agent_budget"].
_BUDGET_KEYS = (
    "max_steps",
    "max_consecutive_failures",
    "raw_cell_timeout_seconds",
    "skill_call_timeout_seconds",
    "max_skill_calls",
    "max_total_tokens",
    "wall_clock_seconds",
)


def run_mini_agent_request(
    request: AutonomousRunRequest,
    *,
    llm_client: Any = None,
    require_sandbox: bool | None = None,
    budget: MiniAgentBudget | None = None,
) -> AutonomousRunResult:
    """Execute one mini-agent run and return an ``AutonomousRunResult``.

    ``require_sandbox`` defaults to the tiered policy (``None`` ->
    :func:`_require_sandbox_default`, i.e. ``OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX``)
    so the sync entry point matches the async one; pass an explicit bool to force it.
    """
    if require_sandbox is None:
        require_sandbox = _require_sandbox_default()
    workspace = create_workspace(request)
    sandbox = envelope_available()

    if require_sandbox and not sandbox:
        return _finalize(
            workspace,
            request,
            status=AutonomousRunStatus.FAILED,
            error=(
                "Strict sandbox mode (OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1) requires "
                "bubblewrap, which is unavailable here. Unset it to use the cross-platform "
                "in-kernel-guard tier, or run on a Linux runtime that has bwrap."
            ),
            metadata={"engine": "mini_agent", "sandbox": False, "fail_closed": True},
            attempts=[],
        )

    # Tiered isolation (ADR 0032): full OS envelope when bubblewrap is available,
    # otherwise the in-kernel guard (no-network + workspace-confined writes).
    process_guard = not sandbox
    budget = (budget or _budget_from_request(request)).clamped()
    llm = llm_client or ProviderChatClient(
        model=request.model_override, provider=request.provider_override
    )
    input_paths = [str(p) for p in request.input_paths] + [str(p) for p in request.upstream_paths]

    session = KernelSession(
        workspace_root=workspace.root,
        read_roots=input_paths,
        sandbox=sandbox,
        repo_root=REPO_ROOT,
        startup_timeout=90,
    )
    try:
        session.start()
    except KernelStartError as exc:
        return _finalize(
            workspace,
            request,
            status=AutonomousRunStatus.FAILED,
            error=f"kernel failed to start: {exc}",
            metadata={"engine": "mini_agent", "sandbox": sandbox},
            attempts=[],
        )

    try:
        outcome = run_mini_agent(
            session=session,
            llm=llm,
            goal=request.goal,
            workspace_root=workspace.root,
            input_paths=input_paths,
            data_schema=request.data_schema,
            analysis_plan=request.analysis_plan,
            budget=budget,
            process_guard=process_guard,
        )
    finally:
        session.shutdown()

    replay_ok = False
    replay_error = ""
    replay_script = ""
    if outcome.succeeded and outcome.accepted_cells:
        replay = validate_replay(
            workspace=workspace.root,
            accepted_cells=outcome.accepted_cells,
            input_paths=input_paths,
            budget=budget,
            sandbox=sandbox,
            process_guard=process_guard,
            repo_root=REPO_ROOT,
        )
        replay_ok = replay.ok
        replay_error = replay.error
        replay_script = replay.script_path

    accepted = outcome.succeeded and replay_ok
    status = AutonomousRunStatus.SUCCEEDED if accepted else AutonomousRunStatus.FAILED
    error = "" if accepted else _failure_message(outcome, replay_ok, replay_error)

    skill_calls = _read_jsonl(workspace.root / SKILL_CALLS_LOG)
    metadata = {
        "engine": "mini_agent",
        "mini_agent_version": MINI_AGENT_VERSION,
        "sandbox": sandbox,
        "isolation": "os_sandbox" if sandbox else "process_guard",
        "termination": outcome.termination.value,
        "answer": outcome.answer,
        "steps": [step.to_dict() for step in outcome.steps],
        "ledger": outcome.ledger,
        "replay_ok": replay_ok,
        "replay_error": replay_error,
        "replay_script": replay_script,
        "skill_calls": skill_calls,
        "computed_results": _computed_results(outcome, skill_calls, replay_ok),
        "interpretive_notes": outcome.answer,
    }

    return _finalize(
        workspace,
        request,
        status=status,
        error=error,
        metadata=metadata,
        attempts=_attempts_from_steps(outcome),
    )


async def run_mini_agent_request_async(
    request: AutonomousRunRequest,
    *,
    llm_client: Any = None,
    require_sandbox: bool | None = None,
    budget: MiniAgentBudget | None = None,
) -> AutonomousRunResult:
    """Async wrapper: the kernel ops are blocking, so run in a worker thread."""
    import asyncio

    if require_sandbox is None:
        require_sandbox = _require_sandbox_default()
    return await asyncio.to_thread(
        run_mini_agent_request,
        request,
        llm_client=llm_client,
        require_sandbox=require_sandbox,
        budget=budget,
    )


def refused_result(request: AutonomousRunRequest, diagnostic: str) -> AutonomousRunResult:
    """Build a clean FAILED result when the capability gate refuses the route.

    Used by the dispatch when the pre-flight capability probe finds the model
    cannot drive the contract — no kernel is started. Keeps output-shape parity
    so Surfaces render it like any run.
    """
    workspace = create_workspace(request)
    return _finalize(
        workspace,
        request,
        status=AutonomousRunStatus.FAILED,
        error=diagnostic or "mini-agent refused: model not capable of the code contract.",
        metadata={"engine": "mini_agent", "refused": True, "capability": "incapable"},
        attempts=[],
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _require_sandbox_default() -> bool:
    # Default is tiered (degrade to the in-kernel guard when no bwrap). Strict
    # OS-sandbox-only mode is opt-in via OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1.
    return os.getenv("OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX", "0").strip().lower() in {"1", "true", "yes", "on"}


def _budget_from_request(request: AutonomousRunRequest) -> MiniAgentBudget:
    overrides = request.metadata.get("mini_agent_budget") if isinstance(request.metadata, dict) else None
    base = MiniAgentBudget()
    if isinstance(overrides, dict):
        return base.with_overrides(**{k: overrides[k] for k in _BUDGET_KEYS if k in overrides})
    return base


def _attempts_from_steps(outcome) -> list[AutonomousAttempt]:
    attempts: list[AutonomousAttempt] = []
    for step in outcome.steps:
        attempts.append(
            AutonomousAttempt(
                attempt_index=step.index,
                argv=["<mini-agent cell>"],
                permission_tier=PermissionTier.ANALYSIS_WRITE,
                status=AutonomousRunStatus.SUCCEEDED if step.accepted else AutonomousRunStatus.FAILED,
                started_at=utcnow_iso(),
                finished_at=utcnow_iso(),
                exit_code=0 if step.accepted else 1,
                error=step.error,
            )
        )
    return attempts


def _failure_message(outcome, replay_ok: bool, replay_error: str) -> str:
    if outcome.termination == TerminationReason.MODEL_INCAPABLE:
        return (
            "the active model could not drive the mini-agent code contract (no valid "
            "Purpose/Reasoning/Next Goal/Code step within the warm-up window); use a "
            "stronger model or run the analysis through a built-in skill."
        )
    if outcome.termination != TerminationReason.RETURNED_ANSWER:
        return f"mini-agent stopped without an answer ({outcome.termination.value})."
    if not outcome.accepted_cells:
        return "mini-agent returned an answer but produced no accepted code to reproduce."
    if not replay_ok:
        return f"replay validation failed (result not reproducible): {replay_error}"
    return "mini-agent run did not complete successfully."


def _computed_results(outcome, skill_calls: list, replay_ok: bool) -> str:
    lines = [
        f"- Steps: {len(outcome.steps)} ({sum(1 for s in outcome.steps if s.accepted)} accepted)",
        f"- Nested skill calls: {len(skill_calls)}"
        + (": " + ", ".join(c.get("skill", "?") for c in skill_calls) if skill_calls else ""),
        f"- Replay validation: {'passed' if replay_ok else 'failed'}",
        f"- Termination: {outcome.termination.value}",
    ]
    return "\n".join(lines)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _finalize(
    workspace,
    request: AutonomousRunRequest,
    *,
    status: AutonomousRunStatus,
    error: str,
    metadata: dict,
    attempts: list[AutonomousAttempt],
) -> AutonomousRunResult:
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=status,
        attempts=attempts,
        finished_at=utcnow_iso(),
        error=error,
        metadata=metadata,
    )
    manifest_path, completion_report_path = write_run_records(
        workspace, request=request, result=result
    )
    result.manifest_path = str(manifest_path)
    result.completion_report_path = str(completion_report_path)
    return result


__all__ = [
    "MINI_AGENT_VERSION",
    "refused_result",
    "run_mini_agent_request",
    "run_mini_agent_request_async",
]
