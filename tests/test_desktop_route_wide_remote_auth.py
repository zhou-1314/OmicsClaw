"""Public HTTP contract for the Desktop-wide remote bearer boundary."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from omicsclaw.remote import auth as remote_auth  # noqa: E402
from omicsclaw.surfaces.desktop import server  # noqa: E402


_REMOTE_TOKEN = "remote-token"
_DEDICATED_EVOLUTION_TOKEN = "a" * 64
_WRONG_EVOLUTION_TOKEN = "b" * 64


@pytest.fixture(autouse=True)
def _isolate_desktop_remote_authority_state():
    state = server.app.state
    sentinel = object()
    attributes = (
        remote_auth.AUTHORITY_STATE_ATTR,
        server._SKILL_EVOLUTION_AUTH_STATE_ATTR,
    )
    previous = {
        attribute: getattr(state, attribute, sentinel) for attribute in attributes
    }
    for attribute in attributes:
        if previous[attribute] is not sentinel:
            delattr(state, attribute)
    yield
    for attribute in attributes:
        if hasattr(state, attribute):
            delattr(state, attribute)
        if previous[attribute] is not sentinel:
            setattr(state, attribute, previous[attribute])


def _materialize_route_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "test", path)


def _capture_distinct_desktop_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", _REMOTE_TOKEN)
    monkeypatch.setenv(
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        _DEDICATED_EVOLUTION_TOKEN,
    )
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    server._capture_skill_evolution_bearer_authority(server.app, os.environ)


def _raw_headers(*values: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", value.encode("ascii")) for value in values]


async def _call_desktop_http_asgi(
    *,
    method: str,
    path: str,
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    forbid_body_read: bool = False,
    omit_raw_path: bool = False,
    query_string: bytes = b"",
    root_path: str = "",
) -> tuple[list[dict], bool]:
    sent: list[dict] = []
    receive_called = False
    request_delivered = False

    async def receive():
        nonlocal receive_called, request_delivered
        receive_called = True
        if forbid_body_read:
            raise AssertionError("authentication must reject before reading the body")
        if not request_delivered:
            request_delivered = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": query_string,
        "root_path": root_path,
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
    }
    if not omit_raw_path:
        scope["raw_path"] = raw_path if raw_path is not None else path.encode("ascii")
    await server.app(scope, receive, send)
    return sent, receive_called


def _response_status(messages: list[dict]) -> int:
    return next(
        int(message["status"])
        for message in messages
        if message["type"] == "http.response.start"
    )


def _response_json(messages: list[dict]) -> dict:
    payload = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(payload)


def _install_fake_health_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server,
        "_core",
        SimpleNamespace(
            LLM_PROVIDER_NAME="test-provider",
            OMICSCLAW_MODEL="test-model",
            _primary_skill_count=lambda: 0,
            get_skill_runner_python=lambda: "python",
        ),
    )


def test_configured_remote_bearer_protects_previously_public_skill_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get("/skills")

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}


def test_configured_remote_bearer_keeps_root_health_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    monkeypatch.setattr(
        server,
        "_core",
        SimpleNamespace(
            LLM_PROVIDER_NAME="test-provider",
            OMICSCLAW_MODEL="test-model",
            _primary_skill_count=lambda: 0,
            get_skill_runner_python=lambda: "python",
        ),
    )
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get(
        "/health",
        headers={"X-OmicsClaw-Expected-Backend-Process-Epoch": "malformed-probe"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": server.__version__,
        "launch_id": "",
        "auth_required": True,
    }
    assert "backend_process_epoch" not in response.json()


def test_root_health_returns_full_details_only_to_the_frozen_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import omicsclaw.autoagent.api as autoagent_api
    from omicsclaw.control import ControlStateRepository

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    monkeypatch.setattr(
        server,
        "_core",
        SimpleNamespace(
            LLM_PROVIDER_NAME="test-provider",
            OMICSCLAW_MODEL="test-model",
            _primary_skill_count=lambda: 0,
            get_skill_runner_python=lambda: "python",
        ),
    )
    with ControlStateRepository(tmp_path / "control") as repository:
        autoagent_api.bind_autoagent_repository(repository)
        monkeypatch.setattr(
            server,
            "_desktop_control_runtime",
            SimpleNamespace(repository=repository),
        )
        client = TestClient(server.app, raise_server_exceptions=False)

        try:
            wrong = client.get(
                "/health",
                headers={"Authorization": "Bearer wrong-token"},
            )
            authorized = client.get(
                "/health",
                headers={"Authorization": "Bearer correct-token"},
            )
            capabilities = client.get(
                "/autoagent/capabilities",
                headers={"Authorization": "Bearer correct-token"},
            )

            assert wrong.status_code == 401
            assert authorized.status_code == 200
            assert capabilities.status_code == 200
            assert authorized.json()["provider"] == "test-provider"
            assert (
                authorized.json()["backend_process_epoch"]
                == server._BACKEND_PROCESS_EPOCH
            )
            assert (
                authorized.json()["control_authority_id"]
                == repository.control_authority_id
                == capabilities.json()["control_authority_id"]
            )
            assert "auth_required" not in authorized.json()
            assert "python_executable" in authorized.json()
        finally:
            autoagent_api.unbind_autoagent_repository(repository)


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        pytest.param(None, 200, id="missing-is-public-liveness"),
        pytest.param(
            {"Authorization": "Bearer wrong-token"},
            401,
            id="wrong-token-fails-closed",
        ),
        pytest.param(
            [
                ("Authorization", "Bearer correct-token"),
                ("Authorization", "Bearer correct-token"),
            ],
            401,
            id="duplicate-token-fails-closed",
        ),
        pytest.param(
            {"Authorization": "Bearer correct-token"},
            200,
            id="correct-token-is-authorized",
        ),
    ],
)
def test_root_health_head_has_explicit_authentication_semantics(
    monkeypatch: pytest.MonkeyPatch,
    headers,
    expected_status: int,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    _install_fake_health_core(monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.request("HEAD", "/health", headers=headers)

    assert response.status_code == expected_status
    assert response.content == b""
    if expected_status == 401:
        assert response.headers["www-authenticate"] == (
            'Bearer realm="omicsclaw-remote"'
        )


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.asyncio
async def test_route_relative_root_health_remains_the_public_liveness_exception(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    monkeypatch.setenv("OMICSCLAW_DESKTOP_LAUNCH_ID", "root-path-launch")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    _install_fake_health_core(monkeypatch)
    full_path = "/api/health"

    messages, _receive_called = await _call_desktop_http_asgi(
        method=method,
        path=full_path,
        raw_path=full_path.encode("ascii"),
        root_path="/api",
    )

    assert _response_status(messages) == 200
    assert _response_json(messages) == {
        "status": "ok",
        "version": server.__version__,
        "launch_id": "root-path-launch",
        "auth_required": True,
    }


@pytest.mark.asyncio
async def test_unverifiable_route_relative_health_is_not_a_public_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    _install_fake_health_core(monkeypatch)

    messages, receive_called = await _call_desktop_http_asgi(
        method="GET",
        path="/api/health",
        raw_path=b"/api/ordinary-clean-path",
        root_path="/api",
        forbid_body_read=True,
    )

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "missing bearer token"}
    assert receive_called is False


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        pytest.param(None, 401, id="missing"),
        pytest.param(
            {"Authorization": "Bearer wrong-token"},
            401,
            id="wrong",
        ),
        pytest.param(
            [
                ("Authorization", "Bearer correct-token"),
                ("Authorization", "Bearer correct-token"),
            ],
            401,
            id="duplicate",
        ),
        pytest.param(
            {"Authorization": "Bearer correct-token"},
            307,
            id="correct-reaches-router",
        ),
    ],
)
def test_health_trailing_slash_is_not_a_public_liveness_exception(
    monkeypatch: pytest.MonkeyPatch,
    headers,
    expected_status: int,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.request(
        "HEAD",
        "/health/",
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == expected_status


def test_remote_bearer_authority_is_frozen_for_one_app_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_a = "token-a"
    token_b = "token-b"
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", token_a)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        remote_auth.capture_remote_bearer_authority(app, os.environ)
        yield

    protected = FastAPI(lifespan=lifespan)
    protected.add_middleware(remote_auth.RemoteBearerMiddleware)

    @protected.get("/protected")
    async def read_protected():
        return {"ok": True}

    with TestClient(protected) as client:
        assert (
            client.get(
                "/protected",
                headers={"Authorization": f"Bearer {token_a}"},
            ).status_code
            == 200
        )

        monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", token_b)

        assert (
            client.get(
                "/protected",
                headers={"Authorization": f"Bearer {token_a}"},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/protected",
                headers={"Authorization": f"Bearer {token_b}"},
            ).status_code
            == 401
        )


def test_lifespan_disabled_app_never_reads_remote_authority_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "late-token-a")
    protected = FastAPI()
    protected.add_middleware(remote_auth.RemoteBearerMiddleware)
    downstream_bodies: list[bytes] = []

    @protected.post("/protected")
    async def mutate(request: Request):
        downstream_bodies.append(await request.body())
        return {"ok": True}

    client = TestClient(protected, raise_server_exceptions=False)
    first = client.post(
        "/protected",
        headers={"Authorization": "Bearer late-token-a"},
        content=b"first-body",
    )
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "late-token-b")
    second = client.post(
        "/protected",
        headers={"Authorization": "Bearer late-token-b"},
        content=b"second-body",
    )

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.json() == {"detail": "remote bearer authority is not initialized"}
    assert second.json() == {"detail": "remote bearer authority is not initialized"}
    assert downstream_bodies == []


@pytest.mark.asyncio
async def test_desktop_lifespan_releases_both_authorities_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", _REMOTE_TOKEN)

    @asynccontextmanager
    async def failing_runtime(_app: FastAPI):
        raise RuntimeError("synthetic startup failure")
        yield  # pragma: no cover - establishes the async context-manager shape

    monkeypatch.setattr(server, "_desktop_runtime_lifespan", failing_runtime)

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        async with server.lifespan(server.app):
            pass

    assert not hasattr(server.app.state, remote_auth.AUTHORITY_STATE_ATTR)
    assert not hasattr(
        server.app.state,
        server._SKILL_EVOLUTION_AUTH_STATE_ATTR,
    )


def test_configured_remote_bearer_rejects_unauthenticated_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    protected.add_middleware(remote_auth.RemoteBearerMiddleware)

    @protected.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    client = TestClient(protected)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass

    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_real_uvicorn_rejects_unauthenticated_websocket_handshake_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin Uvicorn's wire behavior, which differs from TestClient's close code."""

    uvicorn = pytest.importorskip("uvicorn")
    websockets = pytest.importorskip("websockets")
    from websockets.exceptions import InvalidStatus

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    protected.add_middleware(remote_auth.RemoteBearerMiddleware)

    @protected.websocket("/future-ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    instance = uvicorn.Server(
        uvicorn.Config(
            protected,
            log_level="critical",
            lifespan="off",
        )
    )
    serve_task = asyncio.create_task(instance.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if instance.started:
                break
            await asyncio.sleep(0.01)
        assert instance.started is True

        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/future-ws",
                proxy=None,
            ):
                pass

        assert exc_info.value.response.status_code == 403
    finally:
        instance.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)


