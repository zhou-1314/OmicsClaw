"""Unit tests for the ADR-0064 Memory projection applicator."""

import hashlib

import pytest

from omicsclaw.control.models import ProjectionIntentRecord, StateChangeResult
from omicsclaw.memory.projection import (
    DIGEST_MISMATCH,
    SOURCE_MISSING,
    ProjectionResult,
    apply_projection_intent,
)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _intent(
    *,
    state: str = "pending",
    content: bytes = b"frozen-projection",
    intent_id: str = "pi1",
    project_id: str = "proj1",
    last_error_code: str | None = None,
    content_sha256: str | None = None,
) -> ProjectionIntentRecord:
    return ProjectionIntentRecord(
        projection_intent_id=intent_id,
        project_id=project_id,
        origin_kind="turn",
        origin_id="turn1",
        projection_kind="insight",
        projection_schema_version=1,
        source_store="transcript",
        source_ref="ref-1",
        content_sha256=content_sha256 if content_sha256 is not None else _digest(content),
        state=state,
        last_error_code=last_error_code,
        created_at_ms=1,
        updated_at_ms=1,
        applied_at_ms=1 if state == "applied" else None,
    )


class FakeFinisher:
    """Mimics repository.finish_project_projection idempotency semantics."""

    def __init__(self, *, raise_once: bool = False):
        self.state = "pending"
        self.calls: list[tuple[str, str | None]] = []
        self._raise_once = raise_once

    def __call__(self, projection_intent_id, *, state, error_code=None):
        if self._raise_once:
            self._raise_once = False
            raise RuntimeError("simulated crash before durable mark")
        self.calls.append((state, error_code))
        if self.state == state:
            return StateChangeResult(False, "already_terminal")
        if self.state != "pending":
            return StateChangeResult(False, "projection_state_conflict")
        self.state = state
        return StateChangeResult(True, state)


class RecordingWriter:
    """Idempotent-by-intent-ID Memory writer that counts calls."""

    def __init__(self):
        self.writes: dict[str, bytes] = {}
        self.call_count = 0
        self.seen_project_ids: list[str] = []

    def __call__(self, *, intent, content):
        self.call_count += 1
        self.seen_project_ids.append(intent.project_id)
        self.writes[intent.projection_intent_id] = content  # idempotent by ID


def _reader_for(content: bytes | None):
    def _read(*, source_store, source_ref):
        return content

    return _read


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_applies_pending_intent():
    content = b"an insight worth keeping"
    intent = _intent(content=content)
    writer = RecordingWriter()
    finisher = FakeFinisher()

    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(content),
        write_projection=writer,
        finish_intent=finisher,
    )

    assert outcome.result is ProjectionResult.APPLIED
    assert writer.writes == {"pi1": content}
    assert finisher.calls == [("applied", None)]
    assert finisher.state == "applied"


def test_writes_to_frozen_project_scope():
    content = b"x"
    writer = RecordingWriter()
    apply_projection_intent(
        _intent(content=content, project_id="frozen-proj"),
        read_source=_reader_for(content),
        write_projection=writer,
        finish_intent=FakeFinisher(),
    )
    # The applicator hands the writer the frozen project_id, never a "current"
    # one — this is why it can never broaden scope after archive.
    assert writer.seen_project_ids == ["frozen-proj"]


# --------------------------------------------------------------------------- #
# Digest / source failures — permanent, marked failed, no write
# --------------------------------------------------------------------------- #


def test_digest_mismatch_fails_without_writing():
    intent = _intent(content=b"original")
    writer = RecordingWriter()
    finisher = FakeFinisher()

    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(b"tampered-since-freeze"),
        write_projection=writer,
        finish_intent=finisher,
    )

    assert outcome.result is ProjectionResult.FAILED
    assert outcome.error_code == DIGEST_MISMATCH
    assert writer.call_count == 0
    assert finisher.calls == [("failed", DIGEST_MISMATCH)]


def test_source_loss_fails_without_writing():
    intent = _intent()
    writer = RecordingWriter()
    finisher = FakeFinisher()

    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(None),  # source vanished
        write_projection=writer,
        finish_intent=finisher,
    )

    assert outcome.result is ProjectionResult.FAILED
    assert outcome.error_code == SOURCE_MISSING
    assert writer.call_count == 0
    assert finisher.calls == [("failed", SOURCE_MISSING)]


# --------------------------------------------------------------------------- #
# Idempotency / restart / duplicate application
# --------------------------------------------------------------------------- #


def test_already_applied_is_noop():
    intent = _intent(state="applied")
    writer = RecordingWriter()
    finisher = FakeFinisher()

    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(b"anything"),
        write_projection=writer,
        finish_intent=finisher,
    )

    assert outcome.result is ProjectionResult.ALREADY_APPLIED
    assert writer.call_count == 0
    assert finisher.calls == []  # no re-mark, no re-read, no re-write


def test_already_failed_is_noop_and_surfaces_error():
    intent = _intent(state="failed", last_error_code=DIGEST_MISMATCH)
    writer = RecordingWriter()
    finisher = FakeFinisher()

    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(b"anything"),
        write_projection=writer,
        finish_intent=finisher,
    )

    assert outcome.result is ProjectionResult.ALREADY_FAILED
    assert outcome.error_code == DIGEST_MISMATCH
    assert writer.call_count == 0
    assert finisher.calls == []


def test_crash_between_write_and_mark_reapplies_idempotently():
    # First pass writes, then the durable mark "crashes" (raises). The Intent is
    # still pending, so a restart re-runs the whole apply: the idempotent writer
    # must not fork a duplicate, and the second mark commits.
    content = b"resilient-insight"
    intent = _intent(content=content)
    writer = RecordingWriter()
    crashing = FakeFinisher(raise_once=True)

    with pytest.raises(RuntimeError):
        apply_projection_intent(
            intent,
            read_source=_reader_for(content),
            write_projection=writer,
            finish_intent=crashing,
        )
    assert writer.call_count == 1
    assert crashing.state == "pending"  # never durably marked

    # Restart: same pending Intent, same source.
    outcome = apply_projection_intent(
        intent,
        read_source=_reader_for(content),
        write_projection=writer,
        finish_intent=crashing,
    )
    assert outcome.result is ProjectionResult.APPLIED
    assert writer.call_count == 2  # re-attempted...
    assert writer.writes == {"pi1": content}  # ...but idempotent by ID, no fork
    assert crashing.state == "applied"


def test_transient_write_fault_leaves_intent_pending():
    def _raise(*, intent, content):
        raise OSError("disk full")

    finisher = FakeFinisher()
    with pytest.raises(OSError):
        apply_projection_intent(
            _intent(),
            read_source=_reader_for(b"frozen-projection"),
            write_projection=_raise,
            finish_intent=finisher,
        )
    # Never marked terminal — the driver retries a still-pending Intent.
    assert finisher.calls == []
    assert finisher.state == "pending"


def test_concurrent_finish_is_tolerated():
    # Another projector marked the Intent applied between our read and our mark;
    # finish reports no change, but our frozen write is done, so we report APPLIED.
    class AlreadyDoneFinisher:
        def __call__(self, projection_intent_id, *, state, error_code=None):
            return StateChangeResult(False, "already_terminal")

    content = b"frozen-projection"
    outcome = apply_projection_intent(
        _intent(content=content),
        read_source=_reader_for(content),
        write_projection=RecordingWriter(),
        finish_intent=AlreadyDoneFinisher(),
    )
    assert outcome.result is ProjectionResult.APPLIED
