from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from omicsclaw.control import run_runtime as run_runtime_module
from omicsclaw.common.report import write_result_json
from omicsclaw.control import (
    ControlStateRepository,
    RunAcceptanceIntent,
    RunAcceptanceStatus,
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentType,
    RunRecord,
    RunStartupReconciliationResult,
    RunTerminalProjectionIntegrityError,
)
from omicsclaw.control.run_contract import (
    ProjectScope,
    SimpleSkillRunSubmission,
    UnassignedScope,
    canonical_run_fingerprint,
)
from omicsclaw.control.projection_payload import analysis_lineage_digest
from omicsclaw.control.run_dispatcher import RunDispatcher
from omicsclaw.control.run_runtime import (
    LocalVerifiedSkillOutput,
    ResolvedSimpleSkill,
    RunArtifactProjectionIntegrityError,
    RunArtifactReadBackpressure,
    RunRevisionWaitBackpressure,
    RunRuntime,
    RunTerminalResultUnavailable,
    RunTerminalWaitBackpressure,
    SimpleSkillRunTerminalResult,
)
from omicsclaw.control.run_store import FilesystemRunStore
from omicsclaw.skill.execution.async_subprocess_driver import (
    ProcessTreeStopUnconfirmed,
)
from omicsclaw.skill.result import SkillRunAuditIdentity, SkillRunResult
from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    ExecutionResourceRequest,
    ExecutionResourceScheduler,
    ResourceLease,
)


SKILL_ID = "genomics-vcf-operations"
RESOURCE = ExecutionResourceRequest(1, 1024, 0, 1, 2048)
REVISION = {
    "skill_id": SKILL_ID,
    "skill_version": "0.5.0",
    "manifest_hash": "a" * 64,
    "source_hash": "b" * 64,
}
BUDGET = ExecutionResourceBudget(
    cpu_cores=2,
    memory_mib=4096,
    gpu_device_ids=(),
    threads=2,
    temporary_disk_mib=8192,
    max_processes=2,
)


def _submission(
    key: str,
    *,
    scope=UnassignedScope(),
    resource: ExecutionResourceRequest = RESOURCE,
) -> SimpleSkillRunSubmission:
    return SimpleSkillRunSubmission(
        run_submission_id=key * 32,
        scope=scope,
        skill_id=SKILL_ID,
        resource_request=resource,
    )


class _Authority:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def resolve(self, skill_id: str) -> ResolvedSimpleSkill:
        self.calls += 1
        if self.fail:
            raise AssertionError("duplicate touched current Skill authority")
        assert skill_id == SKILL_ID
        return ResolvedSimpleSkill(
            skill_id=SKILL_ID,
            resource_request=RESOURCE,
            skill_revision=REVISION,
            registry_snapshot=None,
        )


def _successful_result(context) -> SkillRunResult:
    artifacts = context.artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=False)
    write_result_json(
        artifacts,
        skill=SKILL_ID,
        version="0.5.0",
        input_checksum="",
        summary={"n_variants": 1},
        data={"ok": True},
    )
    (artifacts / "filtered.vcf").write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    return SkillRunResult(
        skill=SKILL_ID,
        success=True,
        exit_code=0,
        output_dir=str(artifacts),
        files=("filtered.vcf", "result.json"),
        audit_identity=SkillRunAuditIdentity(
            skill_id=SKILL_ID,
            skill_version="0.5.0",
            skill_hash="a" * 64,
            source_hash="b" * 64,
            environment_id="env:" + "c" * 20,
        ),
    )


def _failed_result() -> SkillRunResult:
    return SkillRunResult(
        skill=SKILL_ID,
        success=False,
        exit_code=1,
        stderr="injected executor failure",
        error_kind="script_failed",
    )


async def _wait_terminal(repo: ControlStateRepository, run_id: str):
    for _ in range(200):
        receipt = repo.get_run(run_id)
        if receipt.status in {"succeeded", "failed", "canceled", "interrupted"}:
            return receipt
        await asyncio.sleep(0.01)
    raise AssertionError("Run did not terminalize")


def _runtime(
    tmp_path: Path,
    repo: ControlStateRepository,
    authority: _Authority,
    executor,
    *,
    dispatcher: RunDispatcher | None = None,
    scheduler=None,
    max_terminal_waiters: int = 32,
    max_revision_waiters: int = 64,
    max_artifact_readers: int = 8,
) -> RunRuntime:
    return RunRuntime(
        repository=repo,
        run_store=FilesystemRunStore(tmp_path / "output"),
        dispatcher=dispatcher or RunDispatcher(max_buffered_runs=8, max_active_runs=2),
        resource_scheduler=scheduler or ExecutionResourceScheduler(BUDGET),
        skill_authority=authority,
        skill_executor=executor,
        max_terminal_waiters=max_terminal_waiters,
        max_revision_waiters=max_revision_waiters,
        max_artifact_readers=max_artifact_readers,
    )


