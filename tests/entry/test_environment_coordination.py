"""Physical read/write overlap and durable cross-workspace admission fences."""

import multiprocessing
from pathlib import Path

import pytest

from omicsclaw.entry.environment_coordination import (
    CoordinationError,
    EnvironmentCoordinator,
    root_evidence,
)


def roots(*paths, role="runtime_read"):
    return [root_evidence(str(path), role=role) for path in paths]


def reservation(name, paths, *, kind="kernel", owner="owner", workspace="workspace"):
    return {
        "reservationId": name,
        "ownerId": owner,
        "ownerEpoch": 1,
        "workspaceId": workspace,
        "workspacePath": str(paths[0]),
        "kind": kind,
        "resourceId": name,
        "readRoots": roots(
            *paths, role="control_read" if kind == "control" else "runtime_read"
        ),
        "identity": {"environmentId": name, "revision": 1},
    }


def coordinator(tmp_path):
    return EnvironmentCoordinator(tmp_path / "state" / "shared")


def test_private_venvs_share_interpreter_but_not_write_scope(tmp_path):
    base, one, two = (tmp_path / name for name in ("base", "venv-one", "venv-two"))
    for path in (base, one, two):
        path.mkdir()
    executable = base / "python"
    executable.write_text("fixture")
    c = coordinator(tmp_path)
    c.reserve(reservation("one", [one, executable], workspace="workspace-one"))
    c.reserve(reservation("two", [two, executable], workspace="workspace-two"))
    assert [r["resource_id"] for r in c.impact(roots(one, role="install_write"))] == [
        "one"
    ]
    assert {r["resource_id"] for r in c.impact(roots(base, role="install_write"))} == {
        "one",
        "two",
    }
    alias = tmp_path / "alias"
    alias.symlink_to(one)
    assert [r["resource_id"] for r in c.impact(roots(alias, role="install_write"))] == [
        "one"
    ]


