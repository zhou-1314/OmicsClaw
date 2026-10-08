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
