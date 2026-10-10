"""Job scripts, handles and the state vocabulary — the pure half of C1.

A remote job is two small files and a number. The files are ``cmd.sh``
(the model's command, verbatim) and ``job.sh`` (a wrapper that records
how the command ended); the number is a SLURM job id or a process-group
id, and it is the whole handle — Claude Science's ruling, which this
package adopts: the remote side runs what the host already knows how to
run (``sbatch``, ``setsid nohup``), and the local side needs no daemon,
no agent and no version matrix to stay in touch with it, only the
commands built here.

**Everything in this module is pure.** Script generation, submission
command construction, and the parsing of what comes back are functions
over strings, so the whole submission state machine is tested without a
network; the process half (:mod:`omicsclaw.remote.plane`) only stitches
them together. The remote-side contract is deliberately readable: a
person who ``ssh``es into the work directory finds ``job.log``,
``_omicsclaw_exit_code`` and, for a nohup job, ``_omicsclaw_pgid`` —
plain files, no magic, so the rare manual intervention needs no
documentation from us.

**The ``#SBATCH`` promotion rule.** ``scheduler="auto"`` submits with
``sbatch --parsable`` exactly when the command itself carries ``#SBATCH``
directive lines — the form a user's existing cluster script already has
— and otherwise runs it under ``setsid nohup``. The user's directives,
not a re-implementation of them: this package never parses resource
requests out of the command to synthesize its own ``sbatch`` flags,
which is how a wrapper silently disagrees with the script it wraps.

**nohup jobs report their own death.** The wrapper writes
``_omicsclaw_exit_code`` on every exit path (no ``set -e``, so a failing
command still gets its code recorded), and the launcher writes its own
process id — which, under ``setsid``, is the session id and therefore
the process-group id — to ``_omicsclaw_pgid`` before ``exec``-ing the
wrapper. Status is then two facts the host already knows: does the group
still exist (``kill -0``), and if not, what code did it leave behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .ssh import shell_quote

SBATCH_DIRECTIVE = "#SBATCH"
KIND_SLURM = "slurm"
KIND_NOHUP = "nohup"
SCHEDULERS = ("auto", "slurm", "none")

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"
UNKNOWN = "unknown"
STATES = (PENDING, RUNNING, DONE, FAILED, CANCELED, UNKNOWN)
IN_FLIGHT_STATES = (PENDING, RUNNING)

_RUNNING_MARK = "__OMICSCLAW_RUNNING__"
_LOST_MARK = "__OMICSCLAW_LOST__"
_PGID_FILE = "_omicsclaw_pgid"
_EXIT_FILE = "_omicsclaw_exit_code"
_LOG_FILE = "job.log"

NOHUP_LAUNCHER = (
    "#!/usr/bin/env bash\n"
    f"printf '%s' \"$$\" > ./{_PGID_FILE}\n"
    f"exec bash ./job.sh > ./{_LOG_FILE} 2>&1\n"
)
"""The first process of a nohup job. ``$$`` under ``setsid`` is the
session id — the pgid every member shares — so recording it before
``exec`` hands the local side a durable group handle even though the
launcher process itself is gone an instant later. The log is truncated
on start, which is correct: a work directory belongs to one job."""

NOHUP_WRAPPER = (
    "#!/usr/bin/env bash\n"
    "bash ./cmd.sh\n"
    'status=$?\n'
    f"printf '%s' \"$status\" > ./{_EXIT_FILE}\n"
    'exit "$status"\n'
)
"""Runs the model's command and records its fate. No ``set -e`` and no
traps: the two lines after the command must run whether it succeeded,
failed or was signalled, or the exit-code file — the only record of how
a finished nohup job ended — would never exist."""


@dataclass(frozen=True, slots=True)
class RemoteJobHandle:
    """The durable handle to one remote job — all C2 needs to resume.

    ``kind`` decides which member matters: ``job_id`` for SLURM (the
    string ``sbatch --parsable`` printed), ``pgid`` for nohup. Frozen
    and JSON-round-trippable because the handle outlives the process
    that created it — that is the point of persisting it.
    """

    kind: str
    host: str
    workdir: str
    job_id: str | None = None
    pgid: int | None = None
    submitted_at: float = 0.0
    local_job_id: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "host": self.host,
                "workdir": self.workdir,
                "job_id": self.job_id,
                "pgid": self.pgid,
                "submitted_at": self.submitted_at,
                "local_job_id": self.local_job_id,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> "RemoteJobHandle":
        decoded = json.loads(text) if text.strip() else {}
        return cls(
            kind=str(decoded.get("kind") or KIND_NOHUP),
            host=str(decoded.get("host") or ""),
            workdir=str(decoded.get("workdir") or ""),
            job_id=decoded.get("job_id"),
            pgid=decoded.get("pgid"),
            submitted_at=float(decoded.get("submitted_at") or 0.0),
            local_job_id=str(decoded.get("local_job_id") or ""),
        )

    def identity(self) -> str:
        """The host-side number, whichever kind this is."""
        return self.job_id if self.kind == KIND_SLURM else str(self.pgid)


def detect_scheduler(command: str, scheduler: str = "auto") -> str:
    """Resolve the scheduler choice to ``slurm`` or ``nohup``.

    ``auto`` promotes to SLURM exactly when the command carries a
    ``#SBATCH`` directive line (leading whitespace allowed, the way
    ``sbatch`` itself reads them). ``slurm`` and ``none`` are the user's
    override in either direction.

    :raises ValueError: *scheduler* is none of the three.
    """
    if scheduler == "slurm":
        return KIND_SLURM
    if scheduler == "none":
        return KIND_NOHUP
    if scheduler != "auto":
        raise ValueError(
            f"scheduler must be one of {', '.join(SCHEDULERS)}; got {scheduler!r}"
        )
    promoted = any(
        line.lstrip().startswith(SBATCH_DIRECTIVE)
        for line in command.splitlines()
    )
    return KIND_SLURM if promoted else KIND_NOHUP


def build_job_scripts(command: str, kind: str) -> dict[str, str]:
    """The files a work directory needs for a job of *kind*. Pure.

    SLURM: the command **is** ``job.sh`` (its ``#SBATCH`` lines only
    work if sbatch reads them), with a bash shebang prepended when the
    command did not open with one, because ``sbatch`` runs a shebang-less
    script with ``/bin/sh``.

    nohup: three files — ``cmd.sh`` is the command verbatim under a
    shebang, ``job.sh`` the wrapper that records the exit code, and
    ``_omicsclaw_launch.sh`` the session leader that records the pgid.
    """
    if kind == KIND_SLURM:
        script = command if command.startswith("#!") else (
            "#!/usr/bin/env bash\n" + command
        )
        return {"job.sh": script if script.endswith("\n") else script + "\n"}
    if kind == KIND_NOHUP:
        cmd = command if command.startswith("#!") else (
            "#!/usr/bin/env bash\n" + command
        )
        return {
            "cmd.sh": cmd if cmd.endswith("\n") else cmd + "\n",
            "job.sh": NOHUP_WRAPPER,
            "_omicsclaw_launch.sh": NOHUP_LAUNCHER,
        }
    raise ValueError(f"unknown job kind {kind!r}")


def build_mkdir_command(workdir: str) -> str:
    """Create *workdir* (and parents), mode 700, refusing odd arguments."""
    quoted = shell_quote(workdir)
    return f"mkdir -p -m 700 -- {quoted}"


def build_submit_command(workdir: str, kind: str) -> str:
    """The one remote command that starts the job. Pure.

    nohup waits up to 10 s for ``_omicsclaw_pgid`` to appear — the
    launcher writes it as its first act, so the wait is almost always
    one 0.1 s tick — and prints it; ``0`` means "no pgid was recorded",
    which the caller treats as a failed submission rather than guessing.

    The ``cd`` is joined with ``;`` and its own ``|| exit 1``, **not**
    with ``&&`` before the background launch: in shell grammar
    ``cd X && launch & wait-loop`` puts the *whole* ``cd && launch``
    list in the background, and the wait-loop then reads ``./_pgid``
    relative to the home directory — the exact bug the first version of
    this command shipped, caught by the real-SSH integration test after
    every fake-based test passed it. ``A; B & C`` is the shape that
    backgrounds only ``B``.
    """
    if kind == KIND_SLURM:
        return f"cd -- {shell_quote(workdir)} && sbatch --parsable ./job.sh"
    if kind == KIND_NOHUP:
        return (
            f"cd -- {shell_quote(workdir)} || exit 1; "
            "setsid nohup bash ./_omicsclaw_launch.sh >/dev/null 2>&1 </dev/null & "
            "for _ in 1 2 3 4 5 6 7 8 9 10 "
            "11 12 13 14 15 16 17 18 19 20 "
            "21 22 23 24 25 26 27 28 29 30 "
            "31 32 33 34 35 36 37 38 39 40 "
            "41 42 43 44 45 46 47 48 49 50 "
            "51 52 53 54 55 56 57 58 59 60 "
            "61 62 63 64 65 66 67 68 69 70 "
            "71 72 73 74 75 76 77 78 79 80 "
            "81 82 83 84 85 86 87 88 89 90 "
            "91 92 93 94 95 96 97 98 99 100; do "
            f"[ -s ./{_PGID_FILE} ] && break; sleep 0.1; done; "
            f"cat ./{_PGID_FILE} 2>/dev/null || echo 0"
        )
    raise ValueError(f"unknown job kind {kind!r}")


def parse_submit_output(kind: str, output: str) -> int:
    """Read the number a submission printed.

    SLURM's ``--parsable`` prints ``jobid`` or ``jobid;cluster`` on the
    first line. The nohup form prints the pgid as the last integer in
    the output, because the ``for``/``cat`` tail follows any startup
    noise.

    :raises ValueError: no integer where the kind's number should be.
    """
    if kind == KIND_SLURM:
        first = output.strip().splitlines()[0].strip() if output.strip() else ""
        head = first.split(";", 1)[0]
        if head.isdigit():
            return int(head)
        raise ValueError(f"sbatch did not print a job id: {output.strip()!r}")
    if kind == KIND_NOHUP:
        for line in reversed(output.strip().splitlines()):
            token = line.strip()
            if token.isdigit():
                return int(token)
        raise ValueError(f"no pgid was recorded: {output.strip()!r}")
    raise ValueError(f"unknown job kind {kind!r}")


def build_status_command(handle: RemoteJobHandle) -> str:
    """Ask the host what became of the job. Read-only.

    SLURM: the queue first, then accounting — a job that left the queue
    and has no accounting row yet (or a cluster without ``sacct``) reads
    as gone, which the parser maps to ``done``; that is the standard
    poller's trade and it is stated here rather than hidden.

    nohup: ``kill -0`` on the group, then the exit-code file. A group
    that is gone with no file is ``unknown`` — reaped by a rebooting node
    or a cleaned scratch — never silently ``done``.
    """
    quoted = shell_quote(handle.workdir)
    if handle.kind == KIND_SLURM:
        job_id = handle.job_id or ""
        return (
            f"cd -- {quoted} && "
            f"q=$(squeue -h -j {job_id} -o %T 2>/dev/null); "
            "if [ -n \"$q\" ]; then echo \"$q\"; exit 0; fi; "
            f"a=$(sacct -n -X -j {job_id} -o State 2>/dev/null "
            "| head -n 1 | tr -d ' '); "
            "if [ -n \"$a\" ]; then echo \"$a\"; else "
            f"echo {_LOST_MARK}; fi"
        )
    return (
        f"cd -- {quoted} && "
        f"if kill -0 -- -{handle.pgid} 2>/dev/null; then echo {_RUNNING_MARK}; "
        f"elif [ -f ./{_EXIT_FILE} ]; then cat ./{_EXIT_FILE}; "
        f"else echo {_LOST_MARK}; fi"
    )


def parse_status(kind: str, output: str) -> tuple[str, int | None]:
    """Map a status command's output to ``(state, exit_code)``. Pure.

    Unknown scheduler words map to :data:`UNKNOWN` rather than raising:
    a scheduler upgrade that renames a state must not take the poller
    down with it.
    """
    text = output.strip()
    if kind == KIND_SLURM:
        word = text.splitlines()[0].strip() if text else ""
        upper = word.upper().rstrip("+")
        if upper.startswith("PENDING"):
            return PENDING, None
        if upper.startswith(("RUNNING", "COMPLETING", "SUSPENDED", "REQUEUE")):
            return RUNNING, None
        if upper.startswith("CANCELLED"):
            return CANCELED, None
        if upper.startswith(("COMPLETED", "BOOT_FAIL")):
            return DONE, None
        if upper.startswith(("FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "TIMEOUT")):
            return FAILED, None
        if word == _LOST_MARK:
            # Gone from the queue and from accounting: the poller's
            # standard trade reads this as finished (a cluster without
            # sacct would otherwise have every job unknown forever).
            return DONE, None
        return UNKNOWN, None
    if kind == KIND_NOHUP:
        line = text.splitlines()[-1].strip() if text else ""
        if line == _RUNNING_MARK:
            return RUNNING, None
        if line == _LOST_MARK:
            return UNKNOWN, None
        if line.isdigit():
            code = int(line)
            return (DONE if code == 0 else FAILED), code
        return UNKNOWN, None
    return UNKNOWN, None


def build_cancel_command(handle: RemoteJobHandle) -> str:
    """Stop the job the way its kind stops: ``scancel`` or group SIGTERM."""
    if handle.kind == KIND_SLURM:
        return f"scancel {handle.job_id}"
    return f"kill -TERM -- -{handle.pgid}"


def build_size_command(paths: list[str]) -> str:
    """Print one byte size per line for *paths*, in order, or nothing.

    ``stat -c %s`` first (GNU, and what a Linux cluster runs) with
    ``wc -c`` as the fallback for the odd BSD; ``--`` stops a path that
    begins with ``-`` being read as a flag.
    """
    if not paths:
        return "true"
    parts = [
        f"( stat -c %s -- {shell_quote(p)} 2>/dev/null "
        f"|| wc -c < {shell_quote(p)} 2>/dev/null || echo -1 )"
        for p in paths
    ]
    return "; ".join(parts)


def build_log_tail_command(workdir: str, offset: int, limit: int) -> str:
    """Read ``job.log`` from byte *offset*, capped at *limit* bytes."""
    return (
        f"cd -- {shell_quote(workdir)} && "
        f"[ -f ./{_LOG_FILE} ] && tail -c +{max(1, offset + 1)} ./{_LOG_FILE} "
        f"| head -c {max(0, limit)} || true"
    )


LOG_FILE = _LOG_FILE
EXIT_FILE = _EXIT_FILE
PGID_FILE = _PGID_FILE

__all__ = [
    "CANCELED",
    "DONE",
    "FAILED",
    "IN_FLIGHT_STATES",
    "KIND_NOHUP",
    "KIND_SLURM",
    "LOG_FILE",
    "NOHUP_LAUNCHER",
    "NOHUP_WRAPPER",
    "PENDING",
    "RemoteJobHandle",
    "RUNNING",
    "SCHEDULERS",
    "STATES",
    "UNKNOWN",
    "build_cancel_command",
    "build_job_scripts",
    "build_log_tail_command",
    "build_mkdir_command",
    "build_size_command",
    "build_status_command",
    "build_submit_command",
    "detect_scheduler",
    "parse_status",
    "parse_submit_output",
]
