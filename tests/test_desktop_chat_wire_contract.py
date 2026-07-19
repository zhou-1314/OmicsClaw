from __future__ import annotations

import asyncio
import inspect

from pydantic import ValidationError
import pytest


@pytest.mark.parametrize("host", ["127.0.0.1", "127.12.34.56", "::1", "localhost"])
def test_desktop_server_allows_loopback_without_bearer_token(host: str) -> None:
    from omicsclaw.surfaces.desktop.server import _validate_app_server_security

    _validate_app_server_security(host, "")


@pytest.mark.parametrize(
    "host",
    ["", "0.0.0.0", "::", "192.168.1.20", "2001:db8::1", "desktop.example"],
)
def test_desktop_server_refuses_nonlocal_bind_without_bearer_token(
    host: str,
) -> None:
    from omicsclaw.surfaces.desktop.server import _validate_app_server_security

    with pytest.raises(SystemExit, match="OMICSCLAW_REMOTE_AUTH_TOKEN"):
        _validate_app_server_security(host, "")


def test_desktop_server_allows_nonlocal_bind_with_bearer_token() -> None:
    from omicsclaw.surfaces.desktop.server import _validate_app_server_security

    _validate_app_server_security("0.0.0.0", "configured-token")


def test_chat_request_accepts_the_preparatory_v1_ingress_identity() -> None:
    from omicsclaw.surfaces.desktop.server import ChatRequest

    request = ChatRequest(
        ingress_schema_version=1,
        source_request_id="7" * 32,
        installation_id="3" * 32,
        profile_id="owner",
        session_id="legacy-session",
        content="hello",
    )

    assert request.ingress_schema_version == 1
    assert request.source_request_id == "7" * 32
    assert request.installation_id == "3" * 32
    assert request.profile_id == "owner"
    assert request.session_id == "legacy-session"


@pytest.mark.parametrize("schema_version", [2, True, 1.0])
def test_chat_request_rejects_an_invalid_ingress_schema_version(
    schema_version: object,
) -> None:
    from omicsclaw.surfaces.desktop.server import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest(ingress_schema_version=schema_version, content="hello")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_request_id", "not-an-opaque-id"),
        ("installation_id", "A" * 32),
        ("profile_id", "client-selected-profile"),
    ],
)
def test_chat_request_rejects_malformed_preparatory_identity(
    field: str, value: str
) -> None:
    from omicsclaw.surfaces.desktop.server import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest(content="hello", **{field: value})


def test_abort_request_accepts_only_empty_or_opaque_source_generation() -> None:
    from omicsclaw.surfaces.desktop.server import AbortRequest

    assert AbortRequest(session_id="legacy").source_request_id == ""
    assert (
        AbortRequest(
            session_id="modern",
            source_request_id="a" * 32,
        ).source_request_id
        == "a" * 32
    )
    with pytest.raises(ValidationError):
        AbortRequest(session_id="bad", source_request_id="not-opaque")


def test_health_advertises_authoritative_desktop_control_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omicsclaw.surfaces.desktop import server

    class Core:
        LLM_PROVIDER_NAME = "test"
        OMICSCLAW_MODEL = "test-model"

        @staticmethod
        def _primary_skill_count() -> int:
            return 0

    monkeypatch.setattr(server, "_get_core", lambda: Core())
    monkeypatch.setattr(server, "_kg_status_payload", lambda: {"available": False})
    monkeypatch.setattr(server, "_runtime_health_payload", lambda _core: {})

    payload = asyncio.run(server.health())

    assert payload["contracts"]["desktop_chat"] == {
        "request_schema_version": 1,
        "sse_schema_version": 1,
        "interrupt_schema_version": 1,
        "authoritative_ingress": True,
        "durable_ingress_idempotency": True,
        "source_request_id_required": True,
        "attachments_supported": False,
        "max_sse_frame_bytes": 4 * 1024 * 1024,
        "event_queue_capacity": 8,
        "producer_backpressure": True,
        "oversize_event_projection": True,
        "terminal_error_type_preserved": True,
    }
    assert payload["contracts"]["desktop_turn_observation"] == {
        "schema_version": 1,
        "receipt_path": "/v1/turns/{turn_id}",
        "events_path": "/v1/turns/{turn_id}/events",
        "cancel_path": "/v1/turns/{turn_id}/cancel",
        "snapshot_first": True,
        "event_sequence_starts_at": 1,
        "typed_sse_events": True,
        "last_event_id_replay": True,
        "gap_follows_live": True,
        "durable_event_log": False,
        "interaction_resolution_supported": False,
    }
    assert payload["contracts"]["desktop_turn_submission"] == {
        "request_schema_version": 1,
        "path": "/v1/turns",
        "multipart_supported": True,
        "json_supported": False,
        "attachments_supported": True,
        "file_references_supported": False,
        "legacy_json_files_supported": False,
        "project_commands_supported": False,
        "requested_options_supported": False,
        "retry_of_turn_supported": False,
        "explicit_conversation_supported": True,
        "idempotency_key_required": True,
        "declared_sha256_required": True,
        "max_attachments": 8,
        "max_attachment_bytes": 20 * 1024 * 1024,
        "max_batch_bytes": 50 * 1024 * 1024,
        "max_request_document_bytes": 2 * 1024 * 1024,
        "transport_overhead_allowance_bytes": 64 * 1024,
        "max_transport_bytes": 52 * 1024 * 1024 + 64 * 1024,
        "max_active_multipart_requests": 2,
        "max_inflight_transport_bytes": 2 * (52 * 1024 * 1024 + 64 * 1024),
        "multipart_read_timeout_seconds": 60,
    }


def test_desktop_turn_wire_limits_match_parser_and_attachment_store_defaults() -> None:
    from omicsclaw.attachments.store import (
        DEFAULT_MAX_ATTACHMENT_BYTES,
        DEFAULT_MAX_BATCH_BYTES,
        AttachmentStore,
    )
    from omicsclaw.surfaces.desktop.turn_submission import (
        DEFAULT_MAX_REQUEST_BYTES,
        DEFAULT_MULTIPART_OVERHEAD_BYTES,
        DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS,
        DESKTOP_MAX_ATTACHMENTS,
        DesktopMultipartCapacity,
    )
    from omicsclaw.surfaces.desktop.wire_contract import (
        desktop_turn_submission_contract,
    )

    contract = desktop_turn_submission_contract()
    store_defaults = inspect.signature(AttachmentStore).parameters
    capacity_defaults = inspect.signature(DesktopMultipartCapacity).parameters

    assert contract["max_attachments"] == DESKTOP_MAX_ATTACHMENTS
    assert store_defaults["max_attachments"].default == DESKTOP_MAX_ATTACHMENTS
    assert contract["max_attachment_bytes"] == DEFAULT_MAX_ATTACHMENT_BYTES
    assert contract["max_batch_bytes"] == DEFAULT_MAX_BATCH_BYTES
    assert contract["max_request_document_bytes"] == DEFAULT_MAX_REQUEST_BYTES
    assert (
        contract["transport_overhead_allowance_bytes"]
        == DEFAULT_MULTIPART_OVERHEAD_BYTES
    )
    assert (
        contract["multipart_read_timeout_seconds"]
        == DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS
    )
    assert (
        contract["max_active_multipart_requests"]
        == capacity_defaults["max_active"].default
    )
