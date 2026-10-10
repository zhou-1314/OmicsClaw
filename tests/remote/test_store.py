"""The two remote tables and the jobs-plane runtime column.

C2's persistence contract: probes and notes land in ``remote_hosts``,
handles land in ``remote_jobs``, in-flight rows are never deleted on
doubt, and an old ``jobs`` table gains its ``runtime`` column on open.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from omicsclaw.memory.database import Database
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.store import RemoteHostStore, RemoteJobStore


def _db() -> Database:
    return Database(":memory:")


class TestSchema:
    def test_both_tables_exist_on_a_fresh_database(self):
        db = _db()
        names = {
            row[0]
            for row in db.run(
                lambda conn: conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
        }
        assert {"remote_hosts", "remote_jobs"} <= names

    def test_jobs_table_carries_runtime_with_local_default(self):
        db = _db()
        db.run(
            lambda conn: conn.execute(
                "INSERT INTO jobs (id, created_at) VALUES ('j1', 1.0)"
            )
        )
        rows = db.run(
            lambda conn: conn.execute(
                "SELECT runtime FROM jobs WHERE id = 'j1'"
            ).fetchall()
        )
        assert rows[0][0] == "local"

    def test_an_old_jobs_file_gains_the_runtime_column_on_open(self):
        with tempfile.TemporaryDirectory() as room:
            path = Path(room) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT,"
                " kind TEXT, skill TEXT, inputs_json TEXT, status TEXT,"
                " error TEXT, created_at REAL, started_at REAL, finished_at REAL);"
                "INSERT INTO jobs (id, created_at) VALUES ('old1', 9.0);"
            )
            conn.commit()
            conn.close()
            database = Database(path)  # the migration runs on open
            rows = database.run(
                lambda c: c.execute(
                    "SELECT runtime FROM jobs WHERE id = 'old1'"
                ).fetchall()
            )
            assert rows[0][0] == "local"
            # and again: idempotent on a second open
            database.close()
            second = Database(path)
            cols = {
                row[1]
                for row in second.run(
                    lambda c: c.execute("PRAGMA table_info(jobs)").fetchall()
                )
            }
            assert "runtime" in cols
            second.close()


class TestRemoteHostStore:
    def test_save_and_get_a_probe(self):
        db = _db()
        store = RemoteHostStore(db)
        store.save_probe("hpc1", '{"hostname": "n01"}')
        record = store.get("hpc1")
        assert record is not None
        assert record.probe_dict()["hostname"] == "n01"
        assert record.last_probed_at > 0

    def test_resave_refreshes_the_probe_but_keeps_notes(self):
        db = _db()
        store = RemoteHostStore(db)
        store.save_probe("hpc1", '{"nproc": 8}')
        store.add_note("hpc1", "which partition?", "gpu-long")
        store.save_probe("hpc1", '{"nproc": 32}')
        record = store.get("hpc1")
        assert record.probe_dict()["nproc"] == 32
        notes = record.notes()
        assert notes == [
            {"question": "which partition?", "answer": "gpu-long", "at": notes[0]["at"]}
        ]

    def test_a_note_before_any_probe_creates_the_row(self):
        db = _db()
        store = RemoteHostStore(db)
        store.add_note("fresh", "account?", "lab42")
        record = store.get("fresh")
        assert record is not None
        assert record.notes()[0]["answer"] == "lab42"

    def test_get_unknown_alias_is_none_and_list_is_sorted(self):
        db = _db()
        store = RemoteHostStore(db)
        assert store.get("nope") is None
        store.save_probe("b", "{}")
        store.save_probe("a", "{}")
        assert store.list_aliases() == ["a", "b"]


class TestRemoteJobStore:
    def _handle(self, **kwargs) -> RemoteJobHandle:
        base = dict(
            kind="nohup", host="hpc1", workdir="/scratch/w1", pgid=4242,
            submitted_at=1.0,
        )
        base.update(kwargs)
        return RemoteJobHandle(**base)

    def test_record_returns_the_job_ref_row_id(self):
        store = RemoteJobStore(_db())
        ref = store.record(self._handle(), status="pending")
        row = store.get(str(ref))
        assert row is not None
        assert row.host_alias == "hpc1"
        assert row.pgid == 4242
        assert row.status == "pending"
        assert row.handle().workdir == "/scratch/w1"

    def test_non_numeric_refs_are_not_found(self):
        store = RemoteJobStore(_db())
        assert store.get("remote://x") is None
        assert store.get("") is None

    def test_in_flight_lists_pending_and_running_only(self):
        store = RemoteJobStore(_db())
        first = store.record(self._handle(), status="running")
        second = store.record(self._handle(workdir="/w2"), status="pending")
        store.record(self._handle(workdir="/w3"), status="done")
        store.record(self._handle(workdir="/w4"), status="unknown")
        in_flight = {row.id for row in store.in_flight()}
        assert in_flight == {first, second}

    def test_unknown_is_not_in_flight_but_the_row_survives(self):
        # The outage rule: an unreachable host must not cost the handle.
        store = RemoteJobStore(_db())
        ref = store.record(self._handle(), status="running")
        store.update_status(ref, "unknown")
        assert store.get(str(ref)) is not None
        assert store.get(str(ref)).status == "unknown"
        assert store.in_flight() == []

    def test_by_local_job_finds_the_newest(self):
        store = RemoteJobStore(_db())
        store.record(self._handle(local_job_id="job-a"), status="done")
        latest = store.record(self._handle(workdir="/w2", local_job_id="job-a"))
        row = store.by_local_job("job-a")
        assert row.id == latest
        assert store.by_local_job("nope") is None
