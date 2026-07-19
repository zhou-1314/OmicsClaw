from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from omicsclaw.skill.plan_executor import (
    CandidatePlanValidationError,
    execute_candidate_plan,
)
from omicsclaw.skill.evolution import SkillHealthLedger
from omicsclaw.skill.result import SkillRunAuditIdentity, build_skill_run_result
from omicsclaw.skill.resource_scheduler import (
    ExecutionResourceBudget,
    ExecutionResourceScheduler,
)
from omicsclaw.skill.skill_dag import candidate_plan_digest, candidate_plan_graph_hash


def _edge(
    source: str,
    target: str,
    path: str = "artifact.dat",
    *,
    condition_scope: dict | None = None,
) -> dict:
    return {
        "source": source,
        "target": target,
        "matched_output_path": path,
        "matched_output_key": "artifacts.test",
        "matched_precondition_key": "artifacts.test",
        "edge_kind": "preferred",
        "condition_scope": condition_scope,
        "reviewed": True,
    }


def _plan(
    *,
    skills: list[str],
    phases: list[list[str]],
    edges: list[dict],
    validated_order: bool = True,
    method_bindings: dict[str, str] | None = None,
    resource_requests: dict[str, dict] | None = None,
) -> dict:
    default_request = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 0,
        "threads": 1,
        "temporary_disk_mib": 1024,
    }
    plan = {
        "requested_skills": list(skills),
        "skills": list(skills),
        "phases": phases,
        "edges": edges,
        "validated_order": validated_order,
        "unresolved_pairs": []
        if validated_order
        else [
            {
                "source": skills[0],
                "target": skills[-1],
                "reason": "no_compatibility_edge",
            }
        ],
        "resource_requests": resource_requests
        if resource_requests is not None
        else {skill: dict(default_request) for skill in skills},
        "resource_ready": True,
        "missing_resource_requests": [],
    }
    if method_bindings is not None:
        plan["method_bindings"] = method_bindings
    return plan


def _revision_bound_registry(tmp_path: Path):
    from omicsclaw.skill.registry import OmicsRegistry

    skills_root = tmp_path / "skills"
    skills: dict[str, dict] = {}
    compute_resources = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 0,
        "threads": 1,
        "temporary_disk_mib": 1024,
    }
    for skill in ("a", "b"):
        skill_dir = skills_root / "demo" / skill
        skill_dir.mkdir(parents=True)
        script = skill_dir / "entry.py"
        script.write_text(f"print({skill!r})\n", encoding="utf-8")
        (skill_dir / "skill.yaml").write_text(
            f"id: {skill}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        skills[skill] = {
            "alias": skill,
            "canonical_name": skill,
            "directory_name": skill,
            "script": script,
            "version": "1.0.0",
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "input_contract": {},
            "output_contract": {},
            "compute_resources": dict(compute_resources),
            "lifecycle_status": "mvp",
        }
    registry = OmicsRegistry()
    registry.skills = skills
    registry.canonical_aliases = ["a", "b"]
    registry.domains = {"demo": {"name": "Demo"}}
    registry._loaded_dir = skills_root.resolve()
    registry._state = replace(
        registry._state,
        skill_manifest_revisions={
            skill: "sha256:"
            + hashlib.sha256(
                (skills_root / "demo" / skill / "skill.yaml").read_bytes()
            ).hexdigest()
            for skill in ("a", "b")
        },
    )
    registry._loaded = True
    return registry


def _bind_production_plan(registry, plan: dict) -> dict:
    from omicsclaw.skill.skill_dag import build_candidate_chain_with_revision

    snapshot = registry.snapshot()
    selected, graph_revision = build_candidate_chain_with_revision(
        snapshot,
        skills_root=snapshot.loaded_dir,
        skills=plan["skills"],
        method_bindings=plan.get("method_bindings"),
    )
    for field in (
        "skills",
        "edges",
        "validated_order",
        "unresolved_pairs",
        "resource_requests",
        "resource_ready",
        "missing_resource_requests",
    ):
        plan[field] = selected[field]
    plan["plan_schema_version"] = 2
    plan["skill_revisions"] = snapshot.skill_revisions(plan["skills"])
    plan["graph_revision"] = graph_revision
    return plan


def _identity_from_plan(plan: dict, skill: str) -> SkillRunAuditIdentity:
    revision = plan["skill_revisions"][skill]
    return SkillRunAuditIdentity(
        skill_id=revision["skill_id"],
        skill_version=revision["skill_version"],
        skill_hash=revision["manifest_hash"],
        source_hash=revision["source_hash"],
        environment_id="env:" + "0" * 20,
    )


