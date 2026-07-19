"""Pre-model transport guard for the exact AutoAgent start Adapter."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send


AUTOAGENT_START_BODY_MAX_BYTES = 1024 * 1024
AUTOAGENT_START_BODY_READ_TIMEOUT_SECONDS = 60.0
_AUTOAGENT_START_JSON_MAX_DEPTH = 12
_AUTOAGENT_START_JSON_MAX_NODES = 21_000
_AUTOAGENT_START_JSON_MAX_INTEGER_DIGITS = 128


class _BodyTooLargeError(ValueError):
    pass


class _InvalidBodyError(ValueError):
    pass


def _content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise _InvalidBodyError("duplicate Content-Length")
    raw = values[0]
    if (
        not raw
        or len(raw) > 20
        or any(character < 48 or character > 57 for character in raw)
    ):
        raise _InvalidBodyError("invalid Content-Length")
    return int(raw.decode("ascii"), 10)


async def _read_bounded_body(receive: Receive, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise _InvalidBodyError("request body ended unexpectedly")
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            raise _InvalidBodyError("request body chunk is invalid")
        size += len(chunk)
        if size > maximum:
            raise _BodyTooLargeError("request body exceeds transport bound")
        chunks.append(chunk)
        if not bool(message.get("more_body", False)):
            return b"".join(chunks)


def _strict_json_object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidBodyError("duplicate JSON key")
        result[key] = value
    return result


def _parse_bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _AUTOAGENT_START_JSON_MAX_INTEGER_DIGITS:
        raise _InvalidBodyError("JSON integer exceeds its digit bound")
    return int(value, 10)


def _validate_json_shape(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if (
            nodes > _AUTOAGENT_START_JSON_MAX_NODES
            or depth > _AUTOAGENT_START_JSON_MAX_DEPTH
        ):
            raise _InvalidBodyError("JSON body exceeds structural bounds")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise _InvalidBodyError("JSON body contains a non-finite number")
        elif item is None or isinstance(item, (str, int, bool)):
            continue
        else:  # pragma: no cover - json.loads cannot construct another type
            raise _InvalidBodyError("JSON body contains an unsupported value")


def _validate_strict_json(body: bytes) -> None:
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_strict_json_object_pairs,
            parse_int=_parse_bounded_json_integer,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _InvalidBodyError("non-finite JSON value")
            ),
        )
        if not isinstance(payload, dict):
            raise _InvalidBodyError("AutoAgent start body must be a JSON object")
        _validate_json_shape(payload)
    except (UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise _InvalidBodyError("invalid strict JSON body") from exc


class AutoAgentStartBodyGuardMiddleware:
    """Bound only ``POST /autoagent/start`` before FastAPI model parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_body_bytes: int = AUTOAGENT_START_BODY_MAX_BYTES,
        read_timeout_seconds: float = AUTOAGENT_START_BODY_READ_TIMEOUT_SECONDS,
    ) -> None:
        if maximum_body_bytes <= 0:
            raise ValueError("maximum_body_bytes must be positive")
        if read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        self._app = app
        self._maximum_body_bytes = int(maximum_body_bytes)
        self._read_timeout_seconds = float(read_timeout_seconds)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() == "POST"
            and get_route_path(scope) == "/autoagent/start"
        ):
            await self._app(scope, receive, send)
            return

        try:
            declared_length = _content_length(scope)
        except _InvalidBodyError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="Invalid AutoAgent start Content-Length",
            )
            return
        if declared_length is not None and declared_length > self._maximum_body_bytes:
            await self._reject(
                scope,
                receive,
                send,
                status_code=413,
                detail="AutoAgent start request body is too large",
            )
            return

        try:
            body = await asyncio.wait_for(
                _read_bounded_body(receive, maximum=self._maximum_body_bytes),
                timeout=self._read_timeout_seconds,
            )
        except TimeoutError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=408,
                detail="AutoAgent start request body read timed out",
            )
            return
        except _BodyTooLargeError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=413,
                detail="AutoAgent start request body is too large",
            )
            return
        except _InvalidBodyError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="Invalid AutoAgent start request body",
            )
            return

        if declared_length is not None and declared_length != len(body):
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="Invalid AutoAgent start Content-Length",
            )
            return
        try:
            _validate_strict_json(body)
        except _InvalidBodyError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="Invalid AutoAgent start request body",
            )
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await receive()

        await self._app(scope, replay_receive, send)


__all__ = [
    "AUTOAGENT_START_BODY_MAX_BYTES",
    "AUTOAGENT_START_BODY_READ_TIMEOUT_SECONDS",
    "AutoAgentStartBodyGuardMiddleware",
]
