"""C1's REST face over the host knowledge base, over real HTTP.

The routes of :mod:`omicsclaw.entry.desktop.remote_hosts` through
``create_desktop_app``: the bearer gate, the off answer, the empty list,
a stored probe's card, a sanitized unreachable answer, alias injection
refused at the door, and delete semantics. The plane under them is a
real :class:`~omicsclaw.remote.plane.RemotePlaneBinding` over the
transport seam's double, so the probe path these tests exercise is the
production one minus the socket.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("fastapi", reason="the Desktop HTTP adapter needs it")
testclient = pytest.importorskip("fastapi.testclient")

from omicsclaw.entry.desktop import create_desktop_app  # noqa: E402
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from omicsclaw.memory.database import Database  # noqa: E402
from omicsclaw.remote.plane import RemotePlaneBinding  # noqa: E402
from omicsclaw.remote.ssh import RemoteHostUnreachable  # noqa: E402

from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]  # noqa: E402
    Scripted,
    make_app,
)
from tests.remote._fakes import PROBE_OK, FakeSpawner  # noqa: E402

KEY = "k" * 32
TERMINAL = ("succeeded", "failed", "canceled", "interrupted")


def _plane(
    tmp_path: pathlib.Path, spawner: FakeSpawner | None = None
) -> RemotePlaneBinding:
    return RemotePlaneBinding(
        Database(":memory:"),
        spawner=spawner if spawner is not None else FakeSpawner(),
        workspace=tmp_path,
    )


def _app(tmp_path: pathlib.Path, plane: RemotePlaneBinding | None = None):
    app = attach_sessions(make_app(tmp_path, Scripted()), abandon_grace_s=None)
    if plane is not None:
        # AgentApp is a frozen dataclass; the composition root is the
        # only writer of ``remote`` in production, and the test writes it
        # the one way a frozen field can be written.
        object.__setattr__(app, "remote", plane)
    return app


# ---- the bearer gate -----------------------------------------------------


def test_routes_answer_unauthorized_without_a_token(tmp_path):
    app = _app(tmp_path, _plane(tmp_path))
    with testclient.TestClient(create_desktop_app(app, bearer_token=KEY)) as client:
        assert client.get("/remote/hosts").status_code == 401
        assert (
            client.post(
                "/remote/hosts",
                json={"alias": "hpc1"},
            ).status_code
            == 401
        )
        assert client.delete("/remote/hosts/hpc1").status_code == 401


# ---- the off answer -------------------------------------------------------


def test_off_answers_remote_execution_disabled_on_every_route(tmp_path):
    app = _app(tmp_path)  # no plane bound: remote_execution off
    with testclient.TestClient(create_desktop_app(app)) as client:
        for answer in (
            client.get("/remote/hosts"),
            client.post("/remote/hosts", json={"alias": "hpc1"}),
            client.delete("/remote/hosts/hpc1"),
        ):
            assert answer.status_code == 503
            assert answer.json() == {"detail": "remote_execution_disabled"}
        # the capability the client gates on says the same thing
        health = client.get("/health").json()
        assert health["capabilities"]["remote_execution"] is False


def test_off_leaves_remote_jobs_refused_exactly_as_before(tmp_path):
    """C2's off contract, unchanged by the binding: a remote runtime job
    on a deployment without the plane is still the 409 it was."""
    app = _app(tmp_path)
    with testclient.TestClient(create_desktop_app(app)) as client:
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
        malformed = client.post(
            "/jobs",
            json={
                "kind": "skill_run",
                "skill": "",
                "inputs": {},
                "runtime": "remote:not a host",
            },
        )
        assert malformed.status_code == 422
        assert malformed.json()["detail"] == "invalid_runtime"


# ---- the list ------------------------------------------------------------


def test_an_empty_knowledge_base_lists_no_hosts(tmp_path):
    app = _app(tmp_path, _plane(tmp_path))
    with testclient.TestClient(create_desktop_app(app)) as client:
        answer = client.get("/remote/hosts")
        assert answer.status_code == 200
        assert answer.json() == []


def test_a_note_without_a_probe_still_lists(tmp_path):
    """``ask_about_host`` upserts rows whose probe never ran; the list
    answers them with ``probed`` false rather than hiding them."""
    plane = _plane(tmp_path)
    plane.note_answer("hpc1", "which partition?", "gpu-a800")
    app = _app(tmp_path, plane)
    with testclient.TestClient(create_desktop_app(app)) as client:
        rows = client.get("/remote/hosts").json()
        assert [row["alias"] for row in rows] == ["hpc1"]
        assert rows[0]["probed"] is False
        assert rows[0]["notes"][0]["answer"] == "gpu-a800"


# ---- probing -------------------------------------------------------------


def test_post_probes_stores_and_answers_the_card(tmp_path):
    spawner = FakeSpawner()
    spawner.on("base64 -d", PROBE_OK)
    app = _app(tmp_path, _plane(tmp_path, spawner))
    with testclient.TestClient(create_desktop_app(app)) as client:
        created = client.post("/remote/hosts", json={"alias": "hpc1"})
        assert created.status_code == 201
        card = created.json()
        assert card["alias"] == "hpc1"
        assert card["probed"] is True
        assert card["hostname"] == "fake-n01"
        assert card["nproc"] == 16
        assert card["memory_mb"] == 65536
        assert card["commands"]["conda"] == "/opt/conda/bin/conda"
        assert card["slurm_partitions"] == []
        assert card["scratch_root"] == "/scratch/fake"
        assert card["parse_error"] == ""
        assert card["last_probed_at"] > 0
        listed = client.get("/remote/hosts").json()
        assert [row["alias"] for row in listed] == ["hpc1"]
        # the probe went to the alias the body named, through the plane
        argvs = [argv for argv, _ in spawner.calls if argv and argv[0] == "ssh"]
        assert argvs and all(any("hpc1" == part for part in argv) for argv in argvs)


def test_post_overwrites_an_old_probe_on_reprobe(tmp_path):
    spawner = FakeSpawner()
    spawner.on("base64 -d", PROBE_OK)
    app = _app(tmp_path, _plane(tmp_path, spawner))
    with testclient.TestClient(create_desktop_app(app)) as client:
        first = client.post("/remote/hosts", json={"alias": "hpc1"}).json()
        second = client.post("/remote/hosts", json={"alias": "hpc1"}).json()
        assert second["last_probed_at"] >= first["last_probed_at"]
        rows = client.get("/remote/hosts").json()
        assert len(rows) == 1  # an upsert, not a second row


def test_post_unreachable_answers_a_sanitized_4xx_and_stores_nothing(tmp_path):
    spawner = FakeSpawner()
    home = str(pathlib.Path.home())
    spawner.on(
        "base64 -d",
        RemoteHostUnreachable(
            "ssh: connect to host 10.9.9.9 port 22: Operation timed out\n"
            f"banner noise from {home}/etc/motd\nkex_exchange_identification: "
            "read: Connection reset by peer\n"
        ),
    )
    app = _app(tmp_path, _plane(tmp_path, spawner))
    with testclient.TestClient(create_desktop_app(app)) as client:
        answer = client.post("/remote/hosts", json={"alias": "hpc1"})
        assert answer.status_code == 400
        body = answer.json()
        assert body["error"] == "unreachable"
        assert home not in body["detail"]
        assert "~" in body["detail"]
        assert "\n" not in body["detail"]
        assert client.get("/remote/hosts").json() == []


def test_post_refuses_alias_injection_at_the_door(tmp_path):
    app = _app(tmp_path, _plane(tmp_path))
    with testclient.TestClient(create_desktop_app(app)) as client:
        for alias in (
            "hpc1; rm -rf /",
            "-oProxyCommand=touch /tmp/pwned",
            "hpc1 hpc2",
            "",
            "not/a/host",
        ):
            answer = client.post("/remote/hosts", json={"alias": alias})
            assert answer.status_code == 422, alias
            assert answer.json() == {"detail": "invalid_host"}
        # a missing or non-string alias is the same refusal
        assert client.post("/remote/hosts", json={}).status_code == 422
        assert (
            client.post("/remote/hosts", json={"alias": 42}).status_code == 422
        )
        # a body that is not one application/json never reaches validation
        assert (
            client.post(
                "/remote/hosts",
                content=json.dumps({"alias": "hpc1"}),
                headers={"content-type": "text/plain"},
            ).status_code
            == 415
        )


# ---- deleting ------------------------------------------------------------


def test_delete_removes_the_host_and_then_404s(tmp_path):
    spawner = FakeSpawner()
    spawner.on("base64 -d", PROBE_OK)
    app = _app(tmp_path, _plane(tmp_path, spawner))
    with testclient.TestClient(create_desktop_app(app)) as client:
        assert client.post("/remote/hosts", json={"alias": "hpc1"}).status_code == 201
        assert client.delete("/remote/hosts/hpc1").status_code == 204
        assert client.get("/remote/hosts").json() == []
        assert client.delete("/remote/hosts/hpc1").status_code == 404
        assert client.delete("/remote/hosts/not a host").status_code == 422


def test_health_declares_the_capability_when_a_plane_is_bound(tmp_path):
    app = _app(tmp_path, _plane(tmp_path))
    with testclient.TestClient(create_desktop_app(app)) as client:
        payload = client.get("/health").json()
    assert payload["capabilities"]["remote_execution"] is True
    # additive only: the routes the wire contract promised are all there
    for path in ("/remote/hosts", "/remote/hosts/{alias}"):
        assert path in payload["served_paths"]
