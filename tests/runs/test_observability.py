import sqlite3

import pytest

from omicsclaw.observability import Telemetry
from omicsclaw.runs.models import Run
from omicsclaw.runs.store import RunStore
from tests.observability._support import RecordingTracer


def test_observer_reads_a_committed_event_from_another_connection(tmp_path):
    path = tmp_path / "runs.db"
    reader = RunStore(path)
    seen = []
    writer = RunStore(
        path, on_event=lambda event: seen.append(reader.load(event["object_id"]))
    )
    run = Run()
    writer.save(run)
    assert seen[0].run_id == run.run_id


def test_illegal_transition_neither_commits_nor_emits():
    seen = []
    store = RunStore(on_event=seen.append)
    run = Run()
    store.save(run)
    run.status = "succeeded"
    with pytest.raises(ValueError, match="illegal state transition"):
        store.save(run)
    assert len(seen) == 1
    assert store.load(run.run_id).status == "created"


def test_observer_failure_cannot_undo_a_committed_run(caplog):
    def broken(event):
        raise RuntimeError("observer disconnected")

    store = RunStore(on_event=broken)
    run = Run()
    store.save(run)
    assert store.load(run.run_id).run_id == run.run_id
    assert "observer" in caplog.text


def test_read_and_replay_do_not_emit_telemetry_twice():
    tracer = RecordingTracer()
    store = RunStore(on_event=Telemetry(tracer=tracer).run_event_sink())
    run = Run()
    store.save(run)
    store.replay(run.run_id)
    store.events(run_id=run.run_id)
    store.load(run.run_id)
    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes["event.sequence"] == 1


def test_rollback_does_not_publish_an_event():
    seen = []
    store = RunStore(on_event=seen.append)
    store.db.execute(
        "CREATE TRIGGER reject_run BEFORE INSERT ON run_events BEGIN SELECT RAISE(ABORT, 'storage full'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="storage full"):
        store.save(Run())
    assert seen == []
    assert store.list() == []
