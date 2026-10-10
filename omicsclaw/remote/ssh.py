"""Spawning the system ``ssh`` and ``sftp`` — and nothing else in between.

The transport half of C1. The remote runtime plan's ruling is Claude
Science's: the remote host is asked for **sshd and a POSIX shell**, so
the client side should be the system's own OpenSSH — not a pip SSH
library. Three reasons, all bought by somebody else's failures:

* **Zero new dependencies.** A pip SSH stack (paramiko and friends) is a
  wheel matrix, a cryptography build and a CVE feed this project would
  own forever, for capability ``/usr/bin/ssh`` already has.
* **The user's config is the contract.** ``~/.ssh/config`` aliases,
  ``ProxyJump`` chains, agent forwarding policy and known hosts all
  resolve exactly as they do in the user's terminal. An in-process
  client re-implements a subset of that, differently.
* **Host keys stay the system's business.** Nothing here relaxes
  ``StrictHostKeyChecking`` or writes a fresh ``known_hosts``; the
  security posture of a remote run is the posture of the user's own
  ``ssh`` invocation, no better and no worse.

**Authentication is key- or agent-based only.** :data:`SSH_OPTIONS`
pins ``BatchMode=yes``, so ``ssh`` fails rather than prompting for a
password, and no password parameter exists anywhere in this package.
That is the plan's explicit "not doing" and this is where it is enforced.

**Construction and execution are separate layers**, so the construction
half can be tested without a network: :func:`build_ssh_argv` and
:func:`build_sftp_argv` return ``argv`` lists — pure functions — and
:class:`SystemSshSpawner` is the only place a process starts. A test
double for the spawner is a dict of canned outcomes; the real one writes
output to a temporary file rather than a pipe, for the reason
``builtin/bash.py`` measures at length: a remote command that
backgrounds a grandchild holding the write end of a pipe makes ``wait``
block on something that is not the command.

**Injection is bounded twice over.** A ``host`` is validated against a
closed alphabet (:func:`validate_host`) *and* the ``argv`` puts ``--``
before the destination, so a value that slipped the alphabet could not
become an option. A ``command`` is never filtered — it is a whole
programming language and the boundary is the approval gate, the ruling
``bash.py`` states for its own single argument — but it travels as one
``argv`` element to an ``exec``-style spawn, so no *local* shell ever
re-parses it; what the remote shell does with it is what the human
approved.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import os
import signal
import tempfile
from typing import Protocol, runtime_checkable

from omicsclaw.tools.builtin.remote import ExecOutcome, valid_host


def _mux_token() -> str:
    """The local-user component of the ControlPath.

    ``/tmp`` is world-writable and shared, so two users of one machine
    multiplexing to the same destination would collide on one socket —
    and the socket is mode 600, so for the second user the collision
    reads as "connection failed". The uid prefixes the hash so each
    local user gets a lane of their own. Windows has no ``os.getuid``
    (the one platform this package does not target but refuses to crash
    on), so the user name stands in, reduced to the socket-name
    alphabet.
    """
    try:
        return "u" + str(os.getuid())
    except AttributeError:  # pragma: no cover - Windows
        cleaned = "".join(
            ch for ch in (getpass.getuser() or "") if ch.isalnum() or ch in "-_"
        )
        return cleaned or "shared"


SSH_OPTIONS: tuple[str, ...] = (
    "-o", "BatchMode=yes",
    "-o", "ForwardAgent=no",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath=/tmp/omicsclaw-mux-{_mux_token()}-%C",
    "-o", "ControlPersist=60",
)
"""Every ``ssh``/``sftp`` invocation this package makes carries these.

``BatchMode=yes`` is the authentication ruling: fail, never prompt.
``ForwardAgent=no`` keeps the agent on this machine — a remote command
does not inherit the right to sign further SSH sessions.
``ControlMaster``/``ControlPersist`` reuse one multiplexed connection
per host so the second call pays no handshake; ``%C`` hashes the
destination so two hosts never share a socket, and the uid ahead of it
(see :func:`_mux_token`) so two local users of one machine never share
one either.