def test_runtime_orders_lease_before_assignment_and_verifies_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        events: list[str] = []

        class RecordingScheduler:
            budget = BUDGET

            @asynccontextmanager
            async def reserve(self, ticket):
                assert ticket.request == RESOURCE
                assert repo.get_run(ticket.run_id).status == "queued"
                events.append("lease")
                yield ResourceLease(ticket.request, (), 0.0, run_id=ticket.run_id)
                events.append("released")

        async def executor(context):
            observation = repo.get_run_observation(context.run_id)
            assert observation.receipt.status == "running"
            assert observation.assignment is not None
            assert observation.assignment.assignment_id == context.assignment_id
            assert (
                observation.assignment.execution_reference_type
                == context.execution_reference_type
                == "linux-user-systemd-bwrap-v1"
            )
            assert (
                observation.assignment.execution_reference
                == context.execution_reference
            )
            events.append("executor")
            return _successful_result(context)

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            scheduler=RecordingScheduler(),
        )
        await runtime.start()
        result = await runtime.submit(_submission("1"))
        assert result.acceptance_status is RunAcceptanceStatus.ACCEPTED
        terminal = await _wait_terminal(repo, result.receipt.run_id)
        assert terminal.status == "succeeded"
        assert events == ["lease", "executor", "released"]
        assert runtime.get_receipt(terminal.run_id).assignment is not None
        projected = await runtime.get_terminal_result(terminal.run_id)
        assert projected.success is True
        assert projected.skill_id == SKILL_ID
        assert projected.output is not None
        assert projected.output.output_dir.endswith("/artifacts")
        runtime.run_store.verify_success(
            terminal.manifest_ref,
            assignment_id=runtime.get_receipt(terminal.run_id).assignment.assignment_id,
        )

        duplicate = await runtime.submit(_submission("1"))
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        assert duplicate.receipt.run_id == terminal.run_id
        assert events.count("executor") == 1
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_builds_one_frozen_unassigned_demo_submission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        submission = await runtime.build_simple_skill_demo_submission(
            run_submission_id="f" * 32,
            skill_id=SKILL_ID,
        )

        assert submission.run_submission_id == "f" * 32
        assert submission.skill_id == SKILL_ID
        assert isinstance(submission.scope, UnassignedScope)
        assert submission.resource_request == RESOURCE
        assert authority.calls == 1

        accepted = await runtime.submit(submission)
        duplicate = await runtime.submit(submission)
        assert accepted.acceptance_status is RunAcceptanceStatus.ACCEPTED
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        assert duplicate.receipt.run_id == accepted.receipt.run_id
        await runtime.wait_for_terminal_result(accepted.receipt.run_id)
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_demo_builder_preserves_an_explicit_typed_project_scope(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        project = repo.create_project("Validated Project")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        scope = ProjectScope(project.project_id)
        submission = await runtime.build_simple_skill_demo_submission(
            run_submission_id="e" * 32,
            skill_id=SKILL_ID,
            scope=scope,
        )

        assert submission.scope is scope
        accepted = await runtime.submit(submission)
        assert accepted.acceptance_status is RunAcceptanceStatus.ACCEPTED
        assert accepted.receipt is not None
        assert accepted.receipt.scope_kind == "project"
        assert accepted.receipt.project_id == project.project_id
        await runtime.wait_for_terminal_result(accepted.receipt.run_id)
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_explicit_missing_or_archived_project_is_rejected_without_run_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        archived = repo.create_project("Archived")
        repo.archive_project(archived.project_id)
        executor_calls = 0

        async def executor(_context):
            nonlocal executor_calls
            executor_calls += 1
            raise AssertionError("rejected explicit Project reached executor")

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        monkeypatch.setattr(
            run_runtime_module,
            "governed_process_tree_supported",
            lambda: True,
        )
        await runtime.start()

        missing = await runtime.submit(
            _submission("d", scope=ProjectScope("d" * 32))
        )
        rejected_archived = await runtime.submit(
            _submission("e", scope=ProjectScope(archived.project_id))
        )

        assert missing.acceptance_status is RunAcceptanceStatus.REJECTED
        assert missing.code == "project_not_found"
        assert rejected_archived.acceptance_status is RunAcceptanceStatus.REJECTED
        assert rejected_archived.code == "project_archived"
        assert executor_calls == 0
        assert repo.list_nonterminal_runs() == ()
        assert tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")) == ()
        assert tuple((tmp_path / "output").glob("*__*")) == ()

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_project_archived_between_plan_and_commit_never_downgrades_or_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        project = repo.create_project("Racing archive")
        executor_calls = 0

        async def executor(_context):
            nonlocal executor_calls
            executor_calls += 1
            raise AssertionError("archive race reached executor")

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        monkeypatch.setattr(
            run_runtime_module,
            "governed_process_tree_supported",
            lambda: True,
        )
        original_accept = repo.accept_run
        archived = False

        def archive_then_accept(*args, **kwargs):
            nonlocal archived
            if not archived:
                archived = True
                assert repo.archive_project(project.project_id).status.value == "changed"
            return original_accept(*args, **kwargs)

        monkeypatch.setattr(repo, "accept_run", archive_then_accept)
        await runtime.start()

        result = await runtime.submit(
            _submission("9", scope=ProjectScope(project.project_id))
        )

        assert result.acceptance_status is RunAcceptanceStatus.REJECTED
        assert result.code == "project_archived"
        assert executor_calls == 0
        assert repo.list_nonterminal_runs() == ()
        assert tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")) == ()
        assert tuple((tmp_path / "output").rglob(f"{SKILL_ID}__*")) == ()
        assert tuple((tmp_path / "output").rglob("manifest.json")) == ()
        assert tuple((tmp_path / "output").rglob("artifacts")) == ()
        assert tuple(
            (tmp_path / "output").rglob(".omicsclaw-run-claim.json")
        ) == ()

        project_dirs = tuple((tmp_path / "output").glob("*__*"))
        assert len(project_dirs) == 1
        projected = json.loads(
            (project_dirs[0] / "project_meta.json").read_text(encoding="utf-8")
        )
        assert projected["project_id"] == project.project_id

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_resolves_cli_navigation_only_through_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        active = repo.create_project("Active")
        archived = repo.create_project("Archived")
        repo.archive_project(archived.project_id)
        runtime = _runtime(
            tmp_path,
            repo,
            _Authority(),
            lambda context: _successful_result(context),
        )
        await runtime.start()

        assert isinstance(runtime.resolve_cli_navigation_scope(None), UnassignedScope)
        assert isinstance(
            runtime.resolve_cli_navigation_scope("not-an-opaque-id"),
            UnassignedScope,
        )
        assert isinstance(
            runtime.resolve_cli_navigation_scope("f" * 32),
            UnassignedScope,
        )
        assert runtime.resolve_cli_navigation_scope(active.project_id) == ProjectScope(
            active.project_id
        )
        assert isinstance(
            runtime.resolve_cli_navigation_scope(archived.project_id),
            UnassignedScope,
        )

        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(
                repo,
                "get_project",
                lambda _project_id: (_ for _ in ()).throw(
                    RuntimeError("Control storage unavailable")
                ),
            )
            with pytest.raises(RuntimeError, match="storage unavailable"):
                runtime.resolve_cli_navigation_scope(active.project_id)

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_result_failure_is_content_free_and_waiter_cancel_is_observation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        started = asyncio.Event()

        async def executor(_context):
            started.set()
            await asyncio.Future()

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("e"))
        await started.wait()

        waiter = asyncio.create_task(
            runtime.wait_for_terminal_result(accepted.receipt.run_id)
        )
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert repo.get_run(accepted.receipt.run_id).status == "running"

        await runtime.cancel(accepted.receipt.run_id)
        outcome = await runtime.wait_for_terminal_result(accepted.receipt.run_id)
        assert outcome.success is False
        assert outcome.receipt.status == "canceled"
        assert outcome.receipt.terminal_code == "canceled_by_owner"
        assert outcome.output is None
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_result_rejects_artifact_drift_without_mutating_incident_ledger(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        runtime = _runtime(
            tmp_path,
            repo,
            _Authority(),
            lambda context: asyncio.sleep(0, result=_successful_result(context)),
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("b"))
        outcome = await runtime.wait_for_terminal_result(accepted.receipt.run_id)
        assert outcome.success is True
        assert outcome.output is not None
        (Path(outcome.output.output_dir) / "filtered.vcf").write_text(
            "tampered\n", encoding="utf-8"
        )

        with pytest.raises(RunTerminalProjectionIntegrityError) as projected_error:
            await runtime.get_terminal_result(accepted.receipt.run_id)
        assert projected_error.value.__cause__ is None
        incidents = runtime.list_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents
        assert incidents == ()
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_result_normalizes_unexpected_run_store_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        runtime = _runtime(
            tmp_path,
            repo,
            _Authority(),
            lambda context: asyncio.sleep(0, result=_successful_result(context)),
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("c"))
        await _wait_terminal(repo, accepted.receipt.run_id)

        def rejected_projection(*_args, **_kwargs):
            raise ValueError("patient-secret-name.txt exceeded internal bound")

        monkeypatch.setattr(
            runtime.run_store,
            "project_verified_terminal",
            rejected_projection,
        )
        with pytest.raises(RunTerminalProjectionIntegrityError) as projected_error:
            await runtime.get_terminal_result(accepted.receipt.run_id)
        assert str(projected_error.value) == "terminal_result_integrity_error"
        assert projected_error.value.__cause__ is None
        assert "patient-secret" not in repr(projected_error.value)
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_result_failure_does_not_project_executor_error_content(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")

        async def executor(_context):
            return SkillRunResult(
                skill=SKILL_ID,
                success=False,
                exit_code=1,
                stderr="secret=/private/path/token-value",
                error_kind="script_failed",
            )

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("0"))
        outcome = await runtime.wait_for_terminal_result(accepted.receipt.run_id)

        assert outcome.receipt.status == "failed"
        assert outcome.receipt.terminal_code == "executor_failed"
        assert outcome.output is None
        assert "secret" not in repr(outcome)
        assert "/private" not in repr(outcome)
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_waiters_are_bounded_and_release_capacity_on_cancel(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        started = asyncio.Event()

        async def executor(_context):
            started.set()
            await asyncio.Future()

        runtime = _runtime(
            tmp_path,
            repo,
            _Authority(),
            executor,
            max_terminal_waiters=1,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("6"))
        await started.wait()
        first = asyncio.create_task(
            runtime.wait_for_terminal_result(accepted.receipt.run_id)
        )
        await asyncio.sleep(0)

        with pytest.raises(RunTerminalWaitBackpressure, match="wait_backpressure"):
            await runtime.wait_for_terminal_result(accepted.receipt.run_id)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert runtime._active_terminal_waiters == 0

        replacement = asyncio.create_task(
            runtime.wait_for_terminal_result(accepted.receipt.run_id)
        )
        await asyncio.sleep(0)
        await runtime.cancel(accepted.receipt.run_id)
        outcome = await replacement
        assert outcome.receipt.status == "canceled"
        assert runtime._active_terminal_waiters == 0
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_terminal_waiters_share_one_terminal_projection_without_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        started = asyncio.Event()
        release = asyncio.Event()
        executor_calls = 0

        async def executor(context):
            nonlocal executor_calls
            executor_calls += 1
            started.set()
            await release.wait()
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        projection_calls = 0
        original_projection = runtime.run_store.project_verified_terminal

        def record_projection(*args, **kwargs):
            nonlocal projection_calls
            projection_calls += 1
            return original_projection(*args, **kwargs)

        monkeypatch.setattr(
            runtime.run_store,
            "project_verified_terminal",
            record_projection,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("d"))
        duplicate = await runtime.submit(_submission("d"))
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        await started.wait()

        waiters = [
            asyncio.create_task(
                runtime.wait_for_terminal_result(accepted.receipt.run_id)
            )
            for _ in range(8)
        ]
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(*waiters)

        assert executor_calls == 1
        assert projection_calls == 1
        assert authority.calls == 1
        assert runtime._active_terminal_waiters == 0
        assert {outcome.receipt.run_id for outcome in outcomes} == {
            accepted.receipt.run_id
        }
        assert {outcome.receipt.status for outcome in outcomes} == {"succeeded"}
        assert {outcome.output.output_dir for outcome in outcomes if outcome.output} == {
            outcomes[0].output.output_dir
        }
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_close_terminalizes_active_run_before_draining_waiter(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        started = asyncio.Event()

        async def executor(_context):
            started.set()
            await asyncio.Future()

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("7"))
        await started.wait()
        waiter = asyncio.create_task(
            runtime.wait_for_terminal_result(accepted.receipt.run_id)
        )
        await asyncio.sleep(0)

        await asyncio.wait_for(runtime.close(), timeout=2)
        outcome = await asyncio.wait_for(waiter, timeout=1)

        assert outcome.receipt.status == "interrupted"
        assert outcome.receipt.terminal_code == "execution_interrupted"
        assert outcome.output is None
        assert runtime._active_terminal_waiters == 0
        repo.close()

    asyncio.run(scenario())


def test_runtime_close_finishes_reconciliation_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        runtime = _runtime(
            tmp_path,
            repo,
            _Authority(),
            lambda context: asyncio.sleep(0, result=_successful_result(context)),
        )
        await runtime.start()
        entered = asyncio.Event()
        release = asyncio.Event()
        expected = RunStartupReconciliationResult((), ("f" * 32,))

        async def blocked_reconciliation():
            entered.set()
            await release.wait()
            return expected

        monkeypatch.setattr(
            runtime,
            "_reconcile_nonterminal_run_owners",
            blocked_reconciliation,
        )
        close_caller = asyncio.create_task(runtime.close())
        await entered.wait()
        close_caller.cancel()
        await asyncio.sleep(0)
        assert not close_caller.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_caller, timeout=1)
        assert runtime._closed is True
        assert runtime._recovery_result == expected
        assert await runtime.close() == expected
        repo.close()

    asyncio.run(scenario())


def test_terminal_result_model_rejects_impossible_output_combinations() -> None:
    successful = RunRecord(
        run_id="a" * 32,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status="succeeded",
        terminal_code=None,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1,
        started_at_ms=2,
        finished_at_ms=3,
        revision=2,
    )
    failed = replace(
        successful,
        status="failed",
        terminal_code="executor_failed",
    )
    with pytest.raises(ValueError, match="output invariant"):
        SimpleSkillRunTerminalResult(successful, SKILL_ID, None)
    with pytest.raises(ValueError, match="output invariant"):
        SimpleSkillRunTerminalResult(
            failed,
            SKILL_ID,
            LocalVerifiedSkillOutput("/should/not/exist"),
        )


def test_matching_duplicate_precedes_current_project_and_skill_gates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        project = repo.create_project("Historical project")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        submission = _submission("2", scope=ProjectScope(project.project_id))
        accepted = await runtime.submit(submission)
        await _wait_terminal(repo, accepted.receipt.run_id)
        repo.archive_project(project.project_id)
        authority.fail = True

        duplicate = await runtime.submit(submission)
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        assert duplicate.receipt.run_id == accepted.receipt.run_id
        assert authority.calls == 1

        conflict_resource = ExecutionResourceRequest(2, 1024, 0, 1, 2048)
        conflict = await runtime.submit(
            _submission(
                "2",
                scope=ProjectScope(project.project_id),
                resource=conflict_resource,
            )
        )
        assert conflict.acceptance_status is RunAcceptanceStatus.CONFLICT
        assert conflict.code == "run_idempotency_conflict"
        assert authority.calls == 1
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_concurrent_same_submission_creates_one_manifest_and_executor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        calls = 0

        async def executor(context):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        results = await asyncio.gather(
            *(runtime.submit(_submission("3")) for _ in range(12))
        )
        run_ids = {item.receipt.run_id for item in results}
        assert len(run_ids) == 1
        assert (
            sum(
                item.acceptance_status is RunAcceptanceStatus.ACCEPTED
                for item in results
            )
            == 1
        )
        assert (
            sum(
                item.acceptance_status is RunAcceptanceStatus.DUPLICATE
                for item in results
            )
            == 11
        )
        await _wait_terminal(repo, next(iter(run_ids)))
        assert calls == 1
        assert (
            len(tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")))
            == 1
        )
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_queued_and_running_cancel_follow_assignment_race(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        running = asyncio.Event()
        canceled = asyncio.Event()
        calls: list[str] = []

        async def executor(context):
            calls.append(context.run_id)
            running.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                canceled.set()
                raise

        dispatcher = RunDispatcher(max_buffered_runs=4, max_active_runs=1)
        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            dispatcher=dispatcher,
        )
        await runtime.start()
        first = await runtime.submit(_submission("4"))
        await running.wait()
        second = await runtime.submit(_submission("5"))

        queued_cancel = await runtime.cancel(second.receipt.run_id)
        assert queued_cancel.code == "canceled_before_assignment"
        assert repo.get_run_observation(second.receipt.run_id).assignment is None

        running_cancel = await runtime.cancel(first.receipt.run_id)
        assert running_cancel.code == "cancel_requested"
        assert running_cancel.receipt.status == "cancel_requested"
        await asyncio.wait_for(canceled.wait(), timeout=1)
        assert (await _wait_terminal(repo, first.receipt.run_id)).status == "canceled"
        assert calls == [first.receipt.run_id]
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_enqueue_fault_terminalizes_bound_run_without_invoking_executor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        calls = 0

        def fault(name: str) -> None:
            if name == "enqueue.after_append":
                raise RuntimeError("injected enqueue failure")

        async def executor(_context):
            nonlocal calls
            calls += 1
            raise AssertionError

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            dispatcher=RunDispatcher(
                max_buffered_runs=2,
                max_active_runs=1,
                fault_hook=fault,
            ),
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("6"))

        assert accepted.acceptance_status is RunAcceptanceStatus.ACCEPTED
        assert accepted.receipt.status == "failed"
        assert accepted.receipt.terminal_code == "submission_failed"
        assert calls == 0
        duplicate = await runtime.submit(_submission("6"))
        assert duplicate.receipt.run_id == accepted.receipt.run_id
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_control_commit_fault_abandons_header_and_leaves_no_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        armed = True

        def fault(name: str) -> None:
            if armed and name == "accept_run.before_commit":
                raise RuntimeError("injected control commit failure")

        repo = ControlStateRepository(tmp_path / "state", fault_hook=fault)
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        with pytest.raises(RuntimeError, match="injected control commit"):
            await runtime.submit(_submission("8"))

        inspection = repo.inspect_run_submission(
            run_submission_id="8" * 32,
            fingerprint_version=1,
            fingerprint_sha256=canonical_run_fingerprint(_submission("8"))[1],
        )
        assert inspection.state == "novel"
        assert repo.list_nonterminal_runs() == ()
        assert tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")) == ()

        armed = False
        accepted = await runtime.submit(_submission("8"))
        assert accepted.acceptance_status is RunAcceptanceStatus.ACCEPTED
        await _wait_terminal(repo, accepted.receipt.run_id)
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_resource_mismatch_and_unsupported_budget_create_no_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(_context):
            raise AssertionError("rejected Run reached executor")

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        mismatch = await runtime.submit(
            _submission(
                "9",
                resource=ExecutionResourceRequest(2, 1024, 0, 1, 2048),
            )
        )
        assert mismatch.code == "resource_contract_mismatch"
        assert mismatch.receipt is None
        await runtime.close()
        repo.close()

        repo2 = ControlStateRepository(tmp_path / "state-2")
        too_small = ExecutionResourceBudget(
            cpu_cores=1,
            memory_mib=512,
            gpu_device_ids=(),
            threads=1,
            temporary_disk_mib=1024,
            max_processes=1,
        )
        runtime2 = _runtime(
            tmp_path / "second",
            repo2,
            _Authority(),
            executor,
            scheduler=ExecutionResourceScheduler(too_small),
        )
        await runtime2.start()
        unsupported = await runtime2.submit(_submission("a"))
        assert unsupported.code == "resource_unsupported"
        assert unsupported.receipt is None
        assert repo2.list_nonterminal_runs() == ()
        await runtime2.close()
        repo2.close()

    asyncio.run(scenario())


def test_startup_interrupts_legacy_nonterminal_runs_without_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        legacy = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="legacy-submission",
                fingerprint_version=1,
                fingerprint_sha256="f" * 64,
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store:v1:" + "f" * 32,
            )
        )
        assert legacy.status is RunAcceptanceStatus.ACCEPTED
        calls = 0

        async def executor(_context):
            nonlocal calls
            calls += 1
            raise AssertionError

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        recovered = await runtime.start()

        assert recovered.interrupted_run_ids == (legacy.run_id,)
        receipt = repo.get_run(legacy.run_id)
        assert receipt.status == "interrupted"
        assert receipt.terminal_code == "control_plane_restarted"
        assert calls == 0
        assert (await runtime.start()).interrupted_run_ids == ()
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_real_resource_ready_demo_uses_shared_skill_runner(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        runtime = RunRuntime.for_local_surface(
            repository=repo,
            output_root=tmp_path / "output",
            resource_budget=BUDGET,
            max_buffered_runs=2,
            max_active_runs=1,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("7"))
        terminal = await _wait_terminal(repo, accepted.receipt.run_id)

        assert terminal.status == "succeeded"
        artifacts = runtime.run_store.artifacts_dir(terminal.manifest_ref)
        assert (artifacts / "filtered.vcf").is_file()
        assert (artifacts / "result.json").is_file()
        observation = runtime.get_receipt(terminal.run_id)
        assert observation.assignment is not None
        runtime.run_store.verify_success(
            terminal.manifest_ref,
            assignment_id=observation.assignment.assignment_id,
        )
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("successful", "expected_status"),
    [(True, "succeeded"), (False, "failed")],
)
def test_terminal_manifest_wins_report_rollback_cancel_race(
    tmp_path: Path,
    successful: bool,
    expected_status: str,
) -> None:
    async def scenario() -> None:
        armed = True

        def fault(name: str) -> None:
            nonlocal armed
            if armed and name == "apply_run_report.before_commit":
                armed = False
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                raise RuntimeError("injected report rollback")

        repo = ControlStateRepository(tmp_path / "state", fault_hook=fault)
        authority = _Authority()

        async def executor(context):
            return _successful_result(context) if successful else _failed_result()

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("c" if successful else "d"))
        receipt = await _wait_terminal(repo, accepted.receipt.run_id)

        assert receipt.status == expected_status
        assert runtime.dispatcher.quarantined is False
        manifest = runtime.run_store.read_manifest(receipt.manifest_ref)
        assert manifest["completion"]["kind"] == expected_status
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_unrecoverable_store_failure_is_observed_and_quarantines_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        store = FilesystemRunStore(tmp_path / "output")

        async def executor(context):
            return _successful_result(context)

        def unavailable(*_args, **_kwargs):
            raise OSError("injected durable Store failure")

        monkeypatch.setattr(store, "commit_success", unavailable)
        monkeypatch.setattr(store, "commit_failure", unavailable)
        dispatcher = RunDispatcher(max_buffered_runs=2, max_active_runs=1)
        runtime = RunRuntime(
            repository=repo,
            run_store=store,
            dispatcher=dispatcher,
            resource_scheduler=ExecutionResourceScheduler(BUDGET),
            skill_authority=authority,
            skill_executor=executor,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("e"))
        for _ in range(200):
            if dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert dispatcher.quarantined is True
        assert repo.get_run(accepted.receipt.run_id).status == "running"

        await runtime.close()
        assert repo.get_run(accepted.receipt.run_id).status == "interrupted"
        repo.close()

    asyncio.run(scenario())


