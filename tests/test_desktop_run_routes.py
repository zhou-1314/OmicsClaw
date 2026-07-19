from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from omicsclaw.control import (
    RunAcceptanceStatus,
    RunIntegrityEvidenceCode,
    RunIntegrityIncidentPage,
    RunIntegrityIncidentRecord,
    RunIntegrityIncidentType,
    RunObservationSnapshot,
    RunRecord,
)
from omicsclaw.control.run_runtime import (
    RunCancelResult,
    RunSubmissionResult,
)
from omicsclaw.surfaces.desktop.run_wire import (
    DESKTOP_RUN_MAX_JSON_NESTING,
    DESKTOP_RUN_MAX_REQUEST_BYTES,
    DesktopRunIntegrityIncidentPageV1,
    DesktopRunSubmissionV1,
    DesktopRunWireError,
    decode_desktop_run_submission,
    desktop_run_integrity_incident_page_v1,
    desktop_run_receipt_v1,
)


@pytest.fixture
def desktop_remote_authority(monkeypatch: pytest.MonkeyPatch):
    """Install the same process-lifetime bearer authority as app lifespan."""

    from omicsclaw.remote import auth as remote_auth
    from omicsclaw.surfaces.desktop import server

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    authority = remote_auth.capture_remote_bearer_authority(
        server.app,
        os.environ,
    )
    try:
        yield authority
    finally:
        remote_auth.release_remote_bearer_authority(server.app, authority)


def _body(*, scope=None, resource=None) -> dict:
    return {
        "schema_version": 1,
        "run_kind": "skill",
        "scope": scope or {"kind": "unassigned"},
        "skill_name": "genomics-vcf-operations",
        "input": {"kind": "demo"},
        "parameters": {},
        "resource_contract": resource
        or {
            "kind": "simple",
            "request": {
                "cpu_cores": 1,
                "memory_mib": 1024,
                "gpu_devices": 0,
                "threads": 1,
                "temporary_disk_mib": 2048,
            },
        },
        "parent_turn_id": None,
        "retry_of_run_id": None,
    }


def _receipt(status: str = "queued", revision: int = 1) -> RunRecord:
    return RunRecord(
        run_id="a" * 32,
        scope_kind="unassigned",
        project_id=None,
        run_kind="skill",
        parent_turn_id=None,
        retry_of_run_id=None,
        status=status,
        terminal_code=None,
        manifest_ref="run-store:v1:" + "b" * 32,
        created_at_ms=1,
        started_at_ms=2 if status != "queued" else None,
        finished_at_ms=3 if status in {"succeeded", "failed", "canceled"} else None,
        revision=revision,
    )


def _incident(seed: str = "c") -> RunIntegrityIncidentRecord:
    return RunIntegrityIncidentRecord(
        incident_id=seed * 32,
        run_id="a" * 32,
        assignment_id="b" * 32,
        incident_type=RunIntegrityIncidentType.EXECUTION_OWNER_UNCONFIRMED,
        evidence_code=RunIntegrityEvidenceCode.EXECUTION_OWNER_STOP_UNCONFIRMED,
        receipt_revision=2,
        evidence_schema_version=1,
        evidence_sha256="d" * 64,
        created_at_ms=7,
    )


def test_run_wire_parses_only_typed_scope_and_first_tracer_subset() -> None:
    unassigned = DesktopRunSubmissionV1.model_validate(_body())
    assert unassigned.to_domain("1" * 32).scope.kind == "unassigned"
    project = DesktopRunSubmissionV1.model_validate(
        _body(scope={"kind": "project", "project_id": "2" * 32})
    )
    assert project.to_domain("1" * 32).scope.project_id == "2" * 32

    invalid = [
        _body(scope={"kind": "unassigned", "project_id": "2" * 32}),
        {**_body(), "parameters": {"method": "filter"}},
        {**_body(), "input": {"kind": "path", "path": "/tmp/x"}},
        {**_body(), "retry_of_run_id": "3" * 32},
    ]
    for body in invalid:
        with pytest.raises(ValidationError):
            DesktopRunSubmissionV1.model_validate(body)


