"""Maintenance approval, state preservation, and observer budgets at real owner boundaries."""

import asyncio
import json

import pytest

from omicsclaw.entry.environment_coordination import CoordinationError
from omicsclaw.entry.environment_scopes import (
    registered_read_roots,
    registered_write_roots,
)
from tests.entry.test_environment_coordination import reservation
from tests.entry.test_maintenance_adapter import wheel
from tests.entry.test_maintenance_service import approve, call, plan, wait_state
from tests.entry.test_notebook_environment_guard import fixture
from tests.entry.test_notebook_owner import settled
from tests.entry.test_notebook_r import command


async def fake_prepare(environment, operation, directory, **kwargs):
    return {
        "adapter": "python_wheels",
        "packages": [],
        "artifacts": [],
        "installer": {
            "executable": environment["spec"]["executable"],
            "version": "fixture",
            "argv": [],
        },
        "writeRoots": registered_write_roots(environment),
        "readRoots": registered_read_roots(environment),
        "network": "deny",
        "privileges": "current_user",
        "budgets": {"timeoutSeconds": 300, "outputBytes": 1048576},
        "permission": {"writable": True, "freeBytes": 100000000, "reason": ""},
    }, {
        "artifacts": [],
        "environment": environment,
        "operation": operation,
        "baselineDependencyErrors": [],
    }


