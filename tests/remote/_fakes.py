"""Test doubles shared by the remote-plane tests.

``FakeSpawner`` is the transport seam's double: it records every argv
(and every SFTP batch) and answers from a list of ``(needle, answer)``
rules, the first matching needle winning. ``answer`` is an
:class:`~omicsclaw.tools.builtin.remote.ExecOutcome`, a string to raise
as :exc:`~omicsclaw.remote.ssh.RemoteHostUnreachable`, or a plain string
shorthand for a successful run that printed it.

``FakePlane`` is the tool seam's double: a dict of canned answers with
a call log, standing in for
:class:`~omicsclaw.remote.plane.RemotePlaneBinding` in the tool tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from typing import Any

from omicsclaw.tools.builtin.remote import (
    ExecOutcome,
    FetchOutcome,
    StatusReport,
    SubmitOutcome,
)


def run(coro: Any) -> Any:
    """Drive one coroutine the way this suite's tests do: ``asyncio.run``.

    The repository has no top-level ``async def test_`` anywhere — the
    house pattern is a sync test that runs a coroutine to completion —
    so the remote tests follow it rather than importing an asyncio
    plugin mode the project never configured.
    """
    return asyncio.run(coro)


class FakeSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.rules: list[tuple[str, Any]] = []
        self.claims: dict[str, dict[str, str]] = {}
        self.pgids: dict[str, int] = {}
        self.scheduler_acks: dict[str, str] = {}

    def on(self, needle: str, answer: Any) -> FakeSpawner:
        # Prepended, so a rule a test adds mid-scenario — "now the job
        # has finished" — wins over the fixture's broader rule.
        self.rules.insert(0, (needle, answer))
        return self

    async def spawn(
        self, argv: list[str], *, timeout: float, stdin_text: str | None = None
    ) -> ExecOutcome:
        self.calls.append((list(argv), stdin_text))
        command = "<sftp>" if argv and argv[0] == "sftp" else (argv[-1] if argv else "")
        tokens = shlex.split(command) if command != "<sftp>" else []
        workdir = ""
        if tokens and tokens[0] == "cd":
            workdir = tokens[2] if tokens[1] == "--" else tokens[1]
        for needle, answer in self.rules:
            if needle in command or (stdin_text is not None and needle in stdin_text):
                if needle == "stat -c %s" and "_omicsclaw_process.json" in command:
                    # The size fixture describes output files, not the new
                    # process-proof precondition inside a status command.
                    continue
                if isinstance(answer, type) and issubclass(answer, BaseException):
                    raise answer("fake transport failure")
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, ExecOutcome):
                    return answer
                if (
                    "_omicsclaw_launch_claim" in command
                    and str(answer).strip().isdigit()
                ):
                    self.pgids[workdir] = int(str(answer).strip())
                if "sbatch --parsable" in command:
                    self.scheduler_acks[workdir] = str(answer).strip()
                return ExecOutcome(output=str(answer), exit_code=0)
        if "_omicsclaw_submission_claim/intent.tmp" in command:
            intent = next(
                json.loads(token) for token in tokens if token.startswith('{"kind"')
            )
            observed = workdir in self.claims
            if observed and self.claims[workdir] != intent:
                return ExecOutcome(output="CONFLICT", exit_code=1)
            self.claims[workdir] = intent
            return ExecOutcome(
                output="OBSERVED" if observed else "CLAIMED", exit_code=0
            )
        if "head -c 4096 ./_omicsclaw_scheduler_ack" in command:
            if workdir not in self.claims or workdir not in self.scheduler_acks:
                return ExecOutcome(exit_code=1)
            return ExecOutcome(
                output=json.dumps(self.claims[workdir])
                + "\n"
                + self.scheduler_acks[workdir],
                exit_code=0,
            )
        if (
            "--format=JobIDRaw,Cluster,User,UID,Submit,JobName%80,State,ExitCode"
            in command
        ):
            match = re.search(r"--jobs=([0-9]+)", command)
            if match:
                for directory, ack in self.scheduler_acks.items():
                    job_id, _, cluster = ack.partition(";")
                    if job_id != match[1]:
                        continue
                    intent = self.claims[directory]
                    nonce = hashlib.sha256(
                        (intent["request_id"] + "/" + intent["payload_hash"]).encode()
                    ).hexdigest()[:32]
                    return ExecOutcome(
                        output=f"UID=1000\nUSER=fake\nCLUSTER={cluster or 'fixture'}\nLIVE\nJobId={job_id} UserId=fake(1000) JobName=omicsclaw_{nonce} SubmitTime=2026-10-15T10:00:00 JobState=RUNNING\nACCOUNTING\n",
                        exit_code=0,
                    )
        if (
            "head -c 4096 ./_omicsclaw_process.json" in command
            and workdir in self.claims
        ):
            intent = self.claims[workdir]
            pgid = self.pgids.get(workdir)
            if pgid is None:
                return ExecOutcome(output="", exit_code=1)
            nonce = hashlib.sha256(
                (intent["request_id"] + "/" + intent["payload_hash"]).encode()
            ).hexdigest()[:32]
            proof = {
                "uid": 1000,
                "host_id": "fake-n01",
                "boot_id": "b" * 32,
                "pid": pgid,
                "start_ticks": 123,
                "pgid": pgid,
                "sid": pgid,
                "nonce": nonce,
            }
            return ExecOutcome(
                output=json.dumps(intent) + "\n" + json.dumps(proof), exit_code=0
            )
        return ExecOutcome(output="", exit_code=0)

    def ssh_commands(self) -> list[str]:
        return [argv[-1] for argv, _ in self.calls if argv and argv[0] == "ssh"]

    def sftp_batches(self) -> list[str]:
        return [text or "" for argv, text in self.calls if argv and argv[0] == "sftp"]


PROBE_OK = (
    "login noise\n"
    "===OMICSCLAW_PROBE_START===\n"
    '{"hostname":"fake-n01","platform":"Linux x86_64","home":"/home/fake",'
    '"nproc":16,"memMb":65536,'
    '"gpus":[],"commands":{"sbatch":null,"conda":"/opt/conda/bin/conda",'
    '"module":null,"uv":null,"sinfo":null},"slurm":{"partitions":null},'
    '"scratch":[{"path":"/scratch/fake","exists":true}]}'
    "\n===OMICSCLAW_PROBE_END===\n"
)


class FakePlane:
    """The RemotePlane Protocol, as a call log with canned answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.exec_answer = ExecOutcome(output="ok", exit_code=0, host="hpc1")
        self.submit_answer = SubmitOutcome(
            job_ref="7",
            kind="nohup",
            pgid=4242,
            workdir="/scratch/w",
            host="hpc1",
            submitted_at=1.0,
        )
        self.status_answer = StatusReport(state="running")
        self.cancel_answer = StatusReport(state="canceled")
        self.fetch_answer = FetchOutcome(downloaded=("/tmp/dst/out.txt",))
        self.card = None
        self.notes: list[tuple[str, str, str]] = []

    async def exec(self, host: str, command: str, timeout: float) -> ExecOutcome:
        self.calls.append(("exec", (host, command, timeout)))
        return self.exec_answer

    async def submit(self, host: str, command: str, **kwargs: Any) -> SubmitOutcome:
        self.calls.append(("submit", (host, command, kwargs)))
        return self.submit_answer

    async def status(self, job_ref: str) -> StatusReport:
        self.calls.append(("status", (job_ref,)))
        return self.status_answer

    async def cancel(self, job_ref: str) -> StatusReport:
        self.calls.append(("cancel", (job_ref,)))
        return self.cancel_answer

    async def fetch(
        self, source: str, dest: str, max_mb: float = 100.0
    ) -> FetchOutcome:
        self.calls.append(("fetch", (source, dest, max_mb)))
        return self.fetch_answer

    def host_card(self, alias: str):
        self.calls.append(("host_card", (alias,)))
        return self.card

    def note_answer(self, alias: str, question: str, answer: str) -> None:
        self.calls.append(("note_answer", (alias, question, answer)))
        self.notes.append((alias, question, answer))
