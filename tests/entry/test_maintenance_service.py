"""Actual owner-held maintenance across two workspaces and disposable environments."""

import asyncio
import json

from tests.entry.test_maintenance_adapter import available, python_operation, wheel
from tests.entry.test_notebook_environment_guard import fixture


async def call(owner, action, params):
    return await owner.maintenance({"action": action, "params": params})


async def wait_state(owner, mid, states, *, timeout=60):
    async with asyncio.timeout(timeout):
        while True:
            value = owner.maintenance_service.store.public(mid)
            if value["state"] in states:
                return value
            task = owner.maintenance_service.tasks.get(mid)
            if task and task.done():
                error = task.exception()
                assert error is None, repr(error)
                raise AssertionError(value)
            await asyncio.sleep(0.05)


async def plan(owner, env, artifact, version, *, command_id="plan", repair=None):
    return await call(
        owner,
        "repair" if repair else "plan",
        {
            "commandId": command_id,
            "environmentId": env["environmentId"],
            "environmentRevision": env["acceptedRevision"],
            "operation": python_operation(artifact, version),
            **({"repairOf": repair} if repair else {}),
        },
    )


async def approve(owner, document, command_id="approve"):
    return await call(
        owner,
        "approve",
        {
            "maintenanceId": document["maintenanceId"],
            "commandId": command_id,
            "planRevision": document["planRevision"],
            "planDigest": document["planDigest"],
            "approvalRequestId": document["approval"]["requestId"],
            "approved": True,
        },
    )


