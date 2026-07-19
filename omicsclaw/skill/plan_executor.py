"""Execute a confirmed compatibility candidate plan as a topological DAG.

This module is intentionally narrower than the compatibility graph compiler:
the graph says which skills *may* connect, while this executor only runs the
exact plan whose canonical digest was confirmed.  It propagates declared
artifacts, runs independent nodes in the same phase concurrently, and skips
only descendants of failed steps.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path, PurePosixPath
import re
from typing import Any, Awaitable, Callable, Literal, Mapping

from omicsclaw.common.output_claim import (
    OutputClaimIdentity,
    collect_output_claim_identities,
    is_output_claim_path,
    is_scientific_output_file,
)

from .execution.output_ownership import (
    OutputDirectoryClaimError,
    claim_fresh_output_directory,
)
from .registry import RegistrySnapshot, ensure_registry_loaded
from .result import SkillRunResult, coerce_skill_run_result
from .resource_scheduler import (
    ExecutionResourceRequest,
    ResourceConfigurationError,
    get_process_resource_scheduler,
)
from .skill_dag import candidate_plan_digest, candidate_plan_graph_hash


UnresolvedStrategy = Literal["block", "independent"]
PlanRunner = Callable[..., Awaitable[SkillRunResult | Mapping[str, Any]]]
logger = logging.getLogger(__name__)
_PLAN_SCHEMA_VERSION = 2
_CANONICAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_REVISION_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CandidatePlanValidationError(ValueError):
    """Raised before execution when a plan or its confirmation is invalid."""


@dataclass(slots=True)
class CandidateStepExecution:
    skill: str
    phase: int
    method: str = ""
    status: str = "pending"
    input_paths: list[str] = field(default_factory=list)
    output_dir: str = ""
    error_kind: str = ""
    message: str = ""
    result: SkillRunResult | None = None
    resource_request: dict[str, int] = field(default_factory=dict)
    resource_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "phase": self.phase,
            "method": self.method,
            "status": self.status,
            "input_paths": list(self.input_paths),
            "output_dir": self.output_dir,
            "error_kind": self.error_kind,
            "message": self.message,
            "result": self.result.to_legacy_dict() if self.result else None,
            "resource_request": dict(self.resource_request),
            "resource_wait_seconds": self.resource_wait_seconds,
        }


@dataclass(slots=True)
class CandidatePlanExecutionResult:
    plan_digest: str
    success: bool
    status: str
    steps: list[CandidateStepExecution]
    resource_budget: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_digest": self.plan_digest,
            "success": self.success,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "resource_budget": (
                dict(self.resource_budget) if self.resource_budget is not None else None
            ),
        }


def _as_contract_failure(
    result: SkillRunResult,
    *,
    message: str,
    audit_identity_trusted: bool,
) -> SkillRunResult:
    """Correct a script-level success rejected by the artifact contract.

    The shared runner has already audited the subprocess outcome.  This second,
    explicitly sourced event records the higher-level compatibility contract
    failure without storing the artifact path or message in the ledger.
    """
    corrected = replace(
        result,
        success=False,
        exit_code=result.exit_code or 1,
        stderr=message,
        error_kind="contract_failure",
    )
    try:
        from .evolution import record_skill_run_result

        identity = result.audit_identity if audit_identity_trusted else None
        record_skill_run_result(
            corrected,
            skill_info=(
                {
                    "alias": identity.skill_id,
                    "version": identity.skill_version,
                }
                if identity is not None
                else None
            ),
            skill_hash=(identity.skill_hash if identity is not None else "unknown"),
            source_hash=(identity.source_hash if identity is not None else "unknown"),
            environment_id=(
                identity.environment_id if identity is not None else "unknown"
            ),
            source="candidate-plan-contract",
        )
    except Exception as exc:  # pragma: no cover - audit storage degradation
        logger.warning("failed to record candidate-plan contract event: %s", exc)
    return corrected


def _validated_production_revisions(
    plan: Mapping[str, Any],
    skills: list[str],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Validate the revision authority required by the default runner path."""
    if plan.get("plan_schema_version") != _PLAN_SCHEMA_VERSION:
        raise CandidatePlanValidationError(
            f"candidate plan schema must be {_PLAN_SCHEMA_VERSION}"
        )
    raw_revisions = plan.get("skill_revisions")
    if not isinstance(raw_revisions, Mapping) or set(raw_revisions) != set(skills):
        raise CandidatePlanValidationError(
            "candidate plan skill revisions must exactly match selected skills"
        )
    fields = {"skill_id", "skill_version", "manifest_hash", "source_hash"}
    revisions: dict[str, dict[str, str]] = {}
    for skill in skills:
        raw = raw_revisions[skill]
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise CandidatePlanValidationError(
                f"candidate plan revision for {skill!r} has an invalid schema"
            )
        revision = {field: raw.get(field) for field in fields}
        if any(not isinstance(value, str) for value in revision.values()):
            raise CandidatePlanValidationError(
                f"candidate plan revision for {skill!r} must contain strings"
            )
        skill_id = revision["skill_id"]
        version = revision["skill_version"]
        if skill_id != skill or not _CANONICAL_ID_RE.fullmatch(skill_id):
            raise CandidatePlanValidationError(
                f"candidate plan revision identity mismatch for {skill!r}"
            )
        if (
            not version
            or len(version) > 128
            or version != version.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in version)
        ):
            raise CandidatePlanValidationError(
                f"candidate plan revision version is invalid for {skill!r}"
            )
        if not _REVISION_HASH_RE.fullmatch(revision["manifest_hash"]):
            raise CandidatePlanValidationError(
                f"candidate plan manifest revision is invalid for {skill!r}"
            )
        if not _REVISION_HASH_RE.fullmatch(revision["source_hash"]):
            raise CandidatePlanValidationError(
                f"candidate plan source revision is invalid for {skill!r}"
            )
        revisions[skill] = revision

    raw_graph = plan.get("graph_revision")
    graph_fields = {
        "graph_schema_version",
        "reviews_hash",
        "selected_graph_hash",
    }
    if not isinstance(raw_graph, Mapping) or set(raw_graph) != graph_fields:
        raise CandidatePlanValidationError(
            "candidate plan graph revision has an invalid schema"
        )
    graph_revision = dict(raw_graph)
    if (
        isinstance(graph_revision["graph_schema_version"], bool)
        or graph_revision["graph_schema_version"] != 1
        or not isinstance(graph_revision["reviews_hash"], str)
        or not _REVISION_HASH_RE.fullmatch(graph_revision["reviews_hash"])
        or not isinstance(graph_revision["selected_graph_hash"], str)
        or not _REVISION_HASH_RE.fullmatch(graph_revision["selected_graph_hash"])
    ):
        raise CandidatePlanValidationError("candidate plan graph revision is invalid")
    try:
        submitted_graph_hash = candidate_plan_graph_hash(plan)
    except (TypeError, ValueError) as exc:
        raise CandidatePlanValidationError(
            "candidate plan graph authority payload is invalid"
        ) from exc
    if submitted_graph_hash != graph_revision["selected_graph_hash"]:
        raise CandidatePlanValidationError(
            "candidate plan graph authority payload does not match its revision"
        )
    return revisions, graph_revision


