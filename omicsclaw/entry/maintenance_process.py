"""Bounded maintenance children under an independent network-deny policy."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import signal
import sys
from pathlib import Path

from omicsclaw.sandbox.config import BackendName, EgressMode, SandboxConfig, SandboxMode
from omicsclaw.sandbox.manager import SandboxManager
from omicsclaw.tools.builtin.bash import spawn_group_leader, without_control_credentials

from .environment_coordination import CoordinationError
from .environment_scopes import registered_read_roots
from .notebook.security import process_evidence, same_process
from .process_containment import (
    SubreaperLifecycleWrapper,
    contained_exit,
    gated_argv,
    open_gate,
    wait_containment,
)


class MaintenanceProcess:
    def __init__(self, directory, environment, *, config=None):
        self.directory = Path(directory)
        self.environment = environment
        base = config or SandboxConfig(mode=SandboxMode.AUTO)
        self.config = dataclasses.replace(
            base,
            mode=SandboxMode.AUTO if base.mode == SandboxMode.OFF else base.mode,
            backend=BackendName.AUTO
            if base.backend == BackendName.OFF
            else base.backend,
            egress=EgressMode.NONE,
            sandbox_kernels=True,
            egress_groups=(),
            egress_allow=(),
            egress_endpoints=(),
            allowed_domains=frozenset(),
            extra_allowed_domains=frozenset(),
            extra_write_paths=(),
        )
        self.manager = None
        self.process = None
        self.birth = None
        self.containment = None
        self.exit_confirmed = False
        self.lifecycle = None

    async def __aenter__(self):
        try:
            self.manager = SandboxManager(
                self.directory,
                config=self.config,
                env_provider=without_control_credentials,
            )
            await self.manager.start()
            self.lifecycle = SubreaperLifecycleWrapper(
                self.directory / "process-lifecycles",
                inner=self.manager.spawn_wrapper(host_pid_lifecycle=True),
            )
        except Exception as error:
            if self.manager is not None:
                await self.manager.stop()
            raise CoordinationError(
                "environment_maintenance_sandbox_unavailable"
            ) from error
        return self

    async def __aexit__(self, *args):
        await self.stop()
        if self.manager is not None:
            await self.manager.stop()

    @property
    def evidence(self):
        return {
            "backend": str(self.manager.backend_name),
            "level": str(self.manager.level),
            "network": "deny",
            "processLifecycle": "host_subreaper",
            "policyScope": "maintenance",
        }

    async def stop(self):
        process, birth = self.process, self.birth
        if process is None:
            return
        if same_process(birth) and self.lifecycle:
            await asyncio.to_thread(
                self.lifecycle.terminate, process.pid, lambda pid: same_process(birth)
            )
        if same_process(birth):
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)
        process._transport.close()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(1):
                await process.wait()
        self.exit_confirmed = contained_exit(self.containment, birth)
        self.process = self.birth = None

    async def run(
        self,
        argv,
        *,
        timeout=30,
        maximum=256 * 1024,
        write_roots=(),
        on_spawn=None,
        before_spawn=None,
        before_execute=None,
        on_result=None,
    ):
        env = without_control_credentials()
        for name in tuple(env):
            if name.startswith(
                ("PYTHON", "R_LIBS", "R_ENVIRON", "R_PROFILE")
            ) or name in {"R_HOME", "VIRTUAL_ENV", "CONDA_PREFIX"}:
                env.pop(name)
        private_home = self.directory / "home"
        private_home.mkdir(exist_ok=True)
        env.update(
            HOME=str(private_home),
            TMPDIR=str(self.directory),
            PYTHONDONTWRITEBYTECODE="1",
            PIP_CONFIG_FILE=os.devnull,
        )
        spec = self.environment["spec"]
        if spec["language"] == "r":
            env.update(
                R_LIBS_USER=os.pathsep.join(spec["libraryPaths"]), R_LIBS_SITE=""
            )
        waiting, gate, token = gated_argv(argv, self.directory, python=sys.executable)
        argv, env = self.lifecycle.wrap(
            waiting,
            cwd=self.directory,
            env=env,
            extra_read_paths=[
                *(
                    Path(p["canonicalPath"])
                    for p in registered_read_roots(self.environment)
                ),
                Path(__file__).resolve().parents[1],
                self.directory,
            ],
            extra_write_paths=[self.directory, *(Path(p) for p in write_roots)],
        )
        env = without_control_credentials(env)
        if before_spawn:
            before_spawn()
        process = await spawn_group_leader(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=self.directory,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        )
        self.process, self.birth = process, process_evidence(process.pid)
        if self.birth is None:
            await self.stop()
            raise CoordinationError("environment_maintenance_unconfirmed")
        birth = self.birth
        data = bytearray()
        try:
            self.containment = await wait_containment(self.birth)
            if self.containment is None:
                raise CoordinationError("environment_maintenance_unconfirmed")
            if on_spawn:
                on_spawn(self.birth, self.containment)
            if before_execute:
                before_execute()
            open_gate(gate, token)
            async with asyncio.timeout(timeout):
                while chunk := await process.stdout.read(
                    min(16384, maximum + 1 - len(data))
                ):
                    data.extend(chunk)
                    if len(data) > maximum:
                        raise CoordinationError(
                            "environment_maintenance_resource_limit"
                        )
                await process.wait()
            await self.stop()
            if not self.exit_confirmed:
                raise CoordinationError("environment_maintenance_unconfirmed")
            return {
                "process": birth,
                "exitCode": process.returncode,
                "containment": self.containment,
                "exitConfirmed": True,
                "output": bytes(data).decode("utf-8", errors="replace"),
                **self.evidence,
            }
        except TimeoutError as error:
            raise CoordinationError("environment_maintenance_resource_limit") from error
        finally:
            await self.stop()
            gate.unlink(missing_ok=True)
            if on_result:
                on_result(
                    {
                        "process": birth,
                        "exitCode": process.returncode,
                        "exitConfirmed": self.exit_confirmed,
                        "output": bytes(data[:maximum]).decode(
                            "utf-8", errors="replace"
                        ),
                        "outputTruncated": len(data) > maximum,
                    }
                )
