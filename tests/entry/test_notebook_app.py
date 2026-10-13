import asyncio

from omicsclaw.entry.events import TurnEventType
from omicsclaw.entry.notebook_app import execute_cell_live
from omicsclaw.entry.stream import TurnStream


class FakeBinding:
    async def run_python(self, code, **kwargs):
        kwargs["on_output"]("untrusted kernel output")
        return "result"


def test_live_app_output_is_reinjected_as_progress_data():
    async def scenario():
        stream = TurnStream("session", "turn")
        result = await execute_cell_live(
            FakeBinding(),
            stream,
            "print('x')",
            description="run cell",
            session_id="session",
            notebook_id="nb",
            cell_id="cell",
            client_command_id="cmd",
        )
        await asyncio.sleep(0)
        assert result == "result"
        events = list(stream._ring)
        assert [event.type for event in events] == [
            TurnEventType.PROGRESS,
            TurnEventType.PROGRESS,
        ]
        assert events[0].progress is not None
        assert events[0].progress.message == "untrusted kernel output"
        assert events[1].progress is not None
        assert events[1].progress.message == "cell_completed"

    asyncio.run(scenario())
