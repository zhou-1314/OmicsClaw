from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from omicsclaw.control import DeliveryCandidate
from omicsclaw.control.delivery import DeliveryAttemptRequest
from omicsclaw.control.models import DeliveryAttemptOutcome
from omicsclaw.surfaces.channels.telegram_delivery import TelegramDeliveryAdapter


def _request(*, thread_id: str | None = None) -> DeliveryAttemptRequest:
    reply_target = {
        "schema_version": 1,
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "primary",
        "destination_id": "-1001234567890",
    }
    if thread_id is not None:
        reply_target["thread_id"] = thread_id
    candidate = DeliveryCandidate(
        delivery_id="c" * 32,
        item_id="b" * 32,
        surface="channel",
        reply_target_key="telegram:primary:-1001234567890",
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
        text="one frozen delivery item",
    )


class _FakeBot:
    def __init__(self, *, result=None, error: BaseException | None = None) -> None:
        self.result = result or SimpleNamespace(message_id=314159)
        self.error = error
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result

    async def send_photo(self, **_kwargs):  # pragma: no cover - contract tripwire
        raise AssertionError("Delivery Adapter must not send media")

    async def send_document(self, **_kwargs):  # pragma: no cover - contract tripwire
        raise AssertionError("Delivery Adapter must not send media")


@pytest.mark.asyncio
async def test_success_is_one_accepted_text_attempt_with_bounded_evidence():
    bot = _FakeBot()
    adapter = TelegramDeliveryAdapter(bot)

    result = await adapter(_request(thread_id="42"))

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED
    assert result.error_code is None
    assert result.provider_evidence == {"message_id": 314159}
    assert result.retry_after_ms is None
    assert bot.calls == [
        {
            "chat_id": -1001234567890,
            "text": "one frozen delivery item",
            "message_thread_id": 42,
        }
    ]


@pytest.mark.asyncio
async def test_non_integer_provider_message_id_is_bounded():
    bot = _FakeBot(result=SimpleNamespace(message_id="m" * 1_000))

    result = await TelegramDeliveryAdapter(bot).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTED
    assert result.provider_evidence == {"message_id": "m" * 128}
    assert len(bot.calls) == 1


class RetryAfter(Exception):
    def __init__(self, retry_after) -> None:
        super().__init__("retry later")
        self.retry_after = retry_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "expected_ms"),
    [(3, 3_000), (0.25, 250), (timedelta(seconds=2), 2_000)],
)
async def test_retry_after_is_known_not_accepted_and_never_retried(
    retry_after,
    expected_ms,
):
    bot = _FakeBot(error=RetryAfter(retry_after))

    result = await TelegramDeliveryAdapter(bot).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE
    assert result.error_code == "telegram_retry_after"
    assert result.provider_evidence is None
    assert result.retry_after_ms == expected_ms
    assert len(bot.calls) == 1


class BadRequest(Exception):
    pass


class Forbidden(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (BadRequest("bad payload"), "telegram_bad_request"),
        (Forbidden("blocked"), "telegram_forbidden"),
    ],
)
async def test_stable_provider_rejection_is_permanent(error, error_code):
    bot = _FakeBot(error=error)

    result = await TelegramDeliveryAdapter(bot).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.REJECTED_PERMANENT
    assert result.error_code == error_code
    assert result.provider_evidence is None
    assert result.retry_after_ms is None
    assert len(bot.calls) == 1


class TimedOut(Exception):
    pass


class NetworkError(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimedOut("timeout"), NetworkError("network"), RuntimeError("unexpected")],
)
async def test_uncertain_provider_exception_is_acceptance_unknown(error):
    bot = _FakeBot(error=error)

    result = await TelegramDeliveryAdapter(bot).attempt(_request())

    assert result.outcome is DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN
    assert result.error_code == "telegram_acceptance_unknown"
    assert result.provider_evidence is None
    assert result.retry_after_ms is None
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_classified_as_provider_outcome():
    bot = _FakeBot(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await TelegramDeliveryAdapter(bot).attempt(_request())

    assert len(bot.calls) == 1