@pytest.mark.asyncio
async def test_real_uvicorn_encoded_delegated_path_uses_remote_authority_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", _REMOTE_TOKEN)
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    protected.add_middleware(
        remote_auth.RemoteBearerMiddleware,
        delegated_path_prefixes=("/skill-evolution",),
        delegated_policy_resolver=lambda _scope: remote_auth.BearerGatePolicy(
            token=_DEDICATED_EVOLUTION_TOKEN,
            realm="omicsclaw-skill-evolution",
        ),
    )
    downstream_bodies: list[bytes] = []

    @protected.post("/skill-evolution/{remainder:path}")
    async def future_mutation(request: Request, remainder: str):
        downstream_bodies.append(await request.body())
        return {"remainder": remainder}

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    instance = uvicorn.Server(
        uvicorn.Config(protected, log_level="critical", lifespan="off")
    )
    serve_task = asyncio.create_task(instance.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if instance.started:
                break
            await asyncio.sleep(0.01)
        assert instance.started is True

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            (
                "POST /skill-evolution/%252e%252e/mutate HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {_DEDICATED_EVOLUTION_TOKEN}\r\n"
                "Content-Length: 16\r\n"
                "Connection: close\r\n"
                "\r\n"
                "scientific-input"
            ).encode("ascii")
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        await writer.wait_closed()

        assert response.startswith(b"HTTP/1.1 401 Unauthorized\r\n")
        assert b'www-authenticate: Bearer realm="omicsclaw-remote"' in response
        assert downstream_bodies == []
    finally:
        instance.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)


