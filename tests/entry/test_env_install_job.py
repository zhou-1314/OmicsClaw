"""P5's job face: ``kind="env_install"`` over the real FastAPI app.

The code_run tests' harness (real composition root, scripted backend,
injected jobs manager), pointed at the environment plane with a fake
supply instead of a kernel binding. What these pin:

* the lifecycle speaks the existing vocabulary — ``job.created``,
  ``job.started``, ``tool_started``, ``progress`` **with percent**,
  ``tool_output_chunk``, ``job.done`` — no new event types;
* the installer's step markers map onto progress frames (the honest
  phase-level percent contract);
* the route folds ``{kind, env, packages}`` the code_run way and
  refuses malformed bodies with the codes the wire spells;
* a deployment without the binding answers
  ``env_install_unavailable`` (409), not a silent local anything.
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest

pytest.importorskip("fastapi", reason="the Desktop HTTP adapter needs it")
testclient = pytest.importorskip("fastapi.testclient")

from omicsclaw.entry.desktop import create_desktop_app  # noqa: E402
from omicsclaw.entry.desktop.jobs_manager import (  # noqa: E402
    EnvInstallRunner,
    JobStore,
    JobsManager,
)
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from omicsclaw.memory.database import Database  # noqa: E402
from omicsclaw.schema import Message, Role  # noqa: E402
from tests.entry.test_turn_runner import (  # noqa: E402
    Scripted,  # type: ignore[import-not-found]
    make_app,
)


class FakeEnvSupply:
    """The EnvironmentSupply double for the job face: streams the same
    marker lines the real script echoes, records the ask."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def list_environments(self):
        return []

    async def probe_environment(self, *, env="main", interpreter=None):
        return {"interpreter": interpreter, "exists": True, "ready": True, "missing": []}

    async def install_environment(self, *, env="main", packages=(), on_output=None):
        self.calls.append({"env": env, "packages": tuple(packages)})
        if self.fail is not None:
            raise self.fail
        for line in (
            "Downloading Python 3.11.x",
            "===OMICSCLAW_ENV_STEP:python===",
            "===OMICSCLAW_ENV_STEP:venv===",
            "Installed 42 packages",
            "===OMICSCLAW_ENV_STEP:packages===",
            "===OMICSCLAW_ENV_STEP:verify===",
        ):
            if on_output is not None:
                on_output(line)
        return {
            "env": env,
            "interpreter": "~/.omicsclaw/envs/main/bin/python",
            "packages": ["ipykernel", "scanpy"],
        }


