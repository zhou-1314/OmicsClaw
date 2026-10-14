"""The P1 jobs-plane bridge: one ``POST /jobs`` request, run remotely.

C2's routing half. A job created with ``runtime="remote:<alias>"`` is
executed by :class:`RemoteJobsBridge` instead of its kind's local
runner, and the bridge speaks the P1 event vocabulary through the
:class:`~omicsclaw.entry.desktop.jobs_manager.JobExecutionContext` it is
handed — ``job.started`` and ``job.done`` come from the manager's own
``_run`` exactly as for a local job, and the bridge adds ``progress``
frames for submission and each poll, plus ``tool_output_chunk`` frames
carrying the *new* bytes of the remote ``job.log``.

**Today the remote job is a command.** ``inputs.command`` is what runs;
``inputs.inputs`` / ``inputs.outputs`` / ``inputs.scheduler`` map onto
the tool-plane ``remote_submit`` arguments. A remote *skill* (running
skill code next to the data) is C3's persistent-kernel territory, not
this bridge's — saying so here is cheaper than discovering it in a
stack trace.

Observation timeout or controller shutdown detaches this runner while
the admitted job keeps running. JobsManager sends an explicit cancel
through the plane and waits for observed terminal evidence. The existing
stores and reconciler remain responsible for job state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .plane import RemotePlaneBinding, TargetSnapshot

POLL_INTERVAL_S = 5.0
LOG_CHUNK_CHARS = 4000
"""The poll cadence and the per-chunk log budget. Five seconds is the
Claude-Science poller's cadence, and each poll costs one multiplexed
SSH round-trip — cheap enough that a job's terminal state is noticed
within one interval, rare enough that a thousand-idle cluster is not
pinged apart."""


class RemoteJobFailed(Exception):
    """The remote job ended non-zero. The manager renders this as
    ``job.failed`` with ``phase='run'`` in its exception naming."""


class RemoteJobsBridge:
    """A jobs-plane runner over the plane; also the runtime binding.

    ``JobsManager(remote_runtime=RemoteJobsBridge(plane))`` mounts it.
    As a runner it satisfies the manager's two-method shape —
    ``validate(skill, inputs)`` before insertion, ``__call__(record,
    ctx)`` to run — so the manager needs no remote-specific branch
    beyond choosing it when ``record.runtime`` is not ``"local"``.
    """

    lifetime = "external"

    def __init__(
        self,
        plane: RemotePlaneBinding,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        log_chunk_chars: int = LOG_CHUNK_CHARS,
        target_alias: str = "",
        target_root: str = "",
    ) -> None:
        self._plane = plane
        self._poll_interval_s = float(poll_interval_s)
        self._log_chunk_chars = int(log_chunk_chars)
        self.target_alias = target_alias
        self.target_root = target_root

    @property
    def plane(self) -> RemotePlaneBinding:
        """The plane this bridge submits through — the reconciler's way in."""
        return self._plane

    async def resolve_target(
        self,
        alias: str,
        *,
        capabilities: tuple[str, ...] = ("remote_exec", "remote_submit"),
    ) -> TargetSnapshot:
        """Resolve and freeze one target before its job row is inserted."""
        return await self._plane.resolve_target(
            alias,
            capabilities=capabilities,
            **({"target_root": self.target_root} if self.target_root else {}),
        )

    # ---- the runner contract ------------------------------------------------

    def validate(self, skill: str, inputs: Any) -> None:
        """Refuse what cannot run remotely, before any row is inserted.

        Raises ``ValueError`` the jobs route answers as ``invalid_inputs``
        (the manager wraps nothing: its contract with runners is "raise
        JobError or ValueError codes", and the plain message is what the
        desktop shows).
        """
        if not isinstance(inputs, dict):
            raise ValueError("inputs_must_be_object")  # noqa: TRY004 - public ingress error contract
        command = inputs.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                "remote_runtime_needs_inputs_command: a remote job runs "
                "inputs.command (a shell command or #SBATCH script)"
            )
        scheduler = inputs.get("scheduler", "auto")
        if scheduler not in ("auto", "slurm", "none"):
            raise ValueError(f"invalid_scheduler:{scheduler}")
        upload_items = inputs.get("inputs", [])
        if not isinstance(upload_items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("src"), str)
            and isinstance(item.get("dst"), str)
            and item["src"].strip()
            and item["dst"].strip()
            for item in upload_items
        ):
            raise ValueError(
                "invalid_inputs: expected a list of {src, dst} objects — "
                "src inside the session workspace, dst inside the job's "
                "work directory"
            )
        outputs = inputs.get("outputs", [])
        if not isinstance(outputs, list) or not all(
            isinstance(item, str) for item in outputs
        ):
            raise ValueError("invalid_outputs: expected a list of file names")

    async def __call__(self, record: Any, ctx: Any) -> dict | None:
        """Submit, poll, stream the log, and land on the remote verdict.

        :raises RemoteJobFailed: the remote job finished non-zero.
        :raises asyncio.CancelledError: observation was detached; this does
            not authorize a job cancellation.
        """
        # Dispatch is driven exclusively by the immutable creation snapshot.
        # Legacy rows are populated by the one-time target migration before
        # they can be resumed.
        alias = record.target_alias
        if not alias:
            raise RemoteJobFailed("remote job has no target snapshot")
        target_workdir = str(record.target_workdir or "").strip()
        if not target_workdir:
            raise RemoteJobFailed("remote job has no immutable target workdir snapshot")
        command = str(record.inputs.get("command") or "")
        outcome = await self._plane.submit(
            alias,
            command,
            inputs=tuple(
                (str(item["src"]), str(item["dst"]))
                for item in record.inputs.get("inputs", [])
                if isinstance(item, dict)
            ),
            outputs=tuple(str(o) for o in record.inputs.get("outputs", [])),
            scheduler=str(record.inputs.get("scheduler") or "auto"),
            label=f"desktop job {record.id}",
            local_job_id=record.id,
            target_workdir=target_workdir,
        )
        return await self.observe(outcome, record, ctx, command=command)

    async def observe(self, outcome, record, ctx, *, command=""):
        """Observe an already admitted handle through the original event path."""
        ctx.tool_started(
            "remote_submit",
            command,
            human_description=(f"submit on {outcome.host} ({outcome.kind})"),
        )
        ctx.progress(
            f"submitted on {outcome.host} as {outcome.kind} job "
            f"{outcome.job_id or outcome.pgid}",
        )

        sent = 0
        while True:
            await asyncio.sleep(self._poll_interval_s)
            report = await self._plane.status(outcome.job_ref)
            executor = self._plane.executor_for(outcome.host)
            tail = await executor.tail_log(outcome.workdir, sent, self._log_chunk_chars)
            if tail:
                ctx.tool_output(tail)
                sent += len(tail)
            if report.state in ("pending", "running"):
                continue
            if report.state in {"done", "failed", "canceled"}:
                ctx.emit(
                    "execution.finished",
                    {
                        "exit_code": report.exit_code,
                        "evidence_ref": outcome.job_ref,
                    },
                )
            if report.state == "failed":
                raise RemoteJobFailed(
                    f"remote job {outcome.job_ref} failed with exit code "
                    f"{report.exit_code}"
                )
            if report.state == "canceled":
                return {"status": "canceled"}
            if report.state == "done":
                for payload in remote_output_payloads(
                    outcome.host, outcome.workdir, record.inputs.get("outputs", [])
                ):
                    ctx.emit("artifact.created", payload)
                return None
            # unknown: the host went quiet mid-poll. Say so and keep
            # the loop — the handle is durable and the reconciler
            # will settle it even if this process dies waiting.
            ctx.progress(f"host status unknown ({report.detail[:120]})")


def _alias_from_runtime(runtime: str) -> str:
    """``"remote:<alias>"`` to ``<alias>``, validated where it matters.

    :raises ValueError: the runtime string is not the remote form — the
    caller (the jobs route) validates earlier, so this is the
    belt-and-braces half.
    """
    if not runtime.startswith("remote:") or len(runtime) <= len("remote:"):
        raise ValueError(f"not a remote runtime: {runtime!r}")
    return runtime[len("remote:") :]


__all__ = ["LOG_CHUNK_CHARS", "POLL_INTERVAL_S", "RemoteJobFailed", "RemoteJobsBridge"]


def remote_output_payloads(host, workdir, names):
    """Declared remote references only; no fetch and no success claim for the file."""
    from pathlib import PurePosixPath

    for name in names:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        uri = f"remote://{host}{workdir.rstrip('/')}/{relative}"
        yield {"remote_ref": uri, "host_id": host, "logical_output": str(relative)}