def test_every_registered_http_route_is_gated_except_declared_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    checked: list[tuple[str, str]] = []
    for route in server.app.routes:
        path = getattr(route, "path", "")
        methods = sorted(getattr(route, "methods", ()) or ())
        if path == "/skill-evolution" or path.startswith("/skill-evolution/"):
            continue
        for method in methods:
            if path == "/health" and method in {"GET", "HEAD"}:
                continue
            response = client.request(method, _materialize_route_path(path))
            assert response.status_code == 401, (method, path, response.status_code)
            checked.append((method, path))

    assert len(checked) >= 90


def test_unknown_routes_are_hidden_behind_the_same_remote_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    assert client.get("/route-that-does-not-exist").status_code == 401
    assert (
        client.get(
            "/route-that-does-not-exist",
            headers={"Authorization": "Bearer correct-token"},
        ).status_code
        == 404
    )


def test_public_and_delegated_paths_use_exact_slash_delimited_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    assert client.post("/health").status_code == 401
    assert client.get("/health/").status_code == 401
    assert client.get("/healthcheck").status_code == 401
    assert client.get("/skill-evolutionary").status_code == 401

    delegated = client.get("/skill-evolution")
    assert delegated.status_code == 503
    assert delegated.json()["detail"] == (
        "skill evolution bearer token is not configured"
    )


