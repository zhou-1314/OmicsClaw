"""Post-commit actions preserve verified environments and independent restart results."""

import asyncio
import json
import sys

import pytest

from omicsclaw.entry.environment_coordination import CoordinationError
from omicsclaw.entry.maintenance_worker import MaintenanceWorker
from tests.entry.test_maintenance_adapter import available, wheel
from tests.entry.test_maintenance_service import approve, call, plan, wait_state
from tests.entry.test_notebook_environment_guard import fixture

_FAILED_OWNER = """
import asyncio, json, sys
from pathlib import Path
from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from tests.entry.test_maintenance_adapter import python_environment, wheel
from tests.entry.test_maintenance_service import plan, approve, wait_state
from tests.entry.test_notebook_r import bind
async def run():
    root = Path(sys.argv[1])
    environment = await python_environment(root)
    workspace = root / 'workspace'
    workspace.mkdir()
    owner = OwnerRuntime(workspace, {'authorityId': 'a'*32, 'workspaceId': '1'*32},
        source_digest='recovery-test', env={},
        environment_coordinator=EnvironmentCoordinator(root / 'global'))
    await owner.start()
    peer = None
    try:
        environment = await owner.registry.register(environment['spec'])
        kernel = (await bind(owner, 'python', environment))['kernel']
        if sys.argv[2] == 'peer':
            (root / 'peer').mkdir()
            peer = OwnerRuntime(root / 'peer', {'authorityId': 'a'*32, 'workspaceId': '2'*32},
                source_digest='recovery-test', env={},
                environment_coordinator=EnvironmentCoordinator(root / 'global'))
            await peer.start()
            selected = await peer.registry.register(environment['spec'])
            await bind(peer, 'python', selected)
        artifact = wheel(root, '0.0.3', code="raise RuntimeError('RECOVERY_IMPORT_FAILURE')\\n")
        document = (await plan(owner, environment, artifact, '0.0.3'))['maintenance']
        if sys.argv[2] == 'awaiting':
            failed = document
        else:
            await approve(owner, document)
            failed = await wait_state(owner, document['maintenanceId'], {'needs_repair'})
        (root / 'failed.json').write_text(json.dumps({'document': failed,
            'environment': environment, 'owner': owner.environment_guard.owner,
            'kernel': kernel}))
    finally:
        if peer is not None:
            await peer.close()
        await owner.close()
asyncio.run(run())
"""


