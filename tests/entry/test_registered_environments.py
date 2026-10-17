"""Existing environments keep their own identity and bounded probe evidence."""

import asyncio
import sys

import pytest

from omicsclaw.entry.ssh.environments import EnvironmentRegistry, probe_environment
from omicsclaw.memory.database import Database


def spec(executable=None, **values):
    return {
        "language": "python",
        "executable": executable or sys.executable,
        "libraryPaths": [],
        "requiredPackages": [],
        **values,
    }


def test_real_python_registration_persists_identity_and_revision(tmp_path):
    async def run():
        database = Database(tmp_path / "memory.db")
        registry = EnvironmentRegistry(database, tmp_path)
        first = await registry.register(spec())
        assert first["status"] == "ready"
        assert first["observation"]["prefix"] == sys.prefix
        assert first["observation"]["packages"]["ipykernel"]
        assert first["kind"] == "registered"
        assert first["revision"] == 1
        again = await registry.register(spec())
        assert again["environmentId"] == first["environmentId"]
        assert again["revision"] == 1
        database.close()
        database = Database(tmp_path / "memory.db")
        restored = EnvironmentRegistry(database, tmp_path)
        assert (
            restored.page()["environments"][0]["environmentId"]
            == first["environmentId"]
        )
        database.close()

    asyncio.run(run())


def test_missing_dependency_is_not_ready_and_no_install_occurs(tmp_path):
    async def run():
        with Database(":memory:") as database:
            row = await EnvironmentRegistry(database, tmp_path).register(
                spec(requiredPackages=["omicsclaw_nonexistent_probe_package"])
            )
            assert row["status"] == "missing_dependencies"
            assert (
                "omicsclaw_nonexistent_probe_package" in row["observation"]["missing"]
            )

    asyncio.run(run())


def test_drift_invalidates_previous_revision_until_explicit_acceptance(tmp_path):
    version = ["1"]

    async def probe(*args, **kwargs):
        return {
            "version": version[0],
            "prefix": "/science",
            "packages": {"ipykernel": version[0]},
            "missing": [],
            "executable": "/science/python",
            "libraryPaths": [],
            "pathEvidence": [],
        }

    async def run():
        with Database(":memory:") as database:
            registry = EnvironmentRegistry(database, tmp_path, probe=probe)
            original = await registry.register(spec())
            version[0] = "2"
            changed = await registry.preflight(original["environmentId"])
            assert changed["revision"] == 2 and changed["status"] == "changed"
            with pytest.raises(ValueError, match="environment_revision_conflict"):
                registry.accept(original["environmentId"], 1)
            assert (await registry.preflight(original["environmentId"]))[
                "status"
            ] == "changed"
            accepted = registry.accept(original["environmentId"], 2)
            assert accepted["status"] == "ready"
            assert registry.get_revision(original["environmentId"], 1)["version"] == "1"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["timeout", "output"])
def test_probe_is_bounded_and_does_not_return_raw_child_output(tmp_path, kind):
    executable = tmp_path / "probe"
    executable.write_text(
        "#!/bin/sh\n"
        + ("exec sleep 10\n" if kind == "timeout" else "yes sensitive-output\n")
    )
    executable.chmod(0o700)

    async def run():
        with pytest.raises(
            ValueError,
            match="probe_timeout" if kind == "timeout" else "probe_output_limit",
        ):
            await probe_environment(
                spec(str(executable)), tmp_path, timeout=0.15, output_limit=1024
            )

    asyncio.run(run())


def test_python_venv_launch_path_is_not_replaced_by_resolved_binary(tmp_path):
    import venv

    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(tmp_path / "venv")
    selected = tmp_path / "venv/bin/python"

    async def run():
        result = await probe_environment(spec(str(selected)), tmp_path)
        assert result["prefix"] == str(tmp_path / "venv")
        assert result["launchExecutable"] == str(selected)

    asyncio.run(run())


