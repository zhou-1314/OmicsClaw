import json

from omicsclaw.surfaces.cli._skill_run_support import (
    SkillRunRouteKind,
    build_skill_run_display_view,
    build_skill_run_exception_result,
    build_skill_run_execution_view,
    classify_skill_run_route,
    parse_skill_run_command,
)


def _preflight_stderr(status: str) -> str:
    payload = {
        "kind": "preflight",
        "skill_name": "sc-preprocessing",
        "status": status,
        "confirmations": ["confirm defaults?"] if status == "needs_user_input" else [],
        "missing_requirements": ["raw counts required"] if status == "blocked" else [],
        "guidance": [],
        "pending_fields": [],
    }
    return f"USER_GUIDANCE_JSON: {json.dumps(payload)}\nWARNING: traceback-free stderr"


def test_display_view_preflight_gate_renders_friendly_not_unknown_error():
    """After the gate stopped raising a 'preflight check failed' ValueError, the
    CLI must still render a friendly summary (detected by payload status), not a
    degraded 'unknown error'."""
    confirm = build_skill_run_display_view(
        "sc-preprocessing",
        {"success": False, "stderr": _preflight_stderr("needs_user_input"), "stdout": ""},
    )
    assert confirm.error == "Preflight needs your confirmation before running."

    blocked = build_skill_run_display_view(
        "sc-preprocessing",
        {"success": False, "stderr": _preflight_stderr("blocked"), "stdout": ""},
    )
    assert blocked.error == "Preflight blocked: required data or metadata is missing."


def test_parse_skill_run_command_extracts_supported_flags():
    command = parse_skill_run_command(
        'spatial-preprocessing --demo --input data.h5ad --output ./workspace --method scanpy'
    )

    assert command is not None
    assert command.skill == "spatial-preprocessing"
    assert command.demo is True
    assert command.input_path == "data.h5ad"
    assert command.output_dir == "./workspace"
    assert command.method == "scanpy"
    assert command.extra_args == ["--method", "scanpy"]


def test_parse_skill_run_command_accepts_inline_equals_flags():
    command = parse_skill_run_command(
        "spatial-preprocessing --input=data.h5ad --output=./workspace --method=spagcn"
    )

    assert command is not None
    assert command.input_path == "data.h5ad"
    assert command.output_dir == "./workspace"
    assert command.method == "spagcn"


def test_parse_skill_run_command_rejects_missing_flag_value():
    assert parse_skill_run_command("spatial-preprocessing --method") is None


def test_skill_run_route_exact_demo_is_canonical_and_non_demo_is_legacy():
    assert (
        classify_skill_run_route("genomics-vcf-operations --demo").kind
        is SkillRunRouteKind.CANONICAL_DEMO
    )
    assert (
        classify_skill_run_route("genomics-vcf-operations --input data.vcf").kind
        is SkillRunRouteKind.LEGACY
    )


def test_skill_run_route_demo_variants_fail_closed_instead_of_falling_back():
    variants = (
        "genomics-vcf-operations --demo --input data.vcf",
        "genomics-vcf-operations --demo --output out",
        "genomics-vcf-operations --demo --method filter",
        "genomics-vcf-operations --demo --unknown value",
        "genomics-vcf-operations --demo --project " + "a" * 32,
        "genomics-vcf-operations --demo --no-project",
        "genomics-vcf-operations --demo --demo",
        "genomics-vcf-operations --demo=true",
        "genomics-vcf-operations --d",
        "'genomics-vcf-operations --demo",
    )
    for value in variants:
        route = classify_skill_run_route(value)
        assert route.kind is SkillRunRouteKind.REJECT
        assert route.code in {
            "canonical_demo_options_not_supported",
            "invalid_run_syntax",
        }


def test_build_skill_run_execution_view_success_builds_shared_summary_and_history():
    execution = build_skill_run_execution_view(
        "spatial-preprocessing --demo",
        skill="spatial-preprocessing",
        result={
            "success": True,
            "duration_seconds": 4.2,
            "output_dir": "/tmp/output",
            "method": "scanpy",
            "readme_path": "/tmp/output/README.md",
            "notebook_path": "/tmp/output/run.ipynb",
            "stdout": "analysis complete",
        },
    )

    assert execution.success is True
    assert execution.system_summary_lines == [
        "✓ Skill 'spatial-preprocessing' completed in 4.2s",
        "  Output: /tmp/output",
        "  Method: scanpy",
        "  Guide: /tmp/output/README.md",
        "  Notebook: /tmp/output/run.ipynb",
    ]
    assert execution.system_message == "\n".join(execution.system_summary_lines)
    assert execution.stdout == "analysis complete"
    assert execution.history_messages[0]["content"] == "[Ran skill] spatial-preprocessing --demo"
    assert execution.history_messages[1]["content"] == (
        "Skill 'spatial-preprocessing' completed successfully. Output: /tmp/output"
    )


def test_build_skill_run_execution_view_exception_uses_warning_summary():
    execution = build_skill_run_execution_view(
        "spatial-preprocessing --demo",
        skill="spatial-preprocessing",
        result=build_skill_run_exception_result(RuntimeError("tool bootstrap failed")),
    )

    assert execution.success is False
    assert execution.system_summary_lines == [
        "⚠ Error running skill 'spatial-preprocessing': tool bootstrap failed"
    ]
    assert execution.history_messages[1]["content"] == (
        "Skill 'spatial-preprocessing' failed: tool bootstrap failed"
    )
