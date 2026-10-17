"""Inspect a selected scientific interpreter without importing it into the controller."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import stat
import tempfile
from pathlib import Path

from omicsclaw.tools.builtin.bash import spawn_group_leader, without_control_credentials

PROBE_TIMEOUT_S = 15.0
PROBE_OUTPUT_BYTES = 256 * 1024
PROBE_CLEANUP_S = 0.5
_PYTHON_WORKER = Path(__file__).resolve().parents[2] / "skillenv/registered_probe.py"

_R = r"""
args <- commandArgs(trailingOnly=TRUE)
if (length(args) > 0L && identical(args[[1]], "--args")) args <- args[-1L]
nlibs <- as.integer(args[[1]])
libs <- args[seq_len(nlibs) + 1L]
.libPaths(c(libs, .Library))
required <- args[seq.int(nlibs + 2L, length(args))]
missing <- required[!vapply(required, requireNamespace, logical(1), quietly=TRUE)]
if (!requireNamespace("jsonlite", quietly=TRUE)) quit(status=42)
packages <- installed.packages(lib.loc=.libPaths())
packages <- packages[!duplicated(packages[, "Package"]), , drop=FALSE]
if (nrow(packages) > 2048) stop("package_inventory_limit")
versions <- as.list(setNames(packages[, "Version"], packages[, "Package"]))
cat(jsonlite::toJSON(list(version=as.character(getRversion()), prefix=R.home(),
 executable=normalizePath(file.path(R.home("bin"), "R")),
 libraryPaths=I(.libPaths()), packages=versions, missing=I(missing),
 runtimePaths=list(basePrefix=R.home(), baseExecPrefix=R.home(),
 paths=list(stdlib=R.home("library"), scripts=R.home("bin")))), auto_unbox=TRUE))
"""


def path_evidence(path: str, *, directory: bool = False) -> dict:
    """Keep the launch spelling: resolving a venv's Python changes its prefix."""
    selected = Path(path)
    resolved = selected.resolve(strict=True)
    info = resolved.stat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError("environment_path_type")
    if info.st_mode & 0o002 or (not directory and not os.access(selected, os.X_OK)):
        raise ValueError("environment_path_permissions")
    for parent in (
        selected.parent,
        *selected.parents,
        resolved.parent,
        *resolved.parents,
    ):
        mode = parent.stat().st_mode
        if mode & 0o002 and not mode & stat.S_ISVTX:
            raise ValueError("environment_path_permissions")
    return {
        "path": path,
        "resolved": str(resolved),
        "device": info.st_dev,
        "inode": info.st_ino,
        "ownerUid": info.st_uid,
        "groupGid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "mtimeNs": info.st_mtime_ns,
    }


