"""The shadow-review mechanism (P6 item 4): triggers, verdicts, delivery.

The reviewer's model call is a seam every test here drives through a
fake — the point of these tests is the machinery around it: checkpoint
arithmetic, verdict parsing, the one-shot note, the additive frame, and
the off state. A real reviewer model is the thing the risk-matched
verification level declares unverified.
"""

from __future__ import annotations

import asyncio

import pytest

from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.desktop.turn_observation import desktop_chat_frame
from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.nudges import _shadow_review_nudge
from omicsclaw.entry.render import DESKTOP_CHAT_FRAME_TYPE, to_wire
from omicsclaw.entry.shadow_review import (
    MAX_REVIEW_NOTE_CHARS,
    SHADOW_REVIEW_ARTIFACT_THRESHOLD,
    STRUCTURED_ARTIFACT_KINDS,
    ShadowReview,
    ShadowReviewCheckpoint,
    ShadowReviewNudge,
    ShadowReviewVerdict,
    parse_verdict,
)
from omicsclaw.memory.artifacts import ArtifactRecord
from omicsclaw.schema import Role

WAIT_S = 10.0


def run(coro):
    """One bounded await, driven without a timeout plugin."""
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


def artifact(
    *,
    index: int,
    kind: str = "figure",
    session_id: str = "s1",
) -> ArtifactRecord:
    """One artifact record with a distinct id and increasing timestamp."""
    return ArtifactRecord(
        id=f"art_{index:04d}",
        session_id=session_id,
        kind=kind,
        path=f"/ws/results/{index}.png",
        title=f"figure {index}",
        created_at=float(index),
    )


def records(*indexes: int) -> list[ArtifactRecord]:
    """Newest-first records for the given indexes."""
    return [artifact(index=i) for i in reversed(indexes)]


# ---- the checkpoint --------------------------------------------------------


def test_a_fresh_checkpoint_baselines_without_triggering():
    checkpoint = ShadowReviewCheckpoint("s1")
    assert checkpoint.evaluate(records(1, 2)) == []


def test_artifacts_after_the_baseline_accumulate_to_the_threshold():
    checkpoint = ShadowReviewCheckpoint("s1")
    assert checkpoint.evaluate(records(1)) == []  # baseline
    assert checkpoint.evaluate(records(1, 2, 3)) == []  # three seen, two new
    assert SHADOW_REVIEW_ARTIFACT_THRESHOLD == 3
    fired = checkpoint.evaluate(records(1, 2, 3, 4))
    assert [f.id for f in fired] == ["art_0004", "art_0003", "art_0002"]


def test_a_trigger_resets_the_window():
    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1))
    fired = checkpoint.evaluate(records(1, 2, 3, 4))
    assert len(fired) == 3
    assert checkpoint.evaluate(records(1, 2, 3, 4, 5)) == []


def test_the_threshold_is_exactly_three():
    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1))
    assert checkpoint.evaluate(records(1, 2, 3)) == []  # two new: quiet
    assert checkpoint.evaluate(records(1, 2, 3, 4))  # three new: fires


def test_a_structured_artifact_triggers_immediately():
    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1))
    fired = checkpoint.evaluate(
        [artifact(index=2, kind="report")] + records(1)
    )
    assert [f.kind for f in fired] == ["report"]
    assert STRUCTURED_ARTIFACT_KINDS == frozenset({"report", "table"})


def test_a_structured_artifact_fires_with_everything_batched_so_far():
    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1))
    checkpoint.evaluate(records(1, 2))  # one new, batched
    fired = checkpoint.evaluate([artifact(index=3, kind="table")] + records(1, 2))
    assert [f.id for f in fired] == ["art_0003", "art_0002"]


def test_records_at_or_before_the_watermark_do_not_trigger():
    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1, 2))
    assert checkpoint.evaluate(records(1, 2)) == []


def test_an_empty_store_reading_is_quiet():
    checkpoint = ShadowReviewCheckpoint("s1")
    assert checkpoint.evaluate([]) == []


