"""``python -m omicsclaw.bench``: plan, stage, run and grade a campaign.

Exit status: ``0`` when the command did what it was asked and every run it
looked at has a result that describes the agent; ``1`` when some run is
unfinished, failed for infrastructure reasons or could not be graded; ``2``
for a manifest, argument or grader that cannot be used, or an output root
another invocation is using; ``130`` when interrupted, ``143`` and ``129``
when stopped by ``SIGTERM`` or ``SIGHUP``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .example import write_cases
from .grade import GraderError, grade_campaign
from .layout import Campaign
from .manifest import Manifest, ManifestError, load_manifest
from .outcome import INFRA_FAILURE
from .run import (
    CampaignLocked,
    finished,
    run_campaign,
    selected,
    stage_campaign,
)
from .stage import StageError

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit status."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (ManifestError, StageError, GraderError, CampaignLocked) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omicsclaw.bench", description=__doc__.split("\n\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="print the runs in execution order")
    plan.add_argument("manifest", type=Path)
    plan.set_defaults(handler=_plan)

    stage = commands.add_parser("stage", help="create workspaces without running")
    _campaign_arguments(stage)
    stage.add_argument("--select", default="", help="pattern over run keys")
    stage.set_defaults(handler=_stage)

    run = commands.add_parser("run", help="run every unfinished run")
    _campaign_arguments(run)
    run.add_argument("--select", default="", help="pattern over run keys")
    run.add_argument("--jobs", type=int, default=1, help="runs in progress at once")
    run.add_argument(
        "--retry-infra",
        action="store_true",
        help="run infra_failure runs again, keeping their earlier files",
    )
    run.add_argument(
        "--env-file",
        type=Path,
        help="KEY=VALUE lines added to the agents' environment; a variable "
        "already set keeps its value",
    )
    run.set_defaults(handler=_run)

    grade = commands.add_parser("grade", help="grade finished runs")
    _campaign_arguments(grade)
    grade.set_defaults(handler=_grade)

    example = commands.add_parser("example", help="write the toy suite's cases")
    example.add_argument("directory", type=Path)
    example.set_defaults(handler=_example)
    return parser


def _campaign_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--cases", type=Path, required=True, help="cases root, outside the repository"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="output root, outside the repository"
    )


def _open(arguments: argparse.Namespace) -> tuple[Manifest, Campaign]:
    """The manifest and the campaign directories the arguments name.

    :raises ManifestError: The cases root is missing, one directory is
        inside the other, or either is inside the repository the manifest
        belongs to.
    """
    manifest = load_manifest(arguments.manifest)
    cases = arguments.cases.expanduser().resolve()
    out = arguments.out.expanduser().resolve()
    if not cases.is_dir():
        raise ManifestError(f"the cases root {cases} is not a directory")
    if cases == out or cases in out.parents or out in cases.parents:
        raise ManifestError(
            f"--cases {cases} and --out {out} overlap; a workspace would sit "
            "beside the oracle, and every path in it would look like a "
            "reference to the cases root"
        )
    repository = _repository(manifest.path)
    for label, path in (("--cases", cases), ("--out", out)):
        if repository is not None and (
            path == repository or repository in path.parents
        ):
            raise ManifestError(
                f"{label} {path} is inside the repository {repository}; case "
                "data and results are kept outside it"
            )
    return manifest, Campaign(out=out, cases=cases)


def _repository(manifest: Path) -> Path | None:
    """The checkout *manifest* is in: the nearest ancestor holding ``.git``."""
    for parent in manifest.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _plan(arguments: argparse.Namespace) -> int:
    manifest = load_manifest(arguments.manifest)
    for number, run in enumerate(manifest.runs(), start=1):
        print(f"{number}\t{run.key}")
    return 0


def _stage(arguments: argparse.Namespace) -> int:
    manifest, campaign = _open(arguments)
    count = stage_campaign(manifest, campaign, select=arguments.select)
    print(json.dumps({"staged": count}))
    return 0


def _run(arguments: argparse.Namespace) -> int:
    manifest, campaign = _open(arguments)
    environment = dict(os.environ)
    if arguments.env_file is not None:
        for name, value in _env_file(arguments.env_file).items():
            environment.setdefault(name, value)
    with _stop_signals() as received:
        try:
            summary = run_campaign(
                manifest,
                campaign,
                jobs=arguments.jobs,
                retry_infra=arguments.retry_infra,
                select=arguments.select,
                base_env=environment,
            )
        except KeyboardInterrupt:  # arrived when no run was in progress
            return 128 + received[0] if received else 130
    print(json.dumps(dataclasses.asdict(summary), sort_keys=True))
    if summary.interrupted:
        return 128 + received[0] if received else 130
    records = [
        finished(campaign.paths(run)) for run in selected(manifest, arguments.select)
    ]
    open_runs = [
        record
        for record in records
        if record is None or record.get("outcome") == INFRA_FAILURE
    ]
    return 1 if summary.errors or open_runs else 0


@contextmanager
def _stop_signals() -> Iterator[list[int]]:
    """Treat ``SIGTERM`` and ``SIGHUP`` like Ctrl-C while the block runs.

    The first of them raises :exc:`KeyboardInterrupt` in the main thread,
    which is the stop :func:`~omicsclaw.bench.run.run_campaign` already
    handles by killing the runs in progress. Later ones are ignored, so the
    stop is not cut short. Outside the main thread nothing is installed.

    :returns: A list that receives the number of the signal that arrived.
    """
    received: list[int] = []

    def stop(number: int, frame: object) -> None:
        if not received:
            received.append(number)
            raise KeyboardInterrupt

    previous = {}
    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, stop)
    except ValueError:
        pass
    try:
        yield received
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _grade(arguments: argparse.Namespace) -> int:
    manifest, campaign = _open(arguments)
    summary = grade_campaign(
        manifest, campaign, report=lambda message: print(message, file=sys.stderr)
    )
    print(json.dumps(dataclasses.asdict(summary), sort_keys=True))
    return 0 if summary.ready else 1


def _example(arguments: argparse.Namespace) -> int:
    for case in write_cases(arguments.directory.expanduser().resolve()):
        print(case)
    return 0


def _env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` pairs from *path*.

    Blank lines and lines starting with ``#`` are skipped, a leading
    ``export`` is dropped, and one pair of matching quotes around a value
    is removed.

    :raises ManifestError: The file cannot be read.
    """
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read --env-file {path}: {exc}") from exc
    pairs = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs[name.strip()] = value
    return pairs


if __name__ == "__main__":
    raise SystemExit(main())
