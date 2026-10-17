"""Recovered Release keeps its receipt pending until the original tree exits."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import process_evidence, same_process
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.entry.process_containment import (
    SubreaperLifecycleWrapper,
    capture_containment,
    contained_exit,
)
from tests.entry.test_notebook_analysis_owner import registration, settled
from tests.entry.test_notebook_checkpoint_owner import (
    save_command,
    terminal,
    wait_until,
)
from tests.entry.test_notebook_r import bind, environment
from tests.entry.test_notebook_r import command as user
from tests.entry.test_notebook_restore_owner import restore_command, source

pytestmark = pytest.mark.usefixtures("notebook_control_rss")
IDENTITY = {"authorityId": "a" * 32, "workspaceId": "b" * 32}


def early_owner(directory, mode, language):
    """A real owner exits after kernel start but before containment is registered."""
    root = Path(directory)

    async def run():
        workspace = root / "workspace"
        workspace.mkdir()
        owner = OwnerRuntime(
            workspace,
            IDENTITY,
            source_digest="release-fixture",
            env={},
            environment_coordinator=EnvironmentCoordinator(root / "global"),
        )
        await owner.start()
        if language == "python":
            checkpoint, target = await source(owner)
        else:
            env = await environment(
                owner,
                "r",
                real=True,
                libraries=[os.environ["OMICSCLAW_TEST_R_LIBRARY"]],
            )
            kernel = (await bind(owner, "r", env))["kernel"]
            result = await owner.admit(
                {
                    "command": user(
                        kernel,
                        "make-r",
                        "data<-data.frame(count=c(1L,2L),row.names=c('a','b'))",
                    )
                }
            )
            assert (await settled(owner, result["execution_id"]))[
                "status"
            ] == "completed"
            command = registration(kernel)
            command["arguments"].update(
                adapter="r_rds",
                rowIdentity={"kind": "index"},
                columnIdentity={"kind": "index"},
                declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
            )
            command["expected"]["analysisRevision"] = (
                owner.analysis.store.state_response({"notebookId": "nb"})[
                    "analysisRevision"
                ]
            )
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": command}
            )
            assert (await settled(owner, result["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            receipt = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            op = await terminal(owner, receipt["receipt"]["result"]["operationId"])
            assert op["state"] == "ready", op
            checkpoint = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "checkpoint", op["checkpointId"]
                )
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-source-r",
                    "language": "r",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            target = (await bind(owner, "r", env, "target", takeControl=True))["kernel"]
        (root / "target.json").write_text(json.dumps(target))

        def crash(instance, evidence=None):
            reservation = owner.environment_guard.coordinator.get(
                owner.environment_guard.key("kernel", instance)
            )
            evidence = evidence or json.loads(reservation["process_json"])
            proof = None
            deadline = time.monotonic() + 5
            while proof is None and time.monotonic() < deadline:
                proof = capture_containment(evidence)
                time.sleep(0.01)
            assert proof and proof["kind"] == "linux_subreaper"
            assert reservation["containment_json"] is None
            (root / "crash.json").write_text(
                json.dumps(
                    {
                        "owner": process_evidence(os.getpid()),
                        "root": evidence,
                        "proof": proof,
                        "target": target,
                    }
                )
            )
            os._exit(91)

        if mode == "missing_process":
            owner.environment_guard.process = crash
        else:
            owner.environment_guard.confirm_started = crash
        receipt = await owner.analysis_command(
            restore_command(owner, checkpoint, target)
        )
        (root / "restore-receipt.json").write_text(json.dumps(receipt))
        await asyncio.sleep(60)
        raise AssertionError("kernel never reached the startup crash boundary")

    asyncio.run(run())


def crashed_owner(tmp_path, mode, language):
    log = tmp_path / "early-owner.log"
    with log.open("w") as output:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from tests.entry.test_notebook_recovered_release import early_owner; "
                    "import sys; early_owner(*sys.argv[1:])"
                ),
                str(tmp_path),
                mode,
                language,
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=output,
            stderr=output,
            timeout=90,
            check=False,
        )
    assert result.returncode == 91, (result.returncode, log.read_text())
    info = json.loads((tmp_path / "crash.json").read_text())
    assert not same_process(info["owner"])
    assert same_process(info["root"]) and same_process(info["proof"]["child"])
    return info


async def dispose(info):
    """Only the test's recorded births may be cleaned after a failed assertion."""
    root, child = info["root"], info["proof"]["child"]
    if same_process(root):
        wrapper = SubreaperLifecycleWrapper(Path(info["proof"]["directory"]).parent)
        await asyncio.to_thread(
            wrapper.terminate, root["pid"], lambda pid: same_process(root)
        )
    if same_process(child):
        os.kill(child["pid"], signal.SIGKILL)
    await wait_until(lambda: not same_process(root) and not same_process(child))


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="actual Linux subreaper required"
)
@pytest.mark.parametrize(
    "mode,language",
    [
        ("missing_proof", "python"),
        ("missing_process", "python"),
        ("lost", "python"),
        ("local_commit_failure", "python"),
        ("missing_proof", "r"),
    ],
)
def test_early_owner_loss_release_proves_exit_before_receipt(tmp_path, mode, language):
    if language == "r" and not os.environ.get("OMICSCLAW_TEST_R_LIBRARY"):
        pytest.skip("explicit R IRkernel library required")
    info = crashed_owner(tmp_path, mode, language)

    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        owner = OwnerRuntime(
            tmp_path / "workspace",
            IDENTITY,
            source_digest="release-fixture",
            env={},
            environment_coordinator=coordinator,
        )
        await owner.start()
        target = info["target"]
        instance = target["kernelInstanceId"]
        key = owner.environment_guard.key("kernel", instance)
        row = coordinator.get(key)
        assert row["containment_json"] is None
        assert row["dispatch_state"] == "dispatching"
        assert (row["process_json"] is None) == (mode == "missing_process")
        if mode == "missing_process":
            assert row["uncertain_exit"] == 1
        command = {
            "notebookId": "nb",
            "commandId": "release-recovered",
            "language": language,
            "kernelInstanceId": instance,
            "generation": target["generation"],
        }
        op = owner.db.run(lambda c: owner.restores.active(c, "nb", language))
        assert op["state"] == "unknown" and op["gateActive"]
        try:
            if mode == "lost":
                os.kill(info["root"]["pid"], signal.SIGKILL)
                await wait_until(lambda: not same_process(info["root"]))
                assert same_process(info["proof"]["child"])
                for _ in range(2):
                    with pytest.raises(NotebookError, match="unconfirmed"):
                        await owner.release(command)
                    local, receipt = owner.db.run(
                        lambda c: (
                            dict(
                                c.execute(
                                    "SELECT * FROM notebook_runtime_kernels WHERE instance_id=?",
                                    (instance,),
                                ).fetchone()
                            ),
                            json.loads(
                                c.execute(
                                    "SELECT receipt_json FROM notebook_runtime_commands WHERE command_id=?",
                                    (command["commandId"],),
                                ).fetchone()[0]
                            ),
                        )
                    )
                    assert local["state"] != "released"
                    assert receipt["status"] == "releasing"
                    assert owner.db.run(
                        lambda c: owner.restores.active(c, "nb", language)
                    )["gateActive"]
                    assert coordinator.get(key)["state"] != "closed"
            else:
                if mode == "local_commit_failure":
                    transaction = owner.service._transaction

                    def fault(fn):
                        def rolled_back(c):
                            result = fn(c)
                            if (
                                fn.__name__ == "work"
                                and coordinator.get(key)["state"] == "closed"
                            ):
                                raise sqlite3.OperationalError(
                                    "fixture local commit failure"
                                )
                            return result

                        return transaction(rolled_back)

                    owner.service._transaction = fault
                    try:
                        with pytest.raises(
                            sqlite3.OperationalError,
                            match="fixture local commit failure",
                        ):
                            await owner.release(command)
                    finally:
                        owner.service._transaction = transaction
                    assert coordinator.get(key)["state"] == "closed"
                    assert (
                        owner.db.run(
                            lambda c: c.execute(
                                "SELECT state FROM notebook_runtime_kernels WHERE instance_id=?",
                                (instance,),
                            ).fetchone()[0]
                        )
                        != "released"
                    )
                    assert (
                        owner.db.run(
                            lambda c: json.loads(
                                c.execute(
                                    "SELECT receipt_json FROM notebook_runtime_commands WHERE command_id=?",
                                    (command["commandId"],),
                                ).fetchone()[0]
                            )
                        )["status"]
                        == "releasing"
                    )
                    assert owner.db.run(
                        lambda c: owner.restores.active(c, "nb", language)
                    )["gateActive"]
                result = await owner.release(command)
                assert result["status"] == "released"
                assert await owner.release(command) == result
                assert not same_process(info["root"])
                assert not same_process(info["proof"]["child"])
                assert contained_exit(info["proof"], info["root"])
                row = coordinator.get(key)
                assert row["state"] == "closed" and not row["uncertain_exit"]
                proof = json.loads(row["containment_json"])
                assert proof == info["proof"]
                released = owner.db.run(
                    lambda c: owner.restores.get(c, "nb", op["operationId"])
                )
                assert released["targetReleased"] and not released["gateActive"]
                assert (
                    owner.db.run(
                        lambda c: c.execute(
                            "SELECT count(*) FROM notebook_checkpoint_holds WHERE hold_id=?",
                            ("restore-" + op["operationId"],),
                        ).fetchone()[0]
                    )
                    == 0
                )
        finally:
            await dispose(info)
            await owner.close()

    asyncio.run(run())


