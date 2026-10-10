"""``remote_*`` and ``ask_about_host`` — the remote execution tool family.

C1 of the remote runtime plan: a Claude-Science-shaped execution plane,
where the remote host is asked for nothing but ``sshd`` and a POSIX
shell. The agent gets five tools — ``remote_exec``, ``remote_submit``,
``remote_status``, ``remote_cancel``, ``remote_fetch`` — plus
``ask_about_host``, which puts a question about a host to the person and
writes the answer into the host knowledge base.

**The plane is injected** (:class:`RemotePlane`), for the reason every
environment seam in this package exists: ``omicsclaw/tools/`` is a leaf
layer and may import ``omicsclaw.schema`` and nothing else inside this
namespace, while the SSH spawner, the probe parser and the SQLite stores
live in ``omicsclaw.remote``. The composition root
(:func:`~omicsclaw.entry.assembly.build_app`) binds
``omicsclaw.remote.plane.RemotePlaneBinding`` when
:attr:`~omicsclaw.entry.config.AppConfig.remote_execution` is on; a tool
constructed without a plane refuses at execution time, the same "the
surface binds it" refusal ``save_artifact``'s sink makes, because no
argument the model can send could supply a database or an SSH client.

**Hand-written like ``bash``, and for the same reason**: every tool here
is ``ASK``, and an approval prompt must quote the bytes the model
actually sent — an adapter that decodes and re-encodes cannot. The
approval *reason* names the host and the ``intent`` argument, because
"run a shell command on ``gpu.lab``" and "run a shell command" are
different decisions for the person clicking, and ``intent`` is the
model's own one-line human-language title for the card.

**Host names are validated, commands are not.** A ``host`` that matches
:data:`HOST_NAME` is an ``ssh`` config alias or a DNS name — a closed
alphabet with no space, metacharacter or leading dash, so it cannot
become an option or a second word on any ``ssh`` command line this plane
builds. A ``command`` is deliberately *not* filtered: it is a whole
programming language, the boundary claimed is the approval gate (the
same ruling ``builtin/bash.py`` states for itself), and the command
reaches the process layer as one ``argv`` element through
``exec``-style spawning, so no local shell ever re-parses it. What the
remote shell does with it is exactly what the human approved.

**A non-zero exit is a result, not a failure** — ``remote_exec`` follows
``bash``'s Q6 ruling, and for the same reason: the model's next move
after ``squeue`` answering "invalid job id specified" is to fix the call,
not to look for a broken tool. Timeouts, unreachable hosts and refused
arguments are the tool's own failures and are raised.

**Timeouts and output caps follow ``bash``'s numbers.** Output is cut in
the middle keeping two thirds of the tail (:data:`MAX_OUTPUT_CHARS`
imported rather than re-declared — one ceiling, one place), and the
``remote_exec`` budget is capped at 600 s rather than ``bash``'s 45
because the point of the tool is remote round-trips that don't fit a
local attention span; anything longer than that belongs in
``remote_submit``, which is the whole distinction between the two.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool``,
``omicsclaw.tools.builtin.bash`` (for the truncation constants), and the
standard library.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from omicsclaw.schema import ToolDefinition

from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import (
    AnswerStatus,
    QuestionOption,
    QuestionRequest,
    ask_question,
    require_approval,
)
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments
from .bash import MAX_OUTPUT_CHARS, _truncate

HOST_NAME: re.Pattern[str] = re.compile(r"\A[A-Za-z0-9._-]+\Z")
"""The closed alphabet a ``host`` argument must match.

