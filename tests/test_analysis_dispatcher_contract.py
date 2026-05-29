"""Contracts for deterministic Autonomous Analysis Path dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path

from omicsclaw.analysis_router import (
    AnalysisRoute,
    AnalysisRouteKind,
    build_analysis_tool_plan,
    build_partial_autonomous_continuation,
)
from omicsclaw.runtime.agent.query_engine import (
    QueryEngineCallbacks,
    QueryEngineContext,
    run_planned_tool_calls,
)
from omicsclaw.runtime.policy.policy import TOOL_POLICY_REQUIRE_APPROVAL
from omicsclaw.runtime.policy.state import ToolPolicyState
from omicsclaw.runtime.storage.tool_result import ToolResultStore
from omicsclaw.runtime.storage.transcript import TranscriptStore, sanitize_tool_history
from omicsclaw.runtime.tools.hooks import (
    EVENT_SESSION_START,
    HOOK_MODE_CONTEXT,
    LifecycleHookRuntime,
    LifecycleHookSpec,
)
from omicsclaw.runtime.tools.registry import ToolRegistry
from omicsclaw.runtime.tools.spec import APPROVAL_MODE_ASK, ToolSpec
from omicsclaw.skill.capability_resolver import CapabilityDecision


def _route(
    kind: AnalysisRouteKind,
    *,
    chosen_skill: str = "",
) -> AnalysisRoute:
    return AnalysisRoute(
        kind=kind,
        capability_decision=CapabilityDecision(
            query="test",
            coverage=kind.value,
            chosen_skill=chosen_skill,
            confidence=0.8,
        ),
    )


def test_exact_skill_plan_uses_existing_tool_for_missing_path_instead_of_demo() -> None:
    route = _route(AnalysisRouteKind.EXACT_SKILL, chosen_skill="sc-qc")

    plan = build_analysis_tool_plan(route, user_text="run sc-qc")

    assert plan is not None
    assert plan.calls == (
        (
            "omicsclaw",
            {
                "skill": "sc-qc",
                "mode": "path",
                "query": "run sc-qc",
            },
        ),
    )
    assert plan.final_message == ""


def test_no_skill_plan_validates_input_paths(monkeypatch, tmp_path: Path) -> None:
    trusted_dir = tmp_path / "trusted"
    trusted_dir.mkdir()
    data_path = trusted_dir / "counts.csv"
    data_path.write_text("gene,count\nA,1\n", encoding="utf-8")
    monkeypatch.setenv("OMICSCLAW_DATA_DIRS", str(trusted_dir))
    from omicsclaw.services import path_validation

    path_validation.TRUSTED_DATA_DIRS.clear()
    route = _route(AnalysisRouteKind.NO_SKILL)

    plan = build_analysis_tool_plan(
        route,
        user_text=f"compute a custom score from {data_path} and /etc/passwd",
    )

    assert plan is not None
    assert plan.calls == (
        (
            "autonomous_analysis_execute",
            {
                "goal": f"compute a custom score from {data_path} and /etc/passwd",
                "input_paths": [str(data_path.resolve())],
                "language": "python",
                "max_repair_attempts": 2,
            },
        ),
    )


def test_extract_valid_input_paths_resolves_bare_data_filenames(monkeypatch, tmp_path: Path) -> None:
    """A bare data filename (no path prefix) that exists in a trusted dir is
    resolved; one that doesn't exist, or a non-data extension, is ignored.

    Regression: ``_PATH_TOKEN_RE`` only matched ~/./../-prefixed paths, so a
    bare filename — the natural way a desktop user names a file — was never
    handed to ``validate_input_path`` (which already resolves trusted-dir names).
    """
    from omicsclaw.analysis_router.dispatcher import extract_valid_input_paths
    from omicsclaw.services import path_validation

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "demo.h5ad").write_bytes(b"")
    monkeypatch.setenv("OMICSCLAW_DATA_DIRS", str(trusted))
    path_validation.TRUSTED_DATA_DIRS.clear()

    # bare name flanked directly by CJK characters (real desktop input shape)
    assert extract_valid_input_paths("对demo.h5ad执行分析") == [
        str((trusted / "demo.h5ad").resolve())
    ]
    # a bare data filename that does not exist is not fabricated
    assert extract_valid_input_paths("分析 missing_file.h5ad") == []
    # a dotted word without a data extension is not treated as input
    assert extract_valid_input_paths("see notes.pdf for details") == []


def test_exact_skill_plan_resolves_bare_filename_in_trusted_dir(monkeypatch, tmp_path: Path) -> None:
    """End-to-end seam: an exact-skill route whose user text names a bare file
    (existing in a trusted dir) must pass ``file_path`` to the skill, not run
    path-less and report 'No input file available'."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    data_path = trusted / "slideseqv2_mouse_hippocampus.h5ad"
    data_path.write_bytes(b"")
    monkeypatch.setenv("OMICSCLAW_DATA_DIRS", str(trusted))
    from omicsclaw.services import path_validation

    path_validation.TRUSTED_DATA_DIRS.clear()
    route = _route(AnalysisRouteKind.EXACT_SKILL, chosen_skill="spatial-domains")

    query = "对slideseqv2_mouse_hippocampus.h5ad执行spatial niche的鉴定"
    plan = build_analysis_tool_plan(route, user_text=query)

    assert plan is not None
    assert plan.calls == (
        (
            "omicsclaw",
            {
                "skill": "spatial-domains",
                "mode": "path",
                "file_path": str(data_path.resolve()),
                "query": query,
            },
        ),
    )


