"""The plane: one object that does what the remote tool family asks for.

The composition half of C1. :class:`RemotePlaneBinding` implements the
:class:`~omicsclaw.tools.builtin.remote.RemotePlane` Protocol
structurally — it imports nothing from the tool layer except the
result shapes, which is the same direction ``bash.py``'s environment
seam faces — and is what :func:`~omicsclaw.entry.assembly.build_app`
binds when :attr:`~omicsclaw.entry.config.AppConfig.remote_execution`
is on.

**One host, one executor, one connection policy.** All traffic for a
host goes through :class:`RemoteExecutor`, which builds every command
with the pure functions of :mod:`omicsclaw.remote.ssh` and
:mod:`omicsclaw.remote.jobs` and runs them through the injected
:class:`~omicsclaw.remote.ssh.SshSpawner`. ControlMaster multiplexing
(the spawner's fixed options) makes the second command on a host cheap;
the plane adds no connection state of its own on top of that.

**The scratch is probed once and remembered.** Submission needs a
directory to work in; the plane reads the host knowledge base for the
last probe and probes fresh when there is none, then creates
``<scratch>/omicsclaw/<stamp>-<rand>`` per job. A probe that cannot
parse still yields a scratch (the home directory), because a host with
a broken ``free`` is more often a working cluster than a paperweight.

**Declared outputs travel with the job, not in a schema column.**
``remote_submit`` writes the names it was given into
``_omicsclaw_outputs`` inside the work directory, so what a fetch by
``job_ref`` should bring back is a fact the remote side holds and any
later session can read — no column to migrate, and a person poking at
the workdir sees the same list the agent sees. With nothing declared,
fetch brings back ``job.log``, which is the one file every job has.

**Fetch refuses rather than transports, past the threshold.** Sizes are
checked over SSH before any byte moves; a file over ``max_mb`` is
answered with a ``remote://<host>/<path>`` reference and nothing
travels. The reference is the plan's "data stays put, the handle
travels" rule made concrete.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from omicsclaw.tools.builtin.remote import (
    DEFAULT_FETCH_MAX_MB,
    DEFAULT_SUBMIT_TIMEOUT_S,
    ExecOutcome,
    FetchOutcome,
    HostCard,
    StatusReport,
    SubmitOutcome,
    valid_host,
)

from .jobs import (
    KIND_NOHUP,
    KIND_SLURM,
    LOG_FILE,
    PENDING,
    RemoteJobHandle,
    build_cancel_command,
    build_job_scripts,
    build_log_tail_command,
    build_mkdir_command,
    build_size_command,
    build_status_command,
    build_submit_command,
    detect_scheduler,
    parse_status,
    parse_submit_output,
)
from .probe import PROBE_SCRIPT, HostProbe, _from_decoded, parse_probe_output
from .ssh import (
    RemoteHostRefused,
    RemoteHostUnreachable,
    SshSpawner,
    SystemSshSpawner,
    build_sftp_argv,
    build_sftp_batch,
    build_ssh_argv,
    encode_login_script,
    validate_host,
)
from .store import RemoteHostStore, RemoteJobStore

PROBE_TIMEOUT_S = 60.0
UPLOAD_TIMEOUT_S = 300.0
FETCH_TIMEOUT_S = 600.0
"""Budgets the plane imposes on itself, distinct from the tool-level
command budget: a probe that cannot finish in a minute is a host that
will not answer a job either, and transfers get the room the 100 MB
threshold implies."""

OUTPUTS_FILE = "_omicsclaw_outputs"
"""Where a submit's declared output names live, inside the workdir."""


