from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from omicsclaw.surfaces.cli import _canonical_run_support as canonical
from omicsclaw.control import ProjectScope, RunAcceptanceStatus, UnassignedScope
from omicsclaw.surfaces.cli._skill_run_support import (
    SkillRunCommandArgs,
    SkillRunRouteKind,
    classify_root_skill_run_tokens,
)


ROOT = Path(__file__).resolve().parent.parent


def _load_omicsclaw_script():
    spec = importlib.util.spec_from_file_location(
        "omicsclaw_main_root_canonical_run_test",
        ROOT / "omicsclaw.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(skill: str, *, success: bool, code: str = "") -> dict[str, object]:
    return {
        "skill": skill,
        "success": success,
        "exit_code": 0 if success else 1,
        "output_dir": "/tmp/canonical-output" if success else "",
        "files": [],
        "stdout": "",
        "stderr": code,
        "duration_seconds": 0.1,
        "method": None,
        "readme_path": "",
        "notebook_path": "",
        "run_id": "a" * 32,
    }


@pytest.mark.parametrize(
    ("tokens", "kind", "code"),
    [
        (
            ["genomics-vcf-operations", "--demo"],
            SkillRunRouteKind.CANONICAL_DEMO,
            "",
        ),
        (
            ["genomics-vcf-operations", "--input", "demo.vcf"],
            SkillRunRouteKind.LEGACY,
            "",
        ),
        (
            ["genomics-vcf-operations", "--demo", "--method", "x"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--demo=true"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--dem"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--de"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--d"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--dem=true"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["genomics-vcf-operations", "--de=true"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["--demo", "genomics-vcf-operations"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
        (
            ["--demo", "--demo"],
            SkillRunRouteKind.REJECT,
            "canonical_demo_options_not_supported",
        ),
    ],
)
def test_root_token_classifier_owns_every_demo_shape(
    tokens: list[str],
    kind: SkillRunRouteKind,
    code: str,
) -> None:
    route = classify_root_skill_run_tokens(tokens)
    assert route.kind is kind
    assert route.code == code


def test_root_token_classifier_freezes_each_supported_scope_request() -> None:
    project_id = "a" * 32

    current = classify_root_skill_run_tokens(
        ["genomics-vcf-operations", "--demo"]
    )
    project = classify_root_skill_run_tokens(
        [
            "genomics-vcf-operations",
            "--demo",
            "--project",
            project_id,
        ]
    )
    unassigned = classify_root_skill_run_tokens(
        ["genomics-vcf-operations", "--demo", "--no-project"]
    )

    assert current.kind is SkillRunRouteKind.CANONICAL_DEMO
    assert current.explicit_scope is None
    assert project.kind is SkillRunRouteKind.CANONICAL_DEMO
    assert project.explicit_scope == ProjectScope(project_id)
    assert unassigned.kind is SkillRunRouteKind.CANONICAL_DEMO
    assert isinstance(unassigned.explicit_scope, UnassignedScope)


def test_root_exact_demo_uses_canonical_adapter_and_never_legacy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oc = _load_omicsclaw_script()
    observed: dict[str, object] = {}

    def run_canonical(skill: str, *, workspace_dir: str | Path, scope):
        observed.update(
            skill=skill,
            workspace_dir=Path(workspace_dir),
            scope=scope,
        )
        return _result(skill, success=True)

    monkeypatch.setattr(canonical, "run_root_canonical_demo", run_canonical)
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("canonical request reached legacy runner"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["omicsclaw.py", "run", "genomics-vcf-operations", "--demo"],
    )

    oc.main()

    assert observed == {
        "skill": "genomics-vcf-operations",
        "workspace_dir": ROOT,
        "scope": None,
    }
    output = capsys.readouterr()
    assert "Success: genomics-vcf-operations" in output.out
    assert "/tmp/canonical-output" in output.out
    assert output.err == ""


def test_installed_launcher_reaches_the_same_root_canonical_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from omicsclaw.skill import runner as skill_runner
    from omicsclaw.surfaces.cli import launcher

    observed: list[tuple[str, Path, object]] = []

    def run_canonical(skill: str, *, workspace_dir: str | Path, scope):
        observed.append((skill, Path(workspace_dir), scope))
        return _result(skill, success=True)

    monkeypatch.setattr(launcher, "_discover_cli_path", lambda: ROOT / "omicsclaw.py")
    monkeypatch.setattr(canonical, "run_root_canonical_demo", run_canonical)
    monkeypatch.setattr(
        skill_runner,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("launcher reached legacy runner"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["oc", "run", "genomics-vcf-operations", "--demo"],
    )

    launcher.main()

    assert observed == [("genomics-vcf-operations", ROOT, None)]
    assert "Success: genomics-vcf-operations" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("scope_tokens", "expected_scope"),
    [
        (["--project", "b" * 32], ProjectScope("b" * 32)),
        (["--no-project"], UnassignedScope()),
    ],
)
def test_root_exact_explicit_scope_uses_typed_canonical_adapter_and_never_legacy(
    scope_tokens: list[str],
    expected_scope: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oc = _load_omicsclaw_script()
    observed: list[object] = []

    def run_canonical(
        skill: str,
        *,
        workspace_dir: str | Path,
        scope,
    ):
        assert skill == "genomics-vcf-operations"
        assert Path(workspace_dir) == ROOT
        observed.append(scope)
        return _result(skill, success=True)

    monkeypatch.setattr(canonical, "run_root_canonical_demo", run_canonical)
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("explicit Scope reached legacy runner"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            *scope_tokens,
        ],
    )

    oc.main()

    assert observed == [expected_scope]


def test_root_canonical_admission_failure_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oc = _load_omicsclaw_script()

    monkeypatch.setattr(
        canonical,
        "run_root_canonical_demo",
        lambda skill, **_kwargs: _result(
            skill,
            success=False,
            code="skill_not_canonical",
        ),
    )
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("admission failure reached legacy runner"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["omicsclaw.py", "run", "vcf-operations", "--demo"],
    )

    with pytest.raises(SystemExit) as excinfo:
        oc.main()

    assert excinfo.value.code == 1
    output = capsys.readouterr()
    assert "Failed: vcf-operations" in output.err
    assert "skill_not_canonical" in output.err


@pytest.mark.parametrize(
    "argv",
    [
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--method",
            "bcftools",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--future-option",
            "value",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo=true",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--dem",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--de",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--d",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--dem=true",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--de=true",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--output",
            "/tmp/not-authoritative",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--session",
            "legacy-session",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--demo",
        ],
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--demo",
            "--help",
        ],
    ],
)
def test_root_option_bearing_demo_fails_closed_before_any_executor(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oc = _load_omicsclaw_script()
    monkeypatch.setattr(
        canonical,
        "run_root_canonical_demo",
        lambda *_args, **_kwargs: pytest.fail("rejected request opened Runtime"),
    )
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("rejected request reached legacy runner"),
    )
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as excinfo:
        oc.main()

    assert excinfo.value.code == 2
    assert "canonical_demo_options_not_supported" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "default"],
            "invalid_project_id",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "A" * 32],
            "invalid_project_id",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "a" * 31],
            "invalid_project_id",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "a" * 33],
            "invalid_project_id",
        ),
        (
            [
                "omicsclaw.py",
                "run",
                "skill",
                "--demo",
                "--project",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ],
            "invalid_project_id",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "../study"],
            "invalid_project_id",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project=" + "a" * 32],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--project", "a" * 32, "--demo"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--no-project", "--demo"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--no-project=true"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--proj", "a" * 32],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--no-proj"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--no-project", "--no-project"],
            "canonical_demo_options_not_supported",
        ),
        (
            [
                "omicsclaw.py",
                "run",
                "skill",
                "--demo",
                "--project",
                "a" * 32,
                "--project",
                "a" * 32,
            ],
            "canonical_demo_options_not_supported",
        ),
        (
            [
                "omicsclaw.py",
                "run",
                "skill",
                "--demo",
                "--project",
                "a" * 32,
                "--no-project",
            ],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--", "--demo"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--"],
            "canonical_demo_options_not_supported",
        ),
        (
            ["omicsclaw.py", "run", "skill", "--demo", "--project", "--"],
            "canonical_demo_options_not_supported",
        ),
    ],
)
def test_root_malformed_scope_wire_fails_before_runtime_or_legacy(
    argv: list[str],
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oc = _load_omicsclaw_script()
    monkeypatch.setattr(
        canonical,
        "run_root_canonical_demo",
        lambda *_args, **_kwargs: pytest.fail("malformed Scope opened Runtime"),
    )
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("malformed Scope reached legacy runner"),
    )
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as excinfo:
        oc.main()

    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    assert expected_code in error


