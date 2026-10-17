"""Failure, lease, byte-budget and local-authentication regressions."""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import environment_gate, private_file
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.entry.ssh.controller import WorkspaceOwner
from omicsclaw.entry.ssh.notebook_ipc import controller_evidence
from omicsclaw.kernel.manager import PersistentKernelManager, SessionState
from omicsclaw.kernel.session import SessionKernel
from omicsclaw.tools.context import ProgressUpdate
from tests.entry.test_notebook_owner import Manager, observation, settled, setup


@pytest.mark.usefixtures("notebook_control_rss")
def test_long_silent_execution_renews_event_lease_without_observers(
    tmp_path, monkeypatch
):
    async def run():
        r, m, d = await setup(tmp_path, monkeypatch)
        try:
            r.service.events.lease_s = 0.15
            r.heartbeat.cancel()
            await asyncio.gather(r.heartbeat, return_exceptions=True)
            r.heartbeat = asyncio.create_task(r._heartbeat())
            m.wait = asyncio.Event()
            receipt = await r.admit({"command": d})
            await asyncio.sleep(0.65)
            assert (
                r.views.execution("nb", receipt["execution_id"])["status"] == "running"
            )
            m.wait.set()
            row = await settled(r, receipt["execution_id"])
            assert row["durability"] == "committed"
            assert (
                r.views.events("nb", receipt["execution_id"])["items"][-1]["type"]
                == "exchange_end"
            )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_queue_overflow_is_bounded_visible_and_prevents_new_admission(
    tmp_path, monkeypatch
):
    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        try:
            stream = r.stream("fixture", "pressure", r.service.events)
            for _ in range(10000):
                stream.publish(
                    TurnEvent.progress_update(
                        ProgressUpdate(tool_name="stdout", message="组" * 3000),
                        session_id="fixture",
                        turn_id="pressure",
                    )
                )
            assert len(stream.queue) <= 128 and stream.bytes <= 256 * 1024
            assert len(stream.stream._durable_tasks) <= 1
            assert r.durability_failed and stream.error
            assert await stream.flush_durable() is False
            with pytest.raises(NotebookError, match="notebook_durability_failed"):
                await r.admit({"command": d})
            await r.close_stream("pressure", stream)
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_sqlite_write_failure_blocks_future_admission(tmp_path, monkeypatch):
    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        try:
            original = r.service._transaction

            def full(work):
                raise sqlite3.OperationalError("database or disk is full")

            monkeypatch.setattr(r.service, "_transaction", full)
            with pytest.raises(sqlite3.OperationalError):
                await r.mutate("admit", {"command": d})
            assert r.durability_failed
            monkeypatch.setattr(r.service, "_transaction", original)
            with pytest.raises(NotebookError, match="notebook_durability_failed"):
                await r.admit({"command": d})
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_pending_receipt_is_dispatched_after_owner_recovery(tmp_path, monkeypatch):
    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        r.scheduler.cancel()
        await asyncio.gather(r.scheduler, return_exceptions=True)
        receipt = await r.admit({"command": d})
        await r.close()
        recovered = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="new",
            env={},
            manager=Manager(),
            registry_probe=observation,
        )
        await recovered.start()
        try:
            result = await settled(recovered, receipt["execution_id"])
            assert result["status"] == "completed"
            assert recovered.manager.codes == [d["source"]]
        finally:
            await recovered.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_schema_migration_keeps_legacy_data_without_invented_kernel_identity(
    tmp_path, monkeypatch
):
    from omicsclaw.memory.database import Database
    from omicsclaw.memory.notebook import NotebookCell, NotebookStore

    async def run():
        db = Database(tmp_path / ".omicsclaw/notebooks.db")
        await NotebookStore(db).put_cell(
            NotebookCell("legacy", "cell", source="old_data=1"), 0
        )
        db.close()
        r, _, _ = await setup(tmp_path, monkeypatch)
        try:
            snapshot = r.views.snapshot("legacy")
            assert snapshot["cells"][0]["source"] == "old_data=1"
            assert snapshot["kernel"] is None
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_environment_gate_serializes_acceptance_against_owner_probe(
    tmp_path, monkeypatch
):
    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        try:
            async with environment_gate(tmp_path):
                acceptance = asyncio.create_task(
                    asyncio.to_thread(r.registry.accept, d["environmentId"], 1)
                )
                await asyncio.sleep(0.1)
                assert not acceptance.done()
            assert (await acceptance)["acceptedRevision"] == 1
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_manual_policy_and_quotas_never_recycle_existing_kernel(tmp_path, monkeypatch):
    manager = PersistentKernelManager(tmp_path, manual_lifecycle=True)
    state = SessionState("old", born_at=1, cells=1000000, peak_rss_kb=10**12)
    manager._evaluate_restart_signals(state)
    manager._ensure_reaper()
    assert not state.restart_needed and manager._reaper_task is None

    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        try:
            r.limits["kernels"] = 1
            with pytest.raises(NotebookError, match="notebook_resource_limit"):
                await r.bind(
                    {
                        "notebookId": "other",
                        "sessionId": "other",
                        "commandId": "other",
                        "environmentId": d["environmentId"],
                        "environmentRevision": 1,
                    }
                )
            assert r.views.snapshot("nb")["kernel"]["generation"] == 1
            r.limits["diskBytes"] = 1
            with pytest.raises(NotebookError, match="notebook_resource_limit"):
                await r.admit({"command": d})
        finally:
            await r.close()

    asyncio.run(run())