def _result_matches_expected_revision(
    result: SkillRunResult,
    expected: Mapping[str, str],
) -> bool:
    identity = result.audit_identity
    return identity is not None and {
        "skill_id": identity.skill_id,
        "skill_version": identity.skill_version,
        "manifest_hash": identity.skill_hash,
        "source_hash": identity.source_hash,
    } == dict(expected)


def _safe_relative_artifact_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    parsed = PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or ".." in parsed.parts
        or is_output_claim_path(Path(path))
    ):
        raise CandidatePlanValidationError(
            f"invalid matched_output_path in candidate plan: {path!r}"
        )
    return path


def _is_contained_output_file(
    root: Path,
    relative_path: str,
    *,
    claim_identities: frozenset[OutputClaimIdentity] | None = None,
) -> bool:
    """Return whether one handoff is a non-internal file inside its Run leaf."""
    candidate = root / relative_path
    if not is_scientific_output_file(
        candidate,
        output_root=root,
        claim_identities=claim_identities,
    ):
        return False
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _validate_plan(
    plan: Mapping[str, Any],
    *,
    confirmed: bool,
    confirmed_digest: str,
    unresolved_strategy: UnresolvedStrategy,
) -> tuple[
    list[str],
    list[list[str]],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, ExecutionResourceRequest],
    str,
]:
    if not confirmed:
        raise CandidatePlanValidationError("candidate plan was not confirmed")
    digest = candidate_plan_digest(plan)
    if not confirmed_digest or digest != confirmed_digest:
        raise CandidatePlanValidationError(
            "candidate plan digest does not match the confirmed plan"
        )
    if unresolved_strategy not in {"block", "independent"}:
        raise CandidatePlanValidationError(
            f"unsupported unresolved strategy: {unresolved_strategy!r}"
        )
    if (
        plan.get("validated_order") is not True or bool(plan.get("unresolved_pairs"))
    ) and unresolved_strategy == "block":
        raise CandidatePlanValidationError(
            "candidate plan has unresolved dependencies; explicit independent strategy required"
        )

    raw_skills = plan.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise CandidatePlanValidationError(
            "candidate plan skills must be a non-empty list"
        )
    skills = [str(skill or "").strip() for skill in raw_skills]
    if not all(skills) or len(set(skills)) != len(skills):
        raise CandidatePlanValidationError("candidate plan skills must be unique names")

    raw_phases = plan.get("phases")
    if not isinstance(raw_phases, list) or not all(
        isinstance(phase, list) and phase for phase in raw_phases
    ):
        raise CandidatePlanValidationError(
            "candidate plan phases must be non-empty lists"
        )
    phases = [[str(skill or "").strip() for skill in phase] for phase in raw_phases]
    flattened = [skill for phase in phases for skill in phase]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(skills):
        raise CandidatePlanValidationError(
            "candidate plan phases must contain every skill exactly once"
        )
    phase_by_skill = {
        skill: phase_index
        for phase_index, phase in enumerate(phases)
        for skill in phase
    }

    raw_bindings = plan.get("method_bindings") or {}
    if not isinstance(raw_bindings, Mapping):
        raise CandidatePlanValidationError(
            "candidate plan method_bindings must be an object"
        )
    method_bindings: dict[str, str] = {}
    for raw_skill, raw_method in raw_bindings.items():
        skill = str(raw_skill or "").strip()
        method = str(raw_method or "").strip()
        if skill not in phase_by_skill:
            raise CandidatePlanValidationError(
                f"method binding references an unknown skill: {skill!r}"
            )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", method):
            raise CandidatePlanValidationError(
                f"invalid canonical method binding for {skill!r}: {method!r}"
            )
        method_bindings[skill] = method

    raw_resource_ready = plan.get("resource_ready")
    raw_missing_resources = plan.get("missing_resource_requests")
    if raw_resource_ready is not True or raw_missing_resources != []:
        raise CandidatePlanValidationError(
            "candidate plan resource_ready must be true and "
            "missing_resource_requests must be empty before execution"
        )

    raw_resource_requests = plan.get("resource_requests")
    if not isinstance(raw_resource_requests, Mapping):
        raise CandidatePlanValidationError(
            "candidate plan resource_requests must be an object"
        )
    if set(raw_resource_requests) != set(skills):
        missing = sorted(set(skills) - set(raw_resource_requests))
        unknown = sorted(set(raw_resource_requests) - set(skills))
        raise CandidatePlanValidationError(
            "candidate plan requires one compute reservation per skill; "
            f"missing={missing}, unknown={unknown}"
        )
    resource_requests: dict[str, ExecutionResourceRequest] = {}
    for skill in skills:
        value = raw_resource_requests[skill]
        if not isinstance(value, Mapping):
            raise CandidatePlanValidationError(
                f"candidate plan resource request for {skill!r} must be an object"
            )
        try:
            resource_requests[skill] = ExecutionResourceRequest.from_mapping(value)
        except ResourceConfigurationError as exc:
            raise CandidatePlanValidationError(
                f"invalid resource request for {skill!r}: {exc}"
            ) from exc

    raw_edges = plan.get("edges") or []
    if not isinstance(raw_edges, list):
        raise CandidatePlanValidationError("candidate plan edges must be a list")
    candidate_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping):
            raise CandidatePlanValidationError("candidate plan edge must be an object")
        source = str(raw_edge.get("source") or "").strip()
        target = str(raw_edge.get("target") or "").strip()
        if source not in phase_by_skill or target not in phase_by_skill:
            raise CandidatePlanValidationError(
                f"candidate plan edge references an unknown skill: {source!r} -> {target!r}"
            )
        if phase_by_skill[source] >= phase_by_skill[target]:
            raise CandidatePlanValidationError(
                f"candidate plan edge violates phase order: {source!r} -> {target!r}"
            )
        raw_scope = raw_edge.get("condition_scope")
        condition_scope: dict[str, list[str]] | None = None
        if raw_scope is not None:
            if not isinstance(raw_scope, Mapping) or set(raw_scope) != {
                "source_methods"
            }:
                raise CandidatePlanValidationError(
                    "candidate plan edge condition_scope must contain only source_methods"
                )
            raw_methods = raw_scope.get("source_methods")
            if not isinstance(raw_methods, list):
                raise CandidatePlanValidationError(
                    "candidate plan edge source_methods must be a non-empty list"
                )
            source_methods = sorted(
                {
                    str(method or "").strip()
                    for method in raw_methods
                    if str(method or "").strip()
                }
            )
            if not source_methods or any(
                not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", method)
                for method in source_methods
            ):
                raise CandidatePlanValidationError(
                    "candidate plan edge source_methods must be canonical method identifiers"
                )
            selected_method = method_bindings.get(source)
            if selected_method is None:
                raise CandidatePlanValidationError(
                    f"candidate plan edge {source!r} -> {target!r} requires a method binding"
                )
            if selected_method not in source_methods:
                raise CandidatePlanValidationError(
                    f"bound method {selected_method!r} does not satisfy candidate plan edge "
                    f"{source!r} -> {target!r}; allowed: {source_methods}"
                )
            condition_scope = {"source_methods": source_methods}
        candidate_edges.append(
            {
                "source": source,
                "target": target,
                "matched_output_path": _safe_relative_artifact_path(
                    raw_edge.get("matched_output_path")
                ),
                "matched_precondition_key": str(
                    raw_edge.get("matched_precondition_key") or ""
                ).strip(),
                "edge_kind": str(raw_edge.get("edge_kind") or "alternative").strip(),
                "condition_scope": condition_scope,
                "reviewed": raw_edge.get("reviewed") is True,
            }
        )
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in candidate_edges:
        pairs.setdefault((edge["source"], edge["target"]), []).append(edge)
    unreviewed_pairs = [
        f"{source}->{target}"
        for (source, target), pair_edges in sorted(pairs.items())
        if not any(edge["reviewed"] for edge in pair_edges)
    ]
    if unreviewed_pairs:
        raise CandidatePlanValidationError(
            "candidate plan contains unreviewed compatibility dependencies: "
            + ", ".join(unreviewed_pairs)
        )

    reviewed_edges = [edge for edge in candidate_edges if edge["reviewed"]]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in reviewed_edges:
        grouped.setdefault(
            (edge["target"], edge["matched_precondition_key"]), []
        ).append(edge)
    edges: list[dict[str, Any]] = []
    for identity, alternatives in sorted(grouped.items()):
        sources = {edge["source"] for edge in alternatives}
        if len(sources) <= 1:
            edges.extend(alternatives)
            continue
        preferred_sources = {
            edge["source"] for edge in alternatives if edge["edge_kind"] == "preferred"
        }
        if len(preferred_sources) == 1:
            selected_source = next(iter(preferred_sources))
            edges.extend(
                edge for edge in alternatives if edge["source"] == selected_source
            )
            continue
        if all(edge["edge_kind"] == "required" for edge in alternatives):
            edges.extend(alternatives)
            continue
        raise CandidatePlanValidationError(
            "ambiguous reviewed producers for precondition "
            f"{identity[0]}:{identity[1]}"
        )
    return skills, phases, edges, method_bindings, resource_requests, digest


