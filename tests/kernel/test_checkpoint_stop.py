"""Protected internal saves never turn a deadline or ordinary cancel into reset."""

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

from omicsclaw.kernel import KernelCallbacks, PersistentKernelManager
from omicsclaw.kernel import session as session_module
from omicsclaw.kernel.r_launch import RegisteredRLaunch
from omicsclaw.kernel.session import SessionKernel


def test_protected_deadline_interrupt_preserves_python_object(tmp_path):
    async def run():
        manager = PersistentKernelManager(tmp_path, manual_lifecycle=True)
        try:
            assert (
                await manager.execute("save", "data={'answer':42}; original=id(data)")
            ).status == "ok"
            kernel = manager.state_for("save").kernel
            result = await manager.execute(
                "save",
                "import time; time.sleep(30)",
                timeout_s=0.3,
                retry_on_death=False,
                preserve_on_timeout=True,
            )
            assert result.status == "timeout"
            assert result.usage["stop_confirmed"] is True
            assert result.usage["deadline_exceeded"] is True
            assert manager.state_for("save").kernel is kernel and kernel.is_alive()
            follow = await manager.execute(
                "save", "assert id(data)==original and data['answer']==42"
            )
            assert follow.status == "ok"
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), 60))


@pytest.mark.parametrize("verbose", [False, True])
def test_ignored_interrupt_keeps_ownership_until_real_idle(
    tmp_path, monkeypatch, verbose
):
    monkeypatch.setattr(session_module, "TIMEOUT_INTERRUPT_GRACE_S", 0.1)

    async def run():
        manager = PersistentKernelManager(tmp_path, manual_lifecycle=True)
        stop, began = asyncio.Event(), asyncio.Event()
        release = tmp_path / "release"
        task = queued = None
        loop = asyncio.get_running_loop()
        try:
            await manager.execute("save", "data={'answer':42}; original=id(data)")
            kernel = manager.state_for("save").kernel
            source = f"""import signal, time
from pathlib import Path
previous=signal.signal(signal.SIGINT, signal.SIG_IGN)
print('began',flush=True)
try:
    while not Path({str(release)!r}).exists():
        if {verbose!r}: print('saving',flush=True)
        time.sleep(.02)
finally:
    signal.signal(signal.SIGINT, previous)
"""
            task = asyncio.create_task(
                manager.execute(
                    "save",
                    source,
                    timeout_s=0.4,
                    retry_on_death=False,
                    preserve_on_timeout=True,
                    callbacks=KernelCallbacks(
                        on_stream=lambda name, text: (
                            loop.call_soon_threadsafe(began.set)
                            if "began" in text
                            else None
                        ),
                        on_timeout_pending=lambda elapsed: loop.call_soon_threadsafe(
                            stop.set
                        ),
                    ),
                )
            )
            await asyncio.wait_for(began.wait(), 10)
            await asyncio.wait_for(stop.wait(), 10)
            assert not task.done()
            assert manager.state_for("save").executing and kernel.is_alive()
            assert await manager.cancel("save") == "protected"
            queued = asyncio.create_task(
                manager.execute("save", "assert id(data)==original; data['after']=True")
            )
            await asyncio.sleep(0.05)
            assert not queued.done() and not task.done()
            release.touch()
            result = await asyncio.wait_for(asyncio.shield(task), 15)
            assert result.status == "timeout" and result.usage["stop_confirmed"]
            assert (await asyncio.wait_for(asyncio.shield(queued), 15)).status == "ok"
            assert manager.state_for("save").kernel is kernel and kernel.is_alive()
            assert (
                await manager.execute("save", "assert data=={'answer':42,'after':True}")
            ).status == "ok"
        finally:
            release.touch(exist_ok=True)
            for pending in (task, queued):
                if pending is not None:
                    await asyncio.wait_for(asyncio.shield(pending), 15)
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), 90))


