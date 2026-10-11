"""The manager's location dimension: dispatch, isolation, and the off
contract.

The remote half is a recording double — the manager promises it only an
async ``start(session_id, *, host, ...)`` — so what these pin is the
manager's own behaviour:

* the off contract: without a runtime bound, a remote location is
  refused at the door and nothing else changes (P4's local semantics,
  state keys, snapshots);
* dispatch and laziness: a remote location starts exactly one kernel on
  first execute and reuses it on the second — the same laziness P4's
  local kernels have;
* isolation: one session may hold a local kernel and a remote one (and
  two remote ones, on different hosts) — separate states, separate
  namespaces;
* the P4 semantics that must survive the location dimension unchanged:
  the reaper's idle recycle, the obituary on a dead kernel, protection,
  and the shape refusals (R remote, adata_in remote).
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from omicsclaw.kernel import CellResult, KernelReaper, PersistentKernelManager
from omicsclaw.kernel.manager import normalize_location


class FakeRemoteKernel:
    """The kernel the fake runtime answers: records, mostly honest."""

    def __init__(self, session_id: str, host: str) -> None:
        self.session_id = session_id
        self.host = host
        self.cells: list[str] = []
        self.killed = 0
        self.interrupted = 0
        self.shutdowns = 0
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def execute(self, code: str, *, timeout_s: float = 1800.0, callbacks=None):
        self.cells.append(code)
        if not self.alive:
            result = CellResult()
            result.status = "dead"
            return result
        result = CellResult()
        result.status = "ok"
        result.stdout = f"remote:{self.host}:{self.cells and len(self.cells)}"
        return result

    def interrupt(self) -> None:
        self.interrupted += 1

    def kill(self) -> None:
        self.killed += 1
        self.alive = False

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.alive = False

    def peak_rss_kb(self):
        return None


class FakeRemoteRuntime:
    """The manager's injected remote half, as a call log."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, str]] = []
        self.kernels: list[FakeRemoteKernel] = []

    async def start(self, session_id, *, host, workspace, limits=None,
                    on_figure=None, language="python", startup_timeout=None):
        self.starts.append((session_id, host))
        kernel = FakeRemoteKernel(session_id, host)
        self.kernels.append(kernel)
        return kernel


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "OMICSCLAW.md").write_text("location tests", encoding="utf-8")
    return tmp_path


# ---- the shape -----------------------------------------------------------------


def test_normalize_location_accepts_local_and_one_remote_word():
    assert normalize_location("local") == "local"
    assert normalize_location("remote:gpu-lab") == "remote:gpu-lab"
    assert normalize_location("remote:a.b.c-1") == "remote:a.b.c-1"


@pytest.mark.parametrize("bad", [
    "", "elsewhere", "remote:", "remote:two words", "remote:lab;rm",
    "remote:-leading-dash", "REMOTE:lab", "remote:lab\n",
])
def test_normalize_location_refuses_everything_else(bad):
    with pytest.raises(ValueError):
        normalize_location(bad)


# ---- the off contract ------------------------------------------------------------


def test_without_a_runtime_remote_locations_are_refused_at_the_door(workspace):
    async def scenario():
        manager = PersistentKernelManager(workspace)
        try:
            with pytest.raises(ValueError, match="no remote kernel runtime"):
                await manager.execute("s1", "x = 1", location="remote:lab1")
            # and nothing was created on the way out
            assert manager.states == {}
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_a_local_manager_is_unchanged_by_the_dimension(workspace):
    async def scenario():
        manager = PersistentKernelManager(workspace)
        try:
            state = manager.state_for("s1", language="python", location="local")
            assert manager.state_for("s1") is state  # local is the default
            assert state.location == "local"
            assert state.snapshot()["location"] == "local"
            manager.protect("s1", "reason")
            assert "reason" in state.protected
            manager.unprotect("s1", "reason")
            assert not state.protected
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


# ---- dispatch and laziness --------------------------------------------------------