class RemoteExecutor:
    """Every SSH conversation with one host.

    Stateless beyond the host name and the spawner; safe to build fresh
    per call, which the plane does, because the connection state lives
    in OpenSSH's ControlMaster sockets, not here.
    """

    def __init__(self, host: str, spawner: SshSpawner) -> None:
        self.host = validate_host(host)
        self._spawner = spawner

    async def run(self, command: str, *, timeout: float) -> ExecOutcome:
        return await self._spawner.spawn(
            build_ssh_argv(self.host, command), timeout=timeout
        )

    async def probe(self) -> HostProbe:
        """Run the probe script in a login shell and parse what came back."""
        outcome = await self.run(
            encode_login_script(PROBE_SCRIPT), timeout=PROBE_TIMEOUT_S
        )
        probe = parse_probe_output(outcome.output)
        if probe.parse_error and outcome.exit_code != 0:
            probe.parse_error = f"ssh exit {outcome.exit_code}: {probe.parse_error}"
        return probe

    async def submit_in(
        self,
        workdir: str,
        command: str,
        *,
        scheduler: str = "auto",
        files: dict[str, str] | None = None,
        timeout: float = DEFAULT_SUBMIT_TIMEOUT_S,
    ) -> RemoteJobHandle:
        """Create *workdir*, upload *files*, start the job, read the number."""
        kind = detect_scheduler(command, scheduler)
        scripts = build_job_scripts(command, kind)
        await self.run(build_mkdir_command(workdir), timeout=UPLOAD_TIMEOUT_S)
        await self._upload(workdir, {**scripts, **(files or {})})
        outcome = await self.run(
            build_submit_command(workdir, kind), timeout=timeout
        )
        number = parse_submit_output(kind, outcome.output)
        if number <= 0:
            raise RuntimeError(
                f"submission on {self.host} produced no usable "
                f"{'job id' if kind == KIND_SLURM else 'pgid'}: {outcome.output!r}"
            )
        return RemoteJobHandle(
            kind=kind,
            host=self.host,
            workdir=workdir,
            job_id=str(number) if kind == KIND_SLURM else None,
            pgid=number if kind == KIND_NOHUP else None,
            submitted_at=time.time(),
        )

    async def status(self, handle: RemoteJobHandle) -> StatusReport:
        """Ask the host; an unreachable host answers ``unknown``, not an error."""
        try:
            outcome = await self.run(
                build_status_command(handle), timeout=PROBE_TIMEOUT_S
            )
        except RemoteHostUnreachable as exc:
            return StatusReport(state="unknown", detail=str(exc))
        state, exit_code = parse_status(handle.kind, outcome.output)
        detail = outcome.output.strip()[:200]
        return StatusReport(state=state, exit_code=exit_code, detail=detail)

    async def cancel(self, handle: RemoteJobHandle) -> StatusReport:
        """Stop the job; report what the host said, not what we hoped."""
        try:
            outcome = await self.run(
                build_cancel_command(handle), timeout=PROBE_TIMEOUT_S
            )
        except RemoteHostUnreachable as exc:
            return StatusReport(state="unknown", detail=str(exc))
        return StatusReport(
            state="canceled" if outcome.exit_code == 0 else "unknown",
            detail=outcome.output.strip()[:200],
        )

    async def sizes(self, workdir: str, names: list[str]) -> list[int]:
        """Byte sizes of *names* under *workdir*; ``-1`` where unknown."""
        paths = [remote_join(workdir, name) for name in names]
        outcome = await self.run(
            build_size_command(paths), timeout=PROBE_TIMEOUT_S
        )
        sizes: list[int] = []
        for line in outcome.output.strip().splitlines():
            token = line.strip()
            sizes.append(int(token) if token.lstrip("-").isdigit() else -1)
        while len(sizes) < len(paths):
            sizes.append(-1)
        return sizes[: len(paths)]

    async def read_relative(self, workdir: str, name: str) -> str:
        """One small file under *workdir*, as text (empty when absent)."""
        from .ssh import shell_quote

        outcome = await self.run(
            f"cd -- {shell_quote(workdir)} && cat ./{name} 2>/dev/null || true",
            timeout=PROBE_TIMEOUT_S,
        )
        return outcome.output

    async def tail_log(self, workdir: str, offset: int, limit: int) -> str:
        outcome = await self.run(
            build_log_tail_command(workdir, offset, limit),
            timeout=PROBE_TIMEOUT_S,
        )
        return outcome.output

    async def _upload(self, workdir: str, files: dict[str, str]) -> None:
        """Write *files* locally, then push them in one SFTP batch."""
        pairs: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory(
            prefix=f"omicsclaw-upload-{secrets.token_hex(4)}-"
        ) as staging:
            for name, content in files.items():
                local = Path(staging) / name
                local.write_text(content, encoding="utf-8")
                pairs.append((str(local), remote_join(workdir, name)))
            await self._spawner.spawn(
                build_sftp_argv(self.host),
                timeout=UPLOAD_TIMEOUT_S,
                stdin_text=build_sftp_batch(puts=pairs),
            )

    async def download(
        self, pairs: list[tuple[str, str]], *, timeout: float = FETCH_TIMEOUT_S
    ) -> None:
        """Pull *pairs* of ``(remote, local)`` paths in one SFTP batch."""
        await self._spawner.spawn(
            build_sftp_argv(self.host),
            timeout=timeout,
            stdin_text=build_sftp_batch(gets=pairs),
        )


def remote_join(workdir: str, name: str) -> str:
    """Join without pathlib, which would inject platform separators."""
    return workdir.rstrip("/") + "/" + name.lstrip("/")