def test_executor_rejects_plan_marked_resource_unready_before_runner(
    tmp_path: Path,
) -> None:
    plan = _plan(skills=["step"], phases=[["step"]], edges=[])
    plan["resource_ready"] = False
    plan["missing_resource_requests"] = ["step"]
    calls: list[str] = []

    async def runner(skill: str, **_kwargs):
        calls.append(skill)
        return build_skill_run_result(success=True, output_files=[])

    with pytest.raises(CandidatePlanValidationError, match="resource_ready"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "out",
                runner=runner,
            )
        )

    assert calls == []
    assert not (tmp_path / "out").exists()


def test_executor_rejects_request_larger_than_resource_budget_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "cpu_cores": 4,
        "memory_mib": 8192,
        "gpu_devices": 1,
        "threads": 4,
        "temporary_disk_mib": 4096,
    }
    plan = _plan(
        skills=["gpu-step"],
        phases=[["gpu-step"]],
        edges=[],
        resource_requests={"gpu-step": request},
    )
    calls: list[str] = []

    async def runner(skill: str, **_kwargs):
        calls.append(skill)
        raise AssertionError("runner must not be reached")

    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=8,
            memory_mib=16384,
            gpu_device_ids=(),
            threads=8,
            temporary_disk_mib=8192,
            max_processes=4,
        )
    )
    monkeypatch.setattr(
        "omicsclaw.skill.plan_executor.get_process_resource_scheduler",
        lambda _output_root: scheduler,
    )

    with pytest.raises(CandidatePlanValidationError, match="resource budget"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "out",
                runner=runner,
            )
        )

    assert calls == []
    assert not (tmp_path / "out").exists()


def test_executor_never_overcommits_aggregate_phase_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = {
        skill: {
            "cpu_cores": 2,
            "memory_mib": 2048,
            "gpu_devices": 0,
            "threads": 2,
            "temporary_disk_mib": 512,
        }
        for skill in ("a", "b", "c")
    }
    plan = _plan(
        skills=["a", "b", "c"],
        phases=[["a", "b", "c"]],
        edges=[],
        resource_requests=requests,
    )
    active_cpu = 0
    max_active_cpu = 0

    async def runner(skill: str, **kwargs):
        nonlocal active_cpu, max_active_cpu
        active_cpu += requests[skill]["cpu_cores"]
        max_active_cpu = max(max_active_cpu, active_cpu)
        await asyncio.sleep(0.01)
        active_cpu -= requests[skill]["cpu_cores"]
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=kwargs["output_dir"],
        )

    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=4,
            memory_mib=4096,
            gpu_device_ids=(),
            threads=4,
            temporary_disk_mib=1024,
            max_processes=4,
        )
    )
    monkeypatch.setattr(
        "omicsclaw.skill.plan_executor.get_process_resource_scheduler",
        lambda _output_root: scheduler,
    )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is True
    assert max_active_cpu == 4


def test_executor_serializes_gpu_leases_and_passes_resource_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "cpu_cores": 2,
        "memory_mib": 2048,
        "gpu_devices": 1,
        "threads": 2,
        "temporary_disk_mib": 512,
    }
    plan = _plan(
        skills=["gpu-a", "gpu-b"],
        phases=[["gpu-a", "gpu-b"]],
        edges=[],
        resource_requests={"gpu-a": request, "gpu-b": request},
    )
    active = 0
    max_active = 0
    environments: list[dict[str, str]] = []

    async def runner(skill: str, **kwargs):
        nonlocal active, max_active
        environments.append(dict(kwargs["resource_env"]))
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=kwargs["output_dir"],
        )

    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=4,
            memory_mib=4096,
            gpu_device_ids=("0",),
            threads=4,
            temporary_disk_mib=1024,
            max_processes=4,
        )
    )
    monkeypatch.setattr(
        "omicsclaw.skill.plan_executor.get_process_resource_scheduler",
        lambda _output_root: scheduler,
    )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is True
    assert max_active == 1
    assert [env["CUDA_VISIBLE_DEVICES"] for env in environments] == ["0", "0"]
    assert all(env["OMP_NUM_THREADS"] == "2" for env in environments)
    assert all(Path(env["TMPDIR"]).name == ".tmp" for env in environments)
    assert all(step.resource_request == request for step in result.steps)
    assert all(step.resource_wait_seconds >= 0 for step in result.steps)
    assert result.resource_budget == {
        "cpu_cores": 4,
        "memory_mib": 4096,
        "gpu_devices": 1,
        "threads": 4,
        "temporary_disk_mib": 1024,
        "max_processes": 4,
    }
    assert "gpu_device_ids" not in result.to_dict()["resource_budget"]


