"""Replay validation gate for the Autonomous Code Mini-Agent (ADR 0032 §5).

A persistent kernel hides state: out-of-order or overwritten cells mean the live
session reaching ``ReturnAnswer`` does NOT prove the result reproduces. So the
runner concatenates the *accepted* cells (in execution order) into a standalone
``analysis.py`` and re-runs it in a **fresh, isolated** kernel against the
recorded inputs. The run is only accepted if this replay also completes and
reaches ``ReturnAnswer``.

By default replay re-runs nested skills (faithful reproducibility); reusing
recorded nested outputs is an ADR open question deferred to a later version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile

from omicsclaw.common.output_claim import atomic_write_owned_output_text

from . import run_layout
from .budget import MiniAgentBudget
from .kernel_session import KernelSession, KernelStartError
from .mini_agent import ANSWER_FILE, build_init_code

REPLAY_SCRIPT = run_layout.relpath("analysis")


@dataclass(slots=True)
class ReplayResult:
    """Outcome of re-running the emitted analysis script."""

    ok: bool
    script_path: str
    error: str = ""
    answer: str = ""
    stdout: str = ""


def _replay_script_text(
    accepted_cells: list[str],
    input_paths: list[str],
    budget: MiniAgentBudget,
    *,
    output_root: Path,
    process_guard: bool = False,
) -> str:
    """Build the consolidated replay-script text, rooted at *output_root*.

    ``output_root`` is where the script's init writes figures / answer when run —
    the run workspace for the user-facing deliverable, a throwaway scratch dir for
    the validation re-run.
    """
    header = (
        "# OmicsClaw Autonomous Code Mini-Agent — replay script (ADR 0032).\n"
        "# Re-runs the accepted cells from the input to reproduce the result.\n"
        "# Generated; do not edit by hand.\n"
    )
    init = build_init_code(output_root, input_paths, budget, process_guard=process_guard)
    body = "\n\n".join(
        f"# === accepted step {i + 1} ===\n{cell}" for i, cell in enumerate(accepted_cells)
    )
    return f"{header}{init}\n\n{body}\n"


def emit_replay_script(
    workspace: Path,
    accepted_cells: list[str],
    input_paths: list[str],
    budget: MiniAgentBudget,
    *,
    replay_workspace: Path,
    process_guard: bool = False,
) -> Path:
    """Write the consolidated, re-runnable deliverable ``analysis.py``; return its path."""
    script = _replay_script_text(
        accepted_cells,
        input_paths,
        budget,
        output_root=replay_workspace,
        process_guard=process_guard,
    )
    path = workspace / REPLAY_SCRIPT
    return atomic_write_owned_output_text(
        path,
        output_root=workspace,
        text=script,
        label="autonomous replay script",
    )


def validate_replay(
    *,
    workspace: Path,
    accepted_cells: list[str],
    input_paths: list[str],
    budget: MiniAgentBudget,
    sandbox: bool = True,
    process_guard: bool = False,
    repo_root: Path | None = None,
) -> ReplayResult:
    """Re-run the accepted cells in a fresh kernel; gate run acceptance on it.

    The deliverable ``analysis.py`` is written at the run-workspace root, but its
    init points at a ``rerun/`` sibling (created lazily only if a user actually
    re-runs it), so a manual re-run never mixes new outputs with the original run's
    artifacts. The validation re-run executes the same accepted cells — differing
    only in output root — in a throwaway scratch dir, removed afterwards, so its
    figures / nested skill calls / answer sentinel never clutter the deliverable
    (ADR 0032). Reproducibility of the *result* is what the gate proves; the output
    location cannot affect it.

    ``analysis.py`` is written on every replay *attempt* (before validation runs),
    so it is present even when replay then fails — it is a reproduction record, not
    a "replay-passed" badge.
    """
    workspace = Path(workspace)
    script_path = emit_replay_script(
        workspace,
        accepted_cells,
        input_paths,
        budget,
        replay_workspace=workspace / run_layout.relpath("rerun"),
        process_guard=process_guard,
    )

    if not accepted_cells:
        return ReplayResult(ok=False, script_path=str(script_path), error="no accepted cells to replay")

    scratch = Path(tempfile.mkdtemp(prefix="ock-replay-"))
    try:
        validation_script = _replay_script_text(
            accepted_cells,
            input_paths,
            budget,
            output_root=scratch,
            process_guard=process_guard,
        )
        kwargs = {} if repo_root is None else {"repo_root": repo_root}
        session = KernelSession(
            workspace_root=scratch,
            read_roots=list(input_paths),
            sandbox=sandbox,
            startup_timeout=90,
            **kwargs,
        )
        try:
            session.start()
        except KernelStartError as exc:
            return ReplayResult(ok=False, script_path=str(script_path), error=f"replay kernel start failed: {exc}")

        try:
            cell = session.execute(validation_script, timeout=budget.wall_clock_seconds)
        finally:
            session.shutdown()

        answer = _read(scratch / ANSWER_FILE)
        if not cell.ok:
            return ReplayResult(
                ok=False,
                script_path=str(script_path),
                error=cell.error_summary or (cell.stderr or "")[-1000:] or "replay produced no result",
                stdout=(cell.stdout or "")[-2000:],
            )
        if not (scratch / ANSWER_FILE).exists():
            return ReplayResult(
                ok=False,
                script_path=str(script_path),
                error="replay completed without calling ReturnAnswer(...)",
                stdout=(cell.stdout or "")[-2000:],
            )
        return ReplayResult(
            ok=True,
            script_path=str(script_path),
            answer=answer,
            stdout=(cell.stdout or "")[-2000:],
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__all__ = ["REPLAY_SCRIPT", "ReplayResult", "emit_replay_script", "validate_replay"]