SSH config aliases, DNS names and dotted quads all fit; a space, a
semicolon, a ``$(...)`` or a leading ``-`` does not, which is what makes
the value safe to splice into an ``ssh`` command line as a single word.
A leading dash is refused even though the pattern's alphabet contains
``-``, because a host that begins with one would be parsed as an option
by anything that forgot the ``--`` separator; both halves of that defence
are tested, here and in ``omicsclaw.remote.ssh``.
"""

MAX_EXEC_TIMEOUT_S = 600.0
"""Ceiling on one ``remote_exec`` call. The plan's number, and the whole
point of the split: past ten minutes the work is a job, and the tool the
model should be holding is ``remote_submit``."""

DEFAULT_EXEC_TIMEOUT_S = 120.0
DEFAULT_SUBMIT_TIMEOUT_S = 1800.0
DEFAULT_FETCH_MAX_MB = 100.0
"""The fetch threshold, from the plan: results larger than this stay on
the remote host as a ``remote://`` reference rather than travelling."""

MAX_INPUT_ITEMS = 64
"""How many input files one ``remote_submit`` may declare. The plane
enforces a byte ceiling as well (:data:`MAX_INPUT_FILE_BYTES` there);
this one bounds the batch's *shape* — a model that names a thousand
files has confused the tool for a dataset transfer."""

INPUTS_SUMMARY_ITEMS = 8
"""How many ``src -> dst`` lines the approval card lists before folding
the rest into a count. The card has to be readable by a person deciding
in seconds; eight lines is what fits that decision, and the fold says
the rest without hiding that there is a rest."""

SCHEDULERS = ("auto", "slurm", "none")


def valid_host(host: str) -> bool:
    """Whether *host* is a single closed-alphabet word, safe to splice."""
    return bool(host) and bool(HOST_NAME.match(host)) and not host.startswith("-")


# ---- what the injected plane answers --------------------------------------


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """One finished ``remote_exec`` call.

    ``exit_code`` follows the shell convention (``0`` success,
    ``128 + signum`` for a signal) so it renders the way ``bash``'s does.
    """

    output: str = ""
    exit_code: int = 0
    timed_out: bool = False
    host: str = ""


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """One submitted job: its reference, and where it landed."""

    job_ref: str = ""
    kind: str = "nohup"
    job_id: str | None = None
    pgid: int | None = None
    workdir: str = ""
    host: str = ""
    submitted_at: float = 0.0


