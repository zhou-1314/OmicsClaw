from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from omicsclaw.autonomous import (
    AUTONOMOUS_CODE_RUNNER_SOURCE,
    AUTONOMOUS_WORKSPACE_PURPOSE,
    WORKSPACE_SUBDIRS,
    AutonomousRunRequest,
    AutonomousRunStatus,
    PermissionTier,
    AUTONOMOUS_ANALYSIS_WRITE_TOOL,
    classify_command,
    create_workspace,
    execute_command,
    run_autonomous_code_loop,
    run_commands,
    run_commands_with_approval,
)


def test_create_workspace_uses_autonomous_shape(tmp_path: Path) -> None:
    request = AutonomousRunRequest(
        goal="summarize a dataset",
        output_root=tmp_path,
        input_paths=["/data/input.h5ad"],
        upstream_paths=["/runs/skill-output"],
        run_id="abc123",
    )

    workspace = create_workspace(request)

    assert workspace.root.parent == tmp_path
    assert workspace.root.name.startswith("autonomous-code__")
    assert workspace.root.name.endswith("__abc123")
    for name in WORKSPACE_SUBDIRS:
        assert (workspace.root / name).is_dir()
    assert json.loads((workspace.inputs_dir / "references.json").read_text()) == {
        "references": ["/data/input.h5ad"]
    }
    assert json.loads((workspace.upstream_dir / "references.json").read_text()) == {
        "references": ["/runs/skill-output"]
    }


