"""Bounded, duplex JSON lines. Stdout carries protocol frames only."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from omicsclaw.entry.desktop.turn_submission import parse_chat_stream_document

MAX_FRAME_BYTES = 1024 * 1024
MAX_PENDING = 32
MAX_HANDLERS = 16
MAX_RETIRED = 128
RETIRED_TTL_SECONDS = 3600


def decode_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid frame length")
    frame = parse_chat_stream_document(raw, max_request_bytes=MAX_FRAME_BYTES)
    if not isinstance(frame, dict) or frame.get("kind") not in {
        "request",
        "response",
        "event",
        "cancel",
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
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancelled: OrderedDict[str, float] = OrderedDict()
        self.completed_handlers: OrderedDict[str, float] = OrderedDict()
        self.closed = False
        self._write_lock = asyncio.Lock()

    def _remember(self, records, identifier):
        records[identifier] = time.monotonic() + RETIRED_TTL_SECONDS
        while len(records) > MAX_RETIRED:
            records.popitem(last=False)

    def _prune(self, records):
        now = time.monotonic()
        while records and next(iter(records.values())) <= now:
            records.popitem(last=False)

    def _finished_handler(self, identifier, task):
        self.tasks.pop(identifier, None)
        self._remember(self.completed_handlers, identifier)
        if not task.cancelled():
            task.exception()

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
            async with asyncio.timeout(timeout):
                await self.send(
                    {
                        "kind": "request",
                        "id": identifier,
                        "method": method,
                        "params": params,
                    }
                )
                return await future
        except (asyncio.CancelledError, TimeoutError):
            retired = self.pending.pop(identifier, None)
            if retired is not None:
                self._remember(self.cancelled, identifier)
            if not self.closed:
                try:
                    async with asyncio.timeout(1):
                        await self.send({"kind": "cancel", "id": identifier})
                except (TimeoutError, RpcError, ConnectionError):
                    pass
            raise
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
                self._prune(self.cancelled)
                self._prune(self.completed_handlers)
                if frame["kind"] == "response":
                    future = self.pending.get(frame["id"])
                    if frame["id"] in self.cancelled or (
                        future is not None and future.cancelled()
                    ):
                        self.cancelled.pop(frame["id"], None)
                        if future is not None and future.cancelled():
                            self.pending.pop(frame["id"], None)
                        continue
                    if future is None or future.done():
                        raise RpcError("unsolicited response")
                    if "error" in frame:
                        future.set_exception(RpcError("reverse request refused"))
                    else:
                        future.set_result(frame.get("result"))
                elif frame["kind"] == "request":
                    if (
                        frame["id"] in self.tasks
                        or frame["id"] in self.completed_handlers
                    ):
                        raise RpcError("request identity repeated")
                    if len(self.tasks) >= MAX_HANDLERS:
                        raise RpcError("request concurrency exceeded")
                    task = asyncio.create_task(self._handle(frame))
                    self.tasks[frame["id"]] = task
                    task.add_done_callback(
                        lambda done, identifier=frame["id"]: self._finished_handler(
                            identifier, done
                        )
                    )
                elif frame["kind"] == "cancel":
                    task = self.tasks.get(frame["id"])
                    if task is not None:
                        task.cancel()
                    elif frame["id"] not in self.completed_handlers:
                        raise RpcError("unknown cancellation identity")
                else:
                    raise RpcError("client events are not admitted")
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError("transport closed"))
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
