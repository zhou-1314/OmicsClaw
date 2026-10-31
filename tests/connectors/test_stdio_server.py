# SPDX-License-Identifier: Apache-2.0
"""The minimal stdio MCP server, message by message (no pipes, no network)."""

from __future__ import annotations

import io
import json

from omicsclaw.connectors.bio._common import ConnectorError, Tool
from omicsclaw.connectors.bio._server import KNOWN_PROTOCOL_VERSIONS, StdioServer


def _echo(args):
    return {"echo": args["text"]}


def _refuse(args):
    raise ConnectorError("upstream said no")


def _crash(args):
    raise RuntimeError("bug")


SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def server() -> StdioServer:
    return StdioServer(
        "demo",
        [
            Tool("echo", "Echo text.", SCHEMA, _echo),
            Tool("refuse", "Always fails.", {"type": "object"}, _refuse),
            Tool("crash", "Raises.", {"type": "object"}, _crash),
        ],
        instructions="demo server",
    )


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def test_initialize_echoes_a_known_protocol_version():
    answer = server().handle(request("initialize", {"protocolVersion": "2024-11-05"}))
    result = answer["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "omicsclaw-demo"
    assert result["instructions"] == "demo server"


def test_initialize_falls_back_to_the_newest_known_version():
    answer = server().handle(request("initialize", {"protocolVersion": "1999-01-01"}))
    assert answer["result"]["protocolVersion"] == KNOWN_PROTOCOL_VERSIONS[0]


def test_notifications_get_no_answer():
    assert server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_and_unknown_method():
    assert server().handle(request("ping"))["result"] == {}
    error = server().handle(request("resources/list"))["error"]
    assert error["code"] == -32601


def test_tools_list_keeps_order_and_schema():
    tools = server().handle(request("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo", "refuse", "crash"]
    assert tools[0]["inputSchema"] == SCHEMA


def test_tools_call_success_is_json_text():
    result = server().handle(
        request("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    )["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"echo": "hi"}


def test_tools_call_missing_required_argument_is_a_tool_error():
    result = server().handle(request("tools/call", {"name": "echo", "arguments": {}}))[
        "result"
    ]
    assert result["isError"] is True
    assert "text" in result["content"][0]["text"]


def test_connector_error_and_bug_both_become_is_error_results():
    for name, needle in (("refuse", "upstream said no"), ("crash", "RuntimeError")):
        result = server().handle(request("tools/call", {"name": name}))["result"]
        assert result["isError"] is True
        assert needle in result["content"][0]["text"]


def test_unknown_tool_is_invalid_params():
    error = server().handle(request("tools/call", {"name": "nope"}))["error"]
    assert error["code"] == -32602


def test_run_loop_answers_each_line_and_skips_garbage():
    lines = "\n".join(
        [
            json.dumps(request("initialize", {"protocolVersion": "2025-06-18"}, 1)),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json",
            json.dumps(request("tools/list", None, 2)),
            json.dumps(request("tools/call", {"name": "echo", "arguments": {"text": "x"}}, 3)),
        ]
    )
    out = io.StringIO()
    assert server().run(io.StringIO(lines + "\n"), out) == 0
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    by_id = {a.get("id"): a for a in answers}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == 3
    assert by_id[3]["result"]["isError"] is False
    assert by_id[None]["error"]["code"] == -32700