def test_concurrent_plans_share_one_process_resource_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "cpu_cores": 2,
        "memory_mib": 2048,
        "gpu_devices": 1,
        "threads": 2,
        "temporary_disk_mib": 512,
    }
    budget = ExecutionResourceBudget(
        cpu_cores=4,
        memory_mib=4096,
        gpu_device_ids=("0",),
        threads=4,
        temporary_disk_mib=1024,
        max_processes=4,
    )
    scheduler = ExecutionResourceScheduler(budget)
    monkeypatch.setattr(
        "omicsclaw.skill.plan_executor.get_process_resource_scheduler",
        lambda _output_root: scheduler,
    )
    active = 0
    max_active = 0

    async def runner(skill: str, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=kwargs["output_dir"],
        )

    async def scenario():
        plans = [
            _plan(
                skills=[skill],
                phases=[[skill]],
                edges=[],
                resource_requests={skill: request},
            )
            for skill in ("plan-a", "plan-b")
        ]
        return await asyncio.gather(
            *(
                execute_candidate_plan(
                    plan,
                    confirmed=True,
                    confirmed_digest=candidate_plan_digest(plan),
                    input_path="",
                    output_root=tmp_path / f"out-{index}",
                    runner=runner,
                )
                for index, plan in enumerate(plans)
            )
        )

    results = asyncio.run(scenario())

    assert all(result.success for result in results)
    assert max_active == 1


def test_cancellation_releases_resource_lease_for_waiting_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "cpu_cores": 1,
        "memory_mib": 1024,
        "gpu_devices": 1,
        "threads": 1,
        "temporary_disk_mib": 256,
    }
    scheduler = ExecutionResourceScheduler(
        ExecutionResourceBudget(
            cpu_cores=1,
            memory_mib=1024,
            gpu_device_ids=("0",),
            threads=1,
            temporary_disk_mib=256,
            max_processes=1,
        )
    )
    monkeypatch.setattr(
        "omicsclaw.skill.plan_executor.get_process_resource_scheduler",
        lambda _output_root: scheduler,
    )

    async def scenario():
        holding_started = asyncio.Event()
        waiting_started = asyncio.Event()

        async def runner(skill: str, **kwargs):
            if skill == "holding":
                holding_started.set()
                await asyncio.Event().wait()
            waiting_started.set()
            return build_skill_run_result(
                skill=skill,
                success=True,
                exit_code=0,
                output_dir=kwargs["output_dir"],
            )

        holding_plan = _plan(
            skills=["holding"],
            phases=[["holding"]],
            edges=[],
            resource_requests={"holding": request},
        )
        waiting_plan = _plan(
            skills=["waiting"],
            phases=[["waiting"]],
            edges=[],
            resource_requests={"waiting": request},
        )
        holding_task = asyncio.create_task(
            execute_candidate_plan(
                holding_plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(holding_plan),
                input_path="",
                output_root=tmp_path / "holding",
                runner=runner,
            )
        )
        await asyncio.wait_for(holding_started.wait(), timeout=1)
        waiting_task = asyncio.create_task(
            execute_candidate_plan(
                waiting_plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(waiting_plan),
                input_path="",
                output_root=tmp_path / "waiting",
                runner=runner,
            )
        )
        await asyncio.sleep(0.01)
        assert not waiting_started.is_set()
        assert not (tmp_path / "waiting" / "waiting").exists()

        holding_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holding_task
        result = await asyncio.wait_for(waiting_task, timeout=1)
        return result

    result = asyncio.run(scenario())

    assert result.success is True


def test_executor_rejects_unconfirmed_or_changed_plan_before_runner_call(
    tmp_path: Path,
):
    plan = _plan(skills=["a"], phases=[["a"]], edges=[])
    calls: list[str] = []

    async def runner(skill: str, **_kwargs):
        calls.append(skill)
        raise AssertionError("runner must not be reached")

    with pytest.raises(CandidatePlanValidationError, match="not confirmed"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=False,
                confirmed_digest=candidate_plan_digest(plan),
                input_path=str(tmp_path / "input.dat"),
                output_root=tmp_path / "out",
                runner=runner,
            )
        )

    with pytest.raises(CandidatePlanValidationError, match="digest"):
        asyncio.run(
            execute_candidate_plan(
                plan | {"skills": ["changed"]},
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path=str(tmp_path / "input.dat"),
                output_root=tmp_path / "out",
                runner=runner,
            )
        )
    assert calls == []


