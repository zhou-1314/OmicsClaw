"""Remote bearer-token policy for every Desktop chat mutation route."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from omicsclaw.remote import auth as remote_auth  # noqa: E402
from omicsclaw.surfaces.desktop import server  # noqa: E402


_CHAT_REQUESTS = (
    ("/chat/stream", {}, 422),
    ("/v1/turns", {}, 422),
    ("/chat/abort", {"session_id": "missing-session"}, 404),
    (
        "/chat/permission",
        {
            "permissionRequestId": f"perm_{'a' * 32}",
            "approvalToken": f"v1.{'A' * 43}",
            "decision": {},
        },
        200,
    ),
    ("/chat/session-permission-profile", {}, 422),
    ("/chat/title", {"messages": "not-a-list"}, 422),
)


@pytest.fixture()
def freeze_remote_authority():
    """Install one frozen authority and restore only while still its owner."""

    state = server.app.state
    attribute = remote_auth.AUTHORITY_STATE_ATTR
    sentinel = object()
    previous = getattr(state, attribute, sentinel)
    owned = None

    def freeze() -> None:
        nonlocal owned
        if owned is not None:
            raise AssertionError("remote authority was captured more than once")
        owned = remote_auth.capture_remote_bearer_authority(server.app, os.environ)

    yield freeze

    if owned is not None and getattr(state, attribute, sentinel) is owned:
        if previous is sentinel:
            remote_auth.release_remote_bearer_authority(server.app, owned)
        else:
            setattr(state, attribute, previous)


@pytest.mark.parametrize(("path", "payload", "_expected_status"), _CHAT_REQUESTS)
def test_remote_chat_routes_require_configured_token(
    monkeypatch: pytest.MonkeyPatch,
    freeze_remote_authority,
    path: str,
    payload: dict[str, object],
    _expected_status: int,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    freeze_remote_authority()
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(path, json=payload)

    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"


@pytest.mark.parametrize(("path", "payload", "_expected_status"), _CHAT_REQUESTS)
def test_remote_chat_routes_reject_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
    freeze_remote_authority,
    path: str,
    payload: dict[str, object],
    _expected_status: int,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    freeze_remote_authority()
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        path,
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid bearer token"


@pytest.mark.parametrize(("path", "payload", "expected_status"), _CHAT_REQUESTS)
def test_remote_chat_routes_accept_correct_token(
    monkeypatch: pytest.MonkeyPatch,
    freeze_remote_authority,
    path: str,
    payload: dict[str, object],
    expected_status: int,
) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    freeze_remote_authority()
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        path,
        json=payload,
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == expected_status


@pytest.mark.parametrize(("path", "payload", "expected_status"), _CHAT_REQUESTS)
def test_local_chat_routes_stay_compatible_when_token_is_unset(
    monkeypatch: pytest.MonkeyPatch,
    freeze_remote_authority,
    path: str,
    payload: dict[str, object],
    expected_status: int,
) -> None:
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)
    freeze_remote_authority()
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(path, json=payload)

    assert response.status_code == expected_status