def test_permission_classifier_tiers(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "main.py"
    script.parent.mkdir()
    script.write_text("print('ok')\n", encoding="utf-8")

    assert (
        classify_command([sys.executable, "-c", "print('schema')"], workspace_root=tmp_path)
        == PermissionTier.READ_ONLY_PROBE
    )
    assert (
        classify_command(
            [sys.executable, "-c", "open('x.txt', 'w').write('bad')"],
            workspace_root=tmp_path,
        )
        == PermissionTier.SYSTEM_MUTATION
    )
    assert classify_command(["ls", "scripts"], workspace_root=tmp_path) == PermissionTier.READ_ONLY_PROBE
    assert (
        classify_command([sys.executable, str(script)], workspace_root=tmp_path)
        == PermissionTier.ANALYSIS_WRITE
    )
    assert classify_command(["ls", "/etc"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["head", "../secret.txt"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert (
        classify_command(
            [sys.executable, "scripts/main.py", "--output", "../escape"],
            workspace_root=tmp_path,
        )
        == PermissionTier.SYSTEM_MUTATION
    )
    assert classify_command(["pip", "install", "x"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["curl", "https://example.com"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["git", "clone", "repo"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["sudo", "true"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["sed", "-i", "s/a/b/", "x.txt"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert classify_command(["rm", "-rf", "/tmp/x"], workspace_root=tmp_path) == PermissionTier.SYSTEM_MUTATION
    assert (
        classify_command([sys.executable, "/tmp/outside.py"], workspace_root=tmp_path)
        == PermissionTier.SYSTEM_MUTATION
    )
    assert (
        classify_command([sys.executable, "../outside.py"], workspace_root=tmp_path)
        == PermissionTier.SYSTEM_MUTATION
    )


def test_execute_command_writes_success_logs(tmp_path: Path) -> None:
    workspace = create_workspace(AutonomousRunRequest(goal="run code", output_root=tmp_path))

    attempt = execute_command(
        workspace,
        [sys.executable, "-c", "print('hello autonomous')"],
        attempt_index=0,
        timeout_seconds=10,
    )

    assert attempt.status == AutonomousRunStatus.SUCCEEDED
    assert attempt.exit_code == 0
    assert Path(attempt.stdout_log).read_text(encoding="utf-8").strip() == "hello autonomous"
    assert Path(attempt.stderr_log).read_text(encoding="utf-8") == ""


def test_execute_command_writes_failure_logs(tmp_path: Path) -> None:
    workspace = create_workspace(AutonomousRunRequest(goal="run bad code", output_root=tmp_path))

    attempt = execute_command(
        workspace,
        [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"],
        attempt_index=1,
        timeout_seconds=10,
    )

    assert attempt.status == AutonomousRunStatus.FAILED
    assert attempt.exit_code == 7
    assert Path(attempt.stdout_log).read_text(encoding="utf-8") == ""
    assert "bad" in Path(attempt.stderr_log).read_text(encoding="utf-8")


def test_execute_command_refuses_analysis_write_until_approval_path_exists(tmp_path: Path) -> None:
    workspace = create_workspace(AutonomousRunRequest(goal="run code", output_root=tmp_path))
    script = workspace.scripts_dir / "main.py"
    script.write_text("print('would run later')\n", encoding="utf-8")

    attempt = execute_command(
        workspace,
        [sys.executable, str(script)],
        attempt_index=0,
        timeout_seconds=10,
    )

    assert attempt.status == AutonomousRunStatus.FAILED
    assert attempt.permission_tier == PermissionTier.ANALYSIS_WRITE
    assert "analysis_write approval" in Path(attempt.stderr_log).read_text(encoding="utf-8")


def test_run_commands_writes_manifest_and_completion_report(tmp_path: Path) -> None:
    result = run_commands(
        AutonomousRunRequest(
            goal="produce logs",
            output_root=tmp_path,
            input_paths=["input.csv"],
            upstream_paths=["upstream/run"],
        ),
        [[sys.executable, "-c", "print('done')"]],
    )

    workspace_root = Path(result.workspace_root)
    manifest_path = workspace_root / "manifest.json"
    completion_path = workspace_root / "completion_report.json"
    summary_path = workspace_root / "result_summary.md"
    assert result.ok is True
    assert result.manifest_path == str(manifest_path)
    assert result.completion_report_path == str(completion_path)
    assert manifest_path.exists()
    assert completion_path.exists()
    assert summary_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert manifest["workspace"]["purpose"] == AUTONOMOUS_WORKSPACE_PURPOSE
    assert manifest["metadata"]["source"] == AUTONOMOUS_CODE_RUNNER_SOURCE
    assert manifest["metadata"]["result_summary_path"] == str(summary_path)
    assert manifest["verification"]["status"] == "complete"
    assert completion["status"] == "complete"
    assert completion["completed"] is True
    assert completion["metadata"]["source"] == AUTONOMOUS_CODE_RUNNER_SOURCE
    assert any(
        artifact["path"] == "result_summary.md" and artifact["present"]
        for artifact in completion["artifacts"]
    )


def test_run_commands_failure_still_writes_manifest_and_completion_report(tmp_path: Path) -> None:
    result = run_commands(
        AutonomousRunRequest(goal="fail cleanly", output_root=tmp_path),
        [[sys.executable, "-c", "import sys; sys.exit(3)"]],
    )

    workspace_root = Path(result.workspace_root)
    manifest_path = workspace_root / "manifest.json"
    completion_path = workspace_root / "completion_report.json"
    summary_path = workspace_root / "result_summary.md"
    assert result.ok is False
    assert result.status == AutonomousRunStatus.FAILED
    assert manifest_path.exists()
    assert completion_path.exists()
    assert "Status: `failed`" in summary_path.read_text(encoding="utf-8")

    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert completion["status"] == "failed"
    assert completion["completed"] is False
    assert completion["errors"]


def test_run_commands_without_commands_records_failure_reason(tmp_path: Path) -> None:
    result = run_commands(
        AutonomousRunRequest(goal="nothing to execute", output_root=tmp_path),
        [],
    )

    completion_path = Path(result.completion_report_path)
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    assert result.ok is False
    assert result.error == "No commands were provided."
    assert completion["status"] == "failed"
    assert completion["errors"] == ["No commands were provided."]


def test_run_commands_preserves_timed_out_status(tmp_path: Path) -> None:
    result = run_commands(
        AutonomousRunRequest(goal="timeout", output_root=tmp_path, timeout_seconds=1),
        [[sys.executable, "-c", "import time; time.sleep(3)"]],
    )

    completion = json.loads(Path(result.completion_report_path).read_text(encoding="utf-8"))
    assert result.status == AutonomousRunStatus.TIMED_OUT
    assert result.ok is False
    assert completion["metadata"]["status"] == "timed_out"


def test_run_commands_with_approval_executes_analysis_write(tmp_path: Path) -> None:
    approvals: list[tuple[str, dict]] = []
    request = AutonomousRunRequest(goal="write a file", output_root=tmp_path)
    workspace = create_workspace(request)
    script = workspace.scripts_dir / "main.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path(AUTONOMOUS_OUTPUT_DIR, 'tables', 'ok.txt').write_text('done')\n"
        "print('wrote')\n",
        encoding="utf-8",
    )

    async def approve(tool_request, execution_result):
        approvals.append((tool_request.name, execution_result.policy_decision.to_dict()))
        return {"behavior": "allow"}

    from omicsclaw.autonomous.executor import execute_command_with_approval

    attempt = asyncio.run(
        execute_command_with_approval(
            workspace,
            [sys.executable, str(script)],
            attempt_index=0,
            request=request,
            request_tool_approval=approve,
            runtime_context={"surface": "cli"},
        )
    )

    assert attempt.status == AutonomousRunStatus.SUCCEEDED
    assert attempt.approval_required is True
    assert attempt.approval_granted is True
    assert approvals[0][0] == AUTONOMOUS_ANALYSIS_WRITE_TOOL
    assert "autonomous_code_runner" in approvals[0][1]["policy_tags"]
    assert (workspace.tables_dir / "ok.txt").read_text(encoding="utf-8") == "done"


def test_analysis_write_guard_blocks_workspace_escape_after_approval(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    request = AutonomousRunRequest(goal="bad write", output_root=tmp_path)
    workspace = create_workspace(request)
    script = workspace.scripts_dir / "bad.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(outside)!r}).write_text('bad')\n",
        encoding="utf-8",
    )

    async def approve(_request, _execution_result):
        return {"behavior": "allow"}

    from omicsclaw.autonomous.executor import execute_command_with_approval

    attempt = asyncio.run(
        execute_command_with_approval(
            workspace,
            [sys.executable, str(script)],
            attempt_index=0,
            request=request,
            request_tool_approval=approve,
            runtime_context={"surface": "cli"},
        )
    )

    assert attempt.status == AutonomousRunStatus.FAILED
    assert outside.exists() is False
    assert "Autonomous write outside workspace" in Path(attempt.stderr_log).read_text(
        encoding="utf-8"
    )


def test_run_commands_with_approval_writes_reports(tmp_path: Path) -> None:
    request = AutonomousRunRequest(goal="write report", output_root=tmp_path)
    result = asyncio.run(
        run_commands_with_approval(
            request,
            [[sys.executable, "-c", "print('probe')"]],
            runtime_context={"surface": "cli"},
        )
    )

    assert result.ok is True
    assert Path(result.manifest_path).exists()
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["metadata"]["approval_aware"] is True


class _FakeLLM:
    def __init__(self, responses: list[dict[str, str]]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.responses.pop(0))


def test_autonomous_code_loop_repairs_and_succeeds(tmp_path: Path) -> None:
    llm = _FakeLLM(
        [
            {
                "analysis_plan": "first try",
                "code": "raise RuntimeError('boom')",
                "notes": "",
            },
            {
                "analysis_plan": "repair",
                "code": (
                    "from pathlib import Path\n"
                    "Path(AUTONOMOUS_OUTPUT_DIR, 'result_computed.md').write_text('computed ok')\n"
                    "Path(AUTONOMOUS_OUTPUT_DIR, 'interpretive_notes.md').write_text('interpretation cites result_computed.md')\n"
                    "print('done')\n"
                ),
                "notes": "",
            },
        ]
    )

    async def approve(_request, _execution_result):
        return {"behavior": "allow"}

    result = run_autonomous_code_loop(
        AutonomousRunRequest(
            goal="make summary",
            output_root=tmp_path,
            max_repair_attempts=2,
        ),
        llm_client=llm,
        request_tool_approval=approve,
        runtime_context={"surface": "cli"},
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    assert "Failure evidence JSON" in llm.prompts[1]
    summary = Path(result.workspace_root, "result_summary.md").read_text(encoding="utf-8")
    assert "## Computed Results" in summary
    assert "computed ok" in summary
    assert "## Interpretive Notes" in summary


def test_autonomous_code_loop_static_validation_blocks_network_code(tmp_path: Path) -> None:
    llm = _FakeLLM(
        [
            {
                "analysis_plan": "bad",
                "code": "import requests\nprint(requests.get('https://example.com'))",
                "notes": "",
            }
        ]
    )

    result = run_autonomous_code_loop(
        AutonomousRunRequest(
            goal="bad network",
            output_root=tmp_path,
            max_repair_attempts=0,
        ),
        llm_client=llm,
    )

    assert result.ok is False
    assert result.attempts == []
    assert "blocked import: requests" in result.error


def test_prompts_omit_schema_and_plan_by_default() -> None:
    """ADR 0014 plumbing must be a safe default: with no injected schema/plan,
    the runner prompts are unchanged (no schema/plan lines leak in)."""
    from omicsclaw.autonomous.code_loop import (
        _build_initial_prompt,
        _build_repair_prompt,
    )

    request = AutonomousRunRequest(goal="do X", output_root="/tmp/x")
    initial = _build_initial_prompt(request)
    repair = _build_repair_prompt(
        request, previous_plan="p", previous_code="c", attempts=[], last_error="boom"
    )

    for prompt in (initial, repair):
        assert "Input data schema:" not in prompt
        assert "Approved analysis plan:" not in prompt
    assert "Goal: do X" in initial


def test_prompts_include_injected_schema_and_plan() -> None:
    """When the outer loop injects a data schema and plan (ADR 0014), both the
    initial and repair prompts surface them so codegen and repair use real keys."""
    from omicsclaw.autonomous.code_loop import (
        _build_initial_prompt,
        _build_repair_prompt,
    )

    request = AutonomousRunRequest(
        goal="spatial niche identification",
        output_root="/tmp/x",
        data_schema="obsm: spatial, X_pca | n_obs=53000",
        analysis_plan="1. spatial-weighted leiden\n2. plot domains",
    )

    initial = _build_initial_prompt(request)
    assert "Input data schema:" in initial and "obsm: spatial" in initial
    assert "Approved analysis plan:" in initial and "leiden" in initial
    assert "ground truth" in initial  # the schema-grounding instruction

    repair = _build_repair_prompt(
        request, previous_plan="p", previous_code="c", attempts=[], last_error="KeyError"
    )
    assert "Input data schema:" in repair and "obsm: spatial" in repair
