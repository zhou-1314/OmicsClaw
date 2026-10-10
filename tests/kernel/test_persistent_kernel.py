"""The P4 persistent kernel, tested against real ipykernel processes.

Every test here starts at least one genuine IPython kernel through
jupyter_client — the same channel production rides — so what these pin
is the actual behaviour of the bridge: variables surviving across
cells, the interrupt ladder, the handover notice after a kill, the
three-tier output ceilings, figure capture into the artifact store,
the revision gate on adata syncs, and the reaper's soft top with its
protection set (time parameters injected small; no test sleeps a
wall-clock soft top out).

Fast where it can be: the manager takes ``OutputLimits`` and reaper
timings as constructor arguments precisely so the ceilings and the
sweeps are exercised without generating ten real megabytes or waiting
thirty real minutes. Async scenarios run under ``asyncio.run``, the
convention ``test_desktop_http.py`` set (pytest-asyncio is strict in
this repo and nothing else uses it).
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import pytest

pytest.importorskip("jupyter_client", reason="P4 needs jupyter_client")
pytest.importorskip("ipykernel", reason="P4 needs ipykernel")

from omicsclaw.kernel import (  # noqa: E402
    KernelCallbacks,
    KernelReaper,
    OutputLimits,
    PersistentKernelManager,
)
from omicsclaw.kernel.handles import HandleError, HandleRegistry  # noqa: E402
from omicsclaw.kernel.manager import SUMMARY_CELL_CODE  # noqa: E402


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "OMICSCLAW.md").write_text("kernel tests", encoding="utf-8")
    return tmp_path


def make_manager(workspace: pathlib.Path, **kwargs) -> PersistentKernelManager:
    return PersistentKernelManager(workspace, **kwargs)


# ---- 1. persistence across cells on one session ------------------------------


def test_second_cell_sees_the_first_cells_variable(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        try:
            first = await manager.execute("s1", "marker_v = 41\nprint('one')")
            assert first.status == "ok"
            assert "one" in first.stdout
            second = await manager.execute("s1", "print(marker_v + 1)")
            assert second.status == "ok"
            assert "42" in second.stdout
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_sessions_are_isolated_from_each_other(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        try:
            await manager.execute("s1", "private_v = 's1'")
            second = await manager.execute("s2", "print('private_v' in globals())")
            assert "False" in second.stdout
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 120))


# ---- 2. the interrupt ladder leaves the kernel alive --------------------------


def test_interrupt_cancels_the_cell_and_the_kernel_survives(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        try:
            await manager.execute("s1", "warm = 1")
            task = asyncio.create_task(
                manager.execute(
                    "s1",
                    "import time\nprint('begin', flush=True)\ntime.sleep(600)",
                )
            )
            await asyncio.sleep(3.0)
            tier = await manager.cancel("s1")
            result = await task
            assert tier in ("interrupted", "reset"), tier
            assert result.status in ("interrupted", "timeout"), result.status
            followup = await manager.execute("s1", "print('warm still here:', warm)")
            assert followup.status == "ok"
            assert "warm still here: 1" in followup.stdout
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 180))


# ---- 3. a killed kernel hands over with a notice on the next cell -------------


def test_killed_kernel_announces_the_handover_on_the_next_cell(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        try:
            await manager.execute("s1", "lost_v = 'gone soon'")
            state = manager.state_for("s1")
            pid = state.kernel.pid
            assert pid
            os.kill(pid, 9)
            result = await manager.execute("s1", "print('fresh kernel')")
            assert result.status == "ok"
            assert result.notice, "the next cell must carry the handover notice"
            assert "Variables from the previous kernel are gone" in result.notice
            assert result.stderr.startswith("[kernel restarted"), result.stderr
            assert "fresh kernel" in result.stdout
            # the fresh kernel really is fresh: the variable is gone
            gone = await manager.execute("s1", "print('lost_v' in globals())")
            assert "False" in gone.stdout
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 180))


# ---- 4. the three output ceilings --------------------------------------------


def test_runaway_print_hits_all_three_ceilings(workspace):
    async def scenario() -> None:
        limits = OutputLimits(
            stream_emit_max=2_000, cell_buffer_max=1_000, result_max=500
        )
        manager = make_manager(workspace, limits=limits)
        streamed: list[str] = []

        def on_stream(name: str, text: str) -> None:
            if name == "stdout":
                streamed.append(text)

        try:
            code = "for i in range(600):\n    print('x' * 40)"
            result = await manager.execute(
                "s1", code, callbacks=KernelCallbacks(on_stream=on_stream)
            )
        finally:
            await manager.aclose()
        emitted = sum(len(piece) for piece in streamed)
        assert emitted <= 2_300, f"streamed {emitted} chars past the emit ceiling"
        assert any("streaming cap" in piece for piece in streamed), (
            "the emit-stop marker must reach the audience"
        )
        assert "further bytes dropped" in result.stdout, (
            "the cell buffer must report exactly what it dropped"
        )
        dropped = result.stdout.split("further bytes dropped")[0].split("(")[-1]
        assert int(dropped) > 0
        assert len(result.stdout) <= 700, "the result ceiling must clamp"

    asyncio.run(asyncio.wait_for(scenario(), 120))


# ---- 5. figures land on disk and in the artifact store ------------------------


def test_display_data_figure_is_captured_and_registered(tmp_path):
    async def scenario() -> tuple[object, list[pathlib.Path]]:
        from omicsclaw.entry.assembly import WorkspaceKernelBinding
        from omicsclaw.memory.artifacts import ArtifactStore
        from omicsclaw.memory.database import Database

        binding = WorkspaceKernelBinding(tmp_path)
        binding._store = ArtifactStore(Database(":memory:"))  # the test seams the lazy open
        try:
            result = await binding.run_python(
                "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\nplt.show()",
                description="a line",
                session_id="fig-s1",
            )
        finally:
            await binding.manager.aclose()
        return result, list((tmp_path / "figures").glob("kernel_fig-s1_*.png"))

    result, files = asyncio.run(asyncio.wait_for(scenario(), 180))
    assert len(files) == 1, files
    assert "figures:" in result, result
    assert files[0].stat().st_size > 0


def test_figure_row_carries_the_kernel_capture_context(tmp_path):
    """The artifact row itself: capture='kernel', figure kind, sha256."""
    async def scenario() -> tuple:
        from omicsclaw.entry.assembly import WorkspaceKernelBinding
        from omicsclaw.memory.artifacts import ArtifactStore
        from omicsclaw.memory.database import Database

        store = ArtifactStore(Database(":memory:"))
        binding = WorkspaceKernelBinding(tmp_path)
        binding._store = store
        try:
            await binding.manager.execute(
                "fig-s2",
                "import matplotlib.pyplot as plt\nplt.plot([1], [1])\nplt.show()",
            )
        finally:
            await binding.manager.aclose()
        files = list((tmp_path / "figures").glob("kernel_fig-s2_*.png"))
        return store, files

    store, files = asyncio.run(asyncio.wait_for(scenario(), 180))
    assert len(files) == 1
    row = store.latest_for_path(str(files[0]))
    assert row is not None
    assert row.kind == "figure"
    assert row.meta.get("capture") == "kernel"
    assert row.produced_by == "kernel_display"
    assert row.sha256


# ---- 6. the revision gate on adata syncs --------------------------------------


class _FakeKernel:
    """A kernel double that counts reads: the revision gate under test."""

    def __init__(self, workspace: pathlib.Path) -> None:
        self.workspace = workspace
        self.execute_calls: list[str] = []

    def execute(self, code: str, **_: object) -> object:
        from omicsclaw.kernel.session import CellResult

        self.execute_calls.append(code)
        return CellResult(status="ok", stdout="AnnData object with 10 x 20")


def test_sync_in_skips_when_the_revision_has_not_moved(tmp_path):
    registry = HandleRegistry()
    kernel = _FakeKernel(tmp_path)
    path = tmp_path / "expr.h5ad"
    path.write_bytes(b"not really h5ad; only its revision matters here")

    first = registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    assert first["skipped"] is False
    assert "read_h5ad" in kernel.execute_calls[0]

    second = registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    assert second["skipped"] is True
    assert len(kernel.execute_calls) == 1, "an unchanged revision must not re-read"

    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    third = registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    assert third["skipped"] is False
    assert len(kernel.execute_calls) == 2


def test_sync_in_refuses_cross_session_access(tmp_path):
    registry = HandleRegistry()
    kernel = _FakeKernel(tmp_path)
    path = tmp_path / "expr.h5ad"
    path.write_bytes(b"x")
    registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    with pytest.raises(HandleError):
        registry.sync_in("expr", path, session_id="s2", kernel=_FakeKernel(tmp_path))


def test_drop_session_forgets_the_handles(tmp_path):
    registry = HandleRegistry()
    kernel = _FakeKernel(tmp_path)
    path = tmp_path / "expr.h5ad"
    path.write_bytes(b"x")
    registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    assert registry.drop_session("s1") == ["expr"]
    again = registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    assert again["skipped"] is False, "after a recycle the gate must re-open"


def test_sync_out_writes_the_out_h5ad_suffix(tmp_path):
    registry = HandleRegistry()
    kernel = _FakeKernel(tmp_path)
    path = tmp_path / "expr.h5ad"
    path.write_bytes(b"x")
    registry.sync_in("expr", path, session_id="s1", kernel=kernel)
    target = registry.sync_out(
        "expr", tmp_path / "results", session_id="s1", kernel=kernel,
        workspace=tmp_path,
    )
    assert target.name == "expr.out.h5ad"
    assert target.parent == tmp_path / "results"


# ---- 7. the reaper: soft top, protection set, summary ------------------------


def test_reaper_soft_top_recycles_an_idle_session(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        manager._reaper = KernelReaper(manager, idle_soft_s=0.2, poll_s=0.05)
        try:
            await manager.execute("s1", "kept_v = 1")
            state = manager.state_for("s1")
            await asyncio.sleep(0.4)
            await manager._reaper.sweep_once()
            assert state.kernel is None, "an idle session past the soft top goes"
            assert state.obituary_pending
            result = await manager.execute("s1", "print('back')")
            assert "kernel restarted" in result.stderr
            assert "back" in result.stdout
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 180))


def test_reaper_leaves_protected_sessions_alone(workspace):
    async def scenario() -> None:
        manager = make_manager(workspace)
        manager._reaper = KernelReaper(manager, idle_soft_s=0.0, poll_s=0.05)
        try:
            await manager.execute("s1", "v = 1")
            state = manager.state_for("s1")
            manager.protect("s1", "pending approval")
            await manager._reaper.sweep_once()
            assert state.kernel is not None, "a protected session is never reaped"
            manager.unprotect("s1", "pending approval")
            await manager._reaper.sweep_once()
            assert state.kernel is None, "unprotected, the sweep takes it"
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 180))


def test_reaper_runs_the_namespace_summary_before_recycling(workspace):
    async def scenario() -> str:
        manager = make_manager(workspace)
        manager._reaper = KernelReaper(manager, idle_soft_s=0.1, poll_s=0.05)
        try:
            await manager.execute(
                "s1", "import numpy as np\narr = np.zeros((3, 4))\nkept = 'yes'"
            )
            state = manager.state_for("s1")
            await asyncio.sleep(0.2)
            await manager._reaper.sweep_once()
            return state.obituary
        finally:
            await manager.aclose()

    obituary = asyncio.run(asyncio.wait_for(scenario(), 180))
    assert "kept: str" in obituary
    assert "arr: ndarray shape=(3, 4)" in obituary


# ---- usage probing -----------------------------------------------------------


def test_each_cell_reports_usage_with_rss(workspace):
    async def scenario() -> tuple:
        manager = make_manager(workspace)
        try:
            result = await manager.execute("s1", "print('hi')")
            state = manager.state_for("s1")
            return result, state
        finally:
            await manager.aclose()

    result, state = asyncio.run(asyncio.wait_for(scenario(), 120))
    assert result.usage["wall_s"] >= 0
    assert result.usage["peak_rss_kb"] and result.usage["peak_rss_kb"] > 1000
    assert state.peak_rss_kb == result.usage["peak_rss_kb"]


def test_summary_cell_code_compiles():
    compile(SUMMARY_CELL_CODE, "<summary>", "exec")


# ---- P4 review fixes: figure numbering, retry disclosure, prompt cancel ------


def test_each_cells_figures_get_their_own_files_across_cells_and_restarts(workspace):
    """M1: the figure number is a per-kernel sequence seeded past this
    session's earlier files — neither a second cell nor a lazy cold
    restart overwrites the first cell's figure."""
    import asyncio as _asyncio

    code = "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\nplt.show()"

    async def scenario() -> list[pathlib.Path]:
        manager = make_manager(workspace)
        try:
            await manager.execute("fig-unique", code)
            await manager.execute("fig-unique", code)
            kernel = manager.state_for("fig-unique").kernel
            if kernel is not None:
                kernel.kill()
            await manager.execute("fig-unique", code)
        finally:
            await manager.aclose()
        return sorted((workspace / "figures").glob("kernel_fig-unique_*.png"))

    files = _asyncio.run(_asyncio.wait_for(scenario(), 240))
    assert len(files) == 3, files
    assert len({f.name for f in files}) == 3, files
    assert all(f.stat().st_size > 0 for f in files)


