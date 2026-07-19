from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI

from omicsclaw.autoagent.api import router as autoagent_router
from omicsclaw.autoagent.http_guard import (
    AUTOAGENT_START_BODY_MAX_BYTES,
    AutoAgentStartBodyGuardMiddleware,
)
from omicsclaw.remote.auth import (
    AUTHORITY_STATE_ATTR,
    RemoteBearerAuthority,
    RemoteBearerMiddleware,
)


def _guarded_app(*, read_timeout_seconds: float = 60.0) -> FastAPI:
    app = FastAPI()
    app.include_router(autoagent_router)
    # Starlette inserts each new middleware at the front. The bearer gate is
    # therefore outside the body guard and authenticates before any body read.
    app.add_middleware(
        AutoAgentStartBodyGuardMiddleware,
        read_timeout_seconds=read_timeout_seconds,
    )
    app.add_middleware(RemoteBearerMiddleware)
    setattr(
        app.state,
        AUTHORITY_STATE_ATTR,
        RemoteBearerAuthority(token="test-bearer"),
    )
    return app


async def _invoke(
    app: FastAPI,
    *,
    headers: list[tuple[bytes, bytes]],
    messages: AsyncIterator[dict[str, Any]] | None = None,
    path: str = "/autoagent/start",
    method: str = "POST",
) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages is None:
            raise AssertionError("request body must not be read")
        return await anext(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8765),
        },
        receive,
        send,
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return int(start["status"]), body


async def _messages(*messages: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    for message in messages:
        yield message


@pytest.mark.asyncio
async def test_autoagent_start_rejects_oversized_content_length_without_body_read() -> (
    None
):
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", str(AUTOAGENT_START_BODY_MAX_BYTES + 1).encode()),
        ],
    )

    assert status == 413
    assert body == b'{"detail":"AutoAgent start request body is too large"}'


@pytest.mark.asyncio
async def test_autoagent_start_authentication_precedes_body_guard() -> None:
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer wrong-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", str(AUTOAGENT_START_BODY_MAX_BYTES + 1).encode()),
        ],
    )

    assert status == 401
    assert body == b'{"detail":"invalid bearer token"}'


@pytest.mark.asyncio
async def test_autoagent_start_rejects_unbounded_numeric_content_length() -> None:
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", b"9" * 10_000),
        ],
    )

    assert status == 400
    assert body == b'{"detail":"Invalid AutoAgent start Content-Length"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    (
        (b" " * (AUTOAGENT_START_BODY_MAX_BYTES + 1),),
        (b" " * AUTOAGENT_START_BODY_MAX_BYTES, b"{}"),
        (b'{"skill":"x","method":"m"}', b" " * AUTOAGENT_START_BODY_MAX_BYTES),
    ),
)
async def test_autoagent_start_counts_every_chunk_before_late_valid_json(
    parts: tuple[bytes, ...],
) -> None:
    messages = tuple(
        {
            "type": "http.request",
            "body": part,
            "more_body": index < len(parts) - 1,
        }
        for index, part in enumerate(parts)
    )
    status, _body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
        ],
        messages=_messages(*messages),
    )

    assert status == 413


@pytest.mark.asyncio
async def test_autoagent_start_rejects_duplicate_json_keys_before_model_or_endpoint() -> (
    None
):
    payload = b'{"skill":"first","skill":"second","method":"m"}'
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        messages=_messages(
            {"type": "http.request", "body": payload, "more_body": False}
        ),
    )

    assert status == 400
    assert body == b'{"detail":"Invalid AutoAgent start request body"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        b'{"fixed_params":' + b"[" * 1_000 + b"0" + b"]" * 1_000 + b"}",
        b'{"fixed_params":{"x":' + b"9" * 10_000 + b"}}",
    ),
)
async def test_autoagent_start_rejects_recursive_or_unbounded_numeric_json(
    payload: bytes,
) -> None:
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        messages=_messages(
            {"type": "http.request", "body": payload, "more_body": False}
        ),
    )

    assert status == 400
    assert body == b'{"detail":"Invalid AutoAgent start request body"}'


@pytest.mark.asyncio
async def test_autoagent_start_replays_valid_strict_json_to_fastapi_model() -> None:
    payload = b'{"skill":"only-skill-is-present"}'
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        messages=_messages(
            {"type": "http.request", "body": payload, "more_body": False}
        ),
    )

    assert status == 422
    assert b'"method"' in body


@pytest.mark.asyncio
async def test_autoagent_start_body_read_has_one_whole_request_deadline() -> None:
    release = asyncio.Event()

    async def stalled_messages() -> AsyncIterator[dict[str, Any]]:
        await release.wait()
        yield {"type": "http.request", "body": b"{}", "more_body": False}

    status, body = await _invoke(
        _guarded_app(read_timeout_seconds=0.01),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-type", b"application/json"),
        ],
        messages=stalled_messages(),
    )

    assert status == 408
    assert body == b'{"detail":"AutoAgent start request body read timed out"}'


@pytest.mark.asyncio
async def test_autoagent_body_guard_does_not_read_or_reject_other_routes() -> None:
    status, body = await _invoke(
        _guarded_app(),
        headers=[
            (b"authorization", b"Bearer test-bearer"),
            (b"content-length", str(AUTOAGENT_START_BODY_MAX_BYTES + 1).encode()),
        ],
        path="/autoagent/capabilities",
        method="GET",
    )

    assert status == 503
    assert body == b'{"detail":"AutoAgent lifecycle authority is unavailable"}'


def test_desktop_middleware_orders_bearer_before_autoagent_body_read() -> None:
    from omicsclaw.surfaces.desktop import server

    middleware = [entry.cls for entry in server.app.user_middleware]
    assert middleware.index(RemoteBearerMiddleware) < middleware.index(
        AutoAgentStartBodyGuardMiddleware
    )
