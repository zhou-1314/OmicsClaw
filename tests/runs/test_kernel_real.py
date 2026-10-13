import asyncio
import sys

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")
from omicsclaw.kernel import PersistentKernelManager
from omicsclaw.remote.kernel import RemoteKernelRuntime
from omicsclaw.remote.ssh import SystemSshSpawner
from omicsclaw.runs.models import Run
from omicsclaw.runs.projections import bind_kernel, observe_kernel
from omicsclaw.runs.store import RunStore
from tests.remote.test_kernel_integration_ssh import _self_reachable


@pytest.mark.skipif(not _self_reachable(), reason="ssh localhost unavailable")
def test_real_remote_kernel_identity_heartbeat_and_shared_runs(tmp_path):
    async def scenario():
        manager = PersistentKernelManager(
            tmp_path,
            remote_runtime=RemoteKernelRuntime(
                SystemSshSpawner(), python=sys.executable
            ),
        )
        store = RunStore(tmp_path / "runs.db")
        session = "run-projection-" + tmp_path.name
        try:
            await manager.execute(session, "marker=41", location="remote:localhost")
            state = manager.state_for(session, location="remote:localhost")
            first = await asyncio.to_thread(observe_kernel, state)
            assert first.status == "ready"
            assert first.last_heartbeat
            assert first.environment == sys.executable
            one, two = Run(status="succeeded"), Run(status="succeeded")
            store.save(one)
            store.save(two)
            bind_kernel(store, first, one.run_id)
            result = await manager.execute(
                session, "print(marker+1)", location="remote:localhost"
            )
            assert "42" in result.stdout
            second = await asyncio.to_thread(observe_kernel, state)
            bind_kernel(store, second, two.run_id)
            assert second.kernel_id == first.kernel_id
            assert set(store.load(first.kernel_id).run_ids) == {one.run_id, two.run_id}
            state.kernel._tunnel.terminate()
            state.kernel._tunnel.wait(timeout=10)
            state.kernel._kc.stop_channels()
            disconnected = await asyncio.to_thread(observe_kernel, state)
            assert disconnected.status == "unknown"
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_restart_does_not_reassign_historical_job_kernel(tmp_path):
    import os
    import signal

    from tests.entry.test_desktop_jobs import code_run_client, wait_terminal

    manager, http = code_run_client(tmp_path)
    code = "from pathlib import Path; Path('tables').mkdir(exist_ok=True); Path('tables/result.csv').write_text('x\\n1\\n')"
    with http as client:
        first = client.post(
            "/jobs",
            json={"kind": "code_run", "code": code, "session_id": "restart-history"},
        ).json()["job_id"]
        assert wait_terminal(client, first)["status"] == "succeeded"
        old = next(j for j in manager.run_store.list("job") if j.execution_ref == first)
        old_kernel = old.kernel_id
        state = manager.kernels.manager.state_for("restart-history")
        os.kill(state.kernel.pid, signal.SIGKILL)
        second = client.post(
            "/jobs",
            json={"kind": "code_run", "code": code, "session_id": "restart-history"},
        ).json()["job_id"]
        assert wait_terminal(client, second)["status"] == "succeeded"
        response = client.get("/runs")
        assert response.status_code == 200
        one = manager.run_store.load(old.job_id)
        two = next(
            j for j in manager.run_store.list("job") if j.execution_ref == second
        )
        assert one.kernel_id == old_kernel
        assert two.kernel_id != old_kernel
        assert all(a.environment_ref for a in manager.run_store.list("artifact"))
        assert all(
            a.kernel_id == old_kernel
            for a in manager.run_store.list("artifact")
            if a.job_id == first
        )
        assert all(
            a.kernel_id == two.kernel_id
            for a in manager.run_store.list("artifact")
            if a.job_id == second
        )
        assert two.run_id not in manager.run_store.load(old_kernel).run_ids