def test_run_receipt_wire_preserves_scope_and_opaque_manifest_ref() -> None:
    wire = desktop_run_receipt_v1(
        RunObservationSnapshot(_receipt("succeeded", 3), None)
    )
    assert wire.model_dump(mode="python") == {
        "schema_version": 1,
        "run_id": "a" * 32,
        "scope": {"kind": "unassigned"},
        "run_kind": "skill",
        "parent_turn_id": None,
        "retry_of_run_id": None,
        "status": "succeeded",
        "terminal_code": None,
        "manifest_ref": "run-store:v1:" + "b" * 32,
        "created_at_ms": 1,
        "started_at_ms": 2,
        "finished_at_ms": 3,
        "revision": 3,
    }


def test_run_integrity_incident_wire_is_closed_and_content_free() -> None:
    wire = desktop_run_integrity_incident_page_v1(
        RunIntegrityIncidentPage((_incident(),), None)
    )
    assert wire.model_dump(mode="json") == {
        "schema_version": 1,
        "incidents": [
            {
                "incident_id": "c" * 32,
                "run_id": "a" * 32,
                "assignment_id": "b" * 32,
                "incident_type": "execution_owner_unconfirmed",
                "evidence_code": "execution_owner_stop_unconfirmed",
                "receipt_revision": 2,
                "evidence_schema_version": 1,
                "evidence_sha256": "d" * 64,
                "created_at_ms": 7,
            }
        ],
        "next_cursor": None,
    }
    with pytest.raises(ValidationError):
        DesktopRunIntegrityIncidentPageV1.model_validate(
            {**wire.model_dump(mode="json"), "exception": "/secret/path"}
        )


