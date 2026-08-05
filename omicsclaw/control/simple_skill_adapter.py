"""Surface-neutral composition for one canonical simple Skill demo Run."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from .models import RunAcceptanceStatus
from .run_contract import RunScope
from .run_runtime import (
    RunAdmissionError,
    RunRuntime,
    RunTerminalProjectionIntegrityError,
    RunTerminalResultUnavailable,
    RunTerminalWaitBackpressure,
    SimpleSkillRunTerminalResult,
)


async def execute_simple_skill_demo(
    skill: str,
    *,
    run_runtime: RunRuntime,
    submission_id_factory: Callable[[], str] | None = None,
    scope: RunScope | None = None,
    confirm_task_cancellation: bool = False,
) -> dict[str, Any]:
    """Submit and observe one canonical demo without a legacy fallback."""

    create_submission_id = submission_id_factory or (lambda: uuid.uuid4().hex)
    try:
        submission = await run_runtime.build_simple_skill_demo_submission(
            run_submission_id=create_submission_id(),
            skill_id=skill,
            scope=scope,
        )
        submitted = await run_runtime.submit(submission)
    except RunAdmissionError as exc:
        return build_simple_skill_demo_failure(skill, exc.code)
    except Exception:
        return build_simple_skill_demo_failure(skill, "canonical_run_unavailable")

    if submitted.acceptance_status not in {
        RunAcceptanceStatus.ACCEPTED,
        RunAcceptanceStatus.DUPLICATE,
    }:
        return build_simple_skill_demo_failure(
            skill,
            submitted.code or submitted.acceptance_status.value,
        )
    if submitted.receipt is None:
        return build_simple_skill_demo_failure(
            skill,
            "canonical_run_receipt_missing",
        )

    run_id = submitted.receipt.run_id
    try:
        outcome = await run_runtime.wait_for_terminal_result(run_id)
    except KeyboardInterrupt:
        try:
            await run_runtime.cancel(run_id)
            outcome = await run_runtime.wait_for_terminal_result(run_id)
        except Exception:
            return build_simple_skill_demo_failure(
                skill,
                "run_cancel_unconfirmed",
                run_id=run_id,
            )
    except asyncio.CancelledError as cancelled:
        cancel_task = asyncio.create_task(run_runtime.cancel(run_id))
        try:
            await asyncio.shield(cancel_task)
            if confirm_task_cancellation:
                terminal_task = asyncio.create_task(
                    run_runtime.wait_for_terminal_result(run_id)
                )
                try:
                    await asyncio.shield(terminal_task)
                except Exception:
                    pass
        except Exception:
            pass
        raise cancelled
    except RunTerminalProjectionIntegrityError as exc:
        return build_simple_skill_demo_failure(skill, exc.code, run_id=run_id)
    except RunTerminalWaitBackpressure:
        return build_simple_skill_demo_failure(
            skill,
            "wait_backpressure",
            run_id=run_id,
        )
    except RunTerminalResultUnavailable:
        return build_simple_skill_demo_failure(
            skill,
            "runtime_closed",
            run_id=run_id,
        )
    except Exception:
        return build_simple_skill_demo_failure(
            skill,
            "terminal_result_unavailable",
            run_id=run_id,
        )
    return _terminal_result(outcome)


def _terminal_result(outcome: SimpleSkillRunTerminalResult) -> dict[str, Any]:
    receipt = outcome.receipt
    duration_seconds = 0.0
    if receipt.finished_at_ms is not None:
        duration_seconds = max(
            0.0,
            (receipt.finished_at_ms - receipt.created_at_ms) / 1000.0,
        )
    output = outcome.output
    return {
        "skill": outcome.skill_id,
        "success": outcome.success,
        "exit_code": 0 if outcome.success else 1,
        "output_dir": output.output_dir if output is not None else "",
        "files": [],
        "stdout": "",
        "stderr": ""
        if outcome.success
        else str(receipt.terminal_code or receipt.status),
        "duration_seconds": duration_seconds,
        "method": None,
        "readme_path": str(output.readme_path or "") if output is not None else "",
        "replay_path": (
            str(output.replay_path or "") if output is not None else ""
        ),
        "run_id": receipt.run_id,
    }


def build_simple_skill_demo_failure(
    skill: str,
    code: str,
    *,
    run_id: str = "",
    exit_code: int = 1,
) -> dict[str, Any]:
    return {
        "skill": skill,
        "success": False,
        "exit_code": exit_code,
        "output_dir": "",
        "files": [],
        "stdout": "",
        "stderr": code,
        "duration_seconds": 0.0,
        "method": None,
        "readme_path": "",
        "replay_path": "",
        "run_id": run_id,
    }
