"""Owner admission, recovery and transport boundaries over real durable records."""

import asyncio
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from omicsclaw.entry.notebook.contracts import MAX_RESPONSE_BYTES, command, encoded
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import same_process
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.kernel.session import CellResult


class Sandbox:
    backend_name = "fixture"
    level = "off"

    def __init__(self, *args, **kwargs):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def as_kernel_spawn_wrapper(self):
        return None


class Manager:
    def __init__(self):
        self.states = {}
        self.codes = []
        self.wait = None

    def snapshot(self):
        return []

    async def execute(self, session, code, **kwargs):
        self.codes.append(code)
        if self.wait:
            await self.wait.wait()
        return CellResult(
            status="interrupted" if kwargs["cancelled"]() else "ok", stdout="done"
        )

    async def cancel(self, *args, **kwargs):
        if self.wait:
            self.wait.set()
        return "interrupted"

    async def aclose(self):
        pass


async def observation(spec, workspace, **kwargs):
    return {
        "version": "3.11",
        "prefix": "/fixture/env",
        "executable": spec["executable"],
        "libraryPaths": ["/fixture/env/lib"],
        "packages": {"ipykernel": "1"},
        "missing": [],
        "maintenanceRoots": ["/fixture/env"],
    }


async def setup(tmp_path, monkeypatch, **options):
    monkeypatch.setattr("omicsclaw.entry.notebook.runtime.SandboxManager", Sandbox)
    manager = Manager()
    runtime = OwnerRuntime(
        tmp_path,
        {"authorityId": "a" * 32, "workspaceId": "b" * 32},
        source_digest="fixture",
        env={},
        manager=manager,
        registry_probe=observation,
        **options,
    )
    await runtime.start()
    env = await runtime.registry.register(
        {
            "language": "python",
            "executable": "/fixture/python",
            "libraryPaths": [],
            "requiredPackages": [],
        }
    )
    bound = await runtime.bind(
        {
            "notebookId": "nb",
            "sessionId": "chat",
            "commandId": "bind",
            "environmentId": env["environmentId"],
            "environmentRevision": env["revision"],
        }
    )
    kernel = bound["kernel"]
    document = {
        "version": 1,
        "notebookId": "nb",
        "cellId": "cell",
        "commandId": "run",
        "source": "print('first')",
        "expectedRevision": 0,
        "language": "python",
        "origin": "user",
        "approvalScope": "explicit_user",
        "timeoutSeconds": 20,
        "preconditions": {},
        **{
            k: kernel[k]
            for k in (
                "kernelInstanceId",
                "generation",
                "environmentId",
                "environmentRevision",
            )
        },
    }
    return runtime, manager, document


async def settled(runtime, eid):
    for _ in range(300):
        row = runtime.views.execution("nb", eid)
        if (
            row["status"] in {"completed", "failed", "interrupted"}
            and row["durability"] != "pending"
        ):
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not settle")