def test_probe_scrubs_credentials_and_ambient_python_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-pass-this")
    monkeypatch.setenv("PYTHONPATH", "/ambient/must-not-load")
    wrapper = tmp_path / "python"
    wrapper.write_text(
        f'#!/bin/sh\n[ -z "$OPENAI_API_KEY" ] && [ -z "$PYTHONPATH" ] || exit 77\nexec {sys.executable} "$@"\n'
    )
    wrapper.chmod(0o700)
    result = asyncio.run(probe_environment(spec(str(wrapper)), tmp_path))
    assert result["missing"] == []


def test_reject_invalid_r_paths_and_unknown_schema(tmp_path):
    async def run():
        with Database(":memory:") as database:
            registry = EnvironmentRegistry(database, tmp_path)
            for document in [
                spec(extra=True),
                spec(executable="python"),
                spec(language="r", libraryPaths=[]),
                spec(requiredPackages=["os;print('bad')"]),
            ]:
                with pytest.raises(ValueError):
                    await registry.register(document)

    asyncio.run(run())


def test_controller_registry_fences_target_and_keeps_notebook_execution_lazy(tmp_path):
    from types import SimpleNamespace
    from omicsclaw.entry.ssh.controller import Controller
    from omicsclaw.entry.ssh.protocol import RpcError

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(
                {
                    "provider_id": "test",
                    "dialect": "openai_chat",
                    "model": "test",
                    "allowed_models": ["test"],
                    "context_tokens": 8192,
                    "output_tokens": 1024,
                }
            )
            page = await controller.environment_list({})
            assert page["notebook"] == {
                "enabled": True, "reason": "", "ownerRpcAvailable": True,
            }
            assert controller.app.kernels is controller.notebook_binding
            assert controller.app.kernels.client is controller.notebook_client
            assert not hasattr(controller.app.kernels, "manager")
            assert controller.notebook_client.health is None
            assert not (tmp_path / ".omicsclaw/notebooks.db").exists()
            with pytest.raises(RpcError, match="environment_target_changed"):
                await controller.environment_register(
                    {
                        "target": {**page["target"], "controllerEpoch": "old"},
                        "spec": spec(),
                    }
                )
            created = await controller.environment_register(
                {"target": page["target"], "spec": spec()}
            )
            assert created["environment"]["status"] == "ready"
            assert len((await controller.environment_list({}))["environments"]) == 1
            assert (
                "packages"
                not in (await controller.environment_list({}))["environments"][0][
                    "observation"
                ]
            )
            # Registering and listing only probe the selected interpreter. The
            # owner DB holds scientific bindings and instances once analysis starts.
            assert controller.notebook_client.health is None
            assert not (tmp_path / ".omicsclaw/notebooks.db").exists()
            assert not (tmp_path / ".omicsclaw/kernel").exists()
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_cancelled_probe_keeps_registration_unverified(tmp_path):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    async def run():
        with Database(":memory:") as database:
            registry = EnvironmentRegistry(database, tmp_path, probe=cancelled)
            with pytest.raises(asyncio.CancelledError):
                await registry.register(spec())
            row = registry.page()["environments"][0]
            assert row["status"] == "unverified"
            with pytest.raises(ValueError, match="environment_revision_conflict"):
                registry.require_ready(row["environmentId"], 0)

    asyncio.run(run())


def test_controller_ensure_default_environments_and_rscript_probe(tmp_path):
    from types import SimpleNamespace
    from omicsclaw.entry.ssh.controller import Controller

    async def run():
        controller = Controller(tmp_path, SimpleNamespace(handlers={}))
        try:
            await controller.configure(
                {
                    "provider_id": "test",
                    "dialect": "openai_chat",
                    "model": "test",
                    "allowed_models": ["test"],
                    "context_tokens": 8192,
                    "output_tokens": 1024,
                }
            )
            await controller._ensure_default_environments(force=True)
            envs = (await controller.environment_list({}))["environments"]
            assert any(
                row["spec"]["language"] == "python" and row["status"] == "ready"
                for row in envs
            )
        finally:
            await controller.aclose()

    asyncio.run(run())
