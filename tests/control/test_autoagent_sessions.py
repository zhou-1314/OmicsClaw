from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from omicsclaw.control import (
    AutoAgentActiveCapacityError,
    AutoAgentCapacityError,
    ControlStateRepository,
)
from omicsclaw.control.schema import MIGRATIONS


def _accept(
    repository: ControlStateRepository,
    **kwargs,
):
    session_id = kwargs["session_id"]
    kwargs.setdefault(
        "execution_reference_type",
        "linux-user-systemd-bwrap-v1",
    )
    kwargs.setdefault(
        "execution_reference",
        f"omicsclaw-run-{session_id[:24]}.scope",
    )
    return repository.accept_autoagent_session(**kwargs)


def test_autoagent_migration_is_append_only_and_closes_authority_mutation(
    tmp_path: Path,
) -> None:
    historical = (
        "f87de47352b32e31892e9a6494d040108739fde262d386d3d3e78225d51fb48e",
        "a452d799ca308923e5a71f7396f754f0e1a83e68a6fbd948a55a69a4f7738478",
        "a89c6a289e7ec762f958ab2b63c5eb26f08b6c749e1253e9003c70d6f5cf9769",
        "da99461bb52c019ba8bfea210ffac819ce44cfc7eb2055208980d70c7cfb63db",
        "75d8a9138b56d886f954758992d7cc7fed06c0b5f272e1c1d5f5be8117ae64e2",
        "842d331e3afce0046f7b09eb9ce6fa7397d52ea22bf68025a120fe74e978f459",
        "392c2e4e3f9a6c25b86c8dd69b42fb676df4a0d7e7fe7c5c98bec83ac2208ec4",
        "5464aba854a4de4b2488f29af4542ee9cc0b08d78a72a18e6a8aebaf63d6ed6b",
        "26cf5d5e5b5aa4672f7fba558f87d9f06010a433b3e99ef2e0228f4a2b6bd85f",
        "5c49d8b0d86a60dd728885b2234bd01096088398c26afc96a8c3146678be65ed",
    )

    with ControlStateRepository(tmp_path / "control") as repository:
        assert tuple(migration.version for migration in MIGRATIONS) == tuple(
            range(1, 13)
        )
        assert tuple(migration.checksum for migration in MIGRATIONS[:10]) == historical

        with sqlite3.connect(repository.database_path) as connection:
            table = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'autoagent_sessions'"
            ).fetchone()
            assert table is not None
            assert str(table[0]).rstrip().endswith("STRICT")

            connection.execute(
                """
                INSERT INTO autoagent_sessions (
                    session_id, cwd, output_dir, skill, method, evolution_goal,
                    creation_receipt_sha256, status, result_json,
                    result_sha256, error_code, error_detail,
                    execution_reference_type, execution_reference,
                    created_at_ms, updated_at_ms, finished_at_ms, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, NULL,
                          'linux-user-systemd-bwrap-v1', ?, 1, 1, NULL, 1)
                """,
                (
                    "9" * 32,
                    str(tmp_path),
                    str(tmp_path / "output" / "session-01"),
                    "sc-batch-integration",
                    "harmony",
                    "improve",
                    "a" * 64,
                    f"omicsclaw-run-{'9' * 24}.scope",
                ),
            )
            connection.commit()

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE autoagent_sessions SET cwd = ? WHERE session_id = ?",
                    (str(tmp_path / "other"), "9" * 32),
                )
            connection.rollback()

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE autoagent_sessions SET revision = 3 "
                    "WHERE session_id = ?",
                    ("9" * 32,),
                )
            connection.rollback()

            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM autoagent_sessions WHERE session_id = ?",
                    ("9" * 32,),
                )
            connection.rollback()


