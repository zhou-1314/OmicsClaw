from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from omicsclaw.control import ControlStateRepository


_OWNER_TYPE = "linux-user-systemd-bwrap-v1"
_OWNER_REFERENCE = f"omicsclaw-run-{'b' * 24}.scope"


def _patch_governed_start(monkeypatch, api, owner_type) -> None:
    async def owner_absent(*_args, **_kwargs):
        return "process_tree_absent_v1"

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "durable-test-model",
            "api_key": "governed-test-key",
        },
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_OWNER_TYPE, _OWNER_REFERENCE),
    )
    monkeypatch.setattr(api, "reconcile_governed_worker", owner_absent)
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", owner_type)


def _durable_success(
    *,
    output_dir: str,
    skill: str = "sc-batch-integration",
    method: str = "harmony",
    evolution_goal: str = "",
    **extra,
) -> dict[str, object]:
    return {
        "success": True,
        "mode": "harness_evolution",
        "skill": skill,
        "method": method,
        "evolution_goal": evolution_goal,
        "output_dir": output_dir,
        "promotion": {"status": "skipped"},
        **extra,
    }


def _accept_governed(
    repository: ControlStateRepository,
    *,
    session_id: str,
    cwd: Path,
    output_dir: Path,
    skill: str = "sc-batch-integration",
    method: str = "harmony",
    evolution_goal: str = "",
) -> None:
    repository.accept_autoagent_session(
        session_id=session_id,
        cwd=str(cwd),
        output_dir=str(output_dir),
        skill=skill,
        method=method,
        evolution_goal=evolution_goal,
        creation_receipt_sha256=None,
        execution_reference_type=_OWNER_TYPE,
        execution_reference=_OWNER_REFERENCE,
    )
    repository.confirm_autoagent_owner_stopped(session_id)