@pytest.mark.parametrize(
    ("headers", "expected_detail"),
    [
        pytest.param([], "missing bearer token", id="missing"),
        pytest.param(
            _raw_headers(f"Bearer {_WRONG_EVOLUTION_TOKEN}"),
            "invalid bearer token",
            id="wrong",
        ),
        pytest.param(
            _raw_headers(
                f"Bearer {_DEDICATED_EVOLUTION_TOKEN}",
                f"Bearer {_DEDICATED_EVOLUTION_TOKEN}",
            ),
            "invalid bearer token",
            id="duplicate",
        ),
    ],
)
@pytest.mark.asyncio
async def test_delegated_skill_evolution_http_auth_rejects_at_raw_asgi_boundary(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[bytes, bytes]],
    expected_detail: str,
) -> None:
    """Delegation authenticates before a future route can read or mutate."""

    _capture_distinct_desktop_authorities(monkeypatch)
    downstream_called = False

    async def future_mutation(request: Request):
        nonlocal downstream_called
        downstream_called = True
        await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/future-mutation",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path="/skill-evolution/future-mutation",
            headers=headers,
            forbid_body_read=True,
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": expected_detail}
    assert downstream_called is False
    assert receive_called is False


@pytest.mark.asyncio
async def test_delegated_skill_evolution_uses_frozen_dedicated_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    monkeypatch.setenv(
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        _WRONG_EVOLUTION_TOKEN,
    )
    received_body = b""

    async def future_mutation(request: Request):
        nonlocal received_body
        received_body = await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/future-mutation",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path="/skill-evolution/future-mutation",
            headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
            body=b"authenticated-body",
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 200
    assert _response_json(messages) == {"ok": True}
    assert receive_called is True
    assert received_body == b"authenticated-body"