def test_autoagent_repository_accepts_once_and_fences_receipt_and_terminal_result(
    tmp_path: Path,
) -> None:
    receipt_digest = "b" * 64
    result = {
        "success": True,
        "mode": "harness_evolution",
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "improve",
        "output_dir": str(
            tmp_path / ".omicsclaw" / "autoagent-internal" / ("b" * 32)
        ),
        "promotion": {"status": "skipped"},
        "best_score": 0.9,
    }

    with ControlStateRepository(tmp_path / "control") as repository:
        accepted = _accept(
            repository,
            session_id="b" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="improve",
            creation_receipt_sha256=receipt_digest,
        )
        assert accepted.status == "running"
        assert accepted.result is None
        assert accepted.creation_receipt_sha256 == receipt_digest

        with pytest.raises(KeyError):
            _accept(
                repository,
                session_id="b" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="improve",
                creation_receipt_sha256=receipt_digest,
            )

        assert (
            repository.verify_autoagent_creation_receipt(
                "b" * 32, receipt_digest
            ).session_id
            == "b" * 32
        )
        with pytest.raises(ValueError, match="receipt"):
            repository.verify_autoagent_creation_receipt("b" * 32, "c" * 64)

        repository.confirm_autoagent_owner_stopped("b" * 32)
        completed = repository.complete_autoagent_session_success(
            "b" * 32, result
        )
        assert completed.status == "done"
        assert dict(completed.result or {}) == result
        assert completed.result_sha256 is not None
        assert completed.finished_at_ms is not None
        assert completed.revision == 3

        with pytest.raises(ValueError, match="terminal"):
            repository.complete_autoagent_session_error(
                "b" * 32,
                status="error",
                error_code="harness_failed",
                error_detail="Harness evolution failed",
            )


def test_autoagent_restart_preserves_terminal_result_and_interrupts_orphans(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "control"
    with ControlStateRepository(state_root) as repository:
        _accept(
            repository,
            session_id="c" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="finished",
            creation_receipt_sha256=None,
        )
        repository.confirm_autoagent_owner_stopped("c" * 32)
        repository.complete_autoagent_session_success(
            "c" * 32,
            {
                "success": True,
                "mode": "harness_evolution",
                "skill": "sc-batch-integration",
                "method": "harmony",
                "evolution_goal": "finished",
                "output_dir": str(
                    tmp_path / ".omicsclaw" / "autoagent-internal" / ("c" * 32)
                ),
                "promotion": {"status": "skipped"},
                "best_score": 0.8,
            },
        )
        _accept(
            repository,
            session_id="d" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="orphaned",
            creation_receipt_sha256=None,
        )

    with ControlStateRepository(state_root) as restarted:
        reconciliation = restarted.reconcile_autoagent_sessions()
        assert reconciliation.interrupted_session_ids == ()
        assert reconciliation.unconfirmed_session_ids == ("d" * 32,)

        finished = restarted.get_autoagent_session("c" * 32)
        assert finished.status == "done"

        orphaned = restarted.get_autoagent_session("d" * 32)
        assert orphaned.status == "running"
        assert orphaned.error_code is None
        assert orphaned.result is None


def test_autoagent_terminal_repository_fault_rolls_back_without_exposing_result(
    tmp_path: Path,
) -> None:
    armed = False

    def inject(checkpoint: str) -> None:
        if armed and checkpoint == "complete_autoagent_session_success.before_commit":
            raise OSError("injected commit failure")

    with ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    ) as repository:
        _accept(
            repository,
            session_id="e" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="fault",
            creation_receipt_sha256=None,
        )
        repository.confirm_autoagent_owner_stopped("e" * 32)
        armed = True
        with pytest.raises(OSError, match="injected commit failure"):
            repository.complete_autoagent_session_success(
                "e" * 32,
                {
                    "success": True,
                    "mode": "harness_evolution",
                    "skill": "sc-batch-integration",
                    "method": "harmony",
                    "evolution_goal": "fault",
                    "output_dir": str(
                        tmp_path
                        / ".omicsclaw"
                        / "autoagent-internal"
                        / ("e" * 32)
                    ),
                    "promotion": {"status": "skipped"},
                    "best_score": 1.0,
                },
            )
        armed = False

        retained = repository.get_autoagent_session("e" * 32)
        assert retained.status == "running"
        assert retained.result is None
        assert retained.revision == 2