@pytest.mark.usefixtures("notebook_control_rss")
def test_digest_exact_semantics_and_valid_retry_after_release(tmp_path, monkeypatch):
    async def run():
        r, m, d = await setup(tmp_path, monkeypatch)
        try:
            first, second = await asyncio.gather(
                r.admit({"command": d}),
                r.admit({"command": dict(reversed(list(d.items())))}),
            )
            assert first["execution_id"] == second["execution_id"]
            await settled(r, first["execution_id"])
            assert m.codes == [d["source"]]
            for key, value in (
                ("source", "print('different')"),
                ("timeoutSeconds", 19),
                ("generation", 2),
                ("environmentRevision", 2),
                ("cellId", "other"),
            ):
                with pytest.raises(NotebookError, match="command_conflict"):
                    await r.mutate("admit", {"command": {**d, key: value}})
            legal = await r.mutate(
                "admit", {"command": {**d, "commandId": "legal", "cellId": "next"}}
            )
            assert (await settled(r, legal["execution_id"]))["status"] == "completed"
            await r.release(
                {
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": d["kernelInstanceId"],
                    "generation": 1,
                }
            )
            assert (await r.admit({"command": d}))["execution_id"] == first[
                "execution_id"
            ]

            def denied():
                raise NotebookError("notebook_controller_changed", 409)

            with pytest.raises(NotebookError, match="notebook_controller_changed"):
                await r.mutate("admit", {"command": d}, denied)
            assert len(m.codes) == 2
            assert r.durability_failed is False
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_cancel_observation_does_not_cancel_durable_admission(tmp_path, monkeypatch):
    async def run():
        r, m, d = await setup(tmp_path, monkeypatch)
        try:
            entered, leave = asyncio.Event(), asyncio.Event()
            original = r.registry._preflight

            async def paused(eid):
                entered.set()
                await leave.wait()
                return await original(eid)

            monkeypatch.setattr(r.registry, "_preflight", paused)
            task = asyncio.create_task(r.mutate("admit", {"command": d}))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            leave.set()
            await asyncio.gather(*tuple(r.background))
            row = await r.admit({"command": d})
            assert (await settled(r, row["execution_id"]))["status"] == "completed"
            assert m.codes == [d["source"]]
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_pending_cancel_never_runs_code_and_old_generation_cannot_cancel(
    tmp_path, monkeypatch
):
    async def run():
        r, m, d = await setup(tmp_path, monkeypatch)
        try:
            r.scheduler.cancel()
            await asyncio.gather(r.scheduler, return_exceptions=True)
            row = await r.admit({"command": d})
            eid = row["execution_id"]
            cancel = {
                "notebookId": "nb",
                "executionId": eid,
                "commandId": "cancel",
                "kernelInstanceId": d["kernelInstanceId"],
                "generation": 1,
            }
            with pytest.raises(NotebookError, match="notebook_generation_changed"):
                await r.cancel({**cancel, "generation": 2})
            assert (await r.cancel(cancel))["status"] == "cancel_requested"
            r.scheduler = asyncio.create_task(r._schedule())
            r.wake()
            assert (await settled(r, eid))["status"] == "interrupted"
            assert m.codes == []
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_environment_drift_prevents_admission_and_accept_live_switch(
    tmp_path, monkeypatch
):
    async def run():
        r, m, d = await setup(tmp_path, monkeypatch)
        try:

            async def changed(*args, **kwargs):
                return {**await observation(*args, **kwargs), "version": "3.12"}

            r.registry.probe = changed
            with pytest.raises(ValueError, match="environment_revision_conflict"):
                await r.admit({"command": d})
            row = r.registry.get(d["environmentId"])
            assert row["status"] == "changed" and row["revision"] == 2
            with pytest.raises(ValueError, match="environment_busy"):
                await asyncio.to_thread(r.registry.accept, d["environmentId"], 2)
            assert m.codes == []
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_pending_dispatch_survives_owner_restart_but_uncertain_send_is_not_replayed(
    tmp_path, monkeypatch
):
    async def run():
        r, _m, d = await setup(tmp_path, monkeypatch)
        r.scheduler.cancel()
        await asyncio.gather(r.scheduler, return_exceptions=True)
        row = await r.admit({"command": d})
        eid = row["execution_id"]
        # Simulate the durable send boundary without sending scientific code.
        r.db.run(
            lambda c: c.execute(
                "UPDATE notebook_runtime_executions SET dispatch_state='dispatching' WHERE execution_id=?",
                (eid,),
            )
        )
        await r.close()
        r2 = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="new",
            env={},
            manager=Manager(),
            registry_probe=observation,
        )
        await r2.start()
        try:
            old = r2.views.execution("nb", eid)
            assert (
                old["status"] == "outcome_unknown" and old["dispatchState"] == "unknown"
            )
            assert (await r2.admit({"command": d}))["status"] == "outcome_unknown"
            await asyncio.sleep(0.05)
            assert r2.manager.codes == []
        finally:
            await r2.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_pagination_is_byte_bounded_and_cursor_store_scoped(tmp_path, monkeypatch):
    async def run():
        r, _m, d = await setup(tmp_path, monkeypatch)
        try:
            for index in range(3):
                doc = {
                    **d,
                    "commandId": f"run{index}",
                    "cellId": f"cell{index}",
                    "source": "#" + "组" * 20000,
                }
                row = await r.admit({"command": doc})
                await settled(r, row["execution_id"])
            snap = r.views.snapshot("nb", limit=40)
            assert len(encoded(snap)) <= MAX_RESPONSE_BYTES
            assert len(snap["cells"]) == 1 and snap["cellCursor"] is not None
            second = r.views.snapshot("nb", cell_cursor=snap["cellCursor"])
            assert second["cells"][0]["cell_id"] != snap["cells"][0]["cell_id"]
            with pytest.raises(NotebookError, match="notebook_cursor_mismatch"):
                r.views.snapshot(
                    "nb",
                    cell_cursor={
                        **snap["cellCursor"],
                        "store": {"authorityId": "wrong"},
                    },
                )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
@pytest.mark.parametrize(
    "change",
    [
        {"timeoutSeconds": float("nan")},
        {"extra": True},
        {"origin": "agent"},
        {"preconditions": {"watermark": 3}},
        {"source": "x" * 65537},
    ],
)
def test_command_schema_rejects_unapproved_semantics(tmp_path, monkeypatch, change):
    async def run():
        r, _, d = await setup(tmp_path, monkeypatch)
        try:
            with pytest.raises(NotebookError):
                command({**d, **change})
        finally:
            await r.close()

    asyncio.run(run())


