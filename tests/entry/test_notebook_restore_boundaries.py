"""Restore gates survive lost observers, environment changes and source retention."""

import asyncio
import copy
import sys
import venv

import pytest

from omicsclaw.entry.notebook.analysis_store import entity_ref
from omicsclaw.entry.notebook.checkpoint_wire import validate
from omicsclaw.entry.notebook.contracts import encoded
from omicsclaw.entry.notebook.restores import OwnerRestores
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import registration, settled
from tests.entry.test_notebook_checkpoint_owner import save_command, terminal
from tests.entry.test_notebook_r import bind, create, environment
from tests.entry.test_notebook_r import command as user
from tests.entry.test_notebook_restore_owner import (
    kernel_reference,
    restore_command,
    restored,
    source,
)

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def state(owner, kind="checkpointRestoreOverview", **subject):
    return owner.analysis.store.state_response(
        {"notebookId": "nb", "subject": {"kind": kind, **subject}}
    )["item"]


@pytest.mark.parametrize(
    "change", ["path", "subset", "reader", "target", "generation", "source", "language"]
)
def test_restore_command_has_no_arbitrary_loader_or_partial_group(change):
    kernel = {
        "language": "python",
        "kernelInstanceId": "instance",
        "generation": 2,
        "environmentId": "env",
        "environmentRevision": 1,
    }
    command = {
        "schemaVersion": 1,
        "commandId": "restore",
        "action": "checkpoint.restore",
        "arguments": {
            "checkpoint": {
                "checkpointId": "cp",
                "manifestRevision": 1,
                "manifestSha256": "a" * 64,
            },
            "target": kernel,
        },
        "expected": {
            "analysisRevision": 0,
            "kernels": [kernel],
            "objects": [],
            "selections": [],
        },
    }
    validate("nb", command)
    changed = copy.deepcopy(command)
    if change in {"path", "subset", "reader"}:
        changed["arguments"][change] = "caller-controlled"
    elif change == "target":
        changed["arguments"]["target"]["source"] = "print('wrong')"
    elif change == "generation":
        changed["arguments"]["target"]["generation"] = True
    elif change == "source":
        changed["arguments"]["checkpoint"]["manifestSha256"] = "../file"
    else:
        changed["arguments"]["target"]["language"] = "bash"
    with pytest.raises(NotebookError, match="^notebook_checkpoint_"):
        validate("nb", changed)


def test_restore_projection_has_pinned_history_and_bounded_strict_pages(tmp_path):
    async def run():
        owner = await create(tmp_path)
        try:

            def put(c, oid, revision):
                owner.checkpoints.store.put(
                    c,
                    "nb",
                    "restore",
                    {"operationId": oid, "statusSeq": revision, "state": "unknown"},
                )

            owner.db.run(lambda c: [put(c, oid, 1) for oid in ("a", "c", "e")])
            params = {
                "notebookId": "nb",
                "collection": "checkpointRestoreOperations",
                "limit": 1,
            }
            first = owner.analysis.store.page(params)
            assert first["parentId"] is None and first["parentRevision"] is None
            owner.db.run(lambda c: (put(c, "b", 1), put(c, "c", 2)))
            second = owner.analysis.store.page(
                {**params, "cursor": first["nextCursor"]}
            )
            assert second["items"] == [
                {"operationId": "c", "statusSeq": 1, "state": "unknown"}
            ]
            assert (
                state(owner, "checkpointRestoreOperation", id="c", revision=1)[
                    "statusSeq"
                ]
                == 1
            )
            assert state(owner, "checkpointRestoreOperation", id="c")["statusSeq"] == 2
            assert len(encoded(second)) <= 192 * 1024
            for extra in (
                {"limit": 41},
                {"cursor": "界" * 400},
                {"parentId": "cp", "parentRevision": 1},
                {"notebookId": "other", "cursor": first["nextCursor"]},
            ):
                with pytest.raises(NotebookError, match="^notebook_checkpoint_"):
                    owner.analysis.store.page({**params, **extra})
            for subject in (
                {"kind": "checkpointRestoreOverview", "path": "private"},
                {"kind": "checkpointRestoreOperation", "id": "a", "revision": 0},
            ):
                with pytest.raises(NotebookError, match="^notebook_checkpoint_"):
                    owner.analysis.store.state_response(
                        {"notebookId": "nb", "subject": subject}
                    )
        finally:
            await owner.close()

    asyncio.run(run())


def test_fresh_reserved_source_save_and_unavailable_environment_eligibility(tmp_path):
    async def run():
        owner = await create(tmp_path)
        try:
            env = await environment(owner, "python")
            kernel = (await bind(owner, "python", env, takeControl=True))["kernel"]
            assert state(owner)["languages"]["python"]["eligible"]
            await owner.save(
                {
                    "notebookId": "nb",
                    "commandId": "only-source",
                    "cellId": "draft",
                    "source": "not_yet_executed = 1",
                    "expectedRevision": 0,
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                    "language": "python",
                }
            )
            assert state(owner)["languages"]["python"]["eligible"]
            owner.registry.database.run(
                lambda c: c.execute(
                    "UPDATE registered_environments SET status='unavailable' WHERE environment_id=?",
                    (env["environmentId"],),
                )
            )
            assert not state(owner)["languages"]["python"]["eligible"]
            assert owner.manager.codes == []
        finally:
            await owner.close()

    asyncio.run(run())


