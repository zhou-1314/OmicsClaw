import asyncio
import threading

import pytest

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis
from omicsclaw.kernel import PersistentKernelManager
from omicsclaw.memory.database import Database
from tests.entry.test_notebook_cancellation import Kernel, Process


@pytest.mark.parametrize("identity", ["job_id", "execution_token"])
def test_review_scoped_interrupt_rechecks_owner_at_signal_dispatch(
    tmp_path, monkeypatch, identity
):
    async def scenario():
        manager = PersistentKernelManager(tmp_path)
        manager._ensure_reaper = lambda: None
        state = manager.state_for("s")
        process = Process()
        state.kernel = process
        first_entered = threading.Event()
        first_release = threading.Event()
        next_entered = threading.Event()
        next_release = threading.Event()
        entered, release = asyncio.Event(), asyncio.Event()
        original_to_thread = asyncio.to_thread
        interrupted_jobs = []
        real_execute = process.execute

        def execute(code, **kwargs):
            if code == "first":
                first_entered.set()
                assert first_release.wait(5)
            if code == "next":
                next_entered.set()
                assert next_release.wait(5)
            return real_execute(code, **kwargs)

        def interrupt():
            interrupted_jobs.append(
                state.current_job
                if identity == "job_id"
                else state.current_execution_token
            )

        process.execute = execute
        process.interrupt = interrupt

        async def delayed(function, *args, **kwargs):
            if function is interrupt:
                entered.set()
                await release.wait()
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", delayed)
        first = asyncio.create_task(
            manager.execute("s", "first", **{identity: "original"})
        )
        tasks = [first]
        try:
            assert await original_to_thread(first_entered.wait, 3)
            cancellation = asyncio.create_task(
                manager.cancel(
                    "s", **{identity: "original"}, language="python", location="local"
                )
            )
            tasks.append(cancellation)
            await asyncio.wait_for(entered.wait(), 3)
            first_release.set()
            following = asyncio.create_task(
                manager.execute("s", "next", **{identity: "next"})
            )
            tasks.append(following)
            await original_to_thread(next_entered.wait, 0.1)
            release.set()
            await asyncio.wait_for(cancellation, 3)
            next_release.set()
            await asyncio.gather(first, following)
            assert "next" not in interrupted_jobs
        finally:
            first_release.set()
            next_release.set()
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            monkeypatch.setattr(asyncio, "to_thread", original_to_thread)
            await manager.aclose()

    asyncio.run(scenario())


def test_review_repeated_agent_cancel_cannot_release_undispatched_admission(tmp_path):
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
            while not service.cancelling:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.gather(*tuple(service.admissions), return_exceptions=True)
            await asyncio.gather(*tuple(service.tasks.values()), return_exceptions=True)
            assert kernel.codes == []
        finally:
            release.set()
            await service.close()
            db.close()

    asyncio.run(scenario())


def test_review_cancelled_interrupt_caller_keeps_scope_until_worker_finishes(tmp_path):
    async def scenario():
        manager = PersistentKernelManager(tmp_path)
        manager._ensure_reaper = lambda: None
        state = manager.state_for("s")
        process = Process()
        state.kernel = process
        first_entered, first_release = threading.Event(), threading.Event()
        next_entered, next_release = threading.Event(), threading.Event()
        signal_entered, signal_release = threading.Event(), threading.Event()
        interrupted_jobs = []
        real_execute = process.execute

        def execute(code, **kwargs):
            entered, release = (
                (first_entered, first_release)
                if code == "first"
                else (next_entered, next_release)
            )
            entered.set()
            assert release.wait(5)
            return real_execute(code, **kwargs)

        def interrupt():
            signal_entered.set()
            assert signal_release.wait(5)
            interrupted_jobs.append(state.current_execution_token)

        process.execute = execute
        process.interrupt = interrupt
        first = asyncio.create_task(
            manager.execute("s", "first", execution_token="original")
        )
        tasks = [first]
        try:
            assert await asyncio.to_thread(first_entered.wait, 3)
            cancellation = asyncio.create_task(
                manager.cancel(
                    "s", execution_token="original", language="python", location="local"
                )
            )
            tasks.append(cancellation)
            assert await asyncio.to_thread(signal_entered.wait, 3)
            cancellation.cancel()
            await asyncio.sleep(0)
            first_release.set()
            following = asyncio.create_task(
                manager.execute("s", "next", execution_token="next")
            )
            tasks.append(following)
            await asyncio.to_thread(next_entered.wait, 0.1)
            signal_release.set()
            next_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert "next" not in interrupted_jobs
        finally:
            first_release.set()
            next_release.set()
            signal_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.aclose()

    asyncio.run(scenario())


