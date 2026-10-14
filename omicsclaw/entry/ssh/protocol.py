"""Bounded, duplex JSON lines. Stdout carries protocol frames only."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from omicsclaw.entry.desktop.turn_submission import parse_chat_stream_document

MAX_FRAME_BYTES = 1024 * 1024
MAX_PENDING = 32
MAX_HANDLERS = 16


def decode_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid frame length")
    frame = parse_chat_stream_document(raw, max_request_bytes=MAX_FRAME_BYTES)
    if not isinstance(frame, dict) or frame.get("kind") not in {
        "request",
        "response",
        "event",
    }:
        raise ValueError("invalid frame")
    if frame["kind"] != "event":
        identifier = frame.get("id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise ValueError("invalid request identity")
    return frame


class RpcError(RuntimeError):
    pass


class JsonPeer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        write: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.reader = reader
        self.write = write
        self.handlers: dict[str, Callable[[dict], Awaitable[Any]]] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self._write_lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        if self.closed:
            raise RpcError("transport closed")
        raw = (
            json.dumps(
                frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        if len(raw) > MAX_FRAME_BYTES:
            raise RpcError("frame exceeds limit")
        async with self._write_lock:
            await self.write(raw)

    async def call(self, method: str, params: dict, *, timeout: float = 120) -> Any:
        if len(self.pending) >= MAX_PENDING:
            raise RpcError("too many pending requests")
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send(
                {
                    "kind": "request",
                    "id": identifier,
                    "method": method,
                    "params": params,
                }
            )
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self.pending.pop(identifier, None)

    async def _handle(self, frame: dict) -> None:
        identifier = frame["id"]
        try:
            handler = self.handlers.get(frame.get("method"))
            if handler is None or not isinstance(frame.get("params"), dict):
                raise RpcError("method or parameters refused")
            result = await handler(frame["params"])
            await self.send({"kind": "response", "id": identifier, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - RPC error redaction
            # Exception text can contain model requests or credentials.
            if not self.closed:
                await self.send(
                    {
                        "kind": "response",
                        "id": identifier,
                        "error": {"code": type(exc).__name__},
                    }
                )

    async def run(self) -> None:
        try:
            while raw := await self.reader.readline():
                frame = decode_frame(raw)
                if frame["kind"] == "response":
                    future = self.pending.get(frame["id"])
                    if future is None or future.done():
                        raise RpcError("unsolicited response")
                    if "error" in frame:
                        future.set_exception(RpcError("reverse request refused"))
                    else:
                        future.set_result(frame.get("result"))
                elif frame["kind"] == "request":
                    if len(self.tasks) >= MAX_HANDLERS:
                        raise RpcError("request concurrency exceeded")
                    task = asyncio.create_task(self._handle(frame))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:
                    raise RpcError("client events are not admitted")
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError("transport closed"))
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
