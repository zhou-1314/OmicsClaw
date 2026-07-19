"""Repository contract for explicit Owner resend/retry and operator reads.

ADR 0060 §"Retry, resend and inbound redelivery remain separate":

* ``insert_resend_delivery`` creates a new ``purpose=resend`` Delivery that
  reuses the source's immutable content references, links back through
  ``resend_of_delivery_id`` and never reopens the Turn.
* ``expedite_delivery_retries`` only pulls an already-scheduled ``retry_wait``
  backoff forward — it is the safe in-place retry and never reopens a terminal
  ``failed``/``unknown`` Item.
* ``describe_delivery`` rolls ordered Item states up to one operator view.
"""

from __future__ import annotations

import hashlib

import pytest

from omicsclaw.control import (
    ControlStateRepository,
    DeliveryAttemptOutcome,
    TurnAcceptanceIntent,
    TurnTranscriptRef,
)


def _fingerprint(character: str) -> str:
    return character * 64


def _transcript_ref(seed: str) -> TurnTranscriptRef:
    return TurnTranscriptRef(
        hashlib.sha256(f"entry:{seed}".encode()).hexdigest()[:32],
        hashlib.sha256(f"content:{seed}".encode()).hexdigest(),
    )


def _channel_intent(request_id: str, destination_id: str) -> TurnAcceptanceIntent:
    return TurnAcceptanceIntent(
        surface="channel",
        source_namespace="channel/telegram/v1/primary",
        source_request_id=request_id,
        fingerprint_version=1,
        fingerprint_sha256=_fingerprint(request_id[0]),
        reply_target={
            "kind": "channel",
            "adapter": "telegram",
            "account_namespace": "primary",
            "destination_id": destination_id,
        },
        new_conversation=True,
    )


def _terminal_delivery(repository, *, request_id, destination_id):
    accepted = repository.accept_turn(_channel_intent(request_id, destination_id))
    repository.start_turn(accepted.turn_id)
    transcript_ref = _transcript_ref(request_id)
    from omicsclaw.control import DeliveryItemPlan, DeliveryPlan

    terminal = repository.terminalize_turn(
        accepted.turn_id,
        terminal_status="succeeded",
        transcript_ref=transcript_ref,
        delivery_plan=DeliveryPlan(
            terminal_kind="succeeded",
            items=(
                DeliveryItemPlan(
                    item_kind="text",
                    content_store="transcript",
                    content_ref=transcript_ref.entry_id,
                    content_sha256=_fingerprint("c"),
                ),
            ),
        ),
    )
    assert terminal.delivery is not None
    return terminal.delivery


def test_describe_delivery_returns_none_for_unknown_id(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        assert repository.describe_delivery("missing-delivery") is None


def test_describe_delivery_reports_queued_delivery_in_progress(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository, request_id="a-descr", destination_id="chat-1"
        )

        summary = repository.describe_delivery(delivery.delivery_id)

        assert summary is not None
        assert summary.delivery.delivery_id == delivery.delivery_id
        assert summary.state == "in_progress"
        assert [item.state for item in summary.items] == ["queued"]


def test_insert_resend_delivery_reuses_frozen_content_and_links_source(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        source = _terminal_delivery(
            repository, request_id="b-resend", destination_id="chat-9"
        )

        resend = repository.insert_resend_delivery(source.delivery_id)

        assert resend.purpose == "resend"
        assert resend.resend_of_delivery_id == source.delivery_id
        assert resend.turn_id == source.turn_id
        assert resend.conversation_id == source.conversation_id
        assert resend.reply_target_key == source.reply_target_key
        assert resend.terminal_kind == source.terminal_kind
        assert resend.target_sequence == source.target_sequence + 1

        source_items = repository.list_delivery_items(source.delivery_id)
        resend_items = repository.list_delivery_items(resend.delivery_id)
        assert len(resend_items) == len(source_items)
        assert all(item.state == "queued" for item in resend_items)
        assert [item.content_ref for item in resend_items] == [
            item.content_ref for item in source_items
        ]
        assert [item.content_sha256 for item in resend_items] == [
            item.content_sha256 for item in source_items
        ]
        # Fresh opaque Item identity; the source is never mutated.
        assert {item.item_id for item in resend_items}.isdisjoint(
            item.item_id for item in source_items
        )
        assert [item.state for item in source_items] == ["queued"]


def test_insert_resend_delivery_chains_resend_of_resend(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        source = _terminal_delivery(
            repository, request_id="c-chain", destination_id="chat-3"
        )

        first = repository.insert_resend_delivery(source.delivery_id)
        second = repository.insert_resend_delivery(first.delivery_id)

        assert first.target_sequence == source.target_sequence + 1
        assert second.target_sequence == first.target_sequence + 1
        assert second.resend_of_delivery_id == first.delivery_id


def test_insert_resend_delivery_unknown_source_raises(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        with pytest.raises(KeyError):
            repository.insert_resend_delivery("does-not-exist")


def test_expedite_delivery_retries_pulls_retry_wait_forward(tmp_path):
    clock = {"now": 1_000}
    with ControlStateRepository(tmp_path, clock_ms=lambda: clock["now"]) as repository:
        delivery = _terminal_delivery(
            repository, request_id="d-retry", destination_id="chat-5"
        )
        item = repository.list_delivery_items(delivery.delivery_id)[0]
        started = repository.begin_delivery_attempt(item.item_id)
        assert started.started
        repository.finish_delivery_attempt(
            started.attempt_id,
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_at_ms=5_000,
        )

        waiting = repository.list_delivery_items(delivery.delivery_id)[0]
        assert waiting.state == "retry_wait"
        assert waiting.next_attempt_at_ms == 5_000
        accounts = [("telegram", "primary")]
        assert repository.list_due_delivery_candidates(adapter_accounts=accounts) == ()

        rearmed = repository.expedite_delivery_retries(delivery.delivery_id)

        assert rearmed == 1
        rearmed_item = repository.list_delivery_items(delivery.delivery_id)[0]
        assert rearmed_item.state == "retry_wait"
        assert rearmed_item.next_attempt_at_ms == 1_000
        due = repository.list_due_delivery_candidates(adapter_accounts=accounts)
        assert [candidate.item_id for candidate in due] == [item.item_id]
        # Re-running the expedite is idempotent once the horizon is already due.
        assert repository.expedite_delivery_retries(delivery.delivery_id) == 0


def test_expedite_delivery_retries_noop_without_waiting_item(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository, request_id="e-noop", destination_id="chat-6"
        )

        assert repository.expedite_delivery_retries(delivery.delivery_id) == 0


def test_expedite_delivery_retries_unknown_delivery_raises(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        with pytest.raises(KeyError):
            repository.expedite_delivery_retries("does-not-exist")