@pytest.mark.parametrize(
    "argv",
    [
        [
            "omicsclaw.py",
            "--bogus",
            "run",
            "genomics-vcf-operations",
            "--demo",
        ],
        [
            "omicsclaw.py",
            "--demo",
            "run",
            "genomics-vcf-operations",
            "--demo",
        ],
    ],
)
def test_root_run_command_cannot_be_shifted_past_unknown_global_tokens(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oc = _load_omicsclaw_script()
    monkeypatch.setattr(
        canonical,
        "run_root_canonical_demo",
        lambda *_args, **_kwargs: pytest.fail("shifted command opened Runtime"),
    )
    monkeypatch.setattr(
        oc,
        "run_skill",
        lambda *_args, **_kwargs: pytest.fail("shifted command reached legacy runner"),
    )
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as excinfo:
        oc.main()

    assert excinfo.value.code == 2


def test_root_non_demo_keeps_legacy_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oc = _load_omicsclaw_script()
    captured: dict[str, object] = {}
    from omicsclaw.skill.result import build_skill_run_result

    monkeypatch.setattr(
        canonical,
        "run_root_canonical_demo",
        lambda *_args, **_kwargs: pytest.fail("non-demo request opened Runtime"),
    )

    def legacy(skill: str, **kwargs):
        captured.update(skill=skill, **kwargs)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=None,
        )

    monkeypatch.setattr(oc, "run_skill", legacy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--input",
            "demo.vcf",
            "--future-option",
            "value",
            "--no-project",
        ],
    )

    oc.main()

    assert captured["skill"] == "genomics-vcf-operations"
    assert captured["demo"] is False
    assert captured["extra_args"] == ["--future-option", "value", "--no-project"]


