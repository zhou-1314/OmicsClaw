"""A lost Restore target stays consumed until explicit, proven release."""

import asyncio
import json
import os

import pytest

from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import same_process
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import registration, settled
from tests.entry.test_notebook_checkpoint_owner import (
    save_command,
    terminal,
    wait_until,
)
from tests.entry.test_notebook_r import bind, environment
from tests.entry.test_notebook_r import command as user
from tests.entry.test_notebook_restore_owner import restore_command, restored

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


@pytest.mark.parametrize(
    "language,contained", [("python", True), ("r", True), ("python", False)]
)
def test_lost_restore_target_blocks_bind_until_exact_release(
    tmp_path, language, contained
):
    library = os.environ.get("OMICSCLAW_TEST_R_LIBRARY")
    if language == "r" and not library:
        pytest.skip("explicit R IRkernel library required")

    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        owner = OwnerRuntime(
            workspace,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="restore-gate-fixture",
            env={},
            environment_coordinator=coordinator,
        )
        await owner.start()
        try:
            env = await environment(
                owner,
                language,
                real=True,
                libraries=[library] if language == "r" else None,
            )
            kernel = (await bind(owner, language, env))["kernel"]
            code = (
                "import pandas as pd\ndata=pd.DataFrame({'x':[1,2]},index=['a','b'])\nresult=pd.DataFrame({'y':[3,4]},index=['a','b'])"
                if language == "python"
                else "data<-data.frame(x=c(1L,2L),row.names=c('a','b'));result<-data.frame(y=c(3L,4L),row.names=c('a','b'))"
            )
            accepted = await owner.admit({"command": user(kernel, "make", code)})
            assert (await settled(owner, accepted["execution_id"]))[
                "status"
            ] == "completed"
            for symbol in ("data", "result"):
                command = registration(kernel, "register-" + symbol)
                command["arguments"].update(
                    symbol=symbol,
                    adapter="python_table" if language == "python" else "r_rds",
                    rowIdentity={"kind": "index"},
                    columnIdentity={"kind": "index"},
                    declaredMatrix={
                        "assay": None,
                        "layer": None,
                        "semantics": "unknown",
                    },
                )
                command["expected"]["analysisRevision"] = (
                    owner.analysis.store.state_response({"notebookId": "nb"})[
                        "analysisRevision"
                    ]
                )
                receipt = await owner.analysis_command(
                    {"notebookId": "nb", "command": command}
                )
                assert (
                    await settled(owner, receipt["receipt"]["result"]["operationId"])
                )["status"] == "completed"
            receipt = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            saved = await terminal(owner, receipt["receipt"]["result"]["operationId"])
            assert saved["state"] == "ready", saved
            checkpoint = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "checkpoint", saved["checkpointId"]
                )
            )
            release = lambda selected, cid: {
                "notebookId": "nb",
                "commandId": cid,
                "language": language,
                "kernelInstanceId": selected["kernelInstanceId"],
                "generation": selected["generation"],
            }
            await owner.release(release(kernel, "source-release"))
            target = (await bind(owner, language, env, "target", takeControl=True))[
                "kernel"
            ]
            if not contained:
                owner.lifecycle_wrapper = None
            original = owner.restores.import_group
            committed = []

            def publication_fault(c, *args, **kwargs):
                refs = original(c, *args, **kwargs)
                if kwargs.get("publish"):
                    assert len(refs) == 2
                    committed.append(refs)
                    raise ValueError("fixture publication failure after memory commit")
                return refs

            owner.restores.import_group = publication_fault
            params = restore_command(owner, checkpoint, target)
            receipt = await owner.analysis_command(params)
            oid = receipt["receipt"]["result"]["operationId"]
            op = await restored(owner, oid)
            assert committed and op["state"] == "failed" and op["gateActive"]
            assert op["stopConfirmed"] and op["resultObjectRefs"] == []
            phases = owner.db.run(
                lambda c: [
                    r[0]
                    for r in c.execute(
                        "SELECT phase FROM notebook_checkpoint_executions WHERE operation_id=?",
                        (oid,),
                    )
                ]
            )
            assert set(phases) == {"loading", "committing"}
            evidence = owner.db.run(
                lambda c: json.loads(
                    c.execute(
                        "SELECT process_json FROM notebook_runtime_kernels WHERE instance_id=?",
                        (target["kernelInstanceId"],),
                    ).fetchone()[0]
                )
            )
            assert same_process(evidence)
            local = owner.manager.states[
                ("nk" + target["kernelInstanceId"], language, "local")
            ]
            await asyncio.to_thread(local.kernel.kill)
            await wait_until(
                lambda: (
                    owner.db.run(
                        lambda c: c.execute(
                            "SELECT state FROM notebook_runtime_kernels WHERE instance_id=?",
                            (target["kernelInstanceId"],),
                        ).fetchone()[0]
                    )
                    == "lost"
                )
            )
            assert not same_process(evidence)
            count = owner.db.run(
                lambda c: c.execute(
                    "SELECT count(*) FROM notebook_executions"
                ).fetchone()[0]
            )
            for suffix in ("bypass", "bypass-retry"):
                with pytest.raises(NotebookError, match="notebook_checkpoint_blocked"):
                    await bind(owner, language, env, suffix, takeControl=True)
            assert (await owner.analysis_command(params))["receipt"] == receipt[
                "receipt"
            ]
            overview = owner.db.run(lambda c: owner.restores.overview(c, "nb"))
            projected = overview["languages"][language]
            assert projected["activeOperationId"] == oid and projected["canRelease"]
            assert not projected["eligible"] and not projected["canRun"]
            assert not overview["continuationReady"]
            with pytest.raises(NotebookError, match="notebook_checkpoint_blocked"):
                await owner.admit(
                    {
                        "command": {
                            **user(target, "blocked-run", "1"),
                            "version": 3,
                            "preconditions": {
                                "analysis": owner.db.run(
                                    lambda c: owner.analysis.store.context(c, "nb")
                                )["guards"][language]
                            },
                        }
                    }
                )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_executions"
                    ).fetchone()[0]
                )
                == count
            )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_holds WHERE hold_id=?",
                        ("restore-" + oid,),
                    ).fetchone()[0]
                )
                == 1
            )
            if not contained:
                with pytest.raises(
                    NotebookError, match="notebook_checkpoint_stop_unconfirmed"
                ):
                    await owner.release(release(target, "release-failed"))
                op = owner.db.run(lambda c: owner.restores.get(c, "nb", oid))
                assert op["gateActive"] and not op["targetReleased"]
                assert (
                    owner.db.run(
                        lambda c: c.execute(
                            "SELECT count(*) FROM notebook_checkpoint_holds WHERE hold_id=?",
                            ("restore-" + oid,),
                        ).fetchone()[0]
                    )
                    == 1
                )
                with pytest.raises(NotebookError, match="notebook_checkpoint_blocked"):
                    await bind(
                        owner, language, env, "unconfirmed-exit", takeControl=True
                    )
                return
            await owner.release(release(target, "release-failed"))
            reservation = coordinator.get(
                owner.environment_guard.key("kernel", target["kernelInstanceId"])
            )
            assert (
                reservation["state"] == "closed" and not reservation["uncertain_exit"]
            )
            op = owner.db.run(lambda c: owner.restores.get(c, "nb", oid))
            assert op["targetReleased"] and not op["gateActive"]
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_holds WHERE hold_id=?",
                        ("restore-" + oid,),
                    ).fetchone()[0]
                )
                == 0
            )
            fresh = (
                await bind(owner, language, env, "after-release", takeControl=True)
            )["kernel"]
            assert fresh["generation"] > target["generation"]
            assert owner.db.run(lambda c: owner.restores.overview(c, "nb"))[
                "languages"
            ][language]["eligible"]
            command = {
                **user(
                    fresh,
                    "new-generation",
                    "print('NEW_GENERATION_AFTER_EXPLICIT_RELEASE')",
                ),
                "version": 3,
                "preconditions": {
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guards"][language]
                },
            }
            accepted = await owner.admit({"command": command})
            result = await settled(owner, accepted["execution_id"])
            assert result["status"] == "completed", result
            assert "NEW_GENERATION_AFTER_EXPLICIT_RELEASE" in result["output"]["stdout"]
        finally:
            await owner.close()

    asyncio.run(run())