@pytest.mark.asyncio
async def test_v1_run_routes_are_typed_idempotent_read_only_and_cancelable(
    monkeypatch,
    desktop_remote_authority,
) -> None:
    from omicsclaw.surfaces.desktop import server

    class FakeRuntime:
        ready = True
        lifecycle_ready = True

        def __init__(self) -> None:
            self.submit_calls = 0
            self.get_calls = 0
            self.cancel_calls = 0
            self.receipt = _receipt()

        async def submit(self, submission):
            self.submit_calls += 1
            status = (
                RunAcceptanceStatus.ACCEPTED
                if self.submit_calls == 1
                else RunAcceptanceStatus.DUPLICATE
            )
            return RunSubmissionResult(status, self.receipt)

        def get_receipt(self, run_id):
            assert run_id == self.receipt.run_id
            self.get_calls += 1
            return RunObservationSnapshot(self.receipt, None)

        async def cancel(self, run_id):
            assert run_id == self.receipt.run_id
            self.cancel_calls += 1
            self.receipt = _receipt("canceled", 2)
            return RunCancelResult(True, "canceled_before_assignment", self.receipt)

    runtime = FakeRuntime()
    monkeypatch.setattr(server, "_desktop_run_runtime", runtime)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    headers = {
        "Authorization": "Bearer secret-token",
        "Idempotency-Key": "1" * 32,
    }
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/v1/runs", json=_body(), headers=headers)
        assert accepted.status_code == 202
        assert accepted.headers["location"] == "/v1/runs/" + "a" * 32
        duplicate = await client.post("/v1/runs", json=_body(), headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True

        before_submit_calls = runtime.submit_calls
        observed = await client.get(accepted.headers["location"], headers=headers)
        assert observed.status_code == 200
        assert observed.json()["status"] == "queued"
        assert runtime.submit_calls == before_submit_calls

        canceled = await client.post(
            accepted.headers["location"] + "/cancel", headers=headers
        )
        assert canceled.status_code == 200
        assert canceled.json()["code"] == "canceled_before_assignment"
        assert canceled.json()["receipt"]["status"] == "canceled"

    assert runtime.submit_calls == 2
    assert runtime.get_calls == 1
    assert runtime.cancel_calls == 1


@pytest.mark.asyncio
async def test_v1_run_integrity_incidents_are_pure_observation_in_quarantine(
    monkeypatch,
    desktop_remote_authority,
) -> None:
    from omicsclaw.surfaces.desktop import server

    class ObservationOnlyRuntime:
        ready = False
        lifecycle_ready = True

        def __init__(self) -> None:
            self.calls = []

        def list_integrity_incidents(self, *, run_id, cursor, limit):
            self.calls.append((run_id, cursor, limit))
            if cursor == "f" * 32:
                raise ValueError("unknown cursor")
            return RunIntegrityIncidentPage((_incident(),), "e" * 32)

        def __getattr__(self, name):
            raise AssertionError(f"observation touched execution method {name}")

    runtime = ObservationOnlyRuntime()
    monkeypatch.setattr(server, "_desktop_run_runtime", runtime)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    headers = {"Authorization": "Bearer secret-token"}
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        observed = await client.get(
            "/v1/run-integrity-incidents",
            params={"run_id": "a" * 32, "limit": 17},
            headers=headers,
        )
        assert observed.status_code == 200
        assert observed.json()["incidents"][0]["incident_id"] == "c" * 32
        assert observed.json()["next_cursor"] == "e" * 32
        invalid_cursor = await client.get(
            "/v1/run-integrity-incidents",
            params={"cursor": "f" * 32},
            headers=headers,
        )
        assert invalid_cursor.status_code == 400
        invalid_limit = await client.get(
            "/v1/run-integrity-incidents",
            params={"limit": 101},
            headers=headers,
        )
        assert invalid_limit.status_code == 422

    assert runtime.calls == [("a" * 32, None, 17), (None, "f" * 32, 50)]


def test_v1_run_openapi_and_health_contract_are_versioned() -> None:
    from fastapi.routing import APIRoute
    from omicsclaw.remote.auth import require_bearer_token
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop.wire_contract import desktop_run_contract

    route = next(
        item
        for item in server.app.routes
        if isinstance(item, APIRoute)
        and item.path == "/v1/runs"
        and "POST" in item.methods
    )
    assert any(
        dependency.call is require_bearer_token
        for dependency in route.dependant.dependencies
    )
    operation = server.app.openapi()["paths"]["/v1/runs"]["post"]
    assert {
        "200",
        "202",
        "400",
        "401",
        "408",
        "409",
        "413",
        "415",
        "422",
        "429",
        "503",
    } <= set(operation["responses"])
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["title"] == "DesktopRunSubmissionV1"
    assert "schema_version" in request_schema["required"]
    assert "$defs" in request_schema
    assert "/runs/{run_id}" not in server.app.openapi()["paths"]
    incident_route = next(
        item
        for item in server.app.routes
        if isinstance(item, APIRoute)
        and item.path == "/v1/run-integrity-incidents"
        and "GET" in item.methods
    )
    assert any(
        dependency.call is require_bearer_token
        for dependency in incident_route.dependant.dependencies
    )
    assert {
        "200",
        "400",
        "401",
        "422",
        "503",
    } <= set(
        server.app.openapi()["paths"]["/v1/run-integrity-incidents"]["get"]["responses"]
    )
    assert desktop_run_contract() == {
        "request_schema_version": 1,
        "observation_schema_version": 1,
        "submission_path": "/v1/runs",
        "receipt_path": "/v1/runs/{run_id}",
        "cancel_path": "/v1/runs/{run_id}/cancel",
        "integrity_incident_observation_schema_version": 1,
        "integrity_incident_list_path": "/v1/run-integrity-incidents",
        "max_integrity_incident_page_size": 100,
        "integrity_incident_detail_supported": False,
        "integrity_incident_observation_starts_work": False,
        "simple_skill_supported": True,
        "demo_input_only": True,
        "parameters_supported": False,
        "resource_contract_required": True,
        "idempotency_key_required": True,
        "max_request_bytes": 64 * 1024,
        "max_json_nesting": 64,
        "request_read_timeout_seconds": 60,
        "events_supported": False,
        "observation_starts_work": False,
    }


def _streaming_request(
    chunks: list[bytes],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    delay_seconds: float = 0.0,
) -> Request:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        if messages:
            return messages.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/runs",
            "raw_path": b"/v1/runs",
            "query_string": b"",
            "server": ("test", 80),
            "client": ("client", 1),
            "headers": headers
            or [(b"content-type", b"application/json; charset=utf-8")],
        },
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code", "status"),
    [
        (b'{"schema_version":1,"schema_version":1}', "invalid_run_json", 400),
        (b'{"value":NaN}', "invalid_run_json", 400),
        (b"\xff", "invalid_run_json_encoding", 400),
        (
            (b'{"x":' + b"[" * (DESKTOP_RUN_MAX_JSON_NESTING + 1) + b"0")
            + b"]" * (DESKTOP_RUN_MAX_JSON_NESTING + 1)
            + b"}",
            "invalid_run_json",
            400,
        ),
    ],
)
async def test_run_json_transport_rejects_ambiguous_documents(
    body: bytes,
    code: str,
    status: int,
) -> None:
    with pytest.raises(DesktopRunWireError) as raised:
        await decode_desktop_run_submission(_streaming_request([body]))
    assert raised.value.code == code
    assert raised.value.status_code == status


