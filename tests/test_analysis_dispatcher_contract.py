"""Contracts for deterministic Autonomous Analysis Path dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


def _only_trust(monkeypatch, tmp_path: Path, *trusted: Path) -> None:
    """Make path resolution hermetic: trust only ``trusted`` and point the
    project-root / DATA_DIR fallbacks at an empty sandbox.

    ``validate_input_path`` falls back to ``OMICSCLAW_DIR`` and ``DATA_DIR``
    and treats anything under the project root as trusted, so merely setting
    ``OMICSCLAW_DATA_DIRS`` is not enough: a file with the same basename
    sitting in the real repo ``data/`` (present on a dev machine, gitignored in
    CI) would otherwise leak into the result. Patching the roots to empty tmp
    dirs closes every leak vector and keeps the test deterministic everywhere.
    """
    from omicsclaw.runtime.agent import state
    from omicsclaw.services import path_validation

    empty = tmp_path / "_empty_project_root"
    (empty / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "OMICSCLAW_DIR", empty)
    monkeypatch.setattr(state, "DATA_DIR", empty / "data")
    monkeypatch.setattr(path_validation, "TRUSTED_DATA_DIRS", list(trusted))


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


def test_extract_valid_input_paths_keeps_untrusted_paths_out_with_subdir_discovery(
    monkeypatch, tmp_path: Path
) -> None:
    """The recursive discovery fallback must not widen trust: a bare name that
    only matches a file *outside* the trusted dirs stays unresolved, and an
    explicit untrusted absolute path is still dropped."""
    from omicsclaw.analysis_router.dispatcher import extract_valid_input_paths

    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.h5ad").write_bytes(b"")
    _only_trust(monkeypatch, tmp_path, workspace)

    # bare name only present outside trusted dirs -> not discovered
    assert extract_valid_input_paths("分析 secret.h5ad") == []
    # explicit untrusted absolute path -> still rejected
    assert extract_valid_input_paths(f"use {outside / 'secret.h5ad'}") == []


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
