"""User edits and runs share the owner transaction and never stop the active cell."""

import asyncio

import pytest

from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_owner import settled, setup
from tests.entry.test_ssh_notebook_binding import Client

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def test_user_queue_preserves_each_source_and_save_uses_compare_and_swap(
    tmp_path, monkeypatch
):
    async def run():
        r, manager, command = await setup(tmp_path, monkeypatch)
        manager.wait = asyncio.Event()
        try:
            first = await r.admit({"command": command})
            async with asyncio.timeout(5):
                while not manager.codes:
                    current = r.views.execution("nb", first["execution_id"])
                    assert current["status"] in {"queued", "running"}, current
                    await asyncio.sleep(0)
            current = r.actions.cell({"notebookId": "nb", "cellId": "cell"})
            second = await r.admit(
                {
                    "command": {
                        **command,
                        "commandId": "second",
                        "source": "print('second')",
                        "expectedRevision": current["revision"],
                    }
                }
            )
            assert manager.codes == [command["source"]]
            assert second["status"] == "queued"
            assert (
                r.actions.receipt({"notebookId": "nb", "commandId": "second"})[
                    "receipt"
                ]["execution_id"]
                == second["execution_id"]
            )
            manager.wait.set()
            old, new = (
                await settled(r, first["execution_id"]),
                await settled(r, second["execution_id"]),
            )
            assert old["provenance"]["code"] == command["source"]
            assert new["provenance"]["code"] == "print('second')"
            assert manager.codes == [command["source"], "print('second')"]
            cell = r.actions.cell({"notebookId": "nb", "cellId": "cell"})
            save = {
                "notebookId": "nb",
                "cellId": "cell",
                "source": "draft = 4",
                "expectedRevision": cell["revision"],
                "kernelInstanceId": command["kernelInstanceId"],
                "generation": command["generation"],
                "commandId": "save",
            }
            saved = await r.save(save)
            assert saved == await r.save(save)
            assert saved["cell"]["source"] == "draft = 4"
            assert saved["control"]["userWatermark"] == 3
            with pytest.raises(NotebookError, match="revision_conflict"):
                await r.save({**save, "commandId": "stale"})
            assert (
                r.actions.receipt({"notebookId": "nb", "commandId": "save"})["receipt"]
                == saved
            )
        finally:
            await r.close()

    asyncio.run(run())


def test_resume_waits_for_user_queue_and_records_new_request_handoff(
    tmp_path, monkeypatch
):
    async def run():
        r, manager, command = await setup(tmp_path, monkeypatch)
        manager.wait = asyncio.Event()
        waiting = None
        try:
            receipt = await r.admit({"command": command})
            binding = SSHNotebookBinding(Client(r))
            waiting = asyncio.create_task(
                binding.notebook_augmentor("chat").prepare_request(
                    (), (), "new-request"
                )
            )
            async with asyncio.timeout(5):
                while not binding.waiters:
                    assert not waiting.done(), waiting.result()
                    await asyncio.sleep(0)
            resume = {
                "notebookId": "nb",
                "commandId": "resume",
                "userWatermark": 1,
                "kernelInstanceId": command["kernelInstanceId"],
                "generation": command["generation"],
            }
            returned = await r.resume(resume)
            assert not returned["continuationPrepared"]
            assert (
                returned["sourceRequestId"]
                == (await r.resume(resume))["sourceRequestId"]
            )
            assert not waiting.done()
            manager.wait.set()
            await settled(r, receipt["execution_id"])
            prepared = await asyncio.wait_for(waiting, 2)
            assert prepared.context.request_id == "new-request"
            updated = r.actions.receipt({"notebookId": "nb", "commandId": "resume"})[
                "receipt"
            ]
            assert updated["continuationPrepared"]
            assert not binding.waiters
        finally:
            if waiting and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await r.close()

    asyncio.run(run())
