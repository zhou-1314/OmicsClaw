"""Grading a campaign's finished runs with deterministic graders.

A grader reads the files a run delivered and the case's oracle, and returns
a :class:`Grade`. Before a grader is used it is checked against its own
controls: submissions whose verdict is known in advance. Before a run is
graded its record is checked against the files it left behind.

``grades.jsonl`` holds one row per run of the manifest, graded or not, so a
missing result stays visible. Grading reads and never changes a run, and
may be repeated.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .adapters import Adapter, build_adapter, load_attribute
from .layout import Campaign, write_jsonl
from .manifest import Manifest, RunSpec
from .outcome import INFRA_FAILURE, ProcessExit, classify
from .run import deliverable_state, finished

__all__ = [
    "Control",
    "Grade",
    "GradeSummary",
    "Grader",
    "GraderError",
    "Submission",
    "grade_campaign",
    "health_problems",
    "load_grader",
    "self_check",
]

UNGRADEABLE = frozenset({"unfinished", "infra_failure", "unhealthy", "grader_error"})
"""Reasons a row has no grade that mean the campaign is not ready to be
read, as opposed to an agent that delivered nothing."""


class GraderError(RuntimeError):
    """A grader cannot be used: it failed its controls or has no oracle."""


@dataclass(frozen=True)
class Submission:
    """What one run delivered.

    :param run: The run's key.
    :param case: The case id.
    :param workspace: The run's workspace.
    :param deliverables: Each declared deliverable's workspace-relative
        path, mapped to the file. Every file exists.
    """

    run: str
    case: str
    workspace: Path
    deliverables: Mapping[str, Path]


@dataclass(frozen=True)
class Grade:
    """A grader's verdict on one submission.

    :param passed: Whether the submission meets the case's bar.
    :param score: A number for comparing submissions, when there is one.
    :param metrics: Further named measurements.
    :param detail: One line saying why, for a person.
    """

    passed: bool
    score: float | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class Control:
    """A submission whose verdict is known, for checking a grader.

    :param name: What the control is called in a failure report.
    :param expect_pass: The verdict the grader must reach.
    :param deliverables: File contents by workspace-relative path.
    :param oracle: File contents by oracle-relative path.
    """

    name: str
    expect_pass: bool
    deliverables: Mapping[str, str]
    oracle: Mapping[str, str]


class Grader(Protocol):
    """A deterministic grader for one kind of case."""

    def grade(self, submission: Submission, oracle: Path) -> Grade:
        """Judge *submission* against the files in *oracle*.

        A malformed deliverable is a failed grade, not an exception.
        """
        ...

    def controls(self) -> Sequence[Control]:
        """At least one control that must pass and one that must fail."""
        ...


@dataclass
class GradeSummary:
    """Counts over the rows written.

    :param graded: Rows with a grade.
    :param passed: Graded rows that passed.
    :param skipped: Rows without a grade, counted by reason.
    """

    graded: int = 0
    passed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """Whether no row is missing a grade for a reason in
        :data:`UNGRADEABLE`."""
        return not (set(self.skipped) & UNGRADEABLE)


def load_grader(reference: str) -> Grader:
    """The grader ``module:attribute`` names; a class is instantiated.

    :raises ManifestError: The reference cannot be imported.
    """
    target = load_attribute(reference, "grader")
    return target() if isinstance(target, type) else target


def self_check(grader: Grader) -> list[str]:
    """Run *grader* over its own controls and report what is wrong.

    :returns: One line per problem; empty when the grader may be used. A
        grader without a passing control or without a failing one has a
        problem, as has one whose verdict on a control differs from the
        expected one, or that raises.
    """
    try:
        controls = list(grader.controls())
    except Exception as exc:  # noqa: BLE001 - reported as a problem
        return [f"controls() raised {type(exc).__name__}: {exc}"]
    problems = []
    if not any(control.expect_pass for control in controls):
        problems.append("no control that must pass")
    if not any(not control.expect_pass for control in controls):
        problems.append("no control that must fail")
    for control in controls:
        with tempfile.TemporaryDirectory(prefix="bench-control-") as scratch:
            workspace, oracle = Path(scratch, "workspace"), Path(scratch, "oracle")
            delivered = _write_files(workspace, control.deliverables)
            _write_files(oracle, control.oracle)
            submission = Submission(
                run=f"control/{control.name}",
                case="control",
                workspace=workspace,
                deliverables=delivered,
            )
            try:
                verdict = grader.grade(submission, oracle).passed
            except Exception as exc:  # noqa: BLE001 - reported as a problem
                problems.append(
                    f"control {control.name!r} raised {type(exc).__name__}: {exc}"
                )
                continue
        if verdict is not control.expect_pass:
            expected = "pass" if control.expect_pass else "fail"
            got = "passed" if verdict else "failed"
            problems.append(f"control {control.name!r} must {expected} but {got}")
    return problems


def health_problems(
    run: RunSpec, campaign: Campaign, adapter: Adapter, record: Mapping[str, Any]
) -> list[str]:
    """What is wrong with a finished run's record, judged from its files.

    The run's evidence is read again and classified again. A record that
    names another run, a missing output file or workspace, and an outcome
    that no longer follows from the evidence are each a problem.

    :returns: One line per problem; empty when the run may be graded.
    """
    paths = campaign.paths(run)
    problems = []
    if record.get("run") != run.key:
        problems.append(f"the record is for {record.get('run')!r}")
    for required in (paths.stdout, paths.stderr):
        if not required.is_file():
            problems.append(f"{required.name} is missing")
    if not paths.workspace.is_dir():
        problems.append("the workspace is missing")
    if problems:
        return problems
    exit = ProcessExit(
        started=record.get("exit_code") is not None,
        returncode=record.get("exit_code"),
        timed_out=bool(record.get("timed_out")),
        killed=bool(record.get("killed")),
    )
    evidence = adapter.collect(run, paths, exit)
    missing = [
        entry["path"]
        for entry in deliverable_state(run, paths.workspace)
        if not entry["exists"]
    ]
    outcome, reason = classify(exit, evidence, missing)
    if outcome != record.get("outcome"):
        detail = f" ({reason})" if reason else ""
        problems.append(
            f"recorded as {record.get('outcome')}, but the evidence now says "
            f"{outcome}{detail}"
        )
    return problems


def grade_campaign(
    manifest: Manifest,
    campaign: Campaign,
    *,
    report: Callable[[str], None] = lambda message: None,
) -> GradeSummary:
    """Write ``grades.jsonl``: one row per run of the manifest.

    A row has ``graded`` true and a ``passed`` verdict, or ``graded`` false
    and a ``skip`` reason: ``unfinished`` (no ``done.json``),
    ``infra_failure``, ``unhealthy`` (see :func:`health_problems`, listed
    under ``health``), ``no_deliverable``, ``no_grader`` or
    ``grader_error``. Runs that ended on a timeout or a turn limit are
    graded when their deliverables exist; the row keeps their outcome.

    :raises GraderError: A grader failed its controls, or a case to be
        graded has no ``oracle/`` directory. Nothing is written.
    :raises ManifestError: A grader or adapter cannot be loaded.
    """
    graders: dict[str, Grader] = {}
    for case in manifest.cases:
        if not case.grader or case.grader in graders:
            continue
        grader = load_grader(case.grader)
        problems = self_check(grader)
        if problems:
            raise GraderError(
                f"grader {case.grader} failed its controls: " + "; ".join(problems)
            )
        graders[case.grader] = grader
        report(f"[self-check] {case.grader} ok")
    for case in manifest.cases:
        if case.grader and not campaign.oracle(case).is_dir():
            raise GraderError(
                f"case {case.id!r} has no oracle directory: {campaign.oracle(case)}"
            )
    adapters = {arm.id: build_adapter(arm) for arm in manifest.arms}

    rows = []
    summary = GradeSummary()
    skipped: Counter[str] = Counter()
    ordered = sorted(
        manifest.runs(),
        key=lambda run: (run.arm.id, run.model.id, run.case.id, run.repeat),
    )
    for run in ordered:
        row = _row(run, manifest, campaign, adapters[run.arm.id], graders)
        rows.append(row)
        if row["graded"]:
            summary.graded += 1
            summary.passed += int(bool(row["passed"]))
        else:
            skipped[row["skip"]] += 1
            report(f"[not graded] {run.key}: {row['skip']}")
    summary.skipped = dict(sorted(skipped.items()))
    write_jsonl(campaign.grades, rows)
    return summary


def _row(
    run: RunSpec,
    manifest: Manifest,
    campaign: Campaign,
    adapter: Adapter,
    graders: Mapping[str, Grader],
) -> dict[str, Any]:
    paths = campaign.paths(run)
    record = finished(paths)
    row: dict[str, Any] = {
        "campaign": manifest.name,
        **run.identity(),
        "outcome": record.get("outcome") if record else "unfinished",
        "graded": False,
        "skip": "",
        "health": [],
        "grader": run.case.grader,
        "passed": None,
        "score": None,
        "metrics": {},
        "detail": "",
        "access_flagged": bool(record and record.get("access", {}).get("flagged")),
    }
    if record is None:
        row["skip"] = "unfinished"
        return row
    if record.get("outcome") == INFRA_FAILURE:
        row["skip"] = "infra_failure"
        row["detail"] = str(record.get("reason", ""))
        return row
    problems = health_problems(run, campaign, adapter, record)
    if problems:
        row["skip"], row["health"] = "unhealthy", problems
        return row
    state = deliverable_state(run, paths.workspace)
    if not all(entry["exists"] for entry in state):
        row["skip"] = "no_deliverable"
        return row
    grader = graders.get(run.case.grader)
    if grader is None:
        row["skip"] = "no_grader"
        return row
    submission = Submission(
        run=run.key,
        case=run.case.id,
        workspace=paths.workspace,
        deliverables={
            entry["path"]: paths.workspace / entry["path"] for entry in state
        },
    )
    try:
        grade = grader.grade(submission, campaign.oracle(run.case))
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        row["skip"] = "grader_error"
        row["detail"] = f"{type(exc).__name__}: {exc}"
        return row
    row.update(
        graded=True,
        passed=bool(grade.passed),
        score=grade.score,
        metrics=dict(grade.metrics),
        detail=grade.detail,
    )
    return row


def _write_files(root: Path, files: Mapping[str, str]) -> dict[str, Path]:
    written = {}
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written[relative] = path
    return written