@dataclass(frozen=True, slots=True)
class StatusReport:
    """A job's state as the plane re-estimated it.

    ``state`` is one of ``pending``, ``running``, ``done``, ``failed``,
    ``canceled``, ``unknown``. ``exit_code`` is the remote exit status
    when it is known (``nohup`` jobs record it; SLURM's ``sacct`` maps
    states rather than codes). ``detail`` is what the scheduler said.
    """

    state: str = "unknown"
    exit_code: int | None = None
    detail: str = ""
    job_ref: str = ""


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What a fetch brought back, or deliberately left behind.

    Exactly one of ``downloaded`` (local paths, in order) and
    ``remote_ref`` (a ``remote://<host>/<path>`` reference) is
    meaningful; ``skipped_reason`` says why nothing travelled when
    ``remote_ref`` is set.
    """

    downloaded: tuple[str, ...] = ()
    remote_ref: str | None = None
    skipped_reason: str = ""
    bytes_total: int = 0


@dataclass(frozen=True, slots=True)
class HostCard:
    """The resource card a host's probe and notes add up to."""

    alias: str = ""
    hostname: str = ""
    platform: str = ""
    nproc: int | None = None
    memory_mb: int | None = None
    gpus: tuple[str, ...] = ()
    commands: dict[str, str | None] = field(default_factory=dict)
    slurm_partitions: tuple[str, ...] = ()
    scratch_root: str = ""
    last_probed_at: float | None = None
    notes: tuple[dict[str, Any], ...] = ()
    parse_error: str = ""


@runtime_checkable
class RemotePlane(Protocol):
    """Where the remote tool family's work actually happens. Injected.

    One object, five operations and two knowledge-base reads, narrowed
    the way :class:`~omicsclaw.tools.builtin.bash.BashEnvironment` is:
    Protocols are structural, so ``omicsclaw.remote.plane.RemotePlaneBinding``
    satisfies this without importing it, and a test double is a dict of
    canned answers rather than an SSH client.

    Obligations an implementation carries:

    * ``host`` arguments arrive already validated by the tools; an
      implementation that re-checks with :func:`valid_host` costs nothing.
    * A host that cannot be reached raises; ``status`` and ``cancel``
      say ``unknown`` in words instead, because an unreachable host is a
      fact about the world the model can act on, not a broken tool.
    * ``job_ref`` is the plane's own opaque reference (the row id of the
      persisted handle); ``fetch``'s ``source`` is a ``job_ref`` or a
      ``remote://<host>/<path>`` URL.
    """

    async def exec(self, host: str, command: str, timeout: float) -> ExecOutcome:
        """Run *command* on *host* and return what it printed."""
        ...

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
        """Place *command* on *host* as a job; return its handle.

        ``inputs`` are ``(src, dst)`` pairs: a file inside the local
        workspace, and the name it lands under in the job's remote work
        directory. An implementation refuses a ``src`` outside the
        workspace and a ``dst`` that would climb out of the work
        directory — both before anything is uploaded.
        """
        ...

    async def status(self, job_ref: str) -> StatusReport:
        """Re-estimate one job's state from the remote side."""
        ...

    async def cancel(self, job_ref: str) -> StatusReport:
        """Ask the remote side to stop one job; report what it said."""
        ...

    async def fetch(
        self, source: str, dest: str, max_mb: float = DEFAULT_FETCH_MAX_MB
    ) -> FetchOutcome:
        """Bring outputs back, or decline past *max_mb* with a reference."""
        ...

    def host_card(self, alias: str) -> HostCard | None:
        """The knowledge base's card for *alias*, probing if absent."""
        ...

    def note_answer(self, alias: str, question: str, answer: str) -> None:
        """Write a person's answer about *alias* into the knowledge base."""
        ...


# ---- shared argument plumbing ----------------------------------------------


def _decoded(arguments: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Decode and validate against *schema*, or raise the model-facing form."""
    decoded = decode_arguments(arguments)
    issues = validate_arguments(decoded, schema)
    if issues:
        listed = "\n".join(f"  - {issue}" for issue in issues)
        raise ToolArgumentError(
            "the arguments do not match this tool's schema:\n"
            f"{listed}\nRe-send the call with all of these corrected."
        )
    return decoded


def _host_argument(decoded: dict[str, Any]) -> str:
    """The ``host`` field, validated. The injection boundary, in one place."""
    host = str(decoded.get("host") or decoded.get("host_alias") or "")
    if not host:
        raise ToolArgumentError(
            "input.host is required: the ssh config alias or DNS name of the "
            "remote host, for example 'gpu-lab'"
        )
    if not valid_host(host):
        raise ToolArgumentError(
            f"input.host must be one word of letters, digits, dots, dashes "
            f"and underscores, with no leading dash; got {host!r}. It is "
            "spliced into an ssh command line, so anything else is refused"
        )
    return host


def _timeout_argument(
    decoded: dict[str, Any], default: float, ceiling: float
) -> float:
    """The ``timeout_seconds`` field: omitted means *default*, never above."""
    requested = decoded.get("timeout_seconds")
    if requested is None:
        return default
    if not isinstance(requested, (int, float)) or requested <= 0:
        raise ToolArgumentError(
            f"input.timeout_seconds must be 1 or greater; got {requested!r}. "
            f"Omit it to use the {default:g}s default"
        )
    return min(float(requested), ceiling)


_POLICY = ToolPolicy(
    risk_level=RiskLevel.HIGH,
    approval_mode=ApprovalMode.ASK,
    prompts_for_itself=True,
    read_only=False,
    concurrency_safe=False,
    writes_workspace=False,
    touches_network=True,
    allowed_in_background=False,
    tags=frozenset({"remote", "ssh", "mutation"}),
)
"""``ASK`` and ``HIGH``, declared rather than defaulted — the same ruling
``builtin/bash.py`` makes. The blast radius is deliberately stated as
worse than a local shell: a command on a remote host runs with the
user's credentials *there*, against data this machine may not even hold,
and the person approving can only see the text. ``touches_network=True``
so an offline deployment can refuse the family on a claim rather than a
hunch."""


def _inputs_summary(inputs: tuple[tuple[str, str], ...]) -> str:
    """The files a submit will carry out, as the approval card shows them.

    One ``src -> dst`` line each, at most :data:`INPUTS_SUMMARY_ITEMS`
    of them, the rest folded into a count — a person approving in
    seconds sees what leaves the machine without reading a directory
    listing. The whole block is clamped to :data:`MAX_OUTPUT_CHARS`
    with the head kept: the first lines are the file names, which is
    the part worth reading, and a cut tail loses only the fold count.
    """
    if not inputs:
        return ""
    lines = [
        f"  {src} -> {dst}" for src, dst in inputs[:INPUTS_SUMMARY_ITEMS]
    ]
    hidden = len(inputs) - INPUTS_SUMMARY_ITEMS
    if hidden > 0:
        lines.append(f"  …and {hidden} more")
    block = "Files travelling to the host:\n" + "\n".join(lines)
    if len(block) > MAX_OUTPUT_CHARS:
        block = block[: MAX_OUTPUT_CHARS - 1] + "…"
    return block


def _remote_reason(
    intent: str,
    host: str,
    command: str,
    timeout: float,
    *,
    inputs: tuple[tuple[str, str], ...] = (),
) -> str:
    """What the human is told they are approving.

    The **whole** command, the host by name, and the model's own
    ``intent`` title — the three things a person needs to decide, and
    the reason ``intent`` exists as an argument at all. A submit adds
    the fourth: the files that will leave this machine, because "run a
    command" and "run a command and ship my data there" are different
    decisions even when the command is identical.
    """
    title = intent.strip() or "unnamed remote command"
    reason = (
        f"{title}: run a shell command over SSH on {host}, with a "
        f"{timeout:g}s limit, as the remote user with their credentials. "
        "It can read, change or delete anything that user can reach there:\n"
        f"{command}"
    )
    summary = _inputs_summary(inputs)
    if summary:
        reason += "\n" + summary
    return reason


# ---- remote_exec -----------------------------------------------------------


class RemoteExecTool:
    """Run one bounded shell command on a remote host and read it back."""

    policy = _POLICY

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="remote_exec",
            description=(
                "Run a shell command on a remote host over SSH and read back "
                "stdout and stderr merged, the way the bash tool does locally. "
                "For short, bounded checks only — inspecting a path, loading a "
                "module, querying the scheduler queue. Anything that should "
                "keep running after this call returns belongs in "
                "remote_submit instead: this tool kills its own SSH client at "
                "the timeout and whatever the remote side started may "
                "outlive it. A NON-ZERO EXIT STATUS is the command's own "
                f"result, not a failure of this tool. Output past "
                f"{MAX_OUTPUT_CHARS} characters is cut in the middle like "
                "bash's. Authentication is key- or agent-based only; there "
                "is no password form."
            ),
            input_schema=copy.deepcopy(REMOTE_EXEC_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "remote_exec"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, REMOTE_EXEC_SCHEMA)
        host = _host_argument(decoded)
        command = str(decoded.get("command") or "")
        if not command.strip():
            raise ToolArgumentError(
                "input.command is required and must be a command to run; an "
                "empty string runs nothing"
            )
        intent = str(decoded.get("intent") or "")
        timeout = _timeout_argument(decoded, DEFAULT_EXEC_TIMEOUT_S, MAX_EXEC_TIMEOUT_S)

        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=_remote_reason(intent, host, command, timeout),
            reason_shows_call=True,
        )
        outcome = await self._plane.exec(host, command, timeout)
        if outcome.timed_out:
            return (
                _truncate(outcome.output)
                + f"\n\n[TIMEOUT {timeout:g}s: the SSH call was killed for "
                "running past its time limit. The remote command may still "
                "be running — check with remote_status or remote_submit "
                "next time.]"
            ).strip()
        if outcome.exit_code != 0:
            body = _truncate(outcome.output)
            if not body:
                return (
                    f"[exit status {outcome.exit_code}] The remote command "
                    "failed and printed nothing."
                )
            return f"[exit status {outcome.exit_code}]\n{body}"
        if not outcome.output.strip():
            return "The remote command finished successfully with no output."
        return _truncate(outcome.output)


REMOTE_EXEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": (
                "SSH config alias or DNS name of the host to run on, e.g. "
                "'gpu-lab'. One word of letters, digits, dots, dashes, "
                "underscores. Required."
            ),
        },
        "command": {
            "type": "string",
            "description": "The shell command to run there. Required.",
        },
        "intent": {
            "type": "string",
            "description": (
                "One plain-language line saying what this command is for — "
                "it becomes the title of the approval card the user reads. "
                "Required."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": (
                f"Optional seconds budget, at most {MAX_EXEC_TIMEOUT_S:g} "
                f"(default {DEFAULT_EXEC_TIMEOUT_S:g}). Longer work belongs "
                "in remote_submit."
            ),
        },
    },
    "required": ["host", "command", "intent"],
    "additionalProperties": False,
}


# ---- remote_submit ---------------------------------------------------------


class RemoteSubmitTool:
    """Place a command on a remote host as a detached job."""

    policy = _POLICY

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="remote_submit",
            description=(
                "Submit a command as a long-running job on a remote host and "
                "return immediately with a job reference. The command runs "
                "from a fresh work directory under the host's scratch; "
                "declare input files as {src, dst} pairs — src inside the "
                "session workspace, uploaded to dst inside that work "
                "directory before the job starts — and name outputs so they "
                "can be fetched back. If the command contains #SBATCH lines "
                "it is submitted with sbatch; otherwise it runs under "
                "setsid+nohup with its process group recorded. Track it with "
                "remote_status, stop it with remote_cancel, and bring "
                "outputs back with remote_fetch."
            ),
            input_schema=copy.deepcopy(REMOTE_SUBMIT_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "remote_submit"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, REMOTE_SUBMIT_SCHEMA)
        host = _host_argument(decoded)
        command = str(decoded.get("command") or "")
        if not command.strip():
            raise ToolArgumentError(
                "input.command is required and must be the job's script or "
                "command line; an empty string submits nothing"
            )
        intent = str(decoded.get("intent") or "")
        raw_inputs = decoded.get("inputs", [])
        if not isinstance(raw_inputs, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("src"), str)
            and isinstance(item.get("dst"), str)
            and item["src"].strip()
            and item["dst"].strip()
            for item in raw_inputs
        ):
            raise ToolArgumentError(
                "input.inputs must be a list of {src, dst} objects: src "
                "names a file inside the workspace, dst names where it "
                "lands inside the job's work directory"
            )
        if len(raw_inputs) > MAX_INPUT_ITEMS:
            raise ToolArgumentError(
                f"input.inputs names at most {MAX_INPUT_ITEMS} files "
                f"({len(raw_inputs)} given); stage a directory or an "
                "archive instead of listing a dataset file by file"
            )
        inputs = tuple(
            (str(item["src"]), str(item["dst"])) for item in raw_inputs
        )
        outputs = tuple(str(item) for item in decoded.get("outputs", []))
        scheduler = str(decoded.get("scheduler") or "auto")
        if scheduler not in SCHEDULERS:
            raise ToolArgumentError(
                f"input.scheduler must be one of {', '.join(SCHEDULERS)}; "
                f"got {scheduler!r}"
            )
        timeout = _timeout_argument(
            decoded, DEFAULT_SUBMIT_TIMEOUT_S, DEFAULT_SUBMIT_TIMEOUT_S
        )

        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=_remote_reason(
                intent, host, command, timeout, inputs=inputs
            ),
            reason_shows_call=True,
        )
        outcome = await self._plane.submit(
            host,
            command,
            inputs=inputs,
            outputs=outputs,
            scheduler=scheduler,
            timeout=timeout,
            label=intent,
        )
        identity = outcome.job_id if outcome.kind == "slurm" else outcome.pgid
        return (
            f"Submitted on {outcome.host} as a {outcome.kind} job "
            f"({identity}), job_ref {outcome.job_ref}.\n"
            f"Work directory: {outcome.workdir}\n"
            f"Ask remote_status with job_ref {outcome.job_ref} to track it, "
            f"remote_cancel to stop it, remote_fetch to bring "
            f"'{' '.join(outputs) if outputs else 'outputs'}' back."
        )


REMOTE_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": "SSH alias or DNS name of the host to submit on. Required.",
        },
        "command": {
            "type": "string",
            "description": (
                "The job script or command. Lines beginning with #SBATCH "
                "are honoured: the whole command is then submitted to "
                "sbatch. Required."
            ),
        },
        "intent": {
            "type": "string",
            "description": "One-line human title for the approval card. Required.",
        },
        "inputs": {
            "type": "array",
            "maxItems": MAX_INPUT_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "src": {
                        "type": "string",
                        "description": (
                            "File to upload, inside the session workspace "
                            "and relative to its root (e.g. 'data/counts.csv')."
                        ),
                    },
                    "dst": {
                        "type": "string",
                        "description": (
                            "Name it lands under in the job's work "
                            "directory, relative to that directory (e.g. "
                            "'counts.csv'); it may not climb out of it."
                        ),
                    },
                },
                "required": ["src", "dst"],
                "additionalProperties": False,
            },
            "description": (
                "Workspace files to upload into the job's work directory "
                "before it starts, as {src, dst} pairs: src inside the "
                "workspace, dst inside the work directory. At most "
                f"{MAX_INPUT_ITEMS} files; a directory or archive is the "
                "shape for anything bigger."
            ),
        },
        "outputs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "File names (relative to the work directory) the job is "
                "expected to produce, for remote_fetch to bring back."
            ),
        },
        "scheduler": {
            "type": "string",
            "enum": list(SCHEDULERS),
            "description": (
                "'auto' (default) submits with sbatch when the command "
                "carries #SBATCH lines, otherwise setsid+nohup; 'slurm' and "
                "'none' force one path."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": (
                "Seconds the submission itself may take (default "
                f"{DEFAULT_SUBMIT_TIMEOUT_S:g}); the job, once submitted, "
                "runs until it ends or is cancelled."
            ),
        },
    },
    "required": ["host", "command", "intent"],
    "additionalProperties": False,
}


# ---- remote_status / remote_cancel -----------------------------------------


class RemoteStatusTool:
    """Re-estimate one remote job's state."""

    policy = ToolPolicy(
        risk_level=RiskLevel.MEDIUM,
        approval_mode=ApprovalMode.ASK,
        prompts_for_itself=True,
        read_only=True,
        concurrency_safe=True,
        touches_network=True,
        tags=frozenset({"remote", "ssh"}),
    )

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="remote_status",
            description=(
                "Report the current state of a remote job: pending, "
                "running, done, failed, canceled or unknown. unknown means "
                "the host could not be asked — the job is still recorded, "
                "try again later. Pass the job_ref remote_submit returned."
            ),
            input_schema=copy.deepcopy(REMOTE_REF_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "remote_status"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, REMOTE_REF_SCHEMA)
        job_ref = str(decoded.get("job_ref") or "")
        if not job_ref.strip():
            raise ToolArgumentError(
                "input.job_ref is required: the reference remote_submit "
                "returned"
            )
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=f"ask the remote host what became of job {job_ref} "
            "(read-only: squeue/sacct or a kill -0 probe)",
            reason_shows_call=True,
        )
        report = await self._plane.status(job_ref)
        lines = [f"job {job_ref}: {report.state}"]
        if report.exit_code is not None:
            lines.append(f"remote exit code: {report.exit_code}")
        if report.detail:
            lines.append(f"detail: {report.detail}")
        return "\n".join(lines)


class RemoteCancelTool:
    """Stop one remote job."""

    policy = _POLICY

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="remote_cancel",
            description=(
                "Stop a remote job: scancel for a SLURM job, SIGTERM to the "
                "whole process group for a nohup job. The job's record is "
                "kept."
            ),
            input_schema=copy.deepcopy(REMOTE_REF_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "remote_cancel"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, REMOTE_REF_SCHEMA)
        job_ref = str(decoded.get("job_ref") or "")
        if not job_ref.strip():
            raise ToolArgumentError(
                "input.job_ref is required: the reference remote_submit "
                "returned"
            )
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=f"stop remote job {job_ref} on its host (scancel, or "
            "SIGTERM to its process group)",
            reason_shows_call=True,
        )
        report = await self._plane.cancel(job_ref)
        return f"cancel requested for job {job_ref}: {report.state}"