@pytest.mark.asyncio
async def test_autoagent_capabilities_are_closed_and_governed_worker_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api

    monkeypatch.setattr(api, "_GOVERNED_WORKER_INTEGRATED", False)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    try:
        assert await api.autoagent_capabilities() == {
            "schema_version": 1,
            "control_authority_id": repository.control_authority_id,
            "durable_session": 1,
            "creation_receipt": 1,
            "preaccept_cancel": 1,
            "terminal_event": 1,
            "governed_worker": 0,
        }
    finally:
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_autoagent_sse_preserves_near_limit_unicode_ipc_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import (
        GovernedWorkerOutcome,
        encode_worker_frame,
    )

    session_id = "0" * 32
    message = "数据🧬" * 25_000
    events = tuple({"index": index, "message": message} for index in range(64))
    ipc_bytes = sum(
        len(
            encode_worker_frame(
                {
                    "version": 1,
                    "kind": "event",
                    "event_type": "progress",
                    "data": event,
                },
                max_bytes=256 * 1024,
            )
        )
        - 4
        for event in events
    )
    assert 15 * 1024 * 1024 < ipc_bytes <= 16 * 1024 * 1024

    captured_request: dict[str, object] = {}

    class UnicodeOwner:
        def __init__(self, **kwargs):
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            assert on_event is not None
            for event in events:
                on_event("progress", event)
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_durable_success(
                    output_dir=str(captured_request["output_dir"]),
                    skill=str(captured_request["skill_name"]),
                    method=str(captured_request["method"]),
                    evolution_goal=str(captured_request["evolution_goal"]),
                ),
            )

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, UnicodeOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id=session_id,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )
        wire = bytearray()
        async for chunk in response.body_iterator:
            wire.extend(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
            if b"event: done\n" in wire[-256:]:
                break

        assert len(wire) < 24 * 1024 * 1024
        assert message[:3].encode("utf-8") in wire
        assert b"\\u6570" not in wire
        assert b"event: transport_error" not in wire
        assert b"event: done" in wire
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


def test_autoagent_sse_renderer_rejects_nonfinite_and_oversized_data() -> None:
    import omicsclaw.autoagent.api as api

    frame = api._render_autoagent_sse_frame(
        "progress",
        {"message": "数据🧬"},
    )
    assert "数据🧬".encode("utf-8") in frame
    assert b"\\u6570" not in frame

    with pytest.raises(api.AutoAgentSSEBoundsError, match="finite JSON"):
        api._render_autoagent_sse_frame("progress", {"score": float("nan")})
    with pytest.raises(api.AutoAgentSSEBoundsError, match="byte bound"):
        api._render_autoagent_sse_frame(
            "progress",
            {"message": "x" * (api._AUTOAGENT_SSE_DATA_MAX_BYTES + 1)},
        )


@pytest.mark.asyncio
async def test_transient_terminal_commit_fault_recovers_without_backend_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped worker remains owned until its durable terminal commit lands."""

    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    commit_attempts = 0
    captured_request: dict[str, object] = {}

    def inject(checkpoint: str) -> None:
        nonlocal commit_attempts
        if checkpoint != "complete_autoagent_session_success.before_commit":
            return
        commit_attempts += 1
        if commit_attempts == 1:
            raise sqlite3.OperationalError("injected transient terminal commit fault")

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_durable_success(
                    output_dir=str(captured_request["output_dir"]),
                    skill=str(captured_request["skill_name"]),
                    method=str(captured_request["method"]),
                    evolution_goal=str(captured_request["evolution_goal"]),
                ),
            )

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, SuccessfulOwner)
    repository = ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id="f" * 32,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )

        async def collect_wire() -> bytearray:
            wire = bytearray()
            async for chunk in response.body_iterator:
                wire.extend(
                    chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                )
            return wire

        wire = await asyncio.wait_for(collect_wire(), timeout=2)

        assert commit_attempts >= 2
        assert b"event: transport_error" in wire
        assert b"event: done" in wire
        assert repository.get_autoagent_session("f" * 32).status == "done"
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_transient_error_commit_fault_recovers_without_fake_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provisional worker error is published only after its DB retry wins."""

    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    commit_attempts = 0

    def inject(checkpoint: str) -> None:
        nonlocal commit_attempts
        if checkpoint != "complete_autoagent_session_error.before_commit":
            return
        commit_attempts += 1
        if commit_attempts == 1:
            raise OSError("injected transient error commit fault")

    class FailedOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("error", error_code="harness_failed")

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, FailedOwner)
    repository = ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id="a" * 32,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )
        wire = bytearray()
        async for chunk in response.body_iterator:
            wire.extend(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

        assert commit_attempts >= 2
        assert wire.count(b"event: transport_error") == 1
        assert b"event: error" in wire
        assert b'"error_code":"harness_failed"' in wire
        retained = repository.get_autoagent_session("a" * 32)
        assert retained.status == "error"
        assert retained.error_code == "harness_failed"
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_persistent_terminal_commit_fault_blocks_novel_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonterminal stopped owner quarantines new scientific admission."""

    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    fail_commit = True
    captured_request: dict[str, object] = {}

    def inject(checkpoint: str) -> None:
        if (
            fail_commit
            and checkpoint == "complete_autoagent_session_success.before_commit"
        ):
            raise sqlite3.OperationalError("injected persistent terminal commit fault")

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_durable_success(
                    output_dir=str(captured_request["output_dir"]),
                    skill=str(captured_request["skill_name"]),
                    method=str(captured_request["method"]),
                    evolution_goal=str(captured_request["evolution_goal"]),
                ),
            )

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, SuccessfulOwner)
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_INITIAL_SECONDS",
        30.0,
    )
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_MAX_SECONDS",
        30.0,
    )
    repository = ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    response = None
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id="d" * 32,
                creation_receipt="a" * 64,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )
        iterator = response.body_iterator.__aiter__()
        first = await asyncio.wait_for(anext(iterator), timeout=1)
        second = await asyncio.wait_for(anext(iterator), timeout=1)
        assert b"event: status" in first
        assert b"event: transport_error" in second

        record = repository.get_autoagent_session("d" * 32)
        assert record.status == "running"
        assert record.owner_stop_evidence == "process_tree_absent_v1"
        assert api._sessions["d" * 32].worker is not None
        assert not api._sessions["d" * 32].worker.done()
        assert (await api.autoagent_capabilities())["governed_worker"] == 0
        with pytest.raises(HTTPException) as blocked:
            await api.optimize_start(
                api.OptimizeRequest(
                    session_id="e" * 32,
                    creation_receipt="b" * 64,
                    skill="sc-batch-integration",
                    method="harmony",
                    cwd=str(tmp_path),
                )
            )
        assert blocked.value.status_code == 503
        with pytest.raises(KeyError):
            repository.get_autoagent_session("e" * 32)
    finally:
        fail_commit = False
        runtime = api._sessions.get("d" * 32)
        if runtime is not None:
            runtime.wake_terminal_commit_retry()
            if runtime.worker is not None:
                await asyncio.wait_for(runtime.worker, timeout=1)
        if response is not None:
            await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_status_wakes_terminal_commit_retry_after_receipt_cancel_202(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt cancellation wakes persistence but cannot replace a stopped outcome."""

    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    fail_commit = True
    commit_attempts = 0
    second_attempt = asyncio.Event()
    captured_request: dict[str, object] = {}

    def inject(checkpoint: str) -> None:
        nonlocal commit_attempts
        if checkpoint != "complete_autoagent_session_success.before_commit":
            return
        commit_attempts += 1
        if commit_attempts >= 2:
            second_attempt.set()
        if fail_commit:
            raise OSError("injected persistent terminal commit fault")

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_durable_success(
                    output_dir=str(captured_request["output_dir"]),
                    skill=str(captured_request["skill_name"]),
                    method=str(captured_request["method"]),
                    evolution_goal=str(captured_request["evolution_goal"]),
                ),
            )

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, SuccessfulOwner)
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_INITIAL_SECONDS",
        30.0,
    )
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_MAX_SECONDS",
        30.0,
    )
    receipt = "c" * 64
    session_id = "c" * 32
    repository = ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    response = None
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id=session_id,
                creation_receipt=receipt,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )
        iterator = response.body_iterator.__aiter__()
        wire = bytearray()
        wire.extend(await asyncio.wait_for(anext(iterator), timeout=1))
        wire.extend(await asyncio.wait_for(anext(iterator), timeout=1))
        assert commit_attempts == 1

        cancelled = await api.abort_autoagent_receipt(
            session_id,
            api.ReconcileAutoAgentRequest(creation_receipt=receipt),
        )
        assert cancelled.status_code == 202
        assert json.loads(cancelled.body) == {
            "session_id": session_id,
            "status": "cancel_requested",
        }
        await asyncio.wait_for(second_attempt.wait(), timeout=1)
        pending = repository.get_autoagent_session(session_id)
        assert pending.status == "running"
        assert pending.cancel_requested_at_ms is not None
        assert not api._sessions[session_id].worker.done()

        fail_commit = False
        observed = await api.optimize_status(session_id)
        assert observed.status == "running"
        done, _pending = await asyncio.wait(
            {api._sessions[session_id].worker},
            timeout=1,
        )
        assert done == {api._sessions[session_id].worker}
        async for chunk in iterator:
            wire.extend(chunk)

        assert wire.count(b"event: transport_error") == 1
        assert b"event: done" in wire
        assert repository.get_autoagent_session(session_id).status == "done"
        assert (await api.autoagent_capabilities())["governed_worker"] == 1
    finally:
        fail_commit = False
        runtime = api._sessions.get(session_id)
        if runtime is not None:
            runtime.wake_terminal_commit_retry()
            if runtime.worker is not None:
                await asyncio.wait({runtime.worker}, timeout=1)
        if response is not None:
            await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_shutdown_reconciles_persistently_failed_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown wakes a pending commit and durably interrupts before detach."""

    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    fail_commit = True
    captured_request: dict[str, object] = {}

    def inject(checkpoint: str) -> None:
        if (
            fail_commit
            and checkpoint == "complete_autoagent_session_success.before_commit"
        ):
            raise OSError("injected persistent terminal commit fault")

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_durable_success(
                    output_dir=str(captured_request["output_dir"]),
                    skill=str(captured_request["skill_name"]),
                    method=str(captured_request["method"]),
                    evolution_goal=str(captured_request["evolution_goal"]),
                ),
            )

    api._sessions.clear()
    api._start_timestamps.clear()
    _patch_governed_start(monkeypatch, api, SuccessfulOwner)
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_INITIAL_SECONDS",
        30.0,
    )
    monkeypatch.setattr(
        api,
        "_AUTOAGENT_TERMINAL_COMMIT_RETRY_MAX_SECONDS",
        30.0,
    )
    session_id = "b" * 32
    repository = ControlStateRepository(
        tmp_path / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    response = None
    shutdown_task: asyncio.Task | None = None
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id=session_id,
                creation_receipt="d" * 64,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
            )
        )
        iterator = response.body_iterator.__aiter__()
        await asyncio.wait_for(anext(iterator), timeout=1)
        terminal_loss = await asyncio.wait_for(anext(iterator), timeout=1)
        assert b"event: transport_error" in terminal_loss

        shutdown_task = asyncio.create_task(
            api.shutdown_autoagent_repository_binding(repository)
        )
        done, _pending = await asyncio.wait({shutdown_task}, timeout=1)
        assert done == {shutdown_task}
        reconciled = shutdown_task.result()
        assert reconciled.interrupted_session_ids == (session_id,)
        retained = repository.get_autoagent_session(session_id)
        assert retained.status == "interrupted"
        assert retained.error_code == "backend_shutdown_interrupted"
        assert session_id not in api._sessions
        assert api._autoagent_repository is None
    finally:
        fail_commit = False
        runtime = api._sessions.get(session_id)
        if runtime is not None:
            runtime.wake_terminal_commit_retry()
        if shutdown_task is not None and not shutdown_task.done():
            await asyncio.wait({shutdown_task}, timeout=1)
        if response is not None:
            await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_preaccept_abort_receipt_prevents_delayed_start_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api

    class ForbiddenOwner:
        def __init__(self, **_kwargs):
            raise AssertionError(
                "pre-accept cancellation must never construct a worker"
            )

    _patch_governed_start(monkeypatch, api, ForbiddenOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    receipt = "a" * 64
    try:
        aborted = await api.abort_autoagent_receipt(
            "1" * 32,
            api.ReconcileAutoAgentRequest(creation_receipt=receipt),
        )
        assert aborted.status_code == 200
        assert json.loads(aborted.body) == {
            "session_id": "1" * 32,
            "status": "cancelled",
        }

        delayed = await api.optimize_start(
            api.OptimizeRequest(
                session_id="1" * 32,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
                creation_receipt=receipt,
            )
        )
        assert delayed.status_code == 200
        assert delayed.headers["X-OmicsClaw-AutoAgent-Receipt-Confirmed"] == "true"
        assert json.loads(delayed.body) == {
            "session_id": "1" * 32,
            "status": "cancelled",
        }
        assert "1" * 32 not in api._sessions
        assert repository.get_autoagent_session("1" * 32).status == ("cancelled")
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_receipt_start_capacity_rejects_before_output_claim_or_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from fastapi import HTTPException

    import omicsclaw.autoagent.api as api

    output_claims = 0
    owners = 0

    def forbidden_output_claim(*_args, **_kwargs):
        nonlocal output_claims
        output_claims += 1
        raise AssertionError("capacity rejection must precede output claim")

    class ForbiddenOwner:
        def __init__(self, **_kwargs):
            nonlocal owners
            owners += 1
            raise AssertionError("capacity rejection must precede worker ownership")

    _patch_governed_start(monkeypatch, api, ForbiddenOwner)
    monkeypatch.setattr(api, "preclaim_session_output_root", forbidden_output_claim)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE autoagent_capacity SET cancellation_count = 100000 "
            "WHERE singleton_id = 1"
        )
        connection.commit()
    try:
        with pytest.raises(HTTPException) as rejected:
            await api.optimize_start(
                api.OptimizeRequest(
                    session_id="6" * 32,
                    creation_receipt="d" * 64,
                    skill="sc-batch-integration",
                    method="harmony",
                    cwd=str(tmp_path),
                )
            )
        assert rejected.value.status_code == 507
        assert output_claims == 0
        assert owners == 0
        with pytest.raises(KeyError):
            repository.get_autoagent_session("6" * 32)
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_start_observes_cancellation_intent_created_after_database_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api

    class ForbiddenOwner:
        def __init__(self, **_kwargs):
            raise AssertionError(
                "accepted cancellation intent must prevent worker construction"
            )

    _patch_governed_start(monkeypatch, api, ForbiddenOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    original_accept = repository.accept_autoagent_session
    receipt = "b" * 64

    def accept_then_cancel(**kwargs):
        accepted = original_accept(**kwargs)
        repository.request_autoagent_cancellation(
            session_id=accepted.session_id,
            creation_receipt_sha256=hashlib.sha256(receipt.encode("ascii")).hexdigest(),
        )
        return accepted

    monkeypatch.setattr(repository, "accept_autoagent_session", accept_then_cancel)
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id="2" * 32,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
                creation_receipt=receipt,
            )
        )
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "cancelled"
        assert "2" * 32 not in api._sessions
        record = repository.get_autoagent_session("2" * 32)
        assert record.status == "cancelled"
        # Accept, cancellation intent, owner-absence proof, terminal commit.
        assert record.revision == 4
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_autoagent_start_accepts_durably_before_worker_and_reconciles_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    release = asyncio.Event()
    started = asyncio.Event()

    class BlockingOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            release.set()

        async def run(self, *, on_event=None):
            started.set()
            await release.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "error",
                error_code="harness_failed",
            )

    _patch_governed_start(monkeypatch, api, BlockingOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    receipt = "a" * 64
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id="3" * 32,
                skill="sc-batch-integration",
                method="harmony",
                cwd=str(tmp_path),
                creation_receipt=receipt,
            )
        )
        assert response.headers["X-OmicsClaw-AutoAgent-Receipt-Confirmed"] == "true"
        await asyncio.wait_for(started.wait(), timeout=1)

        accepted = repository.get_autoagent_session("3" * 32)
        assert accepted.status == "running"
        assert (
            accepted.creation_receipt_sha256
            == hashlib.sha256(receipt.encode("ascii")).hexdigest()
        )
        assert accepted.creation_receipt_sha256 != receipt
        assert receipt.encode("ascii") not in repository.database_path.read_bytes()

        reconciled = await api.reconcile_autoagent_session(
            "3" * 32,
            api.ReconcileAutoAgentRequest(creation_receipt=receipt),
        )
        assert reconciled == {"session_id": "3" * 32, "status": "accepted"}

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as mismatch:
            await api.reconcile_autoagent_session(
                "3" * 32,
                api.ReconcileAutoAgentRequest(creation_receipt="b" * 64),
            )
        assert mismatch.value.status_code == 409

        with pytest.raises(HTTPException) as collision:
            await api.optimize_start(
                api.OptimizeRequest(
                    session_id="3" * 32,
                    skill="sc-batch-integration",
                    method="harmony",
                    cwd=str(tmp_path),
                    creation_receipt="b" * 64,
                )
            )
        assert collision.value.status_code == 409
    finally:
        release.set()
        runtime = api._sessions.get("3" * 32)
        if runtime is not None and runtime.worker is not None:
            await asyncio.wait_for(runtime.worker, timeout=2)
        await response.body_iterator.aclose()
        api.unbind_autoagent_repository(repository)
        api._sessions.clear()
        repository.close()


@pytest.mark.asyncio
async def test_harness_error_detail_is_not_persisted_in_control_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    secret_error = "provider token must-not-persist"

    class FailedOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            assert on_event is not None
            on_event("reasoning", {"message": secret_error})
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "error",
                error_code="harness_failed",
            )

    _patch_governed_start(monkeypatch, api, FailedOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id="4" * 32,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
        )
    )
    runtime = api._sessions["4" * 32]
    assert runtime.worker is not None
    await asyncio.wait_for(runtime.worker, timeout=2)
    try:
        record = repository.get_autoagent_session("4" * 32)
        assert record.status == "error"
        assert record.error_code == "harness_failed"
        assert record.error_detail == "Harness evolution failed"
        assert runtime.snapshot()[2] == "Harness evolution failed"
        status = await api.optimize_status("4" * 32)
        assert status.error == "Harness evolution failed"
        assert secret_error.encode() not in repository.database_path.read_bytes()
    finally:
        await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_terminal_autoagent_result_and_save_survive_backend_restart(
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    state_root = tmp_path / "control"
    output_dir = tmp_path / "output" / "run"
    result = _durable_success(
        output_dir=str(output_dir),
        evolution_goal="improve",
        best_score=0.9,
        improvement_pct=10.0,
        patches_accepted=0,
        accepted_files=[],
        accepted_patch_commits=[],
    )
    first = ControlStateRepository(state_root)
    api.bind_autoagent_repository(first)
    _accept_governed(
        first,
        session_id="5" * 32,
        cwd=tmp_path,
        output_dir=output_dir,
        evolution_goal="improve",
    )
    first.complete_autoagent_session_success("5" * 32, result)
    api.unbind_autoagent_repository(first)
    first.close()
    api._sessions.clear()

    restarted = ControlStateRepository(state_root)
    api.bind_autoagent_repository(restarted)
    try:
        status = await api.optimize_status("5" * 32)
        assert status.status == "done"
        assert status.result == result
        assert await api.optimize_results("5" * 32) == result

        saved = await api.save_evolved_config(
            api.SaveConfigRequest(session_id="5" * 32)
        )
        assert saved["success"] is True
        assert saved["relative_path"].startswith(".omicsclaw/evolved/evolved-")
        assert Path(saved["path"]).is_file()
        first_projection = Path(saved["path"]).read_bytes()
    finally:
        api.unbind_autoagent_repository(restarted)
        restarted.close()

    restarted_again = ControlStateRepository(state_root)
    api.bind_autoagent_repository(restarted_again)
    try:
        saved_again = await api.save_evolved_config(
            api.SaveConfigRequest(session_id="5" * 32)
        )
        assert saved_again == saved
        assert Path(saved_again["path"]).read_bytes() == first_projection
    finally:
        api.unbind_autoagent_repository(restarted_again)
        restarted_again.close()


@pytest.mark.asyncio
async def test_backend_restart_interrupts_running_autoagent_and_blocks_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    import omicsclaw.autoagent.api as api

    state_root = tmp_path / "control"
    first = ControlStateRepository(state_root)
    api.bind_autoagent_repository(first)
    first.accept_autoagent_session(
        session_id="6" * 32,
        cwd=str(tmp_path),
        skill="sc-batch-integration",
        method="harmony",
        evolution_goal="improve",
        creation_receipt_sha256=None,
        execution_reference_type=_OWNER_TYPE,
        execution_reference=_OWNER_REFERENCE,
    )
    with pytest.raises(HTTPException) as no_receipt:
        await api.reconcile_autoagent_session(
            "6" * 32,
            api.ReconcileAutoAgentRequest(creation_receipt="c" * 64),
        )
    assert no_receipt.value.status_code == 409
    api.unbind_autoagent_repository(first)
    first.close()
    api._sessions.clear()

    async def owner_absent(*_args, **_kwargs):
        return "process_tree_absent_v1"

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(api, "reconcile_governed_worker", owner_absent)
    restarted = ControlStateRepository(state_root)
    reconciliation = await api.bind_governed_autoagent_repository(restarted)
    try:
        assert reconciliation.interrupted_session_ids == ("6" * 32,)
        status = await api.optimize_status("6" * 32)
        assert status.status == "interrupted"
        assert status.result is None

        with pytest.raises(HTTPException) as save_rejected:
            await api.save_evolved_config(api.SaveConfigRequest(session_id="6" * 32))
        assert save_rejected.value.status_code == 409

        with pytest.raises(HTTPException) as promote_rejected:
            await api.promote_session("6" * 32)
        assert promote_rejected.value.status_code == 409

        with pytest.raises(HTTPException) as results_rejected:
            await api.optimize_results("6" * 32)
        assert results_rejected.value.status_code == 409
        assert results_rejected.value.detail == (
            "Optimization interrupted by Backend restart"
        )
    finally:
        api.unbind_autoagent_repository(restarted)
        restarted.close()


@pytest.mark.asyncio
async def test_terminal_ttl_reaps_only_cache_not_durable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api

    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    output_dir = tmp_path / "ttl-result"
    _accept_governed(
        repository,
        session_id="7" * 32,
        cwd=tmp_path,
        output_dir=output_dir,
    )
    result = _durable_success(
        output_dir=str(output_dir),
        best_score=0.7,
    )
    repository.complete_autoagent_session_success("7" * 32, result)
    runtime = api.OptimizeSessionRuntime(
        session_id="7" * 32,
        loop=asyncio.get_running_loop(),
    )
    runtime.status = "done"
    runtime.result = result
    runtime.finished_at = 1.0
    api._sessions["7" * 32] = runtime
    monkeypatch.setattr(
        api.time,
        "monotonic",
        lambda: 1.0 + api._SESSION_TTL_SECONDS + 10,
    )
    try:
        status = await api.optimize_status("7" * 32)
        assert "7" * 32 not in api._sessions
        assert status.status == "done"
        assert status.result == result
        assert await api.optimize_results("7" * 32) == result
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_shutdown_quarantines_unconfirmed_owner_and_recovers_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    started = asyncio.Event()
    cancellation_seen = asyncio.Event()

    class UnconfirmedOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            cancellation_seen.set()

        async def run(self, *, on_event=None):
            started.set()
            await cancellation_seen.wait()
            return GovernedWorkerOutcome("cancelled", error_code="cancelled")

    _patch_governed_start(monkeypatch, api, UnconfirmedOwner)

    async def owner_unconfirmed(*_args, **_kwargs):
        raise RuntimeError("injected owner remains populated")

    monkeypatch.setattr(api, "reconcile_governed_worker", owner_unconfirmed)
    state_root = tmp_path / "control"
    repository = ControlStateRepository(state_root)
    api.bind_autoagent_repository(repository)
    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id="8" * 32,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
        )
    )
    runtime = api._sessions["8" * 32]
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(api.AutoAgentWorkersUnconfirmedError) as shutdown_error:
        await api.shutdown_autoagent_repository_binding(
            repository,
            worker_join_timeout_seconds=0.01,
        )
    assert shutdown_error.value.session_ids == ("8" * 32,)
    assert shutdown_error.value.reconciliation.interrupted_session_ids == ()
    assert shutdown_error.value.reconciliation.unconfirmed_session_ids == ("8" * 32,)
    assert runtime.worker is not None and runtime.worker.done()
    assert runtime.repository is None
    assert runtime.terminal_commit_failed is True
    assert repository.get_autoagent_session("8" * 32).status == "running"
    assert api._autoagent_repository is repository
    assert api._autoagent_unconfirmed_owner_session_ids == ("8" * 32,)
    assert runtime.result is None

    async def owner_absent(*_args, **_kwargs):
        return "process_tree_absent_v1"

    monkeypatch.setattr(api, "reconcile_governed_worker", owner_absent)
    recovered = await api.shutdown_autoagent_repository_binding(repository)
    assert recovered.interrupted_session_ids == ("8" * 32,)
    retained = repository.get_autoagent_session("8" * 32)
    assert retained.status == "interrupted"
    assert retained.result is None
    await response.body_iterator.aclose()
    repository.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_worker_that_exits_within_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    raw_detail = "raw cancellation detail"

    class CooperativeOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            cancellation_seen.set()

        async def run(self, *, on_event=None):
            started.set()
            assert on_event is not None
            on_event("reasoning", {"message": raw_detail})
            await cancellation_seen.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("cancelled", error_code="cancelled")

    _patch_governed_start(monkeypatch, api, CooperativeOwner)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id="9" * 32,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    reconciliation = await api.shutdown_autoagent_repository_binding(
        repository,
        worker_join_timeout_seconds=1.0,
    )
    try:
        assert reconciliation.interrupted_session_ids == ()
        record = repository.get_autoagent_session("9" * 32)
        assert record.status == "interrupted"
        assert record.error_code == "backend_shutdown_interrupted"
        assert record.error_detail == "Optimization interrupted by Backend shutdown"
        assert raw_detail.encode() not in repository.database_path.read_bytes()
    finally:
        await response.body_iterator.aclose()
        repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_action", "fault_checkpoint", "expected_pre_shutdown_status"),
    (
        (
            "success",
            "complete_autoagent_session_success.before_commit",
            "running",
        ),
        ("error", "complete_autoagent_session_error.before_commit", "running"),
        (
            "cancelled",
            "complete_autoagent_session_error.before_commit",
            "cancelling",
        ),
    ),
)
async def test_runtime_never_exposes_terminal_when_terminal_commit_faults(
    tmp_path: Path,
    terminal_action: str,
    fault_checkpoint: str,
    expected_pre_shutdown_status: str,
) -> None:
    import omicsclaw.autoagent.api as api

    armed = False

    def inject(checkpoint: str) -> None:
        if armed and checkpoint == fault_checkpoint:
            raise OSError("injected terminal commit fault")

    repository = ControlStateRepository(
        tmp_path / terminal_action / "control",
        fault_hook=inject,
    )
    api.bind_autoagent_repository(repository)
    session_id = {
        "success": "a" * 32,
        "error": "b" * 32,
        "cancelled": "c" * 32,
    }[terminal_action]
    output_dir = tmp_path / terminal_action / "output"
    _accept_governed(
        repository,
        session_id=session_id,
        cwd=tmp_path,
        output_dir=output_dir,
    )
    runtime = api.OptimizeSessionRuntime(
        session_id=session_id,
        loop=asyncio.get_running_loop(),
        repository=repository,
    )
    api._sessions[runtime.session_id] = runtime
    armed = True
    if terminal_action == "success":
        runtime.mark_done(
            _durable_success(
                output_dir=str(output_dir),
                best_score=1.0,
            )
        )
    elif terminal_action == "error":
        runtime.mark_error("raw provider failure")
    else:
        runtime.request_cancel()
        runtime.mark_cancelled("raw cancellation detail")
    await asyncio.sleep(0)
    try:
        status, result, error = runtime.snapshot()
        assert status == expected_pre_shutdown_status
        assert result is None
        assert error == "Optimization terminal commit unavailable"
        retained = repository.get_autoagent_session(runtime.session_id)
        assert retained.status == "running"
        assert retained.revision == 2
        assert retained.result is None

        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())
        assert [event["type"] for event in events] == [
            "transport_error",
            "_finished",
        ]

        armed = False
        reconciliation = await api.shutdown_autoagent_repository_binding(repository)
        assert reconciliation.interrupted_session_ids == (runtime.session_id,)
        assert runtime.snapshot()[0] == "interrupted"
        runtime.mark_done({"success": True, "best_score": 2.0})
        runtime.mark_error("late error")
        runtime.mark_cancelled("late cancellation")
        assert runtime.snapshot()[0] == "interrupted"
        assert repository.get_autoagent_session(runtime.session_id).status == (
            "interrupted"
        )
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_shutdown_repository_failure_still_detaches_and_clears_runtime(
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    armed = False

    def inject(checkpoint: str) -> None:
        if armed and checkpoint == "reconcile_autoagent_sessions.before_commit":
            raise OSError("injected shutdown reconciliation fault")

    repository = ControlStateRepository(tmp_path / "control", fault_hook=inject)
    api.bind_autoagent_repository(repository)
    session_id = "d" * 32
    _accept_governed(
        repository,
        session_id=session_id,
        cwd=tmp_path,
        output_dir=tmp_path / "shutdown-fault-output",
    )
    runtime = api.OptimizeSessionRuntime(
        session_id=session_id,
        loop=asyncio.get_running_loop(),
        repository=repository,
    )
    runtime.terminal_commit_failed = True
    api._sessions[runtime.session_id] = runtime
    armed = True
    try:
        with pytest.raises(OSError, match="reconciliation fault"):
            await api.shutdown_autoagent_repository_binding(repository)
        assert runtime.repository is None
        assert runtime.session_id not in api._sessions
        assert api._autoagent_repository is None
        runtime.mark_done({"success": True, "best_score": 1.0})
        assert runtime.snapshot()[0] == "running"
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


def test_manual_promotion_recovers_from_durable_result_and_workspace_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omicsclaw.autoagent as autoagent_package
    import omicsclaw.autoagent.api as api
    import omicsclaw.autoagent.harness_workspace as workspace_module
    from omicsclaw.autoagent.harness_workspace import PromotionResult

    state_root = tmp_path / "control"
    output_root = tmp_path / "output" / "run"
    sandbox_repo = output_root / "sandbox_repo"
    sandbox_repo.mkdir(parents=True)
    journal = output_root / "promotion-applied"
    mutation_count = 0

    class JournalBackedWorkspace:
        def __init__(self, _source_root: Path, candidate_root: Path) -> None:
            assert candidate_root == output_root
            self.repo_root = sandbox_repo

        def open_existing(self) -> None:
            return None

        def durable_accepted_head_record(self) -> object:
            return object()

        def promote_accepted_state(self, *, accepted_patch: object) -> PromotionResult:
            nonlocal mutation_count
            assert accepted_patch is not None
            if not journal.exists():
                mutation_count += 1
                journal.write_text("applied\n", encoding="utf-8")
            return PromotionResult(
                status="applied",
                promoted_files=["skills/example.py"],
                journal_path=str(journal),
            )

    monkeypatch.setattr(workspace_module, "HarnessWorkspace", JournalBackedWorkspace)
    monkeypatch.setattr(
        autoagent_package, "_check_protected_branch", lambda _root: None
    )

    first = ControlStateRepository(state_root)
    api.bind_autoagent_repository(first)
    _accept_governed(
        first,
        session_id="a" * 32,
        cwd=tmp_path,
        output_dir=output_root,
    )
    first.complete_autoagent_session_success(
        "a" * 32,
        _durable_success(
            output_dir=str(output_root),
            best_score=1.0,
            improvement_pct=1.0,
            patches_accepted=1,
            accepted_files=["skills/example.py"],
            accepted_patch_commits=["a" * 40],
        ),
    )
    api.unbind_autoagent_repository(first)
    first.close()

    for _attempt in range(2):
        restarted = ControlStateRepository(state_root)
        api.bind_autoagent_repository(restarted)
        api._sessions.clear()
        try:
            response = asyncio.run(api.promote_session("a" * 32))
            assert response["status"] == "applied"
        finally:
            api.unbind_autoagent_repository(restarted)
            restarted.close()

    assert mutation_count == 1