def test_partial_continuation_uses_skill_output_as_upstream_reference(tmp_path: Path) -> None:
    upstream = tmp_path / "skill-output"
    upstream.mkdir()
    route = _route(AnalysisRouteKind.PARTIAL_SKILL, chosen_skill="sc-qc")

    continuation = build_partial_autonomous_continuation(
        route,
        user_text="run sc-qc and make a custom volcano plot",
        skill_output=f"sc-qc completed. Output: {upstream}\nQC summary here",
    )

    assert continuation is not None
    name, args = continuation
    assert name == "autonomous_analysis_execute"
    assert args["upstream_paths"] == [str(upstream.resolve())]
    assert "Matched built-in skill: sc-qc" in args["context"]


def test_planned_tool_calls_reuse_policy_approval_callbacks_and_transcript(tmp_path: Path) -> None:
    observed: dict[str, object] = {
        "calls": 0,
        "tool_calls": [],
        "tool_results": [],
        "approvals": [],
    }

    async def executor(args):
        observed["calls"] = int(observed["calls"]) + 1
        return f"ran:{args['goal']}"

    async def before_tool(request):
        observed["tool_calls"].append((request.name, request.arguments))
        return {"seen": True}

    async def after_tool(execution_result, result_record, tool_state):
        observed["tool_results"].append(
            (
                execution_result.request.name,
                result_record.content,
                tool_state,
            )
        )

    async def request_tool_approval(request, execution_result):
        observed["approvals"].append((request.name, execution_result.status))
        return {
            "behavior": "allow",
            "policy_state": {
                "surface": "cli",
                "approved_tool_names": ["autonomous_analysis_execute"],
            },
        }

    runtime = ToolRegistry(
        [
            ToolSpec(
                name="autonomous_analysis_execute",
                description="Autonomous",
                parameters={
                    "type": "object",
                    "properties": {"goal": {"type": "string"}},
                    "required": ["goal"],
                },
                approval_mode=APPROVAL_MODE_ASK,
                writes_workspace=True,
                policy_tags=("analysis", "autonomous"),
            )
        ]
    ).build_runtime({"autonomous_analysis_execute": executor})
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool-results")

    result = asyncio.run(
        run_planned_tool_calls(
            calls=[
                (
                    "autonomous_analysis_execute",
                    {"goal": "compute custom score"},
                )
            ],
            context=QueryEngineContext(
                chat_id="chat-planned",
                session_id="session-planned",
                system_prompt="SYSTEM",
                user_message_content="compute custom score",
                surface="cli",
                policy_state=ToolPolicyState(surface="cli"),
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
            callbacks=QueryEngineCallbacks(
                before_tool=before_tool,
                after_tool=after_tool,
                request_tool_approval=request_tool_approval,
            ),
        )
    )

    assert result.interruption_message == ""
    assert observed["calls"] == 1
    assert observed["approvals"] == [
        ("autonomous_analysis_execute", "policy_blocked")
    ]
    assert observed["tool_calls"] == [
        ("autonomous_analysis_execute", {"goal": "compute custom score"})
    ]
    assert observed["tool_results"][0][0] == "autonomous_analysis_execute"
    assert observed["tool_results"][0][1] == "ran:compute custom score"
    assert observed["tool_results"][0][2] == {"seen": True}
    assert result_store.get_records("chat-planned")[0].policy_action != (
        TOOL_POLICY_REQUIRE_APPROVAL
    )

    history = transcript_store.get_history("chat-planned")
    assert [message["role"] for message in history] == ["user", "assistant", "tool"]
    assert history[1]["tool_calls"][0]["function"]["name"] == (
        "autonomous_analysis_execute"
    )
    assert history[2]["content"] == "ran:compute custom score"


def test_planned_tool_calls_emit_session_lifecycle_hooks(tmp_path: Path) -> None:
    async def executor(args):
        return "ok"

    runtime = ToolRegistry(
        [
            ToolSpec(
                name="alpha",
                description="Alpha",
                parameters={"type": "object", "properties": {}},
            )
        ]
    ).build_runtime({"alpha": executor})
    hook_runtime = LifecycleHookRuntime(
        [
            LifecycleHookSpec(
                name="session-context",
                event=EVENT_SESSION_START,
                message="session started",
                mode=HOOK_MODE_CONTEXT,
            )
        ]
    )
    transcript_store = TranscriptStore(sanitizer=sanitize_tool_history)
    result_store = ToolResultStore(storage_dir=tmp_path / "tool-results")

    asyncio.run(
        run_planned_tool_calls(
            calls=[("alpha", {})],
            context=QueryEngineContext(
                chat_id="chat-hook",
                session_id="session-hook",
                system_prompt="SYSTEM",
                user_message_content="hello",
                surface="cli",
                hook_runtime=hook_runtime,
            ),
            tool_runtime=runtime,
            transcript_store=transcript_store,
            tool_result_store=result_store,
        )
    )

    assert hook_runtime.records
    assert hook_runtime.records[0].event.name == EVENT_SESSION_START