REMOTE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_ref": {
            "type": "string",
            "description": "The job reference remote_submit returned. Required.",
        },
    },
    "required": ["job_ref"],
    "additionalProperties": False,
}


# ---- remote_fetch ----------------------------------------------------------


class RemoteFetchTool:
    """Bring remote outputs back, or leave them behind past the threshold."""

    policy = ToolPolicy(
        risk_level=RiskLevel.MEDIUM,
        approval_mode=ApprovalMode.ASK,
        prompts_for_itself=True,
        read_only=False,
        concurrency_safe=False,
        writes_workspace=True,
        touches_network=True,
        tags=frozenset({"remote", "sftp", "mutation"}),
    )

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="remote_fetch",
            description=(
                "Copy a remote job's outputs (or one remote path) into the "
                "workspace over SFTP. Files larger than max_mb megabytes "
                f"(default {DEFAULT_FETCH_MAX_MB:g}) are NOT copied: the "
                "tool returns a remote://<host>/<path> reference instead, "
                "and the data stays where it is."
            ),
            input_schema=copy.deepcopy(REMOTE_FETCH_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "remote_fetch"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, REMOTE_FETCH_SCHEMA)
        source = str(decoded.get("source") or "")
        if not source.strip():
            raise ToolArgumentError(
                "input.source is required: a job_ref from remote_submit, or "
                "a remote://<host>/<path> URL"
            )
        dest = str(decoded.get("dest") or "").strip() or "artifacts/remote"
        max_mb = decoded.get("max_mb", DEFAULT_FETCH_MAX_MB)
        if not isinstance(max_mb, (int, float)) or max_mb <= 0:
            raise ToolArgumentError(
                f"input.max_mb must be a positive number of megabytes; got "
                f"{max_mb!r}"
            )
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=f"download remote results from {source} into the "
            f"workspace at {dest} (SFTP, capped at {float(max_mb):g} MB per "
            "file; larger files stay on the remote host)",
            reason_shows_call=True,
        )
        outcome = await self._plane.fetch(source, dest, float(max_mb))
        if outcome.remote_ref:
            return (
                f"Left on the remote host — {outcome.skipped_reason}. "
                f"Reference: {outcome.remote_ref}\n"
                "The data has not travelled; use it where it is, or raise "
                "max_mb deliberately if the user wants it local."
            )
        if not outcome.downloaded:
            return "Nothing to fetch: no outputs found for that reference."
        listed = "\n".join(f"  {path}" for path in outcome.downloaded)
        return f"Downloaded {len(outcome.downloaded)} file(s):\n{listed}"