def env_client(tmp_path: pathlib.Path, supply=None):
    app = attach_sessions(
        make_app(tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok"))),
        abandon_grace_s=None,
    )
    manager = JobsManager(
        app,
        store=JobStore(Database(":memory:")),
        default_timeout_s=30.0,
        env_supply=supply,
    )
    return manager, testclient.TestClient(create_desktop_app(app, jobs_manager=manager))


def parse_sse(text: str) -> list[dict]:
    """Each frame's envelope with its ``data`` payload decoded.

    The chat SSE shape serializes the payload *inside* ``data`` as a
    JSON string, so both layers are json.loads'd here — the same double
    decode ``test_desktop_jobs.py`` performs."""
    frames = []
    for chunk in text.split("\n\n"):
        data = ""
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                data = line[6:]
        if data:
            envelope = json.loads(data)
            payload = envelope.get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    pass
            envelope["data"] = payload
            frames.append(envelope)
    return frames


def drain_sse(client, path: str) -> list[dict]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def wait_terminal(client, job_id: str, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    record = None
    while time.monotonic() < deadline:
        record = client.get("/jobs/" + job_id).json()["job"]
        if record["status"] in ("succeeded", "failed", "canceled", "interrupted"):
            return record
        time.sleep(0.1)
    raise AssertionError("job did not finish: " + repr(record))


def test_env_install_streams_the_vocabulary_with_progress_percents(tmp_path):
    supply = FakeEnvSupply()
    manager, http = env_client(tmp_path, supply=supply)
    with http as client:
        created = client.post(
            "/jobs", json={"kind": "env_install", "env": "main"}
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "succeeded", record
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        types = [frame["type"] for frame in frames]
        assert types[0] == "job.created"
        assert types[1] == "job.started"
        assert "tool_started" in types
        assert "progress" in types and "tool_output_chunk" in types
        assert types[-1] == "job.done"
        started = next(
            frame["data"] for frame in frames if frame["type"] == "tool_started"
        )
        assert started["tool"] == "manage_environments"
        assert started["human_description"]
        progress = [
            frame["data"] for frame in frames if frame["type"] == "progress"
        ]
        # markers map to phase percents, monotonic through verify
        percents = [p["percent"] for p in progress if "percent" in p]
        assert percents[-1] == 100.0
        assert 20.0 in percents and 75.0 in percents and 95.0 in percents
        assert supply.calls == [{"env": "main", "packages": ()}]


def test_env_install_carries_extra_packages_to_the_supply(tmp_path):
    supply = FakeEnvSupply()
    manager, http = env_client(tmp_path, supply=supply)
    with http as client:
        created = client.post(
            "/jobs",
            json={"kind": "env_install", "env": "spatial", "packages": ["squidpy"]},
        )
        assert created.status_code == 200, created.text
        assert wait_terminal(client, created.json()["job_id"])["status"] == "succeeded"
        assert supply.calls == [
            {"env": "spatial", "packages": ("squidpy",)}
        ]


def test_env_install_refuses_malformed_bodies_at_the_door(tmp_path):
    supply = FakeEnvSupply()
    manager, http = env_client(tmp_path, supply=supply)
    with http as client:
        bad_env = client.post("/jobs", json={"kind": "env_install", "env": 3})
        assert bad_env.status_code == 422
        assert bad_env.json()["detail"] == "invalid_env"
        bad_packages = client.post(
            "/jobs", json={"kind": "env_install", "packages": "squidpy"}
        )
        assert bad_packages.status_code == 422
        assert bad_packages.json()["detail"] == "invalid_packages"
        mixed = client.post(
            "/jobs", json={"kind": "env_install", "packages": ["ok", 7]}
        )
        assert mixed.status_code == 422
        assert mixed.json()["detail"] == "invalid_packages"


def test_env_install_refuses_names_the_runner_would_mistrust(tmp_path):
    supply = FakeEnvSupply()
    manager, http = env_client(tmp_path, supply=supply)
    with http as client:
        escape = client.post(
            "/jobs", json={"kind": "env_install", "env": "../elsewhere"}
        )
        assert escape.status_code == 422
        assert escape.json()["detail"] == "invalid_env"
        url = client.post(
            "/jobs", json={"kind": "env_install", "packages": ["https://x/p.whl"]}
        )
        assert url.status_code == 422
        assert url.json()["detail"] == "invalid_packages"


def test_env_install_without_a_binding_answers_unavailable(tmp_path):
    manager, http = env_client(tmp_path, supply=None)
    with http as client:
        refused = client.post("/jobs", json={"kind": "env_install"})
        assert refused.status_code == 409
        assert refused.json()["detail"] == "env_install_unavailable"


def test_a_failed_install_discloses_the_manual_commands(tmp_path):
    from omicsclaw.envsupply import EnvSupplyError, manual_env_commands

    supply = FakeEnvSupply(
        fail=EnvSupplyError(
            "installing the environment at ~/.omicsclaw/envs/main failed"
            " (exit 127): no uv executable found",
            manual_env_commands("~/.omicsclaw/envs/main"),
        )
    )
    manager, http = env_client(tmp_path, supply=supply)
    with http as client:
        created = client.post("/jobs", json={"kind": "env_install"})
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "failed"
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        last = frames[-1]
        assert last["type"] == "job.failed"
        error = last["data"]["error"]
        # the manual way out rides the failure frame, not a doc page
        assert "uv venv" in error
        assert "uv pip install" in error


def test_the_runner_validates_before_any_row_is_written():
    supply = FakeEnvSupply()
    runner = EnvInstallRunner(supply)
    with pytest.raises(Exception):
        runner.validate("", {"env": "a/b"})
    with pytest.raises(Exception):
        runner.validate("", {"packages": ["scanpy>=1.10"]})
    runner.validate("", {"env": "main", "packages": ["squidpy"]})
    assert supply.calls == []  # validation never reaches the supply


# ---- the composition root's switch --------------------------------------------


def test_the_switch_mounts_the_tool_and_the_binding(tmp_path):
    app = make_app(
        tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok")),
        env_supply=True,
    )
    assert app.registry.get("manage_environments") is not None
    assert app.env_supply is not None


def test_the_switch_off_changes_nothing(tmp_path):
    app = make_app(
        tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok")),
    )
    assert app.registry.get("manage_environments") is None
    assert app.env_supply is None
