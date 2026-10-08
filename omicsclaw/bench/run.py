"""Running a campaign: each run once, in a fixed order, and recorded.

A run is finished when its ``meta`` directory holds ``done.json``. Running
a campaign again skips finished runs, so an interrupted campaign continues
where it stopped. ``predictions.jsonl`` and ``usage.jsonl`` are rewritten
from the ``done.json`` files after every run and hold one row per finished
run of the manifest.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .access import Pattern, audit_access, campaign_patterns
from .adapters import Adapter, build_adapter
from .layout import Campaign, RunPaths, read_json, write_json, write_jsonl
from .manifest import Manifest, RunSpec
from .outcome import INFRA_FAILURE, classify
from .process import Interrupted, run_process
from .stage import StageError, set_aside, stage, staged_files

__all__ = [
    "RunSummary",
    "finished",
    "rebuild_indexes",
    "run_campaign",
    "selected",
    "stage_campaign",
]

SCHEMA = 1


@dataclass
class RunSummary:
    """What one invocation did, and where the whole campaign stands.

    :param executed: Runs this invocation started and finished.
    :param skipped: Runs it left alone because they were already finished.
    :param errors: Runs the harness itself failed on, with the message.
        They have no ``done.json`` and are tried again next time.
    :param outcomes: Finished runs of the manifest, counted by outcome.
    :param unfinished: Runs of the manifest with no ``done.json``.
    :param interrupted: The invocation was stopped before it was through.
    """

    executed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    unfinished: int = 0
    interrupted: bool = False


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def selected(manifest: Manifest, select: str = "") -> tuple[RunSpec, ...]:
    """The manifest's runs in execution order, narrowed by *select*.

    :param select: A shell-style pattern matched against each run's key,
        such as ``oc/*/sum-a/r1``. Empty selects every run.
    """
    runs = manifest.runs()
    if not select:
        return runs
    return tuple(run for run in runs if fnmatch.fnmatchcase(run.key, select))


def finished(paths: RunPaths) -> dict[str, Any] | None:
    """The run's ``done.json``, or ``None`` when the run is not finished."""
    return read_json(paths.done)


def stage_campaign(
    manifest: Manifest,
    campaign: Campaign,
    *,
    select: str = "",
    report: Callable[[str], None] = _say,
) -> int:
    """Stage every selected run that has nothing on disk yet.

    :returns: How many runs were staged.
    :raises StageError: A case has no ``public/`` directory.
    """
    _require_public(manifest, campaign)
    count = 0
    for run in selected(manifest, select):
        paths = campaign.paths(run)
        if paths.meta.exists() or paths.workspace.exists():
            continue
        stage(run, campaign)
        report(f"[staged] {run.key}")
        count += 1
    return count


def run_campaign(
    manifest: Manifest,
    campaign: Campaign,
    *,
    jobs: int = 1,
    retry_infra: bool = False,
    select: str = "",
    base_env: Mapping[str, str] | None = None,
    report: Callable[[str], None] = _say,
) -> RunSummary:
    """Run every selected run that is not finished yet.

    :param jobs: Runs in progress at once.
    :param retry_infra: Also run again the finished runs whose outcome is
        ``infra_failure``. Their earlier directories are kept, renamed.
    :param select: See :func:`selected`.
    :param base_env: The environment agent processes start from; the
        harness's own by default.
    :param report: Called with one progress line per event.
    :returns: A summary; see :class:`RunSummary`.
    :raises ManifestError: An arm names an unknown adapter or sets a
        variable its adapter reserves.
    :raises StageError: A case has no ``public/`` directory.

    A run left unfinished by an earlier invocation is set aside as
    ``r<k>.incomplete<n>`` and started over in a new workspace. On
    ``KeyboardInterrupt`` the runs in progress are killed and left
    unfinished, and the summary says the invocation was interrupted.
    """
    adapters = {arm.id: build_adapter(arm) for arm in manifest.arms}
    _require_public(manifest, campaign)
    patterns = campaign_patterns(
        campaign.cases, campaign.meta, manifest.audit_patterns
    )
    environment = dict(os.environ if base_env is None else base_env)
    campaign.out.mkdir(parents=True, exist_ok=True)

    summary = RunSummary()
    pending: list[RunSpec] = []
    for run in selected(manifest, select):
        record = finished(campaign.paths(run))
        if record is None or (retry_infra and record.get("outcome") == INFRA_FAILURE):
            pending.append(run)
        else:
            summary.skipped += 1
            report(f"[skip] {run.key} ({record.get('outcome')})")

    stop = threading.Event()
    lock = threading.Lock()

    def one(run: RunSpec) -> dict[str, Any]:
        record = _execute(
            run,
            manifest,
            campaign,
            adapters[run.arm.id],
            patterns,
            environment,
            stop,
        )
        with lock:
            rebuild_indexes(manifest, campaign)
        return record

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(one, run): run for run in pending}
        try:
            for future in as_completed(futures):
                run = futures[future]
                try:
                    record = future.result()
                except Interrupted:
                    continue
                except Exception as exc:  # noqa: BLE001 - reported, run stays open
                    summary.errors.append(f"{run.key}: {type(exc).__name__}: {exc}")
                    report(f"[error] {run.key}: {type(exc).__name__}: {exc}")
                    continue
                summary.executed += 1
                detail = f": {record['reason']}" if record.get("reason") else ""
                report(
                    f"[done] {run.key} {record['outcome']}{detail} "
                    f"({record['wall_s']:.1f}s)"
                )
        except KeyboardInterrupt:
            summary.interrupted = True
            stop.set()
            for future in futures:
                future.cancel()

    rebuild_indexes(manifest, campaign)
    counts: Counter[str] = Counter()
    for run in manifest.runs():
        record = finished(campaign.paths(run))
        if record is None:
            summary.unfinished += 1
        else:
            counts[str(record.get("outcome"))] += 1
    summary.outcomes = dict(sorted(counts.items()))
    return summary


def rebuild_indexes(manifest: Manifest, campaign: Campaign) -> None:
    """Rewrite ``predictions.jsonl`` and ``usage.jsonl`` from the run records.

    One row per finished run of the manifest, ordered by arm, model, case
    and repeat.
    """
    predictions, usage = [], []
    ordered = sorted(
        manifest.runs(),
        key=lambda run: (run.arm.id, run.model.id, run.case.id, run.repeat),
    )
    for run in ordered:
        record = finished(campaign.paths(run))
        if record is None:
            continue
        head = {"campaign": manifest.name, **run.identity()}
        head["model_resolved"] = record.get("model_resolved", "")
        head["attempt"] = record.get("attempt")
        head["outcome"] = record.get("outcome")
        approvals = record.get("approvals", {})
        predictions.append({
            **head,
            "reason": record.get("reason", ""),
            "workspace": f"cells/{run.key}",
            "meta": f"meta/{run.key}",
            "deliverables": record.get("deliverables", []),
            "access_flagged": bool(record.get("access", {}).get("flagged")),
            "access_matches": record.get("access", {}).get("matches", 0),
            "approvals_refused": approvals.get("denied", 0)
            + approvals.get("pending", 0),
            "started_at": record.get("started_at", ""),
            "ended_at": record.get("ended_at", ""),
        })
        usage.append({
            **head,
            "wall_s": record.get("wall_s"),
            "turns": record.get("turns"),
            **record.get("usage", {}),
        })
    campaign.out.mkdir(parents=True, exist_ok=True)
    write_jsonl(campaign.predictions, predictions)
    write_jsonl(campaign.usage, usage)


def deliverable_state(run: RunSpec, workspace: Path) -> list[dict[str, Any]]:
    """Each declared deliverable with whether it exists and its size."""
    state = []
    for relative in run.case.deliverables:
        path = workspace / relative
        exists = path.is_file()
        state.append({
            "path": relative,
            "exists": exists,
            "bytes": path.stat().st_size if exists else 0,
        })
    return state


def _require_public(manifest: Manifest, campaign: Campaign) -> None:
    for case in manifest.cases:
        if not campaign.public(case).is_dir():
            raise StageError(
                f"case {case.id!r} has no public directory: {campaign.public(case)}"
            )


def _attempt(paths: RunPaths) -> int:
    """This attempt's number: one more than the attempts set aside."""
    parent = paths.meta.parent
    if not parent.is_dir():
        return 1
    prefix = paths.meta.name + "."
    return 1 + sum(1 for entry in parent.iterdir() if entry.name.startswith(prefix))


def _prepare(run: RunSpec, campaign: Campaign) -> RunPaths:
    """Leave *run* freshly staged, setting aside any earlier attempt."""
    paths = campaign.paths(run)
    staged = (
        paths.workspace.is_dir()
        and paths.staged.is_file()
        and not paths.command.exists()
    )
    if finished(paths) is not None:
        set_aside(paths, "infra")
    elif not staged:
        set_aside(paths, "incomplete")
    if not paths.staged.exists():
        stage(run, campaign)
    return paths


def _execute(
    run: RunSpec,
    manifest: Manifest,
    campaign: Campaign,
    adapter: Adapter,
    patterns: Sequence[Pattern],
    environment: Mapping[str, str],
    stop: threading.Event,
) -> dict[str, Any]:
    """Stage, run, read back, classify, audit and record one run."""
    if stop.is_set():
        raise Interrupted("the harness was stopped")
    paths = _prepare(run, campaign)
    attempt = _attempt(paths)
    launch = adapter.launch(run, paths, manifest.budget, environment)
    write_json(paths.command, {
        "argv": list(launch.argv),
        "cwd": str(launch.cwd),
        "harness_env": dict(launch.harness_env),
        "inherited_env_names": sorted(set(launch.env) - set(launch.harness_env)),
    })
    exit = run_process(
        launch.argv,
        env=launch.env,
        cwd=launch.cwd,
        stdout=paths.stdout,
        stderr=paths.stderr,
        wall_clock_s=manifest.budget.wall_clock_s,
        kill_grace_s=manifest.budget.kill_grace_s,
        stop=stop,
    )
    evidence = adapter.collect(run, paths, exit)
    deliverables = deliverable_state(run, paths.workspace)
    missing = [entry["path"] for entry in deliverables if not entry["exists"]]
    outcome, reason = classify(exit, evidence, missing)
    access = audit_access(
        patterns=patterns,
        commands=evidence.commands,
        audit_log=paths.audit_log,
        workspace=paths.workspace,
        unchanged=staged_files(paths),
        skip=adapter.workspace_state,
    )
    write_json(paths.access, access)
    record = {
        "schema": SCHEMA,
        "campaign": manifest.name,
        **run.identity(),
        "adapter": run.arm.adapter,
        "attempt": attempt,
        "outcome": outcome,
        "reason": reason,
        "started_at": exit.started_at,
        "ended_at": exit.ended_at,
        "wall_s": exit.wall_s,
        "exit_code": exit.returncode,
        "timed_out": exit.timed_out,
        "killed": exit.killed,
        "strays_killed": exit.strays,
        "stop_reason": evidence.stop_reason,
        "turns": evidence.turns,
        "model_resolved": evidence.model_resolved,
        "approvals": {
            "required": evidence.approvals_required,
            "denied": evidence.approvals_denied,
            "pending": evidence.approvals_pending,
        },
        "deliverables": deliverables,
        "usage": evidence.usage.as_row(),
        "access": {"flagged": access["flagged"], "matches": access["matches"]},
        "notes": dict(evidence.notes),
    }
    write_json(paths.done, record)
    return record
