"""The P1/P3 jobs plane over the real FastAPI app.

The app under test is the real composition root over a scripted backend
(the same ``make_app`` ``test_desktop_http.py`` uses), with two
differences the plane itself asked for: a fake skill is planted in the
workspace's ``skills/`` tree so ``skill_run`` has something pure-stdlib
to call, and the jobs manager is injected with an in-memory store so the
tests never touch a real ``.omicsclaw`` directory. Everything else —
routing, auth, SSE framing, the event ring, resume, cancellation and the
approval gate — runs through ``create_desktop_app`` and ``httpx``.

The ``TestClient`` is used as a context manager throughout: a job is an
asyncio task that outlives the request that started it, and only the
context-manager form keeps one portal loop alive for it to run on.
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
    JobStore,
    JobsManager,
    human_tool_description,
)
from omicsclaw.entry.desktop.wire_contract import CONNECTION_EPOCH  # noqa: E402
from omicsclaw.entry.session import attach_sessions  # noqa: E402
from omicsclaw.memory.database import Database  # noqa: E402
from omicsclaw.schema import Message, Role  # noqa: E402
from omicsclaw.tools.context import require_approval  # noqa: E402
from tests.entry.test_turn_runner import (  # noqa: E402
    Scripted,  # type: ignore[import-not-found]
    make_app,
)

KEY = "b" * 32

FAKE_SKILL_MD = """---
name: hello-skill
description: A no-dependency skill the jobs plane tests can run for real.
entry: greet
inputs_schema: |
  {
    "type": "object",
    "properties": {
      "name": {"type": "string"},
      "times": {"type": "integer", "minimum": 1, "maximum": 5}
    },
    "required": ["name"],
    "additionalProperties": false
  }
---

# hello-skill

Greets; the test asserts on its events, not its output.
"""

FAKE_API_PY = '''"""The fake skill entry surface."""


def greet(name, times=1):
    """Return a greeting; deliberately dependency-free."""
    return "hello " + (name * times)


def helper():
    """Second in __all__, so entry: greet has to win."""
    return "unused"
'''


def plant_fake_skill(tmp_path: pathlib.Path) -> None:
    directory = tmp_path / "skills" / "demo" / "hello-skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(FAKE_SKILL_MD, encoding="utf-8")
    (directory / "_api.py").write_text(FAKE_API_PY, encoding="utf-8")


def jobs_client(tmp_path: pathlib.Path, **manager_kwargs):
    plant_fake_skill(tmp_path)
    app = attach_sessions(
        make_app(tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok"))),
        abandon_grace_s=None,
    )
    manager = JobsManager(
        app,
        store=JobStore(Database(":memory:")),
        default_timeout_s=manager_kwargs.pop("default_timeout_s", 30.0),
        **manager_kwargs,
    )
    return manager, testclient.TestClient(create_desktop_app(app, jobs_manager=manager))


def chat_body(**overrides) -> dict:
    document = {
        "ingress_schema_version": 3,
        "content": "hi",
        "session_id": "jobs-s1",
        "source_request_id": KEY,
    }
    document.update(overrides)
    return document


def parse_sse(text: str) -> list[tuple[int | None, dict]]:
    """Each frame of an SSE body: its ``id:`` when present, and its object."""
    frames: list[tuple[int | None, dict]] = []
    for chunk in text.split("\n\n"):
        if not chunk.strip():
            continue
        event_id: int | None = None
        data_line = ""
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                data_line = line[6:]
        if data_line:
            envelope = json.loads(data_line)
            payload = envelope.get("data")
            if isinstance(payload, str):
                try:
                    envelope["data"] = json.loads(payload)
                except (TypeError, ValueError):
                    pass
            frames.append((event_id, envelope))
    return frames


def drain_sse(http, url: str, headers: dict | None = None) -> list[tuple[int | None, dict]]:
    with http.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200, response.text
        body = "".join(
            chunk.decode("utf-8") for chunk in response.iter_bytes()
        )
    return parse_sse(body)


def wait_for(predicate, timeout_s: float = 10.0):
    """Poll while the portal loop keeps the job task running."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- health and contract ----------------------------------------------------