def test_mid_cell_death_retry_discloses_the_double_run(workspace):
    """M2: the single mid-cell retry is disclosed in the result — a cell
    whose side effects ran before the death may have run them twice."""
    import asyncio as _asyncio

    marker = workspace / "side_effect.log"
    code = (
        "import os\n"
        f"p = {str(marker)!r}\n"
        "if not os.path.exists(p):\n"
        "    open(p, 'a').write('x\\n')\n"
        "    os.kill(os.getpid(), 9)\n"
        "print('survived')\n"
    )

    async def scenario():
        manager = make_manager(workspace)
        try:
            return await manager.execute("retry-s1", code)
        finally:
            await manager.aclose()

    result = _asyncio.run(_asyncio.wait_for(scenario(), 180))
    assert result.status == "ok", result.status
    assert "may have partially executed once" in result.stderr, result.stderr
    assert "side effects" in result.stderr, result.stderr


def test_cancel_returns_promptly_when_the_cell_ignores_sigint(workspace):
    """M3: tier 3 kills the kernel before taking the session lock, so a
    SIGINT-immune cell cannot hold the cancel hostage until the cell
    timeout — and the cancelled cell is not retried on the replacement."""
    import asyncio as _asyncio
    import time as _time

    marker = workspace / "cancel_marker.log"
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"open({str(marker)!r}, 'a').write('once\\n')\n"
        "time.sleep(300)\n"
    )

    async def scenario():
        manager = make_manager(workspace)
        try:
            task = _asyncio.create_task(
                manager.execute("cancel-s1", code, timeout_s=290)
            )
            await _asyncio.sleep(2.0)
            began = _time.monotonic()
            answer = await manager.cancel("cancel-s1")
            elapsed = _time.monotonic() - began
            result = await task
            return answer, elapsed, result
        finally:
            await manager.aclose()

    answer, elapsed, result = _asyncio.run(_asyncio.wait_for(scenario(), 180))
    assert answer == "reset", answer
    assert elapsed < 30.0, elapsed
    assert result.status == "dead", result.status
    assert marker.read_text(encoding="utf-8").count("once") == 1