def test_autoagent_repository_rejects_secret_shaped_results_and_raw_error_detail(
    tmp_path: Path,
) -> None:
    with ControlStateRepository(tmp_path / "control") as repository:
        _accept(
            repository,
            session_id="f" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256=None,
        )
        with pytest.raises(ValueError, match="unsupported fields"):
            repository.complete_autoagent_session_success(
                "f" * 32,
                {
                    "success": True,
                    "provider_config": {"api_key": "must-not-persist"},
                },
            )
        canaries = {
            "access_token": "nested-access-token-must-not-persist",
            "private_key": "nested-private-key-must-not-persist",
            "refreshToken": "nested-refresh-token-must-not-persist",
            "authToken": "nested-auth-token-must-not-persist",
            "clientSecret": "nested-client-secret-must-not-persist",
            "bearer": "nested-bearer-must-not-persist",
            "apiKey": "nested-api-key-must-not-persist",
            "accessKey": "nested-access-key-must-not-persist",
        }
        for key, canary in canaries.items():
            with pytest.raises(ValueError, match="credentials"):
                repository.complete_autoagent_session_success(
                    "f" * 32,
                    {
                        "success": True,
                        "best_params": {key: canary},
                    },
                )
        with pytest.raises(ValueError, match="closed code"):
            repository.complete_autoagent_session_error(
                "f" * 32,
                status="error",
                error_code="harness_failed",
                error_detail="provider token must-not-persist",
            )

        repository.confirm_autoagent_owner_stopped("f" * 32)
        closed = repository.complete_autoagent_session_error(
            "f" * 32,
            status="error",
            error_code="harness_failed",
            error_detail="Harness evolution failed",
        )
        assert closed.error_detail == "Harness evolution failed"
        database_bytes = repository.database_path.read_bytes()
        assert b"must-not-persist" not in database_bytes
        for canary in canaries.values():
            assert canary.encode() not in database_bytes


def test_autoagent_acceptance_fault_leaves_no_partial_authority(tmp_path: Path) -> None:
    armed = True

    def inject(checkpoint: str) -> None:
        if armed and checkpoint == "accept_autoagent_session.before_commit":
            raise OSError("injected acceptance failure")

    with ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    ) as repository:
        with pytest.raises(OSError, match="acceptance failure"):
            _accept(
                repository,
                session_id="0" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256=None,
            )
        with pytest.raises(KeyError):
            repository.get_autoagent_session("0" * 32)

        armed = False
        accepted = _accept(
            repository,
            session_id="0" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256=None,
        )
        assert accepted.revision == 1


def test_autoagent_repository_freezes_only_canonical_existing_cwd(
    tmp_path: Path,
) -> None:
    real_cwd = tmp_path / "real"
    real_cwd.mkdir()
    alias_cwd = tmp_path / "alias"
    alias_cwd.symlink_to(real_cwd, target_is_directory=True)
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("not a cwd", encoding="utf-8")

    with ControlStateRepository(tmp_path / "control") as repository:
        for index, invalid_cwd in enumerate(
            ("relative", str(alias_cwd), str(regular_file))
        ):
            with pytest.raises(ValueError, match="cwd"):
                _accept(
                    repository,
                    session_id=f"{index + 20:032x}",
                    cwd=invalid_cwd,
                    skill="sc-batch-integration",
                    method="harmony",
                    evolution_goal="",
                    creation_receipt_sha256=None,
                )
        assert repository.list_running_autoagent_sessions() == ()


