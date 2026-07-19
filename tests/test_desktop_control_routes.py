from __future__ import annotations

import asyncio

import pytest

from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    RawContentBlockV1,
    RawInboundV1,
)
from omicsclaw.control.event_hub import TurnEventHub
from omicsclaw.runtime.agent.events import Final, StreamContent


def _raw(request_id: str) -> RawInboundV1:
    return RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace="desktop/v1/local/owner",
        source_request_id=request_id,
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": "local",
            "profile_id": "owner",
            "slot": "desktop-test",
        },
        content=(RawContentBlockV1(kind="text", text="hello"),),
    )


async def _body(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


@pytest.mark.asyncio
async def test_desktop_receipt_and_event_routes_only_observe_existing_turn(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield StreamContent("do")
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    try:
        submitted = await runtime.submit_and_wait(_raw("1" * 32), ControlRuntimePorts())
        turn_id = submitted.acceptance.turn_id

        receipt = await server.get_turn_receipt(turn_id)
        response = await server.observe_turn_events(turn_id)
        replay = await _body(response)
        resumed = await server.observe_turn_events(turn_id, last_event_id="1")
        resumed_body = await _body(resumed)
        terminal_cursor = await server.observe_turn_events(turn_id, last_event_id="2")
        terminal_cursor_body = await _body(terminal_cursor)

        assert receipt["schema_version"] == 1
        assert receipt["project_id"] is None
        assert receipt["retry_of_turn_id"] is None
        assert receipt["status"] == "succeeded"
        assert receipt["transcript_ref"]["entry_id"]
        assert replay.startswith("event: snapshot\n")
        assert "id:" not in replay.split("\n\n", 1)[0]
        assert "id: 1" in replay and "id: 2" in replay
        assert "event: stream_content" in replay
        assert "event: final" in replay
        assert '"type": "StreamContent"' in replay
        assert '"type": "Final"' in replay
        assert resumed_body.startswith("event: snapshot\n")
        assert "id: 1" not in resumed_body and "id: 2" in resumed_body
        assert terminal_cursor_body.startswith("event: snapshot\n")
        assert "id:" not in terminal_cursor_body
        assert dispatch_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_event_route_rejects_negative_cursor_below_sentinel(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from omicsclaw.surfaces.desktop import server

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    try:
        submitted = await runtime.submit_and_wait(_raw("3" * 32), ControlRuntimePorts())
        with pytest.raises(HTTPException) as caught:
            await server.observe_turn_events(
                submitted.acceptance.turn_id,
                last_event_id="-2",
            )
        assert caught.value.status_code == 400
        with pytest.raises(HTTPException) as future:
            await server.observe_turn_events(
                submitted.acceptance.turn_id,
                last_event_id="999",
            )
        assert future.value.status_code == 400
        with pytest.raises(HTTPException) as noncanonical:
            await server.observe_turn_events(
                submitted.acceptance.turn_id,
                last_event_id=" +1 ",
            )
        assert noncanonical.value.status_code == 400
        with pytest.raises(HTTPException) as malformed_turn:
            await server.observe_turn_events("not-an-opaque-id")
        assert malformed_turn.value.status_code == 404
        assert malformed_turn.value.detail == "Turn not found"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_cancel_route_uses_opaque_turn_id(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    started = asyncio.Event()

    async def dispatch_events(envelope):
        started.set()
        while not envelope.cancel_event.is_set():
            await asyncio.sleep(0)
        yield Final("must not win")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    turn_id = ""
    try:

        def accepted(value: str) -> None:
            nonlocal turn_id
            turn_id = value

        task = asyncio.create_task(
            runtime.submit_and_wait(
                _raw("2" * 32),
                ControlRuntimePorts(),
                on_accepted=accepted,
            )
        )
        await started.wait()

        canceled = await server.cancel_turn(turn_id)
        result = await task
        repeated = await server.cancel_turn(turn_id)

        assert canceled["schema_version"] == 1
        assert canceled["turn_id"] == turn_id
        assert canceled["changed"] is True
        assert canceled["code"] == "cancel_requested"
        assert canceled["receipt"]["status"] == "running"
        assert result.receipt.status == "canceled"
        assert repeated["schema_version"] == 1
        assert repeated["changed"] is False
        assert repeated["code"] == "already_terminal"
        assert repeated["receipt"]["status"] == "canceled"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_receipt_projects_conversation_scope_without_expanding_receipt(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    try:
        project = runtime.repository.create_project("Observation project")
        raw = RawInboundV1(
            schema_version=1,
            surface="desktop",
            source_namespace="desktop/v1/local/owner",
            source_request_id="4" * 32,
            reply_target={
                "schema_version": 1,
                "kind": "desktop",
                "installation_id": "local",
                "profile_id": "owner",
                "slot": "desktop-test",
            },
            content=(),
            project_command={"kind": "bind", "project_id": project.project_id},
        )
        result = await runtime.submit_and_wait(raw, ControlRuntimePorts())

        receipt = await server.get_turn_receipt(result.acceptance.turn_id)

        assert receipt["project_id"] == project.project_id
        assert (
            runtime.get_receipt(result.acceptance.turn_id).conversation_id
            == receipt["conversation_id"]
        )
        assert not hasattr(runtime.get_receipt(result.acceptance.turn_id), "project_id")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_gap_snapshot_atomically_follows_only_new_live_frames(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    early_frames_published = asyncio.Event()
    release_terminal = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope):
        yield StreamContent("one")
        yield StreamContent("two")
        yield StreamContent("three")
        early_frames_published.set()
        await release_terminal.wait()
        yield StreamContent("four")
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    runtime._event_hub = TurnEventHub(max_events_per_turn=2)
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("5" * 32),
            ControlRuntimePorts(),
            on_accepted=accepted,
        )
    )
    try:
        await early_frames_published.wait()
        response = await server.observe_turn_events(turn_id, last_event_id="0")
        body_task = asyncio.create_task(_body(response))
        await asyncio.sleep(0)
        release_terminal.set()
        body, result = await asyncio.gather(body_task, task)

        assert result.receipt.status == "succeeded"
        assert body.startswith("event: snapshot\n")
        assert "event: gap" in body
        assert '"reason": "cursor_evicted"' in body
        assert '"oldest_sequence": 2' in body
        assert '"latest_sequence": 3' in body
        assert "id: 1" not in body
        assert "id: 2" not in body
        assert "id: 3" not in body
        assert "id: 4" in body
        assert "id: 5" in body
        assert body.index("event: gap") < body.index("id: 4")
    finally:
        release_terminal.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_restart_observation_uses_terminal_snapshot_without_fake_gap(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    first = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
    )
    await first.start()
    result = await first.submit_and_wait(_raw("6" * 32), ControlRuntimePorts())
    turn_id = result.acceptance.turn_id
    await first.close()

    restarted = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
    )
    await restarted.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", restarted)
    try:
        response = await server.observe_turn_events(turn_id)
        body = await _body(response)

        assert body.startswith("event: snapshot\n")
        assert '"status": "succeeded"' in body
        assert "event: gap" not in body
        assert "id:" not in body
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_desktop_live_buffer_unavailable_is_observation_only(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope):
        started.set()
        await release.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("7" * 32),
            ControlRuntimePorts(),
            on_accepted=accepted,
        )
    )
    try:
        await started.wait()
        assert runtime._event_hub.abandon_turn(turn_id) is True

        response = await server.observe_turn_events(turn_id)
        body = await _body(response)

        assert body.startswith("event: snapshot\n")
        assert "event: gap" in body
        assert '"reason": "buffer_unavailable"' in body
        assert runtime.get_receipt(turn_id).status == "running"

        release.set()
        result = await task
        assert result.receipt.status == "succeeded"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_sse_disconnect_detaches_without_canceling_turn(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope):
        started.set()
        await release.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("8" * 32),
            ControlRuntimePorts(),
            on_accepted=accepted,
        )
    )
    try:
        await started.wait()
        unstarted = await server.observe_turn_events(turn_id)
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 1
        await unstarted.body_iterator.aclose()
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 0

        response = await server.observe_turn_events(turn_id)
        first_chunk = await anext(response.body_iterator)
        first_text = (
            first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
        )
        await response.body_iterator.aclose()

        assert first_text.startswith("event: snapshot\n")
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 0
        assert runtime.get_receipt(turn_id).status == "running"

        release.set()
        result = await task
        assert result.receipt.status == "succeeded"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_sse_send_failure_always_detaches_observer(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope):
        started.set()
        await release.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("9" * 32),
            ControlRuntimePorts(),
            on_accepted=accepted,
        )
    )
    try:
        await started.wait()
        response = await server.observe_turn_events(turn_id)
        sent: list[str] = []

        async def receive():
            await asyncio.Future()

        async def send(message):
            sent.append(message["type"])
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/v1/turns/{turn_id}/events",
            "raw_path": b"/v1/turns/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8765),
        }
        with pytest.raises(Exception):
            await response(scope, receive, send)

        assert sent == ["http.response.start", "http.response.body"]
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 0
        assert runtime.get_receipt(turn_id).status == "running"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_control_observation_opens_event_seam_before_snapshot_and_detaches_on_failure(
    monkeypatch,
    tmp_path,
):
    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = ""

    async def dispatch_events(_envelope):
        started.set()
        await release.wait()
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path,
        workspace_id="workspace-test",
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
    )
    await runtime.start()

    def accepted(value: str) -> None:
        nonlocal turn_id
        turn_id = value

    task = asyncio.create_task(
        runtime.submit_and_wait(
            _raw("a" * 32),
            ControlRuntimePorts(),
            on_accepted=accepted,
        )
    )
    try:
        await started.wait()
        calls: list[str] = []
        original_open = runtime._event_hub.open_observation
        original_snapshot = runtime.get_turn_snapshot

        def open_spy(*args, **kwargs):
            calls.append("event")
            return original_open(*args, **kwargs)

        def snapshot_spy(*args, **kwargs):
            calls.append("snapshot")
            return original_snapshot(*args, **kwargs)

        monkeypatch.setattr(runtime._event_hub, "open_observation", open_spy)
        monkeypatch.setattr(runtime, "get_turn_snapshot", snapshot_spy)
        observation = runtime.open_turn_observation(turn_id)
        assert calls == ["event", "snapshot"]
        await observation.aclose()

        def failing_snapshot(*_args, **_kwargs):
            raise RuntimeError("snapshot failed")

        monkeypatch.setattr(runtime, "get_turn_snapshot", failing_snapshot)
        with pytest.raises(RuntimeError, match="snapshot failed"):
            runtime.open_turn_observation(turn_id)
        assert len(runtime._event_hub._streams[turn_id].subscribers) == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_desktop_sse_body_construction_failure_detaches_open_observation(
    monkeypatch,
):
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    class Observation:
        gap = None
        closed = False

        async def aclose(self):
            self.closed = True

    observation = Observation()

    class Runtime:
        @staticmethod
        def open_turn_observation(_turn_id, *, after_sequence):
            assert after_sequence == 0
            return observation

    def fail_body(*_args, **_kwargs):
        raise TypeError("snapshot is not wire-safe")

    monkeypatch.setattr(server, "_desktop_control_runtime", Runtime())
    monkeypatch.setattr(server, "DesktopTurnSSEBody", fail_body)

    with pytest.raises(TypeError, match="not wire-safe"):
        await server.observe_turn_events("a" * 32)
    assert observation.closed is True


def test_desktop_turn_observation_openapi_uses_versioned_response_models():
    pytest.importorskip("fastapi")
    from omicsclaw.surfaces.desktop import server

    schema = server.app.openapi()

    receipt_ref = schema["paths"]["/v1/turns/{turn_id}"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    cancel_ref = schema["paths"]["/v1/turns/{turn_id}/cancel"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"]
    assert receipt_ref.endswith("/DesktopTurnReceiptV1")
    assert cancel_ref.endswith("/DesktopTurnCancelResultV1")
    event_content = schema["paths"]["/v1/turns/{turn_id}/events"]["get"]["responses"][
        "200"
    ]["content"]
    assert "text/event-stream" in event_content
    assert "application/json" not in event_content
    receipt_schema = schema["components"]["schemas"]["DesktopTurnReceiptV1"]
    cancel_schema = schema["components"]["schemas"]["DesktopTurnCancelResultV1"]
    assert "schema_version" in receipt_schema["required"]
    assert "interaction_snapshot" in receipt_schema["required"]
    assert "schema_version" in cancel_schema["required"]
