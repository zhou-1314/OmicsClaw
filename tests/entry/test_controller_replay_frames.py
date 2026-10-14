"""Persisted authority survives lost projections without synthetic terminals."""

import asyncio
from types import SimpleNamespace
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog, DurableTurnEvent
from omicsclaw.entry.desktop.event_service import replay_frames
from omicsclaw.entry.events import TurnEventType


def test_approval_live_and_rehydrated_projection_keep_flags_and_canonical_bytes():
    from omicsclaw.entry.events import TurnEvent
    from omicsclaw.entry.render import to_wire
    from omicsclaw.entry.stream import event_from_durable
    from omicsclaw.entry.desktop.turn_observation import desktop_chat_frame
    from omicsclaw.entry.desktop._chat_sse import render_chat_sse_frame
    from omicsclaw.tools import ApprovalRequest

    request = ApprovalRequest(
        "write_file",
        '{"path":"proof.txt"}',
        reason_shows_call=True,
        ask_every_time=True,
    )
    live = TurnEvent.approval_required(
        request, "t#1", seq=3, session_id="s", turn_id="t"
    )
    restored = event_from_durable(
        DurableTurnEvent(
            '["s","t"]',
            "s",
            "t",
            3,
            TurnEventType.APPROVAL_REQUIRED.value,
            to_wire(live),
            0,
        )
    )
    assert render_chat_sse_frame(
        *desktop_chat_frame(live, can_remember=True), event_id=3
    ) == render_chat_sse_frame(
        *desktop_chat_frame(restored, can_remember=True), event_id=3
    )


def test_durable_replay_rebuilds_text_and_partial_terminal_subframe(tmp_path):
    database = Database(tmp_path / "memory.db")
    try:
        log = DurableTurnEventLog(database)
        stream = '["s","t"]'
        log.append(
            DurableTurnEvent(
                stream,
                "s",
                "t",
                1,
                TurnEventType.TEXT_DELTA.value,
                {"delta": "hello"},
                0,
            )
        )
        log.append(
            DurableTurnEvent(
                stream,
                "s",
                "t",
                2,
                TurnEventType.EXCHANGE_END.value,
                {"terminal": "converged"},
                0,
            )
        )
        app = SimpleNamespace(
            memory=SimpleNamespace(database=database),
            config=SimpleNamespace(provider="mac", model="m"),
            resolved_model="m",
        )
        result = asyncio.run(replay_frames(app, "s", "t", epoch="new", limit=64))
        assert [(frame["cursor"], frame["subframe"]) for frame in result["frames"]] == [
            (1, 0),
            (2, 0),
            (2, 1),
        ]
        assert "hello" in result["frames"][0]["frame"]
        resumed = asyncio.run(
            replay_frames(app, "s", "t", after_seq=2, after_subframe=0, epoch="new")
        )
        assert len(resumed["frames"]) == 1 and '"done"' in resumed["frames"][0]["frame"]
        assert not resumed["hasMore"]
    finally:
        database.close()


def test_a_nonterminal_read_page_never_fabricates_done(tmp_path):
    database = Database(tmp_path / "memory.db")
    try:
        log = DurableTurnEventLog(database)
        log.append(
            DurableTurnEvent(
                '["s","t"]',
                "s",
                "t",
                1,
                TurnEventType.TEXT_DELTA.value,
                {"delta": "partial"},
                0,
            )
        )
        app = SimpleNamespace(
            memory=SimpleNamespace(database=database),
            config=SimpleNamespace(provider="mac", model="m"),
            resolved_model="m",
        )
        result = asyncio.run(replay_frames(app, "s", "t"))
        assert len(result["frames"]) == 1 and "partial" in result["frames"][0]["frame"]
        assert all('"done"' not in frame["frame"] for frame in result["frames"])
    finally:
        database.close()