class RemotePlaneBinding:
    """The :class:`~omicsclaw.tools.builtin.remote.RemotePlane` implementation.

    Holds the two stores (one :class:`~omicsclaw.memory.database.Database`
    behind both) and the spawner; builds a :class:`RemoteExecutor` per
    host per call.
    """

    def __init__(
        self,
        database: Any,
        *,
        spawner: SshSpawner | None = None,
    ) -> None:
        self._database = database
        self._hosts = RemoteHostStore(database)
        self._jobs = RemoteJobStore(database)
        self._spawner = spawner if spawner is not None else SystemSshSpawner()

    def close(self) -> None:
        """Close the database this binding was constructed over.

        The composition root hands the plane a freshly opened database
        and hands the plane to the app, so the plane is the thing with
        the lifetime — the same arrangement :class:`AgentApp` has with
        the memory database. Idempotent, and it closes *stores*, not
        sockets: SSH connections belong to OpenSSH's ControlMaster and
        expire on their own.
        """
        close = getattr(self._database, "close", None)
        if callable(close):
            close()

    # ---- the tool surface ------------------------------------------------

    async def exec(self, host: str, command: str, timeout: float) -> ExecOutcome:
        validate_host(host)
        return await RemoteExecutor(host, self._spawner).run(
            command, timeout=timeout
        )

    async def submit(
        self,
        host: str,
        command: str,
        *,
        inputs: tuple[str, ...] = (),
        outputs: tuple[str, ...] = (),
        scheduler: str = "auto",
        timeout: float = DEFAULT_SUBMIT_TIMEOUT_S,
        label: str = "",
        local_job_id: str = "",
    ) -> SubmitOutcome:
        validate_host(host)
        executor = RemoteExecutor(host, self._spawner)
        probe = await self._probe_into_store(executor, host)
        workdir = remote_join(
            probe.scratch_root(),
            "omicsclaw/" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3),
        )
        files: dict[str, str] = {}
        if outputs:
            files[OUTPUTS_FILE] = "\n".join(outputs) + "\n"
        handle = await executor.submit_in(
            workdir, command, scheduler=scheduler, files=files, timeout=timeout
        )
        handle = RemoteJobHandle(
            kind=handle.kind,
            host=handle.host,
            workdir=handle.workdir,
            job_id=handle.job_id,
            pgid=handle.pgid,
            submitted_at=handle.submitted_at,
            local_job_id=local_job_id,
        )
        row_id = self._jobs.record(handle, status=PENDING)
        return SubmitOutcome(
            job_ref=str(row_id),
            kind=handle.kind,
            job_id=handle.job_id,
            pgid=handle.pgid,
            workdir=handle.workdir,
            host=handle.host,
            submitted_at=handle.submitted_at,
        )

    async def status(self, job_ref: str) -> StatusReport:
        row = self._jobs.get(job_ref)
        if row is None:
            return StatusReport(
                state="unknown", detail="no such job_ref", job_ref=job_ref
            )
        report = await RemoteExecutor(row.host_alias, self._spawner).status(
            row.handle()
        )
        self._jobs.update_status(row.id, report.state)
        return StatusReport(
            state=report.state,
            exit_code=report.exit_code,
            detail=report.detail,
            job_ref=job_ref,
        )

    async def cancel(self, job_ref: str) -> StatusReport:
        row = self._jobs.get(job_ref)
        if row is None:
            return StatusReport(
                state="unknown", detail="no such job_ref", job_ref=job_ref
            )
        report = await RemoteExecutor(row.host_alias, self._spawner).cancel(
            row.handle()
        )
        self._jobs.update_status(row.id, report.state)
        return StatusReport(
            state=report.state, detail=report.detail, job_ref=job_ref
        )

    async def fetch(
        self, source: str, dest: str, max_mb: float = DEFAULT_FETCH_MAX_MB
    ) -> FetchOutcome:
        host, workdir, names = await self._resolve_source(source)
        if not host:
            return FetchOutcome(
                downloaded=(),
                remote_ref=None,
                skipped_reason=(
                    "source was neither a known job_ref nor a "
                    "remote://<host>/<path> URL"
                ),
            )
        if not names:
            return FetchOutcome(
                downloaded=(),
                remote_ref=None,
                skipped_reason="no outputs found for that reference",
            )
        executor = RemoteExecutor(host, self._spawner)
        sizes = await executor.sizes(workdir, names)
        limit_bytes = int(max_mb * 1024 * 1024)
        total = sum(max(0, size) for size in sizes)
        if any(size > limit_bytes for size in sizes):
            biggest = names[sizes.index(max(sizes))]
            reference = f"remote://{host}/{remote_join(workdir, biggest).lstrip('/')}"
            return FetchOutcome(
                downloaded=(),
                remote_ref=reference,
                skipped_reason=(
                    f"{max(sizes)} bytes exceeds the {max_mb:g} MB threshold"
                ),
                bytes_total=total,
            )
        local_root = Path(dest)
        local_root.mkdir(parents=True, exist_ok=True)
        pairs = [
            (remote_join(workdir, name), str(local_root / Path(name).name))
            for name in names
        ]
        await executor.download(pairs)
        return FetchOutcome(
            downloaded=tuple(str(local_root / Path(name).name) for name in names),
            remote_ref=None,
            skipped_reason="",
            bytes_total=total,
        )

    def host_card(self, alias: str) -> HostCard | None:
        """The stored card only; probing is SSH work, so it is
        :meth:`probe_card`'s async path — a synchronous method must never
        open a socket on an event loop's behalf."""
        record = self._hosts.get(alias)
        if record is None:
            return None
        return self._card_from_record(record)

    async def probe_card(self, alias: str) -> HostCard:
        """Probe (or re-probe) *alias* and return the fresh card."""
        validate_host(alias)
        executor = RemoteExecutor(alias, self._spawner)
        probe = await executor.probe()
        self._hosts.save_probe(alias, json.dumps(probe.as_json(), ensure_ascii=False))
        record = self._hosts.get(alias)
        assert record is not None
        return self._card_from_record(record)

    def note_answer(self, alias: str, question: str, answer: str) -> None:
        self._hosts.add_note(alias, question, answer)

    def job_store(self) -> RemoteJobStore:
        """The handle store — the reconciler's and the bridge's entry point."""
        return self._jobs

    def executor_for(self, host_alias: str) -> RemoteExecutor:
        """An executor for a stored alias; the reconciler's transport."""
        return RemoteExecutor(host_alias, self._spawner)

    # ---- internals ---------------------------------------------------------

    async def _probe_into_store(
        self, executor: RemoteExecutor, host: str
    ) -> HostProbe:
        record = self._hosts.get(host)
        if record is not None:
            probe = _probe_from_json(record.probed_json)
            if not probe.parse_error:
                return probe
        probe = await executor.probe()
        self._hosts.save_probe(host, json.dumps(probe.as_json(), ensure_ascii=False))
        return probe

    def _card_from_record(self, record: Any) -> HostCard:
        probe = _probe_from_json(record.probed_json)
        return HostCard(
            alias=record.alias,
            hostname=probe.hostname,
            platform=probe.platform,
            nproc=probe.nproc,
            memory_mb=probe.mem_mb,
            gpus=tuple(probe.gpus),
            commands=dict(probe.commands),
            slurm_partitions=tuple(probe.slurm_partitions),
            scratch_root=probe.scratch_root(),
            last_probed_at=record.last_probed_at,
            notes=tuple(record.notes()),
            parse_error=probe.parse_error,
        )

    async def _resolve_source(self, source: str) -> tuple[str, str, list[str]]:
        """A job_ref or a ``remote://`` URL, to ``(host, workdir, names)``.

        The declared-outputs convention (this module's docstring) answers
        the job_ref form: names come from ``_omicsclaw_outputs`` in the
        work directory, or fall back to ``job.log``, the file every job
        has. Nothing is resolved for a source that is neither form.

        :raises RemoteHostRefused: a ``remote://`` URL names a host that
            fails validation.
        """
        text = source.strip()
        if text.startswith("remote://"):
            rest = text[len("remote://"):]
            host, _, path = rest.partition("/")
            if not valid_host(host) or not path:
                raise RemoteHostRefused(
                    f"source {source!r} is not a remote://<host>/<path> URL"
                )
            full = "/" + path
            return host, full.rsplit("/", 1)[0] or "/", [full]
        if text.isdigit():
            row = self._jobs.get(text)
            if row is not None:
                handle = row.handle()
                executor = self.executor_for(row.host_alias)
                listed = await executor.read_relative(
                    handle.workdir, OUTPUTS_FILE
                )
                names = [
                    line.strip()
                    for line in listed.splitlines()
                    if line.strip()
                ] or [LOG_FILE]
                return row.host_alias, handle.workdir, names
        return "", "", []


def _probe_from_json(text: str) -> HostProbe:
    """Rebuild a probe from stored JSON without re-parsing sentinels."""
    try:
        decoded = json.loads(text)
    except ValueError:
        return HostProbe(parse_error="stored probe JSON did not parse")
    if not isinstance(decoded, dict):
        return HostProbe(parse_error="stored probe JSON was not an object")
    return _from_decoded(decoded)


__all__ = [
    "FETCH_TIMEOUT_S",
    "OUTPUTS_FILE",
    "PROBE_TIMEOUT_S",
    "RemoteExecutor",
    "RemotePlaneBinding",
    "UPLOAD_TIMEOUT_S",
    "remote_join",
]