def test_health_publishes_jobs_contract_and_connection_epoch(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        payload = client.get("/health").json()
    assert payload["contracts"]["desktop_jobs"]["jobs_schema_version"] == 1
    assert isinstance(payload["connection_epoch"], int)
    assert payload["connection_epoch"] == CONNECTION_EPOCH
    for path in (
        "/jobs",
        "/jobs/{job_id}",
        "/jobs/{job_id}/events",
        "/jobs/{job_id}/cancel",
        "/jobs/{job_id}/approval/{call_id}",
    ):
        assert path in payload["served_paths"]


def test_chat_stream_frames_carry_epoch(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        frames = drain_sse_post(client)
    assert frames, "the scripted exchange produced no frames"
    for _, envelope in frames:
        assert envelope["epoch"] == CONNECTION_EPOCH


def drain_sse_post(client) -> list[tuple[int | None, dict]]:
    with client.stream(
        "POST", "/chat/stream", json=chat_body()
    ) as response:
        assert response.status_code == 200, response.text
        body = "".join(
            chunk.decode("utf-8") for chunk in response.iter_bytes()
        )
    return parse_sse(body)


# ---- job lifecycle over HTTP -------------------------------------------------


def test_skill_run_lifecycle_and_event_vocabulary(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={
                "kind": "skill_run",
                "skill": "demo/hello-skill",
                "inputs": {"name": "world", "times": 2},
                "session_id": "jobs-s1",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["job_id"]

        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"]
            == "succeeded"
        ), client.get("/jobs/" + job_id).json()

        frames = drain_sse(client, "/jobs/" + job_id + "/events")
    types = [envelope["type"] for _, envelope in frames]
    assert types[0] == "job.created"
    assert "job.started" in types
    assert "tool_started" in types
    assert "tool_output_chunk" in types
    assert "progress" in types
    assert types[-1] == "job.done"
    assert frames[-1][1]["data"]["status"] == "succeeded"

    seqs = [seq for seq, _ in frames if seq is not None]
    assert seqs == sorted(seqs), "ids must be non-decreasing"
    assert all(
        isinstance(seq, int) and seq >= 1 for seq in seqs
    ), "every event frame carries an id"
    for _, envelope in frames:
        assert envelope["epoch"] == CONNECTION_EPOCH
    started = next(
        envelope["data"]
        for _, envelope in frames
        if envelope["type"] == "tool_started"
    )
    assert started["tool"].startswith("skill:demo/hello-skill")
    assert started["human_description"]
    output = next(
        envelope["data"]
        for _, envelope in frames
        if envelope["type"] == "tool_output_chunk"
    )
    assert "worldworld" in output["text"]


def test_skill_run_rejects_inputs_that_miss_the_schema(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        refused = client.post(
            "/jobs",
            json={"kind": "skill_run", "skill": "demo/hello-skill", "inputs": {}},
        )
        assert refused.status_code == 422
        assert "inputs_do_not_match_schema" in refused.json()["detail"]

        unknown = client.post(
            "/jobs",
            json={"kind": "skill_run", "skill": "demo/nope", "inputs": {}},
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == "skill_not_found"

        bad_kind = client.post(
            "/jobs", json={"kind": "explode", "skill": "demo/hello-skill"}
        )
        assert bad_kind.status_code == 422
        assert bad_kind.json()["detail"] == "unknown_kind"


def test_job_list_and_filtering(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        first = client.post(
            "/jobs",
            json={
                "kind": "skill_run",
                "skill": "demo/hello-skill",
                "inputs": {"name": "a"},
                "session_id": "sess-a",
            },
        ).json()["job_id"]
        client.post(
            "/jobs",
            json={
                "kind": "skill_run",
                "skill": "demo/hello-skill",
                "inputs": {"name": "b"},
                "session_id": "sess-b",
            },
        )
        assert wait_for(
            lambda: len(
                [j for j in client.get("/jobs").json()["jobs"] if j["status"] == "succeeded"]
            )
            == 2
        )
        mine = client.get("/jobs", params={"session_id": "sess-a"}).json()["jobs"]
        assert [job["job_id"] for job in mine] == [first]
        succeeded = client.get(
            "/jobs", params={"status": "succeeded"}
        ).json()["jobs"]
        assert len(succeeded) == 2


def test_last_event_id_resumes_from_the_ring(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        job_id = client.post(
            "/jobs",
            json={"kind": "skill_run", "skill": "demo/hello-skill", "inputs": {"name": "x"}},
        ).json()["job_id"]
        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"] == "succeeded"
        )
        complete = drain_sse(client, "/jobs/" + job_id + "/events")
        middle = complete[2][0]
        assert middle is not None

        resumed = drain_sse(
            client,
            "/jobs/" + job_id + "/events",
            headers={"Last-Event-ID": str(middle)},
        )
        original_from = [f for f in complete if f[0] is not None and f[0] > middle]
        assert [f[0] for f in resumed] == [f[0] for f in original_from]
        assert resumed[-1][1]["type"] == "job.done"


def test_cancel_stops_a_running_job(tmp_path):
    manager, http = jobs_client(tmp_path)

    async def slow_runner(record, ctx):
        ctx.progress("starting slow work", 0.0)
        await asyncio_sleep(30.0)

    manager.register_runner("slow_run", slow_runner)
    with http as client:
        job_id = client.post(
            "/jobs", json={"kind": "slow_run", "inputs": {}}
        ).json()["job_id"]
        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"] == "running"
        )
        cancelled = client.post("/jobs/" + job_id + "/cancel")
        assert cancelled.status_code == 200
        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"] == "canceled"
        ), client.get("/jobs/" + job_id).json()
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        assert frames[-1][1]["type"] == "job.done"
        assert frames[-1][1]["data"]["status"] == "canceled"
        again = client.post("/jobs/" + job_id + "/cancel")
        assert again.status_code == 409


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def test_approval_gate_settles_over_http(tmp_path):
    manager, http = jobs_client(tmp_path)

    async def gated_runner(record, ctx):
        decision = await require_approval(
            "demo_tool",
            json.dumps({"command": "run-analysis --n=12"}),
            reason="",
        )
        ctx.emit("tool_output_chunk", {"text": "approved=" + str(decision.approved)})
        return "after-approval"

    manager.register_runner("gated_run", gated_runner)
    with http as client:
        job_id = client.post(
            "/jobs", json={"kind": "gated_run", "inputs": {}}
        ).json()["job_id"]

        call_id = None

        def approval_event():
            nonlocal call_id
            for _, event_type, payload in manager.replay_events(job_id, 0):
                if event_type == "tool_approval_request":
                    call_id = payload["call_id"]
                    return True
            return False

        assert wait_for(approval_event), "no tool_approval_request was emitted"
        answered = client.post(
            "/jobs/" + job_id + "/approval/" + call_id,
            json={"decision": "approve"},
        )
        assert answered.status_code == 200, answered.text
        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"] == "succeeded"
        ), client.get("/jobs/" + job_id).json()

        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        types = [envelope["type"] for _, envelope in frames]
        assert "tool_approval_request" in types
        request_frame = next(
            envelope["data"]
            for _, envelope in frames
            if envelope["type"] == "tool_approval_request"
        )
        assert request_frame["call_id"] == call_id
        assert request_frame["risk"]
        assert "run-analysis" in request_frame["summary"]
        assert types[-1] == "job.done"

        stale = client.post(
            "/jobs/" + job_id + "/approval/" + call_id,
            json={"decision": "deny"},
        )
        assert stale.status_code == 404


def test_failed_job_reports_phase_and_error(tmp_path):
    manager, http = jobs_client(tmp_path)

    async def failing_runner(record, ctx):
        ctx.progress("about to fail", 10.0)
        raise RuntimeError("kaboom")

    manager.register_runner("failing_run", failing_runner)
    with http as client:
        job_id = client.post(
            "/jobs", json={"kind": "failing_run", "inputs": {}}
        ).json()["job_id"]
        assert wait_for(
            lambda: client.get("/jobs/" + job_id).json()["job"]["status"] == "failed"
        )
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        last = frames[-1][1]
        assert last["type"] == "job.failed"
        assert "kaboom" in last["data"]["error"]
        assert last["data"]["phase"] == "run"


def test_orphaned_jobs_are_marked_interrupted(tmp_path):
    plant_fake_skill(tmp_path)
    app = attach_sessions(
        make_app(tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok"))),
        abandon_grace_s=None,
    )
    store = JobStore(Database(":memory:"))
    from omicsclaw.entry.desktop.jobs_manager import JobRecord

    store.insert_job(JobRecord(id="orphan1", created_at=1.0, status="running"))
    store.insert_job(JobRecord(id="orphan2", created_at=2.0, status="succeeded"))
    restarted = JobsManager(app, store=store)
    interrupted = restarted.list_jobs(status="interrupted")
    assert [job.id for job in interrupted] == ["orphan1"]
    assert restarted.list_jobs(status="succeeded")[0].id == "orphan2"


def test_runtime_header_is_tolerated_and_echoed(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        response = client.get(
            "/jobs", headers={"X-OmicsClaw-Runtime": "remote:conn-7"}
        )
        assert response.status_code == 200
        assert response.headers.get("X-OmicsClaw-Runtime") == "remote:conn-7"
        health = client.get("/health", headers={"X-OmicsClaw-Runtime": "local"})
        assert health.status_code == 200


def test_skills_detail_exposes_inputs_schema(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        detail = client.get("/skills/demo/hello-skill")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["entry"] == "greet"
        schema = payload["inputs_schema"]
        assert schema["required"] == ["name"]
        assert schema["properties"]["times"]["maximum"] == 5


# ---- unit: the human description -------------------------------------------


def test_human_tool_description_summarises_arguments():
    assert "spatial domains" in human_tool_description("spatial_domains")
    described = human_tool_description(
        "spatial_domains", json.dumps({"method": "leiden", "n": 12})
    )
    assert "leiden" in described and "n=12" in described
    bash_like = human_tool_description(
        "bash", json.dumps({"command": "rm -rf /tmp/x\nsleep 5"})
    )
    assert bash_like.startswith("run bash rm -rf /tmp/x")


# ---- P4: kind="code_run" over the persistent kernel -------------------------


def code_run_client(tmp_path: pathlib.Path):
    """The jobs client with jupyter_client present, else skipped.

    Same construction as :func:`jobs_client` — the app is the real
    composition root's, so ``app.kernels`` is the binding the ``python``
    tool would use, and the code_run runner shares it.
    """
    importorskip = pytest.importorskip
    importorskip("jupyter_client", reason="code_run needs jupyter_client")
    importorskip("ipykernel", reason="code_run needs ipykernel")
    plant_fake_skill(tmp_path)
    app = attach_sessions(
        make_app(tmp_path, Scripted(Message(role=Role.ASSISTANT, content="ok"))),
        abandon_grace_s=None,
    )
    # A file-backed store, unlike jobs_client()'s in-memory one, because the
    # kernel binding registers figures on <workspace>/.omicsclaw/jobs.db —
    # the same file production shares between the jobs plane and the tool.
    (tmp_path / ".omicsclaw").mkdir(exist_ok=True)
    manager = JobsManager(
        app,
        store=JobStore(Database(tmp_path / ".omicsclaw" / "jobs.db")),
        default_timeout_s=120.0,
    )
    return manager, testclient.TestClient(create_desktop_app(app, jobs_manager=manager))


def wait_terminal(client, job_id: str, timeout_s: float = 90.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = client.get("/jobs/" + job_id).json()["job"]
        if record["status"] in ("succeeded", "failed", "canceled", "interrupted"):
            return record
        time.sleep(0.2)
    raise AssertionError("job did not finish: " + repr(record))


def test_code_run_job_streams_the_p1_vocabulary(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={
                "kind": "code_run",
                "code": "import sys\nprint('cell one', file=sys.stdout)",
                "session_id": "p4-j1",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "succeeded", record
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        types = [envelope["type"] for _, envelope in frames]
        assert types[0] == "job.created"
        assert types[1] == "job.started"
        assert "tool_started" in types
        assert "tool_output_chunk" in types
        assert types[-1] == "job.done"
        started = next(
            envelope["data"] for _, envelope in frames
            if envelope["type"] == "tool_started"
        )
        assert started["tool"] == "python"
        assert started["human_description"]
        usage = [
            envelope["data"] for _, envelope in frames
            if envelope["type"] == "usage"
        ]
        assert usage and usage[0]["wall_s"] >= 0


def test_code_run_persists_variables_across_jobs_on_one_session(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        first = client.post(
            "/jobs",
            json={"kind": "code_run", "code": "shared_v = 11", "session_id": "p4-j2"},
        )
        assert first.status_code == 200
        assert wait_terminal(client, first.json()["job_id"])["status"] == "succeeded"
        second = client.post(
            "/jobs",
            json={
                "kind": "code_run",
                "code": "print('sees', shared_v + 1)",
                "session_id": "p4-j2",
            },
        )
        job_id = second.json()["job_id"]
        assert wait_terminal(client, job_id)["status"] == "succeeded"
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        chunks = "".join(
            envelope["data"].get("text", "")
            for _, envelope in frames
            if envelope["type"] == "tool_output_chunk"
        )
        assert "sees 12" in chunks


def test_code_run_failure_is_a_job_failure_with_the_error(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={"kind": "code_run", "code": "1/0", "session_id": "p4-j3"},
        )
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "failed"
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        last = frames[-1][1]
        assert last["type"] == "job.failed"
        assert "ZeroDivisionError" in last["data"]["error"]
        assert last["data"]["phase"] == "run"


def test_code_run_requires_code(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        refused = client.post("/jobs", json={"kind": "code_run"})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "code_required"
        blank = client.post("/jobs", json={"kind": "code_run", "code": "   "})
        assert blank.status_code == 422
        assert blank.json()["detail"] == "code_required"


def test_code_run_figure_lands_in_the_artifact_tray(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={
                "kind": "code_run",
                "code": (
                    "import matplotlib.pyplot as plt\n"
                    "plt.plot([1, 2], [1, 2])\n"
                    "plt.show()\n"
                ),
                "session_id": "p4-j4",
            },
        )
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "succeeded", record
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        created_frames = [
            envelope["data"] for _, envelope in frames
            if envelope["type"] == "artifact.created"
        ]
        assert created_frames, "the figure must arrive as an artifact.created frame"
        figure = created_frames[0]
        assert figure["kind"] == "figure"
        assert pathlib.Path(figure["path"]).is_file()
        row = client.get("/artifacts/" + figure["artifact_id"]).json()["artifact"]
        assert row["job_id"] == job_id
        assert row["meta"]["capture"] == "kernel"


def test_health_declares_the_kernel_capability(tmp_path):
    _, http = jobs_client(tmp_path)
    with http as client:
        payload = client.get("/health").json()
    assert payload["capabilities"]["kernel"] is True


def test_code_run_interrupted_is_not_a_success(tmp_path):
    """P4 review m8: an interrupted cell ends the job ``interrupted`` —
    job.done with the status, not a success over a half-run cell."""
    manager, http = code_run_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={
                "kind": "code_run",
                "code": "raise KeyboardInterrupt",
                "session_id": "p4-j4",
            },
        )
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "interrupted"
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        last = frames[-1][1]
        assert last["type"] == "job.done"
        assert last["data"]["status"] == "interrupted"


def test_code_run_accepts_language_r(tmp_path):
    """P4 acceptance over HTTP: an R cell on the same jobs plane, same
    event vocabulary, same figure tray."""
    manager, http = code_run_client(tmp_path)
    with http as client:
        created = client.post(
            "/jobs",
            json={
                "kind": "code_run",
                "code": 'writeLines("hello from R"); plot(1:10)',
                "language": "r",
                "session_id": "p4-r1",
            },
        )
        job_id = created.json()["job_id"]
        record = wait_terminal(client, job_id)
        assert record["status"] == "succeeded", record
        frames = drain_sse(client, "/jobs/" + job_id + "/events")
        types = [envelope["type"] for _, envelope in frames]
        assert "tool_started" in types
        started = next(e for _, e in frames if e["type"] == "tool_started")
        assert started["data"]["tool"] == "r"
        assert any(t == "artifact.created" for t in types), types


def test_code_run_refuses_a_bad_language_and_r_with_adata(tmp_path):
    manager, http = code_run_client(tmp_path)
    with http as client:
        for inputs, detail in (
            ({"kind": "code_run", "code": "1", "language": "julia"}, "invalid_language"),
            (
                {
                    "kind": "code_run",
                    "code": "1",
                    "language": "r",
                    "inputs": {"adata_path": "a.h5ad"},
                },
                "adata_bridge_is_python_only",
            ),
        ):
            inputs["kind"] = "code_run"
            refused = client.post("/jobs", json=inputs)
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"] == detail
