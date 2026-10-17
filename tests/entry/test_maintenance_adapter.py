"""Actual offline installers consume approved copies in disposable environments."""

import asyncio
import hashlib
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from omicsclaw.entry.environment_coordination import CoordinationError
from omicsclaw.entry.maintenance_adapter import install_plan, prepare_plan, verify_plan
from omicsclaw.entry.maintenance_artifacts import prepare_artifacts
from omicsclaw.entry.ssh.environment_probe import probe_environment
from omicsclaw.sandbox.manager import probe_sandbox_backend


def wheel(directory, version, *, code=None, requires=""):
    package = "omicsclaw_maintenance_test"
    name = f"{package}-{version}-py3-none-any.whl"
    path = directory / name
    metadata = f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n"
    if requires:
        metadata += f"Requires-Dist: {requires}\n"
    dist = f"{package}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", code or "value = 2\n")
        archive.writestr(f"{dist}/METADATA", metadata)
        archive.writestr(
            f"{dist}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist}/RECORD", "")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def python_operation(artifact, version):
    return {
        "adapter": "python_wheels",
        "artifacts": [artifact],
        "checks": [
            {
                "package": "omicsclaw-maintenance-test",
                "module": "omicsclaw_maintenance_test",
                "version": version,
            }
        ],
    }


def available():
    report = probe_sandbox_backend()
    if not report.available:
        pytest.skip("Offline installer requires native sandbox: " + report.reason)


async def python_environment(tmp_path):
    prefix = tmp_path / "science"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "venv",
        "--without-pip",
        "--system-site-packages",
        str(prefix),
    )
    assert await process.wait() == 0
    spec = {
        "language": "python",
        "executable": str(prefix / "bin/python"),
        "libraryPaths": [],
        "requiredPackages": [],
    }
    return {"spec": spec, "observation": await probe_environment(spec, tmp_path)}


def test_approved_private_wheel_bytes_survive_source_replacement(tmp_path):
    available()

    async def run():
        environment = await python_environment(tmp_path)
        artifact = wheel(tmp_path, "0.0.2")
        directory = tmp_path / "private"
        plan, private = await prepare_plan(
            environment, python_operation(artifact, "0.0.2"), directory
        )
        assert plan["network"] == "deny" and plan["sandbox"]["backend"] in {
            "netns",
            "bwrap",
            "seatbelt",
            "appcontainer",
        }
        wheel(tmp_path, "0.0.2", code="value = 'replaced'\n")
        written = []
        await install_plan(
            plan, private, directory, before_write=lambda: written.append(True)
        )
        assert written == [True]
        await verify_plan(private, directory)
        result = await asyncio.to_thread(
            subprocess.check_output,
            [
                environment["spec"]["executable"],
                "-I",
                "-c",
                "import omicsclaw_maintenance_test; print(omicsclaw_maintenance_test.value)",
            ],
            text=True,
        )
        assert result.strip() == "2"
        assert not Path(
            sys.prefix, "lib/python3.11/site-packages/omicsclaw_maintenance_test"
        ).exists()

    asyncio.run(run())


def test_missing_wheel_dependency_is_refused_without_install(tmp_path):
    available()

    async def run():
        environment = await python_environment(tmp_path)
        artifact = wheel(
            tmp_path, "0.0.4", requires="omicsclaw-impossible-dependency-for-test>=999"
        )
        with pytest.raises(CoordinationError, match="invalid_plan"):
            await prepare_plan(
                environment, python_operation(artifact, "0.0.4"), tmp_path / "private"
            )
        assert not list((tmp_path / "science").rglob("omicsclaw_maintenance_test"))

    asyncio.run(run())


def test_installed_wheel_import_failure_requires_repair(tmp_path):
    available()

    async def run():
        environment = await python_environment(tmp_path)
        artifact = wheel(
            tmp_path, "0.0.3", code="raise RuntimeError('fixture import failure')\n"
        )
        directory = tmp_path / "private"
        plan, private = await prepare_plan(
            environment, python_operation(artifact, "0.0.3"), directory
        )
        await install_plan(plan, private, directory, before_write=lambda: None)
        with pytest.raises(CoordinationError, match="needs_repair"):
            await verify_plan(private, directory)
        assert list((tmp_path / "science").rglob("omicsclaw_maintenance_test"))

    asyncio.run(run())


def test_archive_escape_and_digest_changes_are_rejected(tmp_path):
    artifact = wheel(tmp_path, "0.0.2")
    with zipfile.ZipFile(artifact["path"], "a") as archive:
        archive.writestr("../../outside.py", "bad")
    with pytest.raises(CoordinationError, match="invalid_artifact"):
        prepare_artifacts(
            python_operation(artifact, "0.0.2"), "python", tmp_path / "digest"
        )
    artifact["sha256"] = hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
    with pytest.raises(CoordinationError, match="invalid_artifact"):
        prepare_artifacts(
            python_operation(artifact, "0.0.2"), "python", tmp_path / "escape"
        )


