"""Single-attempt Feishu Delivery Adapter contract (ADR 0060/0063).

The Adapter must perform exactly one provider call and classify only that
call.  The legacy `_send_text_sync` retried internally on transport errors,
which can duplicate a reply the control plane already believes is ambiguous;
these tests pin the replacement behaviour.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.control import DeliveryCandidate
from omicsclaw.control.delivery import DeliveryAttemptRequest
from omicsclaw.control.models import DeliveryAttemptOutcome
from omicsclaw.surfaces.channels.feishu_delivery import FeishuDeliveryAdapter


def _request(
    *,
    destination_kind: str | None = "chat_id",
    destination_id: str = "oc_chat_1",
    adapter: str = "feishu",
    text: str = "one frozen delivery item",
) -> DeliveryAttemptRequest:
    reply_target = {
        "schema_version": 1,
        "kind": "channel",
        "adapter": adapter,
        "account_namespace": "cli_app_1",
        "destination_id": destination_id,
    }
    if destination_kind is not None:
        reply_target["destination_kind"] = destination_kind
    candidate = DeliveryCandidate(
        delivery_id="c" * 32,
        item_id="b" * 32,
        surface="channel",
        reply_target_key="feishu:cli_app_1:oc_chat_1",
        reply_target=reply_target,
        target_sequence=1,
        ordinal=0,
        item_kind="text",
        content_store="transcript",
        content_ref="transcript://entry/1",
        content_sha256="d" * 64,
        content_range=None,
        render_version=1,
        media_type=None,
        caption_ref=None,
        caption_sha256=None,
        attempt_count=0,
    )
    return DeliveryAttemptRequest(
        attempt_id="a" * 32,
        attempt_no=1,
        candidate=candidate,
        text=text,
    )


class _FakeResponse:
    def __init__(self, *, ok: bool, code: int | None = None, message_id: str | None = None):
        self._ok = ok
        self.code = code
        self.data = SimpleNamespace(message_id=message_id) if message_id else None

    def success(self) -> bool:
        return self._ok


class _FakeClient:
    """Minimal lark client stub exposing only `im.v1.message.create`."""

    def __init__(self, *, response=None, error: BaseException | None = None):
        self.response = response
        self.error = error
        self.calls: list[object] = []
        outer = self

        class _Message:
            def create(self, request):
                outer.calls.append(request)
                if outer.error is not None:
                    raise outer.error
                return outer.response

        self.im = SimpleNamespace(v1=SimpleNamespace(message=_Message()))


def _adapter(client) -> FeishuDeliveryAdapter:
    # Bypass the real lark request builder so the contract is testable without
    # the optional SDK installed.
    return FeishuDeliveryAdapter(client, request_builder=lambda arguments: arguments)


@pytest.mark.asyncio
async def test_successful_send_is_accepted_with_message_evidence():
    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_123"))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED
    assert result.provider_evidence == {"message_id": "om_123"}
    assert result.retry_after_ms is None
    assert len(client.calls) == 1
    assert client.calls[0]["receive_id"] == "oc_chat_1"
    assert client.calls[0]["receive_id_type"] == "chat_id"
    assert client.calls[0]["msg_type"] == "text"
    # ADR 0060: the opaque Delivery Item ID is the provider idempotency key.
    assert client.calls[0]["uuid"] == "b" * 32


@pytest.mark.asyncio
async def test_adapter_performs_exactly_one_provider_call_on_failure():
    """The legacy path retried three times here; the Pump owns retry now."""

    client = _FakeClient(error=ConnectionError("tls handshake failed"))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN
    assert result.error_code == "feishu_acceptance_unknown"
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [99991400, 230020])
async def test_rate_limit_codes_are_safely_retryable(code: int):
    client = _FakeClient(response=_FakeResponse(ok=False, code=code))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
    assert result.error_code == "feishu_rate_limited"
    assert result.provider_evidence == {"code": code}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [230001, 230002, 230013, 99991663, 4242])
async def test_other_answered_codes_are_permanent_not_ambiguous(code: int):
    """A structured response proves non-acceptance, so it must not stall the target."""

    client = _FakeClient(response=_FakeResponse(ok=False, code=code))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT
    assert result.error_code == "feishu_rejected"
    assert result.provider_evidence == {"code": code}


@pytest.mark.asyncio
async def test_unreadable_failure_response_is_acceptance_unknown():
    client = _FakeClient(response=_FakeResponse(ok=False, code=None))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN
    assert result.error_code == "feishu_unclassified_response"


@pytest.mark.asyncio
async def test_provider_retry_hint_is_forwarded_when_sane():
    response = _FakeResponse(ok=False, code=230020)
    response.retry_after = 12
    client = _FakeClient(response=response)

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
    assert result.retry_after_ms == 12_000


@pytest.mark.asyncio
async def test_absent_destination_kind_defaults_to_chat_id():
    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))

    result = await _adapter(client).attempt(_request(destination_kind=None))

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED
    assert client.calls[0]["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_open_id_destination_kind_is_honoured():
    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))

    await _adapter(client).attempt(
        _request(destination_kind="open_id", destination_id="ou_9")
    )

    assert client.calls[0]["receive_id_type"] == "open_id"
    assert client.calls[0]["receive_id"] == "ou_9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"destination_kind": "carrier_pigeon"},
        {"adapter": "telegram"},
        {"destination_id": ""},
        {"text": ""},
    ],
)
async def test_malformed_delivery_is_rejected_without_a_provider_call(kwargs):
    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))

    result = await _adapter(client).attempt(_request(**kwargs))

    assert result.outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT
    assert result.error_code == "feishu_invalid_delivery"
    assert client.calls == []


@pytest.mark.asyncio
async def test_adapter_never_sends_media_or_edits_messages():
    """Contract tripwire: only `im.v1.message.create` may be reached."""

    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))

    def _forbidden(*_args, **_kwargs):  # pragma: no cover - tripwire
        raise AssertionError("Delivery Adapter must not edit or send media")

    client.im.v1.message.update = _forbidden
    client.im.v1.message.delete = _forbidden
    client.im.v1.image = SimpleNamespace(create=_forbidden)
    client.im.v1.file = SimpleNamespace(create=_forbidden)

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED


@pytest.mark.asyncio
async def test_retrying_the_same_item_reuses_one_idempotency_key():
    """Feishu dedups by `uuid`, so a retry must not mint a fresh key."""

    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))
    adapter = _adapter(client)

    first = _request()
    retry = DeliveryAttemptRequest(
        attempt_id="z" * 32,
        attempt_no=2,
        candidate=first.candidate,
        text=first.text,
    )
    await adapter.attempt(first)
    await adapter.attempt(retry)

    assert client.calls[0]["uuid"] == client.calls[1]["uuid"] == "b" * 32


@pytest.mark.asyncio
async def test_builder_without_uuid_support_still_delivers():
    """An older lark-oapi must degrade to non-idempotent, not refuse to send."""

    class _NoUuidBody:
        def __init__(self):
            self.seen = {}

        def receive_id(self, value):
            self.seen["receive_id"] = value
            return self

        def msg_type(self, value):
            self.seen["msg_type"] = value
            return self

        def content(self, value):
            self.seen["content"] = value
            return self

        def build(self):
            return self.seen

    def _builder(arguments):
        body = _NoUuidBody()
        body.receive_id(arguments["receive_id"])
        body.msg_type(arguments["msg_type"])
        body.content(arguments["content"])
        return body.build()

    client = _FakeClient(response=_FakeResponse(ok=True, message_id="om_1"))
    adapter = FeishuDeliveryAdapter(client, request_builder=_builder)

    result = await adapter.attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED
    assert "uuid" not in client.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [False, True])
async def test_repeated_cancellation_waits_for_the_blocking_provider_thread(
    provider_error: bool,
):
    """ADR 0063: the Pump may release a Reply Target only on proven termination.

    The lark SDK call is synchronous, and cancelling an executor future does NOT
    stop the underlying thread. If this Adapter completed promptly on
    cancellation, the Pump would read that as proof of termination, record
    `unknown`, and let a later reply to the same chat start while the old
    provider call was still in flight.
    """

    import threading

    release = threading.Event()
    entered = threading.Event()
    exited = threading.Event()

    class _BlockingClient:
        def __init__(self):
            self.calls = []
            outer = self

            class _Message:
                def create(self, request):
                    outer.calls.append(request)
                    entered.set()
                    try:
                        release.wait(timeout=10)
                        if provider_error:
                            raise RuntimeError("late provider error")
                        return _FakeResponse(ok=True, message_id="om_late")
                    finally:
                        exited.set()

            self.im = SimpleNamespace(v1=SimpleNamespace(message=_Message()))

    client = _BlockingClient()
    task = asyncio.ensure_future(_adapter(client).attempt(_request()))
    await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    # Repeated cancellation must not prove termination while the SDK still runs.
    await asyncio.sleep(0.05)
    assert not task.done(), "Adapter claimed termination while the SDK still ran"
    assert not exited.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_contradictory_success_false_with_code_zero_is_unknown():
    """`success()` false with the success code proves nothing; do not guess."""

    client = _FakeClient(response=_FakeResponse(ok=False, code=0))

    result = await _adapter(client).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN
    assert result.error_code == "feishu_unclassified_response"


@pytest.mark.asyncio
async def test_unusable_client_is_permanent_not_ambiguous():
    """A fault before the socket cannot have reached Feishu.

    Classifying a missing SDK attribute as `acceptance_unknown` would halt the
    ADR 0063 Reply Target barrier and block every later reply to this
    destination over a purely local defect, with no operator recourse.
    """

    adapter = FeishuDeliveryAdapter(SimpleNamespace())

    result = await adapter.attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT
    assert result.error_code == "feishu_client_unavailable"


@pytest.mark.asyncio
async def test_shut_down_executor_is_permanent_not_ambiguous():
    """A rejected executor submission never ran the provider call either."""

    class _Loop:
        def run_in_executor(self, *_args, **_kwargs):
            raise RuntimeError("cannot schedule new futures after shutdown")

    sent: list[object] = []
    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=SimpleNamespace(create=sent.append))
        )
    )
    adapter = FeishuDeliveryAdapter(client)

    import omicsclaw.surfaces.channels.feishu_delivery as module

    original = module.asyncio.get_running_loop
    module.asyncio.get_running_loop = lambda: _Loop()
    try:
        result = await adapter.attempt(_request())
    finally:
        module.asyncio.get_running_loop = original

    assert result.outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT
    assert result.error_code == "feishu_client_unavailable"
    assert sent == []
