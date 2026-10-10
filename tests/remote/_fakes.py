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

    def on(self, needle: str, answer: Any) -> "FakeSpawner":
        # Prepended, so a rule a test adds mid-scenario — "now the job
        # has finished" — wins over the fixture's broader rule.
        self.rules.insert(0, (needle, answer))
        return self

    async def spawn(
        self, argv: list[str], *, timeout: float, stdin_text: str | None = None
    ) -> ExecOutcome:
        self.calls.append((list(argv), stdin_text))
        command = "<sftp>" if argv and argv[0] == "sftp" else (argv[-1] if argv else "")
        for needle, answer in self.rules:
            if needle in command or (stdin_text is not None and needle in stdin_text):
                if isinstance(answer, type) and issubclass(answer, BaseException):
                    raise answer("fake transport failure")
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, ExecOutcome):
                    return answer
                return ExecOutcome(output=str(answer), exit_code=0)
        return ExecOutcome(output="", exit_code=0)

    def ssh_commands(self) -> list[str]:
        return [argv[-1] for argv, _ in self.calls if argv and argv[0] == "ssh"]

    def sftp_batches(self) -> list[str]:
        return [
            text or "" for argv, text in self.calls if argv and argv[0] == "sftp"
        ]


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
            job_ref="7", kind="nohup", pgid=4242, workdir="/scratch/w",
            host="hpc1", submitted_at=1.0,
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

    async def fetch(self, source: str, dest: str, max_mb: float = 100.0) -> FetchOutcome:
        self.calls.append(("fetch", (source, dest, max_mb)))
        return self.fetch_answer

    def host_card(self, alias: str):
        self.calls.append(("host_card", (alias,)))
        return self.card

    def note_answer(self, alias: str, question: str, answer: str) -> None:
        self.calls.append(("note_answer", (alias, question, answer)))
        self.notes.append((alias, question, answer))