def test_root_non_demo_keeps_legacy_project_abbreviation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oc = _load_omicsclaw_script()
    captured: dict[str, object] = {}
    from omicsclaw.common import run_paths
    from omicsclaw.skill.result import build_skill_run_result

    def resolve_project(_output_root: Path, value: str) -> tuple[str, str]:
        assert value == "legacy-study"
        return ("legacy-project-id", "Legacy Study")

    def legacy(skill: str, **kwargs):
        captured.update(skill=skill, **kwargs)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=None,
        )

    monkeypatch.setattr(run_paths, "resolve_cli_project", resolve_project)
    monkeypatch.setattr(
        run_paths,
        "get_current_project",
        lambda _output_root: ("wrong-current", "Wrong Current"),
    )
    monkeypatch.setattr(canonical, "run_root_canonical_demo", pytest.fail)
    monkeypatch.setattr(oc, "run_skill", legacy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "omicsclaw.py",
            "run",
            "genomics-vcf-operations",
            "--input",
            "demo.vcf",
            "--proj",
            "legacy-study",
        ],
    )

    oc.main()

    assert captured["project_id"] == "legacy-project-id"
    assert captured["project_name"] == "Legacy Study"
    assert captured["extra_args"] is None


def test_root_help_documents_the_dual_canonical_scope_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oc = _load_omicsclaw_script()
    monkeypatch.setattr(sys, "argv", ["omicsclaw.py", "run", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        oc.main()

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "--demo --project PROJECT_ID" in output
    assert "--demo --no-project" in output
    assert "32 lowercase hexadecimal" in output
    assert "--project default" in output
    assert "rejected" in output


def test_root_one_shot_bundle_closes_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Bundle:
        run_runtime = object()

        async def close(self) -> None:
            events.append("close")

    async def open_bundle(workspace_dir):
        events.append(f"open:{Path(workspace_dir).name}")
        return Bundle()

    async def execute(
        command,
        *,
        run_runtime,
        scope,
        confirm_task_cancellation,
    ):
        assert run_runtime is Bundle.run_runtime
        assert isinstance(scope, UnassignedScope)
        assert confirm_task_cancellation is True
        events.append(f"execute:{command.skill}")
        return _result(command.skill, success=True)

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_bundle)
    monkeypatch.setattr(canonical, "execute_canonical_demo_run", execute)
    monkeypatch.setattr(
        canonical,
        "resolve_root_run_scope",
        lambda _bundle: UnassignedScope(),
    )

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is True
    assert events == [
        f"open:{tmp_path.name}",
        "execute:genomics-vcf-operations",
        "close",
    ]


@pytest.mark.parametrize(
    "explicit_scope",
    [ProjectScope("c" * 32), UnassignedScope()],
)
def test_root_explicit_scope_bypasses_navigation_resolution(
    explicit_scope: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[object] = []

    class Bundle:
        run_runtime = object()

        async def close(self) -> None:
            return None

    async def open_bundle(_workspace_dir):
        return Bundle()

    async def execute(_command, *, scope, **_kwargs):
        observed.append(scope)
        return _result("genomics-vcf-operations", success=True)

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_bundle)
    monkeypatch.setattr(canonical, "execute_canonical_demo_run", execute)
    monkeypatch.setattr(
        canonical,
        "resolve_root_run_scope",
        lambda _bundle: pytest.fail("explicit Scope read current navigation"),
    )

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
            scope=explicit_scope,
        )
    )

    assert result["success"] is True
    assert observed == [explicit_scope]


