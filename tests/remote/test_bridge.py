"""The C2 jobs-plane bridge: runtime routing through a real JobsManager.

``JobsManager`` over an in-memory store, with
:class:`~omicsclaw.remote.RemoteJobsBridge` bound as the remote runtime
over a scripted plane. This is the whole routing path a
``POST /jobs {"runtime": "remote:hpc1"}`` takes, minus HTTP: creation,
validation, the event vocabulary, the persisted runtime, cancellation,
and what a restart does to a job that was never this process's to lose.

**One ``asyncio.run`` per test.** The manager's job task belongs to the
loop that created it, so every scenario here drives creation and
observation inside a single coroutine — the same reason the desktop
tests run against one live server rather than re-entering loops.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.jobs_manager import (
    JobError,
    JobsManager,
    JobStore,
)
from omicsclaw.memory.database import Database
from omicsclaw.remote.bridge import RemoteJobFailed, RemoteJobsBridge
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.plane import RemotePlaneBinding
from omicsclaw.remote.ssh import RemoteHostUnreachable
from omicsclaw.tools.builtin.remote import StatusReport
from tests.remote._fakes import PROBE_OK, FakeSpawner, run

TERMINAL = ("succeeded", "failed", "canceled", "interrupted")


@pytest.fixture()
def spawner() -> FakeSpawner:
    fake = FakeSpawner()
    fake.on("base64 -d", PROBE_OK)
    fake.on("mkdir -p", "")
    fake.on("setsid nohup", "4242")
    fake.on("kill -0", "__OMICSCLAW_RUNNING__")
    fake.on("stat -c %s", "0")
    fake.on("_omicsclaw_outputs", "")
    fake.on("tail -c", "")
    return fake


class _App:
    def __init__(self, workspace: str) -> None:
        config = type(
            "Config", (), {"workspace": workspace, "approval_timeout_s": None}
        )
        self.config = config()


class _ScriptedExecutor:
    """Answers status from the scenario's knob, not the transport.

    The bridge polls through ``plane.executor_for(...).status`` and the
    reconciler through the same seam, so this is the one place a test
    needs to script.
    """

    def __init__(self, plane: _ScriptedPlane) -> None:
        self._plane = plane

    async def status(self, handle: RemoteJobHandle) -> StatusReport:
        if self._plane.state == "running":
            return StatusReport(state="running")
        return StatusReport(
            state=self._plane.state,
            exit_code=0 if self._plane.state == "done" else 3,
        )

    async def tail_log(self, workdir: str, offset: int, limit: int) -> str:
        return ""


class _ScriptedPlane(RemotePlaneBinding):
    """A real plane over a fake transport, with knob-turnable status."""

    def __init__(self, spawner: FakeSpawner, workspace=None):
        super().__init__(Database(":memory:"), spawner=spawner, workspace=workspace)
        self.state = "running"
        self._scripted = _ScriptedExecutor(self)

    def executor_for(self, host_alias: str):
        return self._scripted

    async def status(self, job_ref: str) -> StatusReport:
        # The bridge polls through this method; the reconciler through
        # executor_for().status — both read the same knob.
        return await self._scripted.status(
            RemoteJobHandle(kind="nohup", host="", workdir="")
        )


def _manager(spawner: FakeSpawner, tmp_path, *, plane: _ScriptedPlane | None = None):
    scripted = (
        plane if plane is not None else _ScriptedPlane(spawner, workspace=tmp_path)
    )
    manager = JobsManager(
        _App(str(tmp_path)),
        store=JobStore(Database(":memory:")),
        remote_runtime=RemoteJobsBridge(scripted, poll_interval_s=0.01),
    )
    manager.plane = scripted
    return manager


async def _until(predicate, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition never became true")
        await asyncio.sleep(0.01)


def _events(store: JobStore, job_id: str):
    return [(t, p) for _, t, p in store.events_after(job_id, 0)]


def _terminal(manager: JobsManager, job_id: str):
    return _until(lambda: (r := manager.get_job(job_id)) and r.status in TERMINAL and r)


class TestRemoteRouting:
    def test_observation_timeout_does_not_signal_or_finish_external_business_job(
        self, spawner, tmp_path
    ):
        async def main():
            manager = _manager(spawner, tmp_path)
            manager.default_timeout_s = 0.03
            record = await manager.create_job(
                inputs={"command": "sleep 10"}, runtime="remote:hpc1"
            )
            await _until(
                lambda: (
                    manager._live.get(record.id) is not None
                    and manager._live[record.id].task.done()
                )
            )
            events = _events(manager.store, record.id)
            assert (
                manager.get_job(record.id).status,
                any("kill -TERM" in command for command in spawner.ssh_commands()),
                any(
                    kind in {"job.done", "job.failed", "execution.finished"}
                    for kind, _ in events
                ),
            ) == ("unknown", False, False)

        run(main())

    def test_owner_shutdown_only_detaches_external_observation(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "sleep 10"}, runtime="remote:hpc1"
            )
            await _until(lambda: manager.plane.job_store().by_local_job(record.id))
            await manager.shutdown_observations()
            events = _events(manager.store, record.id)
            assert (
                manager.get_job(record.id).status,
                any("kill -TERM" in command for command in spawner.ssh_commands()),
                any(
                    kind in {"job.done", "job.failed", "execution.finished"}
                    for kind, _ in events
                ),
            ) == ("unknown", False, False)

        run(main())

    def test_a_remote_job_submits_and_finishes(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi > out.txt", "outputs": ["out.txt"]},
                runtime="remote:hpc1",
            )
            assert record.runtime == "remote:hpc1"
            manager.plane.state = "done"
            finished = await _terminal(manager, record.id)
            return finished, any("setsid nohup" in c for c in spawner.ssh_commands())

        finished, submitted = run(main())
        assert finished.status == "succeeded"
        assert submitted

    def test_the_vocabulary_is_the_p1_one(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi"}, runtime="remote:hpc1"
            )
            manager.plane.state = "done"
            await _terminal(manager, record.id)
            return [t for t, _ in _events(manager.store, record.id)]

        types = run(main())
        assert types[0] == "job.created"
        assert "job.started" in types
        assert "progress" in types
        assert "tool_started" in types
        assert types[-1] == "job.done"

    def test_the_runtime_is_persisted_and_read_back(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi"}, runtime="remote:hpc1"
            )
            manager.plane.state = "done"
            await _terminal(manager, record.id)
            reread = manager.get_job(record.id)
            return reread.runtime, reread.as_payload()["runtime"]

        runtime, payload_runtime = run(main())
        assert runtime == "remote:hpc1"
        assert payload_runtime == "remote:hpc1"

    def test_a_failing_remote_job_fails_the_local_one(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "exit 3"}, runtime="remote:hpc1"
            )
            manager.plane.state = "failed"
            await _terminal(manager, record.id)
            return (
                manager.get_job(record.id).status,
                [t for t, _ in _events(manager.store, record.id)][-1],
            )

        status, last_event = run(main())
        assert status == "failed"
        assert last_event == "job.failed"

    def test_the_handle_row_links_back_to_the_local_job(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi"}, runtime="remote:hpc1"
            )
            manager.plane.state = "done"
            await _terminal(manager, record.id)
            row = manager.plane.job_store().by_local_job(record.id)
            assert row is not None
            return row.host_alias, row.kind

        host_alias, kind = run(main())
        assert host_alias == "hpc1"
        assert kind == "nohup"

    def test_creation_freezes_one_target_snapshot_in_both_stores(
        self, spawner, tmp_path
    ):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi"}, runtime="remote:hpc1"
            )
            local = manager.get_job(record.id)
            remote = await _until(
                lambda: manager.plane.job_store().by_local_job(record.id)
            )
            assert local is not None and remote is not None
            return (
                (local.target_alias, local.target_workdir, local.capabilities),
                (remote.target_alias, remote.target_workdir, remote.capabilities),
            )

        local_snapshot, remote_snapshot = run(main())
        assert local_snapshot == remote_snapshot
        assert local_snapshot[0] == "hpc1"
        assert local_snapshot[1].startswith("/scratch/fake/omicsclaw/")
        assert local_snapshot[2] == ("remote_exec", "remote_submit")

    def test_code_run_remote_location_freezes_kernel_target_snapshot(
        self, spawner, tmp_path
    ):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                kind="code_run",
                inputs={"code": "1", "location": "remote:hpc1"},
            )
            return record, await _terminal(manager, record.id)

        record, finished = run(main())
        assert record.runtime == "local"
        assert record.target_alias == "hpc1"
        assert record.target_workdir.startswith("/scratch/fake/omicsclaw/")
        assert record.capabilities == ("remote_kernel",)
        assert finished.status == "failed"
        assert "remote kernel locations are not enabled" in finished.error


class TestValidationAndRefusal:
    def test_remote_resume_without_workdir_snapshot_is_refused(self, spawner, tmp_path):
        async def main():
            plane = _ScriptedPlane(spawner, workspace=tmp_path)
            bridge = RemoteJobsBridge(plane)
            record = SimpleNamespace(
                id="legacy",
                target_alias="hpc1",
                target_workdir="",
                inputs={"command": "echo hi"},
            )
            with pytest.raises(RemoteJobFailed, match="immutable target workdir"):
                await bridge(record, SimpleNamespace())

        run(main())

    def test_remote_without_a_bound_plane_is_refused_not_faked(self, tmp_path):
        async def main():
            bare = JobsManager(
                _App(str(tmp_path)), store=JobStore(Database(":memory:"))
            )
            await bare.create_job(inputs={"command": "x"}, runtime="remote:hpc1")

        with pytest.raises(JobError) as caught:
            run(main())
        assert caught.value.code == "remote_runtime_unavailable"

    @pytest.mark.parametrize(
        "runtime",
        ["remote:", "remote:bad host", "remote:-x", "elsewhere", "remote:a;b"],
    )
    def test_malformed_runtimes_are_refused(self, spawner, tmp_path, runtime):
        async def main():
            manager = _manager(spawner, tmp_path)
            await manager.create_job(inputs={"command": "x"}, runtime=runtime)

        with pytest.raises(JobError) as caught:
            run(main())
        assert caught.value.code == "invalid_runtime"

    def test_remote_validation_needs_a_command(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            await manager.create_job(inputs={}, runtime="remote:hpc1")

        with pytest.raises(JobError) as caught:
            run(main())
        assert "command" in caught.value.code

    @pytest.mark.parametrize(
        "declared",
        [
            ["data/counts.csv"],  # bare strings, not pairs
            [{"src": "data/counts.csv"}],  # no dst
            [{"src": "a", "dst": ""}],  # empty dst
            ["not-a-list"],
        ],
    )
    def test_remote_inputs_must_be_src_dst_objects(self, spawner, tmp_path, declared):
        async def main():
            manager = _manager(spawner, tmp_path)
            await manager.create_job(
                inputs={"command": "x", "inputs": declared},
                runtime="remote:hpc1",
            )

        with pytest.raises(JobError) as caught:
            run(main())
        assert caught.value.code.startswith("invalid_inputs")

    def test_remote_inputs_are_forwarded_as_pairs(self, spawner, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "counts.csv").write_text("count\n1\n")

        async def main():
            manager = _manager(spawner, tmp_path)
            seen = {}
            original = manager.plane.submit

            async def spying(host, command, **kwargs):
                seen.update(kwargs)
                return await original(host, command, **kwargs)

            manager.plane.submit = spying  # type: ignore[method-assign]
            record = await manager.create_job(
                inputs={
                    "command": "x",
                    "inputs": [{"src": "data/counts.csv", "dst": "counts.csv"}],
                },
                runtime="remote:hpc1",
            )
            manager.plane.state = "done"
            await _terminal(manager, record.id)
            return seen.get("inputs"), seen.get("target_workdir"), record.target_workdir

        inputs, target_workdir, snapshot_workdir = run(main())
        assert inputs == (("data/counts.csv", "counts.csv"),)
        assert target_workdir == snapshot_workdir

    def test_local_jobs_still_run_the_local_runner(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            # No skill mounted: a local job is refused by its runner's
            # validate — proof the dispatch chose the local path.
            await manager.create_job(inputs={"command": "x"})

        with pytest.raises(JobError):
            run(main())


class TestCancellationAndRestart:
    def test_cancelling_cancels_remotely_too(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "sleep 1000"}, runtime="remote:hpc1"
            )
            await _until(lambda: manager.get_job(record.id).status == "running")
            outcome = manager.cancel(record.id)
            await _until(
                lambda: any(
                    "kill -TERM -- -4242" in command
                    for command in spawner.ssh_commands()
                )
            )
            assert manager.get_job(record.id).status == "cancel_requested"
            manager.plane.state = "canceled"
            finished = await _terminal(manager, record.id)
            return (
                outcome,
                finished.status,
                any("kill -TERM -- -4242" in c for c in spawner.ssh_commands()),
            )

        outcome, status, cancelled_remotely = run(main())
        assert outcome == "cancel_requested"
        assert status == "canceled"
        assert cancelled_remotely

    def test_a_restart_leaves_remote_jobs_for_the_reconciler(self, spawner, tmp_path):
        async def main():
            store = JobStore(Database(":memory:"))
            plane = _ScriptedPlane(spawner)
            first = JobsManager(
                _App(str(tmp_path)),
                store=store,
                remote_runtime=RemoteJobsBridge(plane, poll_interval_s=0.01),
            )
            record = await first.create_job(
                inputs={"command": "sleep 1000"}, runtime="remote:hpc1"
            )
            await _until(lambda: first.get_job(record.id).status == "running")
            # A "restart": a second manager over the same store.
            second = JobsManager(
                _App(str(tmp_path)),
                store=store,
                remote_runtime=RemoteJobsBridge(plane, poll_interval_s=0.01),
            )
            mid_flight = second.get_job(record.id)
            assert mid_flight.status == "running"  # not interrupted
            plane.state = "done"
            await second.reconcile_remote_jobs()
            return second.get_job(record.id).status

        assert run(main()) == "succeeded"

    def test_reconcile_without_a_bridge_is_a_no_op(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            manager._remote_runtime = None
            return await manager.reconcile_remote_jobs()

        assert run(main())["checked"] == 0

    def test_an_unreachable_host_answers_unknown_and_keeps_the_row(self, spawner):
        class _Gone:
            async def status(self, handle: RemoteJobHandle) -> StatusReport:
                raise RemoteHostUnreachable("ssh exited 255")

        async def main():
            plane = _ScriptedPlane(spawner)
            store = plane.job_store()
            ref = store.record(
                RemoteJobHandle(
                    kind="nohup",
                    host="gone",
                    workdir="/w",
                    pgid=1,
                    submitted_at=1.0,
                    local_job_id="job-x",
                ),
                status="running",
            )
            from omicsclaw.remote.reconcile import refresh_in_flight

            broken = type(
                "_P",
                (),
                {
                    "job_store": lambda self: store,
                    "executor_for": lambda self, alias: _Gone(),
                },
            )()
            report = await refresh_in_flight(broken)
            row = store.get(str(ref))
            return report, row.status if row else "gone"

        report, status = run(main())
        assert report["checked"] == 1
        assert report["unknown"] == 1
        assert status == "unknown"


class TestRunEvidence:
    def test_external_cancellation_is_not_success(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi"}, runtime="remote:hpc1"
            )
            manager.plane.state = "canceled"
            await _terminal(manager, record.id)
            return manager.get_job(record.id).status

        assert run(main()) == "canceled"

    def test_remote_outputs_and_real_exit_evidence(self, spawner, tmp_path):
        async def main():
            manager = _manager(spawner, tmp_path)
            record = await manager.create_job(
                inputs={"command": "echo hi > out.txt", "outputs": ["out.txt"]},
                runtime="remote:hpc1",
                session_id="lineage",
            )
            manager.plane.state = "done"
            await _terminal(manager, record.id)
            return manager.run_store.list("artifact"), manager.run_store.list(
                "verification"
            )

        artifacts, checks = run(main())
        assert artifacts[0].uri.startswith("remote://hpc1/")
        assert artifacts[0].host_id == "hpc1"
        assert any(v.check == "exit_code" and v.verdict == "pass" for v in checks)
        assert any(
            v.check == "file_exists" and v.verdict == "inconclusive" for v in checks
        )


def test_two_remote_outputs_keep_distinct_retry_versions(spawner, tmp_path):
    async def main():
        manager = _manager(spawner, tmp_path)
        manager.plane.state = "done"
        for _ in range(2):
            record = await manager.create_job(
                inputs={"command": "echo hi", "outputs": ["a.txt", "b.txt"]},
                runtime="remote:hpc1",
                session_id="versions",
            )
            await _terminal(manager, record.id)
        return manager.run_store.list("artifact")

    artifacts = run(main())
    assert len({a.artifact_id for a in artifacts}) == 4
    for key in {a.lineage_key for a in artifacts}:
        versions = sorted(
            [a for a in artifacts if a.lineage_key == key], key=lambda a: a.version
        )
        assert [a.version for a in versions] == [1, 2]
        assert versions[1].parent_artifact_id == versions[0].artifact_id


def test_remote_kernel_run_location_uses_target_snapshot(spawner, tmp_path):
    async def main():
        manager = _manager(spawner, tmp_path)

        async def runner(record, ctx):
            return None

        manager.register_runner("code_run", runner)
        record = await manager.create_job(
            kind="code_run",
            inputs={"location": "remote:hpc1"},
            session_id="remote-code",
        )
        await _terminal(manager, record.id)
        return manager.run_store.load(manager._run_ids[record.id]).execution_location

    assert run(main()) == "remote:hpc1"