@pytest.mark.asyncio
async def test_remote_bearer_cannot_substitute_for_distinct_evolution_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    downstream_called = False

    async def future_mutation(request: Request):
        nonlocal downstream_called
        downstream_called = True
        await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/future-mutation",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path="/skill-evolution/future-mutation",
            headers=_raw_headers(f"Bearer {_REMOTE_TOKEN}"),
            forbid_body_read=True,
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "invalid bearer token"}
    assert downstream_called is False
    assert receive_called is False


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/skill-evolution/../skills", id="dot-segment"),
        pytest.param(r"/skill-evolution\..\skills", id="backslash"),
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_paths_never_select_the_delegated_authority(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)

    messages, receive_called = await _call_desktop_http_asgi(
        method="POST",
        path=path,
        headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
        forbid_body_read=True,
    )

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "invalid bearer token"}
    assert receive_called is False


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        pytest.param(
            "/skill-evolution/../skills",
            b"/skill-evolution/%2e%2e/skills",
            id="single-encoded-dot-segment",
        ),
        pytest.param(
            "/skill-evolution/%2e%2e/skills",
            b"/skill-evolution/%252e%252e/skills",
            id="double-encoded-dot-segment",
        ),
        pytest.param(
            r"/skill-evolution/\skills",
            b"/skill-evolution/%5cskills",
            id="single-encoded-backslash",
        ),
        pytest.param(
            "/skill-evolution/%5cskills",
            b"/skill-evolution/%255cskills",
            id="double-encoded-backslash",
        ),
        pytest.param(
            "/skill-evolution/child/skills",
            b"/skill-evolution/child%2fskills",
            id="single-encoded-separator",
        ),
        pytest.param(
            "/skill-evolution/child%2fskills",
            b"/skill-evolution/child%252fskills",
            id="double-encoded-separator",
        ),
        pytest.param(
            "/skill-evolution/\ufffd",
            b"/skill-evolution/%ff",
            id="invalid-utf8-escape",
        ),
    ],
)
@pytest.mark.asyncio
async def test_encoded_routing_syntax_never_selects_delegated_authority(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    raw_path: bytes,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)

    messages, receive_called = await _call_desktop_http_asgi(
        method="POST",
        path=path,
        raw_path=raw_path,
        headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
        forbid_body_read=True,
    )

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "invalid bearer token"}
    assert receive_called is False


