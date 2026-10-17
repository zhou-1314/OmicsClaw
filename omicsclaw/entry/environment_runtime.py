"""Environment occupancy for controllers and the original local kernel surface."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from pathlib import Path

from .environment_coordination import (
    EnvironmentCoordinator,
    coordinator_directory,
    root_evidence,
)
from .environment_scopes import control_read_roots
from .notebook.security import process_evidence, same_process


class ProcessEnvironmentGuard:
    def __init__(
        self, workspace, *, workspace_id=None, role="local_kernel", coordinator=None
    ):
        self.workspace = Path(workspace).resolve(strict=True)
        self.workspace_id = (
            workspace_id or hashlib.sha256(str(self.workspace).encode()).hexdigest()
        )
        self.coordinator = coordinator or EnvironmentCoordinator(
            coordinator_directory()
        )
        self.owner = {"ownerId": uuid.uuid4().hex, "ownerEpoch": 1}
        self.process = process_evidence(os.getpid())
        self.role = role
        self.kernels = {}
        self.register_control()

    def document(self, key, kind, roots, identity):
        return {
            "reservationId": key,
            **self.owner,
            "workspaceId": self.workspace_id,
            "workspacePath": str(self.workspace),
            "resourceId": key,
            "kind": kind,
            "readRoots": roots,
            "identity": {
                **identity,
                "ownerProcess": self.process,
                "surface": self.role,
            },
        }

    def register_control(self):
        self.coordinator.reap_dead_controls()
        self.coordinator.reap_unsent_jobs()
        self.coordinator.reap_finished_jobs()
        key = "control-" + self.owner["ownerId"]
        self.coordinator.reserve(
            self.document(key, "control", control_read_roots(), {"role": self.role})
        )
        self.coordinator.update(key, self.owner, process=self.process, committed=True)

    async def prepare_kernel(self, state, python, wrapper, *, starting=False):
        entry = self.kernels.get(id(state))
        if entry is not None:
            self.coordinator.check(
                entry["id"], self.owner, allow_pending=True, draining=starting
            )
            if starting:
                self.coordinator.update(entry["id"], self.owner, committed=True)
                self.coordinator.starting_kernel(entry["id"], self.owner)
            return
        # Legacy launches retain PYTHONPATH, user profiles and global R specs.
        # Their import roots cannot be inferred from an isolated registry probe.
        roots = [root_evidence("/", role="runtime_read")]
        reason = "legacy_" + state.language + "_scope_unverified"
        key = "kernel-" + uuid.uuid4().hex
        self.coordinator.reserve(
            self.document(
                key,
                "kernel",
                roots,
                {
                    "sessionId": state.session_id,
                    "language": state.language,
                    "reason": reason,
                },
            )
        )
        self.coordinator.update(key, self.owner, has_analysis=True)
        self.kernels[id(state)] = {"id": key, "process": None}
        if starting:
            self.coordinator.update(key, self.owner, committed=True)
            self.coordinator.starting_kernel(key, self.owner)

    def register_kernel(self, state, pid):
        entry = self.kernels[id(state)]
        evidence = process_evidence(pid)
        self.coordinator.update(
            entry["id"], self.owner, process=evidence, committed=True
        )
        entry["process"] = evidence
        return lambda candidate: candidate == pid and same_process(evidence)

    def before_cell(self, state):
        entry = self.kernels[id(state)]
        self.coordinator.check(entry["id"], self.owner, draining=True)
        self.coordinator.update(entry["id"], self.owner, has_analysis=True)

    def kernel_ready(self, state):
        entry = self.kernels[id(state)]
        self.coordinator.confirm_containment(entry["id"], self.owner)

    async def kernel_closed(self, state):
        entry = self.kernels.get(id(state))
        if entry is not None:
            # A signal is not an exit acknowledgement. Await the recorded birth
            # briefly; if it remains alive the coordinator keeps its scope.
            deadline = asyncio.get_running_loop().time() + 1
            while same_process(entry["process"]):
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.01)
            self.coordinator.close(entry["id"], self.owner, local_terminal=True)
            self.kernels.pop(id(state), None)


class LocalKernelEnvironmentGuard:
    """Lazy composition keeps non-analysis chats free of scientific probes."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.guard = None

    async def prepare_kernel(self, state, python, wrapper, *, starting=False):
        if not sys.platform.startswith("linux"):
            return
        if self.guard is None:
            self.guard = ProcessEnvironmentGuard(self.workspace)
        await self.guard.prepare_kernel(state, python, wrapper, starting=starting)

    def register_kernel(self, state, pid):
        return self.guard.register_kernel(state, pid) if self.guard else None

    def before_cell(self, state):
        if self.guard:
            self.guard.before_cell(state)

    def kernel_ready(self, state):
        if self.guard:
            self.guard.kernel_ready(state)

    async def kernel_closed(self, state):
        if self.guard:
            await self.guard.kernel_closed(state)
