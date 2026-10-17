"""Cross-workspace reservations before local Notebook commits and process starts."""

from __future__ import annotations

import hashlib
import json
import os

from omicsclaw.entry.environment_scopes import control_read_roots, registered_read_roots

from .security import process_evidence, same_process
from .service import TERMINAL


def reservation_id(workspace_id, kind, resource):
    return hashlib.sha256(
        json.dumps([workspace_id, kind, resource]).encode()
    ).hexdigest()


class OwnerEnvironmentGuard:
    def __init__(self, runtime, coordinator):
        self.runtime = runtime
        self.coordinator = coordinator
        self.owner = {
            "ownerId": runtime.owner_id,
            "ownerEpoch": runtime.epoch,
            "process": process_evidence(os.getpid()),
        }
        self.workspace_id = runtime.identity["workspaceId"]

    def key(self, kind, resource):
        return reservation_id(self.workspace_id, kind, resource)

    def reserve(self, kind, resource, environment, identity):
        key = self.key(kind, resource)
        self.coordinator.reserve(
            {
                "reservationId": key,
                "ownerId": self.owner["ownerId"],
                "ownerEpoch": self.owner["ownerEpoch"],
                "workspaceId": self.workspace_id,
                "workspacePath": str(self.runtime.workspace),
                "kind": kind,
                "resourceId": resource,
                "readRoots": registered_read_roots(environment),
                "identity": {
                    **identity,
                    "environmentId": environment["environmentId"],
                    "environmentRevision": environment["revision"],
                    "ownerProcess": self.owner["process"],
                    "componentSourceDigest": self.runtime.source_digest,
                },
            }
        )
        return key

    def kernel(self, instance, nid, generation, environment):
        key = self.key("kernel", instance)
        if self.coordinator.get(key) is None:
            return self.reserve(
                "kernel",
                instance,
                environment,
                {
                    "notebookId": nid,
                    "generation": generation,
                    "language": environment["spec"]["language"],
                    "containment": "unverified",
                },
            )
        self.coordinator.check(key, self.owner)
        return key

    def admission(self, document, eid, environment):
        self.kernel(
            document["kernelInstanceId"],
            document["notebookId"],
            document["generation"],
            environment,
        )
        return self.reserve(
            "admission",
            eid,
            environment,
            {
                "notebookId": document["notebookId"],
                "kernelInstanceId": document["kernelInstanceId"],
                "generation": document["generation"],
                "language": document["language"],
            },
        )

    def commit_kernel(self, instance):
        self.coordinator.update(
            self.key("kernel", instance), self.owner, committed=True
        )

    def committed(self, eid):
        row = self.runtime.db.run(
            lambda c: c.execute(
                "SELECT instance_id FROM notebook_runtime_executions WHERE execution_id=?",
                (eid,),
            ).fetchone()
        )
        self.commit_kernel(row[0])
        self.coordinator.update(self.key("admission", eid), self.owner, committed=True)

    def dispatch(self, eid, instance):
        self.coordinator.check(self.key("admission", eid), self.owner, draining=True)
        self.coordinator.check(self.key("kernel", instance), self.owner, draining=True)
        # Code can mutate state before throwing or being interrupted.
        self.coordinator.update(
            self.key("kernel", instance), self.owner, has_analysis=True
        )

    def start(self, instance):
        self.coordinator.starting_kernel(self.key("kernel", instance), self.owner)

    def process(self, instance, evidence):
        self.coordinator.update(
            self.key("kernel", instance), self.owner, process=evidence
        )

    def confirm_started(self, instance):
        return self.coordinator.confirm_containment(
            self.key("kernel", instance), self.owner
        )

    def finished(self, eid):
        row = self.runtime.db.run(
            lambda c: c.execute(
                "SELECT status FROM notebook_executions WHERE execution_id=?", (eid,)
            ).fetchone()
        )
        if row and row[0] in TERMINAL and row[0] != "outcome_unknown":
            self.coordinator.close(
                self.key("admission", eid), self.owner, local_terminal=True
            )

    def release_evidence(self, instance, evidence):
        return self.coordinator.recover_kernel_containment(
            self.key("kernel", instance), self.owner, evidence
        )

    def released(self, instance):
        row = self.coordinator.get(self.key("kernel", instance))
        if row is None or row["state"] == "closed":
            return
        if not self.coordinator.close(
            self.key("kernel", instance), self.owner, local_terminal=True
        ):
            return
        for row in self.coordinator.workspace_reservations(self.workspace_id):
            identity = json.loads(row["identity_json"])["identity"]
            if (
                row["kind"] == "admission"
                and identity.get("kernelInstanceId") == instance
            ):
                self.coordinator.close(
                    row["reservation_id"], self.owner, local_terminal=True
                )

    def rollback_uncommitted(self, kind, resource):
        table, column = (
            ("notebook_runtime_kernels", "instance_id")
            if kind == "kernel"
            else ("notebook_runtime_executions", "execution_id")
        )
        row = self.runtime.db.run(
            lambda c: c.execute(
                f"SELECT 1 FROM {table} WHERE {column}=?", (resource,)
            ).fetchone()
        )
        if row is None:
            self.coordinator.close(
                self.key(kind, resource), self.owner, local_terminal=True
            )

    def recover(self):
        self.coordinator.reap_dead_controls()
        for row in self.coordinator.workspace_reservations(self.workspace_id):
            if row["kind"] not in {"kernel", "admission"}:
                continue
            original = json.loads(row["identity_json"])["identity"]
            self.coordinator.adopt(
                row["reservation_id"],
                self.owner,
                previous_owner_process=original.get("ownerProcess"),
            )
            resource = row["resource_id"]
            if row["kind"] == "admission":
                local = self.runtime.db.run(
                    lambda c, resource=resource: c.execute(
                        "SELECT status FROM notebook_executions WHERE execution_id=?",
                        (resource,),
                    ).fetchone()
                )
                if local is None or (
                    local[0] in TERMINAL and local[0] != "outcome_unknown"
                ):
                    self.coordinator.close(
                        row["reservation_id"], self.owner, local_terminal=True
                    )
                else:
                    self.coordinator.update(
                        row["reservation_id"], self.owner, committed=True
                    )
            else:
                local = self.runtime.db.run(
                    lambda c, resource=resource: c.execute(
                        "SELECT state,process_json FROM notebook_runtime_kernels WHERE instance_id=?",
                        (resource,),
                    ).fetchone()
                )
                if local is None or (
                    local[0] in {"released", "lost"}
                    and not same_process(json.loads(local[1] or "null"))
                ):
                    if not self.coordinator.get(row["reservation_id"])[
                        "uncertain_exit"
                    ]:
                        self.coordinator.close(
                            row["reservation_id"], self.owner, local_terminal=True
                        )
                else:
                    self.coordinator.update(
                        row["reservation_id"], self.owner, committed=True
                    )
        self.register_control()

    def register_control(self):
        process = self.owner["process"]
        key = reservation_id(
            "control",
            "control",
            json.dumps([process, self.owner["ownerId"]], sort_keys=True),
        )
        self.coordinator.reserve(
            {
                "reservationId": key,
                "ownerId": self.owner["ownerId"],
                "ownerEpoch": self.owner["ownerEpoch"],
                "workspaceId": self.workspace_id,
                "workspacePath": str(self.runtime.workspace),
                "kind": "control",
                "resourceId": key,
                "readRoots": control_read_roots(),
                "identity": {"ownerProcess": process, "role": "notebook_owner"},
            }
        )
        self.coordinator.update(key, self.owner, process=process, committed=True)
