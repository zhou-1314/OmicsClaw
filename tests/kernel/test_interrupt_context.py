"""Jupyter's sync interrupt bridge cannot borrow the caller thread's loop."""

import asyncio
import contextvars
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jupyter_core")
from jupyter_core.utils import ensure_event_loop, run_sync

from omicsclaw.kernel.session import SessionKernel


@pytest.mark.parametrize("worker", [False, True])
def test_interrupt_keeps_identity_context_and_closes_only_its_own_loop(tmp_path, worker):
    authority = contextvars.ContextVar("interrupt-test-authority", default=None)
    observed = []
    guarded = []

    async def send():
        observed.append(asyncio.get_running_loop())

    kernel = SessionKernel("context", workspace=tmp_path)
    kernel._km = SimpleNamespace(
        provisioner=SimpleNamespace(pid=123), interrupt_kernel=run_sync(send)
    )

    def guard(pid):
        guarded.append((pid, authority.get()))
        return authority.get() == "current-owner"

    kernel._signal_guard = guard

    async def run():
        token = authority.set("current-owner")
        try:
            main = ensure_event_loop()
            assert main is asyncio.get_running_loop()
            if worker:
                await asyncio.to_thread(kernel.interrupt)
            else:
                kernel.interrupt()
            assert authority.get() == "current-owner"
            assert guarded == [(123, "current-owner")]
            assert len(observed) == 1
            assert not main.is_closed()
            if worker:
                assert observed[0].is_closed()
            authority.set("stale-owner")
            await asyncio.to_thread(kernel.interrupt)
            assert len(observed) == 1
            assert authority.get() == "stale-owner"
        finally:
            authority.reset(token)

    asyncio.run(run())


def test_cached_main_loop_before_start_and_shutdown(tmp_path, recwarn):
    pytest.importorskip("ipykernel")
    from omicsclaw.kernel.manager import PersistentKernelManager
    from omicsclaw.kernel.session import KernelCallbacks

    authority = contextvars.ContextVar("stream-test-authority", default=None)
    callback_authorities = []

    async def run():
        manager = PersistentKernelManager(tmp_path)
        assert ensure_event_loop() is asyncio.get_running_loop()
        token = authority.set("current-owner")
        try:
            result = await manager.execute("cached", "value=21\nprint(value*2)")
            assert result.status == "ok"
            assert result.stdout == "42\n"
            fd_dir = Path("/proc/self/fd")
            before = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None
            burst = await manager.execute(
                "cached",
                "for item in range(200): print(item,flush=True)",
                callbacks=KernelCallbacks(
                    on_stream=lambda *_: callback_authorities.append(authority.get())
                ),
            )
            assert burst.status == "ok"
            assert burst.stdout.splitlines() == [str(item) for item in range(200)]
            assert callback_authorities and set(callback_authorities) == {"current-owner"}
            if before is not None:
                assert len(list(fd_dir.iterdir())) <= before + 2
        finally:
            await manager.aclose()
            authority.reset(token)

    asyncio.run(run())
    assert not [item for item in recwarn if "was never awaited" in str(item.message)]