@pytest.mark.asyncio
async def test_run_json_transport_counts_actual_chunks_and_declared_lengths() -> None:
    oversized = b" " * DESKTOP_RUN_MAX_REQUEST_BYTES + b"{}"
    forged = _streaming_request(
        [oversized[:100], oversized[100:]],
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", b"2"),
        ],
    )
    with pytest.raises(DesktopRunWireError) as raised:
        await decode_desktop_run_submission(forged)
    assert (raised.value.status_code, raised.value.code) == (
        413,
        "run_request_too_large",
    )

    duplicate_length = _streaming_request(
        [json.dumps(_body()).encode()],
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
            (b"content-length", b"2"),
        ],
    )
    with pytest.raises(DesktopRunWireError) as duplicate:
        await decode_desktop_run_submission(duplicate_length)
    assert (duplicate.value.status_code, duplicate.value.code) == (
        400,
        "invalid_content_length",
    )


@pytest.mark.asyncio
async def test_run_json_transport_enforces_content_type_and_deadline() -> None:
    body = json.dumps(_body()).encode()
    unsupported = _streaming_request(
        [body],
        headers=[(b"content-type", b"application/json; boundary=nope")],
    )
    with pytest.raises(DesktopRunWireError) as media:
        await decode_desktop_run_submission(unsupported)
    assert (media.value.status_code, media.value.code) == (
        415,
        "utf8_json_required",
    )

    slow = _streaming_request([body], delay_seconds=0.05)
    with pytest.raises(DesktopRunWireError) as timeout:
        await decode_desktop_run_submission(slow, read_timeout_seconds=0.001)
    assert (timeout.value.status_code, timeout.value.code) == (
        408,
        "run_request_read_timeout",
    )


@pytest.mark.asyncio
async def test_auth_and_idempotency_reject_before_run_body_decode(
    monkeypatch,
    desktop_remote_authority,
) -> None:
    from omicsclaw.surfaces.desktop import server

    async def forbidden_decode(_request):
        raise AssertionError("rejected metadata must not open the request body")

    monkeypatch.setattr(server, "decode_desktop_run_submission", forbidden_decode)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    transport = httpx.ASGITransport(app=server.app)
    body = json.dumps(_body()).encode()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post(
            "/v1/runs",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "1" * 32,
            },
        )
        assert unauthorized.status_code == 401

        invalid = await client.post(
            "/v1/runs",
            content=body,
            headers={
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
                "Idempotency-Key": "not-canonical",
            },
        )
        assert invalid.status_code == 422

        duplicate = await client.post(
            "/v1/runs",
            content=body,
            headers=[
                ("Authorization", "Bearer secret-token"),
                ("Content-Type", "application/json"),
                ("Idempotency-Key", "2" * 32),
                ("Idempotency-Key", "2" * 32),
            ],
        )
        assert duplicate.status_code == 422