def test_historical_gate_stays_visible_and_cannot_release_another_target(tmp_path):
    from omicsclaw.entry.notebook.analysis_store import kernel_ref
    from tests.entry.test_notebook_r import create

    async def run():
        owner = await create(tmp_path)
        try:
            env = await environment(owner, "python")
            original = (await bind(owner, "python", env))["kernel"]

            def seed_old_gate(c):
                row = c.execute(
                    "SELECT * FROM notebook_runtime_kernels WHERE instance_id=?",
                    (original["kernelInstanceId"],),
                ).fetchone()
                op = {
                    "operationId": "old-restore",
                    "statusSeq": 1,
                    "state": "failed",
                    "target": kernel_ref(row),
                    "gateActive": True,
                    "targetReleased": False,
                    "stopConfirmed": True,
                    "finishedAt": 1.0,
                }
                c.execute(
                    "INSERT INTO notebook_restore_intents VALUES(?,?,?,?,1,0)",
                    (op["operationId"], "nb", original["kernelInstanceId"], "{}"),
                )
                owner.checkpoints.store.put(c, "nb", "restore", op)
                c.execute(
                    "UPDATE notebook_runtime_kernels SET state='lost' WHERE instance_id=?",
                    (original["kernelInstanceId"],),
                )
                copied = dict(row)
                copied.update(
                    instance_id="new-target",
                    session_key="new-session",
                    generation=row["generation"] + 1,
                )
                columns = list(copied)
                c.execute(
                    "INSERT INTO notebook_runtime_kernels("
                    + ",".join(columns)
                    + ") VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    list(copied.values()),
                )

            owner.service._transaction(seed_old_gate)
            overview = owner.db.run(lambda c: owner.restores.overview(c, "nb"))
            current = overview["languages"]["python"]
            assert current["kernel"]["kernelInstanceId"] == "new-target"
            assert current["activeOperationId"] == "old-restore"
            assert not current["eligible"] and not current["canRun"]
            assert not overview["continuationReady"]
            with pytest.raises(
                NotebookError, match="notebook_checkpoint_reference_stale"
            ):
                await owner.release(
                    {
                        "notebookId": "nb",
                        "commandId": "wrong-target-release",
                        "language": "python",
                        "kernelInstanceId": "new-target",
                        "generation": original["generation"] + 1,
                    }
                )
            assert owner.db.run(lambda c: owner.restores.get(c, "nb", "old-restore"))[
                "gateActive"
            ]
            assert owner.manager.codes == []
        finally:
            await owner.close()

    asyncio.run(run())
