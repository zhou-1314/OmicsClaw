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

import asyncio
import hashlib
import json
import os
import posixpath
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
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
    build_sftp_read_batch,
    build_ssh_argv,
    encode_login_script,
    shell_quote,
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

REMOTE_FILE_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
"""Largest file the read-only target preview may bring through SSH."""

REMOTE_TREE_MAX_ENTRIES = 2000
"""Upper bound on one read-only target tree response."""


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    """The immutable target facts captured before a job is persisted."""

    alias: str
    workdir: str
    capabilities: tuple[str, ...]


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
        request_id: str = "",
        payload_hash: str = "",
        expected_inputs: dict[str, tuple[str, int]] | None = None,
    ) -> RemoteJobHandle:
        """Create *workdir*, upload *files* and *uploads*, start the job.

        *files* are contents staged locally and pushed by name;
        *uploads* are ``(local path, remote path)`` pairs of files that
        already exist — the caller's input files — verified to land
        inside *workdir* before anything moves.
        """
        validate_workdir = getattr(self._spawner, "validate_workdir", None)
        if callable(validate_workdir):
            validate_workdir(workdir)
        kind = detect_scheduler(command, scheduler)
        request_id = request_id or secrets.token_hex(16)
        payload_hash = payload_hash or hashlib.sha256(command.encode()).hexdigest()
        nonce = hashlib.sha256((request_id + "/" + payload_hash).encode()).hexdigest()[
            :32
        ]
        scripts = build_job_scripts(command, kind)
        scripts["_omicsclaw_nonce"] = nonce
        if set(files or {}).intersection(scripts):
            raise RemotePathRefused("job inputs cannot replace the execution wrapper")
        for local, remote in uploads or ():
            if not _inside_remote_dir(workdir, remote):
                raise RemotePathRefused(
                    f"an input's dst {remote!r} escapes the job's work "
                    f"directory {workdir!r}; dst is relative to that "
                    "directory and may not climb out of it"
                )
            if posixpath.basename(remote).startswith(
                "_omicsclaw_"
            ) or posixpath.basename(remote) in {"cmd.sh", "job.sh"}:
                raise RemotePathRefused("job inputs cannot replace execution evidence")
        await self.run(build_mkdir_command(workdir), timeout=UPLOAD_TIMEOUT_S)
        intent = json.dumps(
            {"request_id": request_id, "payload_hash": payload_hash, "kind": kind},
            sort_keys=True,
        )
        claim = await self.run(
            f"cd -- {shell_quote(workdir)} || exit 1; umask 077; "
            "if mkdir ./_omicsclaw_submission_claim 2>/dev/null; then "
            f"printf '%s' {shell_quote(intent)} > ./_omicsclaw_submission_claim/intent.tmp && "
            "mv -- ./_omicsclaw_submission_claim/intent.tmp ./_omicsclaw_submission_claim/intent.json "
            "&& printf '%s' CLAIMED; "
            f'elif [ "$(head -c 4096 ./_omicsclaw_submission_claim/intent.json 2>/dev/null)" = {shell_quote(intent)} ]; '
            "then printf '%s' OBSERVED; else printf '%s' CONFLICT; exit 1; fi",
            timeout=PROBE_TIMEOUT_S,
        )
        template = RemoteJobHandle(
            kind=kind,
            host=self.host,
            workdir=workdir,
            request_id=request_id,
            payload_hash=payload_hash,
        )
        if claim.exit_code != 0 or claim.output != "CLAIMED":
            if claim.exit_code == 0 and claim.output == "OBSERVED":
                return await self.recover_in(template)
            raise RuntimeError("remote_submission_claim_unconfirmed")
        await self._upload(workdir, {**scripts, **(files or {})}, uploads)
        for remote, (expected_hash, expected_size) in (expected_inputs or {}).items():
            quoted = shell_quote(remote)
            verified = await self.run(
                f"[ -f {quoted} ] && [ ! -L {quoted} ] && "
                f'[ "$(stat -c %s -- {quoted})" = {expected_size} ] && '
                f"_hash=$(sha256sum -- {quoted}) && _hash=${{_hash%% *}} && "
                f'[ "$_hash" = {shell_quote(expected_hash)} ]',
                timeout=UPLOAD_TIMEOUT_S,
            )
            if verified.exit_code != 0:
                raise RemoteTransferError("input_integrity_unconfirmed")
        outcome = await self.run(build_submit_command(workdir, kind), timeout=timeout)
        number = parse_submit_output(kind, outcome.output)
        if number <= 0:
            raise RuntimeError(
                f"submission on {self.host} produced no usable "
                f"{'job id' if kind == KIND_SLURM else 'pgid'}: {outcome.output!r}"
            )
        if kind == KIND_SLURM:
            return replace(template, job_id=str(number), submitted_at=time.time())
        recovered = await self.recover_in(template)
        if recovered.pgid != number or recovered.process_identity is None:
            raise RuntimeError("submission_identity_unconfirmed")
        return recovered

    async def recover_in(self, handle: RemoteJobHandle) -> RemoteJobHandle:
        """Read immutable submission evidence without staging or launching work."""
        if (
            handle.kind != KIND_NOHUP
            or not handle.request_id
            or not handle.payload_hash
        ):
            return handle
        try:
            outcome = await self.run(
                f"cd -- {shell_quote(handle.workdir)} && "
                "head -c 4096 ./_omicsclaw_submission_claim/intent.json && printf '\\n' && "
                "head -c 4096 ./_omicsclaw_process.json",
                timeout=PROBE_TIMEOUT_S,
            )
        except RemoteHostUnreachable:
            return handle
        if outcome.exit_code != 0:
            return handle
        try:
            raw_intent, raw_proof = outcome.output.split("\n", 1)
            intent, proof = json.loads(raw_intent), json.loads(raw_proof)
            if intent != {
                "request_id": handle.request_id,
                "payload_hash": handle.payload_hash,
                "kind": handle.kind,
            }:
                return handle
            nonce = hashlib.sha256(
                (handle.request_id + "/" + handle.payload_hash).encode()
            ).hexdigest()[:32]
            if not isinstance(proof, dict) or proof.get("nonce") != nonce:
                return handle
            return replace(handle, pgid=proof.get("pgid"), process_identity=proof)
        except (ValueError, TypeError):
            return handle

    async def status(self, handle: RemoteJobHandle) -> StatusReport:
        """Ask the host; an unreachable host answers ``unknown``, not an error."""
        if handle.kind == KIND_NOHUP and (
            handle.process_identity is None or handle.host != self.host
        ):
            return StatusReport(state="unknown", detail="unverified_process_identity")
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
        if handle.kind == KIND_NOHUP and (
            handle.process_identity is None or handle.host != self.host
        ):
            return StatusReport(state="unknown", detail="unverified_process_identity")
        try:
            outcome = await self.run(
                build_cancel_command(handle), timeout=PROBE_TIMEOUT_S
            )
        except RemoteHostUnreachable as exc:
            return StatusReport(state="unknown", detail=str(exc))
        if outcome.exit_code != 0:
            return StatusReport(
                state="unknown", detail="cancel_identity_or_signal_unconfirmed"
            )
        return await self.status(handle)

    async def sizes(self, workdir: str, names: list[str]) -> list[int]:
        """Byte sizes of *names* under *workdir*; ``-1`` where unknown."""
        paths = [remote_join(workdir, name) for name in names]
        outcome = await self.run(build_size_command(paths), timeout=PROBE_TIMEOUT_S)
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

    async def list_tree(
        self, root: str, *, depth: int, boundary: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """List regular files and directories through read-only SFTP."""
        boundary_real = await self._sftp_realpath(boundary)
        if not await self._sftp_is_directory(boundary):
            raise RemotePathRefused(f"target boundary {boundary!r} is not a directory")
        root_real = await self._sftp_realpath(root)
        if not _inside_remote_dir(boundary_real, root_real):
            raise RemotePathRefused(
                f"target path {root!r} escapes boundary {boundary!r}"
            )
        if not await self._sftp_is_directory(root):
            raise RemotePathRefused(f"cannot list target path {root!r}")

        entries: list[dict[str, Any]] = []
        queue: list[tuple[str, int]] = [(root_real, 0)]
        while queue and len(entries) <= REMOTE_TREE_MAX_ENTRIES:
            current, level = queue.pop(0)
            for item in await self._sftp_listing(current):
                if item["type"] not in {"file", "directory"}:
                    continue
                path = remote_join(current, item["name"])
                try:
                    canonical = await self._sftp_realpath(path)
                except RemotePathRefused:
                    continue
                if not _inside_remote_dir(boundary_real, canonical):
                    continue
                item = {**item, "path": path}
                entries.append(item)
                if item["type"] == "directory" and level + 1 < depth:
                    queue.append((canonical, level + 1))
                if len(entries) > REMOTE_TREE_MAX_ENTRIES:
                    break
        return entries[:REMOTE_TREE_MAX_ENTRIES], len(entries) > REMOTE_TREE_MAX_ENTRIES

    async def stat_file(self, path: str, *, boundary: str) -> int:
        """Return a regular file size after enforcing the SFTP boundary."""
        boundary_real = await self._sftp_realpath(boundary)
        if not await self._sftp_is_directory(boundary):
            raise RemotePathRefused(f"target boundary {boundary!r} is not a directory")
        candidate_real = await self._sftp_realpath(path)
        if not _inside_remote_dir(boundary_real, candidate_real):
            raise RemotePathRefused(
                f"cannot read target file {path!r}: outside boundary"
            )
        metadata = await self._sftp_lstat(path)
        if metadata["type"] != "file":
            raise RemotePathRefused(f"cannot read target file {path!r}")
        return int(metadata["size"])

    async def read_bytes(
        self,
        path: str,
        *,
        boundary: str,
        start: int = 0,
        max_bytes: int,
    ) -> bytes:
        """Read a bounded range using SFTP's read-only ``get`` command.

        OpenSSH's command-line SFTP client exposes whole-file ``get`` only;
        the bounded slice is therefore taken from a private local staging
        file after the same boundary and size checks.
        """
        if start < 0 or max_bytes < 0 or max_bytes > REMOTE_FILE_PREVIEW_MAX_BYTES:
            raise ValueError("target preview range exceeds the hard byte limit")
        if max_bytes == 0:
            return b""
        total_size = await self.stat_file(path, boundary=boundary)
        # OpenSSH's batch SFTP client only exposes whole-file ``get``.  Do
        # not fetch a large remote file just to slice it locally: callers
        # must receive the stable preview-too-large response instead.
        if total_size > REMOTE_FILE_PREVIEW_MAX_BYTES:
            raise ValueError("preview_too_large")
        from .ssh import sftp_quote

        with tempfile.NamedTemporaryFile(
            prefix="omicsclaw-preview-", delete=False
        ) as sink:
            local = sink.name
        try:
            await self._sftp(
                build_sftp_read_batch(
                    [f"get -- {sftp_quote(path)} {sftp_quote(local)}"]
                ),
                PROBE_TIMEOUT_S,
            )

            def read_range():
                with Path(local).open("rb") as source:
                    source.seek(start)
                    return source.read(max_bytes)

            return await asyncio.to_thread(read_range)
        finally:
            try:
                Path(local).unlink()
            except FileNotFoundError:
                pass

    async def _sftp_request(self, commands: list[str]) -> str:
        outcome = await self._spawner.spawn(
            build_sftp_argv(self.host),
            timeout=PROBE_TIMEOUT_S,
            stdin_text=build_sftp_read_batch(commands),
        )
        if outcome.exit_code != 0:
            raise RemotePathRefused(
                f"SFTP request on {self.host} failed: {outcome.output.strip()[-300:]}"
            )
        return outcome.output

    async def _sftp_realpath(self, path: str) -> str:
        from .ssh import sftp_quote

        output = await self._sftp_request([f"realpath {sftp_quote(path)}"])
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            raise RemotePathRefused(f"cannot resolve remote path {path!r}")
        return lines[-1]

    async def _sftp_listing(self, path: str) -> list[dict[str, Any]]:
        from .ssh import sftp_quote

        output = await self._sftp_request([f"ls -ln {sftp_quote(path)}"])
        result = []
        for line in output.splitlines():
            parsed = _parse_sftp_listing_line(line)
            if parsed is not None:
                prefix = path.rstrip("/") + "/"
                parsed["name"] = parsed["name"].removeprefix(prefix)
                result.append(parsed)
        return result

    async def _sftp_lstat(self, path: str) -> dict[str, Any]:
        from .ssh import sftp_quote

        output = await self._sftp_request([f"ls -ldn {sftp_quote(path)}"])
        for line in output.splitlines():
            parsed = _parse_sftp_listing_line(line)
            if parsed is not None:
                return parsed
        raise RemotePathRefused(f"cannot stat remote path {path!r}")

    async def _sftp_is_directory(self, path: str) -> bool:
        return (await self._sftp_lstat(path))["type"] == "directory"

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
            prefix=f"omicsclaw-upload-{secrets.token_hex(4)}-",
            dir=getattr(self._spawner, "staging_root", None),
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


def _submission_input_evidence(pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Hash bounded stable files; the staged bytes must match before launch."""
    evidence = []
    total = 0
    destinations = set()
    for local, destination in pairs:
        if destination in destinations:
            raise RemotePathRefused("duplicate_input_destination")
        destinations.add(destination)
        digest = hashlib.sha256()
        size = 0
        with Path(local).open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                total += len(chunk)
                if size > MAX_INPUT_FILE_BYTES or total > MAX_INPUT_TOTAL_BYTES:
                    raise RemotePathRefused("submission_input_budget_exceeded")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = Path(local).stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        identity = tuple(getattr(before, key) for key in fields)
        if (
            size != before.st_size
            or identity != tuple(getattr(after, key) for key in fields)
            or identity != tuple(getattr(current, key) for key in fields)
        ):
            raise RemoteTransferError("submission_input_changed")
        evidence.append(
            {"destination": destination, "sha256": digest.hexdigest(), "size": size}
        )
    return evidence


_SFTP_LONG_RE = re.compile(
    r"^(?P<mode>[bcdlps-][rwxXsStT-]{9})\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<clock>\d{2}:\d{2}|\d{4})\s+(?P<name>.*)$"
)


def _parse_sftp_listing_line(line: str) -> dict[str, Any] | None:
    """Parse one OpenSSH ``sftp ls -ln`` record without invoking a shell."""
    match = _SFTP_LONG_RE.match(line.strip())
    if match is None:
        return None
    mode = match.group("mode")
    name = match.group("name")
    if " -> " in name and mode.startswith("l"):
        name = name.split(" -> ", 1)[0]
    mtime: float | None = None
    try:
        stamp = match.group("clock")
        year = datetime.now().year if ":" in stamp else int(stamp)
        if ":" in stamp:
            value = datetime.strptime(
                f"{year} {match.group('month')} {match.group('day')} {stamp}",
                "%Y %b %d %H:%M",
            )
        else:
            value = datetime.strptime(
                f"{year} {match.group('month')} {match.group('day')}",
                "%Y %b %d",
            )
        mtime = value.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    return {
        "path": "",
        "name": name,
        "type": {"d": "directory", "-": "file"}.get(mode[0], "other"),
        "size": int(match.group("size")),
        "mtime": mtime,
    }


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

    @property
    def spawner(self) -> SshSpawner:
        """The transport every remote operation of this deployment
        shares — what the C3 kernel runtime is handed so its kernels,
        its environment probes and the remote tools ride one
        ControlMaster pool per host."""
        return self._spawner

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
        return await RemoteExecutor(host, self._spawner).run(command, timeout=timeout)

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
        target_workdir: str = "",
        request_id: str = "",
    ) -> SubmitOutcome:
        validate_host(host)
        resolved_inputs = self._resolve_inputs(inputs)
        executor = RemoteExecutor(host, self._spawner)
        request_id = request_id or (
            "job:" + local_job_id if local_job_id else secrets.token_hex(16)
        )
        original = self._jobs.by_request(request_id)
        workdir = target_workdir.strip()
        if not workdir and original is not None:
            workdir = original.workdir
        if not workdir:
            probe = await self._probe_into_store(executor, host)
            workdir = remote_join(
                probe.scratch_root(),
                "omicsclaw/"
                + time.strftime("%Y%m%d-%H%M%S")
                + "-"
                + secrets.token_hex(3),
            )
        uploads = [(local, remote_join(workdir, dst)) for local, dst in resolved_inputs]
        files: dict[str, str] = {}
        if outputs:
            files[OUTPUTS_FILE] = "\n".join(outputs) + "\n"
        input_evidence = await asyncio.to_thread(
            _submission_input_evidence, resolved_inputs
        )
        canonical = {
            "host": host,
            "command": command,
            "inputs": input_evidence,
            "outputs": outputs,
            "scheduler": scheduler,
            "timeout": timeout,
            "label": label,
            "local_job_id": local_job_id,
            "workdir": workdir,
        }
        payload_hash = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        unbound = RemoteJobHandle(
            kind=detect_scheduler(command, scheduler),
            host=host,
            workdir=workdir,
            local_job_id=local_job_id,
            target_alias=host,
            target_workdir=workdir,
            capabilities=("remote_exec", "remote_submit"),
        )
        row = self._jobs.claim_submission(request_id, payload_hash, unbound)
        handle = row.handle()
        if row.newly_claimed:
            launched = await executor.submit_in(
                workdir,
                command,
                scheduler=scheduler,
                files=files,
                uploads=uploads,
                timeout=timeout,
                request_id=request_id,
                payload_hash=payload_hash,
                expected_inputs={
                    remote_join(workdir, item["destination"]): (
                        item["sha256"],
                        item["size"],
                    )
                    for item in input_evidence
                },
            )
            handle = replace(
                launched,
                local_job_id=local_job_id,
                target_alias=host,
                target_workdir=workdir,
                capabilities=unbound.capabilities,
            )
        elif handle.pgid is None and handle.job_id is None:
            handle = await executor.recover_in(handle)
        if handle.process_identity is not None or handle.job_id is not None:
            row = self._jobs.bind_submission(row.id, handle)
        return self._submission_outcome(row)

    @staticmethod
    def _submission_outcome(row) -> SubmitOutcome:
        handle = row.handle()
        return SubmitOutcome(
            job_ref=str(row.id),
            kind=handle.kind,
            job_id=handle.job_id,
            pgid=handle.pgid,
            workdir=handle.workdir,
            host=handle.host,
            submitted_at=handle.submitted_at,
        )

    async def resolve_target(
        self,
        alias: str,
        *,
        capabilities: tuple[str, ...] = ("remote_exec", "remote_submit"),
        target_root: str = "",
    ) -> TargetSnapshot:
        """Resolve a target once, including the workdir used by its job.

        Callers persist the returned facts and pass the workdir back to
        :meth:`submit`; dispatch therefore never re-reads mutable target
        configuration after a job has been created.
        """
        validate_host(alias)
        validate_workdir = getattr(self._spawner, "validate_workdir", None)
        if target_root and callable(validate_workdir):
            validate_workdir(target_root)
        executor = RemoteExecutor(alias, self._spawner)
        probe = (
            await self._probe_into_store(executor, alias) if not target_root else None
        )
        workdir = remote_join(
            target_root or probe.scratch_root(),
            "omicsclaw/" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3),
        )
        return TargetSnapshot(alias, workdir, tuple(capabilities))

    async def status(self, job_ref: str) -> StatusReport:
        row = self._jobs.get(job_ref)
        if row is None:
            return StatusReport(
                state="unknown", detail="no such job_ref", job_ref=job_ref
            )
        executor = RemoteExecutor(row.host_alias, self._spawner)
        handle = row.handle()
        if row.source_request_id and handle.pgid is None and handle.job_id is None:
            handle = await executor.recover_in(handle)
            if handle.process_identity is not None:
                row = self._jobs.bind_submission(row.id, handle)
        report = await executor.status(row.handle())
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
            state=report.state,
            detail=report.detail,
            exit_code=report.exit_code,
            job_ref=job_ref,
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
        missing = [name for name, size in zip(names, sizes) if size is None or size < 0]
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

    def host_store(self) -> RemoteHostStore:
        """The host knowledge base — the REST face's read/delete entry point."""
        return self._hosts

    def target_root(self, alias: str, workdir: str = "") -> str:
        """Resolve a target's browsable root under its probed scratch."""
        validate_host(alias)
        card = self.host_card(alias)
        if card is None:
            raise KeyError(alias)
        if card.parse_error:
            raise RemotePathRefused(
                "remote_files_unsupported: host probe is incomplete"
            )
        scratch = card.scratch_root or ""
        if not scratch or scratch in {"~", ".", "./"}:
            raise RemotePathRefused(
                "workdir_unavailable: probe did not return an absolute scratch root"
            )
        root = workdir.strip() or scratch
        if not _inside_remote_dir(scratch, root):
            raise RemotePathRefused(
                f"target workdir {root!r} escapes the target scratch root {scratch!r}"
            )
        return posixpath.normpath(root)

    async def list_target_files(
        self, alias: str, *, workdir: str = "", path: str = "", depth: int = 2
    ) -> tuple[str, list[dict[str, Any]], bool]:
        root = self.target_root(alias, workdir)
        candidate = remote_join(root, path)
        if not _inside_remote_dir(root, candidate):
            raise RemotePathRefused(
                f"target path {path!r} escapes the target workdir {root!r}"
            )
        entries, has_more = await self.executor_for(alias).list_tree(
            candidate, boundary=root, depth=depth
        )
        return root, entries, has_more

    async def read_target_file(
        self,
        alias: str,
        *,
        workdir: str = "",
        path: str = "",
        start: int = 0,
        count: int | None = None,
    ) -> tuple[str, bytes, int]:
        root = self.target_root(alias, workdir)
        candidate = remote_join(root, path)
        if not _inside_remote_dir(root, candidate) or candidate == root:
            raise RemotePathRefused(
                f"target file path {path!r} is outside the target workdir {root!r}"
            )
        executor = self.executor_for(alias)
        total = await executor.stat_file(candidate, boundary=root)
        if start < 0 or start > total:
            raise ValueError("invalid target file range")
        if count is None:
            count = total - start
        if count < 0 or count > REMOTE_FILE_PREVIEW_MAX_BYTES:
            raise ValueError("target preview range exceeds the hard byte limit")
        content = await executor.read_bytes(
            candidate, boundary=root, start=start, max_bytes=count
        )
        return root, content, total

    async def stat_target_file(
        self, alias: str, *, workdir: str = "", path: str = ""
    ) -> tuple[str, int]:
        """Stat one target file without reading any bytes (serve preflight)."""
        root = self.target_root(alias, workdir)
        candidate = remote_join(root, path)
        if not _inside_remote_dir(root, candidate) or candidate == root:
            raise RemotePathRefused(
                f"target file path {path!r} is outside the target workdir {root!r}"
            )
        return root, await self.executor_for(alias).stat_file(candidate, boundary=root)

    def list_host_cards(self) -> list[HostCard]:
        """Every stored host as a card, alias-ordered. Reads only.

        The synchronous twin of :meth:`probe_card`: a list view is
        answered from what the knowledge base already holds, so it never
        opens a socket on an event loop's behalf — the same line
        :meth:`host_card` draws.
        """
        return [self._card_from_record(record) for record in self._hosts.list_records()]

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
            local = candidate if candidate.is_absolute() else root / candidate
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
            if normalized_dst.startswith(("/", "../")) or normalized_dst == "..":
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
        local = candidate if candidate.is_absolute() else root / candidate
        local = local.resolve()
        if local != root and root not in local.parents:
            raise RemotePathRefused(
                f"dest {dest!r} resolves to {local}, outside the workspace "
                f"{root}; downloads land inside the workspace"
            )
        return local

    async def _probe_into_store(self, executor: RemoteExecutor, host: str) -> HostProbe:
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
            rest = text[len("remote://") :]
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
                listed = await executor.read_relative(handle.workdir, OUTPUTS_FILE)
                names = [
                    line.strip() for line in listed.splitlines() if line.strip()
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
    "UPLOAD_TIMEOUT_S",
    "RemoteExecutor",
    "RemotePlaneBinding",
    "TargetSnapshot",
    "remote_join",
]
