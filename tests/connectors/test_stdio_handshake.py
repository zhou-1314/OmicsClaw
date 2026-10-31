# SPDX-License-Identifier: Apache-2.0
"""Every built-in connector starts as a real subprocess and completes the
MCP handshake through :class:`~omicsclaw.mcp.MCPManager` — the same path
``open_app`` takes. No upstream request is made: ``initialize`` and
``tools/list`` are answered locally, and the one tool call below fails
argument validation before any request would be sent."""

from __future__ import annotations

import asyncio

from omicsclaw.connectors.registry import (
    BUILTIN_CONNECTORS,
    merge_builtin_connectors,
    save_connector_state,
)
from omicsclaw.mcp import MCPManager, ServerState, parse_mcp_config
from omicsclaw.schema import ToolCall
from omicsclaw.tools import ToolRegistry, use_tool_context


def _approve(_request):
    return True


def test_all_builtin_connectors_connect_and_list_their_tools(tmp_path):
    for connector in BUILTIN_CONNECTORS:
        save_connector_state(tmp_path, connector.name, True)
    merged = merge_builtin_connectors(parse_mcp_config({}), tmp_path)
    assert merged.added == tuple(c.name for c in BUILTIN_CONNECTORS)

    async def scenario():
        async with MCPManager(merged.config, cwd=tmp_path, connect_timeout_s=60) as manager:
            return manager.statuses(), {tool.name for tool in manager.tools()}

    statuses, mounted = asyncio.run(scenario())
    by_name = {status.name: status for status in statuses}
    for connector in BUILTIN_CONNECTORS:
        status = by_name[connector.name]
        assert status.state is ServerState.CONNECTED, status.error
        assert status.info is not None
        assert status.info.name == f"omicsclaw-{connector.name}"
        assert tuple(detail.tool for detail in status.tools) == connector.tools
        assert {detail.name for detail in status.tools} <= mounted


def test_a_tool_error_comes_back_as_an_error_observation(tmp_path):
    save_connector_state(tmp_path, "chembl", True)
    merged = merge_builtin_connectors(parse_mcp_config({}), tmp_path)

    async def scenario():
        async with MCPManager(merged.config, cwd=tmp_path, connect_timeout_s=60) as manager:
            registry = ToolRegistry(manager.tools())
            name = next(t.name for t in manager.tools() if t.name.endswith("chembl_activities"))
            with use_tool_context(approval=_approve):
                return await registry.execute(ToolCall(id="1", name=name, arguments="{}"))

    result = asyncio.run(scenario())
    assert result.is_error is True
    assert "target_chembl_id" in result.output