def test_fence_blocks_all_new_admission_and_only_old_committed_work_can_drain(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    c.reserve(reservation("old", [env]))
    c.update("old", owner, committed=True)
    writes = roots(env, role="install_write")
    digest = c.impact_digest(c.impact(writes))
    c.fence("maintenance", writes, owner, digest)
    with pytest.raises(CoordinationError, match="frozen"):
        c.reserve(reservation("new", [env], workspace="another-workspace"))
    with pytest.raises(CoordinationError, match="frozen"):
        c.check("old", owner)
    c.check("old", owner, draining=True)
    c.advance_fence("maintenance", owner, "checkpoint_barrier")
    with pytest.raises(CoordinationError, match="frozen"):
        c.check("old", owner, draining=True)
    with pytest.raises(CoordinationError, match="scope_busy"):
        c.fence("competing", writes, owner, digest)


def test_pending_reservation_survives_reopen_and_requires_local_outcome(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    c.reserve(reservation("crashed-before-local-commit", [env]))
    reopened = coordinator(tmp_path)
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    assert reopened.impact(roots(env, role="install_write"))[0]["state"] == "pending"
    with pytest.raises(CoordinationError, match="unconfirmed"):
        reopened.check("crashed-before-local-commit", owner, draining=True)
    with pytest.raises(CoordinationError, match="unconfirmed"):
        reopened.close("crashed-before-local-commit", owner)
    reopened.close("crashed-before-local-commit", owner, local_terminal=True)
    assert not reopened.impact(roots(env, role="install_write"))


def test_control_dependencies_and_stale_preview_refuse_fence(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    writes = roots(env, role="install_write")
    old = c.impact_digest(c.impact(writes))
    c.reserve(reservation("control", [env], kind="control"))
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    with pytest.raises(CoordinationError, match="plan_stale"):
        c.fence("stale", writes, owner, old)
    with pytest.raises(CoordinationError, match="control_environment"):
        c.fence("forbidden", writes, owner, c.impact_digest(c.impact(writes)))


def test_changed_alias_and_live_process_prevent_release(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first)
    c = EnvironmentCoordinator(
        tmp_path / "state" / "shared",
        process_probe=lambda evidence: evidence == {"alive": True},
    )
    value = reservation("kernel", [alias])
    c.reserve(value)
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    c.update("kernel", owner, process={"alive": True}, committed=True)
    alias.unlink()
    alias.symlink_to(second)
    with pytest.raises(CoordinationError, match="scope_changed"):
        c.check("kernel", owner)
    assert c.close("kernel", owner, local_terminal=True) is False
    assert c.get("kernel")["uncertain_exit"] == 1


def competing_writer(directory, env, barrier, queue, maintenance):
    c = EnvironmentCoordinator(Path(directory))
    writes = roots(Path(env), role="install_write")
    digest = c.impact_digest(c.impact(writes))
    barrier.wait()
    try:
        if maintenance:
            c.fence(
                "maintenance",
                writes,
                {"ownerId": "maintenance-owner", "ownerEpoch": 1},
                digest,
            )
        else:
            c.reserve(reservation("admission", [Path(env)]))
        queue.put((maintenance, "accepted"))
    except CoordinationError as error:
        queue.put((maintenance, str(error)))


def test_cross_process_fence_and_reservation_have_one_order(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=competing_writer,
            args=(str(c.directory), str(env), barrier, queue, flag),
        )
        for flag in (True, False)
    ]
    try:
        for process in processes:
            process.start()
        results = dict(queue.get(timeout=10) for _ in processes)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert results in (
            {True: "accepted", False: "environment_maintenance_frozen"},
            {True: "environment_maintenance_plan_stale", False: "accepted"},
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_install_barrier_cannot_advance_past_pending_work_or_live_kernel(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    c.reserve(reservation("kernel", [env]))
    writes = roots(env, role="install_write")
    c.fence("maintenance", writes, owner, c.impact_digest(c.impact(writes)))
    with pytest.raises(CoordinationError, match="unconfirmed"):
        c.advance_fence("maintenance", owner, "checkpoint_barrier")
    c.update("kernel", owner, committed=True)
    c.advance_fence("maintenance", owner, "checkpoint_barrier")
    c.advance_fence("maintenance", owner, "stopping_old_kernels")
    with pytest.raises(CoordinationError, match="unconfirmed"):
        c.advance_fence("maintenance", owner, "installing")
    c.close("kernel", owner, local_terminal=True)
    c.advance_fence("maintenance", owner, "installing")
    with pytest.raises(CoordinationError, match="invalid_state"):
        c.advance_fence("maintenance", owner, "closed")
    c.advance_fence("maintenance", owner, "needs_repair")
    with pytest.raises(CoordinationError, match="needs_repair"):
        c.advance_fence("maintenance", owner, "closed")


def test_uncontained_owner_loss_never_claims_escaped_children_exited(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = EnvironmentCoordinator(tmp_path / "global", process_probe=lambda p: False)
    doc = reservation("kernel", [env])
    old_birth = {"pid": 101, "uid": 1, "boot": "old", "start": "1"}
    doc["identity"].update(ownerProcess=old_birth, containment="uncontained")
    c.reserve(doc)
    c.update(
        "kernel",
        {"ownerId": "owner", "ownerEpoch": 1},
        committed=True,
        has_analysis=True,
    )
    c.starting_kernel("kernel", {"ownerId": "owner", "ownerEpoch": 1})
    replacement = {
        "ownerId": "replacement",
        "ownerEpoch": 2,
        "process": {"pid": 102, "uid": 1, "boot": "new", "start": "2"},
    }
    c.adopt("kernel", replacement, previous_owner_process=old_birth)
    assert c.get("kernel")["uncertain_exit"] == 1
    assert c.close("kernel", replacement, local_terminal=True) is False
    assert c.get("kernel")["uncertain_exit"] == 1


def pending_control_crash(directory, workspace):
    import os

    from omicsclaw.entry.environment_runtime import ProcessEnvironmentGuard

    c = EnvironmentCoordinator(directory)
    c.update = lambda *args, **kwargs: os._exit(91)
    ProcessEnvironmentGuard(workspace, coordinator=c)


def test_pending_control_recovers_its_persisted_owner_birth_after_real_crash(tmp_path):
    c = coordinator(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=pending_control_crash, args=(str(c.directory), str(tmp_path))
    )
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 91
        with c.transaction() as connection:
            row = dict(
                connection.execute(
                    "SELECT * FROM reservations WHERE kind='control'"
                ).fetchone()
            )
        assert row["state"] == "pending" and row["process_json"] is None
        c.reap_dead_controls()
        assert c.get(row["reservation_id"])["state"] == "closed"
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_launch_intent_without_analysis_or_registered_pid_keeps_uncertainty(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    c = coordinator(tmp_path)
    owner = {"ownerId": "owner", "ownerEpoch": 1}
    c.reserve(reservation("kernel", [env]))
    c.update("kernel", owner, committed=True)
    c.starting_kernel("kernel", owner)
    assert c.get("kernel")["has_analysis"] == 0
    assert not c.close("kernel", owner, local_terminal=True)
    assert c.get("kernel")["uncertain_exit"] == 1
    assert c.impact(roots(env, role="install_write"))