REMOTE_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "What to fetch: the job_ref remote_submit returned (fetches "
                "that job's declared outputs), or a remote://<host>/<path> "
                "URL naming one file. Required."
            ),
        },
        "dest": {
            "type": "string",
            "description": (
                "Directory to receive the files, relative to the session "
                "workspace root (an absolute path must already be inside "
                "it); anything resolving outside the workspace is "
                "refused. Defaults to artifacts/remote."
            ),
        },
        "max_mb": {
            "type": "number",
            "description": (
                f"Per-file size ceiling in megabytes; default "
                f"{DEFAULT_FETCH_MAX_MB:g}. Larger files are refused and "
                "referenced remotely."
            ),
        },
    },
    "required": ["source"],
    "additionalProperties": False,
}


# ---- ask_about_host --------------------------------------------------------


class AskAboutHostTool:
    """Ask the person something about a host; keep the answer with the host.

    The bridge rather than a duplicate of ``ask_user``: the question goes
    out through the same
    :func:`~omicsclaw.tools.context.ask_question` channel, and the answer
    is additionally written to the host knowledge base so the next probe
    card — and the next session — starts from it. The Claude-Science
    ``ask_about_compute`` pattern.
    """

    policy = ToolPolicy(
        risk_level=RiskLevel.LOW,
        approval_mode=ApprovalMode.AUTO,
        read_only=False,
        writes_config=True,
        concurrency_safe=True,
        tags=frozenset({"remote", "knowledge"}),
    )

    def __init__(self, plane: RemotePlane) -> None:
        self._plane = plane
        self._definition = ToolDefinition(
            name="ask_about_host",
            description=(
                "Ask the user a question about a remote host — which SLURM "
                "partition to use, which account, how modules are "
                "activated — when the probe cannot tell. The answer is "
                "stored with the host and shown on its card from then on."
            ),
            input_schema=copy.deepcopy(ASK_ABOUT_HOST_SCHEMA),
        )

    @property
    def name(self) -> str:
        return "ask_about_host"

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        decoded = _decoded(arguments, ASK_ABOUT_HOST_SCHEMA)
        alias = _host_argument(decoded)
        question = str(decoded.get("question") or "")
        if not question.strip():
            raise ToolArgumentError(
                "input.question is required: say what you want to know "
                "about the host"
            )
        card = self._plane.host_card(alias)
        context = ""
        if card is not None:
            bits = [f"hostname {card.hostname}"] if card.hostname else []
            if card.slurm_partitions:
                bits.append("partitions " + ", ".join(card.slurm_partitions))
            if bits:
                context = "(known: " + "; ".join(bits) + ")"
        request = QuestionRequest(
            question=f"About the remote host '{alias}' {context}: {question}",
            options=(
                QuestionOption(label="Skip this question"),
            ),
        )
        answer = await ask_question(request)
        if answer.status is not AnswerStatus.ANSWERED:
            return (
                f"No answer about {alias} ({answer.status.value}); proceed "
                "with what the probe knows and say so in the step."
            )
        text = answer.reply or ", ".join(answer.selected)
        if not text:
            return (
                f"The user answered without text about {alias}; nothing was "
                "recorded."
            )
        self._plane.note_answer(alias, question, text)
        return f"Recorded about {alias}: {text}"