def test_actual_reserved_two_owner_install_verifies_and_restarts_new_generations(
    tmp_path,
):
    available()

    async def run():
        _coordinator, owners = await fixture(tmp_path, real=True)
        owner, env, _old = owners[0]
        try:
            response = await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2")
            document = response["maintenance"]
            assert document["state"] == "awaiting_approval"
            assert not document["environmentFence"]["blocked"]
            receipt = await approve(owner, document)
            assert receipt["receipt"]["action"] == "approve"
            final = await wait_state(
                owner,
                document["maintenanceId"],
                {
                    "completed",
                    "needs_repair",
                    "restart_required",
                    "blocked_unconfirmed",
                },
            )
            assert final["state"] == "completed", final
            assert final["result"]["environmentVerified"]
            assert not final["environmentFence"]["blocked"]
            rows = final["result"]["restarts"]
            assert len(rows) == 2 and all(row["status"] == "ready" for row in rows)
            for runtime, original, old_kernel in owners:
                current = runtime.registry.get(original["environmentId"])
                assert current["acceptedRevision"] > original["acceptedRevision"]
                restarted = next(
                    row
                    for row in rows
                    if row["workspaceId"] == runtime.identity["workspaceId"]
                )
                assert restarted["generation"] > old_kernel["generation"]
                kernel = runtime.db.run(
                    lambda c, restarted=restarted: c.execute(
                        "SELECT * FROM notebook_runtime_kernels WHERE instance_id=?",
                        (restarted["kernelInstanceId"],),
                    ).fetchone()
                )
                state = runtime.manager.states[
                    (kernel["session_key"], "python", "local")
                ]
                output = await asyncio.to_thread(
                    state.kernel.execute,
                    "import omicsclaw_maintenance_test; print(omicsclaw_maintenance_test.value)",
                )
                assert output.status == "ok" and "2" in output.stdout
            events = await call(
                owner,
                "events",
                {"maintenanceId": document["maintenanceId"], "limit": 40},
            )
            assert any(
                e["kind"] == "process_result" and e["exitConfirmed"]
                for e in events["items"]
            )
            queried = await call(owner, "receipt", {"commandId": "approve"})
            assert queried["receipt"] == receipt["receipt"]
            import pytest

            from omicsclaw.entry.environment_coordination import CoordinationError
            from tests.entry.test_notebook_r import bind

            affected = next(
                row
                for row in rows
                if row["workspaceId"] == owner.identity["workspaceId"]
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "later-release",
                    "kernelInstanceId": affected["kernelInstanceId"],
                    "generation": affected["generation"],
                    "language": "python",
                }
            )
            latest_env = owner.registry.get(env["environmentId"])
            await bind(owner, "python", latest_env, suffix="-later")
            with pytest.raises(CoordinationError, match="restart_stale"):
                await call(
                    owner,
                    "restart",
                    {
                        "maintenanceId": document["maintenanceId"],
                        "commandId": "old-restart",
                        "restartId": affected["restartId"],
                        "notebookId": "nb",
                        "language": "python",
                        "expectedGeneration": affected["previousGeneration"],
                    },
                )
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_actual_failed_install_cancelled_repair_keeps_damage_then_success_restarts(
    tmp_path,
):
    available()

    async def run():
        _coordinator, owners = await fixture(tmp_path, real=True)
        owner, env, _ = owners[0]
        try:
            bad = wheel(
                tmp_path,
                "0.0.3",
                code="raise RuntimeError('SYNTHETIC_IMPORT_FAILURE')\n",
            )
            first = (await plan(owner, env, bad, "0.0.3"))["maintenance"]
            await approve(owner, first)
            failed = await wait_state(
                owner, first["maintenanceId"], {"needs_repair", "blocked_unconfirmed"}
            )
            assert failed["state"] == "needs_repair" and failed["writeStarted"], failed
            assert failed["environmentFence"]["blocked"]
            damage = failed["environmentFence"]["damageEpochs"]
            import pytest

            from omicsclaw.entry.environment_coordination import CoordinationError

            with pytest.raises(CoordinationError, match="frozen"):
                await owner.registry.preflight(env["environmentId"])
            with pytest.raises(CoordinationError, match="frozen"):
                owner.registry.accept(env["environmentId"], env["revision"])
            events = []
            cursor = None
            while True:
                page = await call(
                    owner,
                    "events",
                    {
                        "maintenanceId": first["maintenanceId"],
                        "limit": 1,
                        "cursor": cursor,
                    },
                )
                assert len(json.dumps(page).encode()) <= 192 * 1024
                events.extend(page["items"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                assert isinstance(cursor, str) and len(cursor.encode()) <= 1024
            assert any(
                e["kind"] == "process_result"
                and e["phase"] == "verify"
                and e["exitCode"] != 0
                and e["exitConfirmed"]
                for e in events
            )
            assert "SYNTHETIC_IMPORT_FAILURE" in "".join(
                e.get("text", "") for e in events
            )
            good = wheel(tmp_path, "0.0.2")
            second = (
                await plan(
                    owner,
                    env,
                    good,
                    "0.0.2",
                    command_id="repair2",
                    repair=first["maintenanceId"],
                )
            )["maintenance"]
            await call(
                owner,
                "cancel",
                {
                    "maintenanceId": second["maintenanceId"],
                    "commandId": "cancel2",
                    "planRevision": second["planRevision"],
                    "planDigest": second["planDigest"],
                    "confirmation": "cancel_maintenance",
                },
            )
            cancelled = await wait_state(
                owner, second["maintenanceId"], {"cancelled_before_mutation"}
            )
            assert cancelled["environmentFence"]["damageEpochs"] == damage
            assert (
                first["maintenanceId"]
                in cancelled["environmentFence"]["maintenanceIds"]
            )
            expired = (
                await plan(
                    owner,
                    env,
                    good,
                    "0.0.2",
                    command_id="repair-expired",
                    repair=first["maintenanceId"],
                )
            )["maintenance"]
            owner.maintenance_service.approvals[expired["maintenanceId"]][0].abandon()
            expired = await wait_state(
                owner, expired["maintenanceId"], {"cancelled_before_mutation"}
            )
            assert expired["environmentFence"]["damageEpochs"] == damage
            repeated = (
                await plan(
                    owner,
                    env,
                    bad,
                    "0.0.3",
                    command_id="repair-fails",
                    repair=first["maintenanceId"],
                )
            )["maintenance"]
            await approve(owner, repeated, "approve-fails")
            repeated = await wait_state(
                owner,
                repeated["maintenanceId"],
                {"needs_repair", "blocked_unconfirmed"},
            )
            assert repeated["state"] == "needs_repair", repeated
            assert len(repeated["environmentFence"]["damageEpochs"]) == 2
            assert set(damage) < set(repeated["environmentFence"]["damageEpochs"])
            third = (
                await plan(
                    owner,
                    env,
                    good,
                    "0.0.2",
                    command_id="repair3",
                    repair=repeated["maintenanceId"],
                )
            )["maintenance"]
            impact = await call(
                owner, "impact", {"maintenanceId": third["maintenanceId"]}
            )
            assert third["plan"]["impactCount"] == 2
            assert len(impact["items"]) == 2
            assert all(row["state"] == "closed" for row in impact["items"])
            await approve(owner, third, "approve3")
            final = await wait_state(
                owner,
                third["maintenanceId"],
                {
                    "completed",
                    "needs_repair",
                    "restart_required",
                    "blocked_unconfirmed",
                },
            )
            assert final["state"] == "completed", final
            assert not final["environmentFence"]["blocked"]
            assert len(final["result"]["restarts"]) == 2
            assert all(row["status"] == "ready" for row in final["result"]["restarts"])
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


def test_actual_r_install_failure_evidence_and_repair_starts_both_irkernels(tmp_path):
    import hashlib
    import os
    import sys
    import tarfile
    from pathlib import Path

    import pytest

    from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
    from omicsclaw.entry.notebook.runtime import OwnerRuntime
    from tests.entry.test_notebook_r import bind

    available()
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]
    if not libraries:
        pytest.skip("Set OMICSCLAW_TEST_R_LIBRARY to explicit IRkernel library")
    library = tmp_path / "library"
    library.mkdir()

    def operation(version, bad=False):
        source = tmp_path / ("source-" + version)
        (source / "R").mkdir(parents=True)
        (source / "DESCRIPTION").write_text(
            f"Package: omicsclawMaintenanceTest\nVersion: {version}\nTitle: Maintenance Test\nDescription: A disposable test package.\nLicense: MIT\nAuthor: Fixture\nMaintainer: Fixture <fixture@example.invalid>\n"
        )
        (source / "NAMESPACE").write_text("export(value)\n")
        (source / "R/value.R").write_text(
            ".onLoad <- function(libname,pkgname) stop('SYNTHETIC_R_NAMESPACE_FAILURE')\nvalue <- function() 3L\n"
            if bad
            else "value <- function() 2L\n"
        )
        archive = tmp_path / ("omicsclawMaintenanceTest_" + version + ".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(source, arcname="omicsclawMaintenanceTest")
        return {
            "adapter": "r_source",
            "libraryPath": str(library),
            "artifacts": [
                {
                    "path": str(archive),
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            ],
            "checks": [{"package": "omicsclawMaintenanceTest", "version": version}],
        }

    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        owners = []
        try:
            for index in range(2):
                workspace = tmp_path / str(index)
                workspace.mkdir()
                owner = OwnerRuntime(
                    workspace,
                    {"authorityId": "a" * 32, "workspaceId": str(index) * 32},
                    source_digest="r-maintenance-test",
                    env={},
                    environment_coordinator=coordinator,
                )
                await owner.start()
                owners.append(owner)
                env = await owner.registry.register(
                    {
                        "language": "r",
                        "executable": str(Path(sys.executable).with_name("R")),
                        "libraryPaths": [str(library), *libraries],
                        "requiredPackages": [],
                    }
                )
                assert env["status"] == "ready", env
                await bind(owner, "r", env)
                if index == 0:
                    original = env
            owner = owners[0]
            request = {
                "commandId": "r-plan",
                "environmentId": original["environmentId"],
                "environmentRevision": original["acceptedRevision"],
                "operation": operation("0.0.3", True),
            }
            first = (await call(owner, "plan", request))["maintenance"]
            await approve(owner, first, "r-approve")
            failed = await wait_state(
                owner, first["maintenanceId"], {"needs_repair", "blocked_unconfirmed"}
            )
            assert failed["state"] == "needs_repair", failed
            events = []
            cursor = None
            while True:
                page = await call(
                    owner,
                    "events",
                    {
                        "maintenanceId": first["maintenanceId"],
                        "cursor": cursor,
                        "limit": 40,
                    },
                )
                events.extend(page["items"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
            assert any(
                e["kind"] == "process_result"
                and e["phase"] == "install"
                and e["exitCode"] != 0
                and e["exitConfirmed"]
                for e in events
            )
            assert "SYNTHETIC_R_NAMESPACE_FAILURE" in "".join(
                e.get("text", "") for e in events
            )
            request.update(
                commandId="r-repair",
                repairOf=first["maintenanceId"],
                operation=operation("0.0.2"),
            )
            repair = (await call(owner, "repair", request))["maintenance"]
            await approve(owner, repair, "r-repair-approve")
            final = await wait_state(
                owner,
                repair["maintenanceId"],
                {
                    "completed",
                    "needs_repair",
                    "restart_required",
                    "blocked_unconfirmed",
                },
            )
            assert final["state"] == "completed", final
            assert len(final["result"]["restarts"]) == 2
            for runtime in owners:
                row = runtime.db.run(
                    lambda c: c.execute(
                        "SELECT session_key FROM notebook_runtime_kernels WHERE language='r' AND state='ready'"
                    ).fetchone()
                )
                state = runtime.manager.states[(row[0], "r", "local")]
                result = await asyncio.to_thread(
                    state.kernel.execute,
                    "print(omicsclawMaintenanceTest::value()); print(colSums(matrix(1:8,nrow=2)))",
                )
                assert result.status == "ok" and "2" in result.stdout
        finally:
            for owner in owners:
                await owner.close()

    asyncio.run(run())