def test_manifest_receipt_binding_failure_records_content_free_incident_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        store = FilesystemRunStore(tmp_path / "output")
        calls = 0

        def mismatched_binding(*_args, **_kwargs):
            raise OSError("secret-token at /private/input/sample.vcf")

        async def executor(_context):
            nonlocal calls
            calls += 1
            raise AssertionError("mismatched Manifest reached executor")

        monkeypatch.setattr(store, "verify_receipt_binding", mismatched_binding)
        runtime = RunRuntime(
            repository=repo,
            run_store=store,
            dispatcher=RunDispatcher(max_buffered_runs=2, max_active_runs=1),
            resource_scheduler=ExecutionResourceScheduler(BUDGET),
            skill_authority=_Authority(),
            skill_executor=executor,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("b"))
        for _ in range(200):
            if runtime.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)

        assert runtime.dispatcher.quarantined is True
        assert calls == 0
        assert repo.get_run(accepted.receipt.run_id).status == "running"
        incident = repo.list_run_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents[0]
        assert (
            incident.incident_type is RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH
        )
        assert (
            incident.evidence_code
            is RunIntegrityEvidenceCode.MANIFEST_RECEIPT_BINDING_MISMATCH
        )
        persisted = repo.database_path.read_bytes()
        assert b"secret-token" not in persisted
        assert b"/private/input" not in persisted
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_conflicting_terminal_manifest_records_incident_without_receipt_overwrite(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        runtime: RunRuntime

        async def executor(context):
            runtime.run_store.commit_failure(
                context.manifest_ref,
                terminal_code="executor_failed",
                execution_evidence={"error_kind": "script_failed"},
                assignment_id=context.assignment_id,
            )
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, _Authority(), executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("5"))
        for _ in range(200):
            if runtime.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)

        assert runtime.dispatcher.quarantined is True
        receipt = repo.get_run(accepted.receipt.run_id)
        assert receipt.status == "running"
        assert (
            runtime.run_store.read_manifest(receipt.manifest_ref)["completion"]["kind"]
            == "failed"
        )
        incident = repo.list_run_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents[0]
        assert (
            incident.incident_type is RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH
        )
        assert (
            incident.evidence_code
            is RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT
        )
        await runtime.close()
        assert repo.get_run(accepted.receipt.run_id).status == "failed"
        repo.close()

    asyncio.run(scenario())


