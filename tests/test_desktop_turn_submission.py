from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.requests import Request


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
)


def _manifest(*, source_id: str = "a" * 32) -> dict[str, object]:
    return {
        "schema_version": 1,
        "conversation_id": None,
        "project_command": None,
        "content": [{"kind": "text", "text": "describe"}],
        "attachment_descriptors": [
            {
                "schema_version": 1,
                "ordinal": 0,
                "source_attachment_id": source_id,
                "display_name": "cell.png",
                "declared_media_type": "image/png",
                "declared_size": len(PNG_BYTES),
                "declared_sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            }
        ],
        "file_selections": [],
        "requested_options": {},
        "retry_of_turn_id": None,
    }


def _multipart_body(
    manifest: dict[str, object] | str,
    *,
    files: list[tuple[str, bytes]] | None = None,
    extra_fields: list[tuple[str, str]] | None = None,
) -> tuple[bytes, str]:
    boundary = "omicsclaw-test-boundary"
    request_json = manifest if isinstance(manifest, str) else json.dumps(manifest)
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="request"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        request_json.encode(),
        b"\r\n",
    ]
    for name, value in extra_fields or []:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    actual_files = [("a" * 32, PNG_BYTES)] if files is None else files
    for source_id, payload in actual_files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    "Content-Disposition: form-data; "
                    f'name="{source_id}"; filename="ignored.png"\r\n'
                ).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                payload,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _request(body: bytes, boundary: str, *, content_length: bool = True) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if content_length:
        headers.append((b"content-length", str(len(body)).encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/turns",
            "raw_path": b"/v1/turns",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8765),
        },
        receive,
    )


@pytest.mark.asyncio
async def test_desktop_multipart_decoder_binds_exact_parts_and_streams_once() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        decode_desktop_multipart_submission,
    )

    body, boundary = _multipart_body(_manifest())
    upload = await decode_desktop_multipart_submission(
        _request(body, boundary),
        max_attachments=8,
        max_batch_bytes=50 * 1024 * 1024,
    )
    try:
        descriptor = upload.submission.attachment_descriptors[0].to_domain()
        assert descriptor.display_name == "cell.png"
        assert upload.source.opened_source_ids == ()
        chunks = [chunk async for chunk in upload.source.open("a" * 32)]
        assert b"".join(chunks) == PNG_BYTES
        assert upload.source.opened_source_ids == ("a" * 32,)
        with pytest.raises(ValueError, match="already opened"):
            _ = [chunk async for chunk in upload.source.open("a" * 32)]
    finally:
        await upload.aclose()
    assert upload.source.closed


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value.update({"file_selections": [{"path": "data.csv"}]}),
        lambda value: value.update({"requested_options": {"model": "other"}}),
        lambda value: value.update({"retry_of_turn_id": "b" * 32}),
        lambda value: value["attachment_descriptors"][0].update(
            {"declared_sha256": None}
        ),
        lambda value: value["attachment_descriptors"][0].update(
            {"source_attachment_id": "provider://secret"}
        ),
    ],
)
def test_desktop_submission_manifest_is_strict(mutation) -> None:
    from omicsclaw.surfaces.desktop.turn_submission import DesktopTurnSubmissionV1

    value = _manifest()
    mutation(value)
    with pytest.raises(ValidationError):
        DesktopTurnSubmissionV1.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("files", "extra_fields"),
    [
        ([], None),
        ([("b" * 32, PNG_BYTES)], None),
        ([("a" * 32, PNG_BYTES), ("a" * 32, PNG_BYTES)], None),
        (None, [("unexpected", "value")]),
    ],
)
async def test_desktop_multipart_decoder_rejects_missing_extra_or_duplicate_parts(
    files,
    extra_fields,
) -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    body, boundary = _multipart_body(
        _manifest(), files=files, extra_fields=extra_fields
    )
    with pytest.raises(DesktopMultipartError):
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )


@pytest.mark.asyncio
async def test_desktop_multipart_decoder_rejects_duplicate_json_keys() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    raw = json.dumps(_manifest())[:-1] + ',"schema_version":1}'
    body, boundary = _multipart_body(raw)
    with pytest.raises(DesktopMultipartError, match="invalid_request_json"):
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )


@pytest.mark.asyncio
async def test_desktop_multipart_decoder_maps_deep_json_and_parser_errors() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    deep_json = '{"nested":' + "[" * 1200 + "0" + "]" * 1200 + "}"
    body, boundary = _multipart_body(deep_json, files=[])
    with pytest.raises(DesktopMultipartError) as deep:
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )
    assert deep.value.code == "invalid_request_json"

    with pytest.raises(DesktopMultipartError) as malformed:
        await decode_desktop_multipart_submission(
            _request(b"not multipart", "b"),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )
    assert malformed.value.code == "invalid_multipart"
    assert malformed.value.status_code == 400