def test_root_one_shot_bundle_closes_before_execution_error_is_projected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Bundle:
        run_runtime = object()

        async def close(self) -> None:
            events.append("close")

    async def open_bundle(_workspace_dir):
        events.append("open")
        return Bundle()

    async def explode(_command, *, run_runtime, **_kwargs):
        assert run_runtime is Bundle.run_runtime
        events.append("execute")
        raise RuntimeError("must not cross root CLI boundary")

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_bundle)
    monkeypatch.setattr(canonical, "execute_canonical_demo_run", explode)
    monkeypatch.setattr(
        canonical,
        "resolve_root_run_scope",
        lambda _bundle: UnassignedScope(),
    )

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is False
    assert result["stderr"] == "canonical_run_unavailable"
    assert events == ["open", "execute", "close"]


def test_root_one_shot_open_failure_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_open(_workspace_dir):
        raise RuntimeError("secret internal state")

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", fail_open)

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is False
    assert result["stderr"] == "canonical_run_unavailable"
    assert "secret" not in str(result)


def test_root_one_shot_open_cleanup_unconfirmed_has_distinct_closed_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_open(_workspace_dir):
        raise canonical.CliRuntimeCloseUnconfirmed("secret owner detail")

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", fail_open)

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is False
    assert result["stderr"] == "canonical_runtime_close_unconfirmed"
    assert "secret" not in str(result)


def test_root_one_shot_close_failure_cannot_publish_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Bundle:
        run_runtime = object()

        async def close(self) -> None:
            raise RuntimeError("owner stop proof unavailable")

    async def open_bundle(_workspace_dir):
        return Bundle()

    async def execute(command, **_kwargs):
        return _result(command.skill, success=True)

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_bundle)
    monkeypatch.setattr(canonical, "execute_canonical_demo_run", execute)
    monkeypatch.setattr(
        canonical,
        "resolve_root_run_scope",
        lambda _bundle: UnassignedScope(),
    )

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is False
    assert result["stderr"] == "canonical_runtime_close_unconfirmed"
    assert "owner stop proof" not in str(result)