def test_real_r_private_library_source_install_and_verify(tmp_path):
    available()
    executable = Path(sys.executable).with_name("R")
    if not executable.is_file():
        pytest.skip("R is not installed beside the selected test Python")
    libraries = [
        p for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep) if p
    ]
    library = tmp_path / "library"
    library.mkdir()
    source = tmp_path / "omicsclawMaintenanceTest"
    (source / "R").mkdir(parents=True)
    (source / "DESCRIPTION").write_text(
        "Package: omicsclawMaintenanceTest\nVersion: 0.0.2\nTitle: Maintenance Test\nDescription: A disposable test package.\nLicense: MIT\nAuthor: Fixture\nMaintainer: Fixture <fixture@example.invalid>\n"
    )
    (source / "NAMESPACE").write_text("export(value)\n")
    (source / "R/value.R").write_text("value <- function() 2L\n")
    archive = tmp_path / "omicsclawMaintenanceTest_0.0.2.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname=source.name)

    async def run():
        spec = {
            "language": "r",
            "executable": str(executable),
            "libraryPaths": [str(library), *libraries],
            "requiredPackages": [],
        }
        observation = await probe_environment(spec, tmp_path)
        if observation["missing"]:
            pytest.skip(
                "Selected R lacks IRkernel/jsonlite; set OMICSCLAW_TEST_R_LIBRARY"
            )
        environment = {"spec": spec, "observation": observation}
        operation = {
            "adapter": "r_source",
            "libraryPath": str(library),
            "artifacts": [
                {
                    "path": str(archive),
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            ],
            "checks": [{"package": "omicsclawMaintenanceTest", "version": "0.0.2"}],
        }
        directory = tmp_path / "private"
        plan, private = await prepare_plan(environment, operation, directory)
        assert plan["packages"][0]["beforeVersion"] is None
        await install_plan(plan, private, directory, before_write=lambda: None)
        checked = await verify_plan(private, directory)
        assert checked["checks"][0]["version"] == "0.0.2"
        assert (library / "omicsclawMaintenanceTest/DESCRIPTION").is_file()

    asyncio.run(run())


@pytest.mark.parametrize("language", ["python", "r"])
def test_network_deny_keeps_host_pids_and_real_science_working(tmp_path, language):
    import socket

    from omicsclaw.entry.maintenance_process import MaintenanceProcess
    from omicsclaw.entry.process_containment import namespace

    available()

    async def run():
        if language == "python":
            environment = await python_environment(tmp_path)
        else:
            executable = Path(sys.executable).with_name("R")
            if not executable.is_file():
                pytest.skip("R not installed beside the test Python")
            libraries = os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep)
            spec = {
                "language": "r",
                "executable": str(executable),
                "libraryPaths": [p for p in libraries if p],
                "requiredPackages": [],
            }
            environment = {
                "spec": spec,
                "observation": await probe_environment(spec, tmp_path),
            }
        private = tmp_path / "process"
        private.mkdir()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        try:
            async with MaintenanceProcess(private, environment) as process:
                if language == "python":
                    argv = [
                        environment["spec"]["executable"],
                        "-I",
                        "-c",
                        f"import numpy as np,socket,json; from pathlib import Path; a=np.arange(256).reshape(16,16); assert (a@a).shape==(16,16); s=socket.socket(); s.settimeout(.2); assert s.connect_ex(('127.0.0.1',{port})) != 0; print('science-and-deny'); print(Path('/proc/self/ns/pid').stat().st_ino)",
                    ]
                else:
                    argv = [
                        environment["spec"]["executable"],
                        "--vanilla",
                        "--slave",
                        "-e",
                        f"stopifnot(identical(colSums(matrix(1:4,2)),c(3,7))); stopifnot(identical(dim(crossprod(matrix(1:256,16))),c(16L,16L))); s<-try(suppressWarnings(socketConnection('127.0.0.1',port={port},open='r+',timeout=.2)),silent=TRUE); stopifnot(inherits(s,'try-error')); cat('science-and-deny')",
                    ]
                result = await process.run(argv, timeout=30)
                assert result["exitCode"] == 0, result
                assert (
                    result["exitConfirmed"] and "science-and-deny" in result["output"]
                )
                assert result["containment"]["kind"] == "linux_subreaper"
                if language == "python" and result["backend"] == "netns":
                    assert (
                        str(namespace(Path("/proc/self/ns/pid"))[1]) in result["output"]
                    )
        finally:
            listener.close()

    asyncio.run(run())
