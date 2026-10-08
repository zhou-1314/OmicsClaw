"""Shared pieces of the harness tests: a fake adapter and a toy campaign.

:class:`FakeAdapter` starts ``fake_agent.py`` instead of a real agent. What
that process does comes from a playbook, a JSON file the arm names under
``options.playbook``::

    {"default": [<spec>, ...], "<run key>": [<spec for attempt 1>, ...]}

A run takes the spec for its attempt number, or the last one listed. See
``fake_agent.py`` for what a spec may hold.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omicsclaw.bench.adapters import Launch
from omicsclaw.bench.example import ANSWER, CASES, write_cases
from omicsclaw.bench.layout import Campaign, RunPaths
from omicsclaw.bench.manifest import Arm, Budget, Manifest, RunSpec, load_manifest
from omicsclaw.bench.outcome import Command, Evidence, ProcessExit, Usage

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")
FAKE = "tests.bench._support:FakeAdapter"
GRADER = "omicsclaw.bench.example:SumGrader"

RIGHT = {case: json.dumps({"sum": sum(numbers)}) for case, numbers in CASES.items()}
"""The correct ``answer.json`` text of each toy case."""

OK_EVIDENCE = {
    "stop_reason": "converged",
    "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 7,
              "llm_calls": 2, "llm_errors": 0, "llm_cancelled": 0},
}


def ok(case: str, **extra: Any) -> dict[str, Any]:
    """A spec that delivers the right answer for *case* and exits 0."""
    return {"write": {ANSWER: RIGHT[case]}, "evidence": OK_EVIDENCE, **extra}


class FakeAdapter:
    """Runs ``fake_agent.py`` with the playbook's spec for the run."""

    reserved_env = frozenset({"FAKE_RESERVED"})
    workspace_state = (".state",)

    def __init__(self, arm: Arm) -> None:
        self._playbook = Path(arm.options["playbook"])

    def launch(
        self,
        run: RunSpec,
        paths: RunPaths,
        budget: Budget,
        base_env: Mapping[str, str],
    ) -> Launch:
        playbook = json.loads(self._playbook.read_text(encoding="utf-8"))
        specs = playbook.get(run.key) or playbook.get("default") or [ok(run.case.id)]
        prefix = paths.meta.name + "."
        attempt = 1 + sum(
            1 for entry in paths.meta.parent.iterdir() if entry.name.startswith(prefix)
        )
        spec = dict(specs[min(attempt, len(specs)) - 1])
        spec.setdefault("label", run.key)
        if spec.get("write") == "right":
            spec["write"] = {ANSWER: RIGHT[run.case.id]}
        (paths.meta / "fake.json").write_text(json.dumps(spec), encoding="utf-8")
        harness = {"FAKE_RUN": run.key}
        return Launch(
            argv=(sys.executable, str(FAKE_AGENT), str(paths.meta / "fake.json")),
            env={**base_env, **harness},
            cwd=paths.workspace,
            harness_env=harness,
        )

    def collect(self, run: RunSpec, paths: RunPaths, exit: ProcessExit) -> Evidence:
        reported: dict[str, Any] = {}
        try:
            for line in paths.stderr.read_text(encoding="utf-8").splitlines():
                if line.startswith("FAKE-EVIDENCE "):
                    reported = json.loads(line.removeprefix("FAKE-EVIDENCE "))
        except OSError:
            pass
        return Evidence(
            stop_reason=reported.get("stop_reason", ""),
            infra_reason=reported.get("infra_reason", ""),
            failure=reported.get("failure", ""),
            approvals_required=reported.get("approvals_denied", 0),
            approvals_denied=reported.get("approvals_denied", 0),
            turns=reported.get("turns"),
            usage=Usage(source="fake", **reported.get("usage", {})),
            commands=tuple(
                Command(tool, text) for tool, text in reported.get("commands", [])
            ),
        )


def manifest_text(
    playbook: Path,
    *,
    arms: Sequence[str] = ("a",),
    cases: Sequence[str] = ("sum-a",),
    repeats: int = 1,
    seed: int = 3,
    wall_clock_s: float = 20,
    kill_grace_s: float = 5,
    grader: str = GRADER,
    extra: str = "",
) -> str:
    """A manifest over the toy cases whose arms all use the fake adapter."""
    parts = [
        'name = "toy"',
        f"seed = {seed}",
        f"repeats = {repeats}",
        extra,
        "[budget]",
        f"wall_clock_s = {wall_clock_s}",
        f"kill_grace_s = {kill_grace_s}",
        "[[models]]",
        'id = "m"',
        'provider = "none"',
        'model = "fake-model"',
    ]
    for arm in arms:
        parts += [
            "[[arms]]",
            f'id = "{arm}"',
            f'adapter = "{FAKE}"',
            f"options = {{ playbook = {json.dumps(str(playbook))} }}",
        ]
    for case in cases:
        parts += [
            "[[cases]]",
            f'id = "{case}"',
            'prompt = "Add the numbers in data/numbers.txt."',
            f'deliverables = ["{ANSWER}"]',
            f'grader = "{grader}"',
        ]
    return "\n".join(parts) + "\n"


