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

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
    "umask 077\nset -f\n"
    "uid=$(id -u); host=$(hostname); boot=$(cat /proc/sys/kernel/random/boot_id)\n"
    "nonce=$(cat ./_omicsclaw_nonce) || exit 1\n"
    "case $host in (*[!A-Za-z0-9._-]*|'') exit 1;; esac\n"
    "proc=$(cat /proc/$$/stat) || exit 1\nproc=${proc##*) }; set -- $proc\n"
    'printf \'{"uid":%s,"host_id":"%s","boot_id":"%s","pid":%s,"start_ticks":%s,"pgid":%s,"sid":%s,"nonce":"%s"}\\n\' '
    '"$uid" "$host" "$boot" "$$" "${20}" "$3" "$4" "$nonce" > ./_omicsclaw_process.json.tmp || exit 1\n'
    "mv -- ./_omicsclaw_process.json.tmp ./_omicsclaw_process.json || exit 1\n"
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
    "cancel_requested=0\ntrap 'cancel_requested=1' TERM INT\n"
    "bash ./cmd.sh &\nchild=$!\n"
    'while true; do\nwait "$child"; status=$?\n'
    'if [ "$cancel_requested" = 1 ] && kill -0 "$child" 2>/dev/null; then sleep 0.05; continue; fi\n'
    "break\ndone\n"
    'if [ "$cancel_requested" = 1 ]; then '
    f"printf '%s' '__OMICSCLAW_CANCELED__' > ./{_EXIT_FILE}; exit 143; fi\n"
    f"printf '%s' \"$status\" > ./{_EXIT_FILE}\n"
    'exit "$status"\n'
)
"""Record the command's observed exit. A signal request becomes terminal
only after the child exits; acknowledging the signal does not end the job."""


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
    target_alias: str = ""
    target_workdir: str = ""
    capabilities: tuple[str, ...] = ()
    process_identity: Mapping[str, object] | None = None
    request_id: str = ""
    payload_hash: str = ""
    cluster: str = ""
    scheduler_identity: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.target_alias:
            object.__setattr__(self, "target_alias", self.host)
        if not self.target_workdir:
            object.__setattr__(self, "target_workdir", self.workdir)
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if self.scheduler_identity is not None:
            identity = dict(self.scheduler_identity)
            if (
                self.kind != KIND_SLURM
                or self.process_identity is not None
                or set(identity)
                != {"uid", "user", "cluster", "job_id", "submit_time", "nonce"}
                or type(identity["uid"]) is not int
                or identity["uid"] < 0
                or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", str(identity[key]))
                    for key in ("user", "cluster")
                )
                or not re.fullmatch(r"[1-9][0-9]*", str(identity["job_id"]))
                or identity["job_id"] != self.job_id
                or identity["cluster"] != self.cluster
                or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
                    str(identity["submit_time"]),
                )
                or not re.fullmatch(r"[a-f0-9]{32}", str(identity["nonce"]))
            ):
                raise ValueError("invalid_scheduler_identity")
            object.__setattr__(self, "scheduler_identity", MappingProxyType(identity))
        if self.process_identity is not None:
            identity = dict(self.process_identity)
            integers = ("uid", "pid", "start_ticks", "pgid", "sid")
            if (
                set(identity) != {*integers, "host_id", "boot_id", "nonce"}
                or any(type(identity[key]) is not int for key in integers)
                or identity["uid"] < 0
                or any(identity[key] <= 0 for key in integers[1:])
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(identity["host_id"]))
                or not re.fullmatch(r"[0-9a-f-]{32,36}", str(identity["boot_id"]))
                or not re.fullmatch(r"[0-9a-f]{32}", str(identity["nonce"]))
                or self.pgid is not None
                and identity["pgid"] != self.pgid
            ):
                raise ValueError("invalid_process_identity")
            object.__setattr__(self, "process_identity", MappingProxyType(identity))

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
                "target_alias": self.target_alias,
                "target_workdir": self.target_workdir,
                "capabilities": list(self.capabilities),
                "process_identity": dict(self.process_identity)
                if self.process_identity
                else None,
                "request_id": self.request_id,
                "payload_hash": self.payload_hash,
                "cluster": self.cluster,
                "scheduler_identity": dict(self.scheduler_identity)
                if self.scheduler_identity
                else None,
            },
            ensure_ascii=False,
        )

    def proof_digest(self) -> str:
        identity = (
            self.process_identity
            if self.kind == KIND_NOHUP
            else self.scheduler_identity
        )
        if identity is None:
            return ""
        return hashlib.sha256(
            json.dumps(
                {
                    "kind": self.kind,
                    "host": self.host,
                    "workdir": self.workdir,
                    "request_id": self.request_id,
                    "payload_hash": self.payload_hash,
                    "identity": dict(identity),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> RemoteJobHandle:
        decoded = json.loads(text) if text.strip() else {}
        return cls(
            kind=str(decoded.get("kind") or KIND_NOHUP),
            host=str(decoded.get("host") or ""),
            workdir=str(decoded.get("workdir") or ""),
            job_id=decoded.get("job_id"),
            pgid=decoded.get("pgid"),
            submitted_at=float(decoded.get("submitted_at") or 0.0),
            local_job_id=str(decoded.get("local_job_id") or ""),
            target_alias=str(decoded.get("target_alias") or ""),
            target_workdir=str(decoded.get("target_workdir") or ""),
            capabilities=tuple(
                str(item) for item in decoded.get("capabilities", ()) if item
            ),
            process_identity=decoded.get("process_identity"),
            request_id=str(decoded.get("request_id") or ""),
            payload_hash=str(decoded.get("payload_hash") or ""),
            cluster=str(decoded.get("cluster") or ""),
            scheduler_identity=decoded.get("scheduler_identity"),
        )

    def identity(self) -> str:
        """The host-side number, whichever kind this is."""
        return self.job_id if self.kind == KIND_SLURM else str(self.pgid)


# Stable scheduler-neutral wire name.
JobHandle = RemoteJobHandle


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
        line.lstrip().startswith(SBATCH_DIRECTIVE) for line in command.splitlines()
    )
    return KIND_SLURM if promoted else KIND_NOHUP


def validate_hard_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if (
        type(value) not in {float, int}
        or not math.isfinite(value)
        or not 0 < value <= 604800
    ):
        raise ValueError("invalid_hard_timeout")
    return float(value)


def build_job_scripts(
    command: str,
    kind: str,
    *,
    hard_timeout_s: float | None = None,
    deadline_python: str = "",
) -> dict[str, str]:
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
        script = (
            command if command.startswith("#!") else ("#!/usr/bin/env bash\n" + command)
        )
        return {"job.sh": script if script.endswith("\n") else script + "\n"}
    if kind == KIND_NOHUP:
        cmd = (
            command if command.startswith("#!") else ("#!/usr/bin/env bash\n" + command)
        )
        scripts = {
            "cmd.sh": cmd if cmd.endswith("\n") else cmd + "\n",
            "job.sh": NOHUP_WRAPPER,
            "_omicsclaw_launch.sh": NOHUP_LAUNCHER,
        }
        if hard_timeout_s is not None:
            from pathlib import Path

            seconds = validate_hard_timeout(hard_timeout_s)
            if not deadline_python.startswith("/") or any(
                char in deadline_python for char in "\x00\r\n"
            ):
                raise ValueError("deadline_interpreter_required")
            scripts["_omicsclaw_deadline.py"] = (
                Path(__file__).with_name("deadline.py").read_text()
            )
            scripts["job.sh"] = (
                "#!/bin/bash\nexec "
                + shell_quote(deadline_python)
                + " -I ./_omicsclaw_deadline.py "
                + str(seconds)
                + "\n"
            )
        return scripts
    raise ValueError(f"unknown job kind {kind!r}")


def build_mkdir_command(workdir: str) -> str:
    """Create *workdir* (and parents), mode 700, refusing odd arguments."""
    quoted = shell_quote(workdir)
    return f"mkdir -p -m 700 -- {quoted}"


def build_submit_command(
    workdir: str, kind: str, *, hard_timeout_s: float | None = None
) -> str:
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
        minutes = (
            math.ceil(validate_hard_timeout(hard_timeout_s) / 60)
            if hard_timeout_s is not None
            else None
        )
        time_limit = f" --time={minutes}" if minutes is not None else ""
        return (
            f"cd -- {shell_quote(workdir)} || exit 1; "
            "_nonce=$(head -c 64 ./_omicsclaw_nonce) && "
            "printf '%s' \"$_nonce\" | grep -Eq '^[a-f0-9]{32}$' || exit 1; "
            "if mkdir ./_omicsclaw_launch_claim 2>/dev/null; then "
            f"sbatch --parsable --job-name=omicsclaw_$_nonce --comment=omicsclaw_$_nonce{time_limit} ./job.sh "
            "> ./_omicsclaw_scheduler_ack.tmp && "
            "mv -- ./_omicsclaw_scheduler_ack.tmp ./_omicsclaw_scheduler_ack && "
            "cat ./_omicsclaw_scheduler_ack; "
            "elif [ -f ./_omicsclaw_scheduler_ack ]; then head -c 4096 ./_omicsclaw_scheduler_ack; "
            "else printf '%s' 0; exit 1; fi"
        )
    if kind == KIND_NOHUP:
        return (
            f"cd -- {shell_quote(workdir)} || exit 1; "
            "if mkdir ./_omicsclaw_launch_claim 2>/dev/null; then "
            "setsid nohup bash ./_omicsclaw_launch.sh >/dev/null 2>&1 </dev/null & "
            "fi; "
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
    as unknown and remains eligible for reconciliation.

    nohup: ``kill -0`` on the group, then the exit-code file. A group
    that is gone with no file is ``unknown`` — reaped by a rebooting node
    or a cleaned scratch — never silently ``done``.
    """
    quoted = shell_quote(handle.workdir)
    if handle.kind == KIND_SLURM:
        if handle.scheduler_identity is None:
            return f"printf '%s\\n' {_LOST_MARK}"
        from .scheduler import probe_command

        return probe_command(handle.job_id, handle.cluster)
    pgid = _nohup_pgid(handle)
    if handle.process_identity is None:
        return f"printf '%s\\n' {_LOST_MARK}"
    # btime has one-second precision; ticks come from the verified birth.
    start = (
        "_hz=$(getconf CLK_TCK) && "
        f'awk -v ticks={handle.process_identity["start_ticks"]} -v hz="$_hz" '
        + shell_quote(
            '$1 == "btime" && hz > 0 {printf "__OMICSCLAW_STARTED_AT__=%.6f\\n", $2 + ticks / hz}'
        )
        + " /proc/stat; "
    )
    return (
        f"cd -- {quoted} && "
        f"if {_process_identity_guard(handle)} && kill -0 -- -{_nohup_pgid(handle)} 2>/dev/null;"
        f" then {start}echo {_RUNNING_MARK}; "
        f"elif {_job_identity_guard(handle)} "
        f"&& ! kill -0 -- -{pgid} 2>/dev/null "
        f"&& [ -f ./{_EXIT_FILE} ]; then {start}cat ./{_EXIT_FILE}; "
        f"else echo {_LOST_MARK}; fi"
    )


def _process_identity_guard(handle: RemoteJobHandle) -> str:
    """Verify the recorded Linux process birth before status or signaling."""
    proof = handle.process_identity
    if proof is None:
        return "false"
    pid = proof["pid"]
    return (
        _job_identity_guard(handle) + " && "
        f'[ "$(stat -c %u /proc/{pid} 2>/dev/null)" = {proof["uid"]} ] && '
        f"_proc=$(cat /proc/{pid}/stat 2>/dev/null) && "
        "_proc=${_proc##*) } && set -f && set -- $_proc && "
        f'[ "$3" = {proof["pgid"]} ] && [ "$4" = {proof["sid"]} ] && '
        f'[ "${{20}}" = {proof["start_ticks"]} ]'
    )


def _job_identity_guard(handle: RemoteJobHandle) -> str:
    """Bind terminal files to the submitted user's host boot and nonce."""
    proof = handle.process_identity
    if proof is None:
        return "false"
    fields = " && ".join(
        "printf '%s' \"$_proof\" | grep -Eq "
        + shell_quote(
            re.escape(json.dumps(key) + ":" + json.dumps(value, separators=(",", ":")))
            + r"([,}])"
        )
        for key, value in proof.items()
    )
    return (
        f'[ "$(id -u)" = {proof["uid"]} ] && '
        f'[ "$(stat -c %u . 2>/dev/null)" = {proof["uid"]} ] && '
        f'[ "$(hostname)" = {shell_quote(str(proof["host_id"]))} ] && '
        f'[ "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" = {shell_quote(str(proof["boot_id"]))} ] && '
        "[ -f ./_omicsclaw_nonce ] && [ ! -L ./_omicsclaw_nonce ] && "
        f'[ "$(cat ./_omicsclaw_nonce 2>/dev/null)" = {shell_quote(str(proof["nonce"]))} ] && '
        "[ -f ./_omicsclaw_process.json ] && [ ! -L ./_omicsclaw_process.json ] && "
        '[ "$(stat -c %s ./_omicsclaw_process.json)" -le 4096 ] && '
        "_proof=$(head -c 4096 ./_omicsclaw_process.json | tr -d ' \\t\\r\\n') && "
        + fields
    )


def has_identity_proof(handle: RemoteJobHandle) -> bool:
    if handle.kind == KIND_NOHUP:
        return handle.process_identity is not None
    if handle.kind == KIND_SLURM:
        return handle.scheduler_identity is not None
    return False


def verified_state(handle: RemoteJobHandle, report) -> str:
    if (
        not has_identity_proof(handle)
        or getattr(report, "identity_verified", False) is not True
        or getattr(report, "identity_digest", "") != handle.proof_digest()
    ):
        return UNKNOWN
    state = getattr(report, "state", UNKNOWN)
    if (
        state not in STATES
        or state in {DONE, FAILED, CANCELED}
        and type(getattr(report, "exit_code", None)) is not int
    ):
        return UNKNOWN
    return state


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
        if upper.startswith("COMPLETED"):
            return DONE, None
        if upper.startswith(
            ("FAILED", "BOOT_FAIL", "NODE_FAIL", "OUT_OF_MEMORY", "TIMEOUT")
        ):
            return FAILED, None
        if word == _LOST_MARK:
            # Accounting can lag behind the queue; retain the handle until
            # the scheduler provides an explicit terminal status.
            return UNKNOWN, None
        return UNKNOWN, None
    if kind == KIND_NOHUP:
        line = text.splitlines()[-1].strip() if text else ""
        if line == "__OMICSCLAW_CANCELED__":
            return CANCELED, 143
        if re.fullmatch(r"__OMICSCLAW_CANCELED__:(137|143)", line):
            return CANCELED, int(line.rsplit(":", 1)[1])
        if line == _RUNNING_MARK:
            return RUNNING, None
        if line == _LOST_MARK:
            return UNKNOWN, None
        if line.isdigit():
            code = int(line)
            return (DONE if code == 0 else FAILED), code
        return UNKNOWN, None
    return UNKNOWN, None


def _nohup_pgid(handle: RemoteJobHandle) -> int:
    """The nohup handle's pgid, or a refusal. Pure.

    A pgid of zero or less spliced into ``kill -- -{pgid}`` is a
    broadcast (``-1``) or the caller's own group (``0``) on the remote
    host — a corrupt handle row must fail the poll loudly here, before
    any command is built, not aim a signal at everything the user may
    kill. SLURM handles never reach this; the pgid a live submission
    records is always positive (:func:`parse_submit_output` refuses
    anything else)."""
    pgid = handle.pgid
    if not isinstance(pgid, int) or pgid <= 0:
        raise ValueError(
            f"a nohup handle carries no usable pgid ({pgid!r}); refusing"
            " to build a group-signal command from it"
        )
    return pgid


def build_cancel_command(handle: RemoteJobHandle) -> str:
    """Stop the job the way its kind stops: ``scancel`` or group SIGTERM."""
    if handle.kind == KIND_SLURM:
        if handle.scheduler_identity is None:
            return f"printf '%s\\n' {_LOST_MARK}; exit 1"
        from .scheduler import cancel_command

        return cancel_command(handle.scheduler_identity)
    pgid = _nohup_pgid(handle)
    if handle.process_identity is None:
        return f"printf '%s\\n' {_LOST_MARK}; exit 1"
    return (
        f"cd -- {shell_quote(handle.workdir)} && "
        f"{_process_identity_guard(handle)} && kill -TERM -- -{pgid}"
    )


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
    "RUNNING",
    "SCHEDULERS",
    "STATES",
    "UNKNOWN",
    "JobHandle",
    "RemoteJobHandle",
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