def test_recovered_approval_cannot_retarget_the_original_verification_plan(tmp_path):
    from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
    from omicsclaw.entry.notebook.runtime import OwnerRuntime
    from omicsclaw.entry.notebook.security import same_process

    available()

    async def run():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _FAILED_OWNER,
            str(tmp_path),
            "awaiting",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(45):
            stdout, stderr = await child.communicate()
        assert child.returncode == 0, (stdout, stderr)
        previous = json.loads((tmp_path / "failed.json").read_text())
        assert not same_process(previous["owner"]["process"])
        owner = OwnerRuntime(
            tmp_path / "workspace",
            {"authorityId": "a" * 32, "workspaceId": "1" * 32},
            source_digest="recovery-test",
            env={},
            environment_coordinator=EnvironmentCoordinator(tmp_path / "global"),
        )
        await owner.start()
        try:
            store = owner.maintenance_service.store
            mid = previous["document"]["maintenanceId"]
            offered = store.public(mid)
            assert offered["state"] == "awaiting_approval"
            assert (
                offered["approval"]["requestId"]
                != previous["document"]["approval"]["requestId"]
            )
            with pytest.raises(CoordinationError, match="plan_stale"):
                await approve(owner, offered, "old-plan-new-approval")
            current, private = store.read(mid)
            assert current["state"] == "plan_stale" and not current["writeStarted"]
            assert (
                private["verificationTargets"][0]["owner_id"]
                == previous["owner"]["ownerId"]
            )
            assert not (
                await call(owner, "receipt", {"commandId": "old-plan-new-approval"})
            )["found"]
            new = (
                await plan(
                    owner,
                    previous["environment"],
                    wheel(tmp_path, "0.0.2"),
                    "0.0.2",
                    command_id="fresh-plan",
                )
            )["maintenance"]
            assert (
                store.read(new["maintenanceId"])[1]["verificationTargets"][0][
                    "owner_id"
                ]
                == owner.owner_id
            )
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("peer_recovery", [False, True])
def test_new_owner_repair_verifies_current_registry_without_retargeting_old_kernel(
    tmp_path,
    peer_recovery,
):
    from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
    from omicsclaw.entry.notebook.runtime import OwnerRuntime
    from omicsclaw.entry.notebook.security import same_process
    from tests.entry.test_notebook_owner import settled
    from tests.entry.test_notebook_r import bind, command

    available()

    async def run():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _FAILED_OWNER,
            str(tmp_path),
            "peer" if peer_recovery else "single",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(60):
            stdout, stderr = await child.communicate()
        assert child.returncode == 0, (stdout, stderr)
        previous = json.loads((tmp_path / "failed.json").read_text())
        assert not same_process(previous["owner"]["process"])
        owner = OwnerRuntime(
            tmp_path / "workspace",
            {"authorityId": "a" * 32, "workspaceId": "1" * 32},
            source_digest="recovery-test",
            env={},
            environment_coordinator=EnvironmentCoordinator(tmp_path / "global"),
        )
        await owner.start()
        peer = None
        try:
            if peer_recovery:
                peer = OwnerRuntime(
                    tmp_path / "peer",
                    {"authorityId": "a" * 32, "workspaceId": "2" * 32},
                    source_digest="recovery-test",
                    env={},
                    environment_coordinator=EnvironmentCoordinator(tmp_path / "global"),
                )
                await peer.start()
            failed = previous["document"]
            document = (
                await plan(
                    owner,
                    previous["environment"],
                    wheel(tmp_path, "0.0.2"),
                    "0.0.2",
                    command_id="repair",
                    repair=failed["maintenanceId"],
                )
            )["maintenance"]
            mid = document["maintenanceId"]
            await approve(owner, document, "approve-repair")
            final = await wait_state(
                owner,
                mid,
                {
                    "completed",
                    "restart_required",
                    "needs_repair",
                    "blocked_unconfirmed",
                },
            )
            assert (final.get("result") or {}).get("environmentVerified"), final
            assert final["state"] == "restart_required"
            assert not final["environmentFence"]["blocked"]
            _, private = owner.maintenance_service.store.read(mid)
            assert private["verificationTargets"][0]["owner_id"] == owner.owner_id
            own_restart = next(
                row
                for row in private["restartRows"]
                if row["workspaceId"] == owner.identity["workspaceId"]
            )
            assert own_restart["target"]["owner_id"] == previous["owner"]["ownerId"]
            if peer is not None:
                target = next(
                    row
                    for row in private["verificationTargets"]
                    if row["workspace_id"] == peer.identity["workspaceId"]
                )
                assert target["owner_id"] == peer.owner_id
                assert all(
                    row["status"] == "restart_required"
                    for row in private["restartRows"]
                )
            assert private["damageEpochs"] == dict(
                zip(
                    failed["environmentFence"]["maintenanceIds"],
                    failed["environmentFence"]["damageEpochs"],
                    strict=True,
                )
            )
            old_kernel = previous["kernel"]
            await owner.release(
                {
                    "notebookId": "nb",
                    "language": "python",
                    "commandId": "release-old",
                    "kernelInstanceId": old_kernel["kernelInstanceId"],
                    "generation": old_kernel["generation"],
                }
            )
            current = owner.registry.get(previous["environment"]["environmentId"])
            assert (
                current["acceptedRevision"]
                > previous["environment"]["acceptedRevision"]
            )
            new_kernel = (await bind(owner, "python", current, suffix="-after-repair"))[
                "kernel"
            ]
            execution = await owner.admit(
                {
                    "command": command(
                        new_kernel,
                        "read-repaired",
                        "import omicsclaw_maintenance_test as repaired; assert repaired.value == 2",
                    )
                }
            )
            assert (await settled(owner, execution["execution_id"]))[
                "status"
            ] == "completed"
            with pytest.raises(CoordinationError, match="unconfirmed"):
                await call(
                    owner,
                    "restart",
                    {
                        "maintenanceId": mid,
                        "commandId": "old-restart",
                        "restartId": own_restart["restartId"],
                        "notebookId": "nb",
                        "language": "python",
                        "expectedGeneration": old_kernel["generation"],
                    },
                )
            assert (
                owner.views.snapshot("nb")["kernel"]["kernelInstanceId"]
                == new_kernel["kernelInstanceId"]
            )
            if peer is not None:
                prior = next(
                    row
                    for row in private["restartRows"]
                    if row["workspaceId"] == peer.identity["workspaceId"]
                )
                await peer.release(
                    {
                        "notebookId": "nb",
                        "language": "python",
                        "commandId": "peer-release",
                        "kernelInstanceId": prior["previousKernelInstanceId"],
                        "generation": prior["previousGeneration"],
                    }
                )
                selected = peer.registry.get(prior["environment"]["environmentId"])
                bound = (await bind(peer, "python", selected, suffix="-recovered"))[
                    "kernel"
                ]
                receipt = await peer.admit(
                    {
                        "command": command(
                            bound,
                            "peer-import",
                            "import omicsclaw_maintenance_test as repaired; assert repaired.value == 2",
                        )
                    }
                )
                assert (await settled(peer, receipt["execution_id"]))[
                    "status"
                ] == "completed"
        finally:
            if peer is not None:
                await peer.close()
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancel", "owner_close"])
def test_cancel_after_real_commit_is_refused_and_restarts_finish(
    tmp_path, monkeypatch, stop
):
    available()

    async def run():
        _coordinator, owners = await fixture(tmp_path, real=True)
        entered, resume = asyncio.Event(), asyncio.Event()
        original_restarts = MaintenanceWorker.restarts

        async def held_restarts(worker, mid, **kwargs):
            entered.set()
            await resume.wait()
            await original_restarts(worker, mid, **kwargs)

        monkeypatch.setattr(MaintenanceWorker, "restarts", held_restarts)
        owner, env, _ = owners[0]
        try:
            document = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            await approve(owner, document)
            await asyncio.wait_for(entered.wait(), 45)
            service = owner.maintenance_service
            mid = document["maintenanceId"]
            committed = service.store.public(mid)
            assert committed["state"] == "environment_committed"
            assert committed["result"]["environmentVerified"]
            if stop == "owner_close":
                await service.close()
                stopped = service.store.public(mid)
                assert stopped["state"] == "restart_required"
                assert stopped["result"]["environmentVerified"]
                assert not stopped["environmentFence"]["blocked"]
                assert service.tasks[mid].cancelled()
                return
            with pytest.raises(CoordinationError, match="invalid_state"):
                await call(
                    owner,
                    "cancel",
                    {
                        "maintenanceId": mid,
                        "commandId": "too-late-cancel",
                        "planRevision": document["planRevision"],
                        "planDigest": document["planDigest"],
                        "confirmation": "cancel_maintenance",
                    },
                )
            assert not service.tasks[mid].done()
            assert not (await call(owner, "receipt", {"commandId": "too-late-cancel"}))[
                "found"
            ]
            assert not service.store.public(mid).get("cancelRequested")
            resume.set()
            final = await wait_state(
                owner, mid, {"completed", "restart_required", "needs_repair"}
            )
            assert final["state"] == "completed", final
            assert all(row["status"] == "ready" for row in final["result"]["restarts"])
            assert not final["environmentFence"]["blocked"]
        finally:
            resume.set()
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("stop", [False, True])
def test_concurrent_restart_results_merge_by_identity(tmp_path, monkeypatch, stop):
    from tests.entry.test_maintenance_boundaries import fake_prepare

    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        try:
            document = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            mid = document["maintenanceId"]
            service = owner.maintenance_service
            document, private = service.store.read(mid)
            # Seed a committed operation; hold only the two mailbox responses.
            rows = [
                {
                    "restartId": f"restart-{index}",
                    "workspaceId": row["workspace_id"],
                    "notebookId": "nb",
                    "language": "python",
                    "previousGeneration": 1,
                    "previousKernelInstanceId": row["resource_id"],
                    "kernelInstanceId": None,
                    "generation": None,
                    "status": "restart_required",
                    "reason": "",
                    "environment": {},
                    "target": row,
                }
                for index, row in enumerate(private["restartImpact"])
                if row["kind"] == "kernel"
            ]
            assert len(rows) == 2
            service.store.private_update(mid, restartRows=rows, committedEpoch=2**60)
            service.store.update(
                mid,
                state="restart_required",
                writeStarted=True,
                result={
                    "environmentVerified": True,
                    "environmentRevision": 2,
                    "verification": {},
                    "restarts": MaintenanceWorker.public_restarts(rows),
                },
            )
            entered = {row["restartId"]: asyncio.Event() for row in rows}
            release = {row["restartId"]: asyncio.Event() for row in rows}

            async def reply(worker, operation, target, kind, payload):
                if kind == "check_restart":
                    return {"current": True}
                assert operation == mid and kind == "restart"
                rid = payload["restartId"]
                entered[rid].set()
                await release[rid].wait()
                return {
                    "kernelInstanceId": f"new-{rid}",
                    "generation": 2,
                    "status": "ready",
                    "reason": "",
                }

            monkeypatch.setattr(MaintenanceWorker, "request", reply)
            tasks = []
            for index, row in enumerate(rows):
                await call(
                    owner,
                    "restart",
                    {
                        "maintenanceId": mid,
                        "commandId": f"retry-{index}",
                        "restartId": row["restartId"],
                        "notebookId": "nb",
                        "language": "python",
                        "expectedGeneration": 1,
                    },
                )
                tasks.append(service.tasks[mid])
                await asyncio.wait_for(entered[row["restartId"]].wait(), 2)
            assert set(tasks) == service.work_tasks
            if stop:
                await service.close()
                assert all(task.cancelled() for task in tasks)
                assert not service.work_tasks
                assert service.store.public(mid)["state"] == "restart_required"
                return
            for row, task in zip(rows, tasks, strict=True):
                release[row["restartId"]].set()
                await task
            assert not service.work_tasks
            final, saved = service.store.read(mid)
            assert [row["status"] for row in final["result"]["restarts"]] == [
                "ready",
                "ready",
            ]
            assert [row["status"] for row in saved["restartRows"]] == ["ready", "ready"]
            assert (
                final["state"] == "completed" and final["result"]["environmentVerified"]
            )
            with coordinator.transaction() as c:
                events = [
                    json.loads(row[0])
                    for row in c.execute(
                        "SELECT document_json FROM maintenance_events WHERE maintenance_id=?",
                        (mid,),
                    )
                ]
            assert {
                event["restartId"]
                for event in events
                if event["kind"] == "kernel_restart"
            } == set(entered)
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_owner_close_stops_auto_restart_and_overlapping_explicit_retry(
    tmp_path, monkeypatch
):
    available()

    async def run():
        _coordinator, owners = await fixture(tmp_path, real=True)
        entered = {"auto": asyncio.Event(), "retry": asyncio.Event()}
        tasks = []

        async def held_restarts(worker, mid, *, only=None):
            entered["retry" if only else "auto"].set()
            await asyncio.Future()

        monkeypatch.setattr(MaintenanceWorker, "restarts", held_restarts)
        owner, env, _ = owners[0]
        try:
            document = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            await approve(owner, document)
            service = owner.maintenance_service
            mid = document["maintenanceId"]
            tasks.append(service.tasks[mid])
            await asyncio.wait_for(entered["auto"].wait(), 45)
            committed = service.store.public(mid)
            assert committed["result"]["environmentVerified"]
            selected = committed["result"]["restarts"][0]
            await call(
                owner,
                "restart",
                {
                    "maintenanceId": mid,
                    "commandId": "overlapping-retry",
                    "restartId": selected["restartId"],
                    "notebookId": selected["notebookId"],
                    "language": selected["language"],
                    "expectedGeneration": selected["previousGeneration"],
                },
            )
            tasks.append(service.tasks[mid])
            assert tasks[0] is not tasks[1]
            await asyncio.wait_for(entered["retry"].wait(), 5)
            assert set(tasks) == service.work_tasks
            await asyncio.wait_for(service.close(), 5)
            assert all(task.cancelled() for task in tasks)
            assert not service.work_tasks
            final = service.store.public(mid)
            assert final["state"] == "restart_required"
            assert final["result"]["environmentVerified"]
            assert not final["environmentFence"]["blocked"]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())