ASK_ABOUT_HOST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host_alias": {
            "type": "string",
            "description": "The host the question is about. Required.",
        },
        "question": {
            "type": "string",
            "description": "What to ask the user about that host. Required.",
        },
    },
    "required": ["host_alias", "question"],
    "additionalProperties": False,
}


def remote_tools(plane: RemotePlane) -> tuple[Any, ...]:
    """The family in mount order: exec, submit, status, cancel, fetch, ask.

    Ordered so the two the prompt's scheduling rules name come first; the
    list is appended at the end of a registry, so within it the order is
    convention rather than a cache-stability promise.
    """
    return (
        RemoteExecTool(plane),
        RemoteSubmitTool(plane),
        RemoteStatusTool(plane),
        RemoteCancelTool(plane),
        RemoteFetchTool(plane),
        AskAboutHostTool(plane),
    )


__all__ = [
    "DEFAULT_EXEC_TIMEOUT_S",
    "DEFAULT_FETCH_MAX_MB",
    "DEFAULT_SUBMIT_TIMEOUT_S",
    "ExecOutcome",
    "FetchOutcome",
    "HOST_NAME",
    "HostCard",
    "MAX_EXEC_TIMEOUT_S",
    "RemoteCancelTool",
    "RemoteExecTool",
    "RemoteFetchTool",
    "RemotePlane",
    "RemoteStatusTool",
    "RemoteSubmitTool",
    "SCHEDULERS",
    "StatusReport",
    "SubmitOutcome",
    "AskAboutHostTool",
    "remote_tools",
    "valid_host",
]
