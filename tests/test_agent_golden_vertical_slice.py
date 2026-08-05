from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from omicsclaw.control import RunAcceptanceStatus, RunRecord
from omicsclaw.control.run_runtime import (
    LocalVerifiedSkillOutput,
    RunSubmissionResult,
    SimpleSkillRunTerminalResult,
)
from omicsclaw.runtime.agent import loop as agent_loop
from omicsclaw.analysis_router import AnalysisRoute, AnalysisRouteKind
from omicsclaw.skill.capability_resolver import CapabilityDecision
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore
from omicsclaw.runtime.tools.builders.agent_executors import execute_create_omics_skill
from omicsclaw.runtime.tools.builders.agent import BotToolContext
from omicsclaw.skill.evaluation_run import EvaluationResultStore, ProtocolRunOutcome
from omicsclaw.skill.evolution import EvolutionProposalStore, SkillHealthLedger
from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance
from omicsclaw.skill.registry import OmicsRegistry, is_skill_automatically_routable
from omicsclaw.skill.schema import load_skill_yaml


RUN_ID = "a" * 32


def _route(
    *,
    kind: AnalysisRouteKind = AnalysisRouteKind.EXACT_SKILL,
    skill: str = "genomics-vcf-operations",
    preflight_required: bool = False,
    metadata: dict | None = None,
) -> AnalysisRoute:
    return AnalysisRoute(
        kind=kind,
        capability_decision=CapabilityDecision(
            query="test",
            coverage=kind.value,
            chosen_skill=skill,
            confidence=1.0,
        ),
        preflight_required=preflight_required,
        metadata=metadata or {},
    )


def _receipt() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status="succeeded",
        terminal_code=None,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1_000,
        started_at_ms=1_500,
        finished_at_ms=3_000,
        revision=3,
    )


class _SuccessfulRunRuntime:
    def __init__(self, skill_id: str = "genomics-vcf-operations") -> None:
        self.events: list[tuple[str, object]] = []
        self.skill_id = skill_id

    async def build_simple_skill_demo_submission(self, **kwargs):
        self.events.append(("build", kwargs))
        return object()

    async def submit(self, submission):
        self.events.append(("submit", submission))
        return RunSubmissionResult(RunAcceptanceStatus.ACCEPTED, _receipt())

    async def wait_for_terminal_result(self, run_id):
        self.events.append(("wait", run_id))
        return SimpleSkillRunTerminalResult(
            receipt=_receipt(),
            skill_id=self.skill_id,
            output=LocalVerifiedSkillOutput(
                output_dir="/verified/output",
                readme_path="/verified/output/README.md",
                replay_path="/verified/output/reproducibility/replay.json",
            ),
        )


class _LlmTrap:
    def __init__(self) -> None:
        self.used = False

    @property
    def chat(self):
        self.used = True
        raise AssertionError("exact demo route must not call the LLM")


@pytest.mark.asyncio
async def test_named_resource_ready_demo_uses_canonical_runtime_without_llm(
    monkeypatch,
    tmp_path,
):
    """Golden slice: User intent -> planned tool -> Receipt -> local artifacts."""

    transcript = TranscriptStore()
    tool_results = ToolResultStore(storage_dir=tmp_path / "tool-results")
    llm = _LlmTrap()
    deps = replace(
        agent_loop._build_engine_dependencies(
            transcript_store_override=transcript,
        ),
        llm=llm,
        transcript_store=transcript,
        tool_result_store=tool_results,
    )
    monkeypatch.setattr(
        agent_loop,
        "_build_engine_dependencies",
        lambda **_kwargs: deps,
    )

    runtime = _SuccessfulRunRuntime()
    tool_calls: list[tuple[str, dict]] = []
    tool_outputs: list[tuple[str, object]] = []

    async def on_tool_call(name, arguments):
        tool_calls.append((name, arguments))

    async def on_tool_result(name, result, *_args):
        tool_outputs.append((name, result))

    result = await agent_loop.llm_tool_loop(
        chat_id="golden-slice",
        user_content="请运行 genomics-vcf-operations demo",
        platform="cli",
        workspace=str(tmp_path),
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        transcript_store_override=transcript,
        run_runtime=runtime,
    )

    assert llm.used is False
    assert tool_calls == [
        ("omicsclaw", {"skill": "genomics-vcf-operations", "mode": "demo"})
    ]
    assert [event[0] for event in runtime.events] == ["build", "submit", "wait"]
    assert tool_outputs and tool_outputs[0][0] == "omicsclaw"
    assert RUN_ID in result
    assert "/verified/output" in result
    assert transcript.get_history("golden-slice")[-1] == {
        "role": "assistant",
        "content": result,
    }


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("请运行 genomics-vcf-operations", _route()),
        ("请运行 VCF demo", _route()),
        (
            "请运行 genomics-vcf-operations demo",
            _route(kind=AnalysisRouteKind.PARTIAL_SKILL),
        ),
        (
            "请运行 genomics-vcf-operations demo",
            _route(preflight_required=True),
        ),
        (
            "请运行 genomics-vcf-operations demo",
            _route(metadata={"candidate_chain": {"skills": ["a", "b"]}}),
        ),
    ],
)
def test_only_exact_explicit_demo_without_gates_is_planned(text, route):
    assert (
        agent_loop._plan_exact_named_demo(
            text,
            route,
            run_runtime=object(),
        )
        == []
    )