# ---- the verdict -----------------------------------------------------------


def test_parse_verdict_reads_the_three_words_and_defaults_to_pass():
    assert parse_verdict("VERDICT: PASS\nfine").verdict == "pass"
    assert parse_verdict("VERDICT: WARN\nrecheck\nthis").verdict == "warn"
    assert parse_verdict("VERDICT: FAIL\nredo it").verdict == "fail"
    assert parse_verdict("hello").verdict == "pass"
    assert parse_verdict("").verdict == "pass"
    assert parse_verdict("VERDICT: PASS").note == ""
    assert parse_verdict("VERDICT: FAIL\nredo it").note == "redo it"


def test_parse_verdict_caps_the_note():
    verdict = parse_verdict("VERDICT: WARN\n" + "x" * 5000)
    assert len(verdict.note) == MAX_REVIEW_NOTE_CHARS


# ---- the review task -------------------------------------------------------


def _fake_review(verdict):
    async def review(session_id, records, history):
        return verdict

    return review


def test_a_pass_verdict_is_quiet():
    published = []
    review = ShadowReview(object(), review_call=_fake_review(ShadowReviewVerdict("pass")))

    async def drive():
        task = review.fire("s1", records(1), publisher=published.append)
        await task
        assert published == []
        assert review.nudge_for("s1") is not None

    run(drive())
    assert run(review.nudge_for("s1").augment(())) == ()


def test_a_warn_verdict_leaves_a_one_shot_note_and_publishes():
    published = []
    review = ShadowReview(
        object(),
        review_call=_fake_review(ShadowReviewVerdict("warn", "recheck the p-value")),
    )

    async def drive():
        task = review.fire("s1", records(1), publisher=published.append)
        await task
        assert [v.verdict for v in published] == ["warn"]

    run(drive())
    nudge = review.nudge_for("s1")
    delivered = run(nudge.augment(()))
    assert len(delivered) == 1
    assert delivered[0].role is Role.USER
    assert "warn" in delivered[0].content
    assert "recheck the p-value" in delivered[0].content
    # One-shot: the next turn gets nothing.
    assert run(nudge.augment(())) == ()


def test_a_failed_review_is_silent_and_never_blocks():
    async def exploding(session_id, records, history):
        raise RuntimeError("no reviewer today")

    published = []
    review = ShadowReview(object(), review_call=exploding)

    async def drive():
        task = review.fire("s1", records(1), publisher=published.append)
        await task  # completes, does not raise
        assert published == []

    run(drive())
    assert run(review.nudge_for("s1").augment(())) == ()


def test_evaluate_without_a_bound_store_is_empty():
    review = ShadowReview(object(), review_call=_fake_review(ShadowReviewVerdict("pass")))
    assert review.evaluate("s1") == []


# ---- the nudge -------------------------------------------------------------


def test_the_nudge_delivers_only_the_session_named():
    notes = {"s1": "note for s1"}
    nudge = ShadowReviewNudge(notes, "s1")
    delivered = run(nudge.augment(()))
    assert len(delivered) == 1
    assert delivered[0].content == "note for s1"
    assert "s1" not in notes
    other = ShadowReviewNudge(notes, "s2")
    assert run(other.augment(())) == ()


# ---- the frame -------------------------------------------------------------


def test_the_frame_projects_to_the_wire_and_the_desktop_name():
    event = TurnEvent.shadow_review_verdict(
        "fail", "the table contradicts the report", session_id="s1", turn_id="t1"
    )
    wire = to_wire(event)
    assert wire["type"] == "shadow_review"
    assert wire["verdict"] == "fail"
    assert wire["note"] == "the table contradicts the report"
    assert DESKTOP_CHAT_FRAME_TYPE[TurnEventType.SHADOW_REVIEW] == "shadow_review"
    name, payload = desktop_chat_frame(event)
    assert name == "shadow_review"
    assert payload["verdict"] == "fail"
    assert payload["session_id"] == "s1"


# ---- the off state ---------------------------------------------------------


