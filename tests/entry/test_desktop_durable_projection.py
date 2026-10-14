"""The existing SSE projector must render persisted events with stable subframe identity."""

import asyncio
import json
from omicsclaw.entry.desktop.turn_observation import (
    desktop_chat_frame,
    DesktopChatSSEBody,
)
from omicsclaw.entry.events import TurnEvent, TurnEventType


def test_persisted_text_and_progress_keep_original_projection():
    text = TurnEvent(
        type=TurnEventType.TEXT_DELTA,
        seq=3,
        turn_id="t",
        session_id="s",
        wire_payload={"delta": "hello"},
    )
    assert desktop_chat_frame(text) == ("text", "hello")
    progress = TurnEvent(
        type=TurnEventType.PROGRESS,
        seq=4,
        session_id="s",
        turn_id="t",
        wire_payload={"message": "reading", "tool_name": "read"},
    )
    assert desktop_chat_frame(progress) == ("tool_output", "reading")


def test_one_durable_terminal_event_has_distinct_stable_subframes():
    class Observation:
        def __init__(self):
            self.done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return TurnEvent(
                type=TurnEventType.EXCHANGE_END,
                seq=5,
                session_id="s",
                turn_id="t",
                terminal="converged",
            )

        async def aclose(self):
            pass

    async def run():
        body = DesktopChatSSEBody(Observation(), keepalive_s=None)
        frames = []
        async with body:
            async for frame in body:
                frames.append(
                    (
                        body.last_seq,
                        body.last_subframe,
                        json.loads(frame.split("data: ", 1)[1])["type"],
                    )
                )
        return frames

    assert asyncio.run(run()) == [(5, 0, "result"), (5, 1, "done")]
    assert asyncio.run(run()) == [(5, 0, "result"), (5, 1, "done")]