def test_process_ownership_loss_retains_lease_and_rejects_novel_runs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        scheduler = ExecutionResourceScheduler(BUDGET)

        async def executor(_context):
            raise ProcessTreeStopUnconfirmed("injected ownership loss")

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            scheduler=scheduler,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("f"))
        for _ in range(200):
            if runtime.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert runtime.dispatcher.quarantined is True
        assert scheduler.ready is False
        assert scheduler.quiescent is False
        assert repo.get_run(accepted.receipt.run_id).status == "running"
        incidents = repo.list_run_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents
        assert len(incidents) == 1
        assert (
            incidents[0].incident_type
            is RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
        )
        assert (
            incidents[0].evidence_code
            is RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED
        )

        duplicate = await runtime.submit(_submission("f"))
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        novel = await runtime.submit(_submission("0"))
        assert novel.acceptance_status is RunAcceptanceStatus.REJECTED
        assert novel.code == "control_not_ready"

        await runtime.close()
        assert repo.get_run(accepted.receipt.run_id).status == "interrupted"
        repo.close()

    asyncio.run(scenario())


def test_duplicate_observation_and_terminal_cancel_survive_dispatcher_quarantine(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("1"))
        terminal = await _wait_terminal(repo, accepted.receipt.run_id)
        reservation = await runtime.dispatcher.try_reserve()
        assert reservation is not None
        await reservation.quarantine()

        duplicate = await runtime.submit(_submission("1"))
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        assert runtime.get_receipt(terminal.run_id).receipt.status == "succeeded"
        canceled = await runtime.cancel(terminal.run_id)
        assert canceled.code == "already_terminal"
        novel = await runtime.submit(_submission("2"))
        assert novel.acceptance_status is RunAcceptanceStatus.REJECTED
        assert novel.code == "control_not_ready"
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_restart_recovers_verified_success_manifest_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        calls = 0

        async def executor(context):
            nonlocal calls
            calls += 1
            return _successful_result(context)

        first = _runtime(tmp_path, repo, authority, executor)
        original_apply = repo.apply_run_report

        def unavailable_report(_report):
            raise OSError("injected terminal transaction outage")

        monkeypatch.setattr(repo, "apply_run_report", unavailable_report)
        await first.start()
        accepted = await first.submit(_submission("7"))
        for _ in range(200):
            if first.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert first.dispatcher.quarantined is True
        receipt = repo.get_run(accepted.receipt.run_id)
        assert receipt.status == "running"
        assert (
            first.run_store.read_manifest(receipt.manifest_ref)["completion"]["kind"]
            == "succeeded"
        )
        await first.dispatcher.close()

        monkeypatch.setattr(repo, "apply_run_report", original_apply)

        async def forbidden_replay(_context):
            raise AssertionError("startup replayed a durable Run")

        second = _runtime(tmp_path, repo, authority, forbidden_replay)
        recovered = await second.start()
        assert recovered.interrupted_run_ids == ()
        assert recovered.unconfirmed_run_ids == ()
        assert repo.get_run(accepted.receipt.run_id).status == "succeeded"
        assert calls == 1
        await second.close()
        await first.close()
        repo.close()

    asyncio.run(scenario())