def test_review_notebook_execution_token_does_not_claim_artifact_job_id(
    tmp_path, monkeypatch
):
    from omicsclaw.kernel import manager as module

    async def scenario():
        registered = []

        class FigureProcess(Process):
            def __init__(self, session_id, **kwargs):
                super().__init__()
                self.on_figure = kwargs["on_figure"]

            def start(self):
                pass

            def execute(self, code, **kwargs):
                self.on_figure(tmp_path / "plot.png", "image/png")
                return super().execute(code, **kwargs)

        monkeypatch.setattr(module, "SessionKernel", FigureProcess)
        manager = PersistentKernelManager(
            tmp_path,
            on_figure=lambda path, mime, session, job: registered.append(
                (session, job)
            ),
        )
        manager._ensure_reaper = lambda: None
        db = Database(tmp_path / "nb.db")
        service = NotebookAnalysis(db, manager)
        try:
            cell = await service.save("n", "c", "plot()", 0)
            result = await service.execute("n", "c", "cmd", cell["revision"])
            await service.tasks[result["execution_id"]]
            assert registered == [("notebook:n", "")]
        finally:
            await service.close()
            await manager.aclose()
            db.close()

    asyncio.run(scenario())


def test_review_cancel_during_kill_finishes_reset_cleanup(tmp_path, monkeypatch):
    from omicsclaw.kernel import manager as module

    async def scenario():
        monkeypatch.setattr(module, "CANCEL_GRACE_S", 0)
        manager = PersistentKernelManager(tmp_path)
        manager._ensure_reaper = lambda: None
        state = manager.state_for("s")
        process = Process()
        state.kernel = process
        execute_entered, execute_release = threading.Event(), threading.Event()
        kill_entered, kill_release = threading.Event(), threading.Event()
        real_execute = process.execute
        kills = []

        def execute(code, **kwargs):
            execute_entered.set()
            assert execute_release.wait(5)
            return real_execute(code, **kwargs)

        def kill():
            kills.append(state.current_execution_token)
            if len(kills) == 1:
                kill_entered.set()
                assert kill_release.wait(5)
                execute_release.set()

        process.execute = execute
        process.kill = kill
        running = asyncio.create_task(
            manager.execute("s", "first", execution_token="original")
        )
        cancellation = None
        try:
            assert await asyncio.to_thread(execute_entered.wait, 3)
            cancellation = asyncio.create_task(
                manager.cancel("s", execution_token="original")
            )
            assert await asyncio.to_thread(kill_entered.wait, 3)
            cancellation.cancel()
            await asyncio.sleep(0)
            cancellation.cancel()
            await asyncio.sleep(0)
            assert state.resetting
            kill_release.set()
            with pytest.raises(asyncio.CancelledError):
                await cancellation
            await running
            assert state.resetting is False
            assert state.kernel is None
            assert state.current_execution_token == ""
        finally:
            execute_release.set()
            kill_release.set()
            await asyncio.gather(
                running,
                *([cancellation] if cancellation else []),
                return_exceptions=True,
            )
            await manager.aclose()

    asyncio.run(scenario())