Notably absent: anything about ``StrictHostKeyChecking`` or
``UserKnownHostsFile``. Host verification falls through to the user's
own configuration and the system default, which is strict on every
mainstream distribution — relaxing it is exactly the trade this package
refuses to make.
"""

SSH_EXIT_HOST_UNREACHABLE = 255
"""``ssh``'s own status for "could not establish the connection" — the
status code that means *the transport failed*, distinct from any status
the remote command produced."""


class RemoteHostRefused(ValueError):
    """A ``host`` argument is not one closed-alphabet word.

    A ``ValueError`` because it is a caller's mistake, caught before any
    process starts; the tool layer reports the same condition to the
    model as a correctable :exc:`ToolArgumentError`.
    """


class RemotePathRefused(ValueError):
    """A path argument would leave the boundary it was promised to.

    The same ruling as :class:`RemoteHostRefused`, aimed one argument
    over: an upload whose ``src`` is not inside the workspace, a ``dst``
    that would climb out of the remote work directory, a fetch ``dest``
    resolving outside the workspace. A ``ValueError`` because the model
    can fix it by sending different arguments, and raised before any
    byte moves.
    """


class RemoteTransferError(RuntimeError):
    """An SFTP transfer did not succeed, and will not be reported as if
    it had.

    Carries the transport's own output (stderr included — the spawner
    merges them) so the failure the model reads is the failure the
    transfer had, not a generic one.
    """


class RemoteHostUnreachable(RuntimeError):
    """The transport could not reach the host (``ssh`` exited 255).

    Distinct from a remote command's non-zero exit, which is that
    command's own result. Callers that tolerate outages — the status
    poller, the reconciler — catch this and answer ``unknown`` rather
    than failing.
    """


def validate_host(host: str) -> str:
    """Return *host* if it is safe to splice, or refuse it.

    :raises RemoteHostRefused: empty, or anything other than one word of
        ``[A-Za-z0-9._-]``, or a leading dash (which ``ssh`` would read
        as an option even behind ``--``-validation mistakes elsewhere).
    """
    if not valid_host(host):
        raise RemoteHostRefused(
            f"host {host!r} is not a usable SSH destination: it must be "
            "one word of letters, digits, dots, dashes and underscores, "
            "with no leading dash (an ssh config alias or DNS name)"
        )
    return host


def shell_quote(text: str) -> str:
    """Quote *text* for the remote POSIX shell, single-quote style.

    ``'`` becomes ``'\''`` and everything else is literal inside the
    quotes. The result is safe to splice into a command string that the
    remote shell will parse — used for paths this package constructs
    (work directories, log names), never for model-supplied command
    text, which travels unparsed.
    """
    return "'" + text.replace("'", "'\\''") + "'"


def build_ssh_argv(
    host: str,
    command: str,
    *,
    options: tuple[str, ...] = SSH_OPTIONS,
) -> list[str]:
    """The ``argv`` that runs *command* on *host*. Pure.

    ``["ssh", *options, "--", host, command]`` — the ``--`` ends option
    parsing, the host is one validated word, and *command* is one argv
    element the remote user's shell interprets. There is no local shell
    anywhere in this path.
    """
    validate_host(host)
    return ["ssh", *options, "--", host, command]


def build_sftp_argv(
    host: str,
    *,
    options: tuple[str, ...] = SSH_OPTIONS,
) -> list[str]:
    """The ``argv`` that opens a batch SFTP session on *host*. Pure.

    ``-b -`` reads the batch from standard input, so the transfer
    instructions never appear in a process list or a shell history.
    """
    validate_host(host)
    return ["sftp", *options, "-b", "-", host]


def sftp_quote(path: str) -> str:
    """Quote *path* for an SFTP batch line. Double-quote style.

    SFTP's batch parser is not a shell; it splits words and honours
    double quotes, so ``"`` and ``\\`` are backslash-escaped inside
    them. Always quoting keeps the rule one-shaped.
    """
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_sftp_batch(
    puts: list[tuple[str, str]] | None = None,
    gets: list[tuple[str, str]] | None = None,
) -> str:
    """One SFTP batch document from *puts* and *gets* of local/remote pairs.

    Pure. Each line is ``put|get -- <local> <remote>`` with both sides
    quoted; ``--`` stops SFTP parsing a path that begins with ``-`` as a
    flag. ``exit`` ends the batch so the session closes even when a
    caller sends nothing.
    """
    lines: list[str] = []
    for local, remote in puts or ():
        lines.append(f"put -- {sftp_quote(local)} {sftp_quote(remote)}")
    for remote, local in gets or ():
        lines.append(f"get -- {sftp_quote(remote)} {sftp_quote(local)}")
    lines.append("exit")
    return "\n".join(lines) + "\n"


def encode_login_script(script: str) -> str:
    """Wrap *script* so a login shell on the far end runs it verbatim.

    ``echo <base64> | base64 -d | bash -l``. Base64 removes every
    quoting question in both shells at the cost of one universally
    present utility (GNU coreutils, busybox and macOS all ship it) —
    the same trade OmicOS's bootstrap made. ``bash -l`` rather than
    ``sh`` because the point of a login shell is the module/conda
    initialisation a probe exists to observe.
    """
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"echo {payload} | base64 -d | bash -l"


@runtime_checkable
class SshSpawner(Protocol):
    """Where an ``ssh``/``sftp`` process actually runs. Injected, never
    imported — the seam every caller in this package is tested through.

    An implementation runs *argv* with *timeout* seconds, feeds
    *stdin_text* (if any) to the child's standard input, and returns the
    merged output with the exit status in shell convention. A transport
    failure should surface as :exc:`RemoteHostUnreachable`; a non-zero
    exit of the *remote command* is a result, not an error.
    """

    async def spawn(
        self, argv: list[str], *, timeout: float, stdin_text: str | None = None
    ) -> ExecOutcome:
        """Run *argv*, return what it printed and how it ended."""
        ...


def _exit_status(returncode: int) -> int:
    """A signal death as the shell would print it — the ``bash.py`` rule."""
    return 128 - returncode if returncode < 0 else returncode


class SystemSshSpawner:
    """Run ``ssh``/``sftp`` as real subprocesses of this process.

    The single place in the remote package a process starts. Output goes
    to a temporary file, not a pipe (the ``bash.py`` measurement: a
    grandchild holding the pipe's write end makes ``wait`` hang for as
    long as the grandchild lives); the child leads its own session, so a
    deadline SIGKILLs the whole client-side process group. Killing the
    client does not stop the remote command — that is a property of SSH
    itself, stated in the ``remote_exec`` description, not a bug here.
    """

    async def spawn(
        self, argv: list[str], *, timeout: float, stdin_text: str | None = None
    ) -> ExecOutcome:
        handle, capture = tempfile.mkstemp(prefix="omicsclaw-ssh-", suffix=".log")
        try:
            with os.fdopen(handle, "wb") as sink:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=sink,
                    stderr=sink,
                    stdin=(
                        asyncio.subprocess.PIPE
                        if stdin_text is not None
                        else asyncio.subprocess.DEVNULL
                    ),
                    start_new_session=True,
                )
                timed_out = False
                try:
                    if stdin_text is not None and process.stdin is not None:
                        process.stdin.write(stdin_text.encode("utf-8"))
                        await process.stdin.drain()
                        process.stdin.close()
                    async with asyncio.timeout(timeout):
                        code = await process.wait()
                except TimeoutError:
                    timed_out = True
                    code = await self._kill(process)
                except asyncio.CancelledError:
                    # Named, not handled: kill the client group, then let
                    # the cancellation continue untouched (bash.py trap 5).
                    await self._kill(process)
                    raise
                finally:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
            with open(capture, "rb") as saved:
                output = saved.read().decode("utf-8", errors="replace")
        finally:
            os.unlink(capture)
        exit_code = _exit_status(code)
        if timed_out:
            return ExecOutcome(
                output=output, exit_code=exit_code, timed_out=True, host=""
            )
        if exit_code == SSH_EXIT_HOST_UNREACHABLE:
            raise RemoteHostUnreachable(
                "ssh exited 255 — the host could not be reached or refused "
                "the key; nothing ran remotely"
            )
        return ExecOutcome(output=output, exit_code=exit_code, host="")

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> int:
        """SIGKILL the client's process group and reap it, best-effort."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            return await asyncio.wait_for(process.wait(), 2.0)
        except TimeoutError:
            return -signal.SIGKILL


async def run_ssh(
    spawner: SshSpawner,
    host: str,
    command: str,
    *,
    timeout: float,
) -> ExecOutcome:
    """Run *command* on *host* through *spawner*; the one-liner callers use."""
    return await spawner.spawn(
        build_ssh_argv(host, command), timeout=timeout
    )


__all__ = [
    "RemoteHostRefused",
    "RemoteHostUnreachable",
    "RemotePathRefused",
    "RemoteTransferError",
    "SSH_EXIT_HOST_UNREACHABLE",
    "SSH_OPTIONS",
    "SshSpawner",
    "SystemSshSpawner",
    "build_sftp_argv",
    "build_sftp_batch",
    "build_ssh_argv",
    "encode_login_script",
    "run_ssh",
    "sftp_quote",
    "shell_quote",
    "validate_host",
]