def test_intent_recovery_keeps_hold_and_gate_without_replaying_loader(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            checkpoint, target = await source(owner)
            owner.restores.schedule = lambda: None
            params = restore_command(owner, checkpoint, target)
            receipt = await owner.analysis_command(params)
            oid = receipt["receipt"]["result"]["operationId"]
            before = owner.db.run(
                lambda c: c.execute(
                    "SELECT count(*) FROM notebook_executions"
                ).fetchone()[0]
            )
            owner.service._transaction(owner.restores.recover)
            owner.restores = OwnerRestores(owner)
            owner.restores.schedule()
            op = state(owner, "checkpointRestoreOperation", id=oid)
            assert (
                op["state"] == "unknown"
                and op["gateActive"]
                and not op["stopConfirmed"]
            )
            assert not owner.restores.workers
            assert (await owner.analysis_command(params))["receipt"] == receipt[
                "receipt"
            ]
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_executions"
                    ).fetchone()[0]
                )
                == before
            )
            for index in range(5):
                owner.db.run(
                    lambda c, index=index: owner.checkpoints.store.put(
                        c,
                        "nb",
                        "checkpoint",
                        {
                            **checkpoint,
                            "checkpointId": f"newer-{index}",
                            "createdAt": checkpoint["createdAt"] + index + 1,
                        },
                    )
                )
            files = owner.db.run(
                lambda c: owner.checkpoints.store.details(c, checkpoint["operationId"])
            )["files"]
            saved_path = (
                owner.checkpoints.directory
                / checkpoint["operationId"]
                / "data"
                / files[0]["file"]
            )
            owner.checkpoints.collect("nb")
            assert saved_path.exists()
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_gc WHERE checkpoint_id=?",
                        (checkpoint["checkpointId"],),
                    ).fetchone()[0]
                )
                == 0
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-recovered",
                    "language": "python",
                    "kernelInstanceId": target["kernelInstanceId"],
                    "generation": target["generation"],
                }
            )
            op = state(owner, "checkpointRestoreOperation", id=oid)
            assert op["targetReleased"] and not op["gateActive"]
            owner.checkpoints.collect("nb")
            assert not saved_path.exists()
        finally:
            await owner.close()

    asyncio.run(run())


