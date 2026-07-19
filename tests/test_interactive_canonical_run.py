from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace
import weakref

import pytest

from omicsclaw.control import RunAcceptanceStatus, RunRecord
from omicsclaw.control.run_runtime import (
    LocalVerifiedSkillOutput,
    RunAdmissionError,
    RunSubmissionResult,
    SimpleSkillRunTerminalResult,
)
from omicsclaw.skill.resource_scheduler import ExecutionResourceBudget
from omicsclaw.surfaces.cli import _canonical_run_support as canonical
from omicsclaw.surfaces.cli import interactive
from omicsclaw.surfaces.cli._skill_run_support import SkillRunCommandArgs


RUN_ID = "a" * 32


def _receipt(
    *,
    status: str = "succeeded",
    terminal_code: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status=status,
        terminal_code=terminal_code,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1_000,
        started_at_ms=1_500,
        finished_at_ms=3_000,
        revision=3,
    )


class _SuccessfulRuntime:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def build_simple_skill_demo_submission(self, **kwargs):
        self.events.append(("build", kwargs))
        return object()

    async def submit(self, submission):
        self.events.append(("submit", submission))
        return RunSubmissionResult(
            RunAcceptanceStatus.ACCEPTED,
            _receipt(),
        )

    async def wait_for_terminal_result(self, run_id):
        self.events.append(("wait", run_id))
        return SimpleSkillRunTerminalResult(
            receipt=_receipt(),
            skill_id="genomics-vcf-operations",
            output=LocalVerifiedSkillOutput(
                output_dir="/verified/output",
                readme_path="/verified/output/README.md",
            ),
        )

    async def cancel(self, run_id):
        self.events.append(("cancel", run_id))


def test_canonical_demo_generates_one_submission_and_projects_verified_result():
    async def scenario() -> None:
        runtime = _SuccessfulRuntime()
        generated = 0

        def submission_id() -> str:
            nonlocal generated
            generated += 1
            return "c" * 32

        result = await canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=runtime,  # type: ignore[arg-type]
            submission_id_factory=submission_id,
        )

        assert generated == 1
        assert result["success"] is True
        assert result["run_id"] == RUN_ID
        assert result["output_dir"] == "/verified/output"
        assert result["readme_path"] == "/verified/output/README.md"
        assert [event[0] for event in runtime.events] == ["build", "submit", "wait"]
        assert runtime.events[0][1]["run_submission_id"] == "c" * 32

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code",
    ["skill_not_found", "skill_not_canonical", "resource_contract_missing"],
)
def test_canonical_admission_failure_is_closed_and_never_submits(code: str):
    class Runtime:
        async def build_simple_skill_demo_submission(self, **_kwargs):
            raise RunAdmissionError(code)

        async def submit(self, _submission):
            raise AssertionError("rejected preparation reached submit")

    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("alias", demo=True),
            run_runtime=Runtime(),  # type: ignore[arg-type]
        )
    )
    assert result["success"] is False
    assert result["stderr"] == code


@pytest.mark.parametrize(
    "code",
    [
        "resource_unsupported",
        "executor_isolation_unavailable",
        "control_not_ready",
        "run_backpressure",
        "admission_contention",
    ],
)
def test_canonical_submit_rejection_never_waits_or_falls_back(code: str):
    class Runtime:
        async def build_simple_skill_demo_submission(self, **_kwargs):
            return object()

        async def submit(self, _submission):
            return RunSubmissionResult(RunAcceptanceStatus.REJECTED, code=code)

        async def wait_for_terminal_result(self, _run_id):
            raise AssertionError("rejected Run reached terminal wait")

    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=Runtime(),  # type: ignore[arg-type]
        )
    )
    assert result["success"] is False
    assert result["stderr"] == code