def test_root_close_unconfirmed_outranks_an_ordinary_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Bundle:
        run_runtime = object()
        close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("owner stop proof unavailable")

    bundle = Bundle()

    async def open_bundle(_workspace_dir):
        return bundle

    async def cancel_execution(_command, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_bundle)
    monkeypatch.setattr(canonical, "execute_canonical_demo_run", cancel_execution)
    monkeypatch.setattr(
        canonical,
        "resolve_root_run_scope",
        lambda _bundle: UnassignedScope(),
    )

    result = asyncio.run(
        canonical.execute_root_canonical_demo_run(
            "genomics-vcf-operations",
            workspace_dir=tmp_path,
        )
    )

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["stderr"] == "canonical_runtime_close_unconfirmed"
    assert bundle.close_calls == 2


def test_root_task_cancellation_requests_cancel_observes_terminal_then_propagates() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.waits = 0

        async def build_simple_skill_demo_submission(self, **_kwargs):
            self.events.append("build")
            return object()

        async def submit(self, _submission):
            self.events.append("submit")
            return SimpleNamespace(
                acceptance_status=RunAcceptanceStatus.ACCEPTED,
                receipt=SimpleNamespace(run_id="a" * 32),
                code="",
            )

        async def wait_for_terminal_result(self, _run_id):
            self.events.append("wait")
            self.waits += 1
            if self.waits == 1:
                raise asyncio.CancelledError
            return object()

        async def cancel(self, _run_id):
            self.events.append("cancel")

    async def scenario() -> None:
        runtime = Runtime()
        with pytest.raises(asyncio.CancelledError):
            await canonical.execute_canonical_demo_run(
                SkillRunCommandArgs(
                    "genomics-vcf-operations",
                    demo=True,
                ),
                run_runtime=runtime,  # type: ignore[arg-type]
                confirm_task_cancellation=True,
            )
        assert runtime.events == ["build", "submit", "wait", "cancel", "wait"]

    asyncio.run(scenario())


def test_root_sync_boundary_projects_keyboard_interrupt_as_exit_130(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        canonical.asyncio,
        "run",
        lambda _awaitable: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    result = canonical.run_root_canonical_demo(
        "genomics-vcf-operations",
        workspace_dir=tmp_path,
    )

    assert result["success"] is False
    assert result["exit_code"] == 130
    assert result["stderr"] == "canonical_run_interrupted"


def test_root_scope_passes_only_an_opaque_navigation_hint_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_id = "a" * 32
    expected = ProjectScope(project_id)
    calls: list[str | None] = []

    class Runtime:
        def resolve_cli_navigation_scope(self, project_hint: str | None):
            calls.append(project_hint)
            return expected

    bundle = SimpleNamespace(
        run_config=SimpleNamespace(output_root=tmp_path / "output"),
        run_runtime=Runtime(),
        control_runtime=SimpleNamespace(
            repository=pytest.fail,
        ),
    )
    monkeypatch.setattr(
        canonical,
        "peek_current_project",
        lambda _root: (project_id, "Display Name"),
    )

    scope = canonical.resolve_root_run_scope(bundle)

    assert scope is expected
    assert calls == [project_id]


def test_root_scope_passes_missing_navigation_as_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str | None] = []

    class Runtime:
        def resolve_cli_navigation_scope(self, project_hint: str | None):
            calls.append(project_hint)
            return UnassignedScope()

    bundle = SimpleNamespace(
        run_config=SimpleNamespace(output_root=tmp_path / "output"),
        run_runtime=Runtime(),
    )
    monkeypatch.setattr(
        canonical,
        "peek_current_project",
        lambda _root: ("", ""),
    )

    assert isinstance(canonical.resolve_root_run_scope(bundle), UnassignedScope)
    assert calls == [None]


def test_root_scope_does_not_downgrade_runtime_failure_to_unassigned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Runtime:
        def resolve_cli_navigation_scope(self, _project_hint: str | None):
            raise RuntimeError("control state unavailable")

    bundle = SimpleNamespace(
        run_config=SimpleNamespace(output_root=tmp_path / "output"),
        run_runtime=Runtime(),
    )
    monkeypatch.setattr(
        canonical,
        "peek_current_project",
        lambda _root: ("a" * 32, "Current"),
    )

    with pytest.raises(RuntimeError, match="control state unavailable"):
        canonical.resolve_root_run_scope(bundle)
