# SPDX-License-Identifier: Apache-2.0
"""A minimal Model Context Protocol server over stdio.

Speaks exactly what :mod:`omicsclaw.mcp` (the client side) needs and the
MCP specification requires of a tools-only server:

* newline-delimited JSON-RPC 2.0 on stdin / stdout, one message per line;
* ``initialize`` — answers with the client's protocol version when it is
  one this server knows, otherwise the newest one it knows;
* ``notifications/initialized`` and every other notification — accepted
  and ignored;
* ``ping`` — an empty result;
* ``tools/list`` — every tool, in one page;
* ``tools/call`` — runs the tool on a worker thread so a slow upstream
  does not block ``ping``; a :class:`~._common.ConnectorError` (or any
  other exception) becomes an ``isError`` result whose text says why.

Nothing but protocol messages is written to stdout; diagnostics go to
stderr.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any, Final

from omicsclaw.version import __version__

from ._common import ConnectorError, Tool

__all__ = ["KNOWN_PROTOCOL_VERSIONS", "StdioServer", "serve"]

KNOWN_PROTOCOL_VERSIONS: Final = ("2025-06-18", "2025-03-26", "2024-11-05")
"""Newest first."""

_PARSE_ERROR: Final = -32700
_INVALID_REQUEST: Final = -32600
_METHOD_NOT_FOUND: Final = -32601
_INVALID_PARAMS: Final = -32602

_MAX_RESULT_CHARS: Final = 200_000
"""A tool answer longer than this is cut before it is sent."""


class StdioServer:
    """One server: a name, a version, and its tools."""

    def __init__(
        self,
        name: str,
        tools: Sequence[Tool],
        *,
        instructions: str = "",
        max_workers: int = 4,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self._tools = {tool.name: tool for tool in tools}
        self._order = [tool.name for tool in tools]
        self._write_lock = threading.Lock()
        self._max_workers = max_workers

    # ---- message handling (pure, testable without pipes) ----------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The response to one decoded *message*, or ``None`` for a
        notification. ``tools/call`` is answered synchronously here; the
        stdio loop runs it on a worker thread instead."""
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            # A response to a request we never send; ignore it.
            return None
        if request_id is None:
            return None
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            return _error(request_id, _INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(
                request_id,
                {"tools": [self._tools[name].listing() for name in self._order]},
            )
        if method == "tools/call":
            return self._call(request_id, params)
        return _error(request_id, _METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in KNOWN_PROTOCOL_VERSIONS else KNOWN_PROTOCOL_VERSIONS[0]
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"omicsclaw-{self.name}", "version": __version__},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _call(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(request_id, _INVALID_PARAMS, f"unknown tool: {name!r}")
        if not isinstance(arguments, Mapping):
            return _error(request_id, _INVALID_PARAMS, "arguments must be an object")
        try:
            missing = [
                key
                for key in tool.input_schema.get("required", ())
                if arguments.get(key) in (None, "")
            ]
            if missing:
                raise ConnectorError(f"missing required argument(s): {', '.join(missing)}")
            answer = tool.handler(arguments)
            text = answer if isinstance(answer, str) else json.dumps(
                answer, ensure_ascii=False, indent=1
            )
            is_error = False
        except ConnectorError as exc:
            text, is_error = f"{tool.name} failed: {exc}", True
        except Exception as exc:  # a bug in a handler must not kill the server
            text, is_error = f"{tool.name} failed: {type(exc).__name__}: {exc}", True
        if len(text) > _MAX_RESULT_CHARS:
            text = text[:_MAX_RESULT_CHARS] + "\n[truncated]"
        return _result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )

    # ---- stdio loop -------------------------------------------------------

    def run(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
        """Serve until stdin closes. Returns the process exit code."""
        reader = stdin if stdin is not None else sys.stdin
        writer = stdout if stdout is not None else sys.stdout
        with ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix=f"{self.name}-tool"
        ) as pool:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self._send(writer, _error(None, _PARSE_ERROR, "invalid JSON"))
                    continue
                if isinstance(message, Mapping) and message.get("method") == "tools/call":
                    pool.submit(self._answer_later, writer, message)
                    continue
                response = self.handle(message)
                if response is not None:
                    self._send(writer, response)
        return 0

    def _answer_later(self, writer: IO[str], message: Mapping[str, Any]) -> None:
        response = self.handle(message)
        if response is not None:
            self._send(writer, response)

    def _send(self, writer: IO[str], message: Mapping[str, Any]) -> None:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            writer.write(text + "\n")
            writer.flush()


def serve(name: str, tools: Sequence[Tool], *, instructions: str = "") -> int:
    """Run a :class:`StdioServer` on this process's stdin and stdout."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    return StdioServer(name, tools, instructions=instructions).run()


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