def test_executor_runs_topological_phases_and_propagates_declared_artifact(
    tmp_path: Path,
):
    input_path = tmp_path / "input.dat"
    input_path.write_text("input", encoding="utf-8")
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )
    calls: list[tuple[str, str | None]] = []

    async def runner(skill: str, **kwargs):
        calls.append((skill, kwargs.get("input_path")))
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if skill == "producer":
            (output_dir / "artifact.dat").write_text("artifact", encoding="utf-8")
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(input_path),
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is True
    assert [step.status for step in result.steps] == ["succeeded", "succeeded"]
    assert calls[0] == ("producer", str(input_path))
    assert calls[1][0] == "consumer"
    assert calls[1][1] == str(tmp_path / "out" / "producer" / "artifact.dat")


def test_executor_rejects_stale_output_root_before_any_step(tmp_path: Path):
    plan = _plan(skills=["step"], phases=[["step"]], edges=[])
    output_root = tmp_path / "out"
    output_root.mkdir()
    stale = output_root / "old-result.json"
    stale.write_text('{"status":"ok"}\n', encoding="utf-8")
    calls: list[str] = []

    async def runner(skill: str, **_kwargs):
        calls.append(skill)
        raise AssertionError("stale candidate-plan output reached runner")

    with pytest.raises(CandidatePlanValidationError, match="fresh output directory"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path=str(tmp_path / "input.dat"),
                output_root=output_root,
                runner=runner,
            )
        )

    assert calls == []
    assert stale.read_text(encoding="utf-8") == '{"status":"ok"}\n'


def test_executor_claims_each_custom_runner_leaf_before_sibling_execution(
    tmp_path: Path,
):
    plan = _plan(skills=["a", "b"], phases=[["a"], ["b"]], edges=[])
    output_root = tmp_path / "out"
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if skill == "a":
            injected = output_root / "b"
            injected.mkdir()
            (injected / "result.json").write_text(
                '{"status":"ok"}\n',
                encoding="utf-8",
            )
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    with pytest.raises(CandidatePlanValidationError, match="fresh output directory"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
                runner=runner,
            )
        )

    assert calls == ["a"]


def test_custom_runner_cannot_redirect_its_result_output_root(tmp_path: Path):
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )
    external = tmp_path / "external"
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        if skill == "producer":
            external.mkdir()
            (external / "artifact.dat").write_text("forged\n", encoding="utf-8")
            output_dir = external
        else:
            output_dir = Path(kwargs["output_dir"])
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    with pytest.raises(CandidatePlanValidationError, match="output directory"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "out",
                runner=runner,
            )
        )

    assert calls == ["producer"]


def test_directory_cannot_satisfy_candidate_edge_artifact(tmp_path: Path):
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        if skill == "producer":
            (output_dir / "artifact.dat").mkdir()
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is False
    assert calls == ["producer"]
    assert result.steps[0].error_kind == "contract_failure"
    assert result.steps[1].status == "skipped"


def test_candidate_edge_artifact_cannot_escape_through_symlink(tmp_path: Path):
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )
    external = tmp_path / "external.dat"
    external.write_text("old external data\n", encoding="utf-8")
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        if skill == "producer":
            (output_dir / "artifact.dat").symlink_to(external)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is False
    assert calls == ["producer"]
    assert result.steps[0].error_kind == "contract_failure"
    assert result.steps[1].status == "skipped"


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_candidate_edge_artifact_cannot_alias_internal_claim(
    tmp_path: Path,
    alias_kind: str,
):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        if skill == "producer":
            alias = output_dir / "artifact.dat"
            claim = output_dir / OUTPUT_CLAIM_FILENAME
            if alias_kind == "symlink":
                alias.symlink_to(claim)
            else:
                alias.hardlink_to(claim)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is False
    assert calls == ["producer"]
    assert result.steps[0].error_kind == "contract_failure"
    assert result.steps[1].status == "skipped"


def test_candidate_plan_rejects_internal_claim_as_edge_artifact(tmp_path: Path):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer", OUTPUT_CLAIM_FILENAME)],
    )

    with pytest.raises(CandidatePlanValidationError, match="matched_output_path"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "out",
                runner=lambda *_args, **_kwargs: None,
            )
        )

    assert not (tmp_path / "out").exists()


