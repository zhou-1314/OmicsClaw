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

**Cancellation cancels remotely too.** The manager cancels the runner's
asyncio task; the bridge catches that, sends the remote cancel, and
re-raises — so "cancel" means the same thing to the user whichever side
of the SSH connection the work is on. The one asymmetry left is the
manager's local timeout: a remote job that outruns
``default_timeout_s`` is marked failed locally while it keeps running
remotely, and its handle row stays in-flight for the reconciler to
report the truth later. That bound is the price of reusing P1's
lifecycle whole rather than forking a second one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .plane import RemotePlaneBinding

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

    def __init__(
        self,
        plane: RemotePlaneBinding,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        log_chunk_chars: int = LOG_CHUNK_CHARS,
    ) -> None:
        self._plane = plane
        self._poll_interval_s = float(poll_interval_s)
        self._log_chunk_chars = int(log_chunk_chars)

    @property
    def plane(self) -> RemotePlaneBinding:
        """The plane this bridge submits through — the reconciler's way in."""
        return self._plane

    # ---- the runner contract ------------------------------------------------

    def validate(self, skill: str, inputs: Any) -> None:
        """Refuse what cannot run remotely, before any row is inserted.

        Raises ``ValueError`` the jobs route answers as ``invalid_inputs``
        (the manager wraps nothing: its contract with runners is "raise
        JobError or ValueError codes", and the plain message is what the
        desktop shows).
        """
        if not isinstance(inputs, dict):
            raise ValueError("inputs_must_be_object")
        command = inputs.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                "remote_runtime_needs_inputs_command: a remote job runs "
                "inputs.command (a shell command or #SBATCH script)"
            )
        scheduler = inputs.get("scheduler", "auto")
        if scheduler not in ("auto", "slurm", "none"):
            raise ValueError(f"invalid_scheduler:{scheduler}")
        for key in ("inputs", "outputs"):
            items = inputs.get(key, [])
            if not isinstance(items, list) or not all(
                isinstance(item, str) for item in items
            ):
                raise ValueError(f"invalid_{key}: expected a list of file names")

    async def __call__(self, record: Any, ctx: Any) -> None:
        """Submit, poll, stream the log, and land on the remote verdict.

        :raises RemoteJobFailed: the remote job finished non-zero.
        :raises asyncio.CancelledError: the manager cancelled the job;
            the remote cancel has been sent first.
        """
        alias = _alias_from_runtime(record.runtime)
        command = str(record.inputs.get("command") or "")
        outcome = await self._plane.submit(
            alias,
            command,
            inputs=tuple(str(i) for i in record.inputs.get("inputs", [])),
            outputs=tuple(str(o) for o in record.inputs.get("outputs", [])),
            scheduler=str(record.inputs.get("scheduler") or "auto"),
            label=f"desktop job {record.id}",
            local_job_id=record.id,
        )
        ctx.tool_started(
            "remote_submit", command, human_description=(
                f"submit on {outcome.host} ({outcome.kind})"
            ),
        )
        ctx.progress(
            f"submitted on {outcome.host} as {outcome.kind} job "
            f"{outcome.job_id or outcome.pgid}",
        )
        from .jobs import LOG_FILE  # local import keeps the module head light

        sent = 0
        try:
            while True:
                await asyncio.sleep(self._poll_interval_s)
                report = await self._plane.status(outcome.job_ref)
                executor = self._plane.executor_for(outcome.host)
                tail = await executor.tail_log(
                    outcome.workdir, sent, self._log_chunk_chars
                )
                if tail:
                    ctx.tool_output(tail)
                    sent += len(tail)
                if report.state in ("pending", "running"):
                    continue
                if report.state == "failed":
                    raise RemoteJobFailed(
                        f"remote job {outcome.job_ref} failed with exit code "
                        f"{report.exit_code}"
                    )
                if report.state == "canceled":
                    return
                if report.state == "done":
                    return
                # unknown: the host went quiet mid-poll. Say so and keep
                # the loop — the handle is durable and the reconciler
                # will settle it even if this process dies waiting.
                ctx.progress(f"host status unknown ({report.detail[:120]})")
        except asyncio.CancelledError:
            await self._plane.cancel(outcome.job_ref)
            raise


def _alias_from_runtime(runtime: str) -> str:
    """``"remote:<alias>"`` to ``<alias>``, validated where it matters.

    :raises ValueError: the runtime string is not the remote form — the
    caller (the jobs route) validates earlier, so this is the
    belt-and-braces half.
    """
    if not runtime.startswith("remote:") or len(runtime) <= len("remote:"):
        raise ValueError(f"not a remote runtime: {runtime!r}")
    return runtime[len("remote:"):]


__all__ = ["LOG_CHUNK_CHARS", "POLL_INTERVAL_S", "RemoteJobFailed", "RemoteJobsBridge"]