def test_autoagent_persistent_capacity_fails_closed_for_repository_and_raw_sql(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "session-capacity"
    with ControlStateRepository(session_root) as repository:
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity "
                "SET session_count = 100000 WHERE singleton_id = 1"
            )
            connection.commit()

            with pytest.raises(sqlite3.IntegrityError, match="session capacity"):
                connection.execute(
                    """
                    INSERT INTO autoagent_sessions (
                        session_id, cwd, output_dir, skill, method, evolution_goal,
                        creation_receipt_sha256, status, result_json,
                        result_sha256, error_code, error_detail,
                        execution_reference_type, execution_reference,
                        created_at_ms, updated_at_ms, finished_at_ms, revision
                    ) VALUES (?, ?, ?, 'skill', 'method', '', NULL, 'running',
                              NULL, NULL, NULL, NULL,
                              'linux-user-systemd-bwrap-v1', ?,
                              1, 1, NULL, 1)
                    """,
                    (
                        "a" * 32,
                        str(tmp_path),
                        str(tmp_path / "raw-over-capacity"),
                        f"omicsclaw-run-{'a' * 24}.scope",
                    ),
                )

            with pytest.raises(sqlite3.IntegrityError, match="monotonic"):
                connection.execute(
                    "UPDATE autoagent_capacity "
                    "SET session_count = 99999 WHERE singleton_id = 1"
                )

        with pytest.raises(AutoAgentCapacityError, match="session capacity"):
            _accept(
                repository,
                session_id="1" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256=None,
            )

    with ControlStateRepository(session_root) as restarted:
        with pytest.raises(AutoAgentCapacityError, match="session capacity"):
            _accept(
                restarted,
                session_id="2" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256=None,
            )

    result_root = tmp_path / "result-capacity"
    with ControlStateRepository(result_root) as repository:
        _accept(
            repository,
            session_id="3" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256=None,
        )
        repository.confirm_autoagent_owner_stopped("3" * 32)
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity "
                "SET result_bytes = 1073741824 WHERE singleton_id = 1"
            )
            connection.commit()
            updated_at_ms = connection.execute(
                "SELECT updated_at_ms FROM autoagent_sessions "
                "WHERE session_id = ?",
                ("3" * 32,),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="result capacity"):
                connection.execute(
                    """
                    UPDATE autoagent_sessions
                    SET status = 'done', result_json = '{"success":true}',
                        result_sha256 = ?, updated_at_ms = ?,
                        finished_at_ms = ?, revision = 3
                    WHERE session_id = ?
                    """,
                    (
                        "a" * 64,
                        updated_at_ms + 1,
                        updated_at_ms + 1,
                        "3" * 32,
                    ),
                )

        with pytest.raises(AutoAgentCapacityError, match="result capacity"):
            repository.complete_autoagent_session_success(
                "3" * 32,
                {
                    "success": True,
                    "mode": "harness_evolution",
                    "skill": "sc-batch-integration",
                    "method": "harmony",
                    "evolution_goal": "",
                    "output_dir": str(
                        tmp_path
                        / ".omicsclaw"
                        / "autoagent-internal"
                        / ("3" * 32)
                    ),
                    "promotion": {"status": "skipped"},
                },
            )


