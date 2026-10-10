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
import posixpath
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
    RemotePathRefused,
    RemoteTransferError,
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

MAX_INPUT_FILE_BYTES = 1024 * 1024 * 1024
"""One input file's ceiling (1 GiB). A submission is a job's inputs, not
a dataset transfer; anything this size wants a path that was staged on
the host (or remote:// references) rather than an SFTP put."""

MAX_INPUT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
"""Ceiling on all of one submission's inputs together (2 GiB). Bounds
the batch even when every file individually fits."""


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
        uploads: list[tuple[str, str]] | None = None,
        timeout: float = DEFAULT_SUBMIT_TIMEOUT_S,
    ) -> RemoteJobHandle:
        """Create *workdir*, upload *files* and *uploads*, start the job.

        *files* are contents staged locally and pushed by name;
        *uploads* are ``(local path, remote path)`` pairs of files that
        already exist — the caller's input files — verified to land
        inside *workdir* before anything moves.
        """
        kind = detect_scheduler(command, scheduler)
        scripts = build_job_scripts(command, kind)
        for local, remote in uploads or ():
            if not _inside_remote_dir(workdir, remote):
                raise RemotePathRefused(
                    f"an input's dst {remote!r} escapes the job's work "
                    f"directory {workdir!r}; dst is relative to that "
                    "directory and may not climb out of it"
                )
        await self.run(build_mkdir_command(workdir), timeout=UPLOAD_TIMEOUT_S)
        await self._upload(workdir, {**scripts, **(files or {})}, uploads)
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

    async def _upload(
        self,
        workdir: str,
        files: dict[str, str],
        uploads: list[tuple[str, str]] | None = None,
    ) -> None:
        """Write *files* locally, then push them — and *uploads* — in one
        SFTP batch. A batch that does not exit zero is a failed upload,
        not a silent one."""
        pairs: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory(
            prefix=f"omicsclaw-upload-{secrets.token_hex(4)}-"
        ) as staging:
            for name, content in files.items():
                local = Path(staging) / name
                local.write_text(content, encoding="utf-8")
                pairs.append((str(local), remote_join(workdir, name)))
            pairs.extend(uploads or ())
            await self._sftp(build_sftp_batch(puts=pairs), UPLOAD_TIMEOUT_S)

    async def download(
        self, pairs: list[tuple[str, str]], *, timeout: float = FETCH_TIMEOUT_S
    ) -> None:
        """Pull *pairs* of ``(remote, local)`` paths in one SFTP batch.

        :raises RemoteTransferError: the batch exited non-zero — the
            files the caller was about to report as downloaded were not.
        """
        await self._sftp(build_sftp_batch(gets=pairs), timeout)

    async def _sftp(self, batch: str, timeout: float) -> None:
        """One SFTP session over stdin; non-zero exits are errors.

        The transport's merged output (stderr included) travels in the
        exception, so the model reads the reason the transfer gave —
        "no such file", "permission denied" — rather than a generic
        complaint it cannot act on.
        """
        outcome = await self._spawner.spawn(
            build_sftp_argv(self.host), timeout=timeout, stdin_text=batch
        )
        if outcome.exit_code != 0:
            tail = outcome.output.strip()[-400:]
            raise RemoteTransferError(
                f"sftp on {self.host} exited {outcome.exit_code}: {tail}"
            )


def remote_join(workdir: str, name: str) -> str:
    """Join without pathlib, which would inject platform separators."""
    return workdir.rstrip("/") + "/" + name.lstrip("/")


def _inside_remote_dir(workdir: str, candidate: str) -> bool:
    """Whether *candidate* — an already-joined remote path — stays
    inside *workdir*.

    The remote side is POSIX no matter what this machine is, so the
    test is spelled in :mod:`posixpath`: normalize both, and the
    candidate must be the directory itself or live under its prefix.
    :func:`posixpath.normpath` collapses ``a/../b`` before the
    comparison, so a ``dst`` that climbs out and comes back reads as
    itself, and one that climbs out and stays out fails the prefix —
    both doors are the same assertion.
    """
    root = posixpath.normpath(workdir).rstrip("/")
    normalized = posixpath.normpath(candidate)
    return normalized == root or normalized.startswith(root + "/")


