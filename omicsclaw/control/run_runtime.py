"""Deep Backend-owned Interface for canonical simple Skill Runs.

Surface Adapters submit one typed request and receive a Receipt.  This Module
keeps duplicate-first admission, Manifest publication, Dispatcher ownership,
resource readiness, Assignment fencing, shared Skill execution and terminal
evidence in one non-bypassable sequence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar

from omicsclaw.skill.registry import RegistrySnapshot, ensure_registry_loaded
from omicsclaw.skill.execution.async_subprocess_driver import (
    ProcessTreeStopUnconfirmed,
    governed_process_tree_supported,
    new_governed_process_tree_reference,
    reconcile_governed_process_tree,
)
from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    ExecutionResourceRequest,
    ResourceLease,
    ResourceTicket,
    get_process_resource_scheduler,
)
from omicsclaw.skill.result import SkillRunResult

from .errors import ControlIntegrityError, RunIntegrityIncidentError
from .models import (
    AssignmentStatus,
    ProjectionIntentInput,
    RunAcceptanceIntent,
    RunAcceptanceStatus,
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentIntent,
    RunIntegrityIncidentPage,
    RunIntegrityIncidentType,
    RunObservationPage,
    RunObservationSnapshot,
    RunRecord,
    RunReport,
    RunStartupReconciliationResult,
)
from .projection_payload import ANALYSIS_LINEAGE_KIND, analysis_lineage_digest
from .repository import ControlStateRepository
from .run_contract import (
    ProjectScope,
    RunScope,
    SimpleSkillRunSubmission,
    UnassignedScope,
    canonical_run_fingerprint,
)
from .run_dispatcher import (
    RunBufferReservation,
    RunDispatcher,
    RunDispatcherIntegrityError,
)
from .run_store import (
    FilesystemRunStore,
    RunManifestHeader,
    RunStoreIntegrityError,
    VerifiedRunArtifact,
    VerifiedRunArtifactFile,
)


logger = logging.getLogger(__name__)


class RunAdmissionError(RuntimeError):
    """Closed admission rejection safe to project onto a transport."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedSimpleSkill:
    skill_id: str
    resource_request: ExecutionResourceRequest
    skill_revision: Mapping[str, str]
    registry_snapshot: RegistrySnapshot | None


class SimpleSkillAuthority(Protocol):
    async def resolve(self, skill_id: str) -> ResolvedSimpleSkill: ...