def test_autoagent_capacity_serializes_competing_raw_admission(tmp_path: Path) -> None:
    state_root = tmp_path / "control"
    with ControlStateRepository(state_root) as repository:
        database_path = repository.database_path
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity "
                "SET session_count = 99999 WHERE singleton_id = 1"
            )
            connection.commit()

    barrier = __import__("threading").Barrier(2)
    outcomes: list[str] = []
    outcome_lock = __import__("threading").Lock()

    def admit(session_id: str) -> None:
        connection = sqlite3.connect(database_path, timeout=5)
        try:
            barrier.wait(2)
            connection.execute(
                """
                INSERT INTO autoagent_sessions (
                    session_id, cwd, output_dir, skill, method, evolution_goal,
                    creation_receipt_sha256, status, result_json,
                    result_sha256, error_code, error_detail,
                    execution_reference_type, execution_reference,
                    created_at_ms, updated_at_ms, finished_at_ms, revision
                ) VALUES (?, '', ?, 'skill', 'method', '', NULL, 'running',
                          NULL, NULL, NULL, NULL,
                          'linux-user-systemd-bwrap-v1', ?, 1, 1, NULL, 1)
                """,
                (
                    session_id,
                    str(tmp_path / "output" / session_id),
                    f"omicsclaw-run-{session_id[:24]}.scope",
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            outcome = "capacity"
        else:
            outcome = "accepted"
        finally:
            connection.close()
        with outcome_lock:
            outcomes.append(outcome)

    import threading

    workers = [
        threading.Thread(target=admit, args=(f"{index + 1:032x}",))
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()

    assert sorted(outcomes) == ["accepted", "capacity"]
    with ControlStateRepository(state_root) as restarted:
        with sqlite3.connect(restarted.database_path) as connection:
            budget = connection.execute(
                "SELECT session_count FROM autoagent_capacity WHERE singleton_id = 1"
            ).fetchone()
        assert budget == (100000,)


def test_autoagent_receipt_cancellation_is_durable_and_exact(tmp_path: Path) -> None:
    state_root = tmp_path / "control"
    cancelled_id = "4" * 32
    wrong_receipt_id = "5" * 32
    running_id = "6" * 32
    with ControlStateRepository(state_root) as repository:
        tombstone = repository.request_autoagent_cancellation(
            session_id=cancelled_id,
            creation_receipt_sha256="a" * 64,
        )
        assert tombstone.status == "cancelled"
        assert tombstone.session is None

        cancelled = repository.accept_autoagent_session(
            session_id=cancelled_id,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256="a" * 64,
        )
        assert cancelled.status == "cancelled"
        assert cancelled.error_code == "cancelled"
        assert cancelled.cancel_requested_at_ms is not None
        assert cancelled.revision == 1

        repository.request_autoagent_cancellation(
            session_id=wrong_receipt_id,
            creation_receipt_sha256="b" * 64,
        )
        not_cancelled = _accept(
            repository,
            session_id=wrong_receipt_id,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256="c" * 64,
        )
        assert not_cancelled.status == "running"
        assert not_cancelled.cancel_requested_at_ms is None

        running = _accept(
            repository,
            session_id=running_id,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256="d" * 64,
        )
        with pytest.raises(ValueError, match="receipt"):
            repository.request_autoagent_cancellation(
                session_id=running.session_id,
                creation_receipt_sha256="e" * 64,
            )
        unchanged = repository.get_autoagent_session(running.session_id)
        assert unchanged.cancel_requested_at_ms is None
        assert unchanged.revision == 1

        requested = repository.request_autoagent_cancellation(
            session_id=running.session_id,
            creation_receipt_sha256="d" * 64,
        )
        assert requested.status == "cancel_requested"
        assert requested.session is not None
        assert requested.session.status == "running"
        assert requested.session.cancel_requested_at_ms is not None
        assert requested.session.revision == 2

    with ControlStateRepository(state_root) as restarted:
        inherited = restarted.get_autoagent_session(running_id)
        assert inherited.cancel_requested_at_ms is not None
        reconciliation = restarted.reconcile_autoagent_sessions()
        assert reconciliation.interrupted_session_ids == ()
        assert set(reconciliation.unconfirmed_session_ids) == {
            running_id,
            wrong_receipt_id,
        }
        assert restarted.get_autoagent_session(running_id).revision == 2


def test_autoagent_preaccept_cancellation_capacity_is_bounded(tmp_path: Path) -> None:
    with ControlStateRepository(tmp_path / "control") as repository:
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity "
                "SET cancellation_count = 100000 WHERE singleton_id = 1"
            )
            connection.commit()
            with pytest.raises(sqlite3.IntegrityError, match="cancellation capacity"):
                connection.execute(
                    """
                    INSERT INTO autoagent_start_cancellations (
                        session_id, creation_receipt_sha256, created_at_ms
                    ) VALUES (?, ?, 1)
                    """,
                    ("7" * 32, "a" * 64),
                )
            connection.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="cancellation capacity"):
                connection.execute(
                    """
                    INSERT INTO autoagent_sessions (
                        session_id, cwd, output_dir, skill, method, evolution_goal,
                        creation_receipt_sha256, cancel_requested_at_ms,
                        execution_reference_type, execution_reference,
                        owner_stopped_at_ms, owner_stop_evidence,
                        status, result_json, result_sha256, error_code,
                        error_detail, created_at_ms, updated_at_ms,
                        finished_at_ms, revision
                    ) VALUES (?, ?, ?, 'skill', 'method', '', ?, NULL,
                              'linux-user-systemd-bwrap-v1', ?, NULL, NULL,
                              'running', NULL, NULL, NULL, NULL, 1, 1, NULL, 1)
                    """,
                    (
                        "8" * 32,
                        str(tmp_path),
                        str(tmp_path / "raw-receipt-capacity"),
                        "b" * 64,
                        f"omicsclaw-run-{'8' * 24}.scope",
                    ),
                )

        with pytest.raises(AutoAgentCapacityError, match="cancellation capacity"):
            repository.request_autoagent_cancellation(
                session_id="7" * 32,
                creation_receipt_sha256="a" * 64,
            )

        with pytest.raises(AutoAgentCapacityError, match="cancellation capacity"):
            _accept(
                repository,
                session_id="8" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256="b" * 64,
            )
        with pytest.raises(KeyError):
            repository.get_autoagent_session("8" * 32)


def test_autoagent_existing_receipt_tombstone_survives_exhausted_capacity(
    tmp_path: Path,
) -> None:
    with ControlStateRepository(tmp_path / "control") as repository:
        repository.request_autoagent_cancellation(
            session_id="8" * 32,
            creation_receipt_sha256="b" * 64,
        )
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity SET cancellation_count = 100000 "
                "WHERE singleton_id = 1"
            )
            connection.commit()

        accepted = repository.accept_autoagent_session(
            session_id="8" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256="b" * 64,
        )
        assert accepted.status == "cancelled"
        assert accepted.cancel_requested_at_ms is not None


