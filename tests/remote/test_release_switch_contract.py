"""Deployment switch acceptance across config, assembly and Desktop HTTP."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.config import resolve_app_config
from omicsclaw.entry.desktop import create_desktop_app
from omicsclaw.entry.session import attach_sessions
from omicsclaw.observability import Telemetry
from tests.entry.test_desktop_http import body, frames_of
from tests.entry.test_turn_runner import Scripted


@pytest.mark.parametrize("enabled", [False, True], ids=["remote-off", "remote-on"])
def test_environment_switch_matches_bound_plane_and_health_without_disabling_desktop(
    tmp_path, enabled
):
    config = resolve_app_config(
        [],
        {
            "OMICSCLAW_WORKSPACE": str(tmp_path),
            "OMICSCLAW_REMOTE_EXECUTION": "1" if enabled else "0",
        },
    )
    provider = Scripted()
    app = attach_sessions(
        build_app(config, provider=provider, telemetry=Telemetry()),
        abandon_grace_s=None,
    )
    (tmp_path / "switch-proof.txt").write_text("desktop remains available\n")
    token = "switch-contract-token"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        assert config.remote_execution is enabled
        assert (app.remote is not None) is enabled
        assert (app.kernels.manager.remote_runtime is not None) is enabled
        assert (tmp_path / ".omicsclaw" / "remote.db").exists() is enabled
        with TestClient(create_desktop_app(app, bearer_token=token)) as client:
            health = client.get("/health", headers=headers)
            assert health.status_code == 200
            assert health.json()["capabilities"]["remote_execution"] is enabled
            assert health.json()["contracts"]["desktop_chat"]["sse_schema_version"] == 3
            assert client.get("/jobs", headers=headers).status_code == 200
            assert client.get("/files/tree", headers=headers).status_code == 200
            download = client.get(
                "/files/serve", params={"path": "switch-proof.txt"}, headers=headers
            )
            assert (download.status_code, download.text) == (
                200,
                "desktop remains available\n",
            )
            stream = client.post("/chat/stream", json=body(), headers=headers)
            assert stream.status_code == 200
            assert frames_of(stream.text)[-1] == {"type": "done", "data": ""}
            assert provider.calls == 1
    finally:
        asyncio.run(app.aclose())
