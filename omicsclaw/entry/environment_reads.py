"""A registered-environment probe is a bounded, globally reserved reader."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .environment_coordination import CoordinationError, root_evidence
from .environment_scopes import registered_read_roots
from .notebook.security import private_directory, process_evidence, same_process
from .process_containment import (
    SubreaperLifecycleWrapper,
    gated_argv,
    open_gate,
    wait_containment,
)


class EnvironmentReadGuard:
    def __init__(self, coordinator, workspace, workspace_id, owner):
        self.coordinator, self.workspace, self.workspace_id = (
            coordinator,
            Path(workspace),
            workspace_id,
        )
        self.owner = owner
        self.allowed = contextvars.ContextVar("maintenance_probe_fences", default=())

    @contextmanager
    def access(self, fences):
        token = self.allowed.set(tuple(fences))
        try:
            yield
        finally:
            self.allowed.reset(token)

    def roots(self, environment):
        try:
            return registered_read_roots(environment)
        except (CoordinationError, OSError, KeyError):
            # A first observation has no proven import upper bound yet.
            return [root_evidence("/", role="runtime_read")]

    def check(self, environment):
        roots = self.roots(environment)
        with self.coordinator.transaction() as c:
            if any(
                row["maintenance_id"] not in self.allowed.get()
                for row in self.coordinator._conflicts(c, roots)
            ):
                raise CoordinationError("environment_maintenance_frozen")

    @contextmanager
    def draining(self, reservation_id):
        self.coordinator.check(reservation_id, self.owner, draining=True)
        with self.coordinator.transaction() as c:
            row = self.coordinator._owned(c, reservation_id, self.owner)
            roots = json.loads(row["roots_json"])
            fences = [
                f["maintenance_id"]
                for f in self.coordinator._conflicts(c, roots)
                if f["state"] in {"fencing", "draining"}
                and row["created_seq"] < f["fence_seq"]
            ]
        with self.access(fences):
            yield

    async def probe(self, before, probe, *, wrapper):
        rid = "probe-" + uuid.uuid4().hex
        document = {
            "reservationId": rid,
            "ownerId": self.owner["ownerId"],
            "ownerEpoch": self.owner["ownerEpoch"],
            "workspaceId": self.workspace_id,
            "workspacePath": str(self.workspace),
            "kind": "probe",
            "resourceId": rid,
            "readRoots": self.roots(before),
            "identity": {
                "environmentId": before["environmentId"],
                "ownerProcess": process_evidence(os.getpid()),
                "maintenanceFences": list(self.allowed.get()),
            },
        }
        self.coordinator.reserve(document, allowed_fences=self.allowed.get())
        self.coordinator.update(rid, self.owner, committed=True)
        scope = _ProbeLaunch(self, rid, wrapper)
        try:
            return await probe(before["spec"], self.workspace, wrapper=scope)
        finally:
            self.coordinator.close(rid, self.owner, local_terminal=True)


class _ProbeLaunch:
    def __init__(self, guard, rid, inner):
        self.guard, self.rid = guard, rid
        self.wrapper = SubreaperLifecycleWrapper(
            private_directory(guard.coordinator.directory / "probe-processes"),
            inner=inner,
        )
        self.gate = self.token = self.birth = None

    def wrap(self, argv, *, cwd, env, **kwargs):
        import sys

        waiting, self.gate, self.token = gated_argv(argv, cwd, python=sys.executable)
        self.guard.coordinator.starting_probe(self.rid, self.guard.owner)
        return self.wrapper.wrap(waiting, cwd=cwd, env=env, **kwargs)

    async def after_spawn(self, process):
        self.birth = process_evidence(process.pid)
        if self.birth is None:
            raise CoordinationError("environment_maintenance_unconfirmed")
        self.guard.coordinator.update(self.rid, self.guard.owner, process=self.birth)
        proof = await wait_containment(self.birth)
        if (
            proof is None
            or self.guard.coordinator.confirm_containment(self.rid, self.guard.owner)
            != proof
        ):
            raise CoordinationError("environment_maintenance_unconfirmed")
        self.guard.coordinator.check(self.rid, self.guard.owner)
        open_gate(self.gate, self.token)

    async def before_cleanup(self, process):
        if same_process(self.birth):
            await asyncio.to_thread(
                self.wrapper.terminate,
                process.pid,
                lambda pid: same_process(self.birth),
            )
        if self.gate:
            self.gate.unlink(missing_ok=True)
