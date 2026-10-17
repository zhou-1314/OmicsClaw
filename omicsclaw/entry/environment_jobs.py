"""Persistent scientific Job reservations, from admission to verified process exit."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .environment_coordination import (
    CoordinationError,
    EnvironmentCoordinator,
    coordinator_directory,
)
from .environment_scopes import registered_read_roots
from .notebook.security import process_evidence
from .ssh.environment_probe import probe_environment


class ScienceEnvironmentGuard:
    def __init__(self, process_guard, *, interpreter, rscript=None, r_library=None):
        self.guard = process_guard
        self.interpreter = interpreter
        self.rscript = rscript
        self.r_library = r_library

    def key(self, job_id):
        return (
            "job-"
            + hashlib.sha256(
                (self.guard.workspace_id + ":" + job_id).encode()
            ).hexdigest()
        )

    async def reserve(self, record):
        spec = {
            "language": "python",
            "executable": str(self.interpreter),
            "libraryPaths": [],
            "requiredPackages": [],
        }
        from .environment_reads import EnvironmentReadGuard
        reader = EnvironmentReadGuard(self.guard.coordinator, self.guard.workspace, self.guard.workspace_id, self.guard.owner)
        observed = await reader.probe({"environmentId":self.key(record.id), "spec":spec}, probe_environment, wrapper=None)
        roots = registered_read_roots({"spec": spec, "observation": observed})
        if self.rscript:
            spec = {
                "language": "r",
                "executable": str(Path(self.rscript).with_name("R")),
                "libraryPaths": [str(self.r_library)],
                "requiredPackages": [],
            }
            observed = await reader.probe({"environmentId":self.key(record.id), "spec":spec}, probe_environment, wrapper=None)
            roots.extend(registered_read_roots({"spec": spec, "observation": observed}))
        self.guard.coordinator.reserve(
            self.guard.document(
                self.key(record.id),
                "job",
                roots,
                {
                    "jobId": record.id,
                    "science": record.inputs["_science"],
                    "targetWorkdir": record.target_workdir,
                    "skill": record.skill,
                    "inputsDigest": hashlib.sha256(
                        json.dumps(
                            {k: v for k, v in record.inputs.items() if k != "_science"},
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                },
            )
        )

    def committed(self, record):
        self.guard.coordinator.update(
            self.key(record.id), self.guard.owner, committed=True
        )

    def abandoned(self, record):
        self.guard.coordinator.close(
            self.key(record.id), self.guard.owner, local_terminal=True
        )

    def request(self, record):
        key = self.key(record.id)
        self.guard.coordinator.check(key, self.guard.owner, draining=True)
        return {
            "reservationId": key,
            **self.guard.owner,
            "workspaceId": self.guard.workspace_id,
        }

    def dispatching(self, record):
        self.guard.coordinator.dispatch_job(self.key(record.id), self.guard.owner)

    def finished(self, record):
        self.guard.coordinator.close_unsent_job(self.key(record.id), self.guard.owner)
        self.guard.coordinator.reap_finished_jobs()


def start_worker_reservation(
    request, *, register=True, directory=None, check_scopes=True
):
    """Legacy requests remain readable; current managed launch supplies this fence."""
    claim = request.get("environmentReservation")
    if claim is None:
        return None
    if not isinstance(claim, dict) or set(claim) != {
        "reservationId",
        "ownerId",
        "ownerEpoch",
        "workspaceId",
    }:
        raise CoordinationError("environment_maintenance_invalid_reservation")
    coordinator = EnvironmentCoordinator(directory or coordinator_directory())
    row = coordinator.get(claim["reservationId"])
    if (
        row is None
        or row["kind"] != "job"
        or row["workspace_id"] != claim["workspaceId"]
        or row["owner_id"] != claim["ownerId"]
        or row["owner_epoch"] != claim["ownerEpoch"]
    ):
        raise CoordinationError("environment_maintenance_unconfirmed")
    identity = json.loads(row["identity_json"])["identity"]
    if (
        identity.get("jobId") != request["jobId"]
        or identity.get("targetWorkdir") != request["workspace"]
    ):
        raise CoordinationError("environment_maintenance_reservation_conflict")
    science = identity.get("science", {})
    if (
        identity.get("skill") != request["skill"]
        or identity.get("inputsDigest")
        != hashlib.sha256(
            json.dumps(
                request["inputs"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        or science.get("payloadRoot") != request["payloadRoot"]
        or science.get("payloadDigest") != request["payloadDigest"]
        or Path(science.get("python", "")).absolute() != Path(sys.executable).absolute()
    ):
        raise CoordinationError("environment_maintenance_reservation_conflict")
    if check_scopes:
        coordinator.check(claim["reservationId"], claim, draining=True)
    if register:
        coordinator.update(
            claim["reservationId"], claim, process=process_evidence(os.getpid())
        )
    return coordinator, claim


def finish_worker_reservation(reservation):
    if reservation is not None:
        coordinator, claim = reservation
        coordinator.finish_job(
            claim["reservationId"], claim, process_evidence(os.getpid())
        )