def test_executor_revalidates_method_scope_and_passes_bound_method_to_runner(
    tmp_path: Path,
):
    scoped_edge = _edge(
        "producer",
        "consumer",
        condition_scope={"source_methods": ["method_a"]},
    )
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[scoped_edge],
        method_bindings={"producer": "method_a"},
    )
    calls: list[tuple[str, list[str] | None]] = []

    async def runner(skill: str, **kwargs):
        calls.append((skill, kwargs.get("extra_args")))
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if skill == "producer":
            (output_dir / "artifact.dat").write_text("artifact", encoding="utf-8")
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(tmp_path / "input.dat"),
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is True
    assert calls == [
        ("producer", ["--method", "method_a"]),
        ("consumer", None),
    ]
    assert result.steps[0].method == "method_a"

    for bindings, message in [
        ({}, "requires a method binding"),
        ({"producer": "method_b"}, "does not satisfy"),
    ]:
        invalid = plan | {"method_bindings": bindings}
        with pytest.raises(CandidatePlanValidationError, match=message):
            asyncio.run(
                execute_candidate_plan(
                    invalid,
                    confirmed=True,
                    confirmed_digest=candidate_plan_digest(invalid),
                    input_path=str(tmp_path / "input.dat"),
                    output_root=tmp_path / "invalid",
                    runner=runner,
                )
            )


def test_executor_cascades_failure_only_to_dependants(tmp_path: Path):
    input_path = tmp_path / "input.dat"
    input_path.write_text("input", encoding="utf-8")
    plan = _plan(
        skills=["fail", "independent", "downstream"],
        phases=[["fail", "independent"], ["downstream"]],
        edges=[_edge("fail", "downstream")],
    )
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        return build_skill_run_result(
            skill=skill,
            success=skill != "fail",
            exit_code=1 if skill == "fail" else 0,
            output_dir=output_dir,
            stderr="dependency missing" if skill == "fail" else "",
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(input_path),
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    by_skill = {step.skill: step for step in result.steps}
    assert calls == ["fail", "independent"]
    assert by_skill["fail"].status == "failed"
    assert by_skill["independent"].status == "succeeded"
    assert by_skill["downstream"].status == "skipped"
    assert by_skill["downstream"].error_kind == "upstream_failed"
    assert result.success is False


def test_executor_bounds_parallel_steps_and_propagates_cancellation(tmp_path: Path):
    plan = _plan(
        skills=["a", "b", "c", "d"],
        phases=[["a", "b", "c", "d"]],
        edges=[],
    )
    active = 0
    max_active = 0

    async def bounded_runner(skill: str, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=kwargs["output_dir"],
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "bounded",
            runner=bounded_runner,
            max_concurrency=2,
        )
    )
    assert result.success is True
    assert max_active == 2

    cancelled: set[str] = set()

    async def cancellation_scenario():
        two_started = asyncio.Event()
        started: set[str] = set()

        async def blocking_runner(skill: str, **_kwargs):
            started.add(skill)
            if len(started) == 2:
                two_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(skill)
                raise

        task = asyncio.create_task(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "cancelled",
                runner=blocking_runner,
                max_concurrency=2,
            )
        )
        await asyncio.wait_for(two_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancellation_scenario())
    assert len(cancelled) == 2


def test_missing_declared_artifact_is_audited_as_contract_failure(
    tmp_path: Path,
    monkeypatch,
):
    ledger_path = tmp_path / "plan-events.jsonl"
    monkeypatch.setenv("OMICSCLAW_SKILL_HEALTH_LEDGER", str(ledger_path))
    import omicsclaw.skill.registry as registry_module

    monkeypatch.setattr(
        registry_module,
        "ensure_registry_loaded",
        lambda: (_ for _ in ()).throw(
            AssertionError("candidate-plan correction must not reread Registry")
        ),
    )
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )

    async def runner(skill: str, **kwargs):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(tmp_path / "input.dat"),
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    producer = result.steps[0]
    events = SkillHealthLedger(ledger_path).events()
    assert producer.status == "failed"
    assert producer.result is not None
    assert producer.result.success is False
    assert producer.result.error_kind == "contract_failure"
    assert len(events) == 1
    assert events[0].source == "candidate-plan-contract"
    assert events[0].error_kind == "contract_failure"
    assert events[0].skill_hash == "unknown"
    assert events[0].source_hash == "unknown"
    assert events[0].environment_id == "unknown"
    assert "artifact.dat" not in str(events[0].to_dict())