def test_matching_duplicate_uses_same_terminal_observation_path():
    class Runtime(_SuccessfulRuntime):
        async def submit(self, submission):
            self.events.append(("submit", submission))
            return RunSubmissionResult(
                RunAcceptanceStatus.DUPLICATE,
                _receipt(),
            )

    runtime = Runtime()
    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=runtime,  # type: ignore[arg-type]
        )
    )
    assert result["success"] is True
    assert [event[0] for event in runtime.events] == ["build", "submit", "wait"]


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_runtime_exception_is_sanitized_without_leaking_message(error_type):
    class Runtime:
        async def build_simple_skill_demo_submission(self, **_kwargs):
            raise error_type("secret=/private/token")

    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=Runtime(),  # type: ignore[arg-type]
        )
    )
    assert result["stderr"] == "canonical_run_unavailable"
    assert "secret" not in repr(result)


def test_terminal_wait_exception_is_sanitized_without_leaking_message():
    class Runtime(_SuccessfulRuntime):
        async def wait_for_terminal_result(self, run_id):
            self.events.append(("wait", run_id))
            raise OSError("secret=/private/control.db")

    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=Runtime(),  # type: ignore[arg-type]
        )
    )
    assert result["stderr"] == "terminal_result_unavailable"
    assert "secret" not in repr(result)
    assert "/private" not in repr(result)


def test_submission_id_factory_exception_is_sanitized_without_leaking_message():
    class Runtime:
        async def build_simple_skill_demo_submission(self, **_kwargs):
            raise AssertionError("failed ID generation reached Runtime")

    def failed_submission_id() -> str:
        raise OSError("secret=/private/random-device")

    result = asyncio.run(
        canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=Runtime(),  # type: ignore[arg-type]
            submission_id_factory=failed_submission_id,
        )
    )
    assert result["stderr"] == "canonical_run_unavailable"
    assert "secret" not in repr(result)
    assert "/private" not in repr(result)


