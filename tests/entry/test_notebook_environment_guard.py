"""Actual owner admission and dispatch obey the same cross-workspace fence."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from omicsclaw.entry.environment_coordination import (
    CoordinationError,
    EnvironmentCoordinator,
)
from omicsclaw.entry.environment_scopes import (
    registered_read_roots,
    registered_write_roots,
)
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from tests.entry.test_notebook_owner import Manager, settled
from tests.entry.test_notebook_r import bind, command


async def fixture(tmp_path, *, real=False, uncontained=False):
    prefix = tmp_path / "science"
    if real:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "venv",
            "--without-pip",
            "--system-site-packages",
            str(prefix),
        )
        assert await process.wait() == 0
    else:
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib").mkdir()
        (prefix / "bin/python").write_text("fixture")
    coordinator = EnvironmentCoordinator(tmp_path / "global")

    async def probe(spec, workspace, **kwargs):
        return {
            "version": "fixture",
            "prefix": str(prefix),
            "executable": spec["executable"],
            "libraryPaths": [str(prefix / "lib")],
            "packages": {"ipykernel": "1"},
            "missing": [],
            "runtimePaths": {
                "basePrefix": str(prefix),
                "baseExecPrefix": str(prefix),
                "paths": {"stdlib": str(prefix / "lib")},
            },
        }

    owners = []
    for index in range(2):
        workspace = tmp_path / f"workspace-{index}"
        workspace.mkdir()
        runtime = OwnerRuntime(
            workspace,
            {"authorityId": "a" * 32, "workspaceId": str(index) * 32},
            source_digest="fixture",
            env={},
            environment_coordinator=coordinator,
            **({} if real else {"manager": Manager(), "registry_probe": probe}),
        )
        await runtime.start()
        if uncontained:
            runtime.lifecycle_wrapper = None
        environment = await runtime.registry.register(
            {
                "language": "python",
                "executable": str(prefix / "bin/python"),
                "libraryPaths": [],
                "requiredPackages": [],
            }
        )
        assert environment["status"] == "ready", environment
        kernel = (await bind(runtime, "python", environment))["kernel"]
        owners.append((runtime, environment, kernel))
    return coordinator, owners


@pytest.mark.usefixtures("notebook_control_rss")
def test_global_fence_rejects_new_admission_but_drains_both_queued_receipts(tmp_path):
    async def run():
        coordinator, owners = await fixture(tmp_path)
        one, env, kernel = owners[0]
        two, _, other = owners[1]
        one.manager.wait = asyncio.Event()
        try:
            first = await one.admit({"command": command(kernel, "first", "first")})
            second = await one.admit({"command": command(kernel, "second", "second")})
            roots = registered_write_roots(env)
            impact = coordinator.impact(roots)
            assert len([r for r in impact if r["kind"] == "admission"]) == 2
            coordinator.fence(
                "maintenance",
                roots,
                {"ownerId": "maintenance", "ownerEpoch": 1},
                coordinator.impact_digest(impact),
            )
            for owner, selected in ((one, kernel), (two, other)):
                with pytest.raises(CoordinationError, match="frozen"):
                    await owner.admit(
                        {"command": command(selected, "blocked", "must_not_execute")}
                    )
            one.manager.wait.set()
            assert (await settled(one, first["execution_id"]))["status"] == "completed"
            assert (await settled(one, second["execution_id"]))["status"] == "completed"
            assert one.manager.codes == ["first", "second"] and two.manager.codes == []
            impact = coordinator.impact(roots)
            assert {r["kind"] for r in impact} == {"kernel"}
            assert (
                next(r for r in impact if r["workspace_id"] == "0" * 32)["has_analysis"]
                == 1
            )
        finally:
            for owner, _, _ in owners:
                await owner.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_durable_reservations_precede_local_commit_and_dispatch(tmp_path, monkeypatch):
    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, kernel = owners[0]
        original = owner.service._transaction
        witnessed = []

        def transaction(work):
            if work.__name__ == "claim":
                rows = coordinator.impact(registered_write_roots(env))
                pending = [r for r in rows if r["kind"] == "admission"]
                assert len(pending) == 1 and pending[0]["state"] == "pending"
                assert not owner.manager.codes
                witnessed.append("global-before-local")
            return original(work)

        monkeypatch.setattr(owner.service, "_transaction", transaction)
        try:
            receipt = await owner.admit({"command": command(kernel, "first", "first")})
            assert (await settled(owner, receipt["execution_id"]))[
                "status"
            ] == "completed"
            assert witnessed == ["global-before-local"]
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_venv_probe_read_scopes_and_start_guard(tmp_path):
    async def run():
        coordinator, owners = await fixture(tmp_path, real=True, uncontained=True)
        owner, env, kernel = owners[0]
        try:
            roots = registered_read_roots(env)
            assert env["observation"]["prefix"] == str(tmp_path / "science")
            assert any("site-packages" in r["path"] for r in roots)
            assert any(
                Path(r["canonicalPath"]) == Path(sys.executable).resolve()
                for r in roots
            )
            receipt = await owner.admit(
                {"command": command(kernel, "first", "marker = 42; print(marker)")}
            )
            result = await settled(owner, receipt["execution_id"])
            assert result["status"] == "completed", result
            # Local terminal publication precedes the cross-database close callback.
            admission = owner.environment_guard.key(
                "admission", receipt["execution_id"]
            )
            async with asyncio.timeout(5):
                while (
                    coordinator.get(admission)["state"] != "closed"
                    or receipt["execution_id"] in owner.service.tasks
                    or owner.running_notebooks.get("nb") == receipt["execution_id"]
                ):
                    await asyncio.sleep(0)
            reservation = coordinator.get(
                owner.environment_guard.key("kernel", kernel["kernelInstanceId"])
            )
            assert json.loads(reservation["process_json"])["pid"] > 0
            writes = registered_write_roots(env)
            coordinator.fence(
                "maintenance",
                writes,
                {"ownerId": "maintenance", "ownerEpoch": 1},
                coordinator.impact_digest(coordinator.impact(writes)),
            )
            with pytest.raises(CoordinationError, match="frozen"):
                owner.environment_guard.kernel("new-instance", "new-notebook", 1, env)
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            assert coordinator.get(reservation["reservation_id"])["uncertain_exit"] == 1
        finally:
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())


async def crash_fixture(workspace, coordinator, prefix):
    async def probe(spec, workspace, **kwargs):
        return {
            "version": "fixture",
            "prefix": str(prefix),
            "executable": spec["executable"],
            "libraryPaths": [str(prefix / "lib")],
            "packages": {"ipykernel": "1"},
            "missing": [],
            "runtimePaths": {
                "basePrefix": str(prefix),
                "baseExecPrefix": str(prefix),
                "paths": {"stdlib": str(prefix / "lib")},
            },
        }

    owner = OwnerRuntime(
        workspace,
        {"authorityId": "a" * 32, "workspaceId": "b" * 32},
        source_digest="fixture",
        env={},
        manager=Manager(),
        registry_probe=probe,
        environment_coordinator=coordinator,
    )
    await owner.start()
    return owner


def crash_admission(workspace, coordinator, prefix, phase):
    import os

    async def run():
        owner = await crash_fixture(
            Path(workspace), EnvironmentCoordinator(coordinator), Path(prefix)
        )
        environment = await owner.registry.register(
            {
                "language": "python",
                "executable": str(Path(prefix) / "bin/python"),
                "libraryPaths": [],
                "requiredPackages": [],
            }
        )
        kernel = (await bind(owner, "python", environment))["kernel"]
        (Path(workspace) / "kernel.json").write_text(json.dumps(kernel))
        original = owner.service._transaction
        if phase == "before_local":

            def transaction(work):
                if work.__name__ == "claim":
                    os._exit(87)
                return original(work)

            owner.service._transaction = transaction
        else:
            owner.admission_committed = lambda eid: os._exit(88)
        await owner.admit({"command": command(kernel, "crash", "accepted_once")})

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["before_local", "after_local"])
def test_process_death_between_global_and_local_commits_keeps_safe_recovery(
    tmp_path, phase
):
    import multiprocessing

    workspace = tmp_path / "work"
    workspace.mkdir()
    prefix = tmp_path / "science"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    (prefix / "bin/python").write_text("fixture")
    coordinator = EnvironmentCoordinator(tmp_path / "global")
    process = multiprocessing.get_context("spawn").Process(
        target=crash_admission,
        args=(str(workspace), str(coordinator.directory), str(prefix), phase),
    )
    process.start()
    process.join(15)
    try:
        assert process.exitcode == (87 if phase == "before_local" else 88)
        pending = [
            r
            for r in coordinator.workspace_reservations("b" * 32)
            if r["kind"] == "admission"
        ]
        assert len(pending) == 1 and pending[0]["state"] == "pending"

        async def recover():
            owner = await crash_fixture(workspace, coordinator, prefix)
            kernel = json.loads((workspace / "kernel.json").read_text())
            try:
                if phase == "after_local":
                    receipt = await owner.admit(
                        {"command": command(kernel, "crash", "accepted_once")}
                    )
                    assert (await settled(owner, receipt["execution_id"]))[
                        "status"
                    ] == "completed"
                    assert owner.manager.codes == ["accepted_once"]
                else:
                    assert owner.manager.codes == []
                    assert not owner.db.run(
                        lambda c: c.execute(
                            "SELECT 1 FROM notebook_runtime_executions"
                        ).fetchall()
                    )
                # The local terminal receipt precedes the async global close.
                for _ in range(200):
                    if (
                        coordinator.get(pending[0]["reservation_id"])["state"]
                        == "closed"
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert (
                    coordinator.get(pending[0]["reservation_id"])["state"] == "closed"
                )
                await owner.release(
                    {
                        "notebookId": "nb",
                        "commandId": "release",
                        "kernelInstanceId": kernel["kernelInstanceId"],
                        "generation": kernel["generation"],
                    }
                )
            finally:
                await owner.close()

        asyncio.run(recover())
    finally:
        if process.is_alive():
            process.kill()
            process.join()


async def real_settled(owner, receipt):
    for _ in range(600):
        row = owner.views.execution("nb", receipt["execution_id"])
        if (
            row["status"] in {"completed", "failed", "interrupted"}
            and row["durability"] != "pending"
        ):
            assert row["status"] == "completed", row
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("real kernel did not settle")


@pytest.mark.usefixtures("notebook_control_rss")
def test_registered_python_ignores_inherited_and_wrapper_pythonpath(
    tmp_path, monkeypatch
):
    from omicsclaw.kernel.session import SessionKernel

    canary = tmp_path / "injected"
    canary.mkdir()
    (canary / "unexpected_environment_package.py").write_text("marker = 'injected'")
    monkeypatch.setenv("PYTHONPATH", str(canary))
    monkeypatch.setenv("PYTHONSTARTUP", str(canary / "startup.py"))
    coordinator = EnvironmentCoordinator(tmp_path / "global")

    class Wrapper:
        kernel_transport = "tcp"

        def wrap(self, argv, *, env, **kwargs):
            return argv, {**env, "PYTHONPATH": str(canary), "LLM_API_KEY": "canary"}

    original = SessionKernel.start

    def start(kernel):
        assert kernel._python_isolated
        kernel._spawn_wrapper = Wrapper()
        return original(kernel)

    monkeypatch.setattr(SessionKernel, "start", start)

    async def run():
        owner = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="fixture",
            env={},
            environment_coordinator=coordinator,
        )
        await owner.start()
        try:
            from tests.entry.test_notebook_r import environment

            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            receipt = await owner.admit(
                {
                    "command": command(
                        kernel,
                        "canary",
                        """
