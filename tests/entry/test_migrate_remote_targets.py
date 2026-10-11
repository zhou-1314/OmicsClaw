from __future__ import annotations

import json


from omicsclaw.entry.desktop.jobs_manager import JobStore
from omicsclaw.entry.desktop import migrate_remote_targets
from omicsclaw.entry.desktop.migrate_remote_targets import migrate_database, migrate_workspace
from omicsclaw.memory.database import Database


def _legacy_jobs(db: Database) -> None:
    db.run(
        lambda conn: conn.executemany(
            "INSERT INTO jobs (id, runtime, inputs_json, created_at) VALUES (?, ?, ?, ?)",
            [
                (
                    "legacy",
                    "remote:hpc1",
                    json.dumps({"command": "true", "target_workdir": "/scratch/omicsclaw/legacy"}),
                    1.0,
                ),
                ("broken", "remote:bad host", "{}", 2.0),
            ],
        )
    )


def test_dry_run_does_not_backfill_or_write_state(tmp_path):
    db = Database(tmp_path / "jobs.db")
    _legacy_jobs(db)
    before = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs ORDER BY id"
        ).fetchall()
    )
    report = migrate_database(db, scope="jobs", dry_run=True)
    after = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs ORDER BY id"
        ).fetchall()
    )
    assert report.mode == "dry-run"
    assert report.migrated == 1
    assert report.unresolved[0]["record_key"] == "broken"
    assert before == after
    assert db.run(
        lambda conn: conn.execute("SELECT COUNT(*) FROM target_migration_state").fetchone()[0]
    ) == 0


def test_apply_is_idempotent_and_legacy_snapshot_survives_reload(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(db_path)
    _legacy_jobs(db)
    first = migrate_database(db, scope="jobs", dry_run=False)
    second = migrate_database(db, scope="jobs", dry_run=False)
    assert first.migrated == 1
    assert second.migrated == 0
    row = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs WHERE id='legacy'"
        ).fetchone()
    )
    assert tuple(row) == (
        "hpc1",
        "/scratch/omicsclaw/legacy",
        json.dumps(["remote_exec", "remote_submit"]),
    )
    state = db.run(
        lambda conn: conn.execute(
            "SELECT status, unresolved_count FROM target_migration_state WHERE scope='jobs'"
        ).fetchone()
    )
    assert tuple(state) == ("unresolved", 1)
    db.close()

    restarted = Database(db_path)
    record = JobStore(restarted).get_job("legacy")
    assert record is not None
    assert record.target_alias == "hpc1"
    assert record.capabilities == ("remote_exec", "remote_submit")
    restarted.close()


def test_remote_job_without_a_frozen_workdir_is_unresolved(tmp_path):
    db = Database(tmp_path / "jobs.db")
    db.run(
        lambda conn: conn.execute(
            "INSERT INTO jobs (id, runtime, inputs_json, created_at) VALUES (?, ?, ?, ?)",
            ("legacy", "remote:hpc1", json.dumps({"command": "true"}), 1.0),
        )
    )
    report = migrate_database(db, scope="jobs", dry_run=False)
    assert report.migrated == 0
    assert report.unresolved == (
        {"record_key": "legacy", "reason": "missing_target_workdir", "detail": "hpc1"},
    )
    row = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs WHERE id='legacy'"
        ).fetchone()
    )
    assert tuple(row) == ("", "", "[]")


def test_legacy_code_run_remote_location_gets_kernel_snapshot(tmp_path):
    db = Database(tmp_path / "jobs.db")
    db.run(
        lambda conn: conn.execute(
            "INSERT INTO jobs (id, kind, runtime, inputs_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "kernel-legacy",
                "code_run",
                "local",
                json.dumps(
                    {
                        "code": "1",
                        "location": "remote:gpu1",
                        "target_workdir": "/scratch/omicsclaw/kernel",
                    }
                ),
                1.0,
            ),
        )
    )
    report = migrate_database(db, scope="jobs", dry_run=False)
    assert report.migrated == 1
    row = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs WHERE id='kernel-legacy'"
        ).fetchone()
    )
    assert tuple(row) == (
        "gpu1",
        "/scratch/omicsclaw/kernel",
        json.dumps(["remote_kernel"]),
    )


def test_local_non_code_job_with_remote_location_is_not_reclassified(tmp_path):
    db = Database(tmp_path / "jobs.db")
    db.run(
        lambda conn: conn.execute(
            "INSERT INTO jobs (id, kind, runtime, inputs_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "skill-legacy",
                "skill_run",
                "local",
                json.dumps(
                    {
                        "location": "remote:gpu1",
                        "target_workdir": "/scratch/omicsclaw/skill",
                    }
                ),
                1.0,
            ),
        )
    )
    report = migrate_database(db, scope="jobs", dry_run=False)
    assert report.migrated == 0
    assert report.unresolved == ()
    row = db.run(
        lambda conn: conn.execute(
            "SELECT target_alias, target_workdir, capabilities_json FROM jobs WHERE id='skill-legacy'"
        ).fetchone()
    )
    assert tuple(row) == ("", "", "[]")


def test_workspace_dry_run_does_not_create_missing_database_files(tmp_path):
    reports = migrate_workspace(tmp_path, dry_run=True)
    assert [report.status for report in reports] == ["complete", "complete"]
    assert not (tmp_path / ".omicsclaw").exists()


def test_workspace_apply_backs_up_existing_database(tmp_path):
    state = tmp_path / ".omicsclaw"
    state.mkdir()
    path = state / "jobs.db"
    db = Database(path)
    _legacy_jobs(db)
    db.close()
    reports = migrate_workspace(tmp_path, dry_run=False)
    assert reports[0].backup_path
    assert (tmp_path / ".omicsclaw" / "jobs.db").exists()
    assert list((tmp_path / ".omicsclaw").glob("jobs.db.target-migration.*.bak"))


def test_workspace_backup_failure_is_structured_and_retryable(tmp_path, monkeypatch):
    state = tmp_path / ".omicsclaw"
    state.mkdir()
    path = state / "jobs.db"
    db = Database(path)
    _legacy_jobs(db)
    db.close()

    def fail_backup(_path):
        raise OSError("read-only backup destination")

    monkeypatch.setattr(migrate_remote_targets, "_backup_database", fail_backup)
    reports = migrate_workspace(tmp_path, dry_run=False)
    assert reports[0].status == "failed"
    assert reports[0].error_code.startswith("backup_copy_failed:")
    assert reports[0].backup_path == ""
    failed = list(state.glob("target-migration.jobs.*.failed.json"))
    assert len(failed) == 1
    payload = json.loads(failed[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_code"] == reports[0].error_code