@pytest.mark.asyncio
async def test_quarantine_keeps_duplicate_receipt_and_cancel_routes_available(
    monkeypatch,
    desktop_remote_authority,
) -> None:
    from omicsclaw.surfaces.desktop import server

    class QuarantinedRuntime:
        ready = False
        lifecycle_ready = True

        async def submit(self, submission):
            if submission.run_submission_id == "a" * 32:
                return RunSubmissionResult(
                    RunAcceptanceStatus.DUPLICATE,
                    _receipt("succeeded", 3),
                )
            return RunSubmissionResult(
                RunAcceptanceStatus.REJECTED,
                code="control_not_ready",
            )

        def get_receipt(self, run_id):
            assert run_id == "a" * 32
            return RunObservationSnapshot(_receipt("succeeded", 3), None)

        async def cancel(self, run_id):
            assert run_id == "a" * 32
            return RunCancelResult(False, "already_terminal", _receipt("succeeded", 3))

    monkeypatch.setattr(server, "_desktop_run_runtime", QuarantinedRuntime())
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    transport = httpx.ASGITransport(app=server.app)
    common = {"Authorization": "Bearer secret-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        duplicate = await client.post(
            "/v1/runs",
            json=_body(),
            headers={**common, "Idempotency-Key": "a" * 32},
        )
        assert duplicate.status_code == 200
        observed = await client.get("/v1/runs/" + "a" * 32, headers=common)
        assert observed.status_code == 200
        canceled = await client.post("/v1/runs/" + "a" * 32 + "/cancel", headers=common)
        assert canceled.status_code == 200

        novel = await client.post(
            "/v1/runs",
            json=_body(),
            headers={**common, "Idempotency-Key": "b" * 32},
        )
        assert novel.status_code == 503
        assert novel.json()["detail"] == "control_not_ready"


@pytest.mark.asyncio
async def test_desktop_shutdown_attempts_every_owned_phase_after_failure(
    monkeypatch,
) -> None:
    import omicsclaw.autoagent.api as autoagent_api
    from omicsclaw.surfaces.desktop import server

    events: list[str] = []
    repository = object()

    async def bridge_loop() -> None:
        try:
            await asyncio.Future()
        finally:
            events.append("bridge_task")

    class Manager:
        async def stop_all(self):
            events.append("bridge_manager")

    class RunRuntime:
        async def close(self):
            events.append("run")
            raise RuntimeError("injected Run shutdown failure")

    class ControlRuntime:
        def __init__(self, bound_repository):
            self.repository = bound_repository

        async def close(self):
            events.append("control")

    async def stop_autoagent(bound_repository):
        assert bound_repository is repository

    class KernelManager:
        async def shutdown_all(self):
            events.append("notebook")

    class MemoryClient:
        async def close(self):
            events.append("memory")

    bridge_task = asyncio.create_task(bridge_loop())
    control_runtime = ControlRuntime(repository)
    await asyncio.sleep(0)
    monkeypatch.setattr(server, "_bridge_task", bridge_task)
    monkeypatch.setattr(server, "_channel_manager", Manager())
    monkeypatch.setattr(server, "_desktop_run_runtime", RunRuntime())
    monkeypatch.setattr(server, "_desktop_control_runtime", control_runtime)
    monkeypatch.setattr(
        autoagent_api,
        "shutdown_autoagent_repository_binding",
        stop_autoagent,
    )
    monkeypatch.setattr(server, "_memory_client", MemoryClient())
    monkeypatch.setattr(server, "_NOTEBOOK_AVAILABLE", True)
    monkeypatch.setattr(server, "get_kernel_manager", lambda: KernelManager())
    monkeypatch.setattr(server, "_mcp_load_fn", object())
    server._active_sessions.clear()

    failure = await server._shutdown_desktop_lifespan_state()

    assert isinstance(failure, RuntimeError)
    assert str(failure) == "injected Run shutdown failure"
    assert events == [
        "bridge_task",
        "bridge_manager",
        "run",
        "control",
        "notebook",
        "memory",
    ]
    assert server._bridge_task is None
    assert server._channel_manager is None
    assert server._desktop_run_runtime is None
    assert server._desktop_control_runtime is None
    assert server._memory_client is None
    assert server._mcp_load_fn is None