def test_foreign_peer_nonce_symlink_and_signal_identity_are_rejected(
    tmp_path, monkeypatch
):
    owner = WorkspaceOwner(tmp_path, "a" * 32)
    try:
        record = json.loads((tmp_path / ".omicsclaw/controller.owner").read_text())
        claim = {
            "instanceId": record["instanceId"],
            "nonce": record["nonce"],
            "epoch": "b" * 32,
        }
        assert controller_evidence(tmp_path, os.getpid(), claim)["pid"] == os.getpid()
        with pytest.raises(NotebookError, match="notebook_controller_changed"):
            controller_evidence(tmp_path, os.getpid(), {**claim, "nonce": "wrong"})
        with pytest.raises(NotebookError, match="notebook_controller_changed"):
            controller_evidence(tmp_path, os.getppid(), claim)
        link = tmp_path / "unsafe"
        link.symlink_to(tmp_path / ".omicsclaw/controller.owner")
        with pytest.raises(OSError):
            private_file(link)
        calls = []
        monkeypatch.setattr(
            "omicsclaw.kernel.session.os.kill", lambda *args: calls.append(args)
        )
        monkeypatch.setattr(
            "omicsclaw.kernel.session.os.killpg", lambda *args: calls.append(args)
        )
        SessionKernel._kill_manager(
            SimpleNamespace(provisioner=SimpleNamespace(pid=os.getpid())),
            guard=lambda _: False,
        )
        assert calls == []
    finally:
        owner.close()


def test_owner_fence_survives_second_attach_and_concurrent_connection_change(
    tmp_path, monkeypatch
):
    from omicsclaw.entry.ssh.notebook_ipc import NotebookOwnerClient

    async def run():
        client = NotebookOwnerClient(tmp_path, {}, "instance", "epoch")
        old = {"ownerId": "old", "ownerEpoch": 1}
        connection = {"address": "original", "token": "original-token", "owner": old}

        async def attach():
            return {}, dict(connection)

        monkeypatch.setattr(client, "_attach_session", attach)
        with pytest.raises(NotebookError, match="notebook_owner_changed"):
            await client.call("bind", {}, expected_owner={**old, "ownerEpoch": 0})

        arrived = []

        class Writer:
            def write(self, raw):
                arrived.append(json.loads(raw))

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def connect(address, **kwargs):
            assert address == "original"
            reader = asyncio.StreamReader()
            reader.feed_data(b'{"result":{"ok":true}}\n')
            return reader, Writer()

        monkeypatch.setattr(
            "omicsclaw.entry.ssh.notebook_ipc.asyncio.open_unix_connection", connect
        )
        monkeypatch.setattr(client, "claim", lambda: {"instanceId": "original"})
        client._semaphore = asyncio.Semaphore(0)
        task = asyncio.create_task(client.call("bind", {}, expected_owner=old))
        await asyncio.sleep(0)
        client.address, client.token = "replacement", "replacement-token"
        connection.update(
            address="replacement",
            token="replacement-token",
            owner={"ownerId": "new", "ownerEpoch": 2},
        )
        client._semaphore.release()
        assert await task == {"ok": True}
        assert arrived[0]["owner"] == old and arrived[0]["token"] == "original-token"

    asyncio.run(run())


