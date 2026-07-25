"""An unknown tool call must cost one turn, not two.

Diagnosis 2026-07-25. A model guessed a tool named ``run_shell``; the runtime
answered with the whole of ``"Unknown tool: run_shell"``. With nothing to
correct it, the model spent its next tool iteration on ``tool_search`` just to
find out what the shell tool is actually called — two of the turn's twenty
iterations for one typo. Naming the nearest real tools makes the recovery
free.
"""

from __future__ import annotations

import asyncio

from omicsclaw.runtime.tools.orchestration import (
    EXECUTION_STATUS_UNKNOWN_TOOL,
    ToolExecutionRequest,
    execute_tool_requests,
)


def _run_unknown(name: str, known: tuple[str, ...]):
    request = ToolExecutionRequest(
        call_id="call-1",
        name=name,
        arguments={},
        spec=None,
        executor=None,
        known_tool_names=known,
    )
    results = asyncio.run(execute_tool_requests([request]))
    return results[0]


def test_unknown_tool_names_the_closest_available_tools():
    result = _run_unknown(
        "run_shell",
        ("run_shell_command", "list_directory", "file_read", "inspect_data"),
    )

    assert result.success is False
    assert result.status == EXECUTION_STATUS_UNKNOWN_TOOL
    assert "run_shell" in result.output
    # The recovery hint the model needs, so it does not spend a turn searching.
    assert "run_shell_command" in result.output


def test_unknown_tool_without_a_close_match_still_reports_cleanly():
    result = _run_unknown("totally_unrelated_xyz", ("file_read", "list_directory"))

    assert result.success is False
    assert result.status == EXECUTION_STATUS_UNKNOWN_TOOL
    assert "totally_unrelated_xyz" in result.output
