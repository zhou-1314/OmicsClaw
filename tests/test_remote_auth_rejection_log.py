"""A bearer rejection explains itself in the Backend log.

``GET /health`` is public by design, so a client that never sends a credential
produces an endless ``200 OK`` / ``401 Unauthorized`` access-log stream with no
statement of what is wrong. These tests pin the WARNING that turns that stream
into a diagnosis, its throttle, and the guarantee that no candidate credential
reaches the log.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omicsclaw.remote.auth import (
    _REJECTION_LOG_INTERVAL_SECONDS,
    RemoteBearerMiddleware,
    capture_remote_bearer_authority,
)

TOKEN = "correct-token"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def guarded(caplog):
    clock = _Clock()
    app = FastAPI()

    @app.get("/health")
    async def _health():  # pragma: no cover - exercised through the client
        return {"status": "ok"}

    @app.get("/workspace")
    async def _workspace():  # pragma: no cover - never reached unauthenticated
        return {"workspace": "/tmp"}

    app.add_middleware(
        RemoteBearerMiddleware,
        public_paths=("/health",),
        monotonic=clock,
    )
    capture_remote_bearer_authority(app, {"OMICSCLAW_REMOTE_AUTH_TOKEN": TOKEN})
    with TestClient(app) as client:
        caplog.set_level(logging.WARNING, logger="omicsclaw.remote.auth")
        yield client, clock, caplog


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "omicsclaw.remote.auth"
    ]


def test_missing_credential_is_explained_once_per_interval(guarded):
    client, clock, caplog = guarded

    assert client.get("/workspace").status_code == 401
    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "GET /workspace" in messages[0]
    assert "missing bearer token" in messages[0]
    assert "OMICSCLAW_REMOTE_AUTH_TOKEN" in messages[0]
    # The log must warn against reading a public /health 200 as proof of auth.
    assert "/health" in messages[0]

    # A polling client must not be able to flood the log with the same reason.
    for _ in range(5):
        assert client.get("/workspace").status_code == 401
    assert len(_warnings(caplog)) == 1

    clock.now += _REJECTION_LOG_INTERVAL_SECONDS + 1
    assert client.get("/workspace").status_code == 401
    assert len(_warnings(caplog)) == 2


def test_a_wrong_credential_reports_its_own_distinct_reason(guarded):
    client, _clock, caplog = guarded

    assert client.get("/workspace").status_code == 401
    response = client.get(
        "/workspace",
        headers={"Authorization": "Bearer wrong-token-value"},
    )
    assert response.status_code == 401

    messages = _warnings(caplog)
    assert len(messages) == 2
    assert "missing bearer token" in messages[0]
    assert "invalid bearer token" in messages[1]
    # The rejected candidate is a credential guess; it never reaches the log.
    assert not any("wrong-token-value" in message for message in messages)


def test_public_liveness_and_authenticated_traffic_stay_silent(guarded):
    client, _clock, caplog = guarded

    assert client.get("/health").status_code == 200
    assert (
        client.get("/workspace", headers={"Authorization": f"Bearer {TOKEN}"})
    ).status_code == 200
    assert _warnings(caplog) == []
