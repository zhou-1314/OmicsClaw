import asyncio
from types import SimpleNamespace

from omicsclaw.entry.run_projection import project_session_run
from omicsclaw.observability import Telemetry
from omicsclaw.runs.store import RunStore
from tests.observability._support import RecordingTracer


def test_session_projection_links_a_durable_run_and_trace(tmp_path):
    tracer = RecordingTracer()
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path),
        plans=None,
        telemetry=Telemetry(tracer=tracer),
    )
    first = project_session_run(app, "chat-1")
    second = project_session_run(app, "chat-1")
    assert first == second == "legacy:chat-1"
    store = RunStore(tmp_path / ".omicsclaw" / "runs.db")
    assert store.load(first).session_id == "chat-1"
    assert len(store.events()) == 1

    async def scenario():
        async with app.telemetry.run(session_id="chat-1", run_id=first):
            pass

    asyncio.run(scenario())
    assert tracer.spans[-1].attributes["run.id"] == first


def test_anonymous_exchange_does_not_create_a_shared_legacy_run(tmp_path):
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path), telemetry=Telemetry()
    )
    assert project_session_run(app, "") == ""
    assert not (tmp_path / ".omicsclaw" / "runs.db").exists()


def test_unavailable_run_database_does_not_break_a_legacy_chat(tmp_path, caplog):
    path = tmp_path / ".omicsclaw" / "runs.db"
    path.parent.mkdir()
    path.write_bytes(b"unreadable database")
    app = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path), plans=None, telemetry=Telemetry()
    )
    assert project_session_run(app, "chat-1") == ""
    assert "session Run projection unavailable" in caplog.text
    assert path.read_bytes() == b"unreadable database"
