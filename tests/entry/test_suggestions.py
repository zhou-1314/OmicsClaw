"""P6 item 2: the follow-up suggestion block, from deltas to frames.

The splitter is the piece this phase adds, and its whole value is the
boundary: the sentinel arriving split across deltas, a held-back tail
that turned out to be answer text, an answer with no block at all.
The kernel test underneath runs the deployed composition root with only
the model backend faked, so what it asserts is the published stream a
surface actually receives.
"""

from __future__ import annotations

import asyncio

from omicsclaw.entry.assembly import FOLLOW_UP_RULES, default_sections
from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.stream import TurnStream
from omicsclaw.entry.suggestions import (
    FOLLOW_UP_SENTINEL,
    SUGGESTION_LIMIT,
    SuggestionSplitter,
    parse_suggestion_block,
    split_suggestions,
    strip_suggestions,
)
from omicsclaw.entry.turn import TurnRunner
from omicsclaw.schema import Message

from tests.entry.test_turn_runner import Scripted, make_app

WAIT_S = 5.0

_BLOCK = "What resolution?\nShow the heatmap?\nList marker genes?"


# ---- the pure splitter ------------------------------------------------------


def test_text_without_the_sentinel_passes_through_untouched() -> None:
    splitter = SuggestionSplitter()
    shown = [splitter.feed(piece) for piece in ("An answer ", "with no block.", " None here.")]
    assert "".join(shown) == "An answer with no block. None here."


def test_a_sentinel_split_across_deltas_is_never_shown() -> None:
    """The case the splitter exists for: the marker arrives mid-delta.

    Every byte of it must be held back until it can be ruled out as the
    sentinel, and once matched, the questions behind it are captured
    rather than shown.
    """
    splitter = SuggestionSplitter()
    shown = splitter.feed("Clustering found 12 domains.\n[[FOL")
    assert shown == "Clustering found 12 domains.\n"
    shown += splitter.feed("LOW-UPS]]\nWhat resolution?\nShow the heatmap?")
    assert shown == "Clustering found 12 domains.\n"
    tail, questions = splitter.close()
    assert tail == ""
    assert questions == ("What resolution?", "Show the heatmap?")


def test_a_tail_that_never_becomes_the_sentinel_is_answer_text() -> None:
    """``[[F`` held back, then the answer ends: those bytes are prose."""
    splitter = SuggestionSplitter()
    shown = splitter.feed("See the matrix [[F")
    assert shown == "See the matrix "
    tail, questions = splitter.close()
    assert tail == "[[F"
    assert questions == ()


def test_close_resets_the_splitter_for_the_next_model_call() -> None:
    """A multi-round exchange narrates between tool calls; only the call
    that ended with the block yields questions."""
    splitter = SuggestionSplitter()
    assert splitter.feed(f"Narration before a tool call. {FOLLOW_UP_SENTINEL}\nq1\nq2") == (
        "Narration before a tool call. "
    )
    _, questions = splitter.close()
    assert questions == ("q1", "q2")
    # The next call's text is matched fresh.
    assert splitter.feed("Done.") == "Done."
    assert splitter.close() == ("", ())


def test_the_block_is_capped_at_three_questions() -> None:
    splitter = SuggestionSplitter()
    splitter.feed(f"{FOLLOW_UP_SENTINEL}\nq1\nq2\nq3\nq4\nq5")
    _, questions = splitter.close()
    assert questions == ("q1", "q2", "q3")
    assert SUGGESTION_LIMIT == 3


def test_numbered_and_bulleted_lines_are_tolerated() -> None:
    assert parse_suggestion_block("1. q1\n2) q2\n- q3\n* q4") == ("q1", "q2", "q3")


def test_blank_and_repeated_lines_are_dropped() -> None:
    assert parse_suggestion_block("q1\n\n   \nq1\nQ1\nq2") == ("q1", "q2")


def test_split_and_strip_read_whole_text() -> None:
    text = f"Answer.\n{FOLLOW_UP_SENTINEL}\nq1\nq2"
    assert split_suggestions(text) == ("Answer.", ("q1", "q2"))
    assert strip_suggestions(text) == "Answer."
    assert strip_suggestions("no block") == "no block"


def test_a_runaway_block_cannot_grow_without_bound() -> None:
    splitter = SuggestionSplitter()
    splitter.feed(FOLLOW_UP_SENTINEL)
    for _ in range(400):
        splitter.feed("x" * 100 + "\n")
    # Must neither raise nor hang; the cap keeps the buffer bounded and
    # the parser still answers with at most three short lines.
    _, questions = splitter.close()
    assert len(questions) <= 3
    assert all(len(question) <= 101 for question in questions)