@pytest.mark.asyncio
async def test_no_skill_creation_activation_then_agent_execution_golden_slice(
    monkeypatch,
    tmp_path: Path,
):
    """No-Skill -> Agent create -> evaluate -> human activate -> Agent run."""
    import omicsclaw.skill.scaffolder as scaffolder_module

    skill_id = "golden-created-analysis"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(scaffolder_module, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(
        scaffolder_module,
        "_run_demo_smoke_gate",
        lambda *_args, **_kwargs: scaffolder_module._DemoGateOutcome(
            verdict="earned",
            reason="deterministic admission seam",
            envelope={"status": "success", "summary": {"created": True}},
        ),
    )
    real_create = scaffolder_module.create_skill_scaffold

    def create_in_test_root(**kwargs):
        kwargs["skills_root"] = skills_root
        kwargs["output_root"] = tmp_path / "outputs"
        return real_create(**kwargs)

    monkeypatch.setattr(scaffolder_module, "create_skill_scaffold", create_in_test_root)

    class Projection:
        def refresh(self, _skills_root, _skill_id):
            return None

        def rebuild(self, _skills_root):
            return None

    class Execution:
        def validate_demo(self, _skill_id):
            raise AssertionError("activation must trust Evaluation Protocol evidence")

        def validate_demo_defect(self, _skill_id):
            raise AssertionError("not a demotion")

    def successful_protocol(_spec):
        return ProtocolRunOutcome(
            "succeeded",
            evidence_refs=("artifact:golden-lifecycle-demo",),
        )

    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=Execution(),
        projection_adapter=Projection(),
        evaluation_store=EvaluationResultStore(tmp_path / "evaluations.jsonl"),
        activation_run_one=successful_protocol,
    )
    prepare_activation = governance.prepare_activation
    governance.prepare_activation = lambda target: prepare_activation(  # type: ignore[method-assign]
        target,
        run_one=successful_protocol,
    )

    no_skill = _route(kind=AnalysisRouteKind.NO_SKILL, skill="")
    assert no_skill.kind is AnalysisRouteKind.NO_SKILL
    creation_message = await execute_create_omics_skill(
        {
            "request": "Create a reusable analysis for an uncovered method.",
            "domain": "genomics",
            "skill_name": skill_id,
            "source": {"kind": "intent"},
            "create_tests": False,
        },
        skill_evolution_governance=governance,
    )

    candidate_path = skills_root / "genomics" / skill_id / "skill.yaml"
    candidate = load_skill_yaml(candidate_path)
    assert candidate.lifecycle.status == "draft"
    assert candidate.validation.level == "smoke-only"
    assert "Human approval required" in creation_message
    proposal = next(
        proposal
        for proposal in governance.proposals.list_latest()
        if proposal.kind == "skill_activation"
    )

    governance.approve(
        proposal.proposal_id,
        approver="local-human",
        reason="reviewed generated code and exact-revision evaluation evidence",
    )
    activated = load_skill_yaml(candidate_path)
    assert activated.lifecycle.status == "mvp"
    assert activated.validation.level == "demo-validated"
    probe = OmicsRegistry()
    probe.load_all(skills_root)
    assert is_skill_automatically_routable(probe.skills[skill_id])

    transcript = TranscriptStore()
    tool_results = ToolResultStore(storage_dir=tmp_path / "tool-results")
    llm = _LlmTrap()
    monkeypatch.setattr(
        agent_loop,
        "_build_bot_tool_context",
        lambda: BotToolContext(
            skill_names=(skill_id, "auto"),
            domain_briefing="Golden lifecycle test registry.",
        ),
    )
    deps = replace(
        agent_loop._build_engine_dependencies(
            transcript_store_override=transcript,
        ),
        llm=llm,
        transcript_store=transcript,
        tool_result_store=tool_results,
        skill_aliases=(skill_id,),
    )
    monkeypatch.setattr(agent_loop, "_build_engine_dependencies", lambda **_kwargs: deps)
    monkeypatch.setattr(
        agent_loop,
        "_route_user_text_with_input_state",
        lambda _text: _route(skill=skill_id),
    )
    runtime = _SuccessfulRunRuntime(skill_id)
    result = await agent_loop.llm_tool_loop(
        chat_id="lifecycle-golden-slice",
        user_content=f"请运行 {skill_id} demo",
        platform="cli",
        workspace=str(tmp_path),
        transcript_store_override=transcript,
        run_runtime=runtime,
    )

    assert llm.used is False
    assert [event[0] for event in runtime.events] == ["build", "submit", "wait"], result
    assert RUN_ID in result
    assert "/verified/output" in result