def test_a_remote_location_starts_one_kernel_and_reuses_it(workspace):
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            first = await manager.execute("s1", "a = 1", location="remote:lab1")
            assert first.status == "ok" and first.stdout == "remote:lab1:1"
            second = await manager.execute("s1", "print(a)", location="remote:lab1")
            assert second.stdout == "remote:lab1:2"
            # lazy: two cells, one kernel — the P4 contract, location-free
            assert runtime.starts == [("s1", "lab1")]
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_locations_and_hosts_are_separate_states(workspace):
    """The key is (session, language, location), and nothing collapses:
    two hosts and the local slot of one session are three states — the
    local one is only conjured by ``state_for``, never by a remote
    execute (which would have started a real local kernel here)."""
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            await manager.execute("s1", "x = 1", location="remote:lab1")
            await manager.execute("s1", "x = 2", location="remote:lab2")
            manager.state_for("s1")  # the local slot, lazily and only now
            assert set(manager.states) == {
                ("s1", "python", "remote:lab1"),
                ("s1", "python", "remote:lab2"),
                ("s1", "python", "local"),
            }
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_two_hosts_never_share_a_kernel(workspace):
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            await manager.execute("s1", "a = 1", location="remote:lab1")
            await manager.execute("s1", "a = 2", location="remote:lab2")
            assert len(runtime.kernels) == 2
            hosts = {kernel.host for kernel in runtime.kernels}
            assert hosts == {"lab1", "lab2"}
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


# ---- the refusals ------------------------------------------------------------------


def test_remote_r_and_remote_adata_are_refused(workspace):
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            with pytest.raises(ValueError, match="python-only"):
                await manager.execute("s1", "x", language="r",
                                      location="remote:lab1")
            with pytest.raises(ValueError, match="beside the file"):
                await manager.execute("s1", "x", adata_in="in.h5ad",
                                      location="remote:lab1")
            assert runtime.starts == []
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


# ---- the P4 semantics that must survive ------------------------------------------


def test_the_reaper_recycles_an_idle_remote_kernel(workspace):
    """The P4 reaper test's shape (replace the manager's reaper, drive
    one sweep): the idle top must not care where the kernel lives."""
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        manager._reaper = KernelReaper(manager, idle_soft_s=0.0, poll_s=0.05)
        try:
            await manager.execute("s1", "a = 1", location="remote:lab1")
            kernel = runtime.kernels[0]
            # soft top 0.0 s and a 0.05 s poll: the background reaper
            # (which execute started) recycles on its own — the point is
            # that the idle top does not care where the kernel lives
            for _ in range(200):
                if manager.state_for(
                    "s1", location="remote:lab1"
                ).kernel is None:
                    break
                await asyncio.sleep(0.05)
            state = manager.state_for("s1", location="remote:lab1")
            assert state.kernel is None  # recycled by the idle top
            assert kernel.killed == 1
            assert state.obituary_pending
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_a_dead_remote_kernel_hands_over_with_an_obituary(workspace):
    async def scenario():
        runtime = FakeRemoteRuntime()
        manager = PersistentKernelManager(workspace, remote_runtime=runtime)
        try:
            await manager.execute("s1", "a = 1", location="remote:lab1")
            runtime.kernels[0].alive = False  # killed out from under us
            second = await manager.execute("s1", "print(a)",
                                           location="remote:lab1")
            assert second.status == "ok"
            assert "kernel restarted for this session" in second.stderr
            # the dead one was killed and a replacement started
            assert runtime.kernels[0].killed == 1
            assert len(runtime.kernels) == 2
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_protection_reaches_the_remote_state(workspace):
    runtime = FakeRemoteRuntime()
    manager = PersistentKernelManager(workspace, remote_runtime=runtime)
    manager.protect("s1", "job:j1", location="remote:lab1")
    state = manager.state_for("s1", location="remote:lab1")
    assert "job:j1" in state.protected
    # and the local state of the same session was not conjured
    assert ("s1", "python", "local") not in manager.states
