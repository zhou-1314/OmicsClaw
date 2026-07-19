"""Production contracts for the Desktop AutoAgent governed-worker cutover."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from omicsclaw.autoagent.process_owner import (
    GovernedWorkerOutcome,
    OWNER_STOP_EVIDENCE_CODE,
)
from omicsclaw.control import ControlStateRepository


_REFERENCE_TYPE = "linux-user-systemd-bwrap-v1"
_REFERENCE = f"omicsclaw-run-{'a' * 24}.scope"


def _reset_api(api: Any) -> None:
    api._sessions.clear()
    api._start_timestamps.clear()
    api._autoagent_repository = None
    api._autoagent_unconfirmed_owner_session_ids = ()


def _successful_result(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "mode": "harness_evolution",
        "skill": request["skill_name"],
        "method": request["method"],
        "evolution_goal": request["evolution_goal"],
        "output_dir": request["output_dir"],
        "promotion": {"status": "skipped"},
    }


@pytest.mark.asyncio
async def test_unsupported_host_rejects_before_accept_or_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    accepted = 0
    spawned = 0
    real_accept = repository.accept_autoagent_session

    def spy_accept(**kwargs: Any):
        nonlocal accepted
        accepted += 1
        return real_accept(**kwargs)

    class ForbiddenOwner:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal spawned
            spawned += 1

    monkeypatch.setattr(repository, "accept_autoagent_session", spy_accept)
    monkeypatch.setattr(api, "governed_worker_available", lambda: False)
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", ForbiddenOwner)
    try:
        with pytest.raises(HTTPException) as error:
            await api.optimize_start(
                api.OptimizeRequest(skill="skill", method="method")
            )
        assert error.value.status_code == 503
        assert accepted == 0
        assert spawned == 0
    finally:
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_missing_provider_rejects_before_owner_reference_or_accept(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    accepted = 0
    referenced = 0
    real_accept = repository.accept_autoagent_session

    def spy_accept(**kwargs: Any):
        nonlocal accepted
        accepted += 1
        return real_accept(**kwargs)

    def forbidden_reference() -> tuple[str, str]:
        nonlocal referenced
        referenced += 1
        return _REFERENCE_TYPE, _REFERENCE

    monkeypatch.setattr(repository, "accept_autoagent_session", spy_accept)
    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: (_ for _ in ()).throw(ValueError("missing provider")),
    )
    monkeypatch.setattr(api, "new_governed_worker_reference", forbidden_reference)
    try:
        with pytest.raises(HTTPException) as error:
            await api.optimize_start(
                api.OptimizeRequest(skill="skill", method="method", cwd=str(tmp_path))
            )
        assert error.value.status_code == 503
        assert accepted == 0
        assert referenced == 0
    finally:
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_app_provider_shape_resolves_secret_only_into_worker_ipc_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.providers.runtime import (
        clear_active_provider_runtime,
        set_active_provider_runtime,
    )

    _reset_api(api)
    secret = "governed-provider-secret-canary"
    set_active_provider_runtime(
        provider="openai",
        base_url="https://active.invalid/v1",
        model="active-model",
        api_key=secret,
    )
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    captured_request: dict[str, Any] = {}

    class ImmediateOwner:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal captured_request
            captured_request = dict(kwargs["request"])
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
                result=_successful_result(captured_request),
            )

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", ImmediateOwner)
    try:
        await api.optimize_start(
            api.OptimizeRequest(
                session_id="b" * 32,
                creation_receipt="c" * 64,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
                provider_id="openai",
                llm_model="requested-model",
            )
        )
        runtime = api._sessions["b" * 32]
        assert runtime.worker is not None
        await runtime.worker
        provider = captured_request["llm_provider_config"]
        assert provider == {
            "provider": "openai",
            "base_url": "https://active.invalid/v1",
            "model": "requested-model",
            "api_key": secret,
        }
        assert secret.encode() not in repository.database_path.read_bytes()
        assert repository.get_autoagent_session("b" * 32).status == "done"
    finally:
        clear_active_provider_runtime()
        api.unbind_autoagent_repository(repository)
        repository.close()


def test_explicit_provider_config_overrides_active_runtime() -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.providers.runtime import (
        clear_active_provider_runtime,
        set_active_provider_runtime,
    )

    set_active_provider_runtime(
        provider="openai",
        base_url="https://active.invalid/v1",
        model="active-model",
        api_key="active-secret",
    )
    try:
        resolved = api._resolve_governed_worker_provider(
            api.OptimizeRequest(
                skill="skill",
                method="method",
                provider_id="openai",
                llm_model="request-model",
                provider_config=api.ProviderConfig(
                    provider="custom",
                    base_url="https://explicit.invalid/v1",
                    model="explicit-model",
                    api_key="explicit-secret",
                ),
            )
        )
        assert resolved == {
            "provider": "custom",
            "base_url": "https://explicit.invalid/v1",
            "model": "explicit-model",
            "api_key": "explicit-secret",
        }
    finally:
        clear_active_provider_runtime()


@pytest.mark.asyncio
async def test_active_oauth_ccproxy_runtime_reaches_worker_ipc_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api
    from omicsclaw.providers.runtime import (
        clear_active_provider_runtime,
        set_active_provider_runtime,
    )

    _reset_api(api)
    set_active_provider_runtime(
        provider="openai",
        model="oauth-model",
        auth_mode="oauth",
        ccproxy_port=19100,
    )
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    captured_request: dict[str, Any] = {}

    class OAuthOwner:
        def __init__(self, **kwargs: Any) -> None:
            captured_request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("error", error_code="harness_failed")

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", OAuthOwner)
    try:
        await api.optimize_start(
            api.OptimizeRequest(
                session_id="c" * 32,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
                provider_id="openai",
            )
        )
        runtime = api._sessions["c" * 32]
        assert runtime.worker is not None
        await runtime.worker
        assert captured_request["llm_provider_config"] == {
            "provider": "openai",
            "base_url": "http://127.0.0.1:19100/codex/v1",
            "model": "oauth-model",
            "api_key": "ccproxy-oauth",
        }
        assert b"ccproxy-oauth" not in repository.database_path.read_bytes()
    finally:
        clear_active_provider_runtime()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_terminal_receipt_waits_for_owner_absence_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    terminal_ready = asyncio.Event()
    release_proof = asyncio.Event()
    request: dict[str, Any] = {}

    class DelayedProofOwner:
        def __init__(self, **kwargs: Any) -> None:
            request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            terminal_ready.set()
            await release_proof.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_successful_result(request),
            )

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", DelayedProofOwner)
    try:
        await api.optimize_start(
            api.OptimizeRequest(
                session_id="d" * 32,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
            )
        )
        runtime = api._sessions["d" * 32]
        await terminal_ready.wait()
        before = repository.get_autoagent_session("d" * 32)
        assert before.status == "running"
        assert before.owner_stop_evidence is None
        assert runtime.queue.empty()
        release_proof.set()
        assert runtime.worker is not None
        await runtime.worker
        after = repository.get_autoagent_session("d" * 32)
        assert after.status == "done"
        assert after.owner_stop_evidence == OWNER_STOP_EVIDENCE_CODE
        terminal_events = []
        while not runtime.queue.empty():
            terminal_events.append(runtime.queue.get_nowait())
        assert terminal_events[-2:] == [
            {
                "type": "done",
                "data": {"session_id": "d" * 32, "status": "done"},
            },
            {"type": "_finished", "data": {}},
        ]
    finally:
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("detach_mode", ["close", "cancel"])
async def test_cancelled_sse_observer_does_not_cancel_accepted_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    detach_mode: str,
) -> None:
    """A receipt-confirmed stream is an observer, not the execution owner."""

    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    started = asyncio.Event()
    release_owner = asyncio.Event()
    owner_cancelled = asyncio.Event()
    request: dict[str, Any] = {}
    session_id = "0" * 32

    class DelayedOwner:
        def __init__(self, **kwargs: Any) -> None:
            request.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            owner_cancelled.set()

        async def run(self, *, on_event=None):
            started.set()
            await release_owner.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_successful_result(request),
            )

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", DelayedOwner)
    response = None
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id=session_id,
                creation_receipt="b" * 64,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
            )
        )
        assert response.headers["X-OmicsClaw-AutoAgent-Receipt-Confirmed"] == "true"
        iterator = response.body_iterator.__aiter__()
        first_frame = await asyncio.wait_for(anext(iterator), timeout=1)
        assert b"event: status" in first_frame
        await asyncio.wait_for(started.wait(), timeout=1)

        if detach_mode == "close":
            await iterator.aclose()
        else:
            pending_observation = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            pending_observation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_observation

        await asyncio.sleep(0)
        assert not owner_cancelled.is_set()
        runtime = api._sessions[session_id]
        assert runtime.worker is not None
        assert not runtime.worker.done()
        assert runtime.snapshot()[0] == "running"
        durable = repository.get_autoagent_session(session_id)
        assert durable.status == "running"
        assert durable.cancel_requested_at_ms is None
        assert (await api.optimize_status(session_id)).status == "running"

        release_owner.set()
        await asyncio.wait_for(runtime.worker, timeout=1)
        assert repository.get_autoagent_session(session_id).status == "done"
        assert (await api.optimize_status(session_id)).status == "done"
        assert (await api.optimize_results(session_id))["success"] is True
    finally:
        release_owner.set()
        runtime = api._sessions.get(session_id)
        if runtime is not None and runtime.worker is not None:
            await asyncio.wait({runtime.worker}, timeout=1)
        if response is not None:
            await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_explicit_abort_cancels_accepted_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The explicit abort Interface remains the execution-cancellation owner."""

    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    started = asyncio.Event()
    owner_cancelled = asyncio.Event()
    session_id = "1" * 32

    class CancellableOwner:
        def __init__(self, **kwargs: Any) -> None:
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            owner_cancelled.set()

        async def run(self, *, on_event=None):
            started.set()
            await owner_cancelled.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("cancelled", error_code="cancelled")

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", CancellableOwner)
    response = None
    try:
        response = await api.optimize_start(
            api.OptimizeRequest(
                session_id=session_id,
                creation_receipt="c" * 64,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
            )
        )
        iterator = response.body_iterator.__aiter__()
        assert b"event: status" in await asyncio.wait_for(anext(iterator), timeout=1)
        await asyncio.wait_for(started.wait(), timeout=1)

        aborted = await api.optimize_abort(session_id)
        assert aborted == {"status": "cancelling", "session_id": session_id}
        await asyncio.wait_for(owner_cancelled.wait(), timeout=1)
        runtime = api._sessions[session_id]
        assert runtime.worker is not None
        await asyncio.wait_for(runtime.worker, timeout=1)

        durable = repository.get_autoagent_session(session_id)
        assert durable.status == "cancelled"
        assert durable.cancel_requested_at_ms is not None
        terminal_wire = bytearray()
        async for frame in iterator:
            terminal_wire.extend(frame)
        assert b"event: error" in terminal_wire
        assert b'"status":"cancelled"' in terminal_wire
    finally:
        runtime = api._sessions.get(session_id)
        if runtime is not None and runtime.worker is not None:
            await asyncio.wait({runtime.worker}, timeout=1)
        if response is not None:
            await response.body_iterator.aclose()
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_restart_reconciles_exact_owner_without_replaying_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    state_root = tmp_path / "control"
    first = ControlStateRepository(state_root)
    first.accept_autoagent_session(
        session_id="e" * 32,
        cwd=str(tmp_path),
        output_dir=str(tmp_path / "output"),
        skill="skill",
        method="method",
        evolution_goal="goal",
        creation_receipt_sha256=None,
        execution_reference_type=_REFERENCE_TYPE,
        execution_reference=_REFERENCE,
    )
    first.close()
    restarted = ControlStateRepository(state_root)
    observed: list[tuple[str, str, str]] = []

    async def reconcile(reference_type: str, reference: str, **kwargs: Any) -> str:
        observed.append((reference_type, reference, kwargs["session_id"]))
        return OWNER_STOP_EVIDENCE_CODE

    class ForbiddenReplay:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("restart reconstructed an executable payload")

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(api, "reconcile_governed_worker", reconcile)
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", ForbiddenReplay)
    try:
        result = await api.bind_governed_autoagent_repository(restarted)
        assert result.interrupted_session_ids == ("e" * 32,)
        assert result.unconfirmed_session_ids == ()
        assert observed == [(_REFERENCE_TYPE, _REFERENCE, "e" * 32)]
        assert restarted.get_autoagent_session("e" * 32).status == "interrupted"
    finally:
        api.unbind_autoagent_repository(restarted)
        restarted.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_stubborn_owner_absence_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    repository = ControlStateRepository(tmp_path / "control")
    api.bind_autoagent_repository(repository)
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_proof = asyncio.Event()

    class StubbornOwner:
        def __init__(self, **kwargs: Any) -> None:
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            cancellation_seen.set()

        async def run(self, *, on_event=None):
            started.set()
            await cancellation_seen.wait()
            await release_proof.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("cancelled", error_code="cancelled")

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        lambda _req: {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "local",
            "api_key": "omicsclaw-local",
        },
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_REFERENCE_TYPE, _REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", StubbornOwner)
    try:
        await api.optimize_start(
            api.OptimizeRequest(
                session_id="f" * 32,
                creation_receipt="a" * 64,
                skill="skill",
                method="method",
                cwd=str(tmp_path),
            )
        )
        await started.wait()
        shutdown = asyncio.create_task(
            api.shutdown_autoagent_repository_binding(repository)
        )
        await cancellation_seen.wait()
        await asyncio.sleep(0)
        assert shutdown.done() is False
        assert repository.get_autoagent_session("f" * 32).status == "running"
        release_proof.set()
        await shutdown
        record = repository.get_autoagent_session("f" * 32)
        assert record.status == "interrupted"
        assert record.owner_stop_evidence == OWNER_STOP_EVIDENCE_CODE
        assert record.error_code == "backend_shutdown_interrupted"
    finally:
        api.unbind_autoagent_repository(repository)
        repository.close()