class Toy:
    """A toy campaign on disk: cases, a playbook, a manifest and an output root."""

    def __init__(self, root: Path, **manifest_options: Any) -> None:
        self.root = root
        self.cases = root / "cases"
        self.out = root / "out"
        self.playbook = root / "playbook.json"
        self.manifest_path = root / "suite" / "manifest.toml"
        write_cases(self.cases)
        self.manifest_path.parent.mkdir(parents=True)
        self.play({})
        self.manifest_path.write_text(
            manifest_text(self.playbook, **manifest_options), encoding="utf-8"
        )

    def play(self, playbook: Mapping[str, Any]) -> None:
        """Replace the playbook."""
        self.playbook.write_text(json.dumps(playbook), encoding="utf-8")

    @property
    def manifest(self) -> Manifest:
        return load_manifest(self.manifest_path)

    @property
    def campaign(self) -> Campaign:
        return Campaign(out=self.out, cases=self.cases)

    def paths(self, key: str) -> RunPaths:
        """The paths of the run with this key."""
        return RunPaths(self.out / "cells" / key, self.out / "meta" / key)

    def done(self, key: str) -> dict[str, Any]:
        """The run's ``done.json``."""
        return json.loads(self.paths(key).done.read_text(encoding="utf-8"))


# ---- a scripted model backend for the real ``oc cli`` ----------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

SHIM = '''\
"""Script the model backend before the command starts."""
import asyncio
import json
import os
import sys

sys.path.insert(0, {root!r})

from omicsclaw.entry import assembly
from omicsclaw.provider import Completion, ProviderError
from omicsclaw.schema import (
    Message, Role, StreamChunk, StreamChunkType, ToolCall, Usage,
)

USAGE = Usage(input_tokens=1000, output_tokens=10, cache_read_tokens=400)


def answer():
    """The ``write_file`` arguments holding the right sum for this workspace."""
    with open("data/numbers.txt", encoding="utf-8") as handle:
        total = sum(int(line) for line in handle.read().split())
    return json.dumps({{"path": {answer!r}, "content": json.dumps({{"sum": total}})}})


def call(name, arguments):
    payload = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return Message(
        role=Role.ASSISTANT,
        tool_calls=(ToolCall(id="call_1", name=name, arguments=payload),),
    )


def say(text):
    return Message(role=Role.ASSISTANT, content=text)


def down():
    raise ProviderError("backend refused", provider="scripted", status_code=401)


class Scripted:
    name = "scripted"

    def __init__(self, scenario):
        self.scenario = scenario

    def reply(self, messages, tools):
        results = sum(1 for message in messages if message.role is Role.TOOL)
        parent = any(tool.name == "task" for tool in tools or ())
        scenario = self.scenario
        delegate = ("task", {{"subagent_type": "general-purpose",
                             "description": "add numbers",
                             "prompt": "Add 3, 14, 15, 92 and 65."}})
        target = os.environ.get("BENCH_ORACLE", "/nonexistent/oracle")
        first = {{
            "": None,
            "write": None,
            "subagent": delegate,
            "subfail": delegate,
            "danger": ("bash", {{"command": "rm -rf " + target}}),
            "web": ("web_fetch", {{"url": "https://example.com/"}}),
            "sleepy": ("bash", {{"command": "sleep 120"}}),
        }}
        if scenario == "fail":
            down()
        if scenario == "loop":
            return call("bash", {{"command": "echo step-%d" % results}})
        if not parent:
            if scenario == "subfail":
                down()
            return say("The sum is 189.")
        steps = [first[scenario]] if first[scenario] else []
        steps.append(("write_file", answer()))
        if results < len(steps):
            return call(*steps[results])
        return say("Done.")

    async def generate(self, messages, tools=None):
        if self.scenario == "hang":
            await asyncio.sleep(120)
        return Completion(message=self.reply(messages, tools), usage=USAGE)

    async def _stream(self, messages, tools=None):
        completion = await self.generate(messages, tools)
        if completion.message.content:
            yield StreamChunk(
                type=StreamChunkType.TEXT_DELTA, delta=completion.message.content
            )
        yield StreamChunk(
            type=StreamChunkType.DONE, message=completion.message, usage=USAGE
        )

    def generate_stream(self, messages, tools=None):
        return self._stream(messages, tools)

    def bind(self, **overrides):
        return self


assembly.provider_from_env = lambda provider="", model="", *a, **k: Scripted(
    model.removeprefix("stub-")
)
'''
"""A ``sitecustomize`` module that replaces the model backend of ``oc cli``.

:mod:`site` imports it before the command starts, and it swaps
``omicsclaw.entry.assembly.provider_from_env`` for a scripted backend, the
way ``tests/launch/test_cli_command.py`` does. The model name picks the
script: ``stub-<scenario>``, or no model for the plain one, which writes
the right sum for the workspace it runs in and stops.
"""


def scripted_backend(root: Path) -> Path:
    """Write the shim under *root* and return the directory to put on
    ``PYTHONPATH``."""
    shim = root / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        SHIM.format(root=str(REPO_ROOT), answer=ANSWER), encoding="utf-8"
    )
    return shim