async def execute_candidate_plan(
    plan: Mapping[str, Any],
    *,
    confirmed: bool,
    confirmed_digest: str,
    input_path: str,
    output_root: str | Path,
    unresolved_strategy: UnresolvedStrategy = "block",
    runner: PlanRunner | None = None,
    project_id: str = "",
    project_name: str = "",
    demo: bool = False,
    max_concurrency: int = 4,
) -> CandidatePlanExecutionResult:
    """Run one confirmed candidate plan and return per-step audit evidence."""
    skills, phases, edges, method_bindings, resource_requests, digest = _validate_plan(
        plan,
        confirmed=confirmed,
        confirmed_digest=confirmed_digest,
        unresolved_strategy=unresolved_strategy,
    )
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency < 1
    ):
        raise CandidatePlanValidationError("max_concurrency must be a positive integer")
    scheduler = get_process_resource_scheduler(output_root)
    effective_budget = scheduler.budget
    oversized = [
        skill
        for skill, request in resource_requests.items()
        if not effective_budget.accommodates(request)
    ]
    if oversized:
        raise CandidatePlanValidationError(
            "candidate plan request exceeds the resource budget: "
            + ", ".join(sorted(oversized))
        )
    uses_default_runner = runner is None
    frozen_snapshot: RegistrySnapshot | None = None
    expected_revisions: dict[str, dict[str, str]] = {}
    expected_graph_revision: dict[str, Any] = {}
    if uses_default_runner:
        expected_revisions, expected_graph_revision = _validated_production_revisions(
            plan,
            skills,
        )
        frozen_snapshot = ensure_registry_loaded().snapshot()
        try:
            current_revisions, current_graph_revision = await asyncio.gather(
                asyncio.to_thread(frozen_snapshot.skill_revisions, skills),
                asyncio.to_thread(
                    frozen_snapshot.graph_revision,
                    skills,
                    method_bindings=method_bindings,
                ),
            )
        except Exception as exc:
            raise CandidatePlanValidationError(
                "candidate plan revision authority could not be verified"
            ) from exc
        if current_revisions != expected_revisions:
            raise CandidatePlanValidationError(
                "candidate plan skill revision does not match current execution source"
            )
        if current_graph_revision != expected_graph_revision:
            raise CandidatePlanValidationError(
                "candidate plan graph revision does not match current authority"
            )
        from .runner import arun_skill

        async def run_default_skill(skill: str, **kwargs):
            return await arun_skill(
                skill,
                **kwargs,
                _registry_snapshot=frozen_snapshot,
                _expected_skill_revision=expected_revisions[skill],
            )

        runner = run_default_skill

    async def assert_default_authority_current(skill: str) -> None:
        if not uses_default_runner or frozen_snapshot is None:
            return
        try:
            current_revision, current_graph_revision = await asyncio.gather(
                asyncio.to_thread(frozen_snapshot.skill_revision, skill),
                asyncio.to_thread(
                    frozen_snapshot.graph_revision,
                    skills,
                    method_bindings=method_bindings,
                ),
            )
        except Exception as exc:
            raise CandidatePlanValidationError(
                f"candidate plan revision for {skill!r} could not be verified"
            ) from exc
        if current_revision != expected_revisions[skill]:
            raise CandidatePlanValidationError(
                f"candidate plan revision changed for {skill!r}"
            )
        if current_graph_revision != expected_graph_revision:
            raise CandidatePlanValidationError(
                "candidate plan graph revision changed during execution"
            )

    try:
        root = claim_fresh_output_directory(
            output_root,
            owner=f"candidate-plan:{digest}",
        )
    except OutputDirectoryClaimError as exc:
        raise CandidatePlanValidationError(str(exc)) from exc
    phase_by_skill = {
        skill: phase_index
        for phase_index, phase in enumerate(phases)
        for skill in phase
    }
    steps_by_skill = {
        skill: CandidateStepExecution(
            skill=skill,
            phase=phase_by_skill[skill],
            method=method_bindings.get(skill, ""),
            output_dir=str(root / skill),
            resource_request=resource_requests[skill].to_dict(),
        )
        for skill in skills
    }
    incoming: dict[str, set[str]] = {skill: set() for skill in skills}
    incoming_paths: dict[tuple[str, str], set[str]] = {}
    outgoing_paths: dict[str, set[str]] = {skill: set() for skill in skills}
    semaphore = asyncio.Semaphore(max_concurrency)
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        path = edge["matched_output_path"]
        incoming[target].add(source)
        incoming_paths.setdefault((source, target), set()).add(path)
        outgoing_paths[source].add(path)

    async def run_one(step: CandidateStepExecution) -> None:
        predecessors = sorted(incoming[step.skill])
        failed_upstream = [
            source
            for source in predecessors
            if steps_by_skill[source].status != "succeeded"
        ]
        if failed_upstream:
            step.status = "skipped"
            step.error_kind = "upstream_failed"
            step.message = "upstream steps failed: " + ", ".join(failed_upstream)
            return

        propagated: list[str] = []
        for source in predecessors:
            source_step = steps_by_skill[source]
            source_root = Path(
                source_step.result.output_dir
                if source_step.result and source_step.result.output_dir
                else source_step.output_dir
            )
            propagated.extend(
                str(source_root / relative_path)
                for relative_path in sorted(incoming_paths[(source, step.skill)])
            )
        root_inputs = [input_path] if input_path else []
        step.input_paths = list(dict.fromkeys(propagated or root_inputs))
        output_dir = Path(step.output_dir)
        try:
            async with semaphore:
                step.status = "waiting_for_resources"
                reservation = scheduler.reserve(resource_requests[step.skill])
                async with reservation as lease:
                    step.resource_wait_seconds = lease.wait_seconds
                    step.status = "running"
                    await assert_default_authority_current(step.skill)
                    if not uses_default_runner:
                        try:
                            output_dir = claim_fresh_output_directory(
                                output_dir,
                                owner=f"candidate-step:{digest}:{step.skill}",
                            )
                        except OutputDirectoryClaimError as exc:
                            raise CandidatePlanValidationError(str(exc)) from exc
                    raw_result = await runner(
                        step.skill,
                        input_path=(
                            step.input_paths[0] if len(step.input_paths) == 1 else None
                        ),
                        input_paths=(
                            step.input_paths if len(step.input_paths) > 1 else None
                        ),
                        output_dir=str(output_dir),
                        demo=bool(demo and not predecessors),
                        project_id=project_id,
                        project_name=project_name,
                        extra_args=(["--method", step.method] if step.method else None),
                        resource_env=lease.environment
                        | {"TMPDIR": str(output_dir / ".tmp")},
                    )
            result = (
                raw_result
                if isinstance(raw_result, SkillRunResult)
                else coerce_skill_run_result(raw_result)
            )
            if uses_default_runner:
                if not _result_matches_expected_revision(
                    result,
                    expected_revisions[step.skill],
                ):
                    raise CandidatePlanValidationError(
                        f"default runner audit identity mismatch for {step.skill!r}"
                    )
                await assert_default_authority_current(step.skill)
            elif result.audit_identity is not None:
                result = replace(result, audit_identity=None)
            result_root = (
                Path(result.output_dir or output_dir).expanduser().resolve(strict=False)
            )
            if result_root != output_dir:
                raise CandidatePlanValidationError(
                    f"runner output directory does not match the claimed candidate "
                    f"step directory for {step.skill!r}"
                )
            step.result = result
        except CandidatePlanValidationError:
            raise
        except Exception as exc:  # runner defects become structured step failures
            step.status = "failed"
            step.error_kind = "script_defect"
            step.message = str(exc)
            return

        if not result.success:
            step.status = "failed"
            step.error_kind = result.error_kind
            step.message = result.error_text()
            return

        claim_identities = collect_output_claim_identities(output_dir)
        result = replace(
            result,
            files=tuple(
                sorted(
                    path.name
                    for path in output_dir.rglob("*")
                    if is_scientific_output_file(
                        path,
                        output_root=output_dir,
                        claim_identities=claim_identities,
                    )
                )
            ),
        )
        step.result = result
        missing_artifacts = [
            path
            for path in sorted(outgoing_paths[step.skill])
            if not _is_contained_output_file(
                output_dir,
                path,
                claim_identities=claim_identities,
            )
        ]
        if missing_artifacts:
            step.status = "failed"
            step.error_kind = "contract_failure"
            step.message = "declared output artifact missing: " + ", ".join(
                missing_artifacts
            )
            step.result = _as_contract_failure(
                result,
                message=step.message,
                audit_identity_trusted=uses_default_runner,
            )
            return
        step.status = "succeeded"

    for phase_index, phase in enumerate(phases):
        phase_tasks = [
            asyncio.create_task(
                run_one(steps_by_skill[skill]),
                name=f"candidate-plan:{phase_index}:{skill}",
            )
            for skill in phase
        ]
        try:
            await asyncio.gather(*phase_tasks)
        except BaseException:
            # ``asyncio.gather`` propagates the first exception without
            # cancelling sibling awaitables.  A fail-closed authority error (or
            # cancellation of the whole Plan) must not leave same-phase Skills
            # running and writing outputs after this executor has returned.
            for task in phase_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*phase_tasks, return_exceptions=True)
            raise

    steps = [steps_by_skill[skill] for skill in skills]
    success = all(step.status == "succeeded" for step in steps)
    return CandidatePlanExecutionResult(
        plan_digest=digest,
        success=success,
        status="completed" if success else "failed",
        steps=steps,
        resource_budget=effective_budget.to_public_dict(),
    )


__all__ = [
    "CandidatePlanExecutionResult",
    "CandidatePlanValidationError",
    "CandidateStepExecution",
    "execute_candidate_plan",
]
