import asyncio
import json

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis
from omicsclaw.memory.database import Database
from tests.entry.test_notebook_cancellation import Kernel


def test_agent_reads_durable_terminal_when_admission_observer_resumes_late(tmp_path):
    async def scenario():
        db = Database(tmp_path / "nb.db")
        kernel = Kernel()
        service = NotebookAnalysis(db, kernel)
        service.bind("n", "s")
        execute = service.execute

        async def delayed_observer(*args, **kwargs):
            admitted = await execute(*args, **kwargs)
            task = service.tasks.get(admitted["execution_id"])
            if task is not None:
                await task
            await asyncio.sleep(0)
            assert admitted["status"] == "queued"
            assert admitted["execution_id"] not in service.tasks
            return admitted

        service.execute = delayed_observer
        try:
            result = json.loads(await service.run_agent("s", "print(1)", "command"))
            assert result["status"] == "completed"
            assert result["output"]["stdout"] == "done"
            assert kernel.codes == ["print(1)"]
        finally:
            await service.close()
            db.close()

    asyncio.run(scenario())


def test_agent_never_returns_success_for_ownerless_pending_flush(tmp_path):
    import hashlib
    import time
    from dataclasses import asdict

    import pytest

    from omicsclaw.entry.desktop.notebooks_manager import NotebookError
    from omicsclaw.memory.notebook import ExecutionRecord

    async def scenario():
        db = Database(tmp_path / "pending.db")
        kernel = Kernel()
        service = NotebookAnalysis(db, kernel)
        service.bind("n", "s")

        async def remote_admission(nid, cid, command, expected, **kwargs):
            eid = hashlib.sha256(json.dumps([nid, command]).encode()).hexdigest()
            record = ExecutionRecord(
                eid,
                nid,
                cid,
                status="completed",
                output={"stdout": "not yet durable", "event_stream_pending": True},
            )
            await service.store.record_execution(record)
            db.run(
                lambda c: c.execute(
                    "INSERT INTO notebook_owners VALUES(?,?,?)",
                    (nid, "another-process", time.time() + 60),
                )
            )
            return asdict(record)

        service.execute = remote_admission
        try:
            with pytest.raises(NotebookError, match="execution_owner_unavailable"):
                await service.run_agent("s", "print(1)", "command")
            assert kernel.codes == []
        finally:
            await service.close()
            db.close()

    asyncio.run(scenario())
