import asyncio
import threading

import pytest

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis
from omicsclaw.kernel import PersistentKernelManager
from omicsclaw.kernel.session import CellResult
from omicsclaw.memory.database import Database


class Kernel:
    def __init__(self):
        self.codes = []
        self.interrupts = 0

    def snapshot(self):
        return []

    async def execute(self, session_id, code, **kwargs):
        self.codes.append(code)
        return CellResult(stdout="done")

    async def cancel(self, session_id, **kwargs):
        self.interrupts += 1
        return "idle"


class Process:
    def __init__(self):
        self.codes = []
        self.interrupts = 0

    def is_alive(self):
        return True

    def execute(self, code, **kwargs):
        self.codes.append(code)
        return CellResult(stdout="done")

    def peak_rss_kb(self):
        return None

    def interrupt(self):
        self.interrupts += 1

    def shutdown(self):
        pass


def test_agent_cancel_during_admission_never_dispatches_code(tmp_path):
    async def scenario():
        db = Database(tmp_path / "nb.db")
        kernel = Kernel()
        service = NotebookAnalysis(db, kernel)
        service.bind("n", "s")
        entered, release = threading.Event(), threading.Event()
        transaction = service._transaction

        def blocked(work):
            entered.set()
            assert release.wait(5)
            return transaction(work)

        service._transaction = blocked
        task = asyncio.create_task(service.run_agent("s", "print(1)", "cmd"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.gather(*tuple(service.admissions))
            await asyncio.gather(*tuple(service.tasks.values()))
            assert kernel.codes == []
            assert (await service.snapshot("n"))["executions"][0][
                "status"
            ] == "interrupted"
        finally:
            release.set()
            await service.close()
            db.close()

    asyncio.run(scenario())


def test_cancel_while_kernel_starts_never_dispatches_source(tmp_path):
    async def scenario():
        db = Database(tmp_path / "nb.db")
        manager = PersistentKernelManager(tmp_path)
        manager._ensure_reaper = lambda: None
        service = NotebookAnalysis(db, manager)
        entered, release = asyncio.Event(), asyncio.Event()
        process = Process()

        async def start(state, **kwargs):
            entered.set()
            await release.wait()
            state.kernel = process

        manager._start_locked = start
        cell = await service.save("n", "c", "print(1)", 0)
        record = await service.execute("n", "c", "cmd", cell["revision"])
        try:
            await entered.wait()
            cancellation = asyncio.create_task(
                service.cancel("n", record["execution_id"])
            )
            while record["execution_id"] not in service.cancelling:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            assert (await cancellation)["status"] == "interrupted"
            assert process.codes == []
        finally:
            release.set()
            await service.close()
            await manager.aclose()
            db.close()

    asyncio.run(scenario())


def test_cancel_waiting_notebook_does_not_interrupt_other_execution(tmp_path):
    async def scenario():
        db = Database(tmp_path / "nb.db")
        manager = PersistentKernelManager(tmp_path)
        manager._ensure_reaper = lambda: None
        service = NotebookAnalysis(db, manager)
        state = manager.state_for("notebook:n")
        process = Process()
        state.kernel = process
        state.current_job = "other-job"
        state.executing = True
        await state.lock.acquire()
        cell = await service.save("n", "c", "print(1)", 0)
        record = await service.execute("n", "c", "cmd", cell["revision"])
        try:
            while (
                await service.execution("n", record["execution_id"])
            ).status != "running":
                await asyncio.sleep(0)
            cancellation = asyncio.create_task(
                service.cancel("n", record["execution_id"])
            )
            while record["execution_id"] not in service.cancelling:
                await asyncio.sleep(0)
            await asyncio.sleep(0.02)
            state.executing = False
            state.current_job = ""
            state.lock.release()
            assert (await cancellation)["status"] == "interrupted"
            assert process.codes == []
            assert process.interrupts == 0
        finally:
            if state.lock.locked():
                state.lock.release()
            await service.close()
            await manager.aclose()
            db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("language", "location", "expected"),
    [
        ("python", "local", "s"),
        ("r", "local", "notebook:n"),
        ("python", "remote:host", "notebook:n"),
    ],
)
def test_binding_only_adopts_local_python_kernel(
    tmp_path, language, location, expected
):
    db = Database(tmp_path / "nb.db")
    kernel = Kernel()
    kernel.snapshot = lambda: [
        {"session_id": "s", "alive": True, "language": language, "location": location}
    ]
    service = NotebookAnalysis(db, kernel)
    try:
        assert service.bind("n", "s")["kernel_session_id"] == expected
    finally:
        db.close()


def test_scoped_cancel_stops_when_original_execution_finishes(tmp_path):
    async def scenario():
        manager = PersistentKernelManager(tmp_path)
        state = manager.state_for("s")
        process = Process()
        state.kernel = process
        state.current_job = "original"
        state.executing = True
        remote = manager.state_for("s", location="remote:host")
        remote.kernel = Process()
        remote.current_job = "other"
        remote.executing = True

        def interrupt():
            process.interrupts += 1
            state.current_job = "next"

        process.interrupt = interrupt
        try:
            assert (
                await manager.cancel(
                    "s", job_id="original", language="python", location="local"
                )
                == "interrupted"
            )
            assert process.codes == []
            assert remote.kernel.interrupts == 0
        finally:
            await manager.aclose()

    asyncio.run(scenario())