def test_true_detached_owner_keeps_kernel_across_controller_close(tmp_path):
    pytest.importorskip("ipykernel")
    from omicsclaw.sandbox.manager import probe_sandbox_backend

    if not probe_sandbox_backend().available:
        pytest.skip("native kernel sandbox unavailable")
    from tests.entry.notebook_process_fixture import run_controller_scenario

    run_controller_scenario(__name__, "_detached_owner_scenario", tmp_path)


def _detached_owner_scenario(tmp_path):
    from omicsclaw.entry.ssh.controller import Controller

    async def configure():
        c = Controller(tmp_path, SimpleNamespace(handlers={}))
        await c.configure(
            {
                "provider_id": "test",
                "dialect": "openai_chat",
                "model": "test",
                "allowed_models": ["test"],
                "context_tokens": 8192,
                "output_tokens": 1024,
            }
        )
        return c

    async def run():
        c = await configure()
        second = None
        health = None
        try:
            environment = (
                await c.environment_register(
                    {
                        "target": c._environment_target(),
                        "spec": {
                            "language": "python",
                            "executable": sys.executable,
                            "libraryPaths": [],
                            "requiredPackages": [],
                        },
                    }
                )
            )["environment"]
            health = await c.notebook_request(
                "attach", {"target": c._environment_target()}
            )
            fence = health["writeFence"]
            bound = await c.notebook_request(
                "bind",
                {
                    "target": fence,
                    "notebookId": "nb",
                    "sessionId": "chat",
                    "commandId": "bind",
                    "environmentId": environment["environmentId"],
                    "environmentRevision": environment["revision"],
                },
            )
            k = bound["kernel"]
            document = {
                "version": 1,
                "notebookId": "nb",
                "cellId": "cell",
                "commandId": "run",
                "source": "import time\nvalue=73\nprint('started',flush=True)\ntime.sleep(2)\nprint(value)",
                "expectedRevision": 0,
                "language": "python",
                "origin": "user",
                "approvalScope": "explicit_user",
                "timeoutSeconds": 20,
                "preconditions": {},
                **{
                    key: k[key]
                    for key in (
                        "kernelInstanceId",
                        "generation",
                        "environmentId",
                        "environmentRevision",
                    )
                },
            }
            receipt = await c.notebook_request(
                "admit", {"target": fence, "command": document}
            )
            await c.aclose()
            assert same_process(health["owner"]["process"])
            second = await configure()
            new = await second.notebook_request(
                "attach", {"target": second._environment_target()}
            )
            assert new["owner"] == health["owner"]
            assert new["writeFence"] != fence
            with pytest.raises(Exception, match="notebook_target_changed"):
                await second.notebook_request(
                    "admit", {"target": fence, "command": document}
                )
            duplicate = await second.notebook_request(
                "admit", {"target": new["writeFence"], "command": document}
            )
            assert duplicate["execution_id"] == receipt["execution_id"]
            for _ in range(300):
                result = await second.notebook_request(
                    "execution",
                    {
                        "target": new["writeFence"],
                        "notebookId": "nb",
                        "executionId": receipt["execution_id"],
                    },
                )
                if (
                    result["status"] == "completed"
                    and result["durability"] == "committed"
                ):
                    break
                await asyncio.sleep(0.05)
            assert result["output"]["stdout"] == "started\n73\n"
            next_doc = {
                **document,
                "cellId": "second",
                "commandId": "second",
                "source": "print(value+1)",
            }
            follow = await second.notebook_request(
                "admit", {"target": new["writeFence"], "command": next_doc}
            )
            for _ in range(200):
                result = await second.notebook_request(
                    "execution",
                    {
                        "target": new["writeFence"],
                        "notebookId": "nb",
                        "executionId": follow["execution_id"],
                    },
                )
                if (
                    result["status"] == "completed"
                    and result["durability"] == "committed"
                ):
                    break
                await asyncio.sleep(0.05)
            assert result["output"]["stdout"] == "74\n"
            released = await second.notebook_request(
                "release",
                {
                    "target": new["writeFence"],
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": k["kernelInstanceId"],
                    "generation": k["generation"],
                },
            )
            assert released["status"] == "released"
        finally:
            if second:
                await second.aclose()
            elif not c.notebook_client.closed:
                await c.aclose()
            if health and same_process(health["owner"]["process"]):
                os.kill(health["owner"]["process"]["pid"], signal.SIGTERM)
                for _ in range(100):
                    if not same_process(health["owner"]["process"]):
                        break
                    await asyncio.sleep(0.05)

    asyncio.run(run())