def test_shadow_review_is_off_by_default():
    assert AppConfig.__dataclass_fields__["shadow_review"].default is False
    assert AppConfig.__dataclass_fields__["shadow_review_model"].default == ""


def test_the_off_state_mounts_no_nudge():
    app = object()
    assert _shadow_review_nudge(app, "s1") is None


def test_build_app_mounts_the_binding_only_when_switched_on(tmp_path):
    off = build_app(AppConfig(workspace=tmp_path))
    assert off.shadow_review is None
    on = build_app(AppConfig(workspace=tmp_path, shadow_review=True))
    assert on.shadow_review is not None
    assert isinstance(on.shadow_review, ShadowReview)


def test_the_verdict_line_accepts_markdown_marks_case_and_blank_lead():
    assert parse_verdict("**VERDICT: WARN**").verdict == "warn"
    assert parse_verdict("**VERDICT: FAIL** redo it").verdict == "fail"
    assert parse_verdict("verdict: fail").verdict == "fail"
    assert parse_verdict("Verdict: Pass").verdict == "pass"
    assert parse_verdict("\n\nVERDICT: WARN\nrecheck").verdict == "warn"
    assert parse_verdict("  VERDICT: PASS").verdict == "pass"


def test_an_unreadable_but_nonempty_answer_is_logged_and_read_as_pass(caplog):
    with caplog.at_level("INFO", logger="omicsclaw.entry.shadow_review"):
        verdict = parse_verdict("the reviewer exploded mid-sentence")
    assert verdict.verdict == "pass"
    assert any("unreadable reviewer answer" in line for line in caplog.text.splitlines())


def test_the_note_is_stripped_of_control_characters():
    # ESC and BEL are control characters; the printable "[8m" of the ANSI
    # sequence is not and survives — the strip removes only what the main
    # model should never be shown.
    verdict = parse_verdict("VERDICT: WARN\nwatch \x1b[8mthis\x07.")
    assert verdict.note == "watch [8mthis."
    assert "\x1b" not in verdict.note and "\x07" not in verdict.note
    verdict = parse_verdict("VERDICT: WARN\nclean\nnote")
    assert verdict.note == "cleannote"


def test_a_full_window_of_new_rows_fires_the_count_trigger():
    # The store reads at most _REVIEW_LOOKBACK newest rows. When all of
    # them are new, at least one more batch fell off the window's older
    # edge — the checkpoint fires rather than let the watermark jump past
    # rows that never counted, and the fired batch is everything it saw.
    from omicsclaw.entry.shadow_review import _REVIEW_LOOKBACK

    checkpoint = ShadowReviewCheckpoint("s1")
    checkpoint.evaluate(records(1))
    window = [artifact(index=index) for index in range(2, _REVIEW_LOOKBACK + 2)]
    fired = checkpoint.evaluate(list(reversed(window)))
    assert len(fired) == _REVIEW_LOOKBACK
    # The oldest row of the window is in the batch: nothing was skipped.
    assert fired[-1].id == window[0].id
    assert checkpoint.evaluate(records(1, 2)) == []


def test_the_review_prompt_is_bounded_by_construction():
    from omicsclaw.entry.shadow_review import _review_prompt
    from omicsclaw.schema import Message

    bulky = [
        artifact(index=index, kind="report", session_id="s1")
        for index in range(60)
    ]
    history = [
        Message(role=Role.ASSISTANT, content="z" * 20_000),
    ]
    prompt = _review_prompt("s1", bulky, history)
    assert len(prompt.encode("utf-8")) < 16_000
    assert len(history[0].content) > len(prompt)


def test_an_unbound_binding_changes_no_stream_bytes(tmp_path):
    # The detector's only observable is the artifact store; without one
    # it answers [] and the stream vocabulary is untouched by default.
    app = build_app(AppConfig(workspace=tmp_path))
    assert app.shadow_review is None
    probe = TurnEvent.shadow_review_verdict("warn", "x")
    assert probe.type.value == "shadow_review"
    assert to_wire(probe)["type"] == "shadow_review"
