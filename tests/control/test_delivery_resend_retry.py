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
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from omicsclaw.control import (
    ControlIntegrityError,
    ControlStateRepository,
    DeliveryAttemptOutcome,
    DeliveryCapacityExceededError,
    TurnAcceptanceIntent,
    TurnTranscriptRef,
)
from omicsclaw.control.schema import MIGRATIONS


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


def _settle(repository, delivery, outcome=DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN):
    """Drive every Item of `delivery` to a terminal state.

    Resend is only admissible once the source has stopped moving (ADR 0060), so
    a test that wants to resend must first play out the provider call rather
    than resending a Delivery the Pump may still deliver.
    """

    for item in repository.list_delivery_items(delivery.delivery_id):
        started = repository.begin_delivery_attempt(item.item_id)
        assert started.started
        repository.finish_delivery_attempt(started.attempt_id, outcome)
    return delivery


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
        source = _settle(
            repository,
            _terminal_delivery(
                repository, request_id="b-resend", destination_id="chat-9"
            ),
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
        assert [item.state for item in source_items] == ["unknown"]


def test_insert_resend_delivery_chains_resend_of_resend(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        source = _settle(
            repository,
            _terminal_delivery(
                repository, request_id="c-chain", destination_id="chat-3"
            ),
        )

        first = repository.insert_resend_delivery(source.delivery_id)
        _settle(repository, first)
        second = repository.insert_resend_delivery(first.delivery_id)

        assert first.target_sequence == source.target_sequence + 1
        assert second.target_sequence == first.target_sequence + 1
        assert second.resend_of_delivery_id == first.delivery_id


def test_concurrent_resends_cannot_race_capacity_one(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        source = _settle(
            repository,
            _terminal_delivery(
                repository, request_id="a-concurrent", destination_id="chat-race"
            ),
        )
        start_together = threading.Barrier(2)

        def resend():
            start_together.wait(timeout=2)
            try:
                delivery = repository.insert_resend_delivery(
                    source.delivery_id,
                    max_total=1,
                    max_per_account=1,
                )
            except DeliveryCapacityExceededError:
                return "backpressure", None
            return "created", delivery.delivery_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(lambda _index: resend(), range(2)))

        assert sorted(code for code, _delivery_id in outcomes) == [
            "backpressure",
            "created",
        ]
        created_ids = {
            delivery_id
            for code, delivery_id in outcomes
            if code == "created" and delivery_id is not None
        }
        assert len(created_ids) == 1
        assert {
            delivery.delivery_id for delivery in repository.list_deliveries()
        } == {source.delivery_id, *created_ids}


def test_insert_resend_delivery_unknown_source_raises(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        with pytest.raises(KeyError):
            repository.insert_resend_delivery("does-not-exist")


def test_insert_resend_delivery_rejects_oversized_historical_source(tmp_path):
    """A baseline database may contain more Items than the current bound."""

    database_path = tmp_path / "control.db"
    initial = MIGRATIONS[0]
    reply_target = {
        "kind": "channel",
        "adapter": "telegram",
        "account_namespace": "primary",
        "destination_id": "chat-historical",
    }
    with sqlite3.connect(database_path) as connection:
        connection.executescript(initial.sql)
        connection.execute(
            """
            INSERT INTO schema_migrations (
                version, name, checksum_sha256, applied_at_ms
            ) VALUES (?, ?, ?, ?)
            """,
            (initial.version, initial.name, initial.checksum, 1),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                conversation_id, surface, reply_target_version,
                reply_target_key, reply_target_json, project_id,
                revision, created_at_ms, updated_at_ms
            ) VALUES (?, 'channel', 1, ?, ?, NULL, 1, 1, 1)
            """,
            ("historical-conversation", "historical-target", json.dumps(reply_target)),
        )
        connection.execute(
            """
            INSERT INTO turns (
                turn_id, conversation_id, turn_kind, status, retry_of_turn_id,
                terminal_code, created_at_ms, started_at_ms, finished_at_ms,
                revision
            ) VALUES (?, ?, 'agent', 'succeeded', NULL, NULL, 1, 1, 1, 2)
            """,
            ("historical-turn", "historical-conversation"),
        )
        connection.execute(
            """
            INSERT INTO deliveries (
                delivery_id, turn_id, conversation_id, purpose, terminal_kind,
                surface, reply_target_version, reply_target_key,
                reply_target_json, target_sequence, resend_of_delivery_id,
                created_at_ms
            ) VALUES (?, ?, ?, 'terminal', 'succeeded', 'channel', 1, ?, ?, 1,
                      NULL, 1)
            """,
            (
                "historical-delivery",
                "historical-turn",
                "historical-conversation",
                "historical-target",
                json.dumps(reply_target),
            ),
        )
        connection.executemany(
            """
            INSERT INTO delivery_items (
                item_id, delivery_id, ordinal, item_kind, content_store,
                content_ref, content_sha256, content_range_json,
                render_version, media_type, caption_ref, caption_sha256,
                state, attempt_count, next_attempt_at_ms, last_error_code,
                provider_evidence_json, blocked_by_item_id, delivered_at_ms,
                updated_at_ms
            ) VALUES (?, 'historical-delivery', ?, 'text', 'transcript', ?, ?,
                      NULL, 1, NULL, NULL, NULL, 'delivered', 1, NULL, NULL,
                      NULL, NULL, 1, 1)
            """,
            (
                (f"historical-item-{ordinal}", ordinal, f"entry-{ordinal}", "c" * 64)
                for ordinal in range(65)
            ),
        )

    with ControlStateRepository(tmp_path) as repository:
        with pytest.raises(ControlIntegrityError, match="exceeds"):
            repository.insert_resend_delivery("historical-delivery")

        assert [delivery.delivery_id for delivery in repository.list_deliveries()] == [
            "historical-delivery"
        ]


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


def test_terminal_delivery_plan_is_bounded_by_the_store(tmp_path):
    """ADR 0060 requires a bounded provider-call plan, enforced by the store.

    The text renderer applies its own bound, but it is not the only producer of
    a plan; the authority that commits the plan must refuse an unbounded one.
    """

    from omicsclaw.control import DeliveryItemPlan, DeliveryPlan
    from omicsclaw.control.repository import MAX_DELIVERY_ITEMS

    with ControlStateRepository(tmp_path) as repository:
        accepted = repository.accept_turn(_channel_intent("f-bound", "chat-bound"))
        repository.start_turn(accepted.turn_id)
        transcript_ref = _transcript_ref("f-bound")
        oversized = DeliveryPlan(
            terminal_kind="succeeded",
            items=tuple(
                DeliveryItemPlan(
                    item_kind="text",
                    content_store="transcript",
                    content_ref=transcript_ref.entry_id,
                    content_sha256=_fingerprint("c"),
                )
                for _ in range(MAX_DELIVERY_ITEMS + 1)
            ),
        )

        with pytest.raises(ValueError, match="exceeding the bound"):
            repository.terminalize_turn(
                accepted.turn_id,
                terminal_status="succeeded",
                transcript_ref=transcript_ref,
                delivery_plan=oversized,
            )


def test_list_delivery_attempts_returns_provider_evidence_for_audit(tmp_path):
    """ADR 0060 promises retained attempt history and provider evidence.

    Persisting it is not enough: without a read the Owner cannot tell a
    never-attempted Item from one whose provider call went unanswered.
    """

    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository, request_id="1-audit", destination_id="chat-audit"
        )
        item = repository.list_delivery_items(delivery.delivery_id)[0]

        first = repository.begin_delivery_attempt(item.item_id)
        repository.finish_delivery_attempt(
            first.attempt_id,
            DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE,
            retry_at_ms=0,
        )
        second = repository.begin_delivery_attempt(item.item_id)
        repository.finish_delivery_attempt(
            second.attempt_id,
            DeliveryAttemptOutcome.ACCEPTED,
            provider_evidence={"message_id": "om_provider_1"},
        )

        attempts = repository.list_delivery_attempts(delivery.delivery_id)

        assert [a.attempt_no for a in attempts] == [1, 2]
        assert attempts[0].outcome == "not_accepted_retryable"
        assert attempts[1].outcome == "accepted"
        assert dict(attempts[1].provider_evidence) == {"message_id": "om_provider_1"}
        # The deciding evidence is also readable from the Item itself.
        settled = repository.list_delivery_items(delivery.delivery_id)[0]
        assert settled.state == "delivered"
        assert dict(settled.provider_evidence) == {"message_id": "om_provider_1"}
        assert settled.delivered_at_ms is not None


def test_list_delivery_attempts_is_empty_for_an_unattempted_delivery(tmp_path):
    with ControlStateRepository(tmp_path) as repository:
        delivery = _terminal_delivery(
            repository, request_id="2-none", destination_id="chat-none"
        )

        assert repository.list_delivery_attempts(delivery.delivery_id) == ()
