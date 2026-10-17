"""PID containment for the existing finite science worker and Job plane."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys

from omicsclaw.entry.environment_coordination import CoordinationError
from omicsclaw.entry.notebook.security import process_evidence, same_process
from omicsclaw.entry.process_containment import (
    SubreaperLifecycleWrapper,
    contained_exit,
    gated_argv,
    open_gate,
    wait_containment,
)
from omicsclaw.tools.builtin.bash import spawn_group_leader


def validate_child(reservation):
    coordinator, claim = reservation
    row = coordinator.get(claim["reservationId"])
    proof = json.loads(row["containment_json"] or "null")
    if (
        not proof
        or not same_process(proof["root"])
        or proof.get("kind") != "linux_subreaper"
        or process_evidence(os.getpid()) != proof.get("child")
    ):
        raise CoordinationError("environment_maintenance_unconfirmed")


async def supervise(request_path, reservation):
    coordinator, claim = reservation
    argv = [
        sys.executable,
        "-B",
        "-m",
        "omicsclaw.entry.ssh.science_worker",
        "--request",
        str(request_path),
        "--contained-coordinator",
        str(coordinator.directory),
    ]
    waiting, gate, token = gated_argv(argv, request_path.parent, python=sys.executable)
    # This wraps process lifetime only. It preserves the Job's existing network
    # policy and does not claim filesystem or network isolation.
    lifecycle = SubreaperLifecycleWrapper(request_path.parent / ".process-lifecycles")
    wrapped, _env = lifecycle.wrap(waiting, cwd=request_path.parent, env={})
    process = await spawn_group_leader(asyncio.create_subprocess_exec(*wrapped))
    birth = process_evidence(process.pid)
    proof = None
    try:
        if birth is None:
            raise CoordinationError("environment_maintenance_unconfirmed")
        coordinator.update(claim["reservationId"], claim, process=birth)
        proof = await wait_containment(birth)
        if (
            proof is None
            or coordinator.confirm_containment(claim["reservationId"], claim) != proof
        ):
            raise CoordinationError("environment_maintenance_unconfirmed")
        coordinator.check(claim["reservationId"], claim, draining=True)
        open_gate(gate, token)
        code = await process.wait()
        for _ in range(100):
            if contained_exit(proof, birth):
                break
            await asyncio.sleep(0.01)
        if not contained_exit(proof, birth):
            raise CoordinationError("environment_maintenance_unconfirmed")
        coordinator.finish_job(claim["reservationId"], claim, birth)
        coordinator.reap_contained()
        if code:
            raise RuntimeError("science worker failed")
    finally:
        if same_process(birth):
            await asyncio.to_thread(
                lifecycle.terminate, birth["pid"], lambda pid: same_process(birth)
            )
        if same_process(birth):
            with contextlib.suppress(ProcessLookupError):
                os.kill(birth["pid"], signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        gate.unlink(missing_ok=True)