@pytest.mark.asyncio
async def test_bounded_progress_queue_preserves_terminal_and_finished() -> None:
    import omicsclaw.autoagent.api as api

    runtime = api.OptimizeSessionRuntime(
        session_id="8" * 32,
        loop=asyncio.get_running_loop(),
    )
    for index in range(api._SESSION_EVENT_QUEUE_CAPACITY * 4):
        runtime.emit("progress", {"index": index})
    await asyncio.sleep(0)
    assert runtime.queue.maxsize == api._SESSION_EVENT_QUEUE_CAPACITY
    assert runtime.queue.qsize() <= api._SESSION_EVENT_PROGRESS_LIMIT
    runtime.mark_error("closed test error")
    await asyncio.sleep(0)
    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait()["type"])
    assert events[-2:] == ["error", "_finished"]


@pytest.mark.asyncio
async def test_terminal_sse_receipt_is_small_and_full_result_stays_on_results() -> None:
    import omicsclaw.autoagent.api as api

    _reset_api(api)
    session_id = "9" * 32
    runtime = api.OptimizeSessionRuntime(
        session_id=session_id,
        loop=asyncio.get_running_loop(),
    )
    api._sessions[session_id] = runtime
    full_result = {
        "success": True,
        "large_scientific_payload": "x" * (4 * 1024 * 1024),
    }
    try:
        runtime.mark_done(full_result)
        await asyncio.sleep(0)
        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())

        done = next(event for event in events if event["type"] == "done")
        assert done["data"] == {
            "session_id": session_id,
            "status": "done",
        }
        assert "large_scientific_payload" not in done["data"]
        assert await api.optimize_results(session_id) == full_result
    finally:
        _reset_api(api)


@pytest.mark.asyncio
async def test_error_terminal_receipts_have_exact_closed_shapes() -> None:
    import omicsclaw.autoagent.api as api

    cases = (
        (
            "5" * 32,
            lambda runtime: runtime.mark_error("raw detail"),
            {
                "session_id": "5" * 32,
                "status": "error",
                "error_code": "harness_failed",
            },
        ),
        (
            "6" * 32,
            lambda runtime: runtime.mark_cancelled("raw detail"),
            {
                "session_id": "6" * 32,
                "status": "cancelled",
                "error_code": "cancelled",
            },
        ),
        (
            "7" * 32,
            lambda runtime: runtime.mark_interrupted(
                error_code="backend_shutdown_interrupted"
            ),
            {
                "session_id": "7" * 32,
                "status": "interrupted",
                "error_code": "backend_shutdown_interrupted",
            },
        ),
    )
    for session_id, terminalize, expected in cases:
        runtime = api.OptimizeSessionRuntime(
            session_id=session_id,
            loop=asyncio.get_running_loop(),
        )
        terminalize(runtime)
        await asyncio.sleep(0)
        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())
        assert events == [
            {"type": "error", "data": expected},
            {"type": "_finished", "data": {}},
        ]