def test_ipc_server_rejects_wrong_owner_even_with_valid_controller_lease(
    tmp_path, monkeypatch
):
    from omicsclaw.entry.ssh.notebook_ipc import NotebookOwnerClient, OwnerServer

    async def run():
        owner = WorkspaceOwner(tmp_path, "a" * 32)
        runtime = SimpleNamespace(workspace=tmp_path, owner_id="new", epoch=2)
        handler = OwnerServer(runtime)
        address = "\0omicsclaw-test-" + os.urandom(10).hex()
        server = await asyncio.start_unix_server(handler.handle, path=address)
        client = NotebookOwnerClient(tmp_path, {}, "a" * 32, "b" * 32)
        try:
            handler.lease = {
                "claim": client.claim(),
                "pid": os.getpid(),
                "token": "fixture",
                "until": float("inf"),
            }
            with pytest.raises(NotebookError, match="notebook_owner_changed"):
                await client._request(
                    "bind",
                    {},
                    address=address,
                    token="fixture",
                    owner={"ownerId": "old", "ownerEpoch": 1},
                )
        finally:
            server.close()
            await server.wait_closed()
            owner.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_terminal_flush_recovery_retains_outcome_and_removes_busy_without_replay(
    tmp_path, monkeypatch, status
):
    async def run():
        r, _, doc = await setup(tmp_path, monkeypatch)
        r.scheduler.cancel()
        await asyncio.gather(r.scheduler, return_exceptions=True)
        receipt = await r.admit({"command": doc})
        eid = receipt["execution_id"]
        await r.service._state(
            "nb", eid, status, {"stdout": "saved-result", "event_stream_pending": True}
        )
        await r.close()
        manager = Manager()
        recovered = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="new",
            env={},
            manager=manager,
            registry_probe=observation,
        )
        await recovered.start()
        try:
            row = recovered.views.execution("nb", eid)
            assert row["status"] == status and row["output"]["stdout"] == "saved-result"
            assert (
                row["durability"] == "failed"
                and row["output"]["event_stream_pending"] is False
            )
            assert (await recovered.admit({"command": doc}))["execution_id"] == eid
            next_receipt = await recovered.admit(
                {"command": {**doc, "cellId": "next", "commandId": "next"}}
            )
            await settled(recovered, next_receipt["execution_id"])
            assert manager.codes == [doc["source"]]
        finally:
            await recovered.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_latest_execution_is_chronological_and_independent_of_history_cursor(
    tmp_path, monkeypatch
):
    async def run():
        r, _, doc = await setup(tmp_path, monkeypatch)
        try:
            old = await r.admit({"command": doc})
            await settled(r, old["execution_id"])
            latest = await r.admit(
                {"command": {**doc, "commandId": "next", "cellId": "next"}}
            )
            await settled(r, latest["execution_id"])
            snapshot = r.views.snapshot("nb")
            assert snapshot["activeExecution"] is None
            assert snapshot["latestExecution"]["execution_id"] == latest["execution_id"]
            assert "output" not in snapshot["latestExecution"]
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_historical_unknown_does_not_hide_current_generation(tmp_path, monkeypatch):
    async def run():
        r, _, doc = await setup(tmp_path, monkeypatch)
        r.scheduler.cancel()
        await asyncio.gather(r.scheduler, return_exceptions=True)
        old = await r.admit({"command": doc})
        r.db.run(
            lambda c: c.execute(
                "UPDATE notebook_runtime_executions SET dispatch_state='dispatching' WHERE execution_id=?",
                (old["execution_id"],),
            )
        )
        await r.close()
        manager = Manager()
        r = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="new",
            env={},
            manager=manager,
            registry_probe=observation,
        )
        await r.start()
        try:
            assert (
                r.views.snapshot("nb")["activeExecution"]["status"] == "outcome_unknown"
            )
            await r.release(
                {
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": doc["kernelInstanceId"],
                    "generation": 1,
                }
            )
            bound = await r.bind(
                {
                    "notebookId": "nb",
                    "sessionId": "chat",
                    "commandId": "rebind",
                    "environmentId": doc["environmentId"],
                    "environmentRevision": 1,
                }
            )
            manager.wait = asyncio.Event()
            new_doc = {
                **doc,
                "commandId": "new",
                "cellId": "new",
                "kernelInstanceId": bound["kernel"]["kernelInstanceId"],
                "generation": 2,
            }
            new = await r.admit({"command": new_doc})
            assert (
                r.views.snapshot("nb")["activeExecution"]["execution_id"]
                == new["execution_id"]
            )
            manager.wait.set()
            await settled(r, new["execution_id"])
            snapshot = r.views.snapshot("nb")
            assert snapshot["activeExecution"] is None
            assert snapshot["latestExecution"]["execution_id"] == new["execution_id"]
            assert (
                r.views.execution("nb", old["execution_id"])["status"]
                == "outcome_unknown"
            )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_slow_sqlite_append_keeps_one_writer_and_advances_only_committed_cursor(
    tmp_path, monkeypatch
):
    import threading

    async def run():
        r, _, doc = await setup(tmp_path, monkeypatch)
        entered, release = threading.Event(), threading.Event()
        original = r.service.events.append
        calls = []

        def held(event):
            calls.append(event.seq)
            entered.set()
            assert release.wait(3)
            return original(event)

        monkeypatch.setattr(r.service.events, "append", held)
        stream = r.stream("chat", "slow-write", r.service.events)
        try:

            def publish():
                stream.publish(
                    TurnEvent.progress_update(
                        ProgressUpdate(tool_name="stdout", message="组学" * 100),
                        session_id="chat",
                        turn_id="slow-write",
                    )
                )

            publish()
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            for _ in range(10000):
                publish()
            assert calls == [1] and len(stream.stream._durable_tasks) == 1
            assert stream.bytes <= 256 * 1024 and len(stream.queue) <= 128
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM turn_events WHERE turn_id='slow-write'"
                    ).fetchone()[0]
                )
                == 0
            )
            with pytest.raises(NotebookError, match="notebook_durability_failed"):
                await r.admit({"command": doc})
            release.set()
            assert await stream.flush_durable() is False
            rows = r.db.run(
                lambda c: c.execute(
                    "SELECT seq FROM turn_events WHERE turn_id='slow-write' ORDER BY seq LIMIT 130"
                ).fetchall()
            )
            assert [row[0] for row in rows] == list(range(1, len(rows) + 1)) and rows
        finally:
            release.set()
            await r.close_stream("slow-write", stream)
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_owner_records_execution_timeout_separately_from_user_cancel(
    tmp_path, monkeypatch
):
    from omicsclaw.kernel.session import CellResult

    async def run():
        r, manager, doc = await setup(tmp_path, monkeypatch)

        async def timed_out(*args, **kwargs):
            return CellResult(status="timeout")

        monkeypatch.setattr(manager, "execute", timed_out)
        try:
            receipt = await r.admit({"command": doc})
            record = await settled(r, receipt["execution_id"])
            assert record["status"] == "interrupted"
            assert record["output"]["termination_reason"] == "execution_timeout"
        finally:
            await r.close()

    asyncio.run(run())