def test_autoagent_receipt_start_and_abort_serialize_one_cancellable_authority(
    tmp_path: Path,
) -> None:
    session_id = "7" * 32
    receipt_digest = "c" * 64
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    with ControlStateRepository(tmp_path / "control") as repository:
        with sqlite3.connect(repository.database_path) as connection:
            connection.execute(
                "UPDATE autoagent_capacity SET cancellation_count = 99999 "
                "WHERE singleton_id = 1"
            )
            connection.commit()

        def accept() -> None:
            try:
                barrier.wait(2)
                _accept(
                    repository,
                    session_id=session_id,
                    cwd=str(tmp_path),
                    skill="sc-batch-integration",
                    method="harmony",
                    evolution_goal="",
                    creation_receipt_sha256=receipt_digest,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def abort() -> None:
            try:
                barrier.wait(2)
                repository.request_autoagent_cancellation(
                    session_id=session_id,
                    creation_receipt_sha256=receipt_digest,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [threading.Thread(target=accept), threading.Thread(target=abort)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()

        assert errors == []
        record = repository.get_autoagent_session(session_id)
        assert record.creation_receipt_sha256 == receipt_digest
        assert record.status == "cancelled" or record.cancel_requested_at_ms is not None


def test_autoagent_governed_owner_requires_absence_proof_before_terminal(
    tmp_path: Path,
) -> None:
    owner_reference = f"omicsclaw-run-{'a' * 24}.scope"
    with ControlStateRepository(tmp_path / "control") as repository:
        accepted = repository.accept_autoagent_session(
            session_id="8" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256=None,
            execution_reference_type="linux-user-systemd-bwrap-v1",
            execution_reference=owner_reference,
        )
        assert accepted.execution_reference == owner_reference
        assert accepted.owner_stop_evidence is None
        assert repository.list_running_autoagent_sessions() == (accepted,)

        with pytest.raises(ValueError, match="not confirmed stopped"):
            repository.complete_autoagent_session_success(
                accepted.session_id,
                {"success": True},
            )
        reconciliation = repository.reconcile_autoagent_sessions()
        assert reconciliation.interrupted_session_ids == ()
        assert reconciliation.unconfirmed_session_ids == (accepted.session_id,)
        assert repository.get_autoagent_session(accepted.session_id).status == (
            "running"
        )

        with pytest.raises(ValueError, match="stop evidence"):
            repository.confirm_autoagent_owner_stopped(
                accepted.session_id,
                evidence_code="pid_missing",
            )
        stopped = repository.confirm_autoagent_owner_stopped(accepted.session_id)
        assert stopped.owner_stop_evidence == "process_tree_absent_v1"
        assert stopped.owner_stopped_at_ms is not None
        assert stopped.revision == 2
        assert repository.confirm_autoagent_owner_stopped(accepted.session_id) == stopped

        completed = repository.complete_autoagent_session_success(
            accepted.session_id,
            {
                "success": True,
                "mode": "harness_evolution",
                "skill": accepted.skill,
                "method": accepted.method,
                "evolution_goal": accepted.evolution_goal,
                "output_dir": accepted.output_dir,
                "promotion": {"status": "skipped"},
            },
        )
        assert completed.status == "done"
        assert completed.revision == 3


def test_autoagent_active_running_capacity_is_durable_and_released_by_terminal(
    tmp_path: Path,
) -> None:
    with ControlStateRepository(tmp_path / "control") as repository:
        running_ids = tuple(f"{index:032x}" for index in range(10, 14))
        for session_id in running_ids:
            _accept(
                repository,
                session_id=session_id,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256=None,
            )

        with pytest.raises(
            AutoAgentActiveCapacityError,
            match="active session capacity",
        ):
            _accept(
                repository,
                session_id="e" * 32,
                cwd=str(tmp_path),
                skill="sc-batch-integration",
                method="harmony",
                evolution_goal="",
                creation_receipt_sha256=None,
            )

        with sqlite3.connect(repository.database_path) as connection:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="active session capacity",
            ):
                connection.execute(
                    """
                    INSERT INTO autoagent_sessions (
                        session_id, cwd, output_dir, skill, method, evolution_goal,
                        creation_receipt_sha256, cancel_requested_at_ms,
                        execution_reference_type, execution_reference,
                        owner_stopped_at_ms, owner_stop_evidence,
                        status, result_json, result_sha256, error_code,
                        error_detail, created_at_ms, updated_at_ms,
                        finished_at_ms, revision
                    ) VALUES (?, ?, ?, 'skill', 'method', '', NULL, NULL,
                              'linux-user-systemd-bwrap-v1', ?, NULL, NULL,
                              'running', NULL, NULL, NULL, NULL, 1, 1, NULL, 1)
                    """,
                    (
                        "f" * 32,
                        str(tmp_path),
                        str(tmp_path / "raw-active-over-capacity"),
                        f"omicsclaw-run-{'f' * 24}.scope",
                    ),
                )

        # An exact pre-accept tombstone remains observable even while scientific
        # worker admission is saturated because it never creates a live owner.
        repository.request_autoagent_cancellation(
            session_id="9" * 32,
            creation_receipt_sha256="9" * 64,
        )
        pre_cancelled = repository.accept_autoagent_session(
            session_id="9" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256="9" * 64,
        )
        assert pre_cancelled.status == "cancelled"

        repository.confirm_autoagent_owner_stopped(running_ids[0])
        repository.complete_autoagent_session_error(
            running_ids[0],
            status="error",
            error_code="harness_failed",
            error_detail="Harness evolution failed",
        )
        replacement = _accept(
            repository,
            session_id="e" * 32,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="",
            creation_receipt_sha256=None,
        )
        assert replacement.status == "running"


def test_autoagent_success_identity_and_manual_promotion_are_exact(
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    with ControlStateRepository(tmp_path / "control") as repository:
        accepted = _accept(
            repository,
            session_id=session_id,
            cwd=str(tmp_path),
            skill="sc-batch-integration",
            method="harmony",
            evolution_goal="exact-goal",
            creation_receipt_sha256=None,
        )
        repository.confirm_autoagent_owner_stopped(session_id)
        canonical = {
            "success": True,
            "mode": "harness_evolution",
            "skill": accepted.skill,
            "method": accepted.method,
            "evolution_goal": accepted.evolution_goal,
            "output_dir": accepted.output_dir,
            "promotion": {"status": "skipped"},
        }
        mutations = (
            {"mode": "legacy"},
            {"skill": "other-skill"},
            {"method": "other-method"},
            {"evolution_goal": "other-goal"},
            {"output_dir": str(tmp_path / "other-output")},
            {"promotion": {"status": "promoted"}},
        )
        for mutation in mutations:
            with pytest.raises(ValueError, match="terminal result"):
                repository.complete_autoagent_session_success(
                    session_id,
                    canonical | mutation,
                )
            retained = repository.get_autoagent_session(session_id)
            assert retained.status == "running"
            assert retained.result is None
            assert retained.revision == 2

        completed = repository.complete_autoagent_session_success(
            session_id,
            canonical,
        )
        assert completed.status == "done"
        assert completed.revision == 3