async def probe_environment(
    spec: dict,
    workspace: Path,
    *,
    wrapper=None,
    timeout=PROBE_TIMEOUT_S,
    output_limit=PROBE_OUTPUT_BYTES,
) -> dict:
    """Run a probe with separate execution and cleanup deadlines.

    A process group cannot contain descendants that create another session.
    Native PID namespaces provide that containment; without one, closing our
    pipe bounds observation but does not claim that escaped children exited.
    """
    evidence = [path_evidence(spec["executable"])]
    evidence += [path_evidence(path, directory=True) for path in spec["libraryPaths"]]
    required = sorted(
        set(spec["requiredPackages"])
        | ({"ipykernel"} if spec["language"] == "python" else {"IRkernel", "jsonlite"})
    )
    with tempfile.TemporaryDirectory(prefix="omicsclaw-env-probe-") as temporary:
        private = Path(temporary)
        environment = without_control_credentials()
        for name in list(environment):
            if name.startswith(
                ("PYTHON", "R_LIBS", "R_ENVIRON", "R_PROFILE")
            ) or name in {"R_HOME", "VIRTUAL_ENV", "CONDA_PREFIX"}:
                environment.pop(name)
        environment.update(
            HOME=temporary, TMPDIR=temporary, PYTHONDONTWRITEBYTECODE="1"
        )
        if spec["language"] == "python":
            argv = [
                spec["executable"],
                "-I",
                "-B",
                str(_PYTHON_WORKER),
                json.dumps(required),
            ]
        else:
            environment.update(
                R_LIBS_USER=os.pathsep.join(spec["libraryPaths"]), R_LIBS_SITE=""
            )
            argv = [
                spec["executable"],
                "--vanilla",
                "--slave",
                "-e",
                _R,
                "--args",
                str(len(spec["libraryPaths"])),
                *spec["libraryPaths"],
                *required,
            ]
        if wrapper is not None:
            argv, environment = wrapper.wrap(
                argv,
                cwd=private,
                env=environment,
                extra_read_paths=[
                    Path(spec["executable"]).parent.parent,
                    *(Path(p) for p in spec["libraryPaths"]),
                ],
                extra_write_paths=[private],
            )
        process = await spawn_group_leader(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=private,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        from omicsclaw.entry.notebook.security import process_evidence, same_process
        birth = process_evidence(process.pid)
        try:
            if getattr(wrapper, "after_spawn", None):
                await wrapper.after_spawn(process)
            async with asyncio.timeout(timeout):
                output = await process.stdout.read(output_limit + 1)
                # StreamReader.read(n) may return a short chunk before EOF.
                while len(output) <= output_limit:
                    chunk = await process.stdout.read(
                        min(16384, output_limit + 1 - len(output))
                    )
                    if not chunk:
                        break
                    output += chunk
                if len(output) > output_limit:
                    raise ValueError("probe_output_limit")
                await process.wait()
                if process.returncode == 42 and spec["language"] == "r":
                    raise ValueError("probe_missing_jsonlite")
                if process.returncode != 0:
                    raise ValueError("probe_failed")
        except TimeoutError as error:
            raise ValueError("probe_timeout") from error
        finally:
            try:
                if getattr(wrapper, "before_cleanup", None):
                    await wrapper.before_cleanup(process)
            finally:
                if same_process(birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                # An escaped writer cannot keep pipe draining alive indefinitely.
                process._transport.close()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(PROBE_CLEANUP_S):
                        await process.wait()
        try:
            observed = json.loads(output)
            if (
                not isinstance(observed, dict)
                or set(observed) - {"runtimePaths"}
                != {
                    "version",
                    "prefix",
                    "executable",
                    "libraryPaths",
                    "packages",
                    "missing",
                }
                or not all(
                    isinstance(observed[key], str)
                    for key in ("version", "prefix", "executable")
                )
                or not isinstance(observed["packages"], dict)
                or not isinstance(observed["libraryPaths"], list)
                or not isinstance(observed["missing"], list)
            ):
                raise ValueError("probe_invalid_result")
        except (ValueError, UnicodeError) as error:
            raise ValueError("probe_invalid_result") from error
        runtime = observed.get("runtimePaths")
        if runtime is not None and (
            not isinstance(runtime, dict)
            or set(runtime) - {"editableRoots"}
            != {"basePrefix", "baseExecPrefix", "paths"}
            or not all(
                isinstance(runtime[key], str) and Path(runtime[key]).is_absolute()
                for key in ("basePrefix", "baseExecPrefix")
            )
            or not isinstance(runtime.get("editableRoots", []), list)
            or len(runtime.get("editableRoots", [])) > 64
            or any(
                not isinstance(p, str) or not Path(p).is_absolute()
                for p in runtime.get("editableRoots", [])
            )
            or not isinstance(runtime["paths"], dict)
            or len(runtime["paths"]) > 32
            or any(
                not isinstance(k, str)
                or not isinstance(v, str)
                or not Path(v).is_absolute()
                for k, v in runtime["paths"].items()
            )
        ):
            raise ValueError("probe_invalid_result")
        after = [path_evidence(spec["executable"])]
        after += [path_evidence(path, directory=True) for path in spec["libraryPaths"]]
        if evidence != after:
            raise ValueError("environment_changed_during_probe")
        observed.update(
            launchExecutable=spec["executable"],
            pathEvidence=evidence,
            maintenanceRoots=sorted(
                {
                    str(Path(observed["prefix"]).resolve()),
                    *(str(Path(p).resolve()) for p in spec["libraryPaths"]),
                }
            ),
        )
        return observed