@pytest.mark.parametrize("return_shape", ["native", "mapping"])
def test_custom_plan_runner_cannot_self_assert_contract_failure_audit_identity(
    tmp_path: Path,
    monkeypatch,
    return_shape: str,
):
    ledger_path = tmp_path / "frozen-plan-events.jsonl"
    monkeypatch.setenv("OMICSCLAW_SKILL_HEALTH_LEDGER", str(ledger_path))
    identity = SkillRunAuditIdentity(
        skill_id="patient-secret",
        skill_version="token-super-secret",
        skill_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        environment_id="env:" + "c" * 20,
    )
    plan = _plan(
        skills=["producer", "consumer"],
        phases=[["producer"], ["consumer"]],
        edges=[_edge("producer", "consumer")],
    )

    async def runner(skill: str, **kwargs):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        result = build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
            audit_identity=identity,
        )
        if return_shape == "native":
            return result
        return result.to_legacy_dict() | {"_audit_identity": identity}

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(tmp_path / "input.dat"),
            output_root=tmp_path / "out",
            runner=runner,
        )
    )

    assert result.success is False
    [event] = SkillHealthLedger(ledger_path).events()
    assert event.skill_id.startswith("unresolved-")
    assert event.skill_version == "unknown"
    assert event.skill_hash == "unknown"
    assert event.source_hash == "unknown"
    assert event.environment_id == "unknown"
    serialized = str(event.to_dict())
    assert "patient-secret" not in serialized
    assert "token-super-secret" not in serialized
    assert identity.skill_hash not in serialized
    assert identity.source_hash not in serialized


def test_unresolved_plan_blocks_by_default_but_allows_explicit_independent_strategy(
    tmp_path: Path,
):
    plan = _plan(
        skills=["a", "b"],
        phases=[["a", "b"]],
        edges=[],
        validated_order=False,
    )
    calls: list[str] = []

    async def runner(skill: str, **kwargs):
        calls.append(skill)
        output_dir = Path(kwargs["output_dir"])
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    with pytest.raises(CandidatePlanValidationError, match="unresolved"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path=str(tmp_path / "input.dat"),
                output_root=tmp_path / "blocked",
                runner=runner,
            )
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path=str(tmp_path / "input.dat"),
            output_root=tmp_path / "independent",
            unresolved_strategy="independent",
            runner=runner,
        )
    )
    assert result.success is True
    assert calls == ["a", "b"]


def test_executor_refuses_unreviewed_compatibility_edges(tmp_path: Path):
    edge = _edge("a", "b") | {"reviewed": False, "edge_kind": "alternative"}
    plan = _plan(
        skills=["a", "b"],
        phases=[["a"], ["b"]],
        edges=[edge],
    )

    with pytest.raises(CandidatePlanValidationError, match="unreviewed"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path=str(tmp_path / "input.dat"),
                output_root=tmp_path / "out",
            )
        )


def test_real_reviewed_singlecell_plan_is_executable_through_the_plan_seam(
    tmp_path: Path,
):
    from omicsclaw.analysis_router import AnalysisRouter

    route = AnalysisRouter().route("run sc-preprocessing and then sc-clustering")
    plan = route.metadata["candidate_chain"]
    calls: list[tuple[str, bool, str | None]] = []

    async def runner(skill: str, **kwargs):
        calls.append((skill, kwargs["demo"], kwargs.get("input_path")))
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if skill == "sc-preprocessing":
            (output_dir / "processed.h5ad").write_text("fixture", encoding="utf-8")
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
        )

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=route.metadata["plan_digest"],
            input_path="",
            output_root=tmp_path / "out",
            demo=True,
            runner=runner,
        )
    )

    assert result.success is True
    assert calls[0] == ("sc-preprocessing", True, None)
    assert calls[1] == (
        "sc-clustering",
        False,
        str(tmp_path / "out" / "sc-preprocessing" / "processed.h5ad"),
    )


def test_default_plan_runner_uses_one_frozen_registry_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module
    from omicsclaw.skill.registry import OmicsRegistry

    registry = _revision_bound_registry(tmp_path)
    frozen_state = registry.snapshot()._state
    replacement = OmicsRegistry()
    replacement.skills = dict(registry.skills)
    replacement.canonical_aliases = list(registry.canonical_aliases)
    replacement.domains = {"demo": {"name": "Replacement"}}
    replacement._loaded = True
    replacement._loaded_dir = registry._loaded_dir
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a", "b"], phases=[["a"], ["b"]], edges=[]),
    )
    seen_states: list[object] = []

    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )

    async def fake_arun(skill: str, **kwargs):
        snapshot = kwargs.get("_registry_snapshot")
        seen_states.append(snapshot._state if snapshot is not None else None)
        if skill == "a":
            registry._state = replacement._state
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
            audit_identity=_identity_from_plan(plan, skill),
        )

    monkeypatch.setattr(runner_module, "arun_skill", fake_arun)

    result = asyncio.run(
        execute_candidate_plan(
            plan,
            confirmed=True,
            confirmed_digest=candidate_plan_digest(plan),
            input_path="",
            output_root=tmp_path / "out",
            unresolved_strategy="independent",
        )
    )

    assert result.success is True
    assert seen_states == [frozen_state, frozen_state]
    assert registry._state is replacement._state


