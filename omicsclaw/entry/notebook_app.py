"""Notebook live-app bridge: execute cells and feed safe progress into TurnStream."""

from __future__ import annotations

import asyncio
from typing import Any

from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.stream import TurnStream
from omicsclaw.tools.context import ProgressUpdate


async def execute_cell_live(
    binding: Any,
    stream: TurnStream,
    code: str,
    *,
    description: str,
    session_id: str,
    notebook_id: str,
    cell_id: str,
    client_command_id: str = "",
    session_epoch: int = 0,
    timeout_s: float | None = None,
) -> str:
    def on_output(text: str) -> None:
        update = ProgressUpdate(tool_name="python", message=text[-2000:])
        stream.publish_threadsafe(
            TurnEvent.progress_update(
                update, session_id=session_id, turn_id=stream.turn_id
            )
        )

    result = await binding.run_python(
        code,
        description=description,
        session_id=session_id,
        on_output=on_output,
        timeout_s=timeout_s,
        notebook_id=notebook_id,
        cell_id=cell_id,
        client_command_id=client_command_id,
        session_epoch=session_epoch,
    )
    # Callbacks may arrive from a worker thread; let queued loop callbacks land
    # before the completion marker so replay preserves output-before-complete.
    await asyncio.sleep(0)
    stream.publish(
        TurnEvent.progress_update(
            ProgressUpdate(tool_name="python", message="cell_completed"),
            session_id=session_id,
            turn_id=stream.turn_id,
        )
    )
    return result