def test_continuous_output_cannot_extend_the_guarded_execution_deadline(tmp_path):
    pytest.importorskip("ipykernel")
    import sys
    import time

    from omicsclaw.entry.notebook.security import process_evidence, same_process

    def register(pid):
        evidence = process_evidence(pid)
        return lambda candidate: candidate == pid and same_process(evidence)

    kernel = SessionKernel(
        "output-deadline",
        workspace=tmp_path,
        python=sys.executable,
        signal_guard_factory=register,
    )
    try:
        kernel.start()
        began = time.monotonic()
        result = kernel.execute(
            "import time\nfor index in range(100):\n print('pulse',flush=True)\n time.sleep(.02)",
            timeout_s=0.3,
        )
        assert result.status == "timeout"
        assert time.monotonic() - began < 1.5
        assert len(result.stdout.splitlines()) < 100
    finally:
        kernel.shutdown()


def test_guarded_shutdown_reaps_detached_child_after_root_exits(tmp_path):
    pytest.importorskip("ipykernel")
    import signal
    import sys
    import time

    from omicsclaw.entry.notebook.security import process_evidence, same_process

    def register(pid):
        birth = process_evidence(pid)
        return lambda candidate: candidate == pid and same_process(birth)

    kernel = SessionKernel(
        "descendants",
        workspace=tmp_path,
        python=sys.executable,
        signal_guard_factory=register,
    )
    child = None
    try:
        kernel.start()
        result = kernel.execute(
            "import subprocess,sys\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\nprint(p.pid)",
            timeout_s=10,
        )
        assert result.status == "ok", result.error
        child = process_evidence(int(result.stdout.strip()))
        assert child
        kernel.shutdown()
        for _ in range(100):
            if not same_process(child):
                break
            time.sleep(0.02)
        assert not same_process(child)
    finally:
        kernel.shutdown()
        if same_process(child):
            os.kill(child["pid"], signal.SIGKILL)