def test_actual_loaded_version_mismatch_fails_without_publishing(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            checkpoint, target = await source(owner)
            original = owner.restores.code

            def changed(op, request, directory, *, commit=False):
                code = original(op, request, directory, commit=commit)
                return (
                    code
                    if commit
                    else "import pandas as pd\npd.__version__='0.0.0-controlled-target'\n"
                    + code
                )

            owner.restores.code = changed
            accepted = await owner.analysis_command(
                restore_command(owner, checkpoint, target)
            )
            oid = accepted["receipt"]["result"]["operationId"]
            op = await restored(owner, oid)
            assert op["state"] == "failed" and op["stopConfirmed"] and op["gateActive"]
            assert op["compatibility"]["status"] == "incompatible", op
            assert (
                op["compatibility"]["reasonCode"] == "restore_dependency_incompatible"
            )
            assert op["resultObjectRefs"] == []
            fixed = owner.db.run(lambda c: owner.restores.details(c, oid))
            assert (
                fixed["targetRuntimeEvidence"]["packages"]["pandas"]["version"]
                == "0.0.0-controlled-target"
            )
            assert (
                fixed["manifest"]["runtimeEvidence"]["packages"]["pandas"]["version"]
                != "0.0.0-controlled-target"
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_unrelated_package_revision_change_allows_real_restore(tmp_path):
    prefix = tmp_path / "private-env"
    (tmp_path / "workspace").mkdir()
    venv.EnvBuilder(system_site_packages=True).create(prefix)
    metadata = (
        prefix
        / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/restore_canary-1.dist-info"
    )
    metadata.mkdir()
    metadata_file = metadata / "METADATA"
    metadata_file.write_text(
        "Metadata-Version: 2.1\nName: restore-canary\nVersion: 1\n"
    )

    async def run():
        owner = await create(tmp_path / "workspace", real=True)
        try:
            spec = {
                "language": "python",
                "executable": str(prefix / "bin/python"),
                "libraryPaths": [],
                "requiredPackages": [],
            }
            env = await owner.registry.register(spec)
            kernel = (await bind(owner, "python", env))["kernel"]
            executed = await owner.admit(
                {
                    "command": user(
                        kernel,
                        "make",
                        "import pandas as pd\ndata=pd.DataFrame({'n':[3,5]},index=['a','b'])",
                    )
                }
            )
            assert (await settled(owner, executed["execution_id"]))[
                "status"
            ] == "completed"
            command = registration(kernel)
            command["arguments"].update(
                adapter="python_table",
                rowIdentity={"kind": "index"},
                columnIdentity={"kind": "index"},
                declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
            )
            command["expected"]["analysisRevision"] = (
                owner.analysis.store.state_response({"notebookId": "nb"})[
                    "analysisRevision"
                ]
            )
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": command}
            )
            assert (await settled(owner, accepted["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            saved = await terminal(owner, accepted["receipt"]["result"]["operationId"])
            assert saved["state"] == "ready"
            cp = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "checkpoint", saved["checkpointId"]
                )
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-before-change",
                    "language": "python",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            metadata_file.write_text(
                "Metadata-Version: 2.1\nName: restore-canary\nVersion: 2\n"
            )
            changed = await owner.registry.preflight(env["environmentId"])
            assert (
                changed["revision"] > env["revision"] and changed["status"] == "changed"
            )
            accepted_env = owner.registry.accept(
                env["environmentId"], changed["revision"]
            )
            target = (
                await bind(owner, "python", accepted_env, "new", takeControl=True)
            )["kernel"]
            accepted = await owner.analysis_command(restore_command(owner, cp, target))
            op = await restored(owner, accepted["receipt"]["result"]["operationId"])
            assert op["state"] == "restored", op
            assert (
                op["compatibility"]["source"]["environmentRevision"] == env["revision"]
            )
            assert (
                op["compatibility"]["target"]["environmentRevision"]
                == accepted_env["revision"]
            )
            assert (
                op["compatibility"]["source"]["fingerprint"]
                != op["compatibility"]["target"]["fingerprint"]
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_restored_dataset_does_not_clear_selection_and_explicit_remap_works(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        selected = {}
        try:

            async def select(owner, kernel, checkpoint):
                obj = owner.db.run(
                    lambda c: owner.analysis.store.active_objects(c, "nb")[0]
                )
                params = {
                    "notebookId": "nb",
                    "command": {
                        "schemaVersion": 1,
                        "commandId": "pick-source",
                        "action": "selection.create",
                        "arguments": {
                            "view": obj["defaultView"],
                            "members": {"kind": "ids", "values": [["b"]]},
                        },
                        "expected": {
                            "analysisRevision": owner.analysis.store.state_response(
                                {"notebookId": "nb"}
                            )["analysisRevision"],
                            "kernels": [kernel_reference(kernel)],
                            "objects": [entity_ref(obj, "object")],
                            "selections": [],
                        },
                    },
                }
                result = await owner.analysis_command(params)
                selected.update(
                    owner.db.run(
                        lambda c: owner.analysis.store.get(
                            c,
                            "nb",
                            "selection",
                            result["receipt"]["result"]["entityId"],
                        )
                    )
                )

            checkpoint, target = await source(owner, select)
            accepted = await owner.analysis_command(
                restore_command(owner, checkpoint, target)
            )
            op = await restored(owner, accepted["receipt"]["result"]["operationId"])
            assert op["state"] == "restored", op
            old = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {"kind": "selection", "id": selected["selectionId"]},
                }
            )["item"]
            assert (
                not old["valid"] and old["membersSha256"] == selected["membersSha256"]
            )
            assert owner.analysis.store.state_response({"notebookId": "nb"})[
                "activeSelection"
            ] == entity_ref(selected, "selection")
            with pytest.raises(NotebookError, match="reference_stale"):
                await owner.admit(
                    {
                        "command": {
                            **user(target, "stale-selection", "must_not_run=True"),
                            "version": 3,
                            "preconditions": {
                                "analysis": owner.db.run(
                                    lambda c: owner.analysis.store.context(c, "nb")
                                )["guards"]["python"]
                            },
                        }
                    }
                )
            obj = owner.db.run(
                lambda c: owner.analysis.store.get(
                    c, "nb", "object", op["resultObjectRefs"][0]["objectId"]
                )
            )
            result = await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": {
                        "schemaVersion": 1,
                        "commandId": "explicit-remap",
                        "action": "selection.remap",
                        "arguments": {
                            "selection": entity_ref(selected, "selection"),
                            "targetView": obj["defaultView"],
                            "expectedMemberCount": 1,
                            "expectedMembersSha256": selected["membersSha256"],
                        },
                        "expected": {
                            "analysisRevision": owner.analysis.store.state_response(
                                {"notebookId": "nb"}
                            )["analysisRevision"],
                            "kernels": [kernel_reference(target)],
                            "objects": [entity_ref(obj, "object")],
                            "selections": [entity_ref(selected, "selection")],
                        },
                    },
                }
            )
            current = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "selection",
                        "id": result["receipt"]["result"]["entityId"],
                    },
                }
            )["item"]
            assert current["valid"] and current["count"] == 1
            assert current["membersSha256"] == selected["membersSha256"]
        finally:
            await owner.close()

    asyncio.run(run())