@pytest.mark.parametrize(
    ("path", "raw_path", "omit_raw_path"),
    [
        pytest.param(
            "/skill-evolution/\ufffd",
            b"/skill-evolution/\xff",
            False,
            id="non-ascii-original-byte",
        ),
        pytest.param(
            "/skill-evolution/future-mutation",
            b"/ordinary-clean-path",
            False,
            id="clean-raw-decoded-mismatch",
        ),
        pytest.param(
            "/skill-evolution/future-mutation",
            None,
            True,
            id="missing-raw-path",
        ),
        pytest.param(
            "/skill-evolution/future-mutation",
            b"/skill-evolution/%",
            False,
            id="malformed-percent-escape",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unverifiable_raw_paths_never_select_delegated_authority(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    raw_path: bytes | None,
    omit_raw_path: bool,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)

    messages, receive_called = await _call_desktop_http_asgi(
        method="POST",
        path=path,
        raw_path=raw_path,
        omit_raw_path=omit_raw_path,
        headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
        forbid_body_read=True,
    )

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "invalid bearer token"}
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert (b"www-authenticate", b'Bearer realm="omicsclaw-remote"') in (
        response_start["headers"]
    )
    assert receive_called is False


@pytest.mark.parametrize(
    ("path", "raw_path", "expected_remainder"),
    [
        pytest.param(
            "/skill-evolution/raw-path-proof/ascii",
            b"/skill-evolution/raw-path-proof/ascii",
            "ascii",
            id="ordinary-ascii-path",
        ),
        pytest.param(
            "/skill-evolution/raw-path-proof/\u6d4b\u8bd5",
            b"/skill-evolution/raw-path-proof/%E6%B5%8B%E8%AF%95",
            "\u6d4b\u8bd5",
            id="percent-encoded-utf8-path",
        ),
    ],
)
@pytest.mark.asyncio
async def test_equivalent_raw_and_decoded_paths_retain_delegated_authority(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    raw_path: bytes,
    expected_remainder: str,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    received: dict[str, object] = {}

    async def future_mutation(request: Request):
        received["remainder"] = request.path_params["remainder"]
        received["query"] = request.query_params.get("marker")
        received["body"] = await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/raw-path-proof/{remainder:path}",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path=path,
            raw_path=raw_path,
            query_string=b"marker=%2Fhealth",
            headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
            body=b"authenticated-body",
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 200
    assert _response_json(messages) == {"ok": True}
    assert receive_called is True
    assert received == {
        "remainder": expected_remainder,
        "query": "/health",
        "body": b"authenticated-body",
    }


@pytest.mark.asyncio
async def test_root_path_named_like_delegated_namespace_cannot_delegate_ordinary_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    downstream_called = False

    async def ordinary_mutation(request: Request):
        nonlocal downstream_called
        downstream_called = True
        await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/ordinary-root-path-proof",
        ordinary_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        full_path = "/skill-evolution/ordinary-root-path-proof"
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path=full_path,
            raw_path=full_path.encode("ascii"),
            root_path="/skill-evolution",
            headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
            forbid_body_read=True,
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "invalid bearer token"}
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert (b"www-authenticate", b'Bearer realm="omicsclaw-remote"') in (
        response_start["headers"]
    )
    assert downstream_called is False
    assert receive_called is False


@pytest.mark.asyncio
async def test_route_relative_delegated_path_uses_dedicated_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    received_body = b""

    async def future_mutation(request: Request):
        nonlocal received_body
        received_body = await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/root-path-proof",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        full_path = "/api/skill-evolution/root-path-proof"
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path=full_path,
            raw_path=full_path.encode("ascii"),
            root_path="/api",
            headers=_raw_headers(f"Bearer {_DEDICATED_EVOLUTION_TOKEN}"),
            body=b"authenticated-body",
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 200
    assert _response_json(messages) == {"ok": True}
    assert receive_called is True
    assert received_body == b"authenticated-body"


@pytest.mark.parametrize(
    "raw_path",
    [
        pytest.param(
            b"/api/skill-evolution/%",
            id="malformed-percent-escape",
        ),
        pytest.param(
            b"/api/ordinary-clean-path",
            id="clean-raw-decoded-mismatch",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unverifiable_route_relative_delegated_path_never_downgrades_to_remote(
    monkeypatch: pytest.MonkeyPatch,
    raw_path: bytes,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    downstream_called = False

    async def future_mutation(request: Request):
        nonlocal downstream_called
        downstream_called = True
        await request.body()
        return JSONResponse({"ok": True})

    route = Route(
        "/skill-evolution/unverifiable-root-path-proof",
        future_mutation,
        methods=["POST"],
    )
    server.app.router.routes.append(route)
    try:
        full_path = "/api/skill-evolution/unverifiable-root-path-proof"
        messages, receive_called = await _call_desktop_http_asgi(
            method="POST",
            path=full_path,
            raw_path=raw_path,
            root_path="/api",
            headers=_raw_headers(f"Bearer {_REMOTE_TOKEN}"),
            forbid_body_read=True,
        )
    finally:
        server.app.router.routes.remove(route)

    assert _response_status(messages) == 400
    assert _response_json(messages) == {"detail": "ambiguous delegated request path"}
    assert downstream_called is False
    assert receive_called is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("OPTIONS", "/skill-evolution", id="bare-options"),
        pytest.param(
            "GET",
            "/skill-evolution/future-unknown-descendant",
            id="unknown-descendant",
        ),
    ],
)
@pytest.mark.asyncio
async def test_delegated_skill_evolution_auth_precedes_router_resolution(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)

    messages, _receive_called = await _call_desktop_http_asgi(
        method=method,
        path=path,
    )

    assert _response_status(messages) == 401
    assert _response_json(messages) == {"detail": "missing bearer token"}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(None, id="missing"),
        pytest.param(
            {"Authorization": f"Bearer {_WRONG_EVOLUTION_TOKEN}"},
            id="wrong",
        ),
    ],
)
def test_future_delegated_websocket_rejects_before_downstream(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str] | None,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    downstream_calls: list[str] = []

    async def future_stream(websocket: WebSocket):
        downstream_calls.append("connected")
        await websocket.accept()
        await websocket.send_text("connected")

    route = WebSocketRoute(
        "/skill-evolution/future-stream",
        future_stream,
    )
    server.app.router.routes.append(route)
    client = TestClient(server.app)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/skill-evolution/future-stream",
                headers=headers or {},
            ):
                pass
    finally:
        server.app.router.routes.remove(route)

    assert exc_info.value.code == 1008
    assert downstream_calls == []


def test_future_delegated_websocket_accepts_frozen_dedicated_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_distinct_desktop_authorities(monkeypatch)
    monkeypatch.setenv(
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        _WRONG_EVOLUTION_TOKEN,
    )
    downstream_calls: list[str] = []

    async def future_stream(websocket: WebSocket):
        downstream_calls.append("connected")
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    route = WebSocketRoute(
        "/skill-evolution/future-stream",
        future_stream,
    )
    server.app.router.routes.append(route)
    client = TestClient(server.app)
    try:
        with client.websocket_connect(
            "/skill-evolution/future-stream",
            headers={
                "Authorization": f"Bearer {_DEDICATED_EVOLUTION_TOKEN}",
            },
        ) as websocket:
            assert websocket.receive_text() == "connected"
    finally:
        server.app.router.routes.remove(route)

    assert downstream_calls == ["connected"]


def test_cors_preflight_is_public_but_bare_options_is_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    client = TestClient(server.app, raise_server_exceptions=False)

    preflight = client.options(
        "/skills",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    bare_options = client.options("/skills")

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert bare_options.status_code == 401


@pytest.mark.asyncio
async def test_remote_gate_rejects_before_reading_the_http_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    authority_app = FastAPI()
    remote_auth.capture_remote_bearer_authority(authority_app, os.environ)
    downstream_called = False
    receive_called = False
    sent: list[dict] = []

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        nonlocal receive_called
        receive_called = True
        raise AssertionError("unauthenticated body must not be consumed")

    async def send(message):
        sent.append(message)

    middleware = remote_auth.RemoteBearerMiddleware(downstream)
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/notebook/execute",
            "raw_path": b"/notebook/execute",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-length", b"1000000000")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8765),
            "app": authority_app,
        },
        receive,
        send,
    )

    assert downstream_called is False
    assert receive_called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_expected_backend_process_epoch_rejects_before_body_or_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    authority_app = FastAPI()
    remote_auth.capture_remote_bearer_authority(authority_app, os.environ)
    downstream_called = False
    receive_called = False
    sent: list[dict] = []

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        nonlocal receive_called
        receive_called = True
        raise AssertionError("epoch mismatch must reject before reading the body")

    async def send(message):
        sent.append(message)

    middleware = remote_auth.RemoteBearerMiddleware(
        downstream,
        backend_process_epoch_resolver=lambda _scope: "a" * 64,
    )
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/notebook/execute",
            "raw_path": b"/notebook/execute",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", b"Bearer correct-token"),
                (
                    b"x-omicsclaw-expected-backend-process-epoch",
                    b"b" * 64,
                ),
                (b"content-length", b"1000000000"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8765),
            "app": authority_app,
        },
        receive,
        send,
    )

    assert downstream_called is False
    assert receive_called is False
    assert _response_status(sent) == 409
    assert _response_json(sent) == {"detail": "backend_process_epoch_mismatch"}


