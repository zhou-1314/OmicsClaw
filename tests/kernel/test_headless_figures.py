"""Python kernels keep notebook figure output independent of the host backend."""

import asyncio
import os

import pytest

from omicsclaw.kernel import PersistentKernelManager


def test_headless_parent_still_captures_figures_and_cell_can_switch_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLBACKEND", "Agg")

    async def scenario():
        manager = PersistentKernelManager(tmp_path)
        try:
            plotted = await manager.execute(
                "headless",
                "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\nplt.show()",
            )
            assert plotted.status == "ok"
            assert len(plotted.figures) == 1
            assert os.environ["MPLBACKEND"] == "Agg"
            switched = await manager.execute(
                "headless",
                "import matplotlib\nmatplotlib.use('Agg')\nprint(matplotlib.get_backend())",
            )
            assert switched.status == "ok"
            assert switched.stdout.strip().lower() == "agg"
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_r_kernel_keeps_parent_matplotlib_setting(tmp_path, monkeypatch):
    from omicsclaw.kernel.session import r_available

    if not r_available():
        pytest.skip("IRkernel not installed")
    monkeypatch.setenv("MPLBACKEND", "Agg")

    async def scenario():
        manager = PersistentKernelManager(tmp_path)
        try:
            result = await manager.execute(
                "r-parent-env",
                'cat(Sys.getenv("MPLBACKEND")); plot(1:3)',
                language="r",
                timeout_s=240,
            )
            assert result.status == "ok"
            assert "Agg" in result.stdout
            assert len(result.figures) == 1
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 300))