import importlib.util, os, sys
assert sys.flags.isolated == 1
assert importlib.util.find_spec('unexpected_environment_package') is None
assert 'PYTHONPATH' not in os.environ and 'PYTHONSTARTUP' not in os.environ
assert 'LLM_API_KEY' not in os.environ
print('registered-scope-confirmed')
""",
                    )
                }
            )
            await real_settled(owner, receipt)
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_registered_r_library_fence_does_not_block_python(tmp_path):
    import os

    from omicsclaw.entry.notebook.security import same_process
    from tests.entry.test_notebook_r import environment

    executable = Path(sys.executable).with_name("R")
    if not executable.is_file():
        pytest.skip("R is not installed beside the selected test Python")
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]
    private_library = tmp_path / "private-library"
    private_library.mkdir()
    coordinator = EnvironmentCoordinator(tmp_path / "global")

    async def run():
        owner = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="fixture",
            env={},
            environment_coordinator=coordinator,
        )
        await owner.start()
        try:
            re = await environment(
                owner, "r", real=True, libraries=[str(private_library), *libraries]
            )
            if re["status"] != "ready":
                pytest.skip(
                    "Selected R lacks IRkernel/jsonlite; set OMICSCLAW_TEST_R_LIBRARY"
                )
            py = await environment(owner, "python", real=True)
            r_kernel = (await bind(owner, "r", re))["kernel"]
            py_kernel = (await bind(owner, "python", py))["kernel"]
            receipt = await owner.admit(
                {
                    "command": command(
                        r_kernel,
                        "r-real",
                        "guard_marker <- 42L; stopifnot(guard_marker == 42L); cat(.libPaths(), sep='\\n')",
                    )
                }
            )
            await real_settled(owner, receipt)
            roots = registered_write_roots(re, library_path=str(private_library))
            impact = coordinator.impact(roots)
            assert len(impact) == 1 and impact[0]["kind"] == "kernel"
            assert impact[0]["has_analysis"] == 1
            evidence = json.loads(impact[0]["process_json"])
            assert same_process(evidence)
            assert str(private_library) in {
                r["canonicalPath"] for r in json.loads(impact[0]["roots_json"])
            }
            coordinator.fence(
                "r-maintenance",
                roots,
                {"ownerId": "fixture", "ownerEpoch": 1},
                coordinator.impact_digest(impact),
            )
            with pytest.raises(CoordinationError, match="frozen"):
                await owner.admit(
                    {"command": command(r_kernel, "r-blocked", "guard_marker <- 99L")}
                )
            receipt = await owner.admit(
                {
                    "command": command(
                        py_kernel,
                        "python-separate",
                        "guard_marker = 7; assert guard_marker == 7",
                    )
                }
            )
            await real_settled(owner, receipt)
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-r",
                    "kernelInstanceId": r_kernel["kernelInstanceId"],
                    "generation": r_kernel["generation"],
                }
            )
            assert not same_process(evidence)
            coordinator.reap_contained()
            retained = coordinator.impact(roots)
            if impact[0]["containment_json"]:
                assert retained == []
            else:
                assert len(retained) == 1 and retained[0]["uncertain_exit"] == 1
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.usefixtures("notebook_control_rss")
def test_normal_release_keeps_scope_when_detached_descendant_survives(
    tmp_path, contained
):
    import os
    import signal

    from omicsclaw.entry.notebook.security import process_evidence, same_process
    from omicsclaw.sandbox.netns import probe_netns

    if contained and not probe_netns().available:
        pytest.skip("Native Linux PID namespace unavailable")

    async def run():
        coordinator, owners = await fixture(
            tmp_path, real=True, uncontained=not contained
        )
        owner, env, kernel = owners[0]
        marker = tmp_path / "owned-child.json"
        child = None
        daemon = (
            "import os,sys,time,json; from pathlib import Path; p=os.fork();\n"
            "if p: os._exit(0)\n"
            "os.setsid(); open(sys.argv[1],'w').write(json.dumps({'pid':int(Path('/proc/self').resolve().name)})); time.sleep(60)"
        )
        source = (
            "import subprocess,sys\np=subprocess.Popen([sys.executable,'-c',"
            + repr(daemon)
            + ","
            + repr(str(marker))
            + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\np.wait()"
        )
        try:
            receipt = await owner.admit(
                {"command": command(kernel, "detached", source)}
            )
            await real_settled(owner, receipt)
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            child = process_evidence(json.loads(marker.read_text())["pid"])
            assert same_process(child)
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            coordinator.reap_contained()
            rows = coordinator.impact(registered_write_roots(env))
            if contained:
                assert not same_process(child)
                assert all(
                    row["resource_id"] != kernel["kernelInstanceId"] for row in rows
                )
                return
            assert same_process(child)
            retained = next(
                row for row in rows if row["resource_id"] == kernel["kernelInstanceId"]
            )
            assert retained["uncertain_exit"] == 1 and retained["state"] != "closed"
            assert (
                owner.actions.binding({"sessionId": "chat"})["kernels"]["python"][
                    "state"
                ]
                == "released"
            )
        finally:
            for runtime, _, _ in owners:
                await runtime.close()
            if child and same_process(child):
                argv = (
                    (Path("/proc") / str(child["pid"]) / "cmdline")
                    .read_bytes()
                    .split(b"\0")
                )
                assert str(marker).encode() in argv
                os.kill(child["pid"], signal.SIGTERM)
                for _ in range(200):
                    if not same_process(child):
                        break
                    await asyncio.sleep(0.01)
                assert not same_process(child)

    asyncio.run(run())


@pytest.mark.parametrize(
    "language,mode", [("python", "off"), ("r", "off"), ("python", "netns")]
)
@pytest.mark.usefixtures("notebook_control_rss")
def test_actual_native_owner_release_closes_only_with_namespace_proof(
    tmp_path, language, mode
):
    import os

    from omicsclaw.entry.notebook.security import same_process
    from omicsclaw.sandbox.config import EgressMode, SandboxConfig, SandboxMode
    from omicsclaw.sandbox.netns import probe_netns
    from tests.entry.test_notebook_r import environment

    if not probe_netns().available:
        pytest.skip("Native Linux PID namespace unavailable")
    libraries = os.environ.get("OMICSCLAW_TEST_R_LIBRARY")
    if language == "r" and not libraries:
        pytest.skip("Set OMICSCLAW_TEST_R_LIBRARY to an IRkernel library")

    async def run():
        coordinator = EnvironmentCoordinator(tmp_path / "global")
        owner = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="native-fixture",
            env={},
            config=SandboxConfig(
                mode=SandboxMode(mode),
                egress=EgressMode.NONE if mode == "netns" else EgressMode.OPEN,
            ),
            environment_coordinator=coordinator,
        )
        await owner.start()
        try:
            env = await environment(
                owner, language, real=True, libraries=[libraries] if libraries else None
            )
            assert env["status"] == "ready", env
            kernel = (await bind(owner, language, env))["kernel"]
            receipt = await owner.admit(
                {"command": command(kernel, "actual", "print(42)")}
            )
            for _ in range(600):
                result = owner.db.run(
                    lambda c: dict(
                        c.execute(
                            "SELECT * FROM notebook_executions WHERE execution_id=?",
                            (receipt["execution_id"],),
                        ).fetchone()
                    )
                )
                if result["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.05)
            assert result["status"] == "completed", result
            rid = owner.environment_guard.key("kernel", kernel["kernelInstanceId"])
            row = coordinator.get(rid)
            proof = json.loads(row["containment_json"] or "null")
            assert proof is not None, row
            assert same_process(proof["root"]) and same_process(
                proof.get("init") or proof["child"]
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-native",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                    "language": language,
                }
            )
            for _ in range(200):
                coordinator.reap_contained()
                if coordinator.get(rid)["state"] == "closed":
                    break
                await asyncio.sleep(0.01)
            assert coordinator.get(rid)["state"] == "closed"
            assert not same_process(proof["root"]) and not same_process(
                proof.get("init") or proof["child"]
            )
        finally:
            await owner.close()

    asyncio.run(run())