@pytest.mark.parametrize("revision_state", ["missing", "stale"])
def test_default_plan_rejects_missing_or_stale_revisions_before_output_creation(
    tmp_path: Path,
    monkeypatch,
    revision_state: str,
):
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a", "b"], phases=[["a"], ["b"]], edges=[]),
    )
    if revision_state == "stale":
        Path(registry.skills["b"]["script"]).write_text(
            "print('changed')\n",
            encoding="utf-8",
        )
    else:
        plan.pop("skill_revisions")
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="revision"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
                unresolved_strategy="independent",
            )
        )

    assert not output_root.exists()


def test_default_plan_rejects_manifest_changed_after_plan_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    manifest = registry._loaded_dir / "demo" / "a" / "skill.yaml"
    manifest.write_text("id: a\nversion: 2.0.0\n", encoding="utf-8")
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="revision authority"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
            )
        )

    assert not output_root.exists()


def test_default_plan_revalidates_each_revision_immediately_before_step_spawn(
    tmp_path: Path,
    monkeypatch,
):
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a", "b"], phases=[["a"], ["b"]], edges=[]),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )

    async def mutate_next_skill(skill: str, **kwargs):
        calls.append(skill)
        if skill == "a":
            Path(registry.skills["b"]["script"]).write_text(
                "print('changed between phases')\n",
                encoding="utf-8",
            )
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        return build_skill_run_result(
            skill=skill,
            success=True,
            exit_code=0,
            output_dir=output_dir,
            audit_identity=_identity_from_plan(plan, skill),
        )

    monkeypatch.setattr(runner_module, "arun_skill", mutate_next_skill)

    with pytest.raises(CandidatePlanValidationError, match="revision"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=tmp_path / "out",
                unresolved_strategy="independent",
            )
        )

    assert calls == ["a"]


@pytest.mark.parametrize("authority_state", ["missing_schema", "stale_graph"])
def test_default_plan_rejects_unversioned_or_stale_graph_authority(
    tmp_path: Path,
    monkeypatch,
    authority_state: str,
):
    import omicsclaw.skill.plan_executor as plan_executor_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a", "b"], phases=[["a"], ["b"]], edges=[]),
    )
    if authority_state == "missing_schema":
        plan.pop("plan_schema_version")
    else:
        (registry._loaded_dir / "skill_dag_reviews.yaml").write_text(
            "schema_version: 2\nreviews: []\n",
            encoding="utf-8",
        )
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )

    with pytest.raises(
        CandidatePlanValidationError,
        match="schema|graph revision",
    ):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
                unresolved_strategy="independent",
            )
        )

    assert not output_root.exists()


def test_default_plan_rejects_submitted_authority_payload_hash_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    # Keep the confirmation digest internally consistent while mixing a new
    # submitted authority payload with the old selected_graph_hash.
    plan["validated_order"] = False
    plan["unresolved_pairs"] = [
        {"source": "a", "target": "a", "reason": "tampered-authority"}
    ]
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="authority payload"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
                unresolved_strategy="independent",
            )
        )

    assert not output_root.exists()


@pytest.mark.parametrize(
    "field,value",
    [("memory_mib", 1), ("temporary_disk_mib", 0)],
)
def test_default_plan_rejects_rehashed_resource_authority_downgrade(
    tmp_path: Path,
    monkeypatch,
    field: str,
    value: int,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    plan["resource_requests"]["a"][field] = value
    plan["graph_revision"]["selected_graph_hash"] = candidate_plan_graph_hash(plan)
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="graph revision"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
            )
        )

    assert not output_root.exists()


def test_default_plan_rejects_rehashed_undeclared_method_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    plan["method_bindings"] = {"a": "invented"}
    plan["graph_revision"]["selected_graph_hash"] = candidate_plan_graph_hash(plan)
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="revision authority"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
            )
        )

    assert not output_root.exists()


