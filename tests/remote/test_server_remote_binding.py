"""The desktop server's C2 binding: one ``POST /jobs``, run remotely.

What :func:`~omicsclaw.entry.desktop.server.create_desktop_app` owes the
jobs plane now that C1/C2 exist: a default ``JobsManager`` whose remote
runtime is the bridge over ``app.remote``, so a ``runtime:
"remote:<alias>"`` job a desktop client creates is submitted over SSH,
polled, and settled by the plane instead of failing with
``remote_runtime_unavailable`` — while a deployment without the plane
answers exactly as it did before the binding, and a caller that brings
its own ``jobs_manager`` is not re-wrapped.

The transport is the seam double, so the loop here is the production
one (route → manager → bridge → plane → store) minus the socket. The
bridge's poll cadence is the default, so the terminal-state wait below
allows a few intervals — this is the one deliberately slow test in the
family, kept real rather than parameterised for a private default.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("fastapi", reason="the Desktop HTTP adapter needs it")
testclient = pytest.importorskip("fastapi.testclient")

from omicsclaw.entry.desktop import create_desktop_app  # noqa: E402
from omicsclaw.entry.desktop.jobs_manager import (  # noqa: E402
    JobStore,
    JobsManager,
)
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from omicsclaw.memory.database import Database  # noqa: E402
from omicsclaw.remote.plane import RemotePlaneBinding  # noqa: E402

from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]  # noqa: E402
    Scripted,
    make_app,
)
from tests.remote._fakes import PROBE_OK, FakeSpawner  # noqa: E402

TERMINAL = ("succeeded", "failed", "canceled", "interrupted")


def _serving_spawner() -> FakeSpawner:
    """A transport whose host answers probe, mkdir, submit and — on the
    first poll — a nohup status of ``0``: the job ran and exited zero."""
    spawner = FakeSpawner()
    spawner.on("base64 -d", PROBE_OK)
    spawner.on("mkdir -p", "")
    spawner.on("setsid nohup", "4242")
    spawner.on("kill -0", "0")
    spawner.on("tail -c", "")
    return spawner


def _app_with_plane(tmp_path: pathlib.Path, spawner: FakeSpawner):
    app = attach_sessions(make_app(tmp_path, Scripted()), abandon_grace_s=None)
    object.__setattr__(
        app,
        "remote",
        RemotePlaneBinding(
            Database(":memory:"), spawner=spawner, workspace=tmp_path
        ),
    )
    return app


def _terminal_record(client, job_id: str, timeout_s: float = 40.0):
    deadline = time.monotonic() + timeout_s
    record = None
    while time.monotonic() < deadline:
        record = client.get("/jobs/" + job_id).json()["job"]
        if record["status"] in TERMINAL:
            return record
        time.sleep(0.25)
    return record


def test_on_routes_a_remote_job_through_the_bound_bridge(tmp_path):
    spawner = _serving_spawner()
    app = _app_with_plane(tmp_path, spawner)
    with testclient.TestClient(create_desktop_app(app)) as client:
        assert client.get("/health").json()["capabilities"]["remote_execution"] is True
        created = client.post(
            "/jobs",
            json={
                "kind": "skill_run",
                "skill": "",
                "inputs": {"command": "printf done"},
                "runtime": "remote:hpc1",
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["job"]["runtime"] == "remote:hpc1"
        job_id = payload["job_id"]
        record = _terminal_record(client, job_id)
        assert record is not None and record["status"] == "succeeded", record
        # the plane learned the host (the probe before submission) and
        # settled the handle: nothing is left in flight
        assert app.remote.host_card("hpc1") is not None
        assert app.remote.job_store().in_flight() == []
        # the job really went to hpc1's transport, not a local runner
        commands = spawner.ssh_commands()
        assert any("setsid nohup" in command for command in commands)


def test_a_caller_supplied_manager_is_not_rewrapped(tmp_path):
    """``jobs_manager=...`` keeps its own remote wiring: the adapter's
    binding equips only the default manager, so a test or embedding that
    brings its own stays exactly as configured."""
    spawner = _serving_spawner()
    app = _app_with_plane(tmp_path, spawner)
    own = JobsManager(
        app, store=JobStore(Database(":memory:")), remote_runtime=None
    )
    try:
        with testclient.TestClient(create_desktop_app(app, jobs_manager=own)) as client:
            refused = client.post(
                "/jobs",
                json={
                    "kind": "skill_run",
                    "skill": "",
                    "inputs": {"command": "true"},
                    "runtime": "remote:hpc1",
                },
            )
            assert refused.status_code == 409
            assert refused.json()["detail"] == "remote_runtime_unavailable"
    finally:
        own.store.database.close()