def test_startup_audits_terminal_manifest_receipt_drift_without_replay_or_repair(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")

        async def executor(context):
            return _successful_result(context)

        first = _runtime(tmp_path, repo, _Authority(), executor)
        await first.start()
        accepted = await first.submit(_submission("4"))
        terminal = await _wait_terminal(repo, accepted.receipt.run_id)
        assert terminal.status == "succeeded"
        await first.close()

        manifest_path = (
            first.run_store.artifacts_dir(terminal.manifest_ref).parent
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["completion"] = None
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        calls = 0

        async def forbidden_replay(_context):
            nonlocal calls
            calls += 1
            raise AssertionError("terminal audit replayed a Run")

        second = _runtime(tmp_path, repo, _Authority(), forbidden_replay)
        await second.start()
        assert calls == 0
        assert repo.get_run(terminal.run_id).status == "succeeded"
        incident = repo.list_run_integrity_incidents(run_id=terminal.run_id).incidents[
            0
        ]
        assert (
            incident.incident_type is RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH
        )
        assert (
            incident.evidence_code
            is RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID
        )
        await second.start()
        assert len(repo.list_run_integrity_incidents().incidents) == 1
        await second.close()
        repo.close()

    asyncio.run(scenario())


def test_recovery_terminal_commit_failure_is_durable_idempotent_and_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")

        async def executor(context):
            return _successful_result(context)

        first = _runtime(tmp_path, repo, _Authority(), executor)
        original_apply = repo.apply_run_report

        def unavailable_report(_report):
            raise OSError("database commit unavailable at /secret/control.db")

        monkeypatch.setattr(repo, "apply_run_report", unavailable_report)
        await first.start()
        accepted = await first.submit(_submission("6"))
        for _ in range(200):
            if first.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert first.dispatcher.quarantined is True
        await first.dispatcher.close()

        scheduler = ExecutionResourceScheduler(BUDGET)

        async def forbidden_replay(_context):
            raise AssertionError("recovery replayed durable completion evidence")

        second = _runtime(
            tmp_path,
            repo,
            _Authority(),
            forbidden_replay,
            scheduler=scheduler,
        )
        recovered = await second.start()
        assert recovered.unconfirmed_run_ids == (accepted.receipt.run_id,)
        assert second.ready is False
        assert scheduler.ready is False
        assert repo.get_run(accepted.receipt.run_id).status == "running"
        incidents = repo.list_run_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents
        assert len(incidents) == 1
        assert (
            incidents[0].incident_type
            is RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED
        )
        assert (
            incidents[0].evidence_code
            is RunIntegrityEvidenceCode.RECOVERY_TERMINAL_TRANSACTION_FAILED
        )
        assert b"/secret/control.db" not in repo.database_path.read_bytes()

        repeated = await second.start()
        assert repeated.unconfirmed_run_ids == (accepted.receipt.run_id,)
        assert (
            len(
                repo.list_run_integrity_incidents(
                    run_id=accepted.receipt.run_id
                ).incidents
            )
            == 1
        )

        monkeypatch.setattr(repo, "apply_run_report", original_apply)
        await second.close()
        await first.close()
        assert repo.get_run(accepted.receipt.run_id).status == "succeeded"
        repo.close()

    asyncio.run(scenario())


def test_unconfirmed_restart_owner_preserves_nonterminal_evidence_and_quarantines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        first_scheduler = ExecutionResourceScheduler(BUDGET)
        temporary: Path | None = None

        async def executor(context):
            nonlocal temporary
            temporary = context.temporary_dir
            raise ProcessTreeStopUnconfirmed("injected ownership uncertainty")

        async def unobservable_owner(_reference_type, _reference):
            raise ProcessTreeStopUnconfirmed("injected recovery uncertainty")

        monkeypatch.setattr(
            run_runtime_module,
            "reconcile_governed_process_tree",
            unobservable_owner,
        )
        first = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            scheduler=first_scheduler,
        )
        await first.start()
        accepted = await first.submit(_submission("8"))
        for _ in range(200):
            if first.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert temporary is not None and temporary.is_dir()
        waiter = asyncio.create_task(
            first.wait_for_terminal_result(accepted.receipt.run_id)
        )
        await asyncio.sleep(0)
        closed = await first.close()
        with pytest.raises(RunTerminalResultUnavailable, match="runtime_closed"):
            await asyncio.wait_for(waiter, timeout=1)
        assert await first.close() == closed
        assert closed.unconfirmed_run_ids == (accepted.receipt.run_id,)
        assert repo.get_run(accepted.receipt.run_id).status == "running"
        assert temporary.is_dir()
        assert first_scheduler.ready is False

        second_scheduler = ExecutionResourceScheduler(BUDGET)

        async def forbidden_replay(_context):
            raise AssertionError("unconfirmed Run was replayed")

        second = _runtime(
            tmp_path,
            repo,
            authority,
            forbidden_replay,
            scheduler=second_scheduler,
        )
        recovered = await second.start()
        assert recovered.unconfirmed_run_ids == (accepted.receipt.run_id,)
        assert second.lifecycle_ready is True
        assert second.ready is False
        assert second_scheduler.ready is False
        incidents = repo.list_run_integrity_incidents(
            run_id=accepted.receipt.run_id
        ).incidents
        assert len(incidents) == 1
        assert (
            incidents[0].evidence_code
            is RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED
        )
        duplicate = await second.submit(_submission("8"))
        assert duplicate.acceptance_status is RunAcceptanceStatus.DUPLICATE
        assert second.get_receipt(accepted.receipt.run_id).receipt.status == "running"
        canceled = await second.cancel(accepted.receipt.run_id)
        assert canceled.code == "cancel_requested"
        assert canceled.receipt.status == "cancel_requested"
        novel = await second.submit(_submission("9"))
        assert novel.acceptance_status is RunAcceptanceStatus.REJECTED
        assert novel.code == "control_not_ready"
        await second.close()
        repo.close()

    asyncio.run(scenario())


def test_startup_missing_execution_owner_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        accepted = repo.accept_run(
            RunAcceptanceIntent(
                run_submission_id="legacy-assigned",
                fingerprint_version=1,
                fingerprint_sha256="a" * 64,
                run_kind="skill",
                scope_kind="unassigned",
                manifest_ref="run-store:v1:" + "a" * 32,
            )
        )
        repo.assign_run(accepted.run_id, executor_kind="legacy-local")

        async def forbidden_replay(_context):
            raise AssertionError("legacy assigned Run was replayed")

        runtime = _runtime(tmp_path, repo, _Authority(), forbidden_replay)
        recovered = await runtime.start()
        assert recovered.interrupted_run_ids == ()
        assert recovered.unconfirmed_run_ids == (accepted.run_id,)
        assert runtime.ready is False
        assert repo.get_run(accepted.run_id).status == "running"
        incident = repo.list_run_integrity_incidents(run_id=accepted.run_id).incidents[
            0
        ]
        assert (
            incident.incident_type
            is RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
        )
        assert (
            incident.evidence_code
            is RunIntegrityEvidenceCode.EXECUTION_REFERENCE_MISSING
        )
        await runtime.close()
        repo.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not run_runtime_module.governed_process_tree_supported(),
    reason="no cgroup-backed governed process-tree Adapter",
)
def test_restart_stops_persisted_active_scope_before_interrupted_receipt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        wrapper: subprocess.Popen[str] | None = None
        started = tmp_path / "active-scope-started"
        late_marker = tmp_path / "active-scope-late"

        async def uncertain_executor(context):
            nonlocal wrapper
            target = tmp_path / "active_scope_target.py"
            target.write_text(
                textwrap.dedent(
                    f"""
                    import pathlib, time
                    pathlib.Path({str(started)!r}).write_text("started")
                    time.sleep(1)
                    pathlib.Path({str(late_marker)!r}).write_text("late")
                    time.sleep(30)
                    """
                ),
                encoding="utf-8",
            )
            wrapper = subprocess.Popen(
                [
                    "systemd-run",
                    "--user",
                    "--scope",
                    "--quiet",
                    f"--unit={context.execution_reference.removesuffix('.scope')}",
                    "--",
                    sys.executable,
                    str(target),
                ],
                text=True,
            )
            for _ in range(200):
                if started.exists():
                    break
                await asyncio.sleep(0.01)
            assert started.exists()
            raise ProcessTreeStopUnconfirmed("simulated Backend ownership loss")

        first = _runtime(tmp_path, repo, authority, uncertain_executor)
        await first.start()
        accepted = await first.submit(_submission("a"))
        for _ in range(200):
            if first.dispatcher.quarantined:
                break
            await asyncio.sleep(0.01)
        assert first.dispatcher.quarantined is True
        assert repo.get_run(accepted.receipt.run_id).status == "running"

        async def forbidden_replay(_context):
            raise AssertionError("restart replayed an assigned Run")

        second = _runtime(
            tmp_path,
            repo,
            authority,
            forbidden_replay,
            scheduler=ExecutionResourceScheduler(BUDGET),
        )
        try:
            recovered = await second.start()
            assert recovered.unconfirmed_run_ids == ()
            receipt = repo.get_run(accepted.receipt.run_id)
            assert receipt.status == "interrupted"
            assert receipt.terminal_code == "control_plane_restarted"
            assert (
                second.run_store.read_manifest(receipt.manifest_ref)["completion"][
                    "terminal_code"
                ]
                == "control_plane_restarted"
            )
            await asyncio.sleep(1.1)
            assert not late_marker.exists()
        finally:
            if wrapper is not None:
                try:
                    wrapper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    wrapper.kill()
                    wrapper.wait(timeout=5)
            await second.close()
            await first.close()
            repo.close()

    asyncio.run(scenario())