class ResourceScheduler(Protocol):
    budget: ExecutionResourceBudget

    def reserve(self, request: ExecutionResourceRequest): ...

    async def quarantine(self, lease: ResourceLease) -> None: ...

    async def quarantine_unknown_owner(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SimpleSkillExecutionContext:
    run_id: str
    assignment_id: str
    manifest_ref: str
    submission: SimpleSkillRunSubmission
    resolved_skill: ResolvedSimpleSkill
    artifacts_dir: Path
    temporary_dir: Path
    resource_lease: ResourceLease
    execution_reference_type: str
    execution_reference: str


SimpleSkillExecutor = Callable[[SimpleSkillExecutionContext], Awaitable[SkillRunResult]]
_EffectResult = TypeVar("_EffectResult")


@dataclass(frozen=True, slots=True)
class RunSubmissionResult:
    acceptance_status: RunAcceptanceStatus
    receipt: RunRecord | None = None
    code: str = ""


@dataclass(frozen=True, slots=True)
class RunCancelResult:
    changed: bool
    code: str
    receipt: RunRecord


@dataclass(frozen=True, slots=True)
class LocalVerifiedSkillOutput:
    """Local-only verified paths; never a Desktop or remote wire model."""

    output_dir: str
    readme_path: str | None = None
    notebook_path: str | None = None


@dataclass(frozen=True, slots=True)
class SimpleSkillRunTerminalResult:
    """Pure verified terminal projection for local Runtime adapters."""

    receipt: RunRecord
    skill_id: str
    output: LocalVerifiedSkillOutput | None = None

    def __post_init__(self) -> None:
        terminal = self.receipt.status in {
            "succeeded",
            "failed",
            "canceled",
            "interrupted",
        }
        if not terminal:
            raise ValueError("terminal result requires a terminal Receipt")
        if (self.receipt.status == "succeeded") != (self.output is not None):
            raise ValueError("terminal result/output invariant violated")

    @property
    def success(self) -> bool:
        return self.receipt.status == "succeeded"


class RunTerminalResultPending(RuntimeError):
    """Raised when a caller requests a terminal projection too early."""


class RunTerminalProjectionIntegrityError(ControlIntegrityError):
    """Closed, content-free terminal observation failure."""

    def __init__(self) -> None:
        super().__init__("terminal_result_integrity_error")
        self.code = "terminal_result_integrity_error"


class RunTerminalWaitBackpressure(RuntimeError):
    """Raised when the bounded terminal-wait observer capacity is full."""


class RunTerminalResultUnavailable(RuntimeError):
    """Raised when Runtime shutdown leaves a Receipt nonterminal."""


class RunReceiptProjectionIntegrityError(ControlIntegrityError):
    """Closed, content-free failure to bind a Receipt to its Manifest header."""

    def __init__(self) -> None:
        super().__init__("run_receipt_projection_integrity_error")
        self.code = "run_receipt_projection_integrity_error"


class RunRevisionWaitBackpressure(RuntimeError):
    """Raised when bounded lifecycle-observer capacity is exhausted."""

    def __init__(self) -> None:
        super().__init__("revision_wait_backpressure")
        self.code = "revision_wait_backpressure"


class RunRevisionWaitUnavailable(RuntimeError):
    """Raised when Runtime shutdown detaches a revision observer."""

    def __init__(self) -> None:
        super().__init__("runtime_closed")
        self.code = "runtime_closed"


class RunArtifactProjectionIntegrityError(ControlIntegrityError):
    """Closed, content-free successful-Run artifact verification failure."""

    def __init__(self) -> None:
        super().__init__("run_artifact_integrity_error")
        self.code = "run_artifact_integrity_error"


class RunArtifactsUnavailable(RuntimeError):
    """Closed lifecycle rejection for a Run without successful artifacts."""

    def __init__(self, code: str = "run_artifacts_unavailable") -> None:
        super().__init__(code)
        self.code = code


class RunArtifactReadBackpressure(RuntimeError):
    """Raised when bounded verified-reader capacity is exhausted."""

    def __init__(self) -> None:
        super().__init__("artifact_read_backpressure")
        self.code = "artifact_read_backpressure"


class RunArtifactNotFound(KeyError):
    """Content-free missing inventory item result."""

    def __init__(self) -> None:
        super().__init__("run_artifact_not_found")
        self.code = "run_artifact_not_found"


@dataclass(frozen=True, slots=True)
class RunVerifiedArtifactPage:
    """Bounded successful-Run artifact page with no local filesystem paths."""

    receipt: RunRecord
    skill_id: str
    artifacts: tuple[VerifiedRunArtifact, ...]
    total: int
    next_cursor: str | None = None


class RunVerifiedArtifactReader:
    """Async Runtime-owned reader over one already-verified Store descriptor."""

    def __init__(
        self,
        *,
        runtime: "RunRuntime",
        receipt: RunRecord,
        skill_id: str,
        opened: VerifiedRunArtifactFile,
    ) -> None:
        self.receipt = receipt
        self.skill_id = skill_id
        self.artifact = opened.artifact
        self._runtime = runtime
        self._opened = opened
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def read_chunk(
        self,
        *,
        offset: int,
        max_bytes: int = 1024 * 1024,
    ) -> bytes:
        if self._closed:
            raise RunArtifactsUnavailable("artifact_reader_closed")
        try:
            return await asyncio.to_thread(
                self._opened.read_chunk,
                offset=offset,
                max_bytes=max_bytes,
            )
        except (RunStoreIntegrityError, OSError):
            raise RunArtifactProjectionIntegrityError() from None

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._opened.close()
            finally:
                await self._runtime._release_artifact_reader(self)


@dataclass(frozen=True, slots=True)
class _AcceptedSimpleSkillRun:
    run_id: str
    manifest_ref: str
    submission: SimpleSkillRunSubmission
    resolved_skill: ResolvedSimpleSkill


class RegistrySimpleSkillAuthority:
    """Adapter from the current immutable Skill Registry publication."""

    async def resolve(self, skill_id: str) -> ResolvedSimpleSkill:
        snapshot = ensure_registry_loaded().snapshot()
        info = snapshot.skills.get(skill_id)
        if info is None:
            raise RunAdmissionError("skill_not_found")
        if (
            skill_id not in snapshot.canonical_aliases
            or str(info.get("alias") or "") != skill_id
        ):
            raise RunAdmissionError("skill_not_canonical")
        if str(info.get("type") or "leaf") != "leaf":
            raise RunAdmissionError("run_kind_not_supported")
        if str(info.get("lifecycle_status") or "") == "deprecated":
            raise RunAdmissionError("skill_deprecated")
        if not info.get("demo_args"):
            raise RunAdmissionError("skill_demo_not_supported")
        raw_resources = info.get("compute_resources")
        if not isinstance(raw_resources, Mapping) or not raw_resources:
            raise RunAdmissionError("resource_contract_missing")
        try:
            resource_request = ExecutionResourceRequest.from_mapping(raw_resources)
            revision = await asyncio.to_thread(snapshot.skill_revision, skill_id)
        except RunAdmissionError:
            raise
        except Exception as exc:
            raise RunAdmissionError("skill_authority_unavailable") from exc
        return ResolvedSimpleSkill(
            skill_id=skill_id,
            resource_request=resource_request,
            skill_revision=revision,
            registry_snapshot=snapshot,
        )


async def _shared_skill_executor(
    context: SimpleSkillExecutionContext,
) -> SkillRunResult:
    from omicsclaw.skill.runner import arun_skill

    resource_env = dict(context.resource_lease.environment)
    resource_env["TMPDIR"] = str(context.temporary_dir)
    return await arun_skill(
        context.resolved_skill.skill_id,
        output_dir=str(context.artifacts_dir),
        demo=True,
        extra_args=[],
        resource_env=resource_env,
        project_id="",
        project_name="",
        _registry_snapshot=context.resolved_skill.registry_snapshot,
        _expected_skill_revision=context.resolved_skill.skill_revision,
        _trusted_resource_temp_dir=str(context.temporary_dir),
        _allow_adaptive_environment=False,
        _require_process_tree_proof=True,
        _governed_execution_reference=context.execution_reference,
    )


class RunRuntime:
    """Canonical Simple Skill Run facade used by every V1 Surface Adapter."""

    def __init__(
        self,
        *,
        repository: ControlStateRepository,
        run_store: FilesystemRunStore,
        dispatcher: RunDispatcher,
        resource_scheduler: ResourceScheduler,
        skill_authority: SimpleSkillAuthority | None = None,
        skill_executor: SimpleSkillExecutor | None = None,
        max_terminal_waiters: int = 32,
        max_revision_waiters: int = 64,
        max_artifact_readers: int = 8,
    ) -> None:
        for name, value in (
            ("max_terminal_waiters", max_terminal_waiters),
            ("max_revision_waiters", max_revision_waiters),
            ("max_artifact_readers", max_artifact_readers),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be positive")
        self.repository = repository
        self.run_store = run_store
        self.dispatcher = dispatcher
        self.resource_scheduler = resource_scheduler
        self.skill_authority = skill_authority or RegistrySimpleSkillAuthority()
        self.skill_executor = skill_executor or _shared_skill_executor
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[RunStartupReconciliationResult] | None = None
        self._recovery_result = RunStartupReconciliationResult(())
        self._terminal_condition = asyncio.Condition()
        self._active_terminal_waiters = 0
        self._max_terminal_waiters = max_terminal_waiters
        self._revision_condition = asyncio.Condition()
        self._active_revision_waiters = 0
        self._max_revision_waiters = max_revision_waiters
        self._artifact_condition = asyncio.Condition()
        self._active_artifact_readers = 0
        self._max_artifact_readers = max_artifact_readers
        self._artifact_readers: set[RunVerifiedArtifactReader] = set()
        self._terminal_projection_tasks: dict[
            str, asyncio.Task[SimpleSkillRunTerminalResult]
        ] = {}

    @classmethod
    def for_local_surface(
        cls,
        *,
        repository: ControlStateRepository,
        output_root: str | Path,
        resource_budget: ExecutionResourceBudget,
        max_buffered_runs: int,
        max_active_runs: int,
    ) -> "RunRuntime":
        return cls(
            repository=repository,
            run_store=FilesystemRunStore(output_root),
            dispatcher=RunDispatcher(
                max_buffered_runs=max_buffered_runs,
                max_active_runs=max_active_runs,
            ),
            resource_scheduler=get_process_resource_scheduler(
                output_root,
                budget=resource_budget,
            ),
            max_terminal_waiters=max_buffered_runs + max_active_runs,
            max_revision_waiters=max_buffered_runs + max_active_runs,
            max_artifact_readers=max_buffered_runs + max_active_runs,
        )

    @property
    def ready(self) -> bool:
        return (
            self._started
            and not self._closing
            and not self._closed
            and not self._recovery_result.unconfirmed_run_ids
            and self.dispatcher.ready
        )

    @property
    def lifecycle_ready(self) -> bool:
        """Whether duplicate, observation, and cancel Interfaces remain usable."""

        return self._started and not self._closing and not self._closed

    async def start(self) -> RunStartupReconciliationResult:
        """Prove inherited owners stopped before opening an empty FIFO."""

        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("closed RunRuntime cannot start")
            if self._started:
                return RunStartupReconciliationResult(
                    (),
                    self._recovery_result.unconfirmed_run_ids,
                )
            reconciled = await self._reconcile_nonterminal_run_owners()
            await self._audit_terminal_manifest_receipts()
            self._recovery_result = reconciled
            if not reconciled.unconfirmed_run_ids:
                await self.dispatcher.start(self._execute_accepted_run)
            self._started = True
            return reconciled

    async def close(self) -> RunStartupReconciliationResult:
        """Stop live executors before releasing their Resource Leases."""

        async with self._lifecycle_lock:
            if self._closed:
                return self._recovery_result
            task = self._close_task
            if task is None or (task.done() and not self._closed):
                if task is not None:
                    try:
                        task.exception()
                    except (asyncio.CancelledError, Exception):
                        pass
                self._closing = True
                task = asyncio.create_task(
                    self._close_lifecycle(),
                    name="omicsclaw-run-runtime-close",
                )
                self._close_task = task
        return await self._await_close_lifecycle(task)

    async def _close_lifecycle(self) -> RunStartupReconciliationResult:
        try:
            await self.dispatcher.close()
            reconciled = await self._reconcile_nonterminal_run_owners()
            self._recovery_result = reconciled
            await self._notify_terminal_waiters()
            await self._close_artifact_readers()
            await self._finalize_terminal_observer_shutdown()
            return reconciled
        except BaseException:
            # Keep lifecycle admission closed, but let a later close() retry an
            # unexpected failed reconciliation instead of asserting completion.
            await self._notify_terminal_waiters()
            await self._close_artifact_readers()
            raise

    @staticmethod
    async def _await_close_lifecycle(
        task: asyncio.Task[RunStartupReconciliationResult],
    ) -> RunStartupReconciliationResult:
        caller_canceled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    result = task.result()
                    break
                caller_canceled = True
        if caller_canceled:
            raise asyncio.CancelledError
        return result

    async def _audit_terminal_manifest_receipts(self) -> None:
        """Observe assigned terminal cross-Store drift without repairing either side."""

        for observation in self.repository.list_terminal_assigned_run_observations():
            assignment = observation.assignment
            assert assignment is not None
            try:
                report = await self._verified_manifest_terminal_report(
                    run_id=observation.receipt.run_id,
                    manifest_ref=observation.receipt.manifest_ref,
                    assignment_id=assignment.assignment_id,
                )
            except RunIntegrityIncidentError:
                continue
            except Exception:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH),
                    evidence_code=(
                        RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID
                    ),
                )
                continue
            if report is None:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH),
                    evidence_code=(
                        RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID
                    ),
                )
                continue
            if (
                report.terminal_status != observation.receipt.status
                or report.terminal_code != observation.receipt.terminal_code
            ):
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH),
                    evidence_code=(RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT),
                )

    async def _reconcile_nonterminal_run_owners(
        self,
    ) -> RunStartupReconciliationResult:
        """Stop each durable owner; unknown ownership keeps global admission shut."""

        for observation in self.repository.list_nonterminal_run_observations():
            assignment = observation.assignment
            if assignment is None:
                continue
            if (
                assignment.execution_reference_type is None
                or assignment.execution_reference is None
            ):
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(
                        RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
                    ),
                    evidence_code=(
                        RunIntegrityEvidenceCode.EXECUTION_REFERENCE_MISSING
                    ),
                )
                logger.error(
                    "Run recovery blocked by missing execution owner: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            try:
                await reconcile_governed_process_tree(
                    assignment.execution_reference_type,
                    assignment.execution_reference,
                )
            except ProcessTreeStopUnconfirmed:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(
                        RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
                    ),
                    evidence_code=(
                        RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED
                    ),
                )
                logger.error(
                    "Run recovery could not confirm execution owner: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            try:
                report = await self._verified_manifest_terminal_report(
                    run_id=observation.receipt.run_id,
                    manifest_ref=observation.receipt.manifest_ref,
                    assignment_id=assignment.assignment_id,
                )
            except RunIntegrityIncidentError:
                logger.error(
                    "Run recovery found conflicting Manifest evidence: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            except Exception:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH),
                    evidence_code=(
                        RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID
                    ),
                )
                logger.error(
                    "Run recovery could not verify Manifest evidence: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            if report is None:
                try:
                    await self._run_store_effect(
                        self.run_store.commit_stop,
                        observation.receipt.manifest_ref,
                        terminal_status="interrupted",
                        terminal_code="control_plane_restarted",
                        assignment_id=assignment.assignment_id,
                        propagate_cancellation=False,
                    )
                    await self._run_store_effect(
                        self.run_store.verify_stop,
                        observation.receipt.manifest_ref,
                        terminal_status="interrupted",
                        terminal_code="control_plane_restarted",
                        assignment_id=assignment.assignment_id,
                        propagate_cancellation=False,
                    )
                except Exception:
                    self._record_integrity_incident(
                        run_id=observation.receipt.run_id,
                        assignment_id=assignment.assignment_id,
                        incident_type=(
                            RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED
                        ),
                        evidence_code=(
                            RunIntegrityEvidenceCode.RECOVERY_TERMINAL_REPORT_REJECTED
                        ),
                    )
                    logger.error(
                        "Run recovery could not commit stop evidence: run_id=%s",
                        observation.receipt.run_id,
                    )
                    continue
                report = RunReport(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    terminal_status="interrupted",
                    terminal_code="control_plane_restarted",
                )
            try:
                terminalized = await self._apply_run_report_with_retry(report)
            except RunIntegrityIncidentError:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH),
                    evidence_code=(RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT),
                )
                logger.error(
                    "Run recovery found terminal Receipt conflict: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            except Exception:
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(
                        RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED
                    ),
                    evidence_code=(
                        RunIntegrityEvidenceCode.RECOVERY_TERMINAL_TRANSACTION_FAILED
                    ),
                )
                logger.error(
                    "Run recovery could not commit terminal Receipt: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
            if not terminalized.changed and not self._report_already_applied(
                observation.receipt.run_id,
                terminalized.code,
                terminal_status=report.terminal_status,
                terminal_code=report.terminal_code,
            ):
                self._record_integrity_incident(
                    run_id=observation.receipt.run_id,
                    assignment_id=assignment.assignment_id,
                    incident_type=(
                        RunIntegrityIncidentType.RECOVERY_TERMINAL_COMMIT_FAILED
                    ),
                    evidence_code=(
                        RunIntegrityEvidenceCode.RECOVERY_TERMINAL_REPORT_REJECTED
                    ),
                )
                logger.error(
                    "Run recovery terminal report was rejected: run_id=%s",
                    observation.receipt.run_id,
                )
                continue
        result = self.repository.reconcile_nonterminal_runs()
        if result.unconfirmed_run_ids:
            await self.resource_scheduler.quarantine_unknown_owner()
        return result

    async def submit(
        self,
        submission: SimpleSkillRunSubmission,
    ) -> RunSubmissionResult:
        self._require_lifecycle_ready()
        fingerprint_version, fingerprint_sha256, _ = canonical_run_fingerprint(
            submission
        )
        inspection = self.repository.inspect_run_submission(
            run_submission_id=submission.run_submission_id,
            fingerprint_version=fingerprint_version,
            fingerprint_sha256=fingerprint_sha256,
        )
        if inspection.state != "novel":
            return self._inspection_result(inspection)
        try:
            guard = self.dispatcher.admission_guard(submission.run_submission_id)
            async with guard:
                inspection = self.repository.inspect_run_submission(
                    run_submission_id=submission.run_submission_id,
                    fingerprint_version=fingerprint_version,
                    fingerprint_sha256=fingerprint_sha256,
                )
                if inspection.state != "novel":
                    return self._inspection_result(inspection)

                try:
                    resolved = await self.skill_authority.resolve(submission.skill_id)
                except RunAdmissionError as exc:
                    return RunSubmissionResult(
                        RunAcceptanceStatus.REJECTED, code=exc.code
                    )
                if resolved.resource_request != submission.resource_request:
                    return RunSubmissionResult(
                        RunAcceptanceStatus.REJECTED,
                        code="resource_contract_mismatch",
                    )
                if not governed_process_tree_supported():
                    return RunSubmissionResult(
                        RunAcceptanceStatus.REJECTED,
                        code="executor_isolation_unavailable",
                    )
                if not self.resource_scheduler.budget.accommodates(
                    submission.resource_request
                ):
                    return RunSubmissionResult(
                        RunAcceptanceStatus.REJECTED,
                        code="resource_unsupported",
                    )
                if not getattr(self.resource_scheduler, "ready", True):
                    return RunSubmissionResult(
                        RunAcceptanceStatus.REJECTED,
                        code="control_not_ready",
                    )
                return await self._accept_novel(
                    submission,
                    resolved,
                    fingerprint_version=fingerprint_version,
                    fingerprint_sha256=fingerprint_sha256,
                )
        except RunDispatcherIntegrityError as exc:
            # A concurrent direct Repository winner remains observable even if
            # bounded novel admission became unavailable in the meantime.
            inspection = self.repository.inspect_run_submission(
                run_submission_id=submission.run_submission_id,
                fingerprint_version=fingerprint_version,
                fingerprint_sha256=fingerprint_sha256,
            )
            if inspection.state != "novel":
                return self._inspection_result(inspection)
            code = (
                "admission_contention"
                if "guards are exhausted" in str(exc)
                else "control_not_ready"
            )
            return RunSubmissionResult(RunAcceptanceStatus.REJECTED, code=code)

    async def build_simple_skill_demo_submission(
        self,
        *,
        run_submission_id: str,
        skill_id: str,
        scope: RunScope | None = None,
    ) -> SimpleSkillRunSubmission:
        """Resolve one CLI-style demo intent through Backend Skill authority.

        The returned immutable submission can be retried without re-reading the
        Registry, preserving its exact fingerprint and duplicate semantics.
        """

        self._require_lifecycle_ready()
        resolved = await self.skill_authority.resolve(skill_id)
        return SimpleSkillRunSubmission(
            run_submission_id=run_submission_id,
            scope=UnassignedScope() if scope is None else scope,
            skill_id=resolved.skill_id,
            resource_request=resolved.resource_request,
        )

    def resolve_cli_navigation_scope(
        self,
        project_hint: str | None,
    ) -> RunScope:
        """Turn a non-authoritative CLI pointer into a Control-proven Scope.

        Missing, malformed, stale, and archived navigation hints deliberately
        converge on Unassigned.  Storage and integrity failures propagate so a
        Surface cannot silently change Scope when Control is unavailable.
        Final Run admission revalidates an active Project transactionally.
        """

        self._require_lifecycle_ready()
        try:
            scope = ProjectScope(project_hint)
        except ValueError:
            return UnassignedScope()
        try:
            project = self.repository.get_project(scope.project_id)
        except KeyError:
            return UnassignedScope()
        if project.project_id != scope.project_id:
            raise ControlIntegrityError("Control Project identity mismatch")
        if project.lifecycle == "active":
            return scope
        if project.lifecycle == "archived":
            return UnassignedScope()
        raise ControlIntegrityError("Control Project lifecycle is invalid")

    def get_receipt(self, run_id: str) -> RunObservationSnapshot:
        self._require_observation_ready()
        return self.repository.get_run_observation(run_id)

    def list_receipts(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunObservationPage:
        """Pure bounded durable observation; never touches Dispatcher or Store."""

        self._require_observation_ready()
        return self.repository.list_run_observations(
            status=status,
            cursor=cursor,
            limit=limit,
        )

    def get_receipt_skill_id(self, run_id: str) -> str:
        """Project only the verified Skill identity for an existing Receipt."""

        self._require_observation_ready()
        observation = self.repository.get_run_observation(run_id)
        receipt = observation.receipt
        try:
            projection = self.run_store.project_receipt_header(
                receipt.manifest_ref,
                run_id=receipt.run_id,
                run_kind=receipt.run_kind,
                scope_kind=receipt.scope_kind,
                project_id=receipt.project_id,
            )
        except Exception:
            raise RunReceiptProjectionIntegrityError() from None
        return projection.skill_id

    async def wait_for_receipt_revision(
        self,
        run_id: str,
        *,
        after_revision: int,
    ) -> RunObservationSnapshot:
        """Wait for one newer durable revision without owning or canceling the Run.

        A terminal Receipt at exactly ``after_revision`` returns immediately so
        an SSE Adapter can emit its snapshot and close rather than wait forever.
        Cancellation only releases this bounded observer slot.
        """

        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise ValueError("after_revision must be a non-negative integer")
        self._require_observation_ready()
        terminal_statuses = {"succeeded", "failed", "canceled", "interrupted"}

        async with self._revision_condition:
            observation = self.repository.get_run_observation(run_id)
            current_revision = observation.receipt.revision
            if after_revision > current_revision:
                raise ValueError("Run revision cursor is ahead of the Receipt")
            if (
                current_revision > after_revision
                or observation.receipt.status in terminal_statuses
            ):
                return observation
            if self._closing or self._closed:
                raise RunRevisionWaitUnavailable()
            if self._active_revision_waiters >= self._max_revision_waiters:
                raise RunRevisionWaitBackpressure()
            self._active_revision_waiters += 1

        try:
            while True:
                async with self._revision_condition:
                    observation = self.repository.get_run_observation(run_id)
                    current_revision = observation.receipt.revision
                    if current_revision > after_revision:
                        return observation
                    if current_revision < after_revision:
                        raise ControlIntegrityError("Run Receipt revision regressed")
                    if observation.receipt.status in terminal_statuses:
                        return observation
                    if self._closing or self._closed:
                        raise RunRevisionWaitUnavailable()
                    await self._revision_condition.wait()
        finally:
            async with self._revision_condition:
                self._active_revision_waiters -= 1
                self._revision_condition.notify_all()

    async def list_verified_artifacts(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunVerifiedArtifactPage:
        """Return a bounded page after full Receipt/Manifest/inventory proof."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("artifact page limit must be between 1 and 100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("artifact cursor must be a non-empty relative path")
        self._require_observation_ready()
        observation = self.repository.get_run_observation(run_id)
        receipt = observation.receipt
        if receipt.status != "succeeded":
            code = (
                "run_artifacts_not_ready"
                if receipt.status in {"queued", "running", "cancel_requested"}
                else "run_artifacts_unavailable"
            )
            raise RunArtifactsUnavailable(code)
        assignment = observation.assignment
        if assignment is None:
            raise RunArtifactProjectionIntegrityError()
        try:
            inventory = await asyncio.to_thread(
                self.run_store.project_verified_artifacts,
                receipt.manifest_ref,
                run_id=receipt.run_id,
                run_kind=receipt.run_kind,
                scope_kind=receipt.scope_kind,
                project_id=receipt.project_id,
                assignment_id=assignment.assignment_id,
                terminal_status=receipt.status,
                terminal_code=receipt.terminal_code,
            )
        except Exception:
            raise RunArtifactProjectionIntegrityError() from None

        start = 0
        if cursor is not None:
            for index, artifact in enumerate(inventory.artifacts):
                if artifact.relative_path == cursor:
                    start = index + 1
                    break
            else:
                raise ValueError("invalid artifact cursor")
        visible = inventory.artifacts[start : start + limit]
        has_more = start + limit < len(inventory.artifacts)
        next_cursor = visible[-1].relative_path if has_more and visible else None
        return RunVerifiedArtifactPage(
            receipt=receipt,
            skill_id=inventory.skill_id,
            artifacts=visible,
            total=len(inventory.artifacts),
            next_cursor=next_cursor,
        )

    async def open_verified_artifact(
        self,
        run_id: str,
        relative_path: str,
    ) -> RunVerifiedArtifactReader:
        """Open one deeply verified artifact without a path-reopen TOCTOU."""

        self._require_observation_ready()
        observation = self.repository.get_run_observation(run_id)
        receipt = observation.receipt
        if receipt.status != "succeeded":
            code = (
                "run_artifacts_not_ready"
                if receipt.status in {"queued", "running", "cancel_requested"}
                else "run_artifacts_unavailable"
            )
            raise RunArtifactsUnavailable(code)
        assignment = observation.assignment
        if assignment is None:
            raise RunArtifactProjectionIntegrityError()
        async with self._artifact_condition:
            if self._closing or self._closed:
                raise RunArtifactsUnavailable("runtime_closed")
            if self._active_artifact_readers >= self._max_artifact_readers:
                raise RunArtifactReadBackpressure()
            self._active_artifact_readers += 1

        opened: VerifiedRunArtifactFile | None = None
        try:
            skill_id, opened = await self._open_verified_store_artifact(
                receipt=receipt,
                assignment_id=assignment.assignment_id,
                relative_path=relative_path,
            )
            reader = RunVerifiedArtifactReader(
                runtime=self,
                receipt=receipt,
                skill_id=skill_id,
                opened=opened,
            )
            async with self._artifact_condition:
                if self._closing or self._closed:
                    opened.close()
                    opened = None
                    self._active_artifact_readers -= 1
                    self._artifact_condition.notify_all()
                    raise RunArtifactsUnavailable("runtime_closed")
                self._artifact_readers.add(reader)
            return reader
        except KeyError:
            if opened is not None:
                opened.close()
            await self._release_artifact_reservation()
            raise RunArtifactNotFound() from None
        except ValueError:
            if opened is not None:
                opened.close()
            await self._release_artifact_reservation()
            raise
        except RunArtifactsUnavailable:
            raise
        except asyncio.CancelledError:
            if opened is not None:
                opened.close()
            await self._release_artifact_reservation()
            raise
        except BaseException:
            if opened is not None:
                opened.close()
            await self._release_artifact_reservation()
            raise RunArtifactProjectionIntegrityError() from None

    async def get_terminal_result(
        self,
        run_id: str,
    ) -> SimpleSkillRunTerminalResult:
        """Purely verify and project one terminal Run without recording drift."""

        if self._closed:
            raise RunTerminalResultUnavailable("runtime_closed")
        self._require_observation_ready()
        async with self._terminal_condition:
            if self._closed:
                raise RunTerminalResultUnavailable("runtime_closed")
            task = self._terminal_projection_tasks.get(run_id)
            if task is None:
                task = asyncio.create_task(
                    self._project_terminal_result(run_id),
                    name=f"omicsclaw-run-terminal-projection-{run_id}",
                )
                self._terminal_projection_tasks[run_id] = task
                task.add_done_callback(
                    lambda done, projected_run_id=run_id: asyncio.create_task(
                        self._forget_terminal_projection(projected_run_id, done)
                    )
                )
        return await asyncio.shield(task)

    async def _project_terminal_result(
        self,
        run_id: str,
    ) -> SimpleSkillRunTerminalResult:
        observation = self.repository.get_run_observation(run_id)
        receipt = observation.receipt
        if receipt.status not in {"succeeded", "failed", "canceled", "interrupted"}:
            raise RunTerminalResultPending(run_id)
        assignment = observation.assignment
        try:
            projected = await asyncio.to_thread(
                self.run_store.project_verified_terminal,
                receipt.manifest_ref,
                run_id=receipt.run_id,
                run_kind=receipt.run_kind,
                scope_kind=receipt.scope_kind,
                project_id=receipt.project_id,
                assignment_id=(
                    assignment.assignment_id if assignment is not None else None
                ),
                terminal_status=receipt.status,
                terminal_code=receipt.terminal_code,
            )
        except Exception:
            # Run Store validation may include scientific filenames or local
            # paths. The Runtime boundary exposes only one content-free code.
            raise RunTerminalProjectionIntegrityError() from None
        output = (
            LocalVerifiedSkillOutput(
                output_dir=projected.output.output_dir,
                readme_path=projected.output.readme_path,
                notebook_path=projected.output.notebook_path,
            )
            if projected.output is not None
            else None
        )
        return SimpleSkillRunTerminalResult(
            receipt=receipt,
            skill_id=projected.skill_id,
            output=output,
        )

    async def wait_for_terminal_result(
        self,
        run_id: str,
    ) -> SimpleSkillRunTerminalResult:
        """Bounded wait; cancellation detaches observation and never cancels Run."""

        if self._closed:
            raise RunTerminalResultUnavailable("runtime_closed")
        self._require_observation_ready()
        observation = self.repository.get_run_observation(run_id)
        if observation.receipt.status in {
            "succeeded",
            "failed",
            "canceled",
            "interrupted",
        }:
            return await self.get_terminal_result(run_id)

        async with self._terminal_condition:
            observation = self.repository.get_run_observation(run_id)
            if observation.receipt.status in {
                "succeeded",
                "failed",
                "canceled",
                "interrupted",
            }:
                terminal_now = True
            else:
                terminal_now = False
                if self._closing or self._closed:
                    raise RunTerminalResultUnavailable("runtime_closed")
                if self._active_terminal_waiters >= self._max_terminal_waiters:
                    raise RunTerminalWaitBackpressure("wait_backpressure")
                self._active_terminal_waiters += 1
        if terminal_now:
            return await self.get_terminal_result(run_id)

        try:
            while True:
                async with self._terminal_condition:
                    observation = self.repository.get_run_observation(run_id)
                    if observation.receipt.status in {
                        "succeeded",
                        "failed",
                        "canceled",
                        "interrupted",
                    }:
                        break
                    if self._closing or self._closed:
                        raise RunTerminalResultUnavailable("runtime_closed")
                    await self._terminal_condition.wait()
            return await self.get_terminal_result(run_id)
        finally:
            async with self._terminal_condition:
                self._active_terminal_waiters -= 1
                self._terminal_condition.notify_all()

    def list_integrity_incidents(
        self,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunIntegrityIncidentPage:
        """Pure durable observation; never touches execution or Run Store state."""

        self._require_observation_ready()
        return self.repository.list_run_integrity_incidents(
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )

    async def cancel(self, run_id: str) -> RunCancelResult:
        self._require_lifecycle_ready()
        state = self.repository.request_run_cancel(run_id)
        if state.code == "run_not_found":
            raise KeyError(run_id)
        snapshot = self.repository.get_run_observation(run_id)
        if state.changed:
            await self._notify_revision_waiters()
        if snapshot.receipt.status in {
            "succeeded",
            "failed",
            "canceled",
            "interrupted",
        }:
            await self._notify_terminal_waiters()
        should_signal = state.code in {
            "canceled",
            "cancel_requested",
            "already_cancel_requested",
        } or (
            state.code == "run_terminal"
            and snapshot.receipt.status == "canceled"
            and snapshot.assignment is None
        )
        if should_signal:
            signal_task = asyncio.create_task(
                self.dispatcher.cancel(run_id, reason="owner"),
                name=f"omicsclaw-run-cancel-{run_id}",
            )
            live_result = await self._await_cancel_signal(signal_task)
            if (
                live_result == "not_live"
                and snapshot.receipt.status == "cancel_requested"
            ):
                latest = self.repository.get_run(run_id)
                if (
                    latest.status == "cancel_requested"
                    and run_id not in self._recovery_result.unconfirmed_run_ids
                ):
                    if snapshot.assignment is not None:
                        self._record_integrity_incident(
                            run_id=run_id,
                            assignment_id=snapshot.assignment.assignment_id,
                            incident_type=(
                                RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
                            ),
                            evidence_code=(
                                RunIntegrityEvidenceCode.DISPATCHER_OWNER_MISSING
                            ),
                        )
                    raise ControlIntegrityError(
                        "cancel-requested Run has no live Dispatcher owner"
                    )
        code = {
            "canceled": "canceled_before_assignment",
            "cancel_requested": "cancel_requested",
            "already_cancel_requested": "cancel_already_requested",
            "run_terminal": "already_terminal",
        }.get(state.code, state.code)
        return RunCancelResult(state.changed, code, snapshot.receipt)

    async def _accept_novel(
        self,
        submission: SimpleSkillRunSubmission,
        resolved: ResolvedSimpleSkill,
        *,
        fingerprint_version: int,
        fingerprint_sha256: str,
    ) -> RunSubmissionResult:
        async with self.dispatcher.serialize_admission():
            # A direct Repository caller or another future Adapter may have won
            # while current validation ran. Repeat duplicate-first inside the
            # total admission order before reserving any process-local capacity.
            inspection = self.repository.inspect_run_submission(
                run_submission_id=submission.run_submission_id,
                fingerprint_version=fingerprint_version,
                fingerprint_sha256=fingerprint_sha256,
            )
            if inspection.state != "novel":
                return self._inspection_result(inspection)

            planning_intent = self._acceptance_intent(
                submission,
                fingerprint_version=fingerprint_version,
                fingerprint_sha256=fingerprint_sha256,
                manifest_ref="run-store:v1:pending",
            )
            plan = self.repository.plan_run_acceptance(planning_intent)
            if plan.state == "duplicate":
                return RunSubmissionResult(
                    RunAcceptanceStatus.DUPLICATE,
                    self.repository.get_run(plan.run_id),
                )
            if plan.state == "conflict":
                return RunSubmissionResult(
                    RunAcceptanceStatus.CONFLICT,
                    (self.repository.get_run(plan.run_id) if plan.run_id else None),
                    plan.code,
                )
            if plan.state == "rejected":
                return RunSubmissionResult(RunAcceptanceStatus.REJECTED, code=plan.code)
            if not plan.proposed_run_id:
                raise ControlIntegrityError("novel Run plan has no proposed Run ID")

            reservation = await self.dispatcher.try_reserve()
            if reservation is None:
                return RunSubmissionResult(
                    RunAcceptanceStatus.REJECTED, code="run_backpressure"
                )
            manifest_ref: str | None = None
            accepted = False
            try:
                project_name = ""
                if isinstance(submission.scope, ProjectScope):
                    project_name = self.repository.get_project(
                        submission.scope.project_id
                    ).display_name
                provisional = self.run_store.create_header(
                    RunManifestHeader(
                        run_id=plan.proposed_run_id,
                        run_submission_id=submission.run_submission_id,
                        fingerprint_version=fingerprint_version,
                        fingerprint_sha256=fingerprint_sha256,
                        run_kind=submission.run_kind,
                        scope=submission.scope,
                        inputs=submission.inputs,
                        parameters=submission.parameters,
                        resource_contract=submission.resource_contract,
                        skill_revision=resolved.skill_revision,
                    ),
                    project_name=project_name,
                )
                manifest_ref = provisional.manifest_ref
                intent = self._acceptance_intent(
                    submission,
                    fingerprint_version=fingerprint_version,
                    fingerprint_sha256=fingerprint_sha256,
                    manifest_ref=manifest_ref,
                )
                outcome = self.repository.accept_run(
                    intent,
                    proposed_run_id=plan.proposed_run_id,
                )
                if outcome.status is not RunAcceptanceStatus.ACCEPTED:
                    self.run_store.abandon(manifest_ref)
                    await reservation.release()
                    receipt = (
                        self.repository.get_run(outcome.run_id)
                        if outcome.run_id
                        else None
                    )
                    return RunSubmissionResult(outcome.status, receipt, outcome.code)
                accepted = True
                try:
                    self.run_store.mark_accepted(manifest_ref)
                    await reservation.commit(
                        _AcceptedSimpleSkillRun(
                            run_id=outcome.run_id,
                            manifest_ref=manifest_ref,
                            submission=submission,
                            resolved_skill=resolved,
                        )
                    )
                except BaseException:
                    return await self._compensate_enqueue_failure(
                        reservation,
                        outcome.run_id,
                    )
                await self._notify_revision_waiters()
                return RunSubmissionResult(
                    RunAcceptanceStatus.ACCEPTED,
                    self.repository.get_run(outcome.run_id),
                )
            except BaseException:
                if not accepted:
                    if manifest_ref is not None:
                        try:
                            self.run_store.abandon(manifest_ref)
                        finally:
                            await reservation.release()
                    else:
                        await reservation.release()
                raise

    async def _compensate_enqueue_failure(
        self,
        reservation: RunBufferReservation,
        run_id: str,
    ) -> RunSubmissionResult:
        try:
            failed = self.repository.fail_queued_run(
                run_id, terminal_code="submission_failed"
            )
            if not failed.changed and failed.code != "already_terminal":
                raise ControlIntegrityError(
                    "accepted Run enqueue failure could not terminalize"
                )
            await reservation.discard_terminalized()
            await self._notify_terminal_waiters()
        except BaseException:
            await reservation.quarantine()
            raise
        return RunSubmissionResult(
            RunAcceptanceStatus.ACCEPTED,
            self.repository.get_run(run_id),
        )

    async def _execute_accepted_run(self, raw_payload: Any) -> None:
        if not isinstance(raw_payload, _AcceptedSimpleSkillRun):
            raise ControlIntegrityError("Dispatcher produced an invalid Run payload")
        payload = raw_payload
        assignment_id: str | None = None
        success_evidence_committed = False
        try:
            current = self.repository.get_run(payload.run_id)
            if current.status != "queued":
                return
            async with self.resource_scheduler.reserve(
                ResourceTicket(
                    request=payload.submission.resource_request,
                    run_id=payload.run_id,
                )
            ) as lease:
                current = self.repository.get_run(payload.run_id)
                if current.status != "queued":
                    return
                (
                    execution_reference_type,
                    execution_reference,
                ) = new_governed_process_tree_reference()
                assignment = self.repository.assign_run(
                    payload.run_id,
                    executor_kind="local-simple-skill-v1",
                    execution_reference_type=execution_reference_type,
                    execution_reference=execution_reference,
                )
                if assignment.status is not AssignmentStatus.ASSIGNED:
                    # ALREADY_ASSIGNED is observation, never a second start grant.
                    return
                assignment_id = assignment.assignment_id
                await self._notify_revision_waiters()
                temporary = self.run_store.execution_tmp_dir(payload.manifest_ref)
                context = SimpleSkillExecutionContext(
                    run_id=payload.run_id,
                    assignment_id=assignment_id,
                    manifest_ref=payload.manifest_ref,
                    submission=payload.submission,
                    resolved_skill=payload.resolved_skill,
                    artifacts_dir=self.run_store.artifacts_dir(payload.manifest_ref),
                    temporary_dir=temporary,
                    resource_lease=lease,
                    execution_reference_type=execution_reference_type,
                    execution_reference=execution_reference,
                )
                await self._verify_manifest_receipt_binding(
                    self.repository.get_run_observation(payload.run_id)
                )
                ownership_unconfirmed = False
                try:
                    try:
                        result = await self.skill_executor(context)
                    except ProcessTreeStopUnconfirmed:
                        ownership_unconfirmed = True
                        await self.resource_scheduler.quarantine(lease)
                        self._record_integrity_incident(
                            run_id=payload.run_id,
                            assignment_id=assignment_id,
                            incident_type=(
                                RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED
                            ),
                            evidence_code=(
                                RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED
                            ),
                        )
                        raise
                    if result.success:
                        try:
                            await self._run_store_effect(
                                self.run_store.commit_success,
                                payload.manifest_ref,
                                result,
                                assignment_id=assignment_id,
                            )
                            success_evidence_committed = True
                        except Exception:
                            recovered_report = (
                                await self._verified_manifest_terminal_report(
                                    run_id=payload.run_id,
                                    manifest_ref=payload.manifest_ref,
                                    assignment_id=assignment_id,
                                )
                            )
                            if (
                                recovered_report is not None
                                and recovered_report.terminal_status == "succeeded"
                            ):
                                success_evidence_committed = True
                            elif recovered_report is not None:
                                raise self._manifest_incident_error(
                                    run_id=payload.run_id,
                                    assignment_id=assignment_id,
                                    evidence_code=(
                                        RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT
                                    ),
                                )
                            else:
                                await self._record_assigned_failure(
                                    payload,
                                    assignment_id,
                                    terminal_code="completion_commit_failed",
                                    evidence={
                                        "error_kind": "completion_verification_failed"
                                    },
                                )
                                return
                        terminalized = await self._apply_run_report_with_retry(
                            RunReport(
                                run_id=payload.run_id,
                                assignment_id=assignment_id,
                                terminal_status="succeeded",
                                projections=self._load_analysis_lineage_projections(
                                    run_id=payload.run_id,
                                    manifest_ref=payload.manifest_ref,
                                ),
                            )
                        )
                        if (
                            not terminalized.changed
                            and not self._report_already_applied(
                                payload.run_id,
                                terminalized.code,
                                terminal_status="succeeded",
                                terminal_code=None,
                            )
                        ):
                            raise ControlIntegrityError(
                                "verified Run success could not terminalize Receipt"
                            )
                        await self._notify_terminal_waiters()
                        return
                    terminal_code = (
                        "validation_failed"
                        if result.error_kind
                        in {"contract_validator_failed", "output_contract_failed"}
                        else "executor_failed"
                    )
                    await self._record_assigned_failure(
                        payload,
                        assignment_id,
                        terminal_code=terminal_code,
                        evidence={
                            "exit_code": result.adapter_exit_code,
                            "error_kind": result.error_kind,
                        },
                    )
                finally:
                    if not ownership_unconfirmed:
                        shutil.rmtree(temporary, ignore_errors=True)
        except ProcessTreeStopUnconfirmed:
            # The Dispatcher observes the exception and quarantines. Receipt
            # and Manifest deliberately remain nonterminal because no stop
            # evidence exists; the uncertain Lease remains reserved.
            raise
        except asyncio.CancelledError:
            if assignment_id is not None:
                report = await self._verified_manifest_terminal_report(
                    run_id=payload.run_id,
                    manifest_ref=payload.manifest_ref,
                    assignment_id=assignment_id,
                )
                if report is None:
                    reason = await self.dispatcher.cancellation_reason(payload.run_id)
                    terminal_status = "canceled" if reason == "owner" else "interrupted"
                    terminal_code = (
                        "canceled_by_owner"
                        if terminal_status == "canceled"
                        else "execution_interrupted"
                    )
                    try:
                        await self._run_store_effect(
                            self.run_store.commit_stop,
                            payload.manifest_ref,
                            terminal_status=terminal_status,
                            terminal_code=terminal_code,
                            assignment_id=assignment_id,
                            propagate_cancellation=False,
                        )
                    except Exception:
                        # A terminal evidence transaction may have won the race
                        # after the first read.  Reconcile that exact evidence;
                        # never overwrite succeeded/failed proof with a stop.
                        report = await self._verified_manifest_terminal_report(
                            run_id=payload.run_id,
                            manifest_ref=payload.manifest_ref,
                            assignment_id=assignment_id,
                        )
                        if report is None:
                            raise
                    else:
                        await self._run_store_effect(
                            self.run_store.verify_stop,
                            payload.manifest_ref,
                            terminal_status=terminal_status,
                            terminal_code=terminal_code,
                            assignment_id=assignment_id,
                            propagate_cancellation=False,
                        )
                        report = RunReport(
                            run_id=payload.run_id,
                            assignment_id=assignment_id,
                            terminal_status=terminal_status,
                            terminal_code=terminal_code,
                        )
                result = await self._apply_run_report_with_retry(report)
                if not result.changed and not self._report_already_applied(
                    payload.run_id,
                    result.code,
                    terminal_status=report.terminal_status,
                    terminal_code=report.terminal_code,
                ):
                    raise ControlIntegrityError(
                        "canceled worker could not reconcile its terminal Receipt"
                    )
                await self._notify_terminal_waiters()
            raise
        except RunIntegrityIncidentError:
            # The durable incident is the terminal fact for this failed
            # reconciliation attempt. Never manufacture conflicting failure
            # evidence or mutate the Receipt after an integrity rejection.
            raise
        except Exception:
            if assignment_id is not None:
                if success_evidence_committed:
                    # A verified successful Manifest is immutable.  Never
                    # manufacture contradictory failed Control state when its
                    # terminal transaction cannot be committed.
                    raise
                await self._record_assigned_failure(
                    payload,
                    assignment_id,
                    terminal_code="executor_failed",
                    evidence={"error_kind": "executor_adapter_failed"},
                )
            else:
                self.repository.fail_queued_run(
                    payload.run_id, terminal_code="submission_failed"
                )
                await self._notify_terminal_waiters()

    async def _record_assigned_failure(
        self,
        payload: _AcceptedSimpleSkillRun,
        assignment_id: str,
        *,
        terminal_code: str,
        evidence: Mapping[str, Any],
    ) -> None:
        for attempt in range(2):
            try:
                await self._run_store_effect(
                    self.run_store.commit_failure,
                    payload.manifest_ref,
                    terminal_code=terminal_code,
                    execution_evidence=evidence,
                    assignment_id=assignment_id,
                )
                break
            except Exception:
                recovered_report = await self._verified_manifest_terminal_report(
                    run_id=payload.run_id,
                    manifest_ref=payload.manifest_ref,
                    assignment_id=assignment_id,
                )
                if (
                    recovered_report is not None
                    and recovered_report.terminal_status == "failed"
                    and recovered_report.terminal_code == terminal_code
                ):
                    try:
                        await self._run_store_effect(
                            self.run_store.verify_failure,
                            payload.manifest_ref,
                            terminal_code=terminal_code,
                            execution_evidence=evidence,
                            assignment_id=assignment_id,
                        )
                    except Exception as exc:
                        raise self._manifest_incident_error(
                            run_id=payload.run_id,
                            assignment_id=assignment_id,
                            evidence_code=(
                                RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT
                            ),
                        ) from exc
                    break
                if recovered_report is not None:
                    raise self._manifest_incident_error(
                        run_id=payload.run_id,
                        assignment_id=assignment_id,
                        evidence_code=(
                            RunIntegrityEvidenceCode.MANIFEST_TERMINAL_CONFLICT
                        ),
                    )
                if attempt:
                    raise
        terminalized = await self._apply_run_report_with_retry(
            RunReport(
                run_id=payload.run_id,
                assignment_id=assignment_id,
                terminal_status="failed",
                terminal_code=terminal_code,
            )
        )
        if not terminalized.changed and not self._report_already_applied(
            payload.run_id,
            terminalized.code,
            terminal_status="failed",
            terminal_code=terminal_code,
        ):
            raise ControlIntegrityError("failed Run could not terminalize its Receipt")
        await self._notify_terminal_waiters()

    def _record_integrity_incident(
        self,
        *,
        run_id: str,
        assignment_id: str,
        incident_type: RunIntegrityIncidentType,
        evidence_code: RunIntegrityEvidenceCode,
    ):
        return self.repository.record_run_integrity_incident(
            RunIntegrityIncidentIntent(
                run_id=run_id,
                assignment_id=assignment_id,
                incident_type=incident_type,
                evidence_code=evidence_code,
            )
        )

    def _manifest_incident_error(
        self,
        *,
        run_id: str,
        assignment_id: str,
        evidence_code: RunIntegrityEvidenceCode,
    ) -> RunIntegrityIncidentError:
        appended = self._record_integrity_incident(
            run_id=run_id,
            assignment_id=assignment_id,
            incident_type=RunIntegrityIncidentType.MANIFEST_RECEIPT_MISMATCH,
            evidence_code=evidence_code,
        )
        return RunIntegrityIncidentError(
            "Run Manifest and Receipt evidence disagree; "
            f"incident_id={appended.incident.incident_id}",
            incident_id=appended.incident.incident_id,
        )

    async def _verify_manifest_receipt_binding(
        self,
        observation: RunObservationSnapshot,
    ) -> None:
        assignment = observation.assignment
        if assignment is None:
            raise ControlIntegrityError("assigned Run has no Assignment")
        receipt = observation.receipt
        try:
            await self._run_store_effect(
                self.run_store.verify_receipt_binding,
                receipt.manifest_ref,
                run_id=receipt.run_id,
                run_kind=receipt.run_kind,
                scope_kind=receipt.scope_kind,
                project_id=receipt.project_id,
                propagate_cancellation=False,
            )
        except Exception as exc:
            raise self._manifest_incident_error(
                run_id=receipt.run_id,
                assignment_id=assignment.assignment_id,
                evidence_code=(
                    RunIntegrityEvidenceCode.MANIFEST_RECEIPT_BINDING_MISMATCH
                ),
            ) from exc

    @staticmethod
    def _analysis_lineage_projections(
        *,
        receipt: RunRecord,
        manifest: Mapping[str, Any],
        manifest_ref: str,
    ) -> tuple[ProjectionIntentInput, ...]:
        """Freeze one analysis-lineage Project Projection Intent (ADR 0064).

        Attached to the success RunReport so the Intent is inserted in the SAME
        control transaction that terminalizes the Run — archive then observes
        either nonterminal work or terminal work with its complete frozen
        projection authority, never the gap between. The frozen source is the
        immutable Run Manifest; the digest is the canonical analysis-lineage
        derivation the projector re-verifies. Unassigned Runs contribute no
        Project Memory (the repository also drops projections when project_id is
        None), so they freeze nothing.
        """
        if not receipt.project_id:
            return ()
        return (
            ProjectionIntentInput(
                projection_kind=ANALYSIS_LINEAGE_KIND,
                source_store="run",
                source_ref=manifest_ref,
                content_sha256=analysis_lineage_digest(manifest),
            ),
        )

    def _load_analysis_lineage_projections(
        self, *, run_id: str, manifest_ref: str
    ) -> tuple[ProjectionIntentInput, ...]:
        """Load the receipt + Manifest and derive the projection for the live path.

        A read failure PROPAGATES rather than being swallowed. The terminal
        transaction is then not attempted, so the (immutable, already-committed)
        success Manifest stays durable with a still-`running` Receipt, and
        startup recovery re-derives and freezes the Intent via
        `_verified_manifest_terminal_report`. This preserves ADR 0064
        completeness: a project success can never terminalize WITHOUT its Intent.
        Swallowing here would be unrecoverable — a later `apply_run_report`
        returns `already_terminal` without ever inserting the dropped projection.
        """
        receipt = self.repository.get_run(run_id)
        if not receipt.project_id:
            # Unassigned Run: no Project Memory, so short-circuit BEFORE reading
            # the Manifest — an unassigned Run must never fail terminalization on
            # a projection read it does not need.
            return ()
        manifest = self.run_store.read_manifest(manifest_ref)
        return self._analysis_lineage_projections(
            receipt=receipt, manifest=manifest, manifest_ref=manifest_ref
        )

    async def _verified_manifest_terminal_report(
        self,
        *,
        run_id: str,
        manifest_ref: str,
        assignment_id: str,
    ) -> RunReport | None:
        """Translate already-durable Manifest evidence into one fenced report."""

        observation = self.repository.get_run_observation(run_id)
        if (
            observation.assignment is None
            or observation.assignment.assignment_id != assignment_id
            or observation.receipt.manifest_ref != manifest_ref
        ):
            raise ControlIntegrityError(
                "Manifest verification does not match canonical Run Assignment"
            )
        await self._verify_manifest_receipt_binding(observation)
        try:
            manifest = self.run_store.read_manifest(manifest_ref)
            completion = manifest.get("completion")
            if completion is None:
                return None
            if not isinstance(completion, Mapping):
                raise ValueError("invalid completion")
            if completion.get("assignment_id") != assignment_id:
                raise self._manifest_incident_error(
                    run_id=run_id,
                    assignment_id=assignment_id,
                    evidence_code=(
                        RunIntegrityEvidenceCode.MANIFEST_ASSIGNMENT_MISMATCH
                    ),
                )
            completion_kind = completion.get("kind")
            if completion_kind == "succeeded":
                await self._run_store_effect(
                    self.run_store.verify_success,
                    manifest_ref,
                    assignment_id=assignment_id,
                    propagate_cancellation=False,
                )
                return RunReport(
                    run_id=run_id,
                    assignment_id=assignment_id,
                    terminal_status="succeeded",
                    projections=self._analysis_lineage_projections(
                        receipt=observation.receipt,
                        manifest=manifest,
                        manifest_ref=manifest_ref,
                    ),
                )
            terminal_code = completion.get("terminal_code")
            if not isinstance(terminal_code, str):
                raise ValueError("invalid terminal code")
            if completion_kind == "failed":
                evidence = completion.get("execution_evidence")
                if not isinstance(evidence, Mapping):
                    raise ValueError("invalid failure evidence")
                await self._run_store_effect(
                    self.run_store.verify_failure,
                    manifest_ref,
                    terminal_code=terminal_code,
                    execution_evidence=evidence,
                    assignment_id=assignment_id,
                    propagate_cancellation=False,
                )
            elif completion_kind in {"canceled", "interrupted"}:
                await self._run_store_effect(
                    self.run_store.verify_stop,
                    manifest_ref,
                    terminal_status=completion_kind,
                    terminal_code=terminal_code,
                    assignment_id=assignment_id,
                    propagate_cancellation=False,
                )
            else:
                raise ValueError("invalid completion kind")
            return RunReport(
                run_id=run_id,
                assignment_id=assignment_id,
                terminal_status=completion_kind,
                terminal_code=terminal_code,
            )
        except RunIntegrityIncidentError:
            raise
        except Exception as exc:
            raise self._manifest_incident_error(
                run_id=run_id,
                assignment_id=assignment_id,
                evidence_code=RunIntegrityEvidenceCode.MANIFEST_COMPLETION_INVALID,
            ) from exc

    @staticmethod
    async def _run_store_effect(
        effect: Callable[..., _EffectResult],
        *args: Any,
        propagate_cancellation: bool = True,
        **kwargs: Any,
    ) -> _EffectResult:
        """Keep filesystem finalization off-loop without releasing its Lease."""

        task = asyncio.create_task(asyncio.to_thread(effect, *args, **kwargs))
        caller_canceled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    result = task.result()
                    caller_canceled = True
                    break
                caller_canceled = True
        if caller_canceled and propagate_cancellation:
            raise asyncio.CancelledError
        return result

    async def _open_verified_store_artifact(
        self,
        *,
        receipt: RunRecord,
        assignment_id: str,
        relative_path: str,
    ) -> tuple[str, VerifiedRunArtifactFile]:
        """Finish descriptor acquisition on caller cancellation, then close it."""

        task = asyncio.create_task(
            asyncio.to_thread(
                self.run_store.open_verified_artifact,
                receipt.manifest_ref,
                run_id=receipt.run_id,
                run_kind=receipt.run_kind,
                scope_kind=receipt.scope_kind,
                project_id=receipt.project_id,
                assignment_id=assignment_id,
                terminal_status=receipt.status,
                terminal_code=receipt.terminal_code,
                relative_path=relative_path,
            ),
            name=f"omicsclaw-run-artifact-open-{receipt.run_id}",
        )
        caller_canceled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    result = task.result()
                    caller_canceled = True
                    break
                caller_canceled = True
        if caller_canceled:
            result[1].close()
            raise asyncio.CancelledError
        return result

    def _report_already_applied(
        self,
        run_id: str,
        code: str,
        *,
        terminal_status: str,
        terminal_code: str | None,
    ) -> bool:
        if code != "already_terminal":
            return False
        observed = self.repository.get_run(run_id)
        return (
            observed.status == terminal_status
            and observed.terminal_code == terminal_code
        )

    async def _apply_run_report_with_retry(self, report: RunReport):
        """Retry one local idempotent terminal transaction after rollback."""

        for attempt in range(2):
            try:
                return self.repository.apply_run_report(report)
            except RunIntegrityIncidentError:
                # Repository already committed the content-free incident in
                # the rejecting transaction; replay would add no authority.
                raise
            except Exception:
                if attempt:
                    raise
                # The Repository transaction is synchronous and local.  Keep
                # the idempotent retry in the same event-loop turn so a cancel
                # cannot split rollback from the reconciliation attempt.
                continue
        raise AssertionError("unreachable Run report retry state")

    @staticmethod
    async def _await_cancel_signal(task: asyncio.Task[str]) -> str:
        """Finish the durable-to-live cancel effect despite caller cancellation."""

        caller_canceled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                caller_canceled = True
        if caller_canceled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    def _acceptance_intent(
        submission: SimpleSkillRunSubmission,
        *,
        fingerprint_version: int,
        fingerprint_sha256: str,
        manifest_ref: str,
    ) -> RunAcceptanceIntent:
        return RunAcceptanceIntent(
            run_submission_id=submission.run_submission_id,
            fingerprint_version=fingerprint_version,
            fingerprint_sha256=fingerprint_sha256,
            run_kind=submission.run_kind,
            scope_kind=submission.scope.kind,
            project_id=submission.scope.project_id,
            parent_turn_id=None,
            retry_of_run_id=None,
            manifest_ref=manifest_ref,
        )

    def _inspection_result(self, inspection: Any) -> RunSubmissionResult:
        if inspection.state == "duplicate":
            return RunSubmissionResult(
                RunAcceptanceStatus.DUPLICATE,
                self.repository.get_run(inspection.canonical_id),
            )
        if inspection.state == "conflict":
            receipt = (
                self.repository.get_run(inspection.canonical_id)
                if inspection.canonical_id
                else None
            )
            return RunSubmissionResult(
                RunAcceptanceStatus.CONFLICT,
                receipt,
                inspection.code or "run_idempotency_conflict",
            )
        raise ControlIntegrityError("invalid Run idempotency inspection state")

    def _require_lifecycle_ready(self) -> None:
        if not self._started or self._closing or self._closed:
            raise RuntimeError("RunRuntime is not ready")

    def _require_observation_ready(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("RunRuntime observation is not ready")

    async def _notify_terminal_waiters(self) -> None:
        async with self._terminal_condition:
            self._terminal_condition.notify_all()
        await self._notify_revision_waiters()

    async def _notify_revision_waiters(self) -> None:
        async with self._revision_condition:
            self._revision_condition.notify_all()

    async def _release_artifact_reservation(self) -> None:
        async with self._artifact_condition:
            self._active_artifact_readers -= 1
            if self._active_artifact_readers < 0:
                raise ControlIntegrityError("artifact reader capacity underflow")
            self._artifact_condition.notify_all()

    async def _release_artifact_reader(
        self,
        reader: RunVerifiedArtifactReader,
    ) -> None:
        async with self._artifact_condition:
            if reader not in self._artifact_readers:
                return
            self._artifact_readers.remove(reader)
            self._active_artifact_readers -= 1
            if self._active_artifact_readers < 0:
                raise ControlIntegrityError("artifact reader capacity underflow")
            self._artifact_condition.notify_all()

    async def _close_artifact_readers(self) -> None:
        async with self._artifact_condition:
            readers = tuple(self._artifact_readers)
        if readers:
            await asyncio.gather(
                *(reader.aclose() for reader in readers),
                return_exceptions=True,
            )
        async with self._artifact_condition:
            while self._active_artifact_readers:
                await self._artifact_condition.wait()

    async def _forget_terminal_projection(
        self,
        run_id: str,
        task: asyncio.Task[SimpleSkillRunTerminalResult],
    ) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        async with self._terminal_condition:
            if self._terminal_projection_tasks.get(run_id) is task:
                self._terminal_projection_tasks.pop(run_id, None)
            self._terminal_condition.notify_all()

    async def _finalize_terminal_observer_shutdown(self) -> None:
        async with self._terminal_condition:
            while self._active_terminal_waiters or self._terminal_projection_tasks:
                await self._terminal_condition.wait()
        async with self._revision_condition:
            while self._active_revision_waiters:
                await self._revision_condition.wait()
        async with self._terminal_condition:
            self._closed = True
            self._closing = False
            self._terminal_condition.notify_all()
        async with self._revision_condition:
            self._revision_condition.notify_all()
        async with self._artifact_condition:
            self._artifact_condition.notify_all()


__all__ = [
    "LocalVerifiedSkillOutput",
    "RegistrySimpleSkillAuthority",
    "ResolvedSimpleSkill",
    "RunAdmissionError",
    "RunArtifactNotFound",
    "RunArtifactProjectionIntegrityError",
    "RunArtifactReadBackpressure",
    "RunArtifactsUnavailable",
    "RunCancelResult",
    "RunReceiptProjectionIntegrityError",
    "RunRevisionWaitBackpressure",
    "RunRevisionWaitUnavailable",
    "RunRuntime",
    "RunSubmissionResult",
    "RunTerminalProjectionIntegrityError",
    "RunTerminalResultPending",
    "RunTerminalResultUnavailable",
    "RunTerminalWaitBackpressure",
    "RunVerifiedArtifactPage",
    "RunVerifiedArtifactReader",
    "SimpleSkillExecutionContext",
    "SimpleSkillRunTerminalResult",
]