def test_bearer_authentication_precedes_backend_process_epoch_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    route_calls: list[str] = []

    def unexpected_epoch_resolution(_scope):
        raise AssertionError("unauthenticated requests must not resolve the epoch")

    protected.add_middleware(
        remote_auth.RemoteBearerMiddleware,
        backend_process_epoch_resolver=unexpected_epoch_resolution,
    )

    @protected.post("/protected")
    async def mutate():
        route_calls.append("called")
        return {"ok": True}

    response = TestClient(protected, raise_server_exceptions=False).post(
        "/protected",
        headers={
            "X-OmicsClaw-Expected-Backend-Process-Epoch": "malformed",
        },
        content=b"must-not-reach-route",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}
    assert route_calls == []


def test_matching_backend_process_epoch_reaches_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    expected_epoch = "a" * 64
    protected.add_middleware(
        remote_auth.RemoteBearerMiddleware,
        backend_process_epoch_resolver=lambda _scope: expected_epoch,
    )

    @protected.post("/protected")
    async def mutate(request: Request):
        return {"body": (await request.body()).decode("ascii")}

    response = TestClient(protected).post(
        "/protected",
        headers={
            "Authorization": "Bearer correct-token",
            "X-OmicsClaw-Expected-Backend-Process-Epoch": expected_epoch,
        },
        content=b"route-body",
    )

    assert response.status_code == 200
    assert response.json() == {"body": "route-body"}