@pytest.mark.asyncio
async def test_desktop_shutdown_reports_unconfirmed_autoagent_worker(
    monkeypatch,
) -> None:
    import omicsclaw.autoagent.api as autoagent_api
    from omicsclaw.control import AutoAgentStartupReconciliationResult
    from omicsclaw.surfaces.desktop import server

    events: list[str] = []
    repository = object()

    class ControlRuntime:
        async def close(self):
            events.append("control")

    control_runtime = ControlRuntime()
    control_runtime.repository = repository

    async def fail_shutdown(bound_repository):
        assert bound_repository is repository
        raise autoagent_api.AutoAgentWorkersUnconfirmedError(
            ("stubborn-worker",),
            AutoAgentStartupReconciliationResult(("stubborn-worker",)),
        )

    monkeypatch.setattr(
        autoagent_api,
        "shutdown_autoagent_repository_binding",
        fail_shutdown,
    )
    monkeypatch.setattr(server, "_desktop_control_runtime", control_runtime)
    monkeypatch.setattr(server, "_desktop_run_runtime", None)
    monkeypatch.setattr(server, "_bridge_task", None)
    monkeypatch.setattr(server, "_channel_manager", None)
    monkeypatch.setattr(server, "_memory_client", None)
    monkeypatch.setattr(server, "_NOTEBOOK_AVAILABLE", False)
    server._active_sessions.clear()

    failure = await server._shutdown_desktop_lifespan_state()

    assert isinstance(failure, autoagent_api.AutoAgentWorkersUnconfirmedError)
    assert failure.session_ids == ("stubborn-worker",)
    assert events == []
    assert server._desktop_control_runtime is control_runtime


@pytest.mark.asyncio
async def test_v1_run_real_http_executes_one_canonical_demo_and_duplicates(
    monkeypatch,
    tmp_path,
    desktop_remote_authority,
) -> None:
    import asyncio

    from omicsclaw.control import ControlStateRepository
    from omicsclaw.control.run_runtime import RunRuntime
    from omicsclaw.skill.resource_scheduler import ExecutionResourceBudget
    from omicsclaw.surfaces.desktop import server

    repo = ControlStateRepository(tmp_path / "state")
    runtime = RunRuntime.for_local_surface(
        repository=repo,
        output_root=tmp_path / "output",
        resource_budget=ExecutionResourceBudget(
            cpu_cores=2,
            memory_mib=4096,
            gpu_device_ids=(),
            threads=2,
            temporary_disk_mib=8192,
            max_processes=2,
        ),
        max_buffered_runs=2,
        max_active_runs=1,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_run_runtime", runtime)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    headers = {
        "Authorization": "Bearer secret-token",
        "Idempotency-Key": "d" * 32,
    }
    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            accepted = await client.post("/v1/runs", json=_body(), headers=headers)
            assert accepted.status_code == 202
            location = accepted.headers["location"]
            receipt = None
            for _ in range(200):
                receipt = await client.get(location, headers=headers)
                if receipt.json()["status"] in {
                    "succeeded",
                    "failed",
                    "canceled",
                    "interrupted",
                }:
                    break
                await asyncio.sleep(0.02)
            assert receipt is not None
            assert receipt.json()["status"] == "succeeded"

            duplicate = await client.post("/v1/runs", json=_body(), headers=headers)
            assert duplicate.status_code == 200
            assert duplicate.json()["run_id"] == accepted.json()["run_id"]
            assert duplicate.json()["duplicate"] is True

        observation = repo.get_run_observation(accepted.json()["run_id"])
        assert observation.assignment is not None
        assert observation.receipt.revision == 3
        assert (
            len(tuple((tmp_path / "output" / ".run-store" / "refs").glob("*.json")))
            == 1
        )
    finally:
        await runtime.close()
        repo.close()