def test_analysis_checkpoint_barrier_cancel_preserves_kernel_and_does_not_install(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def run():
        _coordinator, owners = await fixture(tmp_path)
        owner, env, kernel = owners[0]
        try:
            receipt = await owner.admit(
                {"command": command(kernel, "state", "marker = 42")}
            )
            assert (await settled(owner, receipt["execution_id"]))[
                "status"
            ] == "completed"
            initial = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            await approve(owner, initial)
            blocked = await wait_state(
                owner, initial["maintenanceId"], {"checkpoint_required"}
            )
            assert (
                not blocked["writeStarted"] and blocked["environmentFence"]["blocked"]
            )
            assert owner.manager.codes == ["marker = 42"]
            assert (
                owner.views.snapshot("nb")["kernel"]["kernelInstanceId"]
                == kernel["kernelInstanceId"]
            )
            await call(
                owner,
                "cancel",
                {
                    "maintenanceId": initial["maintenanceId"],
                    "commandId": "cancel",
                    "planRevision": initial["planRevision"],
                    "planDigest": initial["planDigest"],
                    "confirmation": "cancel_maintenance",
                },
            )
            cancelled = await wait_state(
                owner, initial["maintenanceId"], {"cancelled_before_mutation"}
            )
            assert not cancelled["environmentFence"]["blocked"]
            next_cell = await owner.admit(
                {"command": command(kernel, "next", "print(marker)")}
            )
            assert (await settled(owner, next_cell["execution_id"]))[
                "status"
            ] == "completed"
            assert owner.manager.codes == ["marker = 42", "print(marker)"]
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_new_workspace_reservation_invalidates_fixed_approval_before_fence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        try:
            initial = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            coordinator.reserve(
                reservation(
                    "new-reader", [tmp_path / "science"], workspace="new-workspace"
                )
            )
            with pytest.raises(CoordinationError, match="plan_stale"):
                await approve(owner, initial)
            value = owner.maintenance_service.store.public(initial["maintenanceId"])
            assert value["state"] == "plan_stale" and not value["writeStarted"]
            assert not value["environmentFence"]["blocked"]
            assert not (await call(owner, "receipt", {"commandId": "approve"}))["found"]
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_unicode_event_pages_are_sql_bounded_and_cursors_do_not_cross_operations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def run():
        _coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        try:
            first = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            second = (
                await plan(
                    owner, env, wheel(tmp_path, "0.0.4"), "0.0.4", command_id="second"
                )
            )["maintenance"]
            store = owner.maintenance_service.store
            for index in range(50):
                store.update(
                    first["maintenanceId"],
                    event={
                        "kind": "process_output",
                        "phase": "install",
                        "text": "😀" * 512,
                        "chunkIndex": index,
                        "truncated": False,
                    },
                )
            cursor = None
            events = []
            seen = set()
            while True:
                page = await call(
                    owner,
                    "events",
                    {
                        "maintenanceId": first["maintenanceId"],
                        "limit": 40,
                        "cursor": cursor,
                    },
                )
                assert len(json.dumps(page).encode()) <= 192 * 1024
                assert all(
                    len(json.dumps(item).encode()) <= 8192 for item in page["items"]
                )
                events.extend(page["items"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                assert cursor not in seen
                seen.add(cursor)
                with pytest.raises(CoordinationError, match="invalid_page"):
                    await call(
                        owner,
                        "events",
                        {"maintenanceId": second["maintenanceId"], "cursor": cursor},
                    )
                with pytest.raises(CoordinationError, match="invalid_page"):
                    await call(
                        owner,
                        "impact",
                        {"maintenanceId": first["maintenanceId"], "cursor": cursor},
                    )
            outputs = [event for event in events if event["kind"] == "process_output"]
            assert [event["chunkIndex"] for event in outputs] == list(range(50))
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("crash_stage", ["awaiting_approval", "approved", "installing"])
def test_owner_recovery_preserves_original_damage_at_each_repair_window(
    tmp_path, monkeypatch, crash_stage
):
    from omicsclaw.entry.maintenance import MaintenanceService

    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)
    # These tests seed durable failure records; actual installer failures are covered separately.
    monkeypatch.setattr("omicsclaw.entry.maintenance.contained_exit", lambda *_: True)

    async def hold_worker(*args):
        await asyncio.Future()

    monkeypatch.setattr(
        "omicsclaw.entry.maintenance_worker.MaintenanceWorker.run", hold_worker
    )

    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        try:
            original = (await plan(owner, env, wheel(tmp_path, "0.0.3"), "0.0.3"))[
                "maintenance"
            ]
            await approve(owner, original)
            store = owner.maintenance_service.store
            store.update(
                original["maintenanceId"], state="needs_repair", writeStarted=True
            )
            store.private_update(
                original["maintenanceId"], activeProcess={"birth": {}, "proof": {}}
            )
            for runtime, _, kernel in owners:
                await runtime.release(
                    {
                        "notebookId": "nb",
                        "commandId": "stopped-before-install",
                        "kernelInstanceId": kernel["kernelInstanceId"],
                        "generation": kernel["generation"],
                    }
                )
            for phase in (
                "checkpoint_barrier",
                "stopping_old_kernels",
                "installing",
                "needs_repair",
            ):
                coordinator.advance_fence(
                    original["maintenanceId"], owner.environment_guard.owner, phase
                )
            original_damage = store.public(original["maintenanceId"])[
                "environmentFence"
            ]["damageEpochs"]
            repair = (
                await plan(
                    owner,
                    env,
                    wheel(tmp_path, "0.0.2"),
                    "0.0.2",
                    command_id="repair",
                    repair=original["maintenanceId"],
                )
            )["maintenance"]
            old_approval = repair["approval"]["requestId"]
            if crash_stage != "awaiting_approval":
                await approve(owner, repair, "approve-repair")
            if crash_stage == "installing":
                store.update(
                    repair["maintenanceId"], state="installing", writeStarted=True
                )
                for phase in (
                    "checkpoint_barrier",
                    "stopping_old_kernels",
                    "installing",
                ):
                    coordinator.advance_fence(
                        repair["maintenanceId"], owner.environment_guard.owner, phase
                    )
            await owner.maintenance_service.close()
            with coordinator.transaction() as c:
                c.execute(
                    "UPDATE maintenance_records SET owner_id='dead-owner',owner_process_json=? WHERE maintenance_id=?",
                    (
                        json.dumps(
                            {
                                **owner.environment_guard.owner["process"],
                                "pid": 2147483647,
                            }
                        ),
                        repair["maintenanceId"],
                    ),
                )
            recovered = owner.maintenance_service = MaintenanceService(owner)
            await recovered.start()
            current = recovered.store.public(repair["maintenanceId"])
            assert current["environmentFence"]["blocked"]
            assert set(original_damage) <= set(
                current["environmentFence"]["damageEpochs"]
            )
            assert (
                original["maintenanceId"]
                in current["environmentFence"]["maintenanceIds"]
            )
            assert (
                recovered.store.public(original["maintenanceId"])["state"]
                == "needs_repair"
            )
            if crash_stage == "awaiting_approval":
                assert current["state"] == "awaiting_approval"
                assert current["approval"]["requestId"] != old_approval
                with pytest.raises(CoordinationError, match="approval_expired"):
                    await approve(owner, repair, "old-request")
            elif crash_stage == "approved":
                assert current["state"] == "cancelled_before_mutation"
                assert current["environmentFence"]["damageEpochs"] == original_damage
            else:
                assert current["state"] == "needs_repair"
                assert len(current["environmentFence"]["damageEpochs"]) == 2
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_changed_registered_inventory_invalidates_approval_before_install(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        try:
            first = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            original_probe = owner.registry.probe

            async def changed(*args, **kwargs):
                observed = await original_probe(*args, **kwargs)
                return {**observed, "packages": {"ipykernel": "2"}}

            owner.registry.probe = changed
            with pytest.raises(CoordinationError, match="plan_stale"):
                await approve(owner, first)
            current = owner.maintenance_service.store.public(first["maintenanceId"])
            assert current["state"] == "plan_stale"
            assert (
                not current["environmentFence"]["blocked"]
                and not current["writeStarted"]
            )
            assert not (await call(owner, "receipt", {"commandId": "approve"}))["found"]
            assert len(coordinator.impact(first["plan"]["writeRoots"])) == 2
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())