def test_revision_wait_observes_assignment_cancel_request_and_terminal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        allow_assignment = asyncio.Event()
        executor_started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        allow_stop = asyncio.Event()

        class GateScheduler:
            budget = BUDGET
            ready = True

            @asynccontextmanager
            async def reserve(self, ticket):
                await allow_assignment.wait()
                yield ResourceLease(ticket.request, (), 0.0, run_id=ticket.run_id)

        async def executor(_context):
            executor_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await allow_stop.wait()
                raise

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            scheduler=GateScheduler(),
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("a"))
        assert accepted.receipt is not None
        run_id = accepted.receipt.run_id
        assert repo.get_run(run_id).revision == 1
        assert runtime.get_receipt_skill_id(run_id) == SKILL_ID
        page = runtime.list_receipts(status="queued", limit=1)
        assert page.observations[0].receipt.run_id == run_id

        assigned_wait = asyncio.create_task(
            runtime.wait_for_receipt_revision(run_id, after_revision=1)
        )
        await asyncio.sleep(0)
        allow_assignment.set()
        assigned = await asyncio.wait_for(assigned_wait, timeout=2)
        assert assigned.receipt.status == "running"
        assert assigned.receipt.revision == 2
        await asyncio.wait_for(executor_started.wait(), timeout=2)

        cancel_requested_wait = asyncio.create_task(
            runtime.wait_for_receipt_revision(run_id, after_revision=2)
        )
        cancel_result = await runtime.cancel(run_id)
        assert cancel_result.code == "cancel_requested"
        cancel_requested = await asyncio.wait_for(cancel_requested_wait, timeout=2)
        assert cancel_requested.receipt.status == "cancel_requested"
        assert cancel_requested.receipt.revision == 3
        await asyncio.wait_for(cancellation_seen.wait(), timeout=2)

        terminal_wait = asyncio.create_task(
            runtime.wait_for_receipt_revision(run_id, after_revision=3)
        )
        allow_stop.set()
        terminal = await asyncio.wait_for(terminal_wait, timeout=2)
        assert terminal.receipt.status == "canceled"
        assert terminal.receipt.revision == 4
        # Equal terminal revision is snapshot-first and never waits forever.
        replay = await runtime.wait_for_receipt_revision(
            run_id,
            after_revision=terminal.receipt.revision,
        )
        assert replay.receipt == terminal.receipt

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_revision_wait_capacity_and_cancellation_only_detach_observation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()
        never_assign = asyncio.Event()

        class GateScheduler:
            budget = BUDGET
            ready = True

            @asynccontextmanager
            async def reserve(self, ticket):
                await never_assign.wait()
                yield ResourceLease(ticket.request, (), 0.0, run_id=ticket.run_id)

        async def forbidden_executor(_context):
            raise AssertionError("queued Run unexpectedly executed")

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            forbidden_executor,
            scheduler=GateScheduler(),
            max_revision_waiters=1,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("b"))
        assert accepted.receipt is not None
        run_id = accepted.receipt.run_id

        first = asyncio.create_task(
            runtime.wait_for_receipt_revision(run_id, after_revision=1)
        )
        await asyncio.sleep(0)
        with pytest.raises(RunRevisionWaitBackpressure):
            await runtime.wait_for_receipt_revision(run_id, after_revision=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert repo.get_run(run_id).status == "queued"
        assert repo.get_run_observation(run_id).assignment is None

        next_wait = asyncio.create_task(
            runtime.wait_for_receipt_revision(run_id, after_revision=1)
        )
        await runtime.cancel(run_id)
        terminal = await asyncio.wait_for(next_wait, timeout=2)
        assert terminal.receipt.status == "canceled"
        assert terminal.assignment is None

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_artifact_page_reader_capacity_and_tamper_are_content_safe(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(
            tmp_path,
            repo,
            authority,
            executor,
            max_artifact_readers=1,
        )
        await runtime.start()
        accepted = await runtime.submit(_submission("c"))
        assert accepted.receipt is not None
        run_id = accepted.receipt.run_id
        terminal = await runtime.wait_for_terminal_result(run_id)
        assert terminal.success is True

        first_page = await runtime.list_verified_artifacts(run_id, limit=1)
        assert first_page.skill_id == SKILL_ID
        assert first_page.total == 2
        assert len(first_page.artifacts) == 1
        assert first_page.next_cursor == first_page.artifacts[0].relative_path
        second_page = await runtime.list_verified_artifacts(
            run_id,
            cursor=first_page.next_cursor,
            limit=1,
        )
        assert len(second_page.artifacts) == 1
        assert second_page.next_cursor is None

        reader = await runtime.open_verified_artifact(run_id, "filtered.vcf")
        assert await reader.read_chunk(offset=0, max_bytes=2) == b"##"
        with pytest.raises(RunArtifactReadBackpressure):
            await runtime.open_verified_artifact(run_id, "result.json")
        await reader.aclose()
        replacement = await runtime.open_verified_artifact(run_id, "result.json")
        assert await replacement.read_chunk(offset=0, max_bytes=1) == b"{"
        await replacement.aclose()

        receipt = repo.get_run(run_id)
        artifact = runtime.run_store.artifacts_dir(receipt.manifest_ref) / "filtered.vcf"
        artifact.write_text("tampered\n", encoding="utf-8")
        with pytest.raises(RunArtifactProjectionIntegrityError) as captured:
            await runtime.list_verified_artifacts(run_id)
        assert captured.value.code == "run_artifact_integrity_error"
        assert repo.list_run_integrity_incidents(run_id=run_id).incidents == ()

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_runtime_close_closes_verified_artifact_descriptors(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        accepted = await runtime.submit(_submission("d"))
        assert accepted.receipt is not None
        run_id = accepted.receipt.run_id
        await runtime.wait_for_terminal_result(run_id)
        reader = await runtime.open_verified_artifact(run_id, "filtered.vcf")
        assert reader.closed is False

        await runtime.close()
        assert reader.closed is True
        repo.close()

    asyncio.run(scenario())


def test_successful_project_run_freezes_an_analysis_lineage_projection(
    tmp_path: Path,
) -> None:
    # ADR 0064 Producer: a project-scoped Run that terminalizes as succeeded must
    # freeze exactly one analysis_lineage Projection Intent, atomically, whose
    # source is the Run Manifest and whose digest the projector can re-verify.
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        project = repo.create_project("Projected study")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        result = await runtime.submit(
            _submission("1", scope=ProjectScope(project.project_id))
        )
        terminal = await _wait_terminal(repo, result.receipt.run_id)
        assert terminal.status == "succeeded"

        intents = repo.list_projection_intents(project.project_id)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.projection_kind == "analysis_lineage"
        assert intent.source_store == "run"
        assert intent.source_ref == terminal.manifest_ref
        assert intent.state == "pending"
        # The frozen digest matches a re-derivation from the durable Manifest.
        manifest = runtime.run_store.read_manifest(terminal.manifest_ref)
        assert intent.content_sha256 == analysis_lineage_digest(manifest)

        await runtime.close()
        repo.close()

    asyncio.run(scenario())


def test_successful_unassigned_run_freezes_no_projection(tmp_path: Path) -> None:
    # An unassigned Run contributes no Project Memory, so it freezes no Intent.
    async def scenario() -> None:
        repo = ControlStateRepository(tmp_path / "state")
        authority = _Authority()

        async def executor(context):
            return _successful_result(context)

        runtime = _runtime(tmp_path, repo, authority, executor)
        await runtime.start()
        result = await runtime.submit(_submission("2"))  # UnassignedScope default
        terminal = await _wait_terminal(repo, result.receipt.run_id)
        assert terminal.status == "succeeded"
        assert terminal.project_id is None

        await runtime.close()
        repo.close()

    asyncio.run(scenario())