def test_keyboard_interrupt_requests_canonical_cancel_before_second_wait():
    class Runtime(_SuccessfulRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.waits = 0

        async def wait_for_terminal_result(self, run_id):
            self.waits += 1
            self.events.append(("wait", run_id))
            if self.waits == 1:
                raise KeyboardInterrupt
            return SimpleSkillRunTerminalResult(
                receipt=_receipt(
                    status="canceled",
                    terminal_code="canceled_by_owner",
                ),
                skill_id="genomics-vcf-operations",
                output=None,
            )

    async def scenario() -> None:
        runtime = Runtime()
        result = await canonical.execute_canonical_demo_run(
            SkillRunCommandArgs("genomics-vcf-operations", demo=True),
            run_runtime=runtime,  # type: ignore[arg-type]
        )
        assert result["success"] is False
        assert result["stderr"] == "canceled_by_owner"
        assert [event[0] for event in runtime.events] == [
            "build",
            "submit",
            "wait",
            "cancel",
            "wait",
        ]

    asyncio.run(scenario())


def test_task_cancellation_finishes_canonical_cancel_before_propagating():
    async def scenario() -> None:
        wait_started = asyncio.Event()
        cancel_started = asyncio.Event()
        release_cancel = asyncio.Event()

        class Runtime(_SuccessfulRuntime):
            async def wait_for_terminal_result(self, run_id):
                self.events.append(("wait", run_id))
                wait_started.set()
                await asyncio.Future()

            async def cancel(self, run_id):
                self.events.append(("cancel", run_id))
                cancel_started.set()
                await release_cancel.wait()
                self.events.append(("cancel_done", run_id))

        runtime = Runtime()
        execution = asyncio.create_task(
            canonical.execute_canonical_demo_run(
                SkillRunCommandArgs("genomics-vcf-operations", demo=True),
                run_runtime=runtime,  # type: ignore[arg-type]
            )
        )
        await wait_started.wait()
        execution.cancel()
        await cancel_started.wait()
        await asyncio.sleep(0)
        assert not execution.done()

        release_cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert [event[0] for event in runtime.events] == [
            "build",
            "submit",
            "wait",
            "cancel",
            "cancel_done",
        ]

    asyncio.run(scenario())


def test_canonical_handler_never_calls_legacy_runner_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed(_command, *, run_runtime):
        del run_runtime
        return canonical.build_canonical_demo_failure_result(
            "genomics-vcf-operations",
            "resource_contract_missing",
        )

    monkeypatch.setattr(interactive, "execute_canonical_demo_run", failed)
    monkeypatch.setattr(
        interactive,
        "_handle_legacy_run",
        lambda _arg: (_ for _ in ()).throw(AssertionError("legacy fallback")),
    )
    monkeypatch.setattr(interactive, "_print_skill_run_execution", lambda _view: None)

    result = asyncio.run(
        interactive._handle_run(
            "genomics-vcf-operations --demo",
            run_runtime=object(),  # type: ignore[arg-type]
        )
    )
    assert result is not None
    assert result.success is False
    assert "resource_contract_missing" in result.result_text


def test_non_demo_handler_remains_explicit_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(interactive, "_handle_legacy_run", lambda _arg: sentinel)
    result = asyncio.run(
        interactive._handle_run(
            "genomics-vcf-operations --input data.vcf",
            run_runtime=object(),  # type: ignore[arg-type]
        )
    )
    assert result is sentinel


def test_runtime_bundle_starts_control_then_run_and_closes_in_reverse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        repository = object()

        async def start(self):
            events.append("control-start")

        async def close(self):
            events.append("control-close")

    class Run:
        async def start(self):
            events.append("run-start")

        async def close(self):
            events.append("run-close")

    control = Control()
    run = Run()
    monkeypatch.setattr(
        canonical.ControlRuntime,
        "for_local_surface",
        lambda **_kwargs: control,
    )
    captured: dict[str, object] = {}

    def build_run(**kwargs):
        captured.update(kwargs)
        return run

    monkeypatch.setattr(canonical.RunRuntime, "for_local_surface", build_run)
    budget = ExecutionResourceBudget(2, 4096, (), 2, 4096, 2)
    config = canonical.CliRunRuntimeConfig(tmp_path / "output", budget, 4, 1)

    async def scenario() -> None:
        bundle = await canonical.open_cli_runtime_bundle(
            tmp_path,
            run_config=config,
        )
        assert captured["repository"] is control.repository
        assert captured["resource_budget"] is budget
        await bundle.close()
        await bundle.close()

    asyncio.run(scenario())
    assert events == ["control-start", "run-start", "run-close", "control-close"]


def test_runtime_bundle_retries_unconfirmed_run_close_before_releasing_control(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        async def close(self):
            events.append("control-close")

    class Run:
        calls = 0

        async def close(self):
            self.calls += 1
            events.append(f"run-close-{self.calls}")
            if self.calls == 1:
                raise RuntimeError("stop proof unavailable")

    bundle = canonical.CliRuntimeBundle(
        workspace_id=str(tmp_path),
        control_runtime=Control(),  # type: ignore[arg-type]
        run_runtime=Run(),  # type: ignore[arg-type]
        run_config=canonical.CliRunRuntimeConfig(
            tmp_path / "output",
            ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
            2,
            1,
        ),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="stop proof unavailable"):
            await bundle.close()
        assert events == ["run-close-1"]
        await bundle.close()
        await bundle.close()

    asyncio.run(scenario())
    assert events == ["run-close-1", "run-close-2", "control-close"]
    assert id(bundle.control_runtime) not in canonical._QUARANTINED_CLI_RUNTIME_OWNERS


def test_runtime_bundle_propagates_cancel_only_after_reverse_close(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        async def close(self):
            events.append("control-close")

    class Run:
        calls = 0

        async def close(self):
            self.calls += 1
            events.append(f"run-close-{self.calls}")
            if self.calls == 1:
                raise asyncio.CancelledError

    bundle = canonical.CliRuntimeBundle(
        workspace_id=str(tmp_path),
        control_runtime=Control(),  # type: ignore[arg-type]
        run_runtime=Run(),  # type: ignore[arg-type]
        run_config=canonical.CliRunRuntimeConfig(
            tmp_path / "output",
            ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
            2,
            1,
        ),
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await bundle.close()
        await bundle.close()

    asyncio.run(scenario())
    assert events == ["run-close-1", "run-close-2", "control-close"]


def test_runtime_bundle_preserves_interrupt_across_transient_control_close_failure(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        calls = 0

        async def close(self):
            self.calls += 1
            events.append(f"control-close-{self.calls}")
            if self.calls == 1:
                raise RuntimeError("transient Control close failure")

    class Run:
        calls = 0

        async def close(self):
            self.calls += 1
            events.append(f"run-close-{self.calls}")
            if self.calls == 1:
                raise asyncio.CancelledError

    bundle = canonical.CliRuntimeBundle(
        workspace_id=str(tmp_path),
        control_runtime=Control(),  # type: ignore[arg-type]
        run_runtime=Run(),  # type: ignore[arg-type]
        run_config=canonical.CliRunRuntimeConfig(
            tmp_path / "output",
            ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
            2,
            1,
        ),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="transient Control"):
            await bundle.close()
        with pytest.raises(asyncio.CancelledError):
            await bundle.close()
        await bundle.close()

    asyncio.run(scenario())
    assert events == [
        "run-close-1",
        "run-close-2",
        "control-close-1",
        "control-close-2",
    ]
    assert id(bundle.control_runtime) not in canonical._QUARANTINED_CLI_RUNTIME_OWNERS


def test_runtime_bundle_keeps_control_when_owner_stop_remains_unconfirmed(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        async def close(self):
            events.append("control-close")

    class Run:
        async def close(self):
            events.append("run-close")
            return SimpleNamespace(unconfirmed_run_ids=("a" * 32,))

    bundle = canonical.CliRuntimeBundle(
        workspace_id=str(tmp_path),
        control_runtime=Control(),  # type: ignore[arg-type]
        run_runtime=Run(),  # type: ignore[arg-type]
        run_config=canonical.CliRunRuntimeConfig(
            tmp_path / "output",
            ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
            2,
            1,
        ),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="owner stop"):
            await bundle.close()
        with pytest.raises(RuntimeError, match="owner stop"):
            await bundle.close()

    asyncio.run(scenario())
    assert events == ["run-close", "run-close"]
    assert id(bundle.control_runtime) in canonical._QUARANTINED_CLI_RUNTIME_OWNERS
    canonical._release_cli_runtime_owner(bundle.control_runtime)


def test_run_start_failure_closes_both_runtime_owners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        repository = object()

        async def start(self):
            events.append("control-start")

        async def close(self):
            events.append("control-close")

    class Run:
        async def start(self):
            events.append("run-start")
            raise RuntimeError("injected")

        async def close(self):
            events.append("run-close")

    monkeypatch.setattr(
        canonical.ControlRuntime,
        "for_local_surface",
        lambda **_kwargs: Control(),
    )
    monkeypatch.setattr(
        canonical.RunRuntime,
        "for_local_surface",
        lambda **_kwargs: Run(),
    )
    config = canonical.CliRunRuntimeConfig(
        tmp_path / "output",
        ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
        2,
        1,
    )

    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(
            canonical.open_cli_runtime_bundle(tmp_path, run_config=config)
        )
    assert events == ["control-start", "run-start", "run-close", "control-close"]


@pytest.mark.parametrize("close_mode", ["unconfirmed", "raises"])
def test_run_start_failure_keeps_control_when_run_stop_is_unconfirmed(
    close_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        repository = object()

        async def start(self):
            events.append("control-start")

        async def close(self):
            events.append("control-close")

    class Run:
        async def start(self):
            events.append("run-start")
            raise RuntimeError("start failed")

        async def close(self):
            events.append("run-close")
            if close_mode == "raises":
                raise RuntimeError("stop proof unavailable")
            return SimpleNamespace(unconfirmed_run_ids=("a" * 32,))

    control_refs: list[weakref.ReferenceType[Control]] = []

    def build_control(**_kwargs):
        control = Control()
        control_refs.append(weakref.ref(control))
        return control

    monkeypatch.setattr(
        canonical.ControlRuntime,
        "for_local_surface",
        build_control,
    )
    monkeypatch.setattr(
        canonical.RunRuntime,
        "for_local_surface",
        lambda **_kwargs: Run(),
    )
    config = canonical.CliRunRuntimeConfig(
        tmp_path / "output",
        ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
        2,
        1,
    )

    with pytest.raises(canonical.CliRuntimeCloseUnconfirmed):
        asyncio.run(canonical.open_cli_runtime_bundle(tmp_path, run_config=config))

    assert events == ["control-start", "run-start", "run-close", "run-close"]
    gc.collect()
    retained = control_refs[0]()
    assert retained is not None
    canonical._release_cli_runtime_owner(retained)


def test_run_start_failure_retries_transient_stop_before_releasing_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        repository = object()

        async def start(self):
            events.append("control-start")

        async def close(self):
            events.append("control-close")

    class Run:
        close_calls = 0

        async def start(self):
            events.append("run-start")
            raise RuntimeError("start failed")

        async def close(self):
            self.close_calls += 1
            events.append(f"run-close-{self.close_calls}")
            if self.close_calls == 1:
                raise RuntimeError("transient stop proof failure")

    monkeypatch.setattr(
        canonical.ControlRuntime,
        "for_local_surface",
        lambda **_kwargs: Control(),
    )
    monkeypatch.setattr(
        canonical.RunRuntime,
        "for_local_surface",
        lambda **_kwargs: Run(),
    )
    config = canonical.CliRunRuntimeConfig(
        tmp_path / "output",
        ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
        2,
        1,
    )

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(canonical.open_cli_runtime_bundle(tmp_path, run_config=config))

    assert events == [
        "control-start",
        "run-start",
        "run-close-1",
        "run-close-2",
        "control-close",
    ]


def test_control_start_failure_still_releases_partial_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Control:
        async def start(self):
            events.append("control-start")
            raise RuntimeError("control-start-failed")

        async def close(self):
            events.append("control-close")

    monkeypatch.setattr(
        canonical.ControlRuntime,
        "for_local_surface",
        lambda **_kwargs: Control(),
    )
    config = canonical.CliRunRuntimeConfig(
        tmp_path / "output",
        ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
        2,
        1,
    )

    with pytest.raises(RuntimeError, match="control-start-failed"):
        asyncio.run(canonical.open_cli_runtime_bundle(tmp_path, run_config=config))
    assert events == ["control-start", "control-close"]


def test_reopen_closes_old_bundle_and_reuses_frozen_run_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = canonical.CliRunRuntimeConfig(
        tmp_path / "fixed-output",
        ExecutionResourceBudget(1, 1024, (), 1, 1024, 1),
        2,
        1,
    )
    events: list[str] = []

    class Current:
        run_config = config

        async def close(self):
            events.append("old-close")

    replacement = object()

    async def open_new(workspace, *, run_config):
        events.append(f"open:{Path(workspace).name}")
        assert run_config is config
        return replacement

    monkeypatch.setattr(canonical, "open_cli_runtime_bundle", open_new)
    result = asyncio.run(
        canonical.reopen_cli_runtime_bundle(Current(), tmp_path / "new-workspace")  # type: ignore[arg-type]
    )
    assert result is replacement
    assert events == ["old-close", "open:new-workspace"]


def test_cli_run_config_freezes_output_root_budget_and_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    budget = ExecutionResourceBudget(1, 1024, (), 1, 1024, 1)
    captured: dict[str, object] = {}

    def detect(root, *, environ):
        captured.update(root=root, environ=environ)
        return budget

    monkeypatch.setattr(canonical, "detect_execution_resource_budget", detect)
    environment = {
        "OMICSCLAW_OUTPUT_ROOT": str(tmp_path / "fixed-output"),
        "OMICSCLAW_RUN_BUFFER_CAPACITY": "7",
        "OMICSCLAW_RUN_MAX_ACTIVE": "3",
    }
    config = canonical.resolve_cli_run_runtime_config(
        tmp_path / "workspace",
        environ=environment,
    )

    assert config.output_root == (tmp_path / "fixed-output").resolve()
    assert config.resource_budget is budget
    assert config.max_buffered_runs == 7
    assert config.max_active_runs == 3
    assert captured == {"root": config.output_root, "environ": environment}