def test_registered_r_protected_deadline_preserves_object(tmp_path):
    library = os.environ.get("OMICSCLAW_TEST_R_LIBRARY")
    if not library:
        pytest.skip("Set OMICSCLAW_TEST_R_LIBRARY to an explicit IRkernel library")
    launch = RegisteredRLaunch(str(Path(sys.executable).with_name("R")), (library,))

    async def factory(state, on_figure):
        kernel = SessionKernel(
            state.session_id,
            workspace=tmp_path,
            language="r",
            r_launch=launch,
            on_figure=on_figure,
        )
        await asyncio.to_thread(kernel.start)
        return kernel

    async def run():
        manager = PersistentKernelManager(
            tmp_path, kernel_factory=factory, manual_lifecycle=True
        )
        try:
            assert (
                await manager.execute(
                    "save",
                    "data <- new.env(); data$value <- 42; original <- data",
                    language="r",
                )
            ).status == "ok"
            kernel = manager.state_for("save", language="r").kernel
            result = await manager.execute(
                "save",
                "Sys.sleep(30)",
                language="r",
                timeout_s=0.3,
                retry_on_death=False,
                preserve_on_timeout=True,
            )
            assert result.status == "timeout" and result.usage["stop_confirmed"]
            assert (
                manager.state_for("save", language="r").kernel is kernel
                and kernel.is_alive()
            )
            assert (
                await manager.execute(
                    "save",
                    "stopifnot(identical(data, original), data$value==42)",
                    language="r",
                )
            ).status == "ok"
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), 90))


def test_cancelling_protected_observer_keeps_lock_and_token_until_real_idle(tmp_path):
    async def run():
        manager = PersistentKernelManager(tmp_path, manual_lifecycle=True)
        release, began = tmp_path / "release", asyncio.Event()
        loop = asyncio.get_running_loop()
        task = queued = None
        try:
            await manager.execute("save", "data={'answer':42}; original=id(data)")
            state = manager.state_for("save")
            kernel = state.kernel
            source = f"""import signal,time
from pathlib import Path
previous=signal.signal(signal.SIGINT,signal.SIG_IGN)
print('protected_began',flush=True)
try:
    while not Path({str(release)!r}).exists():time.sleep(.02)
finally:signal.signal(signal.SIGINT,previous)
"""
            task = asyncio.create_task(
                manager.execute(
                    "save",
                    source,
                    timeout_s=60,
                    retry_on_death=False,
                    preserve_on_timeout=True,
                    execution_token="protected",
                    callbacks=KernelCallbacks(
                        on_stream=lambda name, text: (
                            loop.call_soon_threadsafe(began.set)
                            if "protected_began" in text
                            else None
                        )
                    ),
                )
            )
            await asyncio.wait_for(began.wait(), 10)
            task.cancel()
            queued = asyncio.create_task(
                manager.execute(
                    "save",
                    "assert id(data)==original;data['next']=1",
                    retry_on_death=False,
                    execution_token="next",
                )
            )
            for _ in range(4):
                await asyncio.sleep(0)
                task.cancel()
                assert not task.done() and not queued.done()
                assert (
                    state.executing
                    and state.preserve_on_timeout
                    and state.lock.locked()
                )
                assert (
                    state.current_execution_token == "protected" and kernel.is_alive()
                )
                assert await manager.cancel("save") == "protected"
                assert (
                    await manager.cancel("save", execution_token="protected")
                    == "protected"
                )
            release.touch()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 15)
            assert (await asyncio.wait_for(asyncio.shield(queued), 15)).status == "ok"
            assert state.kernel is kernel and kernel.is_alive()
            assert (
                await manager.execute(
                    "save", "assert data=={'answer':42,'next':1} and id(data)==original"
                )
            ).status == "ok"
        finally:
            release.touch(exist_ok=True)
            for pending in (task, queued):
                if pending is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(pending), 15)
                    except asyncio.CancelledError:
                        pass
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), 90))


def test_protected_observer_cancellation_during_ready_callback_keeps_ownership(
    tmp_path,
):
    async def run():
        manager = PersistentKernelManager(tmp_path, manual_lifecycle=True)
        release, began = threading.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        task = None
        try:
            await manager.execute("save", "data={'answer':42};original=id(data)")
            state = manager.state_for("save")
            kernel = state.kernel

            def ready(current):
                if current.current_execution_token == "protected":
                    loop.call_soon_threadsafe(began.set)
                    assert release.wait(10)

            manager._on_kernel_ready = ready
            task = asyncio.create_task(
                manager.execute(
                    "save",
                    "data['saved']=True",
                    timeout_s=10,
                    retry_on_death=False,
                    preserve_on_timeout=True,
                    execution_token="protected",
                )
            )
            await asyncio.wait_for(began.wait(), 10)
            task.cancel()
            await asyncio.sleep(0)
            assert state.executing and state.preserve_on_timeout and state.lock.locked()
            assert state.current_execution_token == "protected"
            assert await manager.cancel("save") == "protected"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 15)
            assert state.kernel is kernel and kernel.is_alive()
            assert (
                await manager.execute(
                    "save",
                    "assert data=={'answer':42,'saved':True} and id(data)==original",
                )
            ).status == "ok"
        finally:
            release.set()
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 15)
                except asyncio.CancelledError:
                    pass
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), 60))