def test_recovered_containment_requires_owned_launch_and_original_birth(tmp_path):
    from omicsclaw.entry.environment_coordination import CoordinationError
    from tests.entry.test_environment_coordination import reservation

    env = tmp_path / "env"
    env.mkdir()
    coordinator = EnvironmentCoordinator(tmp_path / "global")
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    birth = process_evidence(os.getpid())
    coordinator.reserve(reservation("kernel", [env]))
    coordinator.update("kernel", owner, committed=True)
    with pytest.raises(CoordinationError, match="process_changed"):
        coordinator.recover_kernel_containment("kernel", owner, birth)
    coordinator.starting_kernel("kernel", owner)
    coordinator.update("kernel", owner, process=birth)
    before = coordinator.get("kernel")
    with pytest.raises(CoordinationError, match="owner_changed"):
        coordinator.recover_kernel_containment(
            "kernel", {**owner, "ownerEpoch": 2}, birth
        )
    with pytest.raises(CoordinationError, match="process_changed"):
        coordinator.recover_kernel_containment(
            "kernel", owner, {**birth, "start": "wrong"}
        )
    # This live process is not a verified launcher; its configuration grants no proof.
    assert coordinator.recover_kernel_containment("kernel", owner, birth) is None
    assert coordinator.get("kernel") == before
    coordinator.reserve(reservation("admission", [env], kind="admission"))
    with pytest.raises(CoordinationError, match="process_changed"):
        coordinator.recover_kernel_containment("admission", owner, birth)