@pytest.mark.asyncio
async def test_desktop_multipart_decoder_rejects_non_utf8_request_document() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    boundary = "utf8-boundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="request"\r\n\r\n',
            b'{"display_name":"\xff"}\r\n',
            f"--{boundary}--\r\n".encode(),
        ]
    )
    with pytest.raises(DesktopMultipartError) as caught:
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )
    assert caught.value.code == "invalid_request_encoding"
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_incomplete_file_part_closes_parser_owned_provisional_spool(
    monkeypatch,
) -> None:
    import starlette.formparsers
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    created = []
    real_spooled_file = starlette.formparsers.SpooledTemporaryFile

    def tracking_spooled_file(*args, **kwargs):
        spool = real_spooled_file(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(
        starlette.formparsers,
        "SpooledTemporaryFile",
        tracking_spooled_file,
    )
    boundary = "incomplete-boundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="a"; filename="x.png"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            PNG_BYTES,
        ]
    )
    with pytest.raises(DesktopMultipartError):
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )
    assert created
    assert all(spool.closed for spool in created)


@pytest.mark.asyncio
async def test_valid_prefix_with_incomplete_extra_file_is_rejected_and_closed(
    monkeypatch,
) -> None:
    import starlette.formparsers
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    created = []
    real_spooled_file = starlette.formparsers.SpooledTemporaryFile

    def tracking_spooled_file(*args, **kwargs):
        spool = real_spooled_file(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(
        starlette.formparsers,
        "SpooledTemporaryFile",
        tracking_spooled_file,
    )
    valid, boundary = _multipart_body(_manifest())
    closing = f"--{boundary}--\r\n".encode()
    assert valid.endswith(closing)
    body = b"".join(
        [
            valid[: -len(closing)],
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="b"; filename="extra.png"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            PNG_BYTES,
        ]
    )

    with pytest.raises(DesktopMultipartError) as caught:
        await decode_desktop_multipart_submission(
            _request(body, boundary),
            max_attachments=8,
            max_batch_bytes=50 * 1024 * 1024,
        )

    assert caught.value.code == "invalid_multipart"
    assert len(created) == 2
    assert all(spool.closed for spool in created)


def test_bounded_parser_accepts_legacy_starlette_bytes_part_storage() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import _BoundedMultiPartParser

    parser = object.__new__(_BoundedMultiPartParser)
    parser._max_request_part_size = 4
    parser._current_part = SimpleNamespace(file=None, data=b"ab")
    parser._file_parts_to_write = []

    parser.on_part_data(b"cd", 0, 2)

    assert parser._current_part.data == b"abcd"


@pytest.mark.asyncio
async def test_chunked_desktop_multipart_is_bounded_by_counted_request_stream() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import (
        DesktopMultipartError,
        decode_desktop_multipart_submission,
    )

    body, boundary = _multipart_body(_manifest(), files=[("a" * 32, b"x" * 2048)])
    with pytest.raises(DesktopMultipartError) as caught:
        await decode_desktop_multipart_submission(
            _request(body, boundary, content_length=False),
            max_attachments=8,
            max_batch_bytes=512,
            max_request_bytes=256,
            multipart_overhead_bytes=64,
        )
    assert caught.value.status_code == 413


def test_desktop_multipart_capacity_fails_closed_and_releases_idempotently() -> None:
    from omicsclaw.surfaces.desktop.turn_submission import DesktopMultipartCapacity

    capacity = DesktopMultipartCapacity(max_active=1)
    first = capacity.try_acquire()
    assert first is not None
    assert capacity.try_acquire() is None
    first.release()
    first.release()
    second = capacity.try_acquire()
    assert second is not None
    second.release()


def test_v1_turns_route_authenticates_before_manual_multipart_parsing() -> None:
    from fastapi.routing import APIRoute
    from omicsclaw.remote.auth import require_bearer_token
    from omicsclaw.surfaces.desktop import server

    route = next(
        route
        for route in server.app.routes
        if isinstance(route, APIRoute)
        and route.path == "/v1/turns"
        and "POST" in route.methods
    )
    assert route.dependant.body_params == []
    assert any(
        dependency.call is require_bearer_token
        for dependency in route.dependant.dependencies
    )


def test_v1_turns_openapi_declares_required_header_body_and_actual_statuses() -> None:
    from omicsclaw.surfaces.desktop import server

    operation = server.app.openapi()["paths"]["/v1/turns"]["post"]
    header = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    assert operation["requestBody"]["required"] is True
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert {
        "200",
        "202",
        "400",
        "408",
        "409",
        "413",
        "415",
        "422",
        "429",
        "503",
    } <= set(operation["responses"])


@pytest.mark.asyncio
async def test_v1_turns_real_http_accepts_receipt_events_and_duplicate(
    monkeypatch,
    tmp_path,
) -> None:
    import httpx

    from omicsclaw.control import ControlRuntime
    from omicsclaw.remote import auth as remote_auth
    from omicsclaw.runtime.agent.events import Final
    from omicsclaw.surfaces.desktop import server

    async def dispatch_events(_envelope):
        yield Final("done")

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path / "state",
        workspace_id=str(tmp_path),
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
        attachment_input_enabled=True,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "secret-token")
    authority = remote_auth.capture_remote_bearer_authority(
        server.app,
        {"OMICSCLAW_REMOTE_AUTH_TOKEN": "secret-token"},
    )
    headers = {
        "Authorization": "Bearer secret-token",
        "Idempotency-Key": "6" * 32,
    }

    def multipart_files():
        return [
            ("request", (None, json.dumps(_manifest()), "application/json")),
            (
                "a" * 32,
                ("ignored.png", PNG_BYTES, "application/octet-stream"),
            ),
        ]

    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post(
                "/v1/turns",
                files=multipart_files(),
                headers=headers,
            )
            assert accepted.status_code == 202
            assert accepted.json()["duplicate"] is False
            location = accepted.headers["location"]

            await runtime._coordinator.wait_idle()
            receipt = await client.get(location, headers=headers)
            assert receipt.status_code == 200
            assert receipt.json()["status"] == "succeeded"

            events = await client.get(f"{location}/events", headers=headers)
            assert events.status_code == 200
            assert events.text.startswith("event: snapshot\n")
            assert "event: final" in events.text
            assert '"terminal": true' in events.text

            duplicate = await client.post(
                "/v1/turns",
                files=multipart_files(),
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["duplicate"] is True
            assert duplicate.json()["turn_id"] == accepted.json()["turn_id"]
    finally:
        remote_auth.release_remote_bearer_authority(server.app, authority)
        await runtime.close()


@pytest.mark.asyncio
async def test_v1_turns_accepts_desktop_image_then_duplicates_without_source_open(
    monkeypatch,
    tmp_path,
) -> None:
    from omicsclaw.control import ControlRuntime
    from omicsclaw.runtime.agent.events import Final
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop.turn_submission import (
        decode_desktop_multipart_submission as real_decode,
    )

    seen_messages = []
    decoded_uploads = []

    async def dispatch_events(envelope):
        seen_messages.append(envelope)
        yield Final("done")

    async def capture_decode(*args, **kwargs):
        upload = await real_decode(*args, **kwargs)
        decoded_uploads.append(upload)
        return upload

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path / "state",
        workspace_id=str(tmp_path),
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
        attachment_input_enabled=True,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    monkeypatch.setattr(server, "decode_desktop_multipart_submission", capture_decode)
    try:
        body, boundary = _multipart_body(_manifest())
        first = await server.submit_desktop_turn_v1(
            _request(body, boundary), idempotency_key="1" * 32
        )
        assert first.status_code == 202
        first_payload = json.loads(first.body)
        assert first_payload["duplicate"] is False
        assert first_payload["status"] in {"queued", "running"}
        assert decoded_uploads[0].source.opened_source_ids == ("a" * 32,)
        assert decoded_uploads[0].source.closed

        duplicate_body, duplicate_boundary = _multipart_body(_manifest())
        duplicate = await server.submit_desktop_turn_v1(
            _request(duplicate_body, duplicate_boundary),
            idempotency_key="1" * 32,
        )
        assert duplicate.status_code == 200
        duplicate_payload = json.loads(duplicate.body)
        assert duplicate_payload["turn_id"] == first_payload["turn_id"]
        assert duplicate_payload["duplicate"] is True
        assert decoded_uploads[1].source.opened_source_ids == ()
        assert decoded_uploads[1].source.closed

        await runtime._coordinator.wait_idle()
        assert len(seen_messages) == 1
        durable_content = json.dumps(seen_messages[0].stored_user_content)
        assert "attachment_ref" in durable_content
        assert "data:" not in durable_content
        assert ".uploads" not in durable_content
        references = runtime.attachment_store.get_turn_references(
            first_payload["turn_id"],
            first_payload["conversation_id"],
        )
        assert len(references) == 1
        assert (
            runtime.repository.get_turn_attachment_commitment(first_payload["turn_id"])
            is not None
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_v1_turns_conflict_and_invalid_digest_fail_closed(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi import HTTPException
    from omicsclaw.control import ControlRuntime
    from omicsclaw.runtime.agent.events import Final
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop.turn_submission import (
        decode_desktop_multipart_submission as real_decode,
    )

    decoded_uploads = []
    dispatch_count = 0

    async def dispatch_events(_envelope):
        nonlocal dispatch_count
        dispatch_count += 1
        yield Final("done")

    async def capture_decode(*args, **kwargs):
        upload = await real_decode(*args, **kwargs)
        decoded_uploads.append(upload)
        return upload

    runtime = ControlRuntime.for_local_surface(
        state_root=tmp_path / "state",
        workspace_id=str(tmp_path),
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        dispatch_events=dispatch_events,
        attachment_input_enabled=True,
    )
    await runtime.start()
    monkeypatch.setattr(server, "_desktop_control_runtime", runtime)
    monkeypatch.setattr(server, "decode_desktop_multipart_submission", capture_decode)
    try:
        body, boundary = _multipart_body(_manifest())
        accepted = await server.submit_desktop_turn_v1(
            _request(body, boundary), idempotency_key="2" * 32
        )
        assert accepted.status_code == 202

        changed = _manifest()
        changed["attachment_descriptors"][0]["display_name"] = "other.png"
        conflict_body, conflict_boundary = _multipart_body(changed)
        with pytest.raises(HTTPException) as conflict:
            await server.submit_desktop_turn_v1(
                _request(conflict_body, conflict_boundary),
                idempotency_key="2" * 32,
            )
        assert conflict.value.status_code == 409
        assert conflict.value.detail == "idempotency_conflict"
        assert decoded_uploads[-1].source.opened_source_ids == ()
        assert decoded_uploads[-1].source.closed

        bad_digest = _manifest()
        bad_digest["attachment_descriptors"][0]["declared_sha256"] = "f" * 64
        rejected_body, rejected_boundary = _multipart_body(bad_digest)
        with pytest.raises(HTTPException) as rejected:
            await server.submit_desktop_turn_v1(
                _request(rejected_body, rejected_boundary),
                idempotency_key="3" * 32,
            )
        assert rejected.value.status_code == 422
        assert rejected.value.detail == "attachment_rejected"
        assert decoded_uploads[-1].source.opened_source_ids == ("a" * 32,)
        assert decoded_uploads[-1].source.closed
        assert (
            runtime.repository.lookup_ingress_turn_id(
                surface="desktop",
                source_namespace="desktop/v1/local/owner",
                source_request_id="3" * 32,
            )
            is None
        )
        await runtime._coordinator.wait_idle()
        assert dispatch_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_v1_turns_rejects_bad_key_and_multipart_backpressure(
    monkeypatch,
) -> None:
    from fastapi import HTTPException
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop.turn_submission import DesktopMultipartCapacity

    body, boundary = _multipart_body(_manifest())
    with pytest.raises(HTTPException) as bad_key:
        await server.submit_desktop_turn_v1(
            _request(body, boundary), idempotency_key="not-opaque"
        )
    assert bad_key.value.status_code == 422

    capacity = DesktopMultipartCapacity(max_active=1)
    lease = capacity.try_acquire()
    assert lease is not None
    monkeypatch.setattr(server, "_desktop_multipart_capacity", capacity)
    monkeypatch.setattr(
        server,
        "_desktop_control_runtime",
        SimpleNamespace(attachment_store=SimpleNamespace()),
    )
    try:
        blocked_body, blocked_boundary = _multipart_body(_manifest())
        with pytest.raises(HTTPException) as blocked:
            await server.submit_desktop_turn_v1(
                _request(blocked_body, blocked_boundary),
                idempotency_key="4" * 32,
            )
        assert blocked.value.status_code == 429
        assert blocked.value.detail == "desktop_multipart_backpressure"
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_v1_turns_read_timeout_cancels_decoder_and_releases_capacity(
    monkeypatch,
) -> None:
    from fastapi import HTTPException
    from omicsclaw.surfaces.desktop import server
    from omicsclaw.surfaces.desktop.turn_submission import DesktopMultipartCapacity

    decoder_finished = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            decoder_finished.set()

    capacity = DesktopMultipartCapacity(max_active=1)
    monkeypatch.setattr(server, "_desktop_multipart_capacity", capacity)
    monkeypatch.setattr(server, "DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(server, "decode_desktop_multipart_submission", never_finishes)
    monkeypatch.setattr(
        server,
        "_desktop_control_runtime",
        SimpleNamespace(
            attachment_store=SimpleNamespace(
                max_attachments=8,
                max_batch_bytes=50 * 1024 * 1024,
            )
        ),
    )
    body, boundary = _multipart_body(_manifest())

    with pytest.raises(HTTPException) as caught:
        await server.submit_desktop_turn_v1(
            _request(body, boundary),
            idempotency_key="5" * 32,
        )

    assert caught.value.status_code == 408
    assert caught.value.detail == "multipart_read_timeout"
    assert decoder_finished.is_set()
    lease = capacity.try_acquire()
    assert lease is not None
    lease.release()