def test_missing_backend_process_epoch_header_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    protected.add_middleware(
        remote_auth.RemoteBearerMiddleware,
        backend_process_epoch_resolver=lambda _scope: "a" * 64,
    )

    @protected.get("/protected")
    async def read_protected():
        return {"ok": True}

    response = TestClient(protected).get(
        "/protected",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "epoch_headers",
    [
        pytest.param(
            [("X-OmicsClaw-Expected-Backend-Process-Epoch", "a" * 63)],
            id="malformed",
        ),
        pytest.param(
            [
                ("X-OmicsClaw-Expected-Backend-Process-Epoch", "a" * 64),
                ("X-OmicsClaw-Expected-Backend-Process-Epoch", "a" * 64),
            ],
            id="duplicate",
        ),
    ],
)
def test_invalid_backend_process_epoch_header_has_one_closed_response(
    monkeypatch: pytest.MonkeyPatch,
    epoch_headers: list[tuple[str, str]],
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "correct-token")
    protected = FastAPI()
    remote_auth.capture_remote_bearer_authority(protected, os.environ)
    route_calls: list[str] = []
    protected.add_middleware(
        remote_auth.RemoteBearerMiddleware,
        backend_process_epoch_resolver=lambda _scope: "a" * 64,
    )

    @protected.post("/protected")
    async def mutate():
        route_calls.append("called")
        return {"ok": True}

    response = TestClient(protected, raise_server_exceptions=False).post(
        "/protected",
        headers=[("Authorization", "Bearer correct-token"), *epoch_headers],
        content=b"must-not-reach-route",
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "backend_process_epoch_mismatch"}
    assert route_calls == []


def test_desktop_app_fences_expected_backend_process_epoch_before_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", _REMOTE_TOKEN)
    remote_auth.capture_remote_bearer_authority(server.app, os.environ)
    wrong_epoch = "0" * 64 if server._BACKEND_PROCESS_EPOCH != "0" * 64 else "1" * 64

    response = TestClient(server.app, raise_server_exceptions=False).get(
        "/route-that-must-not-be-resolved",
        headers={
            "Authorization": f"Bearer {_REMOTE_TOKEN}",
            "X-OmicsClaw-Expected-Backend-Process-Epoch": wrong_epoch,
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "backend_process_epoch_mismatch"}


@pytest.mark.asyncio
async def test_direct_asgi_non_loopback_scope_fails_closed_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)
    sent: list[dict] = []
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = remote_auth.RemoteBearerMiddleware(
        downstream,
        public_paths=("/health",),
    )
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("203.0.113.10", 1234),
            "server": ("0.0.0.0", 8765),
        },
        receive,
        send,
    )

    assert downstream_called is False
    assert sent[0]["status"] == 503
