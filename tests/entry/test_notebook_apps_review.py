import asyncio

import pytest

from omicsclaw.entry.desktop.notebooks_manager import NotebookError
from omicsclaw.memory.notebook import ExecutionRecord
from tests.entry import test_notebook_apps as fixtures

analysis = fixtures.analysis


def test_hosting_waits_until_completed_execution_output_is_durable(analysis):
    async def scenario():
        await analysis.store.record_execution(
            ExecutionRecord(
                execution_id="pending-flush",
                notebook_id="n",
                cell_id="c",
                status="completed",
                output={"event_stream_pending": True},
            )
        )
        with pytest.raises(NotebookError, match="execution_not_completed"):
            await analysis.apps.publish(
                "n", fixtures.document(execution_id="pending-flush")
            )
        assert await analysis.apps.read("n") is None

    asyncio.run(scenario())


def test_app_update_during_state_publication_keeps_old_frame_from_writing(analysis):
    async def scenario():
        await analysis.apps.publish("n", fixtures.document())
        read = analysis.apps.read

        async def update_after_read(nid, *, metadata=False):
            value = await read(nid, metadata=metadata)
            await analysis.apps.publish(
                "n", fixtures.document(expected_revision=1, title="Updated")
            )
            return value

        analysis.apps.read = update_after_read
        with pytest.raises(NotebookError, match="app_revision_conflict"):
            await analysis.apps.publish_state("n", fixtures.state())
        assert (await analysis.context.snapshot("n"))["live_state"] is None
        assert (await read("n"))["revision"] == 2

    asyncio.run(scenario())