# ---- the prompt rule --------------------------------------------------------


def test_the_prompt_teaches_the_sentinel_and_the_exactly_three_rule() -> None:
    assert FOLLOW_UP_SENTINEL in FOLLOW_UP_RULES
    assert "exactly three" in FOLLOW_UP_RULES
    assert "Omit" in FOLLOW_UP_RULES, "the block must stay optional"


def test_the_rule_is_a_static_section_beside_the_other_guidance(tmp_path) -> None:
    from tests.entry.test_assembly import _config

    keys = tuple(section.key for section in default_sections(_config(tmp_path)))
    assert keys.index("tools") < keys.index("follow-ups") < keys.index("planning")


# ---- the kernel, through the real composition root --------------------------


def _drive(app):
    async def run():
        stream = TurnStream("s1", "t1")
        runner = TurnRunner(
            app, stream, session_id="s1", turn_id="t1", user_text="go"
        )
        collected: list[TurnEvent] = []
        outcome_task = asyncio.create_task(asyncio.wait_for(runner.run(), WAIT_S))

        async def observe() -> None:
            async with stream.observe() as observation:
                async for event in observation:
                    collected.append(event)
                    if event.type is TurnEventType.EXCHANGE_END:
                        return

        observer = asyncio.create_task(asyncio.wait_for(observe(), WAIT_S))
        outcome = await outcome_task
        await observer
        return outcome, collected

    return asyncio.run(run())


def test_an_answer_with_a_block_publishes_clean_text_and_the_frame(tmp_path):
    reply = Message.assistant(
        "Clustering found 12 domains.\n"
        f"{FOLLOW_UP_SENTINEL}\n"
        "What resolution?\nShow the heatmap?\nList marker genes?\nSave the figure?"
    )
    app = make_app(tmp_path, Scripted(reply))
    outcome, events = _drive(app)

    text = "".join(
        event.engine.delta
        for event in events
        if event.type is TurnEventType.TEXT_DELTA and event.engine is not None
    )
    # The newline the model wrote before the block is answer text; the
    # block itself never reaches a delta.
    assert text == "Clustering found 12 domains.\n"
    assert FOLLOW_UP_SENTINEL not in text

    frames = [event for event in events if event.type is TurnEventType.SUGGESTIONS]
    assert len(frames) == 1
    # Four questions offered; the transport caps at three.
    assert frames[0].suggestions == (
        "What resolution?",
        "Show the heatmap?",
        "List marker genes?",
    )

    # The reply a Channel posts verbatim carries no block either.
    assert outcome.reply == "Clustering found 12 domains."
    # History keeps the model's own bytes — the block included.
    assert FOLLOW_UP_SENTINEL in outcome.result.messages[-1].content


def test_an_answer_without_a_block_publishes_no_frame(tmp_path):
    app = make_app(tmp_path, Scripted(Message.assistant("Just an answer.")))
    outcome, events = _drive(app)

    assert not [e for e in events if e.type is TurnEventType.SUGGESTIONS]
    assert outcome.reply == "Just an answer."


# ---- the wire ---------------------------------------------------------------


def test_the_suggestions_frame_projects_as_an_additive_payload() -> None:
    from omicsclaw.entry.desktop.turn_observation import desktop_chat_frame
    from omicsclaw.entry.render import DESKTOP_CHAT_FRAME_TYPE, to_wire

    event = TurnEvent.suggestions_offered(
        ("q1", "q2", "q3"), seq=5, session_id="s", turn_id="t"
    )
    wire = to_wire(event)
    assert wire["suggestions"] == ["q1", "q2", "q3"]
    assert wire["sequence"] == 5

    name, data = desktop_chat_frame(event)
    assert name == DESKTOP_CHAT_FRAME_TYPE[TurnEventType.SUGGESTIONS]
    assert name == "suggestions"
    assert data["suggestions"] == ["q1", "q2", "q3"]
    assert data["session_id"] == "s"


def test_the_cli_line_lists_the_questions_inertly() -> None:
    from omicsclaw.entry.render import TextRenderer

    line = TextRenderer().feed(
        TurnEvent.suggestions_offered(("q1?", "q2?"), seq=2)
    )
    assert line == "Follow-ups: q1? | q2?"


def test_an_empty_suggestions_frame_renders_no_cli_line() -> None:
    from omicsclaw.entry.render import TextRenderer

    assert (
        TextRenderer().feed(TurnEvent.suggestions_offered((), seq=2)) is None
    )