def test_sandbox_policy_roundtrip_refuses_secrets_and_changed_policy_attach(
    tmp_path, monkeypatch
):
    from omicsclaw.entry.notebook.policy import (
        decode_policy,
        encode_policy,
        policy_digest,
    )
    from omicsclaw.entry.ssh.notebook_ipc import NotebookOwnerClient, OwnerServer
    from omicsclaw.sandbox.config import EgressMode, SandboxConfig

    policy = SandboxConfig(
        egress=EgressMode.NONE,
        extra_read_paths=(tmp_path,),
        egress_allow=("example.org",),
    )
    assert encode_policy(decode_policy(encode_policy(policy))) == encode_policy(policy)
    assert decode_policy(encode_policy(None)) is None
    with pytest.raises(NotebookError, match="invalid_notebook_policy"):
        encode_policy(SandboxConfig(upstream_proxy="https://credential@proxy"))

    async def run():
        owner = WorkspaceOwner(tmp_path, "a" * 32)
        runtime = SimpleNamespace(
            workspace=tmp_path, identity={}, policy_digest=policy_digest(None)
        )
        address = "\0omicsclaw-policy-" + os.urandom(8).hex()
        server = await asyncio.start_unix_server(
            OwnerServer(runtime).handle, path=address
        )
        client = NotebookOwnerClient(
            tmp_path, {}, "a" * 32, "b" * 32, sandbox_config=policy
        )
        try:
            with pytest.raises(NotebookError, match="notebook_policy_changed"):
                await client._request(
                    "attach",
                    {"identity": {}, "policyDigest": client.policy_digest},
                    address=address,
                    token=None,
                    owner=None,
                )
        finally:
            server.close()
            await server.wait_closed()
            owner.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_explicit_native_policy_hides_owner_ipc_and_scrubs_scientific_credentials(
    tmp_path,
):
    pytest.importorskip("ipykernel")
    import sys

    from omicsclaw.sandbox.config import BackendName, EgressMode, SandboxConfig
    from omicsclaw.sandbox.manager import probe_sandbox_backend

    if not probe_sandbox_backend(BackendName.NETNS).available:
        pytest.skip("netns/PID namespace unavailable on this host")

    async def run():
        address = "\0omicsclaw-science-denied-" + os.urandom(8).hex()
        connected = []

        async def accept(reader, writer):
            connected.append(True)
            writer.close()

        server = await asyncio.start_unix_server(accept, path=address)
        r = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="fixture",
            env={**os.environ, "OPENAI_API_KEY": "fixture-private-secret"},
            config=SandboxConfig(backend=BackendName.NETNS, egress=EgressMode.NONE),
        )
        await r.start()
        try:
            env = await r.registry.register(
                {
                    "language": "python",
                    "executable": sys.executable,
                    "libraryPaths": [],
                    "requiredPackages": [],
                }
            )
            bound = await r.bind(
                {
                    "notebookId": "nb",
                    "sessionId": "chat",
                    "commandId": "bind",
                    "environmentId": env["environmentId"],
                    "environmentRevision": env["revision"],
                }
            )
            code = (
                "import socket,os\nassert os.environ.get('OPENAI_API_KEY') is None\ns=socket.socket(socket.AF_UNIX)\ntry:\n s.connect("
                + repr(address)
                + ")\nexcept OSError:\n print('IPC isolated')\nelse:\n raise AssertionError('owner endpoint reachable')\nfinally:\n s.close()"
            )
            doc = {
                "version": 1,
                "notebookId": "nb",
                "cellId": "cell",
                "commandId": "run",
                "source": code,
                "expectedRevision": 0,
                "language": "python",
                "origin": "user",
                "approvalScope": "explicit_user",
                "timeoutSeconds": 20,
                "preconditions": {},
                **{
                    k: bound["kernel"][k]
                    for k in (
                        "kernelInstanceId",
                        "generation",
                        "environmentId",
                        "environmentRevision",
                    )
                },
            }
            receipt = await r.admit({"command": doc})
            row = await settled(r, receipt["execution_id"])
            assert row["status"] == "completed", row
            assert "IPC isolated" in row["output"]["stdout"] and connected == []
            assert r.health()["sandbox"]["level"] == "net_only"
        finally:
            await r.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