def test_confirmed_plan_registry_contract_cannot_be_weakened_in_place(
    tmp_path: Path,
) -> None:
    """Plan authority must not share mutable contract metadata with callers."""
    registry = _revision_bound_registry(tmp_path)
    registry.skills["a"]["output_contract"] = {"files": ["result.json"]}
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    frozen_snapshot = registry.snapshot()
    before_revision = dict(plan["skill_revisions"]["a"])

    with pytest.raises(TypeError):
        registry.skills["a"]["output_contract"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        registry.skills["a"]["output_contract"]["files"].clear()

    assert dict(frozen_snapshot.skills["a"]["output_contract"]) == {
        "files": ("result.json",)
    }
    assert frozen_snapshot.skill_revision("a") == before_revision


def test_default_plan_rejects_profile_without_unified_method_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    registry.skills["a"]["param_hints"] = {"profile": {}}
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    plan["method_bindings"] = {"a": "profile"}
    plan["graph_revision"]["selected_graph_hash"] = candidate_plan_graph_hash(plan)
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="revision authority"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
            )
        )

    assert not output_root.exists()


def test_default_plan_rejects_profile_not_accepted_by_method_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    script = Path(registry.skills["a"]["script"])
    script.write_text(
        'parser.add_argument("--method", choices=["actual"])\n',
        encoding="utf-8",
    )
    registry.skills["a"].update(
        {
            "source": "v2",
            "runtime_language": "python",
            "param_hints": {"profile": {}},
            "allowed_extra_flags": {"--method"},
        }
    )
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a"], phases=[["a"]], edges=[]),
    )
    plan["method_bindings"] = {"a": "profile"}
    plan["graph_revision"]["selected_graph_hash"] = candidate_plan_graph_hash(plan)
    output_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        runner_module,
        "arun_skill",
        lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )

    with pytest.raises(CandidatePlanValidationError, match="revision authority"):
        asyncio.run(
            execute_candidate_plan(
                plan,
                confirmed=True,
                confirmed_digest=candidate_plan_digest(plan),
                input_path="",
                output_root=output_root,
            )
        )

    assert not output_root.exists()


def test_default_plan_identity_failure_cancels_and_drains_same_phase_siblings(
    tmp_path: Path,
    monkeypatch,
):
    import omicsclaw.skill.plan_executor as plan_executor_module
    import omicsclaw.skill.runner as runner_module

    registry = _revision_bound_registry(tmp_path)
    plan = _bind_production_plan(
        registry,
        _plan(skills=["a", "b"], phases=[["a", "b"]], edges=[]),
    )
    monkeypatch.setattr(
        plan_executor_module,
        "ensure_registry_loaded",
        lambda: registry,
        raising=False,
    )

    async def scenario() -> bool:
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()
        never_finish = asyncio.Event()
        sibling_task: asyncio.Task | None = None

        async def fake_arun(skill: str, **kwargs):
            nonlocal sibling_task
            output_dir = Path(kwargs["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            if skill == "a":
                sibling_task = asyncio.current_task()
                sibling_started.set()
                try:
                    await never_finish.wait()
                except asyncio.CancelledError:
                    sibling_cancelled.set()
                    raise
                return build_skill_run_result(
                    skill=skill,
                    success=True,
                    exit_code=0,
                    output_dir=output_dir,
                    audit_identity=_identity_from_plan(plan, skill),
                )

            await sibling_started.wait()
            expected = _identity_from_plan(plan, skill)
            return build_skill_run_result(
                skill=skill,
                success=True,
                exit_code=0,
                output_dir=output_dir,
                audit_identity=SkillRunAuditIdentity(
                    skill_id=expected.skill_id,
                    skill_version=expected.skill_version,
                    skill_hash="sha256:" + "f" * 64,
                    source_hash=expected.source_hash,
                    environment_id=expected.environment_id,
                ),
            )

        monkeypatch.setattr(runner_module, "arun_skill", fake_arun)
        try:
            with pytest.raises(
                CandidatePlanValidationError,
                match="audit identity mismatch",
            ):
                await execute_candidate_plan(
                    plan,
                    confirmed=True,
                    confirmed_digest=candidate_plan_digest(plan),
                    input_path="",
                    output_root=tmp_path / "out",
                    max_concurrency=2,
                    unresolved_strategy="independent",
                )
            return sibling_cancelled.is_set()
        finally:
            if sibling_task is not None and not sibling_task.done():
                sibling_task.cancel()
                await asyncio.gather(sibling_task, return_exceptions=True)

    assert asyncio.run(scenario()) is True