class RemotePlaneBinding:
    """The :class:`~omicsclaw.tools.builtin.remote.RemotePlane` implementation.

    Holds the two stores (one :class:`~omicsclaw.memory.database.Database`
    behind both), the spawner and — since uploads and downloads cross
    the workspace boundary — the workspace root both are anchored to;
    builds a :class:`RemoteExecutor` per host per call.

    ``workspace=None`` is the refused state, not the permissive one: a
    plane that cannot check a ``src`` against a root, or resolve a
    ``dest`` inside one, declines uploads and fetches rather than
    guessing where files should go. The composition root always binds
    one; a bare binding in a test binds one or tests the refusal.
    """

    def __init__(
        self,
        database: Any,
        *,
        spawner: SshSpawner | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        self._database = database
        self._hosts = RemoteHostStore(database)
        self._jobs = RemoteJobStore(database)
        self._spawner = spawner if spawner is not None else SystemSshSpawner()
        self._workspace = Path(workspace).resolve() if workspace is not None else None

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
        inputs: tuple[tuple[str, str], ...] = (),
        outputs: tuple[str, ...] = (),
        scheduler: str = "auto",
        timeout: float = DEFAULT_SUBMIT_TIMEOUT_S,
        label: str = "",
        local_job_id: str = "",
    ) -> SubmitOutcome:
        validate_host(host)
        resolved_inputs = self._resolve_inputs(inputs)
        executor = RemoteExecutor(host, self._spawner)
        probe = await self._probe_into_store(executor, host)
        workdir = remote_join(
            probe.scratch_root(),
            "omicsclaw/" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3),
        )
        uploads = [
            (local, remote_join(workdir, dst)) for local, dst in resolved_inputs
        ]
        files: dict[str, str] = {}
        if outputs:
            files[OUTPUTS_FILE] = "\n".join(outputs) + "\n"
        handle = await executor.submit_in(
            workdir,
            command,
            scheduler=scheduler,
            files=files,
            uploads=uploads,
            timeout=timeout,
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
        local_root = self._resolve_dest(dest)
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
        missing = [
            name for name, size in zip(names, sizes) if size is None or size < 0
        ]
        if missing:
            # A size the host could not answer means the file is not
            # there (or not stat-able); downloading anyway would either
            # fail the batch or — worse — succeed for the others and
            # let the caller report a fetch that did not happen.
            raise RemoteTransferError(
                "no size was answered for "
                f"{', '.join(missing)} under {workdir} on {host} — the "
                "file does not exist there or cannot be read"
            )
        limit_bytes = int(max_mb * 1024 * 1024)
        total = sum(sizes)
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

    def _resolve_inputs(
        self, inputs: tuple[tuple[str, str], ...]
    ) -> list[tuple[str, str]]:
        """``(src, dst)`` declarations to ``(local path, dst name)`` pairs.

        The local half is anchored to the workspace the way every file
        tool's is: a relative ``src`` resolves against the root, an
        absolute one must already be inside it, and anything that walks
        out — or names something that is not a file — is refused here,
        before a single remote command runs. The remote half keeps its
        bare name; whether the name stays inside the work directory is
        checked in :meth:`RemoteExecutor.submit_in`, where the workdir
        exists to check against.
        """
        if not inputs:
            return []
        if self._workspace is None:
            raise RemotePathRefused(
                "this plane has no workspace bound, so input uploads "
                "cannot be anchored to one; refusing rather than guessing "
                "which files may travel"
            )
        pairs: list[tuple[str, str]] = []
        total_bytes = 0
        for src, dst in inputs:
            root = self._workspace
            candidate = Path(src)
            local = (candidate if candidate.is_absolute() else root / candidate)
            local = local.resolve()
            if local != root and root not in local.parents:
                raise RemotePathRefused(
                    f"input src {src!r} resolves to {local}, outside the "
                    f"workspace {root}; uploads stay inside the workspace"
                )
            if not local.is_file():
                raise RemotePathRefused(
                    f"input src {src!r} is not a file inside the workspace "
                    f"({local} does not exist or is not a regular file)"
                )
            size = local.stat().st_size
            if size > MAX_INPUT_FILE_BYTES:
                raise RemotePathRefused(
                    f"input src {src!r} is {size} bytes, over the "
                    f"{MAX_INPUT_FILE_BYTES}-byte single-file ceiling; data "
                    "this size belongs where it already is — reference it "
                    "remotely or stage it on the host"
                )
            total_bytes += size
            if total_bytes > MAX_INPUT_TOTAL_BYTES:
                raise RemotePathRefused(
                    f"the declared inputs total {total_bytes} bytes, over "
                    f"the {MAX_INPUT_TOTAL_BYTES}-byte ceiling for one "
                    "submission; send the job to the data instead of the "
                    "data to the job"
                )
            if not dst.strip():
                raise RemotePathRefused(
                    f"input dst for src {src!r} is empty; name where the "
                    "file lands inside the job's work directory"
                )
            # The bare name is checked before any joining, because
            # remote_join strips a leading slash — an absolute dst would
            # otherwise be silently re-anchored inside the workdir
            # instead of refused. The prefix assertion in submit_in is
            # the second lock; this one keeps the first lock honest.
            normalized_dst = posixpath.normpath(dst)
            if (
                normalized_dst.startswith("/")
                or normalized_dst == ".."
                or normalized_dst.startswith("../")
            ):
                raise RemotePathRefused(
                    f"input dst {dst!r} is not a name inside the job's "
                    "work directory; it may not be absolute and may not "
                    "climb out with .."
                )
            pairs.append((str(local), dst))
        return pairs

    def _resolve_dest(self, dest: str) -> Path:
        """A fetch destination as an absolute path inside the workspace.

        Relative to the workspace root — that is what the tool's schema
        promises — with an absolute path tolerated only when it is
        already inside. A ``..`` climb or an outside absolute path is
        refused before any transfer starts.
        """
        if self._workspace is None:
            raise RemotePathRefused(
                "this plane has no workspace bound, so a fetch destination "
                "cannot be anchored to one; refusing rather than writing "
                "anywhere reachable"
            )
        root = self._workspace
        candidate = Path(dest)
        local = (candidate if candidate.is_absolute() else root / candidate)
        local = local.resolve()
        if local != root and root not in local.parents:
            raise RemotePathRefused(
                f"dest {dest!r} resolves to {local}, outside the workspace "
                f"{root}; downloads land inside the workspace"
            )
        return local

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
